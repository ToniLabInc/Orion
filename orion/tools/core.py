"""Orion's own controls, written once: ``Controls`` (the HUD's REST endpoints call it as "hud", his core tools as
"ai") and the in-process MCP server of his core tools.

What the HUD can do, the AI can do too, except widen what he can do behind the user's back (``tools.policy``): he
switches tools and servers off but never back on, never sets or moves a server's tokens, never fills settings in from
the environment, adds a server run by a command only while run_command is on (one nobody reviewed starts in the
sandbox, and the user alone says otherwise), and changes only the settings on the AI allow-list, and adds, changes or
removes a server or a plugin only with the user's yes (``ask_first``). A change that would cut short the answer he is
giving (a new conversation, another brain, a restart)
waits until that answer is over.

The server lives inside Orion's process, bound to the running Runtime; the hub reaches it directly, without a pipe.
"""

import asyncio
import contextlib
import inspect
import json
import re
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal, Protocol

from pydantic import Field

from orion.base import JSON, Actor, GroupInfo, number, server_on, switched_off
from orion.config import Apply, Refused, ai_sections, ai_view, keys, meta, normalize_patch, public_view
from orion.gestures import (
    ACTIONS,
    MAX_HOLD_S,
    Param,
    bindings_of,
    check_bindings,
    choice_words,
    default_bindings,
    describe,
    fresh,
    gesture_remotes,
    hold_words,
    hud_parts,
    part_words,
)
from orion.llm import label
from orion.service import OllamaError, tagged
from orion.timing import STATS_TURNS, in_words, stats
from orion.tools import GROUPS, Group, ToolsServer, build_server, current_call
from orion.tools.policy import Refusal, mask_secrets, server_change_refusal, tool_switch_refusal
from orion.voice import voice_cards
from orion.voice.wake import EVERYTHING, HUD_PARTS, SETTINGS_TABS

if TYPE_CHECKING:
    from orion.activity import Activity
    from orion.base import EventBus, StepRecord
    from orion.config import Config, Settings
    from orion.confirm import Confirmer
    from orion.connectors import Connectors
    from orion.mcp import MCPHub, OpTrace, ServerStatus
    from orion.memory import MemorySystem
    from orion.memory.review import Reviewer
    from orion.plugins import Library
    from orion.service import Ollama
    from orion.voice.session import VoiceSession

type ExitAction = Literal["stop", "restart"]
type HudAction = Literal["show", "hide", "toggle", "dismiss"]  # a summon: a toggle flips its part, dismiss the newest

CORE: Final = GroupInfo("orion", "ORION himself", "Orion's own controls: his settings, voice, "
                                                           "microphone, brain and models, MCP servers and tools, the "
                                                           "conversation, and restarting or shutting down Orion "
                                                           "(not the PC).")
EXIT_DELAY_S: Final = 0.5  # the HUD hears "shutting down" before the process goes
BACKUP_WAIT_S: Final = 45  # backup_now answers after this long even if the backup is still going (a call has 60 s)
RECENT_MAX: Final = 50  # recent_tool_calls lists at most this many
RECENT_ARGS_CHARS: Final = 120  # ...each call's arguments cut to this
RECENT_RESULT_CHARS: Final = 200  # ...and its answer to this
ACTIVITY_MAX: Final = 50  # activity_recent lists at most this many
SERVER_NAME: Final = re.compile(r"[a-z0-9][a-z0-9_-]{0,40}")  # names of the servers the user or the AI adds
GROUP_FIELDS: Final = frozenset({"enabled", "disabled_tools"})  # all a built-in group's settings may hold
DEFERRED: Final = Apply.LLM | Apply.OLLAMA_UNIT  # changes the AI asks for that wait for the answer to end
WHERE: Final = {"claude": "your Claude plan (Claude Code)", "ollama": "this PC (Ollama)", "openrouter": "OpenRouter",
                "script": "a script (no model: script.path)"}
HIDDEN: Final = "(web or app content, not shown)"  # what recent_tool_calls says instead of an untrusted answer
VOCABULARY_URI: Final = "orion://gestures"  # a gesture remote's list of the gestures it recognises
GESTURES_PAGE: Final = "in the HUD, on the remote's page in Customize, Connectors"
NO_ONE_TO_ASK: Final = {"discord": "Discord", "job": "a job"}  # where nobody is there to say yes to a change
ASK_ARG_CHARS: Final = 160  # a plugin's description in the question about installing it
SEARCH_SHOWN: Final = 15  # the plugins a search lists
AI_ACTOR: Final = "the brain, with your yes"  # the activity log's actor for what he does once the user said yes


class _Turns(Protocol):
    """What the controls need of the turn runner (``brain.TurnRunner``)."""

    quiet: bool
    hud_open: Callable[[], bool]  # whether a HUD is connected

    @property
    def busy(self) -> bool:
        """Whether a turn is running."""

    @property
    def loading(self) -> str:
        """The brain being switched to ("" when none is loading)."""

    async def idle(self) -> None:
        """Return once no turn runs and its reply has been said."""

    def new_conversation(self) -> None:
        """Start a new conversation."""


class _Supervisor(Protocol):
    """What the controls need of the runtime's supervisor."""

    def spawn(self, job: Awaitable[Any], *, name: str) -> "asyncio.Task[Any]":
        """Run background work, kept referenced and logged if it fails."""


class RuntimeFacade(Protocol):
    """The parts of the running Orion the controls use (``runtime.Runtime`` provides them)."""

    cfg: "Config"
    settings: "Settings"
    bus: "EventBus"
    hub: "MCPHub"
    ops: "OpTrace"
    memory: "MemorySystem"
    ollama: "Ollama"
    turns: _Turns
    voice: "VoiceSession | None"
    reviewer: "Reviewer | None"
    supervisor: _Supervisor
    activity: "Activity"  # the log of what he did and who allowed it
    confirmer: "Confirmer"
    connectors: "Connectors"
    library: "Library"
    skills: Any  # his skills (``all()`` names them)
    intents: Any  # the intent runner (``awake_for`` says how long a remote's gestures stay awake)
    backups: Any  # his backups (``orion.backup.Backups``)
    documents: Any  # his documents (``orion.documents.Documents``)

    @property
    def brain_error(self) -> str:
        """Why the brain is not ready ("" when it is)."""

    async def apply_settings(self, patch: Mapping[str, Any], *, actor: Actor) -> dict[str, JSON]:
        """Validate (with the live choices), save and apply a settings change; returns ``{changed, restart}``.

        Raises:
            Refused: the change is invalid or not the actor's to make.
        """

    async def validate_settings(self, patch: Mapping[str, Any], *, actor: Actor) -> dict[str, Any]:
        """Validate a settings change (with the live choices) without saving it; returns the keys that change.

        Raises:
            Refused: the change is invalid or not the actor's to make.
        """

    async def apply_servers(self, servers: Mapping[str, Any], *, actor: Actor) -> dict[str, JSON]:
        """Save ``mcp_servers`` whole (the controls have applied the server policy) and reconfigure the hub."""

    def set_muted(self, muted: bool, minutes: int = 0) -> None:
        """Mute or unmute the microphone (unmuting by itself after ``minutes``, when more than 0)."""

    def request_exit(self, action: ExitAction) -> None:
        """Stop or restart Orion."""


@contextlib.contextmanager
def refusals() -> Iterator[None]:
    """Something Orion won't or can't do (``Refused``, another ValueError or LookupError, an ``OllamaError``)
    reaches the model as just its reason.

    Raises:
        ToolError: the reason, without a traceback or the exception's name.
    """
    from mcp.server.mcpserver.exceptions import ToolError

    try:
        yield
    except (ValueError, LookupError, OllamaError) as e:
        raise ToolError(str(e)) from None


def enforce(refusal: Refusal | None) -> None:
    """Act on a ``tools.policy`` decision: nothing when there is no refusal.

    Raises:
        Refused: its reason.
    """
    if refusal is not None:
        raise Refused(refusal.reason)


def is_builtin(name: str) -> bool:
    """Whether a server is one of Orion's own: a group of the tools process, or the in-process orion
    server."""
    return name in GROUPS or name == CORE.name


class Controls:
    """Operations on the running Orion; each takes its actor and applies ``tools.policy``."""

    def __init__(self, rt: RuntimeFacade) -> None:
        """Hold the runtime.

        Args:
            rt: the running Orion.
        """
        self.rt = rt
        self._known = frozenset(keys())
        self._said: dict[str, dict[str, JSON]] = {}  # what each remote said of its gestures, last read

    # ------------------------------------------------------------------------------------------------ settings
    async def change_settings(self, patch: Mapping[str, Any], *, actor: Actor) -> dict[str, JSON]:
        """Change settings. For the AI, a change that swaps the brain waits until the answer under way is over.

        Args:
            patch: the change, as sections of options or as flat keys.
            actor: who asks.

        Returns:
            ``{changed, restart}``; for a change made later ``{changed: [], restart: [], deferred: [keys]}``.

        Raises:
            Refused: the change is invalid or not the actor's to make (a deferred change is checked now too).
        """
        flat = normalize_patch(patch)
        if actor != "hud" and any(key in self._known and meta(key).apply & DEFERRED for key in flat):
            changes = await self.rt.validate_settings(patch, actor=actor)
            if not changes:
                return {"changed": [], "restart": []}
            self.after_reply(lambda: self.rt.apply_settings(patch, actor=actor))
            return {"changed": [], "restart": [], "deferred": sorted(changes)}
        return await self.rt.apply_settings(patch, actor=actor)

    def masked_settings(self) -> dict[str, JSON]:
        """The settings for showing them: no prompts, every secret hidden."""
        cfg = self.rt.cfg
        return mask_secrets(public_view(cfg), openrouter_key_set=bool(cfg.openrouter.key))

    # ------------------------------------------------------------------------------------------------ servers
    async def ask_first(self, tool: str, title: str, args: Mapping[str, JSON], *, actor: Actor) -> None:
        """The user's yes before the AI changes their connectors or plugins: asked where the request came from (the
        HUD, aloud, ...), never from Discord or a job, and no answer is no. The user's own changes are not asked about.

        Args:
            tool: the tool asking ("mcp_servers", "plugins").
            title: what it would do ("Install a plugin").
            args: what exactly, as the HUD shows it (the question names the first of command, url, name).
            actor: who changes it.

        Raises:
            Refused: the request came from Discord or a job, the user said no, or didn't answer in time.
        """
        if actor == "hud":
            return
        call = current_call()
        if call.source in NO_ONE_TO_ASK:
            raise Refused("Only the user changes connectors and plugins, in the HUD under Customize: it isn't done "
                          f"from {NO_ONE_TO_ASK[call.source]}.")
        confirmer = self.rt.confirmer
        question = confirmer.question(turn=call.turn, tool=tool, title=title, server=CORE.name, args=args,
                                      reason="always", source="", source_kind=call.source, device=call.device)
        match await confirmer.ask(question):
            case "yes":
                return
            case "no":
                raise Refused("The user said no, so it wasn't done. Don't try another way to do the same thing: say it "
                              "wasn't done and ask what they would like instead.")
            case _:
                raise Refused("The user didn't answer in time, so it wasn't done. Don't try another way to do the same "
                              "thing: say it needs their OK and wasn't done.")

    async def switch_tool(self, server: str, tool: str, enabled: bool, *, actor: Actor) -> dict[str, JSON]:
        """Switch one tool on or off: nothing restarts, and other apps stop (or start) being lent it too.

        Args:
            server: the server it belongs to.
            tool: its name, as the model or its server calls it.
            enabled: on or off.
            actor: who asks.

        Returns:
            The settings result.

        Raises:
            Refused: there is no such tool, or the AI asked to switch one back on.
        """
        entry = self._status(server)
        found = next((t for t in entry["tools"] if tool in (t["name"], t["remote"])), None)
        if found is None:
            raise Refused(f"The {server} server has no tool called {tool}.")
        enforce(tool_switch_refusal(enabled=enabled, actor=actor, server_enabled=entry["enabled"],
                                    tool_disabled=not found["enabled"]))
        settings = dict(self.rt.cfg.mcp_servers.get(server) or {})
        off = switched_off(settings)
        off = off - {found["remote"]} if enabled else off | {found["remote"]}
        settings.pop("disabled_tools", None)
        if off:
            settings["disabled_tools"] = sorted(off)
        return await self._save(server, settings, actor=actor)

    async def switch_server(self, name: str, enabled: bool, *, actor: Actor) -> dict[str, JSON]:
        """Switch a whole server on or off, keeping its switched-off tools, env and headers.

        Args:
            name: the server.
            enabled: on or off.
            actor: who asks.

        Returns:
            The settings result.

        Raises:
            Refused: there is no such server, or the AI asked to switch one back on.
        """
        self._status(name)
        current = dict(self.rt.cfg.mcp_servers.get(name) or {})
        enforce(server_change_refusal(action="enable" if enabled else "disable", actor=actor,
                                      builtin=is_builtin(name), old=current or None, new=None,
                                      run_command_on=self._run_command_on(), name=name))
        settings = {key: value for key, value in current.items() if key != "enabled"}
        if not enabled:
            settings["enabled"] = False
        return await self._save(name, settings, actor=actor)

    async def save_server(self, name: str, settings: Mapping[str, Any], *, actor: Actor) -> dict[str, JSON]:
        """Add a server or replace its settings (a built-in group holds only enabled and disabled_tools).

        Args:
            name: the server.
            settings: its complete settings; empty values are left out.
            actor: who asks.

        Returns:
            The settings result.

        Raises:
            Refused: a bad name or setting, or a change that is not the actor's to make.
        """
        from orion.connectors import is_reserved

        builtin = is_builtin(name)
        exists = builtin or name in self.rt.cfg.mcp_servers
        if not exists and not SERVER_NAME.fullmatch(name):
            raise Refused("Server names use lower-case letters, digits, - and _, up to 41 characters.")
        if not exists and actor != "hud" and is_reserved(name):
            raise Refused(f"{name} is a name Orion keeps for his own connectors and the directory's: pick another.")
        cleaned = {key: value for key, value in settings.items() if value not in (None, "", [], {})}
        if cleaned.get("enabled") is True:
            del cleaned["enabled"]  # on is the default
        current = dict(self.rt.cfg.mcp_servers.get(name) or {})
        enforce(server_change_refusal(action="update" if exists else "add", actor=actor, builtin=builtin,
                                      old=current or None, new=cleaned, run_command_on=self._run_command_on(),
                                      name=name))
        if builtin and (extra := sorted(cleaned.keys() - GROUP_FIELDS)):
            raise Refused(f"{name} is one of Orion's own: it takes only enabled and disabled_tools, not "
                          f"{', '.join(extra)}.")
        if not builtin:
            shown = {key: value for key, value in cleaned.items() if key in ("command", "args", "url", "description")}
            await self.ask_first("mcp_servers", "Change an MCP server" if exists else "Add an MCP server",
                                 {"name": name, **shown}, actor=actor)
        return await self._save(name, cleaned, actor=actor)

    async def remove_server(self, name: str, *, actor: Actor) -> dict[str, JSON]:
        """Remove an added server.

        Args:
            name: the server.
            actor: who asks.

        Returns:
            The settings result.

        Raises:
            Refused: there is no such server, or it is one of Orion's own.
        """
        if is_builtin(name):
            raise Refused(f"{name} is one of Orion's own: switch it off instead.")
        if name not in self.rt.cfg.mcp_servers:
            raise Refused(f"There's no MCP server called {name!r}.")
        if plugin := (self.rt.cfg.mcp_servers[name] or {}).get("plugin"):
            raise Refused(f"{name} comes with the {plugin} plugin: remove the plugin, or switch {name} off.")
        enforce(server_change_refusal(action="remove", actor=actor, builtin=False,
                                      old=dict(self.rt.cfg.mcp_servers[name]), new=None,
                                      run_command_on=self._run_command_on(), name=name))
        await self.ask_first("mcp_servers", "Remove an MCP server", {"name": name}, actor=actor)
        return await self._save(name, {}, actor=actor)

    async def restart_server(self, name: str, *, actor: Actor) -> None:
        """Connect a server again (for a built-in group, the tools process they share restarts).

        Args:
            name: the server.
            actor: who asks.

        Raises:
            Refused: there is no such server, or it is the in-process orion server.
            ValueError: the server is not running (switched off).
        """
        self._status(name)
        enforce(server_change_refusal(action="restart", actor=actor, builtin=is_builtin(name), old=None,
                                      new=None, run_command_on=self._run_command_on(), name=name))
        await self.rt.hub.restart(name)

    # ------------------------------------------------------------------------------------------------ gestures
    async def vocabulary(self, server: str) -> tuple[dict[str, JSON], str]:
        """What a remote says of its gestures, read from it now: ``{"gestures", "static_hold", "hold_steps",
        "two_hands"}`` (the gestures it recognises, how long a shape is held before it counts, the hold steps it
        reports a held shape at, and the distances in palm lengths that make hands_together and hands_apart; None for
        what it doesn't say).

        Returns:
            (what it said, "" or why it can't say: then what it said last, if it ever did).
        """
        try:
            text = await self.rt.hub.read_resource(server, VOCABULARY_URI)
            doc = json.loads(text)
        except Exception as e:  # noqa: BLE001 (not connected, an older remote, a timeout: all said the same way)
            return self.said(server), str(e) or type(e).__name__
        gestures = doc.get("gestures") if isinstance(doc, dict) else None
        if not isinstance(gestures, list):
            return self.said(server), "it sent no list of gestures"
        listed: list[JSON] = [{key: str(g.get(key) or "") for key in ("name", "title", "kind", "how")}
                              for g in gestures if isinstance(g, dict) and isinstance(g.get("name"), str)]

        def hold(value: object) -> float | None:
            """A hold it says, in seconds (up to the longest a binding may ask for); None for anything else."""
            seconds = number(value)
            return seconds if seconds is not None and 0 < seconds <= MAX_HOLD_S else None

        steps: list[JSON] = [step for value in doc.get("hold_steps") or [] if (step := hold(value)) is not None]
        pair = doc.get("two_hands") if isinstance(doc.get("two_hands"), dict) else {}
        together, apart = number(pair.get("together")), number(pair.get("apart"))
        two: JSON = {"together": together, "apart": apart} if together is not None and apart is not None else None
        said: dict[str, JSON] = {"gestures": listed, "hold_steps": steps, "two_hands": two,
                                 "static_hold": hold(doc.get("static_hold"))}
        if listed:
            self._said[server] = said
        return said, "" if listed else str(doc.get("error") or "it recognises no gestures")

    def said(self, server: str) -> dict[str, JSON]:
        """What a remote said of its gestures when last read (nothing, when it never did)."""
        return self._said.get(server, {"gestures": [], "hold_steps": [], "two_hands": None, "static_hold": None})

    async def gestures_page(self) -> dict[str, JSON]:
        """Everything a gesture remote's settings show: each remote with its gestures and what they do, the defaults,
        the actions a gesture can take with their options and what each starts with, and what those options can be
        (the HUD's parts, his skills, every tool switched on)."""
        cfg, hub = self.rt.cfg, self.rt.hub
        status = {entry["name"]: entry for entry in hub.status()}
        remotes: list[JSON] = []
        for name in gesture_remotes(cfg.mcp_servers, on_only=False):
            settings = cfg.mcp_servers.get(name) or {}
            bindings, problems = bindings_of(settings, cfg.gestures.defaults)
            entry = status.get(name)
            if entry is not None and entry["status"] == "connected":
                said, why = await self.vocabulary(name)
            else:
                said, why = self.said(name), "it isn't connected"
            remotes.append({
                "name": name, "status": entry["status"] if entry else "disabled",
                "enabled": server_on(settings), "error": entry["error"] if entry else "",
                "custom": "gestures" in settings, **said,
                "vocabulary_error": why, "bindings": {gesture: b.as_dict() for gesture, b in bindings.items()},
                "problems": problems, "words": {gesture: describe(b) for gesture, b in bindings.items()},
                "awake": round(self.rt.intents.awake_for(name), 1),
            })
        usable = {(entry["name"], tool["remote"]) for entry in status.values() if entry["enabled"]
                  for tool in entry["tools"] if tool["enabled"]}
        tools: list[JSON] = [
            {"server": server, "name": tool.remote, "title": tool.title, "description": tool.summary,
             "destructive": tool.hints.get("destructive") is True, "schema": tool.parameters}
            for server, connected, tool in hub.offered() if connected and (server, tool.remote) in usable]
        skills = sorted(self.rt.skills.all())
        offered = [(str(tool["server"]), str(tool["name"])) for tool in tools if isinstance(tool, dict)]
        return {
            "remotes": remotes,
            "defaults": {gesture: b.as_dict() for gesture, b in default_bindings(cfg.gestures.defaults).items()},
            "actions": {name: {"title": a.title, "group": a.group, "repeats": a.repeats,
                               "params": [{"name": p.name, "kind": p.kind, "choices": self._choices(name, p),
                                           "required": p.required,
                                           "default": list(p.default) if isinstance(p.default, tuple) else p.default}
                                          for p in a.params],
                               "start": fresh(name, cfg.gestures.starts, skills=skills, tools=offered)}
                        for name, a in ACTIONS.items()},
            "parts": [{"value": part, "label": part_words(part)} for part in HUD_PARTS],
            "skills": skills,
            "tools": tools,
            "pause_minutes": cfg.gestures.pause_minutes,
        }

    def _choices(self, do: str, param: Param) -> list[JSON]:
        """What an option of an action can be, each in words: its own choices, or the timers and waking times
        config.yaml offers (``gestures.timer_minutes``, ``gestures.awake_seconds``); [] for a free one."""
        g = self.rt.cfg.gestures
        match param.kind:
            case "choice":
                return [{"value": choice, "label": choice_words(do, choice)} for choice in param.choices]
            case "minutes":
                return [{"value": minutes, "label": f"{minutes} min"} for minutes in g.timer_minutes]
            case "seconds":
                return [{"value": seconds, "label": f"Awake {seconds} s"} for seconds in g.awake_seconds]
        return []

    async def save_gestures(self, server: str, bindings: object, *, actor: Actor) -> dict[str, JSON]:
        """Set what each of a remote's gestures does (the whole set: a gesture left out, or set to null, does nothing;
        a wrong binding already in config.yaml stays until the set names its gesture).

        Args:
            server: the gesture remote.
            bindings: gesture -> binding (``orion.gestures``), or None for nothing.
            actor: who asks; only the user may.

        Returns:
            The settings result.

        Raises:
            Refused: not the user, no such remote, or a binding that is wrong.
        """
        current = self._remote(server, actor)
        try:
            checked = check_bindings(bindings)
        except ValueError as e:
            raise Refused(str(e)) from None
        # a binding in config.yaml that is wrong (the page shows it as not used) stays, unless this set names its
        # gesture (null for nothing): a change made here never drops a typo there you haven't seen
        before = current.get("gestures")
        kept = {}
        if isinstance(before, dict) and isinstance(bindings, dict):
            _, problems = bindings_of(current, self.rt.cfg.gestures.defaults)
            kept = {gesture: before[gesture] for gesture in problems if gesture in before and gesture not in bindings}
        current["gestures"] = {**kept, **checked}
        return await self._save(server, current, actor=actor)

    async def reset_gestures(self, server: str, *, actor: Actor) -> dict[str, JSON]:
        """A remote's gestures do the defaults again.

        Raises:
            Refused: not the user, or no such remote.
        """
        current = self._remote(server, actor)
        if "gestures" not in current:
            return {"changed": [], "restart": []}
        del current["gestures"]
        return await self._save(server, current, actor=actor)

    def _remote(self, server: str, actor: Actor) -> dict[str, Any]:
        """A gesture remote's settings, to change what its gestures do (the user's alone).

        Raises:
            Refused: not the user, or no such remote.
        """
        if actor != "hud":
            raise Refused(f"Only the user can choose what gestures do, {GESTURES_PAGE}.")
        current = dict(self.rt.cfg.mcp_servers.get(server) or {})
        if current.get("intents") is not True:
            raise Refused(f"{server} doesn't send gestures (it needs intents: true in data/config.yaml).")
        return current

    # ------------------------------------------------------------------------------------------------ models
    def pull_model(self, name: str) -> None:
        """Download a model for the brain on this PC in the background; ``model_pull`` events tell how far it got.

        Args:
            name: the model, e.g. "qwen3.5:4b".
        """
        self.rt.supervisor.spawn(self.rt.ollama.pull(name, self.rt.bus.publish), name=f"pull {name}")

    async def delete_model(self, name: str) -> None:
        """Delete a model of the brain on this PC.

        Args:
            name: the model.

        Raises:
            Refused: it is the model in use.
            OllamaError: Ollama refused, or isn't running.
        """
        if tagged(name) == tagged(self.rt.cfg.ollama.model):
            raise Refused("That model is in use: switch to another one first.")
        await self.rt.ollama.delete(name)

    # ------------------------------------------------------------------------------------------------ lifecycle
    def exit(self, action: ExitAction) -> None:
        """Stop or restart Orion, telling the HUD first.

        Args:
            action: "stop" or "restart".
        """
        self.rt.bus.publish("shutdown", restart=action == "restart")
        asyncio.get_running_loop().call_later(EXIT_DELAY_S, self.rt.request_exit, action)

    def after_reply(self, action: Callable[[], Awaitable[Any] | None]) -> None:
        """Do something once the answer under way is over (a tool that asks for it runs in the middle of one).

        Args:
            action: what to do; a coroutine it returns is awaited.
        """

        async def wait() -> None:
            """Wait until no turn runs, then act."""
            await self.rt.turns.idle()
            result = action()
            if inspect.isawaitable(result):
                await result

        self.rt.supervisor.spawn(wait(), name="after the reply")

    # ------------------------------------------------------------------------------------------------ internals
    def _status(self, name: str) -> "ServerStatus":
        """A server as the hub lists it.

        Raises:
            Refused: there is no such server.
        """
        entry = next((s for s in self.rt.hub.status() if s["name"] == name), None)
        if entry is None:
            raise Refused(f"There's no MCP server called {name!r}.")
        return entry

    def _run_command_on(self) -> bool:
        """Whether run_command is switched on: its group on, and the tool itself not switched off."""
        system = self.rt.cfg.mcp_servers.get("system") or {}
        return server_on(system) and "run_command" not in switched_off(system)

    async def _save(self, name: str, settings: Mapping[str, Any], *, actor: Actor) -> dict[str, JSON]:
        """Save one server's settings; empty settings drop its entry (a built-in group goes back to its defaults)."""
        servers = {key: value for key, value in self.rt.cfg.mcp_servers.items() if key != name}
        if settings:
            servers[name] = dict(settings)
        return await self.rt.apply_servers(servers, actor=actor)


def _listed(value: JSON | None) -> list[str]:
    """A list of names from a settings result ([] for anything else)."""
    return [str(item) for item in value] if isinstance(value, list) else []


def outcome(result: Mapping[str, JSON]) -> str:
    """A settings result, in words for the model.

    Args:
        result: what ``Controls.change_settings`` returned.

    Returns:
        One or two sentences.
    """
    if deferred := _listed(result.get("deferred")):
        return f"{', '.join(deferred)} will change once this answer is over."
    changed = _listed(result.get("changed"))
    if not changed:
        return "Nothing changed: it was set that way already."
    later = [key for key in changed if key in set(_listed(result.get("restart")))]
    text = f"Saved {', '.join(changed)}."
    return text + (f" {', '.join(later)} take effect once Orion restarts (orion_restart)." if later else "")


def plugin_line(plugin: Mapping[str, JSON]) -> str:
    """One installed plugin, for the model.

    Args:
        plugin: a plugin as ``Library.view`` returns it.

    Returns:
        E.g. "- linear 1.2.0, on: 3 skills, 1 command, 1 connector".
    """
    parts = plugin["components"] if isinstance(plugin["components"], dict) else {}

    def count(kind: str, noun: str) -> str:
        n = len(parts.get(kind) or [])
        return f"{n} {noun}{'s' * (n != 1)}"

    update = plugin.get("update")
    newer = f"; a newer version is out ({update['version']})" if isinstance(update, dict) else ""
    version = f" {plugin['version']}" if plugin["version"] else ""
    return (f"- {plugin['name']}{version}, {'on' if plugin['enabled'] else 'off'}: "
            f"{count('skills', 'skill')}, {count('commands', 'command')}, {count('connectors', 'connector')}{newer}")


def installed_words(plugin: Mapping[str, JSON]) -> str:
    """What installing a plugin brought, for the model (its connectors are off: only the user switches one on).

    Args:
        plugin: the plugin as ``Library.view`` returns it.

    Returns:
        A sentence or two.
    """
    parts = plugin["components"] if isinstance(plugin["components"], dict) else {}
    connectors = parts.get("connectors") or []
    text = f"Installed {plugin['name']}: {plugin_line(plugin).partition(': ')[2]}."
    if connectors:
        text += (" Its connectors are off until the user switches them on and signs in, in the HUD under Customize, "
                 "Connectors.")
    return text


def _discovered(library: "Library", *, query: str, marketplace: str = "") -> list[dict[str, JSON]]:
    """The marketplaces' plugins that match (``Library.discover``'s items)."""
    items = library.discover(query=query, marketplace=marketplace)["items"]
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _marketplace_of(library: "Library", name: str) -> str:
    """The marketplace that has a plugin not installed yet.

    Raises:
        LookupError: none has it.
        ValueError: several have it.
    """
    found = [item["marketplace"] for item in _discovered(library, query=name) if item["name"] == name]
    if not found:
        raise LookupError(f"No marketplace has a plugin called {name}: search for it.")
    if len(found) > 1:
        raise ValueError(f"{name} is in several marketplaces ({', '.join(map(str, found))}): say which.")
    return str(found[0])


def step_line(record: "StepRecord") -> str:
    """One line of recent_tool_calls. Another app's arguments and answers, and answers drawn from web or app content,
    are left out: echoed into a later turn, they would pass for something the user said.

    Args:
        record: one operation of the trace.

    Returns:
        The line.
    """
    at = time.strftime("%H:%M:%S", time.localtime(record["at"]))
    head = f"{at} {record['source']} {record['server'] or '-'}.{record['name']}"
    ok = "ok" if record["ok"] else "failed"
    if record["source"] == "app":
        return f"{head} (for {record['client'] or 'another app'}) {ok}"
    args = json.dumps(record["args"], ensure_ascii=False)[:RECENT_ARGS_CHARS]
    answer = HIDDEN if record["taints"] else " ".join(record["result"].split())[:RECENT_RESULT_CHARS]
    return f"{head}({args}) {ok}: {answer}"


def server(controls: Controls) -> ToolsServer:
    """Orion's controls as an MCP server, bound to this running Orion.

    Args:
        controls: the controls; every tool calls them as "ai".

    Returns:
        The server (the hub reaches it in-process).
    """
    from mcp.server.mcpserver.exceptions import ToolError

    from orion.tools import automation, brains, device, documents

    rt = controls.rt
    group = Group(CORE)
    sections = "One section: " + ", ".join(ai_sections()) + "; empty for all."

    @group.tool(read_only=True, title="Read his settings")
    async def settings_get(section: Annotated[str, Field(description=sections)] = "") -> str:
        """Show the settings Orion may read, all of them or one section."""
        with refusals():
            return json.dumps(ai_view(rt.cfg, section.strip()), ensure_ascii=False)

    @group.tool(destructive=True, title="Change his settings")
    async def settings_set(
        changes: Annotated[dict[str, Any], Field(description='New values, by section or as flat keys, e.g. '
                                                             '{"tts": {"speed": 1.2}} or {"user.city": "Athens"}.')],
    ) -> str:
        """Change Orion's settings (see settings_get). Most apply at once; the answer says which wait for a
        restart, and a brain switch waits for this answer to end. MCP servers and tools have tools of their own."""
        if any(key.partition(".")[0] == "mcp_servers" for key in changes):
            raise ToolError("MCP servers change with the mcp_servers and tool_switch tools.")
        with refusals():
            return outcome(await controls.change_settings(changes, actor="ai"))

    parts_said = ("comms (the conversation), vitals, weather, timers, tasks, news (the headlines), hands (the live "
                 "view of the user's hands, as the gesture camera sees them), neural (his memory map), settings, or "
                 "one settings tab: " + ", ".join(f"settings/{tab}" for tab in SETTINGS_TABS))
    def summon_hud(action: HudAction, parts: list[str]) -> str:
        """Show, hide or toggle parts of the HUD, or dismiss its newest panel (the same event the fast path in the
        turn runner publishes)."""
        if not rt.turns.hud_open():
            raise ToolError("The HUD isn't open on this PC: there's no screen to show it on.")
        if action == "dismiss":
            rt.bus.publish("summon", action=action, panels=[], source="brain")
            return "The newest panel is off the screen."
        asked, unknown = hud_parts(parts)
        if unknown:
            raise ToolError(f"The HUD has no {', '.join(unknown)}; it has {parts_said}.")
        chosen = list(asked) or (list(EVERYTHING) if action == "hide" else [])
        if not chosen:
            raise ToolError(f"Say what to show: {parts_said}.")
        rt.bus.publish("summon", action=action, panels=chosen, source="brain")
        where = {"show": "On the screen", "hide": "Off the screen", "toggle": "On or off the screen"}[action]
        return f"{where}: {', '.join(chosen)}."

    @group.tool(title="Show on the screen")
    async def hud_show(
        parts: Annotated[list[str], Field(description=f"What to show: {parts_said}.")],
        toggle: Annotated[bool, Field(description="Take them off instead when they are on the screen already "
                                                  "(as the first one is).")] = False,
    ) -> str:
        """Put parts of the HUD on the user's screen: the conversation, the PC's vitals, the weather, timers, tasks,
        the news, his memory map or settings. Use it whenever the user asks to see one of them."""
        return summon_hud("toggle" if toggle else "show", parts)

    @group.tool(title="Clear the screen")
    async def hud_hide(
        parts: Annotated[list[str], Field(description=f"What to hide: {parts_said}; an empty list for everything.")],
        newest: Annotated[bool, Field(description="Only the panel that came last (parts are then not needed).")] = False,
    ) -> str:
        """Take parts of the HUD off the user's screen, or everything when none is named."""
        return summon_hud("dismiss" if newest else "hide", parts)

    @group.tool(title="Speak his replies or not")
    async def spoken_replies(on: Annotated[bool, Field(description="True: spoken aloud; false: only written in the "
                                                                   "HUD.")]) -> str:
        """Switch Orion's spoken replies on or off (VOICE in the HUD)."""
        rt.turns.quiet = not on
        rt.bus.publish("voice", on=on)
        return "Spoken replies are on." if on else "Spoken replies are off: replies are only written in the HUD."

    @group.tool(title="Mute or unmute the microphone")
    async def microphone(
        muted: bool,
        minutes: Annotated[int, Field(ge=0, description="Unmute by itself after this many minutes (0: until "
                                                        "unmuted).")] = 0,
    ) -> str:
        """Mute or unmute Orion's microphone. While it is muted he hears nothing, not even his name."""
        voice = rt.voice
        if voice is None or voice.listener is None:
            raise ToolError("There's no microphone: voice input is off.")
        rt.set_muted(muted, minutes)
        if not muted:
            return "The microphone couldn't be opened again." if voice.listener.muted else "The microphone is on."
        if minutes:
            return f"The microphone is muted for {minutes} minute{'s' * (minutes != 1)}."
        return "The microphone is muted."

    @group.tool(read_only=True, title="List the voices")
    async def voices_list() -> str:
        """List the voices Orion can speak with."""
        tts = rt.voice.tts if rt.voice is not None else None
        if tts is None:
            raise ToolError("The voice is still loading.")
        return "\n".join(f"{card['id']}: {card['name']}, {card['accent']} {card['gender']}"
                         + (" (in use)" if card["id"] == rt.cfg.tts.voice else "")
                         for card in voice_cards(tts.voices()))

    @group.tool(title="Choose his voice")
    async def voice_choose(
        voice: Annotated[str, Field(description='A voice from voices_list, e.g. "bm_george".')],
        speed: Annotated[float, Field(description="How fast he speaks, 0.5 to 2 (0: as now).")] = 0,
    ) -> str:
        """Change the voice Orion speaks with, and optionally how fast he speaks."""
        patch: dict[str, Any] = {"voice": voice.strip()}
        if speed:
            patch["speed"] = float(speed)
        with refusals():
            return outcome(await controls.change_settings({"tts": patch}, actor="ai"))

    @group.tool(read_only=True, title="Check his brain")
    async def brain_status() -> str:
        """Which language model Orion thinks with, where it runs, and whether it's ready."""
        cfg = rt.cfg
        state = "loading" if rt.turns.loading else f"not ready: {rt.brain_error}" if rt.brain_error else "ready"
        text = f"Thinking with {label(cfg)} on {WHERE[cfg.brain.provider]}: {state}."
        if cfg.brain.provider == "claude":
            from orion.llm.claude import claude_status

            status = await claude_status()  # never says who the account is
            text += (" Claude Code isn't installed." if not status.installed else
                     f" Claude Code is signed in ({status.plan or status.method})." if status.logged_in else
                     " Claude Code isn't signed in: run `claude` in a terminal and sign in.")
        return text

    @group.tool(title="Switch his brain")
    async def brain_switch(
        provider: Literal["claude", "ollama", "openrouter"],
        model: Annotated[str, Field(description='Optional: "haiku", "sonnet" or "opus" on the Claude plan; an '
                                                'installed model for Ollama; a model id for OpenRouter.')] = "",
    ) -> str:
        """Switch the language model Orion thinks with: your Claude plan, this PC (Ollama) or OpenRouter. It
        happens once this answer is over."""
        cfg = rt.cfg
        model = model.strip()
        if provider == cfg.brain.provider and model in ("", getattr(cfg, provider).model):
            return f"He already thinks with {label(cfg)}."
        patch: dict[str, dict[str, Any]] = {"brain": {"provider": provider}}
        if model:
            patch[provider] = {"model": model}
        with refusals():
            return outcome(await controls.change_settings(patch, actor="ai"))

    @group.tool(destructive=True, open_world=True, taints=False, title="Manage the models on this PC")
    async def local_models(
        action: Literal["list", "pull", "delete"],
        name: Annotated[str, Field(description='The model, for pull and delete, e.g. "qwen3.5:4b".')] = "",
    ) -> str:
        """List, download (from ollama.com) or delete the language models Ollama keeps on this PC, for when Orion
        thinks here (brain_switch to ollama)."""
        installed, why = await rt.ollama.models()
        if why:
            raise ToolError(why)
        if action == "list":
            in_use = tagged(rt.cfg.ollama.model)
            return "\n".join(f"{m['name']}: {m['params']} {m['quant']}, {m['size'] / 1e9:.1f} GB"
                             + (", in use" if m["name"] == in_use else "") for m in installed) or "None yet."
        name = name.strip()
        if not name:
            raise ToolError("Say which model.")
        if action == "pull":
            controls.pull_model(name)
            return f"Downloading {name} in the background; the HUD shows how far it got."
        with refusals():
            await controls.delete_model(name)
        return f"Deleted {name}."

    @group.tool(read_only=True, title="List the integrations",
                examples=("What can you connect to?", "Can you control my lights?"))
    async def integrations(
        query: Annotated[str, Field(description="Words to look for, e.g. \"notes\" or \"calendar\"; empty: all.")] = "",
    ) -> str:
        """What Orion can be connected to in a click (Home Assistant, a browser, e-mail, Google, Microsoft, calendars,
        Discord, and the connectors of Claude's directory such as Notion, Linear or Slack), and which are set up.
        Setting one up is the user's to do (Customize, Connectors): it needs their sign-in or tokens."""
        found = await rt.connectors.directory(query.strip())
        how = {"oauth": "needs a sign-in", "fields": "needs a few settings", "none": "needs nothing"}
        lines = [f"- {item['title']} ({item['category']}): {'set up' if item['installed'] else 'not set up'}"
                 f"{'' if item['installed'] else ', ' + how[str(item['auth'])]}. {item['description']}"
                 for item in found]
        return "\n".join(lines) or "Nothing in the directory matches."

    @group.tool(destructive=True, title="Manage MCP servers")
    async def mcp_servers(
        action: Literal["list", "add", "update", "remove", "restart", "enable", "disable"],
        name: Annotated[str, Field(description="The server (not needed to list them).")] = "",
        command: Annotated[str, Field(description='A server on this PC: its program, e.g. "uvx".')] = "",
        args: Annotated[Sequence[str], Field(description='Its arguments, e.g. ["mcp-server-time"].')] = (),
        url: Annotated[str, Field(description="A remote server: its URL.")] = "",
        description: Annotated[str, Field(description="What it is for, in a few words.")] = "",
    ) -> str:
        """List the MCP servers that give Orion his tools (his own and those added), add, change or remove one,
        restart it, or switch it off. Adding, changing and removing one asks the user first. Only the user switches
        one back on, sets its tokens or re-points one that has them (Customize, Connectors in the HUD); he adds a
        server run by a command only while run_command is on."""
        if action == "list":
            return "\n".join(f"{s['name']} ({s['title']}): {s['status']}" + (f": {s['error']}" if s["error"] else "")
                             + f", {sum(t['enabled'] for t in s['tools'])} of {len(s['tools'])} tools on"
                             for s in rt.hub.status()) or "No servers."
        name = name.strip()
        if not name:
            raise ToolError("Say which server.")
        with refusals():
            if action == "restart":
                await controls.restart_server(name, actor="ai")
                return f"{name} is restarting."
            if action in ("enable", "disable"):
                await controls.switch_server(name, action == "enable", actor="ai")
                return f"{name} is switched {'on' if action == 'enable' else 'off'}."
            if action == "remove":
                await controls.remove_server(name, actor="ai")
                return f"{name} is removed."
            exists = is_builtin(name) or name in rt.cfg.mcp_servers
            if action == "add" and exists:
                raise Refused(f"There's a server called {name} already: update it.")
            if action == "update" and not exists:
                raise Refused(f"There's no MCP server called {name!r}.")
            given = {"command": command.strip(), "args": [str(arg) for arg in args], "url": url.strip(),
                     "description": description.strip()}
            current = dict(rt.cfg.mcp_servers.get(name) or {})
            await controls.save_server(name, {**current, **{k: v for k, v in given.items() if v}}, actor="ai")
        return f"{name} is saved; it connects in the background (list shows how it went)."

    @group.tool(destructive=True, open_world=True, title="Manage plugins",
                examples=("Is there a plugin for Linear?", "Install the pdf plugin", "What plugins do you have?"))
    async def plugins(
        action: Literal["list", "search", "install", "update", "disable", "remove"],
        name: Annotated[str, Field(description="The plugin (install, update, disable, remove).")] = "",
        query: Annotated[str, Field(description="Words to look for in the marketplaces (search); empty: all.")] = "",
        marketplace: Annotated[str, Field(description="Install from this marketplace (default: the one that has "
                                                      "it).")] = "",
    ) -> str:
        """List the plugins installed, search the marketplaces, install, update, switch off or remove a plugin. A
        plugin is a Claude plugin: its skills and commands become skills, its servers connectors; its hooks, agents
        and the rest are never run. Installing, updating and removing ask the user first; the connectors of a plugin
        Orion installs stay off until the user switches them on (Customize, Connectors), and only the user switches
        a plugin back on."""
        from orion.activity import SOURCES
        from orion.plugins import FetchError

        library = rt.library
        if action == "list":
            return "\n".join(plugin_line(plugin) for plugin in library.plugins()) or "No plugin is installed."
        if action == "search":
            found = _discovered(library, query=query.strip(), marketplace=marketplace.strip())
            lines = [f"- {item['name']} ({item['marketplace']}{', ' + str(item['category']) if item['category'] else ''})"
                     f"{', installed' if item['installed'] else ''}: {item['description']}"
                     for item in found[:SEARCH_SHOWN]]
            more = f"\n(and {len(found) - SEARCH_SHOWN} more: narrow the search)" if len(found) > SEARCH_SHOWN else ""
            return "\n".join(lines) + more if lines else "No plugin matches."
        name = name.strip()
        if not name:
            raise ToolError("Say which plugin.")
        call = current_call()
        asked = {"source": call.source if call.source in SOURCES else "hud", "actor": AI_ACTOR}
        try:
            with refusals():
                if action == "disable":
                    await library.set_enabled(name, False, **asked)
                    return f"{name} is switched off."
                if action == "install":
                    if name in rt.cfg.plugins.installed:
                        raise ValueError(f"{name} is installed already: update it instead.")
                    market = marketplace.strip() or _marketplace_of(library, name)
                    _, entry = library.listing(market, name)
                    await controls.ask_first("plugins", "Install a plugin", {
                        "name": name, "marketplace": market,
                        "about": " ".join(str(entry.get("description") or "").split())[:ASK_ARG_CHARS]}, actor="ai")
                    done = await library.install(market, name, connectors_on=False, **asked)
                    return installed_words(done)
                if name not in rt.cfg.plugins.installed:
                    raise LookupError(f"There's no plugin called {name}.")
                if action == "update":
                    await controls.ask_first("plugins", "Update a plugin", {"name": name}, actor="ai")
                    done = await library.update(name, connectors_on=False, **asked)
                    return f"Updated {name}" + (f" to version {done['version']}." if done["version"] else ".")
                await controls.ask_first("plugins", "Remove a plugin", {"name": name}, actor="ai")
                await library.remove(name, **asked)
                return f"{name} is removed."
        except FetchError as e:
            raise ToolError(f"It couldn't be fetched: {e}") from None

    @group.tool(title="Switch a tool off")
    async def tool_switch(
        server: Annotated[str, Field(description='The server it belongs to, e.g. "system".')],
        tool: Annotated[str, Field(description='The tool, e.g. "power".')],
        enabled: bool,
    ) -> str:
        """Switch one of Orion's tools off: it is no longer offered to him (nor lent to other apps). Only the user
        can switch it back on, in the HUD under Customize, Connectors."""
        with refusals():
            await controls.switch_tool(server.strip(), tool.strip(), enabled, actor="ai")
        return f"{tool} is switched {'on' if enabled else 'off'}."

    @group.tool(read_only=True, title="Say what the gestures do")
    async def gestures() -> str:
        """Which hand gestures the user can make at the camera (the hand-gesture remote), and what each one does.
        Only the user changes what they do, on the remote's page in Customize, Connectors."""
        remotes = gesture_remotes(rt.cfg.mcp_servers, on_only=False)
        if not remotes:
            return "No hand-gesture remote is set up (an MCP server with intents: true, such as ORION hands)."
        lines: list[str] = []
        for name in remotes:
            bindings, problems = bindings_of(rt.cfg.mcp_servers.get(name) or {}, rt.cfg.gestures.defaults)
            said, why = await controls.vocabulary(name)
            listed = said["gestures"] if isinstance(said["gestures"], list) else []
            titles = {str(g["name"]): str(g["title"]) for g in listed if isinstance(g, dict)}
            lines.append(f"Gestures of {name}" + (f" (it can't list its gestures: {why})" if why else "") + ":")
            for gesture, binding in bindings.items():
                how = hold_words(binding)
                lines.append(f"- {titles.get(gesture, gesture.replace('_', ' '))}: {describe(binding)}"
                             + (f" ({how})" if how else ""))
            if free := [titles[g] for g in titles if g not in bindings]:
                lines.append("Free (they do nothing yet): " + ", ".join(free) + ".")
            lines += [f"- {gesture}: not used ({why})" for gesture, why in problems.items()]
        lines.append(f"Only the user changes what they do, {GESTURES_PAGE}.")
        return "\n".join(lines)

    @group.tool(read_only=True, title="Show his latest tool calls")
    async def recent_tool_calls(
        limit: Annotated[int, Field(ge=1, le=RECENT_MAX, description="How many, newest first.")] = 10,
    ) -> str:
        """The tools used lately (by Orion, from the HUD or by other apps) and what they answered."""
        records = rt.ops.recent(limit)
        return "\n".join(step_line(record) for record in records) or "No tools used yet."

    @group.tool(read_only=True, taints=False, title="Show what he did lately",
                examples=("What have you done today?", "Did anything get blocked?", "What did the job do?"))
    async def activity_recent(
        limit: Annotated[int, Field(ge=1, le=ACTIVITY_MAX, description="How many entries, newest first.")] = 20,
        kind: Annotated[str, Field(description="Only this kind: tool, ask, deny, block, alert, job, backup, connector, "
                                               "plugin, skill, secret, instant, login, bench or system (empty: all).")] = "",
    ) -> str:
        """What Orion did lately and who allowed it: his tool calls, the questions he asked and how they were answered,
        what a limit refused or the redactor blocked, warnings and jobs. Titles and outcomes only."""
        from orion.activity import KINDS

        if kind and kind not in KINDS:
            raise ToolError(f"The kinds are {', '.join(KINDS)}.")
        rows = await asyncio.to_thread(rt.activity.recent, limit=limit, kinds=[kind] if kind else [])
        return "\n".join(f"{time.strftime('%d %b %H:%M:%S', time.localtime(row['at']))} {row['source']} {row['kind']}: "
                         f"{row['title']}" + (f" ({row['outcome']})" if row["outcome"] else "")
                         + (f", {row['actor']}" if row["actor"] else "") for row in rows) or "Nothing yet."

    @group.tool(read_only=True, title="Say how fast he is")
    async def speed_report() -> str:
        """How quickly he has been answering lately, for each brain and for instant commands: the wait for the first
        sound after a request ends, and what it went on (hearing, thinking, tools, speaking)."""
        return in_words(stats(await asyncio.to_thread(rt.memory.store.timings, STATS_TURNS)))

    @group.tool(title="Start a new conversation")
    async def new_conversation() -> str:
        """Start a fresh conversation (NEW CHAT in the HUD) once this answer is over. What was said stays in the log
        and is reviewed for his memory."""
        controls.after_reply(rt.turns.new_conversation)
        return "A new conversation starts after this answer." if rt.turns.busy else "A new conversation."

    @group.tool(title="Review the conversation now")
    async def memory_review_now() -> str:
        """Review this conversation for Orion's memory now (it otherwise happens after a quiet spell), once this
        answer is over."""
        reviewer = rt.reviewer
        if reviewer is None:
            raise ToolError("The memory review is not running.")
        controls.after_reply(reviewer.review_now)
        return "The conversation is reviewed for memory after this answer."

    @group.tool(destructive=True, title="Restart Orion")
    async def orion_restart() -> str:
        """Restart ORION himself (this assistant's own program, not the PC: power does that) once this answer is
        over. He is back within a minute; settings that wait for a restart then apply."""
        controls.after_reply(lambda: controls.exit("restart"))
        return "Orion restarts after this answer."

    @group.tool(destructive=True, title="Shut Orion down")
    async def orion_shutdown() -> str:
        """Shut down ORION himself (this assistant's own program, not the PC: power does that) once this answer is
        over. He stays off until started again (orion start)."""
        controls.after_reply(lambda: controls.exit("stop"))
        return "Orion shuts down after this answer."

    brains.register(group, rt)

    @group.tool(title="Back up his memory now", examples=("Back up your memory", "Make a backup of yourself"))
    async def backup_now() -> str:
        """Back up his memory, conversations, settings, skills and plugins now, into an archive in the backup folder
        (and into the restic repository, when one is set up). Not in a backup: the vault of secrets, the models, the
        logs and the index of the user's documents. A backup made less than five minutes ago is not repeated when you
        ask: the oldest ones would be pushed out."""
        from orion.backup import MIN_GAP_S, BackupError

        call = current_call()
        age = time.time() - (rt.backups.last() or 0.0)
        if call.provenance != "hud" and age < MIN_GAP_S:
            return f"He backed up {max(1, round(age / 60))} minute(s) ago already; the backup is there."
        task = rt.backups.start(call.source or "app")
        try:
            made = await asyncio.wait_for(asyncio.shield(task), BACKUP_WAIT_S)
        except TimeoutError:
            return "The backup is under way and takes a while; the HUD shows when it is done."
        except BackupError as e:
            raise ToolError(str(e)) from None
        copy = ("" if not made.restic else " A copy is in the restic repository too." if made.restic == "ok"
                else f" The restic copy failed: {made.restic}")
        return f"Backed up: {made.name} ({made.size / 1e6:.1f} MB, {made.files} files).{copy}"

    @group.tool(read_only=True, title="List his backups",
                examples=("What backups do you have?", "When did you last back yourself up?"))
    async def backups_list() -> str:
        """The backups of his memory and settings: when each was made and how big it is, where they are kept, and
        when the next one is made by itself."""
        from orion.backup import backup_folder
        from orion.jobs import explain

        items, why = await rt.backups.items()
        settings = rt.cfg.backup
        lines = [f"- {item.name}: {time.strftime('%d %b %H:%M', time.localtime(item.at))}, {item.size / 1e6:.1f} MB, "
                 f"{'in restic' if item.where == 'restic' else 'on this PC'}" for item in items]
        when = f"made by itself: {explain(settings.schedule)}" if settings.schedule else "not made by itself"
        head = f"{len(lines)} backup(s) in {backup_folder(rt.cfg)} ({when}; the newest {settings.keep} are kept)."
        if why:
            head += f" The restic repository couldn't be listed: {why}"
        return "\n".join([head, *lines]) if lines else head + " None yet."

    return build_server([group, *device.groups(rt), *automation.groups(rt), *documents.groups(rt)],
                        name=CORE.name)
