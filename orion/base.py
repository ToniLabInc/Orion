"""The shared vocabulary of ORION: the types every layer passes around, the one event bus and two small caches.

This module is a leaf. It imports nothing else from ``orion`` and only light parts of the standard library at
import time; asyncio, hashlib, json and logging load inside the few functions that need them, so pure modules and
their tests import it in milliseconds.

It holds:
- the JSON and event aliases; who asked for a tool call (``Origin``), who asks to change settings (``Actor``) and
  where a memory came from (``Provenance``), with the ``TRUSTED`` sources that may reach the core memory and the
  values allowed on the MCP wire;
- the chat ``Message``, the model's ``ToolCall``, the brain's ``ToolRequest`` and the ``CallContext`` the hub gets;
- ``Tool``, ``ToolView`` and ``ToolSet`` (the advertised toolbox), ``GroupInfo`` and ``Skill``;
- ``StepRecord`` (one operation of the trace, as the HUD and the database keep it), ``TurnResult`` and ``Query``;
- ``format_transcript``, the one transcript formatter (Claude session replay and the memory review);
- small readers of settings and data: ``server_on``, ``switched_off``, ``number`` and ``FRONT_MATTER``;
- ``lower_priority``: background work yields the processor to everything else;
- ``logged`` and ``conversations_logged``: what a log line may show of something said or remembered
  (``memory.log_conversations``);
- ``EventBus`` (thread-safe fan-out to sinks on the event loop), ``Expiring`` (an async single-flight value with a
  TTL) and ``TTLCache`` (a small thread-safe LRU with per-entry expiry).
"""

import contextlib
import functools
import math
import os
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, Required, TypedDict

if TYPE_CHECKING:
    import asyncio
    from datetime import datetime, tzinfo

    import numpy as np
    from numpy.typing import NDArray

type JSON = str | int | float | bool | list[JSON] | dict[str, JSON] | None
type Event = dict[str, JSON]  # one HUD event: always "type" and "ts" (epoch seconds), then its fields
type Publish = Callable[..., None]  # publish(type: str, /, **data: JSON) -> None
type Origin = Literal["brain", "hud", "app", "user"]  # who asked for a tool call ("user": theirs, no model)
type Actor = Literal["hud", "ai"]  # who asks to change settings or servers: the user at the HUD, or the AI
type Provenance = Literal["brain", "tainted", "hud", "app", "review", "hand"]  # where a memory came from
type Channel = Literal["voice", "text"]  # how a reply reaches the user
type TurnSource = Literal["voice", "hud", "discord", "job"]  # where a turn's request came from
type AssistantState = Literal["idle", "loading", "listening", "thinking", "speaking"]
type StepKind = Literal["tool", "memory", "skill", "recall"]  # the kinds of rows in the operation trace

# Sources whose memory writes may reach USER.md and MEMORY.md ("hud" is the user at the HUD).
TRUSTED: Final[frozenset[Provenance]] = frozenset({"brain", "hud", "review", "hand"})
# The provenance values a request may carry in its MCP _meta["orion/provenance"].
WIRE_PROVENANCE: Final[frozenset[str]] = frozenset({"brain", "tainted", "hud", "app"})
# The _meta keys Orion's hub and his tools process exchange: on a call request, and on its result (events).
PROVENANCE_META: Final = "orion/provenance"  # where the request comes from (one of WIRE_PROVENANCE)
QVEC_META: Final = "orion/qvec"  # the query vector of the tool's embed_arg: base64 of float16 values
EVENTS_META: Final = "orion/events"  # on the result: the HUD events the tool published
CONTEXT_META: Final = "orion/context"  # the turn, the request's source and its device (Orion's own in-process tools)

_OMITTED: Final = "(answer drawn from web or app content omitted)"  # an assistant reply format_transcript hides
_VIEWS_KEPT: Final = 64  # memoised ToolView objects per ToolSet (one per distinct ordered selection)


class OrionError(Exception):
    """Base class of ORION's own errors: a problem he can describe in plain words."""


type LLMErrorKind = Literal["limit", "billing", "auth", "unavailable", "timeout", "model", "other"]


class LLMError(OrionError):
    """A language-model problem worth saying out loud: a bad key, a rate limit, an unknown model, a timeout.

    Attributes:
        kind: what went wrong, as ``brain.fallback_on`` names it: "limit" (a usage or rate limit), "billing", "auth"
            (signed out, a bad key), "unavailable" (not installed, not running, unreachable), "timeout", "model" (a
            model the provider doesn't have) or "other".
    """

    def __init__(self, message: str = "", kind: LLMErrorKind = "other") -> None:
        """Say what went wrong.

        Args:
            message: the sentence to show and say.
            kind: what kind of problem it is.
        """
        super().__init__(message)
        self.kind: LLMErrorKind = kind


@functools.cache
def local_zone() -> "tzinfo":
    """This PC's time zone, with its daylight-saving rules: ``$TZ``, else the system's (tzlocal).

    Returns:
        The zone.
    """
    from tzlocal import get_localzone

    return get_localzone()


def local_now() -> "datetime":
    """The current local time, aware of the zone (see ``local_zone``).

    Returns:
        ``datetime.now(local_zone())``.
    """
    from datetime import datetime

    return datetime.now(local_zone())


def new_id() -> str:
    """Return a fresh random id for a turn or an operation.

    Returns:
        Eight hex characters from the operating system's random source, as ``secrets.token_hex(4)`` gives.
    """
    return os.urandom(4).hex()


@dataclass(frozen=True, slots=True)
class Conv:
    """One conversation: its database id and when it started.

    Attributes:
        id: the conversation's row id.
        started: when it started, in epoch seconds.
    """

    id: int
    started: float


class Message(TypedDict, total=False):
    """A neutral chat message, as the brain keeps its history and the providers' adapters translate it.

    Keys:
        role: "system", "user", "assistant" or "tool" (required).
        content: the text (required; "" for an assistant message that only calls tools).
        tool_calls: an assistant's tool calls, each ``{"id", "name", "arguments"}`` (``ToolCall.as_dict()``).
        tool_call_id: the call a tool message answers.
        name: the tool a tool message comes from.
        raw: the user's own words, without the per-turn tail the content carries.
        turn: the turn id the message belongs to.
        ts: when it was said, in epoch seconds.
        images: pictures that go with it (a user's attachment, a tool's screenshot), each an ``Image``; only for the
            turn that has them (the history keeps a note instead).
    """

    role: Required[Literal["system", "user", "assistant", "tool"]]
    content: Required[str]
    tool_calls: list[dict[str, JSON]]
    tool_call_id: str
    name: str
    raw: str
    turn: str
    ts: float
    images: "list[Image]"


class Image(TypedDict):
    """A picture a model may look at, ready to send: sized down and compressed (``orion.images``).

    Keys:
        mime: its type ("image/jpeg", "image/png", "image/webp").
        data: its bytes, base64 (no "data:" prefix).
        label: what it shows, in a few words ("the screen", "a photo from Discord").
    """

    mime: str
    data: str
    label: str


@dataclass(frozen=True, slots=True)
class ToolOutput:
    """What a tool call answered: its text, and the pictures it returned.

    Attributes:
        text: the result text ("Error: ..." when it failed).
        images: the pictures (a screenshot, a camera frame), prepared for the models.
    """

    text: str
    images: tuple[Image, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A tool call a model asked for.

    Attributes:
        id: the model's id for the call, matched by the tool result.
        name: the model-visible tool name.
        arguments: the arguments, parsed from the model's JSON.
    """

    id: str
    name: str
    arguments: dict[str, JSON]

    def as_dict(self) -> dict[str, JSON]:
        """Return the call as a plain dict for ``Message["tool_calls"]``.

        Returns:
            ``{"id": ..., "name": ..., "arguments": ...}`` sharing this call's arguments dict.
        """
        return {"id": self.id, "name": self.name, "arguments": self.arguments}


@dataclass(frozen=True, slots=True)
class ToolRequest:
    """A tool call the brain asks the hub to run.

    Attributes:
        name: the model-visible tool name.
        args: the arguments.
    """

    name: str
    args: dict[str, JSON]


@dataclass(frozen=True, slots=True)
class CallContext:
    """Who is calling a tool, handed to the hub with every call.

    Attributes:
        origin: "brain" for the model, "hud" for the user at the HUD, "app" for another AI app over MCP,
            "user" for the user's own command run without the model (an instant command).
        turn: the turn the call belongs to, None outside a turn.
        client: the other app's MCP client name (origin "app"), "" otherwise.
        source: where the turn's request came from ("voice", "hud", "discord", "job"...; "" outside a turn): a
            question before a risky call is also asked that way.
        device: the HUD the request came from or that carries the voice ("" for this machine itself): what is on
            its screen, its clipboard (``orion.surfaces``).
    """

    origin: Origin
    turn: str | None = None
    client: str = ""
    source: str = ""
    device: str = ""


@dataclass(slots=True, eq=False)
class Tool:
    """A tool as the brain and the hub see it, whichever server provides it.

    Instances compare and hash by identity. The fields are not meant to change after construction: ``schema()``
    is built once from them.

    Attributes:
        name: the name the model sees (an external tool carries its server's name in front).
        description: what it does, for the model.
        parameters: the JSON schema of its arguments.
        server: the server that provides it.
        remote: its name on that server.
        title: a short title for people ("" when the server gives none).
        summary: the first sentence of the description, for people.
        examples: requests it serves, which tool routing ranks against.
        hints: MCP's read_only, destructive, open_world and idempotent hints (None or absent when unsaid).
        taints: whether a successful call makes the rest of its turn untrusted (see ``tools.policy.taints``).
        embed_arg: the argument whose embedding the hub sends along as a query vector ("" for none).
        builtin: whether it is one of Orion's own tools.
        requires: what the machine must have for it to work (``orion.host.NEEDS``): offered only where it does.
    """

    name: str
    description: str
    parameters: dict[str, JSON]
    server: str
    remote: str
    title: str = ""
    summary: str = ""
    examples: tuple[str, ...] = ()
    hints: Mapping[str, bool | None] = field(default_factory=dict)
    taints: bool = False
    embed_arg: str = ""
    builtin: bool = False
    requires: tuple[str, ...] = ()
    _schema: dict[str, JSON] | None = field(default=None, init=False, repr=False)

    def schema(self) -> dict[str, JSON]:
        """Return the OpenAI-style function schema, built on the first call and reused after.

        Returns:
            ``{"type": "function", "function": {"name", "description", "parameters"}}``; treat it as read-only.
        """
        if self._schema is None:
            self._schema = {"type": "function", "function": {"name": self.name, "description": self.description,
                                                             "parameters": self.parameters}}
        return self._schema

    @property
    def read_only(self) -> bool:
        """Whether the tool declares that it changes nothing (MCP's read_only hint is True)."""
        return self.hints.get("read_only") is True


@dataclass(frozen=True, slots=True)
class ToolView:
    """An ordered selection of tools with everything a provider needs, computed once.

    Attributes:
        tools: the tools, in order.
        schemas: their function schemas, in the same order.
        digest: blake2b (16 bytes, hex) of the schemas' canonical JSON; equal digests mean identical tool lists.
    """

    tools: tuple[Tool, ...]
    schemas: tuple[dict[str, JSON], ...]
    digest: str


class ToolSet:
    """One version of the toolbox: tools by model-visible name, and memoised views of ordered selections.

    A ToolSet never changes after it is built; a new toolbox (a server connected, a tool switched off) is a new
    ToolSet with a higher version. Views are kept for the last 64 distinct selections. Safe to use from any thread.

    Attributes:
        version: the hub's toolbox version this set was built from.
    """

    __slots__ = ("_lock", "_names", "_tools", "_views", "version")

    version: int

    def __init__(self, tools: Mapping[str, Tool], version: int) -> None:
        """Copy the tools and remember the version.

        Args:
            tools: the tools by model-visible name, in the order they are listed.
            version: the hub's toolbox version this set was built from.
        """
        self._tools: dict[str, Tool] = dict(tools)
        self._names: tuple[str, ...] = tuple(self._tools)
        self._views: dict[tuple[str, ...], ToolView] = {}
        self._lock = threading.Lock()
        self.version = version

    def get(self, name: str) -> Tool | None:
        """Return the tool with this model-visible name, or None when there is none."""
        return self._tools.get(name)

    def names(self) -> tuple[str, ...]:
        """Return every model-visible name, in listing order."""
        return self._names

    def view(self, names: Sequence[str]) -> ToolView:
        """Return the ordered selection of tools with these names, built once per distinct sequence.

        Args:
            names: model-visible names in the order the model should see them; unknown names and repeats are
                skipped.

        Returns:
            The memoised ``ToolView`` for exactly this sequence of names.
        """
        key = tuple(names)
        with self._lock:
            found = self._views.get(key)
        if found is not None:
            return found
        built = _build_view(key, self._tools)
        with self._lock:
            found = self._views.get(key)
            if found is not None:
                return found
            if len(self._views) >= _VIEWS_KEPT:
                del self._views[next(iter(self._views))]
            self._views[key] = built
        return built


def _build_view(names: tuple[str, ...], tools: Mapping[str, Tool]) -> ToolView:
    """Build the view of ``names`` over ``tools``: known names only, each at its first position, schemas hashed.

    Args:
        names: the requested model-visible names, in order.
        tools: the toolbox's tools by model-visible name.

    Returns:
        The new ``ToolView``.
    """
    import hashlib
    import json

    chosen = {name: tools[name] for name in names if name in tools}
    picked = tuple(chosen.values())
    schemas = tuple(tool.schema() for tool in picked)
    canonical = json.dumps(schemas, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.blake2b(canonical.encode(), digest_size=16).hexdigest()
    return ToolView(tools=picked, schemas=schemas, digest=digest)


@dataclass(frozen=True, slots=True)
class GroupInfo:
    """A built-in tool group, as the hub and the Abilities page show it.

    Attributes:
        name: the group's server name, e.g. "system".
        title: its title for people, e.g. "PC control".
        description: what its tools do, in a sentence or two.
    """

    name: str
    title: str
    description: str


@dataclass(frozen=True, slots=True)
class Skill:
    """A skill: instructions saved as a SKILL.md file, started by name or by a trigger phrase.

    Attributes:
        name: the skill's normalised name ("plugin:skill" for a plugin's).
        description: what it is for, in one line.
        body: the instructions themselves.
        tools: the tools it uses.
        triggers: phrases that start it directly.
        path: its SKILL.md file (a plugin's command: its .md file).
        source: where it comes from: "builtin" (ships with Orion), "learned" (he wrote it from what you taught him),
            "yours" (you wrote or imported it) or "plugin".
        plugin: the plugin it comes with ("" for none).
        origin: where an imported skill was imported from ("" for one written here).
    """

    name: str
    description: str
    body: str
    tools: tuple[str, ...]
    triggers: tuple[str, ...]
    path: Path
    source: str = "yours"
    plugin: str = ""
    origin: str = ""

    @property
    def trusted(self) -> bool:
        """Whether its instructions are Orion's or the user's own: a plugin's or an imported skill's are someone
        else's text, so following one makes the turn untrusted (its risky calls need a yes)."""
        return self.source != "plugin" and not self.origin


class StepRecord(TypedDict):
    """One operation of the trace, as the HUD receives it (history, live turn) and the database stores it.

    Keys:
        id: the operation id.
        turn: the turn it belongs to; "" for an operation of another app or the HUD outside a turn.
        kind: "tool", "memory", "skill" or "recall".
        name: the tool's model-visible name (or the skill's name).
        title: its title for people.
        server: the server that ran it ("" when none did).
        args: a preview of the arguments: at most 8 keys, strings at most 160 characters, secrets hidden.
        source: who asked: "brain", "hud", "app" or "user".
        client: the other app's MCP client name (source "app"), "" otherwise.
        at: when it started, in epoch seconds.
        ms: how long it took, in milliseconds.
        ok: whether it succeeded.
        result: what it answered, at most ``hud.tool_result_chars`` characters.
        taints: whether it made its turn untrusted.
        memory: ``{"id", "text", "profile"}`` when it changed memory, else None.
    """

    id: str
    turn: str
    kind: StepKind
    name: str
    title: str
    server: str
    args: dict[str, JSON]
    source: Origin
    client: str
    at: float
    ms: int
    ok: bool
    result: str
    taints: bool
    memory: dict[str, JSON] | None


@dataclass(frozen=True, slots=True)
class TurnResult:
    """How a turn ended.

    Attributes:
        turn: the turn id.
        interrupted: whether it was cut short (STOP, a new request, barge-in).
        text: the reply (an apology when it failed; what was said before a cut).
    """

    turn: str
    interrupted: bool
    text: str = ""


@dataclass(frozen=True, slots=True)
class Query:
    """A user request embedded once per turn and shared by tool routing and memory recall.

    Attributes:
        text: the request as heard or typed.
        vec: its unit-length embedding, None when no embedder is loaded.
    """

    text: str
    vec: "NDArray[np.float32] | None"


def format_transcript(messages: Iterable[Message], *, user: str = "User", me: str = "You", tools: bool = True,
                      times: bool = False, omit_turns: Collection[str] = ()) -> str:
    """Render chat messages as a plain transcript, one entry per line, oldest first.

    User and assistant messages read ``User: ...`` and ``You: ...``, using their ``raw`` text when present, so the
    per-turn tail a user message carried is not replayed; empty ones are left out, and so are system messages.
    With ``tools``, an assistant's tool calls follow its text as ``(you called NAME {"arg": ...})`` and tool results
    read ``(NAME answered: ...)``.

    Args:
        messages: the messages, oldest first.
        user: the label of the user's lines.
        me: the label of the assistant's lines; tool calls read "you called" when it is "You", else "<me> called".
        tools: include tool calls and tool results.
        times: start each line with the message's local time as ``[HH:MM]`` (a message without ``ts`` gets none).
        omit_turns: turns whose answers drew on web or app content: the first assistant message of such a turn reads
            ``(answer drawn from web or app content omitted)``, and the turn's other assistant messages, tool calls
            and tool results are left out, so no external text reaches the reader.

    Returns:
        The transcript, or "" when nothing is left to show.
    """
    import json

    omit = omit_turns if isinstance(omit_turns, (set, frozenset)) else frozenset(omit_turns)
    caller = "you" if me == "You" else me
    lines: list[str] = []
    hidden: set[str] = set()
    for message in messages:
        role = message["role"]
        stamp = _stamp(message) if times else ""
        turn = message.get("turn", "")
        if role == "user":
            text = _words(message)
            if text:
                lines.append(f"{stamp}{user}: {text}")
        elif role == "assistant":
            if turn and turn in omit:
                if turn not in hidden:
                    hidden.add(turn)
                    lines.append(f"{stamp}{me}: {_OMITTED}")
                continue
            text = _words(message)
            if text:
                lines.append(f"{stamp}{me}: {text}")
            if tools:
                for call in message.get("tool_calls") or ():
                    args = json.dumps(call.get("arguments", {}), ensure_ascii=False, default=str)
                    lines.append(f"{stamp}({caller} called {call.get('name') or 'tool'} {args})")
        elif role == "tool" and tools and not (turn and turn in omit):
            lines.append(f"{stamp}({message.get('name') or 'tool'} answered: {_words(message)})")
    return "\n".join(lines)


def _words(message: Message) -> str:
    """Return a message's text for a transcript: its ``raw`` text when present, else its content, stripped."""
    raw = message.get("raw")
    text = raw if raw is not None else message.get("content")
    return (text or "").strip()


def _stamp(message: Message) -> str:
    """Return the ``[HH:MM] `` local-time prefix of a message, or "" when it has no ``ts``."""
    ts = message.get("ts")
    return "" if ts is None else time.strftime("[%H:%M] ", time.localtime(ts))


@dataclass(slots=True)
class _LogPrivacy:
    """Whether log lines may hold what was said or remembered: the live ``memory.log_conversations``."""

    enabled: Callable[[], bool] = lambda: False


_LOG_PRIVACY: Final = _LogPrivacy()


def log_conversations(enabled: Callable[[], bool]) -> None:
    """Tell ``logged`` where to read the live setting (the runtime passes ``lambda: cfg.memory.log_conversations``).

    Args:
        enabled: returns whether words may appear in the logs, read at every log line.
    """
    _LOG_PRIVACY.enabled = enabled


def conversations_logged() -> bool:
    """Whether log lines may hold what was said or remembered (the live ``memory.log_conversations``).

    Returns:
        True while the setting is on.
    """
    return _LOG_PRIVACY.enabled()


def logged(text: str) -> str:
    """Something said or remembered, as a log line shows it: quoted while ``memory.log_conversations`` is on (to
    troubleshoot), otherwise only how many words it had, so the logs never keep a conversation.

    Args:
        text: a transcript, a reply or a memory.

    Returns:
        ``repr(text)``, or for example ``<6 words>``.
    """
    if conversations_logged():
        return repr(text)
    words = len(text.split())
    return f"<{words} word{'' if words == 1 else 's'}>"


class EventBus:
    """The one fan-out of HUD events: publish from any thread, sinks run on the event loop's thread.

    Every event is a new dict with "type" and "ts" (epoch seconds) followed by the published fields. The same dict
    goes to every sink, so sinks must not change it. A sink that raises is logged and the other sinks still run.
    Until a loop is bound, and in the loop's own thread, sinks run at once in the publishing thread; once the loop
    has closed, events published from other threads are dropped (Orion is shutting down).
    """

    __slots__ = ("_lock", "_loop", "_loop_thread", "_sinks")

    def __init__(self) -> None:
        """Start with no loop bound and no sinks."""
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: int | None = None
        self._sinks: tuple[tuple[object, Callable[[Event], None]], ...] = ()
        self._lock = threading.Lock()

    def bind(self, loop: "asyncio.AbstractEventLoop") -> None:
        """Deliver events on this loop from now on.

        Args:
            loop: the event loop the sinks live on. Call this from the thread that runs it (inside a coroutine, or
                just before running the loop in this thread).
        """
        self._loop = loop
        self._loop_thread = threading.get_ident()

    def publish(self, type: str, /, **data: JSON) -> None:
        """Send one event to every sink; safe to call from any thread, and never raises.

        Args:
            type: the event type, e.g. "state" or "tool".
            **data: the event's fields, JSON values only; a "ts" field replaces the publishing time.
        """
        event: Event = {"type": type, "ts": time.time(), **data}
        if "type" in data:
            event["type"] = type
        loop = self._loop
        if loop is None or threading.get_ident() == self._loop_thread:
            self._fan_out(event)
            return
        try:
            loop.call_soon_threadsafe(self._fan_out, event)
        except RuntimeError:  # the loop has closed: nobody is listening any more
            return

    def subscribe(self, sink: Callable[[Event], None]) -> Callable[[], None]:
        """Add a sink for every event published from now on.

        Args:
            sink: called with each event on the loop's thread; it must return quickly and not change the event.

        Returns:
            A function that removes this sink again; calling it more than once is harmless.
        """
        token = object()
        with self._lock:
            self._sinks = (*self._sinks, (token, sink))

        def unsubscribe() -> None:
            """Remove the sink this subscription added; later events no longer reach it."""
            with self._lock:
                self._sinks = tuple(entry for entry in self._sinks if entry[0] is not token)

        return unsubscribe

    def _fan_out(self, event: Event) -> None:
        """Hand one event to every sink subscribed now; a sink that raises is logged and skipped."""
        for _, sink in self._sinks:
            try:
                sink(event)
            except Exception:
                import logging

                logging.getLogger("orion.base").exception("an event sink failed on a %r event", event.get("type"))


class Expiring[T]:
    """An async value fetched at most once per TTL; callers that arrive during a fetch share it (single flight).

    A fetch that fails caches nothing, so the next call tries again. ``invalidate`` forgets the value, and a fetch
    still running when it is called is not cached. Use one instance from one event loop at a time; a fetch left
    running on a loop that has gone is abandoned.
    """

    __slots__ = ("_cached", "_generation", "_task", "_ttl_s")

    def __init__(self, ttl_s: float) -> None:
        """Start empty.

        Args:
            ttl_s: how long a fetched value stays fresh, in seconds (0 or less: only callers sharing one fetch
                share its value).
        """
        self._ttl_s = ttl_s
        self._cached: tuple[T, float] | None = None
        self._task: asyncio.Future[T] | None = None
        self._generation = 0

    async def get(self, fetch: Callable[[], Awaitable[T]], *, refresh: bool = False) -> T:
        """Return the value: cached while fresh, else from one fetch shared by everyone asking meanwhile.

        Args:
            fetch: makes the value when it is needed; called at most once at a time.
            refresh: skip the cached value; a fetch already running is joined, since its answer is fresh anyway.

        Returns:
            The cached or newly fetched value.

        Raises:
            Exception: whatever the shared fetch raised (nothing is cached then). A cancelled caller does not cancel
                the fetch the others wait for.
        """
        import asyncio

        cached = self._cached
        if not refresh and cached is not None and time.monotonic() - cached[1] < self._ttl_s:
            return cached[0]
        task = self._task
        if task is None or task.get_loop() is not asyncio.get_running_loop():
            task = asyncio.ensure_future(fetch())
            self._task = task
            generation = self._generation
            task.add_done_callback(lambda done: self._settle(done, generation))
        return await asyncio.shield(task)

    def invalidate(self) -> None:
        """Forget the cached value; the next ``get`` fetches again, and a fetch running now is not cached."""
        self._cached = None
        self._task = None
        self._generation += 1

    def _settle(self, task: "asyncio.Future[T]", generation: int) -> None:
        """Cache a finished fetch unless it failed or was invalidated, and free the slot for the next fetch.

        Args:
            task: the finished fetch.
            generation: the invalidation count when the fetch started.
        """
        if self._task is task:
            self._task = None
        if task.cancelled():
            return
        error = task.exception()  # retrieved here, so a failure nobody awaited is not reported as never retrieved
        if error is None and generation == self._generation:
            self._cached = (task.result(), time.monotonic())


class TTLCache[K, V]:
    """A small thread-safe LRU mapping whose entries also expire, each the time it was stored for."""

    __slots__ = ("_clock", "_data", "_lock", "_maxsize")

    def __init__(self, maxsize: int, clock: Callable[[], float] = time.monotonic) -> None:
        """Start empty.

        Args:
            maxsize: the most entries kept; the least recently used goes first (0 or less keeps nothing).
            clock: the time source in seconds (monotonic by default; tests pass a fake one). ``put`` reads it once,
                and ``get`` once when the key is present.
        """
        self._data: OrderedDict[K, tuple[float, V]] = OrderedDict()
        self._lock = threading.Lock()
        self._maxsize = max(0, maxsize)
        self._clock = clock

    def get(self, key: K) -> V | None:
        """Return the value stored for ``key`` while it is fresh, else None (an expired entry is dropped).

        Args:
            key: the key to look up.

        Returns:
            The fresh value, marked as recently used, or None.
        """
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            if entry[0] <= self._clock():
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return entry[1]

    def put(self, key: K, value: V, ttl_s: float) -> None:
        """Store ``value`` for ``key`` for ``ttl_s`` seconds, evicting the least recently used entries over maxsize.

        Args:
            key: the key.
            value: the value to keep.
            ttl_s: how long it stays valid (the setting as it is now, so a changed one applies at once).
        """
        with self._lock:
            self._data[key] = (self._clock() + ttl_s, value)
            self._data.move_to_end(key)
            while len(self._data) > self._maxsize:
                self._data.popitem(last=False)


TRUNCATED: Final = "\n…(truncated)"  # after the part of a long tool result that a model reads
CORE_SERVER: Final = "orion"  # the in-process MCP server of his own controls, under mcp_servers
# A Markdown file's YAML front matter (a skill's SKILL.md, a note): its text between the two --- lines.
FRONT_MATTER: Final = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.DOTALL)
PLUGIN_NAME: Final = r"[a-z0-9][a-z0-9-]{0,63}"  # a plugin's name (it also starts its skills' names: "plugin:skill")
MARKET_NAME: Final = r"[a-z0-9][a-z0-9._-]{0,63}"  # a marketplace's name
SKILL_REF: Final = r"[a-z0-9]+(?:-[a-z0-9]+)*(?::[a-z0-9]+(?:-[a-z0-9]+)*)?"  # "skill" or "plugin:skill"
GITHUB_REPO: Final = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?")
SHORT_REPO: Final = re.compile(r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")
HTTPS_GIT: Final = re.compile(r"https://[^\s/@]+(?::\d+)?/[^\s@]+")  # an HTTPS git URL with no credentials in it


def repo_url(text: str) -> str:
    """A git repository as the settings keep it: "owner/repo" means GitHub's; any other must be an HTTPS git URL.

    Args:
        text: what was given ("anthropics/skills", "https://github.com/anthropics/skills.git", "https://gitlab.com/x/y").

    Returns:
        Its HTTPS URL (a GitHub one without ".git" or a trailing slash).

    Raises:
        ValueError: it is neither.
    """
    text = text.strip()
    if (short := SHORT_REPO.fullmatch(text)) is not None:
        return f"https://github.com/{short.group(1)}/{short.group(2)}"
    if (hub := GITHUB_REPO.fullmatch(text)) is not None:
        return f"https://github.com/{hub.group(1)}/{hub.group(2)}"
    if HTTPS_GIT.fullmatch(text) and ".." not in text:
        return text.rstrip("/")
    raise ValueError(f"{text!r} isn't a repository: give owner/repo for GitHub, or an https:// git URL.")


class NeedsUser(OrionError):
    """A connector can't connect until the user does something (signs in, fills in a field): trying again changes
    nothing, so the hub waits for a changed setting, a sign-in or a restart instead."""


def server_on(settings: Mapping[str, object]) -> bool:
    """Whether an MCP server's settings leave it switched on (on unless ``enabled: false``)."""
    return settings.get("enabled", True) is not False


def switched_off(settings: Mapping[str, object] | None) -> frozenset[str]:
    """The tools an MCP server's settings switch off (``disabled_tools``), by their name on the server."""
    tools = (settings or {}).get("disabled_tools")
    return frozenset(str(tool) for tool in tools) if isinstance(tools, list) else frozenset()


def number(value: object) -> float | None:
    """A finite number read from JSON or YAML, None for anything else (a bool is not a number here)."""
    if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def lower_priority(nice: int) -> None:
    """Make the calling thread yield the processor to everything else (Linux gives each thread its own niceness), so
    background work such as a backup or indexing documents never slows the voice down. Only a thread of the work's
    own: an unprivileged thread cannot be given its priority back, so a shared pool's thread would stay low for every
    job it ran afterwards. A system that refuses (or has no such call) leaves the thread as it is: the work is then
    merely slower for others, never wrong.

    Args:
        nice: 1 to 19; the higher, the more it yields.
    """
    with contextlib.suppress(OSError, AttributeError):
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), nice)


def file_stamp(st: os.stat_result) -> tuple[int, int, int]:
    """(mtime_ns, size, inode): changes whenever a file is written or replaced."""
    return st.st_mtime_ns, st.st_size, st.st_ino


def clip(text: str, limit: int, *, flat: bool = True) -> str:
    """Text cut to at most ``limit`` characters, "…" included, when it is longer: after its last whole word that fits,
    or mid-word when one word alone is longer.

    Args:
        text: the text.
        limit: the most characters.
        flat: put it on one line first (any run of whitespace becomes one space).

    Returns:
        The text, whole or cut.
    """
    if flat:
        text = " ".join(text.split())
    if len(text) <= limit:
        return text
    head = text[:max(0, limit - 1)]
    if not text[len(head)].isspace() and any(c.isspace() for c in head):  # the cut fell inside a word: drop it
        head = head.rsplit(None, 1)[0]
    return head.rstrip(" ,;:\n\t") + "…"


def write_atomic(path: Path, text: str, *, private: bool = False) -> None:
    """Replace a whole file in one step, so no reader (this process or another) ever sees half of it.

    The text goes to a temporary file beside it (named for this process and thread, created exclusively), is flushed
    to disk, then replaces the file.

    Args:
        path: the file.
        text: its new text.
        private: readable by its owner alone (mode 0600: the settings, which may hold keys); otherwise the umask's.

    Raises:
        OSError: it could not be written (the old file is left as it was).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.unlink(missing_ok=True)  # left by a process that died while writing
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600 if private else 0o666)
    try:
        with open(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
