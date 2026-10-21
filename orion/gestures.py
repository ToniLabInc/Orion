"""Gestures: what each hand gesture does. The actions a gesture can take, the bindings the user chose (or the
defaults), and when a held hand shape fires.

A gesture remote, an MCP server whose settings say ``intents: true`` such as the ORION hands tracker, reports each
gesture it sees as an intent named ``gesture`` (``orion.intents``): which gesture, which hand, and, for a hand
shape, whether it just started or is still held and for how long. It never says what to do: that is the user's to
choose, per server, in ``mcp_servers.<server>.gestures`` (Customize → Connectors → that server), or config.yaml's
``gestures.defaults`` when the server has no such entry. A binding is the action and its options, flat::

    gestures:
      victory: {do: media, action: play_pause}
      thumb_up: {do: volume, action: up, repeat: true}     # again at each hold step while it is held
      closed_fist: {do: volume, action: mute, hold: 1.0}   # only once it has been held a second
      circle_cw: {do: tool, server: system, tool: open, args: {target: spotify}}

The actions (``ACTIONS``): show, hide or toggle parts of the HUD and dismiss its newest panel; media and the
volume; the microphone, his spoken replies, a new conversation, something to ask him, one of his skills, a timer;
any tool of any MCP server; and ``arm``, which makes the other gestures work only for a while after it (a safety
for anyone who finds gestures firing by accident). Listening and stopping stay with his voice: his name wakes him.
Each runs one of Orion's MCP tools (``orion.intents``), so a tool switched off in Abilities does nothing by gesture
either.

Only the user chooses bindings: the HUD saves them (``Controls.save_gestures``), the AI never can (a gesture bound
to a tool runs it with no questions asked, so a prompt injection must not be able to bind one). A remote may also ask
for one of a few actions itself (``PUSHED``), never a tool.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from orion.base import clip, number, server_on
from orion.skills import normalize_name
from orion.voice.wake import HUD_PARTS, PANELS

type ParamKind = Literal["panels", "panel", "choice", "text", "minutes", "seconds", "skill", "server", "tool", "args"]
type Decision = Literal["run", "again", "wait", "done", "asleep"]

MAX_HOLD_S: Final = 8.0  # the longest hold a binding may ask for (ORION hands reports a held shape up to 8 s)
HOLD_SLACK_S: Final = 0.05  # a hold step reported this much before the hold asked for counts
SHOWINGS_KEPT_S: Final = 30.0  # a showing's state is forgotten this long after its last report
TEXT_MAX: Final = 500
ARGS_MAX_CHARS: Final = 2000
_NAME: Final = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class BindingError(ValueError):
    """A binding that can't be used; the message says why, in words."""


@dataclass(frozen=True, slots=True)
class Param:
    """One option of an action.

    Attributes:
        name: its key in the binding.
        kind: what it holds (the HUD picks its control by it).
        choices: the values a "choice" takes.
        required: whether the binding must give it.
        default: its value when left out.
    """

    name: str
    kind: ParamKind
    choices: tuple[str, ...] = ()
    required: bool = True
    default: Any = None


@dataclass(frozen=True, slots=True)
class Action:
    """Something a gesture can do.

    Attributes:
        title: its name for people.
        group: where the HUD lists it.
        params: its options.
        repeats: whether doing it again while the shape is held makes sense (the volume does).
    """

    title: str
    group: str
    params: tuple[Param, ...] = ()
    repeats: bool = False


ACTIONS: Final[Mapping[str, Action]] = {
    "show": Action("Show on the screen", "Screen", (Param("panels", "panels"),)),
    "hide": Action("Take off the screen", "Screen", (Param("panels", "panels", required=False, default=()),)),
    "toggle": Action("Show or hide", "Screen", (Param("panel", "panel"),)),
    "dismiss": Action("Dismiss the newest panel", "Screen"),
    "media": Action("Media", "Media", (Param("action", "choice", ("play_pause", "next", "previous", "play", "pause",
                                                                   "stop")),)),
    "volume": Action("Volume", "Media", (Param("action", "choice", ("up", "down", "mute")),), repeats=True),
    "mic": Action("Microphone", "Orion", (Param("action", "choice", ("toggle", "mute", "unmute")),)),
    "spoken": Action("Spoken replies", "Orion", (Param("action", "choice", ("toggle", "on", "off")),)),
    "new_chat": Action("New conversation", "Orion"),
    "say": Action("Ask him", "Orion", (Param("text", "text"),)),
    "skill": Action("Run a skill", "Orion", (Param("name", "skill"),)),
    "timer": Action("Start a timer", "Orion", (Param("minutes", "minutes"),)),
    "arm": Action("Wake the other gestures", "Gestures", (Param("seconds", "seconds"),)),
    "tool": Action("Use a tool", "Tools", (Param("server", "server"), Param("tool", "tool"),
                                           Param("args", "args", required=False, default={}))),
}
PUSHED: Final = frozenset({"show", "hide", "toggle", "dismiss", "media", "volume", "mic", "spoken", "new_chat",
                           "say"})  # what a remote may ask for itself, unbound (never a tool: only the user's bindings)


@dataclass(frozen=True, slots=True)
class Binding:
    """What one gesture does.

    Attributes:
        do: the action (a key of ``ACTIONS``).
        params: its options, checked (every option the action has, defaults filled in).
        hold: hand shapes: how long the shape must be held before it fires (0: as soon as it counts).
        repeat: hand shapes: fire again at each hold step while it stays held.
    """

    do: str
    params: Mapping[str, Any] = field(default_factory=dict)
    hold: float = 0.0
    repeat: bool = False

    def as_dict(self) -> dict[str, Any]:
        """The binding as data/config.yaml writes it: the action, its options, then hold and repeat when set."""
        spec = ACTIONS[self.do]
        out: dict[str, Any] = {"do": self.do}
        for param in spec.params:
            value = self.params.get(param.name, param.default)
            if param.required or value != param.default:
                out[param.name] = list(value) if isinstance(value, tuple) else value
        if self.hold:
            out["hold"] = self.hold
        if self.repeat:
            out["repeat"] = True
        return out


def parse_binding(raw: object) -> Binding:
    """Check one binding as written in data/config.yaml (or sent by the HUD).

    Args:
        raw: ``{do, <its options>, hold?, repeat?}``.

    Returns:
        The binding.

    Raises:
        BindingError: what is wrong with it, in words.
    """
    if not isinstance(raw, Mapping):
        raise BindingError("a binding is a mapping: {do: <action>, ...its options}")
    do = raw.get("do")
    if not isinstance(do, str) or do not in ACTIONS:
        raise BindingError(f"unknown action {do!r}; choose from {', '.join(ACTIONS)}")
    spec = ACTIONS[do]
    names = {param.name for param in spec.params}
    if extra := sorted(str(key) for key in raw if key not in names | {"do", "hold", "repeat"}):
        takes = ", ".join(sorted(names)) or "no options"
        raise BindingError(f"{do} takes {takes} (and hold, repeat), not {', '.join(extra)}")
    params: dict[str, Any] = {}
    for param in spec.params:
        if param.name in raw:
            params[param.name] = _param(do, param, raw[param.name])
        elif param.required:
            raise BindingError(f"{do} needs {param.name}")
        else:
            params[param.name] = _param(do, param, param.default)
    if do == "show" and not params["panels"]:
        raise BindingError("show needs at least one part of the HUD")
    hold = number(raw.get("hold", 0.0))
    if hold is None or not 0 <= hold <= MAX_HOLD_S:
        raise BindingError(f"hold is a number of seconds from 0 to {MAX_HOLD_S:g}")
    repeat = raw.get("repeat", False)
    if not isinstance(repeat, bool):
        raise BindingError("repeat is true or false")
    return Binding(do, params, round(hold, 2), repeat)


def _param(do: str, param: Param, value: Any) -> Any:
    """One option, checked and made plain."""
    where = f"{do}.{param.name}"
    match param.kind:
        case "panels":
            if not isinstance(value, list | tuple) or not all(isinstance(p, str) for p in value):
                raise BindingError(f"{where} is a list of the HUD's parts")
            parts, unknown = hud_parts(value)
            if unknown:
                raise BindingError(f"the HUD has no {', '.join(unknown)}; it has {', '.join(HUD_PARTS)}")
            return parts
        case "panel":
            if not isinstance(value, str) or value.strip().lower() not in HUD_PARTS:
                raise BindingError(f"{where} is one of {', '.join(HUD_PARTS)}")
            return value.strip().lower()
        case "choice":
            if value not in param.choices:
                raise BindingError(f"{where} is one of {', '.join(param.choices)}")
            return value
        case "text":
            if not isinstance(value, str) or not value.strip() or len(value) > TEXT_MAX:
                raise BindingError(f"{where} is a text of 1 to {TEXT_MAX} characters")
            return " ".join(value.split())
        case "skill":
            if not isinstance(value, str) or not value or normalize_name(value) != value:
                raise BindingError(f"{where} is a skill's name, e.g. morning-briefing")
            return value
        case "minutes":
            if (minutes := number(value)) is None or not 1 <= minutes <= 1440:
                raise BindingError(f"{where} is a number of minutes from 1 to 1440")
            return int(minutes) if minutes.is_integer() else round(minutes, 2)
        case "seconds":
            if (seconds := number(value)) is None or not 2 <= seconds <= 120:
                raise BindingError(f"{where} is a number of seconds from 2 to 120")
            return int(seconds) if seconds.is_integer() else round(seconds, 1)
        case "server" | "tool":
            if not isinstance(value, str) or not _NAME.match(value.strip()):
                raise BindingError(f"{where} is a name of letters, digits, _, . or -")
            return value.strip()
        case "args":
            if not isinstance(value, Mapping) or not all(isinstance(k, str) for k in value):
                raise BindingError(f"{where} is a mapping of the tool's arguments")
            try:
                text = json.dumps(value, ensure_ascii=False)
            except (TypeError, ValueError):
                raise BindingError(f"{where} must hold plain values (text, numbers, lists, mappings)") from None
            if len(text) > ARGS_MAX_CHARS:
                raise BindingError(f"{where} is longer than {ARGS_MAX_CHARS} characters")
            return json.loads(text)
    raise BindingError(f"{where}: unknown option kind {param.kind}")  # pragma: no cover


def hud_parts(names: Iterable[str]) -> tuple[tuple[str, ...], list[str]]:
    """Parts of the HUD as named (any case, spaces around), each once, in order; and the names that aren't one.

    Args:
        names: what was asked for ("Weather", " comms ").

    Returns:
        (the parts, the unknown names).
    """
    parts = tuple(dict.fromkeys(name.strip().lower() for name in names if name.strip()))
    return parts, [part for part in parts if part not in HUD_PARTS]


def gesture_remotes(servers: Mapping[str, Any], *, on_only: bool) -> list[str]:
    """The MCP servers that push gestures (``intents: true``).

    Args:
        servers: config.yaml's ``mcp_servers``.
        on_only: only those switched on.

    Returns:
        Their names, in the settings' order.
    """
    return [name for name, settings in servers.items() if isinstance(settings, dict)
            and settings.get("intents") is True and (server_on(settings) or not on_only)]


def default_bindings(raw: Mapping[str, Any]) -> dict[str, Binding]:
    """config.yaml's ``gestures.defaults`` as bindings (checked when the settings load, so all of them parse).

    Args:
        raw: gesture -> binding, as config.yaml writes it (None for nothing).

    Returns:
        The bindings by gesture.
    """
    return {gesture: parse_binding(item) for gesture, item in raw.items() if item not in (None, {})}


def bindings_of(settings: Mapping[str, Any], defaults: Mapping[str, Any]) -> tuple[dict[str, Binding], dict[str, str]]:
    """The bindings a server's settings give: its ``gestures`` entry, or the defaults when it has none.

    A gesture set to nothing (null, or an empty mapping) does nothing; a binding that is wrong is left out and said.

    Args:
        settings: the server's ``mcp_servers`` entry.
        defaults: config.yaml's ``gestures.defaults``.

    Returns:
        (the bindings by gesture, why each one left out was).
    """
    raw = settings.get("gestures")
    if raw is None:
        return default_bindings(defaults), {}
    if not isinstance(raw, Mapping):
        return {}, {"": "gestures must map each gesture to what it does"}
    bindings: dict[str, Binding] = {}
    problems: dict[str, str] = {}
    for gesture, item in raw.items():
        if not isinstance(gesture, str) or not _NAME.match(gesture):
            problems[str(gesture)] = "not a gesture's name"
        elif item in (None, {}):
            continue
        else:
            try:
                bindings[gesture] = parse_binding(item)
            except ValueError as e:
                problems[gesture] = str(e)
    return bindings, problems


def check_bindings(raw: object) -> dict[str, dict[str, Any]]:
    """Check a whole set of bindings, as the HUD saves them; every one must be right.

    Args:
        raw: gesture -> binding (or None for nothing).

    Returns:
        The bindings as data/config.yaml writes them (the gestures set to nothing left out).

    Raises:
        BindingError: the first binding that is wrong, naming its gesture.
    """
    if not isinstance(raw, Mapping):
        raise BindingError("Send the bindings as a mapping of gesture to what it does.")
    out: dict[str, dict[str, Any]] = {}
    for gesture, item in raw.items():
        if not isinstance(gesture, str) or not _NAME.match(gesture):
            raise BindingError(f"{gesture!r} is not a gesture's name.")
        if item in (None, {}):
            continue
        try:
            out[gesture] = parse_binding(item).as_dict()
        except ValueError as e:
            raise BindingError(f"{gesture}: {e}.") from None
    return out


# ---------------------------------------------------------------------------------------------------- in words
_WORDS: Final[Mapping[str, str]] = {
    "media:play_pause": "play / pause", "media:next": "next track", "media:previous": "previous track",
    "media:play": "play", "media:pause": "pause", "media:stop": "stop the music",
    "volume:up": "volume up", "volume:down": "volume down", "volume:mute": "sound off / on",
    "mic:toggle": "microphone off / on", "mic:mute": "microphone off", "mic:unmute": "microphone on",
    "spoken:toggle": "spoken replies off / on", "spoken:on": "spoken replies on", "spoken:off": "spoken replies off",
}
_PART_WORDS: Final[Mapping[str, str]] = {"comms": "COMMS", "vitals": "VITALS", "weather": "WEATHER",
                                         "timers": "TIMERS", "tasks": "TASKS", "news": "NEWS", "hands": "HANDS",
                                         "neural": "the neural map", "settings": "settings"}


def choice_words(do: str, choice: str) -> str:
    """One choice of an action's option, in words for the HUD: "Next track", "Microphone off / on"."""
    words = _WORDS.get(f"{do}:{choice}", choice.replace("_", " "))
    return words[:1].upper() + words[1:]


def fresh(do: str, starts: Mapping[str, Mapping[str, Any]], *, skills: Sequence[str],
          tools: Sequence[tuple[str, str]]) -> dict[str, Any] | None:
    """A gesture newly given an action, before its options are changed: what config.yaml starts it with
    (``gestures.starts``), else its first choice, the first skill, the first tool switched on.

    Args:
        do: the action.
        starts: config.yaml's ``gestures.starts``.
        skills: his skills.
        tools: the tools a gesture can use, as (server, tool).

    Returns:
        The binding, or None when the action can't have one yet (a skill with none to run, a tool with none on).
    """
    out: dict[str, Any] = {"do": do, **starts.get(do, {})}
    for param in ACTIONS[do].params:
        if param.name in out or not param.required:
            continue
        match param.kind:
            case "choice":
                out[param.name] = param.choices[0]
            case "skill" if skills:
                out[param.name] = skills[0]
            case "server" | "tool" if tools:
                out["server"], out["tool"] = tools[0]
            case _:
                return None
    return out


def part_words(part: str) -> str:
    """A part of the HUD in words: "COMMS", "the neural map", "voice settings"."""
    if part.startswith("settings/"):
        return f"{part.split('/', 1)[1]} settings"
    return _PART_WORDS.get(part, part)


def describe(binding: Binding) -> str:
    """What a binding does, in a few words: "next track", "COMMS on / off", "run morning-briefing"."""
    p = binding.params
    match binding.do:
        case "media" | "volume" | "mic" | "spoken":
            return _WORDS[f"{binding.do}:{p['action']}"]
        case "show":
            parts = list(p["panels"])
            return "show everything" if set(parts) >= set(PANELS) else "show " + ", ".join(map(part_words, parts))
        case "hide":
            parts = list(p["panels"])
            return "clear the screen" if not parts else "hide " + ", ".join(map(part_words, parts))
        case "toggle":
            return f"{part_words(p['panel'])} on / off"
        case "dismiss":
            return "dismiss the newest panel"
        case "new_chat":
            return "new conversation"
        case "say":
            text = str(p["text"])
            return f"“{clip(text, 40)}”"
        case "skill":
            return f"run {p['name']}"
        case "timer":
            return f"{p['minutes']:g}-minute timer"
        case "arm":
            return f"wake gestures for {p['seconds']:g} s"
        case "tool":
            args = ", ".join(f"{k}: {v}" for k, v in dict(p["args"]).items())
            return f"{p['server']}.{p['tool']}" + (f" ({args})" if args else "")
    return binding.do  # pragma: no cover


def hold_words(binding: Binding) -> str:
    """How a binding fires, in words ("held 1 s", "repeats while held"), "" when plainly."""
    parts = []
    if binding.hold:
        parts.append(f"held {binding.hold:g} s")
    if binding.repeat:
        parts.append("repeats while held")
    return ", ".join(parts)


# ---------------------------------------------------------------------------------------------------- when to fire
@dataclass(slots=True)
class _Showing:
    """One showing of a hand shape, as reported: whether its binding has fired yet (or found gestures asleep)."""

    fired: bool = False
    asleep: bool = False
    seen: float = 0.0


class Firing:
    """When the gestures of one server fire: a hand shape once held its binding's ``hold`` (then at each hold step if
    it repeats), a motion at once; and, while a gesture is bound to ``arm``, the others only while awake."""

    def __init__(self, clock: Any = time.monotonic) -> None:
        """Start with nothing seen and nothing awake.

        Args:
            clock: the monotonic clock (tests pass their own).
        """
        self._clock = clock
        self._showings: dict[int, _Showing] = {}
        self.awake_until = -math.inf

    def decide(self, binding: Binding, *, phase: str, held: float, showing: int | None, armed: Binding | None) -> \
            Decision:
        """Whether a reported gesture fires its binding now.

        Args:
            binding: what it is bound to.
            phase: "start" or "hold" (a hand shape still held).
            held: how long the shape has been held, in seconds.
            showing: the id of the shape's start, None for a motion.
            armed: the binding of the gesture that wakes the others, None when none is bound to ``arm``.

        Returns:
            "run" to fire it now, "again" to fire it again (a repeating binding, at a hold step of the same showing);
            "wait" (not held long enough yet), "done" (fired already this showing) or "asleep" (gestures aren't awake:
            said once a showing).
        """
        now = self._clock()
        again = False
        if showing is not None:
            for old in [k for k, s in self._showings.items() if now - s.seen > SHOWINGS_KEPT_S]:
                del self._showings[old]
            state = self._showings.setdefault(showing, _Showing())
            state.seen = now
            if state.fired:
                if state.asleep or not (binding.repeat and phase == "hold"):
                    return "done"
                again = True
            elif held + HOLD_SLACK_S < binding.hold:
                return "wait"
            elif phase == "hold" and not binding.hold and not binding.repeat:
                return "done"  # its start was missed (before Orion connected): don't fire it halfway through a hold
        elif phase != "start":
            return "done"
        if armed is not None and binding.do != "arm" and now >= self.awake_until:
            if showing is not None:
                self._showings[showing].fired = self._showings[showing].asleep = True  # one "asleep" a showing
            return "asleep"
        if showing is not None:
            self._showings[showing].fired = True
        if armed is not None:
            self.awake_until = now + float(armed.params["seconds"])  # each gesture keeps them awake
        return "again" if again else "run"
