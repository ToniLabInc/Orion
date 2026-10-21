"""Instant commands: a request that is one of Home Assistant's English sentences ("set a timer for 5 minutes", "pause
the music", "what time is it", "turn on the kitchen light") is done at once, without the model, and still works while
the brain is down.

- ``Matcher`` holds the sentence templates of Home Assistant's own intents (the home-assistant-intents package,
  matched by hassil), only those in ``instant.intents``, loaded once in a worker thread. A request matches whole: one
  word more and it goes to the brain. Every list a template names is given: the timers' numbers come with the
  templates, the names and areas of Home Assistant's devices come from its live context (``Home``), and a list Orion
  has nothing for is empty, so its sentences never match. Two equally good readings that differ are no match.
- ``Instant.plan`` turns a match into what to do: Orion's own tools (the organizer for timers and the to-do list, the
  system for media and volume), the Home Assistant connector's tool of the same intent name (lights and devices of
  ``instant.device_domains``; media and volume when a room or a device is named), or an answer he knows (the time,
  the date, how long a timer has left, the weather of the home city). Whatever it can't do exactly as said is refused,
  and the brain takes the request as before: a timer that would run a command, "turn on the lights" with no area set
  for this PC, a lock or a door, two timers that fit. A wrong action is worse than the model's answer a second later.
- ``Instant.execute`` runs the calls through the hub with origin "user" (the user's own command: a channel's "never"
  limits apply, nothing is asked), records it in the activity log, and hands back the reply from ``instant.replies``;
  an "Error" from any call hands the request to the brain instead.

Matching runs on one worker thread of its own (hassil builds its indexes lazily, and one thread keeps them simple).
"""

import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Protocol

from orion.base import JSON, CallContext, Tool, local_now

if TYPE_CHECKING:
    from hassil import RecognizeResult, TextSlotList

    from orion.activity import Activity
    from orion.config import Config
    from orion.memory.store import ReminderRow, Store
    from orion.tools.web import Weather

log = logging.getLogger("orion.instant")

LANGUAGE: Final = "en"
HERE: Final = "<here>"  # the area a request gets from its context: where Orion is (never an area's name)
CONTEXT: Final[Mapping[str, Any]] = {"area": {"value": HERE, "text": ""}}  # the context of every match
CLIENT: Final = "instant commands"  # who reads Home Assistant's names, as the trace shows it
LIVE_CONTEXT: Final = "GetLiveContext"  # Home Assistant's tool that lists its devices and areas
WARM_UP: Final = ("set a timer for 5 minutes", "set the volume to 50 percent", "what time is it")  # first matches
TIMERS: Final = frozenset({"HassStartTimer", "HassCancelTimer", "HassCancelAllTimers", "HassTimerStatus",
                           "HassIncreaseTimer", "HassDecreaseTimer"})
MEDIA: Final[Mapping[str, str]] = {"HassMediaPause": "pause", "HassMediaUnpause": "play", "HassMediaNext": "next",
                                   "HassMediaPrevious": "previous"}  # intent -> the media tool's action
SOUND: Final = frozenset({"HassSetVolume", "HassSetVolumeRelative", "HassMediaPlayerMute", "HassMediaPlayerUnmute"})
HOME_ONLY: Final = frozenset({"HassTurnOn", "HassTurnOff", "HassLightSet", "HassSetPosition", "HassListAddItem"})
KNOWN: Final = frozenset({"HassGetCurrentTime", "HassGetCurrentDate", "HassGetWeather", "HassShoppingListAddItem"})
HANDLED: Final = TIMERS | MEDIA.keys() | SOUND | HOME_ONLY | KNOWN  # what instant.intents may name
# Each reply of instant.replies and the placeholders it may use besides {honorific} and {Honorific}.
REPLIES: Final[Mapping[str, frozenset[str]]] = {
    "timer_started": frozenset({"timer"}), "timer_cancelled": frozenset({"timer"}),
    "timers_cancelled": frozenset({"timers"}), "no_timer": frozenset(), "timer_left": frozenset({"timer", "left"}),
    "timer_changed": frozenset({"timer", "left"}), "time": frozenset({"time"}), "date": frozenset({"date"}),
    "weather": frozenset({"place", "temp", "condition", "high", "low"}), "paused": frozenset(),
    "playing": frozenset(), "next": frozenset(), "previous": frozenset(), "volume_set": frozenset({"level"}),
    "louder": frozenset(), "quieter": frozenset(), "muted": frozenset(), "unmuted": frozenset(),
    "added": frozenset({"item"}), "switched_on": frozenset(), "switched_off": frozenset(), "done": frozenset(),
}
MEDIA_REPLIES: Final[Mapping[str, str]] = {"pause": "paused", "play": "playing", "next": "next", "previous": "previous"}
TITLES: Final[Mapping[str, str]] = {  # each intent as the activity log says what was done
    "HassStartTimer": "Started a timer", "HassCancelTimer": "Cancelled a timer",
    "HassCancelAllTimers": "Cancelled the timers", "HassTimerStatus": "Said how long the timers have left",
    "HassIncreaseTimer": "Added time to a timer", "HassDecreaseTimer": "Took time off a timer",
    "HassGetCurrentTime": "Said the time", "HassGetCurrentDate": "Said the date", "HassGetWeather": "Said the weather",
    "HassMediaPause": "Paused the media", "HassMediaUnpause": "Resumed the media",
    "HassMediaNext": "Skipped to the next track", "HassMediaPrevious": "Went back a track",
    "HassSetVolume": "Set the volume", "HassSetVolumeRelative": "Changed the volume",
    "HassMediaPlayerMute": "Muted the sound", "HassMediaPlayerUnmute": "Unmuted the sound",
    "HassShoppingListAddItem": "Added to the to-do list", "HassListAddItem": "Added to a Home Assistant list",
    "HassTurnOn": "Switched on in Home Assistant", "HassTurnOff": "Switched off in Home Assistant",
    "HassLightSet": "Set a light in Home Assistant", "HassSetPosition": "Moved a cover in Home Assistant",
}
_UNITS: Final = (("hours", 3600.0), ("minutes", 60.0), ("seconds", 1.0))
_ARTICLE: Final = re.compile(r"^(?:the|a|an|my|our)\s+", re.IGNORECASE)  # "the pasta" names the pasta timer


# ---------------------------------------------------------------------------------------------------- pure parts
@dataclass(frozen=True, slots=True)
class Slot:
    """One slot of a match.

    Attributes:
        value: its value (a number, a text, a list's value).
        text: the words said for it; "" when the template or the context gave it.
    """

    value: Any
    text: str


@dataclass(frozen=True, slots=True)
class Match:
    """A request that is one of the templates.

    Attributes:
        intent: the intent ("HassStartTimer").
        slots: its slots, by name.
        domain: the Home Assistant domain of what it names (a device's, or the template's: "light"); "" for none.
    """

    intent: str
    slots: Mapping[str, Slot]
    domain: str = ""

    def said(self, name: str) -> bool:
        """Whether this slot was said (the template or the context didn't give it)."""
        slot = self.slots.get(name)
        return slot is not None and bool(slot.text)

    def words(self, name: str) -> str:
        """A slot's value as text ("" when it isn't there): a wildcard's words, a device's name."""
        slot = self.slots.get(name)
        return " ".join(str(slot.value).split()) if slot is not None and slot.value is not None else ""

    def seconds(self, prefix: str = "") -> float:
        """The length its hours, minutes and seconds slots add up to (``prefix`` "start_" for a timer's own)."""
        total = 0.0
        for unit, factor in _UNITS:
            slot = self.slots.get(prefix + unit)
            if slot is not None and isinstance(slot.value, int | float) and not isinstance(slot.value, bool):
                total += float(slot.value) * factor
        return total


@dataclass(frozen=True, slots=True)
class Home:
    """Home Assistant's live context as hassil's lists.

    Attributes:
        names: its devices' names and aliases, each with the device's domain as context (a name that devices of two
            domains share has none, so it never matches).
        areas: its areas' names and aliases, each giving the area's name.
    """

    names: "TextSlotList"
    areas: "TextSlotList"


@dataclass(frozen=True, slots=True)
class Call:
    """One tool an instant command calls.

    Attributes:
        server: the server (a built-in group, or the Home Assistant connector).
        tool: its name there.
        args: its arguments.
        home: whether it is Home Assistant's (its answer is JSON saying what failed).
    """

    server: str
    tool: str
    args: dict[str, JSON]
    home: bool = False


@dataclass(frozen=True, slots=True)
class Plan:
    """What an instant command does.

    Attributes:
        intent: the intent.
        said: the request.
        calls: the tools it calls, in order (none when he only says something).
        reply: what he says once they are done.
    """

    intent: str
    said: str
    calls: tuple[Call, ...]
    reply: str


@dataclass(frozen=True, slots=True)
class Outcome:
    """How an instant command went.

    Attributes:
        reply: what he says; "" when it wasn't done (the brain takes the request).
        why: what stopped it ("" when it was done).
        ms: how long its calls took.
    """

    reply: str
    why: str
    ms: float


class Refused(Exception):
    """An instant command that can't be done exactly as said; the brain takes the request (the reason is logged)."""


def say_duration(seconds: float) -> str:
    """A length as said aloud: "30 seconds", "4 minutes and 10 seconds", "1 hour and 5 minutes" (an hour or more
    leaves the seconds out), "no time" for nothing left."""
    total = max(0, round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    parts = [f"{n} {unit}{'s' * (n != 1)}" for n, unit in ((hours, "hour"), (minutes, "minute"),
                                                          (0 if hours else secs, "second")) if n]
    return " and ".join(parts) if parts else "no time"


def joined(items: Sequence[str]) -> str:
    """Items as said: "a", "a and b", "a, b and c"."""
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def pick_timers(timers: Sequence["ReminderRow"], *, name: str, length: float) -> list["ReminderRow"]:
    """The running timers a request names: by name (exactly, case aside), by length (within ``SAME_LENGTH_S``), or
    all of them when it names neither.

    Args:
        timers: the running timers.
        name: the name said ("pasta"; "" for none).
        length: the length said, in seconds (0 for none).

    Returns:
        The ones that fit, in order.
    """
    from orion.tools.organizer import SAME_LENGTH_S, timer_label

    chosen = list(timers)
    if name:
        chosen = [t for t in chosen if timer_label(t).casefold() == name.casefold()]
    if length:
        chosen = [t for t in chosen if abs((t["due"] - t["ts"]) - length) < SAME_LENGTH_S]
    return chosen


def home_tool(tools: Iterable[Tool], server: str, intent: str) -> Tool | None:
    """The Home Assistant connector's tool for an intent: named after it, alone or after a namespace (its MCP server
    lists "HassTurnOn", newer ones "intent__HassTurnOn").

    Args:
        tools: the tools advertised now.
        server: the connector's server name ("" for none).
        intent: the intent.

    Returns:
        The tool, or None.
    """
    if not server:
        return None
    return next((t for t in tools if t.server == server and (t.remote == intent or t.remote.endswith(f"__{intent}"))),
                None)


def home_args(match: Match, tool: Tool, area: str) -> dict[str, JSON]:
    """A match's slots as the arguments of Home Assistant's tool of its intent.

    Every slot must be one of the tool's arguments: leaving one out would change what is done ("the lights upstairs"
    becoming every light). The area a template takes from the context is this PC's (``instant.area``). A device's
    domain is added when the tool takes one, so its name can't reach a device of another kind.

    Args:
        match: the match.
        tool: the tool.
        area: Home Assistant's area this PC is in ("" when unknown).

    Returns:
        The arguments (an array argument gets a list).

    Raises:
        Refused: a slot the tool can't take, or the context's area while this PC has none.
    """
    properties = tool.parameters.get("properties")
    props: Mapping[str, Any] = properties if isinstance(properties, dict) else {}
    values: dict[str, Any] = {}
    for name, slot in match.slots.items():
        value = slot.value
        if name == "area" and value == HERE:
            if not area:
                raise Refused("it means the room this PC is in, and instant.area doesn't say which")
            value = area
        if name not in props:
            raise Refused(f"Home Assistant's {tool.remote} takes no {name}")
        values[name] = value
    if match.domain and "domain" in props and "domain" not in values:
        values["domain"] = match.domain
    args: dict[str, JSON] = {}
    for name, value in values.items():
        if isinstance(value, float) and value.is_integer():
            value = int(value)  # a range's 50.0 is the 50 a tool's integer argument takes
        spec = props[name] if isinstance(props[name], dict) else {}
        args[name] = [value] if spec.get("type") == "array" and not isinstance(value, list) else value
    return args


def home_failed(text: str) -> str:
    """Why a Home Assistant tool's answer says it failed ("" when it didn't): an error response, or targets it
    couldn't change.

    Args:
        text: the answer (JSON of Home Assistant's intent response).
    """
    try:
        data = json.loads(text)
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    if data.get("response_type") == "error":
        return str(data.get("data", {}).get("code") or "Home Assistant said it failed")
    failed = (data.get("data") or {}).get("failed") if isinstance(data.get("data"), dict) else None
    return f"Home Assistant couldn't change {len(failed)} of them" if failed else ""


def parse_live_context(text: str) -> Home:
    """Home Assistant's live context (what its GetLiveContext tool answers) as the lists of its names and areas.

    The answer is JSON whose "result" is a header line and a YAML list of devices, each with its "names" and "areas"
    (comma-separated, the first area being the area's own name, the rest its aliases) and its "domain".

    Args:
        text: the tool's answer.

    Returns:
        The lists.

    Raises:
        ValueError: the text isn't a live context.
    """
    import yaml
    from hassil import TextSlotList
    from hassil.expression import TextChunk
    from hassil.intents import TextSlotValue
    from hassil.util import normalize_text

    body = text
    with contextlib.suppress(ValueError):
        data = json.loads(text)
        if isinstance(data, dict) and isinstance(data.get("result"), str):
            body = data["result"]
    head, _, rest = body.partition("\n")
    if not head.startswith("Live Context"):
        raise ValueError("this isn't Home Assistant's live context")
    try:
        entities = yaml.load(rest, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
    except yaml.YAMLError as e:
        raise ValueError(f"Home Assistant's live context isn't YAML: {e}") from None
    match entities:
        case dict():
            devices = list(entities.values())
        case list():
            devices = entities
        case _:
            raise ValueError("Home Assistant's live context lists no devices")
    domains: dict[str, set[str]] = {}
    spelled: dict[str, str] = {}
    areas: dict[str, tuple[str, str]] = {}
    for entity in devices:
        if not isinstance(entity, dict):
            continue
        domain = str(entity.get("domain") or "")
        for name in _listed(entity.get("names")):
            domains.setdefault(name.casefold(), set()).add(domain)
            spelled.setdefault(name.casefold(), name)
        if found := _listed(entity.get("areas")):
            for alias in found:
                areas.setdefault(alias.casefold(), (alias, found[0]))
    names = [TextSlotValue(TextChunk(normalize_text(spelled[key])), spelled[key],
                           {"domain": next(iter(kinds))} if len(kinds) == 1 else None)
             for key, kinds in domains.items()]
    places = [TextSlotValue(TextChunk(normalize_text(alias)), area) for alias, area in areas.values()]
    return Home(TextSlotList(name="name", values=names), TextSlotList(name="area", values=places))


def _listed(value: object) -> list[str]:
    """A comma-separated field of the live context as its items."""
    return [part.strip() for part in value.split(",") if part.strip()] if isinstance(value, str) else []


def _rank(result: "RecognizeResult") -> tuple[int, int, int, int, int]:
    """How good a reading is, lower better. As Home Assistant's own matching has it: a reading that names a device
    (the longest name) beats one that only names an area or a kind ("the kitchen light" is the device called that,
    not every light in the kitchen); then fewer wildcards, more literal words, less text in wildcards."""
    wildcards = [e for e in result.entities_list if e.is_wildcard]
    device = result.entities.get("name")
    named = device is not None and not device.is_wildcard
    return (0 if named else 1, -len(device.text) if named and device is not None else 0, len(wildcards),
            -result.text_chunks_matched, sum(len(e.text or "") for e in wildcards))


# ---------------------------------------------------------------------------------------------------- the matcher
class Matcher:
    """Home Assistant's templates of some intents, matched whole by hassil. Not thread-safe: ``Instant`` uses it from
    one thread of its own."""

    def __init__(self, data: Mapping[str, Any], intents: Iterable[str]) -> None:
        """Parse the templates of these intents and make the first matches (blocking).

        Args:
            data: a language's intents file (the lists, expansion rules, skip words and intents of Home Assistant's
                sentence files).
            intents: the intents to keep (``instant.intents``).
        """
        from hassil import Intents, TextSlotList

        wanted = frozenset(intents)
        self.intents: Intents = Intents.from_dict(
            {**data, "intents": {name: body for name, body in data["intents"].items() if name in wanted}})
        named: set[str] = set()
        provided = set(self.intents.slot_lists)
        for intent in self.intents.intents.values():
            for block in intent.data:
                rules = {**self.intents.expansion_rules, **block.expansion_rules}
                provided |= block.slot_lists.keys()
                for sentence in block.sentences:
                    named.update(sentence.list_names(rules))
        # Lists only Home Assistant fills (its names, areas and floors): empty until it does, so nothing matches them.
        self.empty: dict[str, TextSlotList] = {name: TextSlotList(name=name, values=[]) for name in named - provided}
        for text in WARM_UP:
            self.match(text, None)

    @classmethod
    def english(cls, intents: Iterable[str]) -> "Matcher":
        """The English templates of the home-assistant-intents package.

        Args:
            intents: the intents to keep.

        Returns:
            The matcher.

        Raises:
            RuntimeError: the package has no English sentences.
        """
        import home_assistant_intents

        data = home_assistant_intents.get_intents(LANGUAGE)
        if data is None:
            raise RuntimeError("home-assistant-intents has no English sentences")
        return cls(data, intents)

    def match(self, text: str, home: Home | None, skip: Sequence[str] = ()) -> Match | None:
        """The one best reading of a request as a template.

        Args:
            text: the request.
            home: Home Assistant's names and areas (None while it isn't connected).
            skip: words left out of the request besides the templates' own ("please", "can you"): how he is
                addressed ("sir").

        Returns:
            The match, or None when no template fits, or two equally good readings differ.
        """
        from hassil import recognize_all

        lists: dict[str, Any] = dict(self.empty)
        if home is not None:
            lists |= {"name": home.names, "area": home.areas}
        best: list[RecognizeResult] = []
        best_rank: tuple[int, ...] | None = None
        for result in recognize_all(text, self.intents, slot_lists=lists, skip_words=list(skip),
                                    intent_context=dict(CONTEXT), language=LANGUAGE):
            rank = _rank(result)
            if best_rank is None or rank < best_rank:
                best, best_rank = [result], rank
            elif rank == best_rank:
                best.append(result)
        readings = {_reading(result) for result in best}
        if len(readings) != 1:
            if readings:
                log.debug("instant: %d equally good readings, none taken", len(readings))
            return None
        result = best[0]
        slots = {name: Slot(entity.value, entity.text.strip()) for name, entity in result.entities.items()}
        domain = slots["domain"].value if "domain" in slots else result.context.get("domain")
        return Match(result.intent.name, slots, domain if isinstance(domain, str) else "")


def _reading(result: "RecognizeResult") -> tuple[str, tuple[tuple[str, str], ...]]:
    """What a reading means: its intent and its slots' values (two parses that mean the same are one reading)."""
    return result.intent.name, tuple(sorted((name, repr(entity.value)) for name, entity in result.entities.items()))


# ---------------------------------------------------------------------------------------------------- the engine
class InstantHub(Protocol):
    """What instant commands need of the hub (``mcp.MCPHub`` provides it)."""

    def tools(self) -> Mapping[str, Tool]:
        """The tools advertised now, by model-visible name."""

    async def call(self, server: str, tool: str, args: Mapping[str, JSON], ctx: CallContext) -> str:
        """Run a tool; never raises ("Error: ..." in words)."""


class Instant:
    """Instant commands for the turn runner (see the module docstring). Use it on the event loop."""

    def __init__(self, cfg: "Config", hub: InstantHub, store: "Store", *,
                 weather: Callable[[str], Awaitable["Weather | None"]], home_city: Callable[[], str],
                 activity: "Activity", spawn: Callable[..., "asyncio.Task[Any]"]) -> None:
        """Prepare; the templates load in ``load``.

        Args:
            cfg: the live settings.
            hub: runs the tools.
            store: the database (the running timers).
            weather: the weather service's forecast for a place (today's included).
            home_city: the user's city ("" when unknown).
            activity: the activity log.
            spawn: the runtime's way of running background work (``spawn(job, name=...)``).
        """
        self.cfg = cfg
        self.hub = hub
        self.store = store
        self.weather = weather
        self.home_city = home_city
        self.activity = activity
        self.spawn = spawn
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="instant")
        self._matcher: Matcher | None = None
        self._loaded: tuple[str, ...] | None = None  # the intents the matcher holds
        self._loading: asyncio.Task[None] | None = None
        self._home: Home | None = None
        self._home_at = 0.0  # when Home Assistant's names were last asked for (monotonic seconds; 0: never)
        self._home_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------------------------------------ lifecycle
    async def load(self) -> None:
        """Load the templates of ``instant.intents`` on the matcher's thread (a failure is logged; requests then go
        to the brain)."""
        wanted = tuple(self.cfg.instant.intents)
        started = time.perf_counter()
        try:
            matcher = await asyncio.get_running_loop().run_in_executor(self._pool, Matcher.english, wanted)
        except Exception:
            log.exception("instant commands couldn't load: every request goes to the brain")
            self._loaded = wanted  # not tried again until the intents change
            return
        self._matcher, self._loaded = matcher, wanted
        log.info("instant commands: %d intents ready in %.0f ms", len(matcher.intents.intents),
                 (time.perf_counter() - started) * 1000)

    def tools_changed(self) -> None:
        """The hub's toolbox changed: read Home Assistant's names once it is connected, forget them once it is
        gone."""
        if self._tool(LIVE_CONTEXT) is None:
            self._home = None
            return
        if self._home is None:
            self._read_home()

    async def close(self) -> None:
        """Stop the matcher's thread."""
        self._pool.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------------------------------------ requests
    async def match(self, text: str) -> Match | None:
        """Whether a request is an instant command (about a millisecond, on the matcher's thread).

        Args:
            text: the request, as heard or typed.

        Returns:
            Its match; None when instant commands are off, not loaded yet, or nothing fits (the brain answers). A stop
            phrase or a dismissal ("stop", "thanks") never is one: those are the voice's to handle.
        """
        from orion.voice.wake import Phrases, address, is_dismissal, is_stop

        cfg = self.cfg
        if not cfg.instant.enabled:
            return None
        if tuple(cfg.instant.intents) != self._loaded:
            if self._loading is None or self._loading.done():
                self._loading = self.spawn(self.load(), name="load instant commands")
            return None
        matcher = self._matcher
        if matcher is None:
            return None
        phrases, wake = Phrases.from_config(cfg), cfg.wake
        edits = wake.name_max_edits
        if (said := address(text, phrases, wake.lead_words, max_edits=edits)).addressed:
            text = said.request  # typed "Orion, what time is it?": his name isn't part of the request
        if is_stop(text, phrases, max_edits=edits) or is_dismissal(text, phrases, max_edits=edits):
            return None
        home = self._home if self._tool(LIVE_CONTEXT) is not None else None
        if home is not None and time.monotonic() - self._home_at >= cfg.instant.context_s:
            self._read_home()
        return await asyncio.get_running_loop().run_in_executor(self._pool, matcher.match, text, home,
                                                                (cfg.user.honorific,))

    async def plan(self, match: Match, said: str) -> Plan | None:
        """What a match does, with what he will say; nothing is done yet.

        Args:
            match: the match.
            said: the request.

        Returns:
            The plan, or None when it can't be done exactly as said (the reason is logged; the brain answers).
        """
        try:
            return await self._plan(match, said)
        except Refused as e:
            log.info("instant %s handed to the brain: %s", match.intent, e)
            return None

    async def execute(self, plan: Plan, ctx: CallContext) -> Outcome:
        """Run a plan's calls in order, stopping at the first that fails, and record it in the activity log.

        Args:
            plan: the plan.
            ctx: who asks (origin "user"), in which turn, from where.

        Returns:
            The outcome: the reply, or why it wasn't done (then the brain takes the request).
        """
        started = time.monotonic()
        why = ""
        for call in plan.calls:
            text = await self.hub.call(call.server, call.tool, call.args, ctx)
            if text.startswith("Error"):
                why = text.removeprefix("Error:").strip()
            elif call.home:
                why = home_failed(text)
            if why:
                break
        ms = (time.monotonic() - started) * 1000
        first = plan.calls[0] if plan.calls else None
        self.activity.record("instant", source=ctx.source, title=TITLES[plan.intent],
                             outcome="failed" if why else "ok", actor="you, without the model",
                             connector=first.server if first else "", tool=first.tool if first else "",
                             detail={"heard": plan.said, "intent": plan.intent,
                                     **({"why": f"{why} (the brain took it)"} if why else {})},
                             turn=ctx.turn or "", ms=round(ms))
        if why:
            log.info("instant %s failed, the brain takes it: %s", plan.intent, why)
            return Outcome("", why, ms)
        log.info("instant %s: %d call%s, %.0f ms", plan.intent, len(plan.calls), "s" * (len(plan.calls) != 1), ms)
        return Outcome(plan.reply, "", ms)

    # ------------------------------------------------------------------------------------------------ planning
    async def _plan(self, match: Match, said: str) -> Plan:
        """``plan``, raising ``Refused``."""
        intent = match.intent
        if intent in HOME_ONLY or ((intent in MEDIA or intent in SOUND)
                                   and (match.said("area") or "name" in match.slots or "floor" in match.slots)):
            return self._home_plan(match, said)
        if intent in TIMERS:
            return self._timer_plan(match, said)
        if intent in MEDIA:
            action = MEDIA[intent]
            return Plan(intent, said, (Call("system", "media", {"action": action}),), self._say(MEDIA_REPLIES[action]))
        if intent in SOUND:
            return self._sound_plan(match, said)
        if intent == "HassGetCurrentTime":
            return Plan(intent, said, (), self._say("time", time=local_now().strftime(self.cfg.instant.time_format)))
        if intent == "HassGetCurrentDate":
            return Plan(intent, said, (), self._say("date", date=local_now().strftime(self.cfg.instant.date_format)))
        if intent == "HassGetWeather":
            return await self._weather_plan(match, said)
        if intent == "HassShoppingListAddItem":
            item = match.words("item")
            if not item:
                raise Refused("nothing to add")
            return Plan(intent, said, (Call("organizer", "todo", {"action": "add", "item": item}),),
                        self._say("added", item=item))
        raise Refused(f"{intent} is not one Orion does")  # pragma: no cover (instant.intents is checked)

    def _timer_plan(self, match: Match, said: str) -> Plan:
        """Timers: Orion's organizer, the running ones read from the database."""
        from orion.tools.organizer import spoken_duration, timer_name

        intent = match.intent
        if match.said("area"):
            raise Refused("a timer of a room is Home Assistant's")
        if intent == "HassStartTimer":
            if "conversation_command" in match.slots:
                raise Refused("a timer that runs a command")
            seconds = match.seconds()
            if seconds <= 0:
                raise Refused("a timer without a length")
            label = _ARTICLE.sub("", match.words("name"))
            name = f"{label} timer" if label else f"{spoken_duration(seconds)} timer"
            return Plan(intent, said, (Call("organizer", "set_timer", {"duration": f"{seconds:g} seconds",
                                                                         "label": label}),),
                        self._say("timer_started", timer=name))
        timers = [t for t in self.store.pending_reminders() if t["kind"] == "timer"]
        if not timers:
            return Plan(intent, said, (), self._say("no_timer"))
        if intent == "HassCancelAllTimers":
            return Plan(intent, said, tuple(Call("organizer", "cancel_timer_or_reminder", {"which": f"#{t['id']}"})
                                            for t in timers),
                        self._say("timers_cancelled", timers=joined([f"your {timer_name(t)}" for t in timers])))
        chosen = pick_timers(timers, name=_ARTICLE.sub("", match.words("name")), length=match.seconds("start_"))
        if intent == "HassTimerStatus":
            if not chosen:
                raise Refused("no running timer is called that")
            now = time.time()
            return Plan(intent, said, (), " ".join(self._say("timer_left", timer=timer_name(t),
                                                             left=say_duration(t["due"] - now)) for t in chosen))
        if len(chosen) != 1:
            raise Refused(f"{len(chosen)} running timers fit")
        target = chosen[0]
        if intent == "HassCancelTimer":
            return Plan(intent, said, (Call("organizer", "cancel_timer_or_reminder", {"which": f"#{target['id']}"}),),
                        self._say("timer_cancelled", timer=timer_name(target)))
        change = match.seconds() * (1 if intent == "HassIncreaseTimer" else -1)
        if not change:
            raise Refused("no time to add or take off")
        left = target["due"] - time.time() + change
        return Plan(intent, said, (Call("organizer", "change_timer", {"which": f"#{target['id']}",
                                                                        "by": f"{change:+g} seconds"}),),
                    self._say("timer_changed", timer=timer_name(target), left=say_duration(left)))

    def _sound_plan(self, match: Match, said: str) -> Plan:
        """This PC's volume (the system's volume tool)."""
        intent = match.intent
        if intent == "HassMediaPlayerMute":
            return Plan(intent, said, (Call("system", "volume", {"action": "mute"}),), self._say("muted"))
        if intent == "HassMediaPlayerUnmute":
            return Plan(intent, said, (Call("system", "volume", {"action": "unmute"}),), self._say("unmuted"))
        if intent == "HassSetVolume":
            level = match.slots["volume_level"].value
            if not isinstance(level, int | float) or not 0 <= level <= 100:
                raise Refused(f"a volume of {level}")
            return Plan(intent, said, (Call("system", "volume", {"action": "set", "level": int(level)}),),
                        self._say("volume_set", level=f"{int(level)}"))
        step = match.slots["volume_step"].value
        args: dict[str, JSON]
        if step in ("up", "down"):
            args = {"action": str(step)}  # the usual step (system.volume_step)
        elif isinstance(step, int | float) and step and abs(step) <= 100:
            args = {"action": "up" if step > 0 else "down", "level": int(abs(step))}
        else:
            raise Refused(f"a volume step of {step}")
        return Plan(intent, said, (Call("system", "volume", args),), self._say("louder" if args["action"] == "up"
                                                                              else "quieter"))

    async def _weather_plan(self, match: Match, said: str) -> Plan:
        """The weather of the home city now, from the weather service (cached a while)."""
        import httpx

        if "name" in match.slots:
            raise Refused("the weather of a Home Assistant device")
        city = self.home_city()
        if not city:
            raise Refused("the home city is unknown")
        try:
            weather = await self.weather(city)
        except httpx.HTTPError as e:
            raise Refused(f"the weather service couldn't be reached: {e}") from None
        if weather is None or not weather["days"]:
            raise Refused(f"no weather for {city}")
        today = weather["days"][0]
        return Plan(match.intent, said, (), self._say(
            "weather", place=weather["place"].split(",")[0], temp=f"{weather['temp']}",
            condition=weather["condition"], high=f"{today['max']}", low=f"{today['min']}"))

    def _home_plan(self, match: Match, said: str) -> Plan:
        """Lights and devices: the Home Assistant connector's tool of the same intent."""
        cfg = self.cfg.instant
        tool = self._tool(match.intent)
        if tool is None:
            raise Refused("Home Assistant isn't connected, or has no tool for it")
        kind = match.domain or ("media_player" if match.intent in MEDIA or match.intent in SOUND else "")
        if kind not in cfg.device_domains:
            raise Refused(f"a {kind or 'kind of'} device isn't one of instant.device_domains")
        args = home_args(match, tool, cfg.area)
        intent = match.intent
        if intent in MEDIA:
            reply = self._say(MEDIA_REPLIES[MEDIA[intent]])
        elif intent == "HassTurnOn" or intent == "HassTurnOff":
            reply = self._say("switched_on" if intent == "HassTurnOn" else "switched_off")
        elif intent == "HassListAddItem":
            reply = self._say("added", item=match.words("item"))
        elif intent == "HassMediaPlayerMute" or intent == "HassMediaPlayerUnmute":
            reply = self._say("muted" if intent == "HassMediaPlayerMute" else "unmuted")
        else:
            reply = self._say("done")
        return Plan(intent, said, (Call(tool.server, tool.remote, args, home=True),), reply)

    # ------------------------------------------------------------------------------------------------ helpers
    def _say(self, key: str, **values: str) -> str:
        """One of ``instant.replies``, filled in, its first letter a capital."""
        honorific = self.cfg.user.honorific
        text = self.cfg.instant.replies[key].format(honorific=honorific,
                                                    Honorific=honorific[:1].upper() + honorific[1:], **values)
        return text[:1].upper() + text[1:]

    def _tool(self, intent: str) -> Tool | None:
        """The Home Assistant connector's tool for an intent (or its live context), while it is connected."""
        return home_tool(self.hub.tools().values(), self.cfg.instant.home_assistant, intent)

    def _read_home(self) -> None:
        """Read Home Assistant's names and areas again in the background (one read at a time)."""
        if self._home_task is not None and not self._home_task.done():
            return
        self._home_at = time.monotonic()
        self._home_task = self.spawn(self._fetch_home(), name="read Home Assistant's names")

    async def _fetch_home(self) -> None:
        """Ask Home Assistant's live context for its names and areas (its lists are built on the matcher's
        thread); a failure keeps the old lists and is tried again after ``instant.context_s``."""
        tool = self._tool(LIVE_CONTEXT)
        if tool is None:
            return
        text = await self.hub.call(tool.server, tool.remote, {}, CallContext("hud", client=CLIENT))
        if text.startswith("Error"):
            log.warning("Home Assistant's devices couldn't be read for instant commands: %s", text)
            return
        try:
            home = await asyncio.get_running_loop().run_in_executor(self._pool, parse_live_context, text)
        except ValueError as e:
            log.warning("Home Assistant's devices couldn't be read for instant commands: %s", e)
            return
        self._home = home
        log.info("instant commands know %d of Home Assistant's names and %d areas", len(home.names.values),
                 len(home.areas.values))
