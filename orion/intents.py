"""Intents: what an MCP server may push for Orion to do, such as the hand-gesture remote.

A server whose settings allow it (``mcp_servers.<name>.intents: true``, off by default) and that lists the resource
``orion://intents`` is watched by the hub (``mcp.py``): Orion subscribes to it, and each time the server says it was
updated, reads it and runs, in id order, the intents above the last id it handled from that server. Ids only grow
within a run of the server: it makes them from its clock in milliseconds, and says with each read what its clock is
(``now``) and which run this is (``instance``). So an intent older than ``FRESH_MS`` (``now`` minus its id) never
runs, whatever this PC's clock says; neither a reconnect nor a restart of Orion replays old gestures; and a server
that restarted (another instance, its ids maybe lower) is followed again from its first intent. A server that doesn't
say ``now`` makes its ids from this PC's clock. The resource is JSON (the last 20)::

    {"intents": [{"id": 1790000000500, "intent": "gesture", "gesture": "swipe_right", "args": {"kind": "motion",
                  "phase": "start", "held": 0}, "ts": 1790000000.47, "hand": "Right", "score": 0.93}, ...],
     "instance": "3f9a0c1b2d4e", "now": 1790000000620}

``gesture`` says only what the user did; what it does is the user's choice (``orion.gestures``: their bindings for
that server, or the defaults), and a held hand shape fires once held as long as its binding asks (then again at each
hold step when it repeats). A server may also ask for one of a few actions itself (``orion.gestures.PUSHED``: the
screen, the media and the volume, the microphone, spoken replies, a new conversation, something to ask him), named
and written as a binding's action is (``{"intent": "media", "args": {"action": "next"}}``); anything else, or
arguments that don't fit, is ignored with a debug log. Never a tool: only a binding the user chose reaches one.

Every action but asking him something runs an MCP tool through the hub: his own for the screen, the microphone,
spoken replies and a new conversation, the system's for the media and the volume, the organizer's for a timer, any
server's for a binding to a tool. So each is one traced operation of that server's app (its source "app", its
client the server's name, e.g. "hands"), and a tool switched off in Abilities does nothing by gesture either. Each
is announced to the HUD as an ``intent`` event (``IntentRunner.perform``) and logs one line.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Protocol

from orion import images
from orion.base import CORE_SERVER, JSON, CallContext, number
from orion.gestures import ACTIONS, PUSHED, Binding, BindingError, Firing, bindings_of, parse_binding
from orion.gestures import describe as said

if TYPE_CHECKING:
    from orion.base import EventBus
    from orion.config import Config

log = logging.getLogger("orion.intents")

INTENTS_URI: Final = "orion://intents"
FEED_MAX: Final = 200  # entries read from one resource at most (the contract keeps the last 20)
FRESH_MS: Final = 10_000  # an intent older than this (the server's clock minus its id, in ms) never runs


@dataclass(frozen=True, slots=True)
class Intent:
    """One entry of a server's intents feed.

    Attributes:
        id: its number; each new intent has a higher one (the hands tracker: its clock, in milliseconds).
        name: what to do ("gesture", "show", "say"...).
        args: its arguments.
        gesture: what the user did to ask for it ("swipe_right"), "" when unsaid.
        hand: which hand ("Right", "Left"), "" when unsaid.
    """

    id: int
    name: str
    args: Mapping[str, JSON]
    gesture: str = ""
    hand: str = ""


@dataclass(frozen=True, slots=True)
class Feed:
    """An intents resource as read.

    Attributes:
        intents: the entries with the contract's shape, by id.
        instance: the server's run ("" when it doesn't say): another one means it restarted, and its ids start over.
        now: its clock when read, in milliseconds (None when it doesn't say: its ids come from this PC's clock).
    """

    intents: list[Intent]
    instance: str
    now: float | None


def parse_feed(text: str) -> Feed:
    """An intents resource: its entries that have the contract's shape, by id, and the server's run and clock.

    Args:
        text: the resource's JSON text.

    Returns:
        The entries with an integer id and a string intent, sorted by id; their arguments (an object, or none),
        gesture and hand (text, or none) are taken when they fit and left empty when they don't. Anything else
        (``ts``, ``score``) is left out; not JSON, or no ``intents`` list: none.
    """
    try:
        data = json.loads(text)
    except ValueError:
        return Feed([], "", None)
    entries = data.get("intents") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return Feed([], "", None)
    found: dict[int, Intent] = {}
    for entry in entries[-FEED_MAX:]:
        if not isinstance(entry, dict):
            continue
        ident, name, args = entry.get("id"), entry.get("intent"), entry.get("args") or {}
        if not isinstance(ident, int) or isinstance(ident, bool) or not isinstance(name, str):
            continue
        if not isinstance(args, dict):
            args = {"": args}  # kept, so validation refuses it with the reason, rather than read as no arguments
        found[ident] = Intent(ident, name, args, _text(entry.get("gesture")), _text(entry.get("hand")))
    return Feed([found[ident] for ident in sorted(found)], _text(data.get("instance")), number(data.get("now")))


def _text(value: object) -> str:
    """A short text field, or "" for anything else."""
    return value[:80] if isinstance(value, str) else ""


def due(feed: Feed, last: int, now_ms: float) -> tuple[list[Intent], int]:
    """What to run from a feed, and the last id handled after it.

    Args:
        feed: the feed as read.
        last: the last id handled from that server's run (0 for none yet).
        now_ms: this PC's clock, in milliseconds (what the ids are compared with when the feed doesn't say its own).

    Returns:
        (the entries above ``last`` and younger than ``FRESH_MS`` by their id, in order; the new last id: the highest
        above ``last``, run or not, so nothing is looked at twice).
    """
    clock = feed.now if feed.now is not None else now_ms
    above = [intent for intent in feed.intents if intent.id > last]
    return ([intent for intent in above if clock - intent.id < FRESH_MS],
            max((intent.id for intent in above), default=last))


def pushed(intent: Intent) -> Binding | str:
    """An action a server asks for itself (not a gesture), as the binding that does it; or why it is ignored.

    Args:
        intent: the entry: its name is the action, its arguments the action's options.

    Returns:
        The binding, or the reason it is ignored.
    """
    if intent.name not in PUSHED:
        return f"{intent.name!r} is not an action a server may ask for"
    if extra := sorted(intent.args.keys() & {"do", "hold", "repeat"}):
        return f"{intent.name} takes only its options, not {', '.join(extra)}"
    try:
        return parse_binding({**intent.args, "do": intent.name})
    except BindingError as e:
        return str(e)


def _with_gesture(gesture: str, what: str) -> str:
    """What was done, after the gesture that asked for it when there was one."""
    gesture = gesture.replace("_", " ").strip()
    return f"{gesture[:1].upper()}{gesture[1:]} → {what}" if gesture else what[:1].upper() + what[1:]


class IntentHost(Protocol):
    """What running an intent needs of the running Orion (``runtime.Runtime`` provides it)."""

    cfg: Config
    bus: EventBus

    @property
    def hub(self) -> Any:
        """The MCP hub (its ``call`` runs a tool, traced; its ``ops`` is the operation trace)."""

    @property
    def voice(self) -> Any:
        """The voice session (None while voice input is off)."""

    @property
    def turns(self) -> Any:
        """The turn runner."""


class IntentRunner:
    """Runs the intents a server pushes, one at a time, each traced and logged (see the module docstring)."""

    def __init__(self, host: IntentHost) -> None:
        """Hold the running Orion.

        Args:
            host: the running Orion.
        """
        self.host = host
        self._firing: dict[str, Firing] = {}  # each server's gestures: their showings, whether they're awake

    def awake_for(self, server: str) -> float:
        """How many more seconds a server's gestures stay awake (0 when they aren't, or need no waking)."""
        firing = self._firing.get(server)
        return max(0.0, firing.awake_until - time.monotonic()) if firing is not None else 0.0

    async def run(self, server: str, intent: Intent) -> None:
        """Run one intent from a server; never raises.

        Args:
            server: the server's name (the trace's client, e.g. "hands").
            intent: the entry.
        """
        if intent.name == "gesture":
            await self._gesture(server, intent)
            return
        if intent.name == "message":
            await self._message(server, intent)
            return
        binding = pushed(intent)
        if isinstance(binding, str):
            log.debug("intent #%d from %s ignored: %s", intent.id, server, binding)
            return
        await self.perform(server, binding, intent)

    # ------------------------------------------------------------------------------------------------ messages
    async def _message(self, server: str, intent: Intent) -> None:
        """A message the user sent through a chat server (Discord): run it as a turn of the conversation (answered
        there, not spoken here), then answer it with the tool the message names (``reply``: its tool on the same
        server, and that tool's arguments). A voice message is heard first; pictures go with the request."""
        host, args = self.host, intent.args
        text = str(args.get("text") or "").strip()[:host.cfg.hud.max_message_chars]
        audio = args.get("audio")
        if isinstance(audio, str) and audio.startswith("data:"):
            try:
                heard = await host.voice.transcribe(base64.b64decode(audio.split(",", 1)[1]))
            except Exception as e:  # noqa: BLE001 (a voice message that can't be heard is said so, the rest goes on)
                log.warning("a voice message from %s couldn't be heard: %s", server, e)
                heard = ""
            text = "\n".join(part for part in (text, heard) if part)
        pictures = []
        eyes = host.cfg.eyes
        for item in args.get("images") or ():
            if isinstance(item, str):
                try:
                    pictures.append(images.from_base64(item, label=f"a picture sent on {server}",
                                                       max_side=eyes.image_max_side, quality=eyes.image_quality))
                except ValueError:
                    log.info("a picture from %s couldn't be read", server)
        reply = args.get("reply")
        if not text and not pictures:
            answer = "I couldn't make out that message." if isinstance(audio, str) else ""
        else:
            result = await host.turns.handle(text or "What do you make of this?", "discord", device=server,
                                             pictures=pictures)
            answer = result.text or "…"
        if answer and isinstance(reply, dict) and isinstance(reply.get("tool"), str):
            reply_args = reply.get("args") if isinstance(reply.get("args"), dict) else {}
            said = await host.hub.call(server, str(reply["tool"]), {**reply_args, "text": answer},
                                       CallContext("hud", client=server))
            if said.startswith("Error"):
                log.warning("the answer couldn't go back to %s: %s", server, said)

    # ------------------------------------------------------------------------------------------------ gestures
    async def _gesture(self, server: str, intent: Intent) -> None:
        """A gesture: run what the user bound to it, once its hold is met (and again at each hold step if it repeats),
        unless gestures are asleep."""
        args = intent.args
        cfg = self.host.cfg
        bindings, _ = bindings_of(cfg.mcp_servers.get(server) or {}, cfg.gestures.defaults)
        binding = bindings.get(intent.gesture)
        if binding is None:
            log.debug("gesture %s from %s: bound to nothing", intent.gesture or "?", server)
            return
        held = number(args.get("held")) or 0.0
        showing = args.get("showing") if isinstance(args.get("showing"), int) and args.get("kind") == "shape" else None
        armed = next((b for b in bindings.values() if b.do == "arm"), None)
        firing = self._firing.setdefault(server, Firing())
        decision = firing.decide(binding, phase=str(args.get("phase") or "start"), held=held, showing=showing,
                                 armed=armed)
        if decision == "asleep":
            self.host.bus.publish("intent", id=intent.id, intent="asleep", args={}, gesture=intent.gesture,
                                  hand=intent.hand, source=server, ok=False,
                                  text=_with_gesture(intent.gesture, "gestures are asleep"),
                                  result="Make the waking gesture first.")
            return
        if decision not in ("run", "again"):
            return
        await self.perform(server, binding, intent, again=decision == "again")

    async def perform(self, server: str, binding: Binding, intent: Intent, *, again: bool = False) -> tuple[bool, str]:
        """Do what a binding says, traced, announced to the HUD and logged; never raises.

        Args:
            server: the server whose gesture (or pushed intent) it is.
            binding: what to do.
            intent: the gesture that asked for it, or the intent itself.
            again: a repeat of the same showing (a thumb still up): the HUD updates what it shows, and the log keeps
                it out of the way.

        Returns:
            (whether it was done, what happened).
        """
        ctx = CallContext("app", client=server)
        started = time.monotonic()
        params = dict(binding.params)
        ok, result, traced = False, "Error: stopped before it finished.", False
        try:
            ok, result, traced = await self._do(binding, ctx)
        except Exception as e:  # an intent never takes the watcher down
            log.warning("%s %s from %s failed", binding.do, intent.gesture or "intent", server, exc_info=True)
            ok, result = False, f"Error: {e}"
        finally:
            shown: dict[str, JSON] = {**_plain(params), "gesture": intent.gesture}
            if not traced:
                self.host.hub.ops.record(server=server, name=binding.do, title=ACTIONS[binding.do].title, ctx=ctx,
                                         args=shown, ok=ok, result=result,
                                         ms=round((time.monotonic() - started) * 1000))
            self.host.bus.publish("intent", id=intent.id, intent=binding.do, args=_plain(params),
                                  gesture=intent.gesture, hand=intent.hand, source=server, ok=ok,
                                  text=_with_gesture(intent.gesture, said(binding)), result=result,
                                  awake=round(self.awake_for(server), 1), showing=intent.args.get("showing"),
                                  again=again)
        (log.debug if again else log.info)("from %s: %s -> %s %s (%s)", server, intent.gesture or "an intent",
                                           binding.do, json.dumps(_plain(params)) if params else "",
                                           result if ok else f"refused: {result}")
        return ok, result

    async def _do(self, binding: Binding, ctx: CallContext) -> tuple[bool, str, bool]:
        """Do one binding's action; (whether it was done, what happened, whether the hub traced it already)."""
        host, p = self.host, binding.params
        match binding.do:
            case "say":
                host.turns.submit(str(p["text"])[:host.cfg.hud.max_message_chars], "hud")
                return True, "Sent as if typed.", False
            case "skill":
                host.turns.submit(f"Run your {p['name']} skill.", "hud")
                return True, f"Asked him to run {p['name']}.", False
            case "arm":
                return True, f"Gestures are awake for {p['seconds']:g} s.", False
        target, tool, args = await self._tool(binding, ctx)
        result = await host.hub.call(target, tool, args, ctx)
        return not result.startswith("Error"), result, True

    async def _tool(self, binding: Binding, ctx: CallContext) -> tuple[str, str, dict[str, JSON]]:
        """The MCP tool that does a binding's action: (its server, its name, its arguments). A toggle reads what is
        on now: the microphone, the spoken replies, the sound (a mute of sound already off unmutes it)."""
        host, p = self.host, binding.params
        match binding.do:
            case "show" | "hide":
                return CORE_SERVER, f"hud_{binding.do}", {"parts": list(p["panels"])}
            case "toggle":
                return CORE_SERVER, "hud_show", {"parts": [p["panel"]], "toggle": True}
            case "dismiss":
                return CORE_SERVER, "hud_hide", {"parts": [], "newest": True}
            case "mic":
                voice = host.voice
                muted = not voice.muted if p["action"] == "toggle" and voice is not None else p["action"] != "unmute"
                return CORE_SERVER, "microphone", {"muted": muted}
            case "spoken":
                return CORE_SERVER, "spoken_replies", {"on": host.turns.quiet if p["action"] == "toggle"
                                                       else p["action"] == "on"}
            case "new_chat":
                return CORE_SERVER, "new_conversation", {}
            case "media":
                return "system", "media", {"action": p["action"]}
            case "volume":
                action = str(p["action"])
                if action == "mute":
                    now = await host.hub.call("system", "volume", {"action": "get"}, ctx)
                    action = "unmute" if "(muted)" in now else "mute"
                return "system", "volume", {"action": action}
            case "timer":
                return "organizer", "set_timer", {"duration": f"{p['minutes']:g} minutes"}
            case "tool":
                return str(p["server"]), str(p["tool"]), dict(p["args"])
        raise BindingError(f"{binding.do} is not an action Orion knows")  # pragma: no cover


def _plain(params: Mapping[str, Any]) -> dict[str, JSON]:
    """A binding's options as JSON values (its tuples as lists)."""
    return {key: list(value) if isinstance(value, tuple) else value for key, value in params.items()}
