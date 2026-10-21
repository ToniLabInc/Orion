"""The MCP layer: every tool server Orion uses, the toolbox the brain sees, the one way any tool is called, the
operation trace, and lending tools to other AI apps.

Links. One supervised connection per kind of server:
- "orion": the in-process core server (his own controls), reached directly, without a pipe;
- "tools": the private tools process (``orion mcp-serve --hub``), one stdio child serving every built-in group;
- one per external server: a stdio subprocess, Streamable HTTP or SSE.
A server starts with an environment of its own (``safety.env_allow``, plus its declared env and, as its own, only its
secrets: ``connector.<id>.*``), never Orion's; one flagged ``sandbox: true`` (a plugin's, a custom one) runs in the
sandbox with its own folder as home (``orion.sandbox``). A link that drops keeps its tools advertised for ``RECONNECT_GRACE_S`` while it reconnects, so the toolbox (and the
prompt prefix built on it) doesn't flap. Reconnects back off from ``BACKOFF_MIN_S`` to ``mcp.backoff_max_s`` and
start over once a link has stayed up ``STABLE_AFTER_S``. Idle links are checked by listing their tools past the
cache: remote ones every ``mcp.heartbeat_s``, stdio ones four times less often, the in-process one never.

Calls. ``call`` and ``call_named`` never raise: errors come back as "Error: ..." text the model can act on. Every call
is one operation of the trace (a start and an end event, then a StepRecord) and one entry of the activity log. Before a
call runs, the hub applies what no model can talk it out of: the channel's limits (``safety.channels``: a tool a channel
may never use is refused, whoever says yes), the secrets in its arguments (a call carrying one is blocked and recorded),
and the user's yes where it is needed (after web content, for a channel that always asks, every time for a command
outside the sandbox or a change of servers). What comes back, a result or an error, is redacted before anyone reads it.
Calls to Orion's own links carry their
provenance in the request ``_meta`` (plus the query's vector for a tool that declares ``embed_arg``), and bring back
the HUD events their tool published, which the hub stamps with the operation and the turn. Each call has a time limit, and one that lost its
connection is tried again once when the tool is read-only or idempotent. A successful call to a tool that taints its
turn marks the turn, so the turn's later calls carry "tainted".

Lending. ``Gateway`` serves the switched-on built-in tools at /mcp to other apps, on this PC or a paired device
(stateful Streamable HTTP, so each call knows which app made it), and those of any server whose settings say
``lend: true`` (the hands tracker's, say); their calls are "app" operations.

Connecting. The connectors layer (``orion.connectors``, attached as ``hub.hooks``) prepares a server's settings as it
connects (the secrets and a plugin's fields filled in; nothing of that is kept) and gives a remote server its OAuth
provider (``orion.oauth``). A server that can't connect until the user does something (sign in, fill in a field)
raises ``NeedsUser``: it is not retried on a timer, it waits for a changed setting, a sign-in or a restart.

Intents. A server whose settings say ``intents: true`` and that lists ``orion://intents`` is followed while it is
connected (``orion.intents``): Orion subscribes to that resource (``resources/subscribe`` on a 2025-era connection, a
``subscriptions/listen`` stream on a 2026-07-28 one), and each ``notifications/resources/updated`` for it, which the
connection's message handler receives either way, has the resource read again and the intents due run in order (above
the last id handled from that server, kept across reconnects, and fresh by their id). A server with no tools at all
(one that only pushes intents) connects too, with an empty toolbox.

Watching. ``watch`` follows any resource of a server while it is connected, across reconnects (the live view of the
hands, while a HUD shows it): subscribed the same way, read once at first and again after each update, the latest
only, never a backlog, and (``every``) again after a quiet spell too: reading is what keeps a hands remote's live
view on, so it never lapses while nothing new comes (the camera paused, the Pi away). ``read_resource`` reads one
once. Every list and read reaches the server: the clients keep no response cache.

The MCP SDK and numpy load on first use; the pure helpers below need neither.
"""

import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import shutil
import sys
import time
import warnings
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol, TextIO, TypedDict

from orion import images, redact, sandbox, secrets
from orion.activity import SOURCES
from orion.base import (
    CONTEXT_META,
    CORE_SERVER,
    EVENTS_META,
    JSON,
    PROVENANCE_META,
    QVEC_META,
    CallContext,
    GroupInfo,
    Image,
    NeedsUser,
    Origin,
    Provenance,
    Publish,
    StepRecord,
    Tool,
    ToolOutput,
    ToolRequest,
    clip,
    new_id,
    number,
    server_on,
    switched_off,
)
from orion.host import profile
from orion.intents import INTENTS_URI, due, parse_feed
from orion.service import local_path
from orion.tools.policy import (
    HIDDEN,
    UNATTENDED,
    channel_of,
    channel_verdict,
    confirm_reason,
    masked_server,
    taints,
    widening,
)

if TYPE_CHECKING:
    import mcp_types as mt
    from mcp import StdioServerParameters
    from starlette.types import Receive, Scope, Send

    from orion.activity import Activity, Kind
    from orion.config import Config, HudConfig, McpConfig, SystemConfig
    from orion.confirm import Confirmer
    from orion.intents import Intent

log = logging.getLogger("orion.mcp")

type LinkStatus = Literal["connecting", "connected", "reconnecting", "error", "disabled"]
type LinkKind = Literal["core", "tools", "external"]

TOOLS: Final = "tools"  # the link to the tools process
HUD_EVENTS: Final[frozenset[str]] = frozenset(  # what Orion's own tools may tell the HUD; other servers can't
    {"reminders", "todos", "memory_update", "skill_used", "skill_changed", "weather", "news"})
TAINT_EVENT: Final = "taint"  # a tool of Orion's says its answer is untrusted text (a plugin's skill): it taints its turn
RECONNECT_GRACE_S: Final = 10  # a dropped link's tools stay offered this long while it reconnects
STDIO_HEARTBEAT_FACTOR: Final = 4  # a local (stdio) server is checked this many times less often than a remote one
HEARTBEAT_TIMEOUT_S: Final = 10  # an idle server that doesn't list its tools within this is reconnected
BACKOFF_MIN_S: Final = 1  # the second reconnect attempt waits this long (the first is immediate), doubling after
STABLE_AFTER_S: Final = 60  # a link up this long starts its backoff over when it drops
OWN_TIMEOUT_S: Final = 60  # time limit of a call to one of Orion's own tools
OWN_READ_ONLY_TIMEOUT_S: Final = 20  # ...of one of his read-only tools
COMMAND_GRACE_S: Final = 5  # run_command's call limit: the command's own limit plus this
ARGS_MAX_KEYS: Final = 8  # arguments shown in the trace
ARGS_MAX_CHARS: Final = 160  # the longest argument value shown in the trace
ERROR_CHARS: Final = 160  # the longest error line shown in the trace
TAINT_TURNS: Final = 64  # turns whose taint (and steps) are remembered
LOG_MAX_BYTES: Final = 1_048_576  # an external server's log is cut back when it grows past this...
LOG_KEEP_BYTES: Final = 524_288  # ...to its last this many bytes
DESCRIBE_CHARS: Final = 300  # the longest error description
NAME_MAX: Final = 64  # the longest tool name a model accepts
INSTRUCTIONS: Final = ("ORION, the voice assistant on this Linux PC: his tools for the PC, the web, the user's "
                       "memory, reminders and to-dos, and his own controls.")

_SCHEMA_MAPS: Final = frozenset({"properties", "patternProperties", "$defs", "definitions"})  # names -> schemas
_SCHEMA_PARTS: Final = frozenset({"items", "prefixItems", "anyOf", "oneOf", "allOf", "not", "if", "then", "else",
                                  "contains", "additionalProperties"})  # schemas within schemas
_PLACES: Final = {"voice": "by voice", "hud": "from the HUD", "discord": "on Discord", "job": "in a scheduled job",
                  "app": "for another app"}  # where a channel's limit applies, in words
_WIDENS: Final = {"outside_sandbox": "Running a command outside the sandbox"}
_ANSWERED: Final = {"the HUD": "you, in the HUD", "voice": "you, by voice", "discord": "you, on Discord",
                    "job": "you, on Discord"}  # who answered a question, as the activity log says it
_SENTENCE_END: Final = re.compile(r"[.!?](?=\s+[A-Z])")
_ABBREVIATIONS: Final = frozenset({"e.g", "i.e", "etc", "vs", "cf", "approx", "incl", "mr", "mrs", "ms", "dr", "st"})


class ToolStatus(TypedDict):
    """One tool as the Abilities page lists it."""

    name: str
    remote: str
    title: str
    description: str
    enabled: bool
    hints: dict[str, bool | None]
    unavailable: str  # what this machine lacks for it ("" when it can run here)


class ServerStatus(TypedDict):
    """One server as the Abilities page lists it (an external server's tokens hidden).

    Keys beyond the obvious: ``sign_in`` (its link failed because it needs the user to sign in) and ``calls`` (the
    tool calls made to it since Orion started).
    """

    name: str
    title: str
    description: str
    status: LinkStatus
    enabled: bool
    builtin: bool
    error: str
    config: dict[str, JSON]
    tools: list[ToolStatus]
    sign_in: bool
    calls: int


class Hooks(Protocol):
    """What the connectors layer (``orion.connectors``) gives the hub for the external servers it connects."""

    async def prepare(self, name: str, settings: Mapping[str, Any], expand: Callable[[Any], Any]) -> dict[str, Any]:
        """A server's settings as it connects with them: secrets and a plugin's fields filled in. Tokens are put in
        only here and never kept.

        Args:
            name: the server.
            settings: its ``mcp_servers`` entry.
            expand: the hub's ``${VAR}`` expansion from the environment, for the servers that may read it.

        Raises:
            NeedsUser: something the user must do first (a field isn't set, a sign-in is needed).
        """

    def auth(self, name: str, settings: Mapping[str, Any], on_need: Callable[[], None]) -> Any:
        """The ``httpx`` auth of a remote server (an ``OAuthClientProvider``), None for a server that has none.

        Args:
            name: the server.
            settings: what ``prepare`` returned.
            on_need: to call just before raising ``NeedsUser`` because a person must sign in.
        """


# ---------------------------------------------------------------------------------------------------- pure helpers
def untitled(schema: JSON) -> JSON:
    """A JSON schema without the ``title`` pydantic gives every property (the name again, in words).

    The model reads the schemas with every message, and those titles add about an eighth. A property that is itself
    called "title" stays.

    Args:
        schema: a JSON schema.

    Returns:
        A copy without ``title`` keys.
    """
    if isinstance(schema, list):
        return [untitled(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    out: dict[str, JSON] = {}
    for key, value in schema.items():
        if key in _SCHEMA_MAPS and isinstance(value, dict):
            out[key] = {name: untitled(sub) for name, sub in value.items()}
        elif key in _SCHEMA_PARTS:
            out[key] = untitled(value)
        elif key != "title":
            out[key] = value
    return out


def _safe(name: str) -> str:
    """A name as the start of a tool name: lower case, runs of other characters made one underscore."""
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", name).strip("_").lower()


def shown_name(server: str, remote: str, *, builtin: bool) -> str:
    """The name the model sees for a tool: his own keep theirs; an external one gets its server's name in front.

    Args:
        server: the server's name ("Home Assistant!").
        remote: the tool's name on that server ("turn_on").
        builtin: whether it is one of Orion's own tools.

    Returns:
        "turn_on" or "home_assistant__turn_on", at most 64 characters.
    """
    return remote if builtin else f"{_safe(server)}__{_safe(remote)}"[:NAME_MAX]


def wrap_schema(schema: Mapping[str, JSON] | None) -> dict[str, JSON]:
    """A tool's argument schema as the models need it: an object (with ``properties``), titles removed.

    Args:
        schema: what the server declared (possibly nothing, or not an object).

    Returns:
        The schema; ``{"type": "object", "properties": {}}`` for anything that isn't an object schema.
    """
    clean = untitled(dict(schema or {}))
    if not isinstance(clean, dict) or clean.get("type") != "object":
        return {"type": "object", "properties": {}}
    clean.setdefault("properties", {})
    return clean


def summary_of(about: str) -> str:
    """A tool description's first sentence, for people ("Opens apps, e.g. Firefox. More." -> its first sentence).

    Args:
        about: the description, on one line.

    Returns:
        Up to the first full stop, question or exclamation mark followed by a capital, skipping abbreviations
        such as "e.g." and initials; the whole text when there is none.
    """
    for match in _SENTENCE_END.finditer(about):
        words = about[:match.start()].split()
        last = words[-1].lower().rstrip(".") if words else ""
        if last in _ABBREVIATIONS or (len(last) == 1 and last.isalpha()):
            continue
        return about[:match.end()]
    return about


def result_text(result: Any) -> str:
    """What a tool's result says, as one text for the model.

    Args:
        result: an MCP CallToolResult.

    Returns:
        Its texts joined by newlines (an image or a resource named by a placeholder; structured-only content as
        JSON; "(no output)" when it holds nothing), with "Error: " in front when the tool failed.
    """
    parts: list[str] = []
    for content in getattr(result, "content", None) or ():
        kind = getattr(content, "type", "")
        if kind == "text":
            parts.append(str(content.text))
        elif kind == "image":
            parts.append("[the tool returned a picture]")
        elif kind == "audio":
            parts.append("[the tool returned audio]")
        elif kind == "resource":
            resource = content.resource
            parts.append(getattr(resource, "text", None) or f"[resource {getattr(resource, 'uri', '')}]")
        elif kind == "resource_link":
            parts.append(f"[link: {content.uri}]")
    structured = getattr(result, "structured_content", None)
    if not parts and structured:
        parts.append(json.dumps(structured, ensure_ascii=False, default=str))
    text = "\n".join(parts).strip() or "(no output)"
    return f"Error: {text}" if getattr(result, "is_error", False) else text


def result_images(result: Any, *, label: str, max_side: int, quality: int) -> tuple[Image, ...]:
    """The pictures a tool's result holds, prepared for the models (one that can't be decoded is left out).

    Args:
        result: an MCP CallToolResult.
        label: what they show ("Take a screenshot").
        max_side: ``eyes.image_max_side``.
        quality: ``eyes.image_quality``.

    Returns:
        The pictures, in order.
    """
    found: list[Image] = []
    for content in getattr(result, "content", None) or ():
        if getattr(content, "type", "") != "image":
            continue
        try:
            found.append(images.from_base64(str(content.data), label=label, max_side=max_side, quality=quality))
        except ValueError:
            log.debug("a picture from %s couldn't be read", label)
    return tuple(found)


def describe_error(error: BaseException) -> str:
    """An exception in words, for the model and the HUD.

    Args:
        error: what went wrong (anyio's exception groups are unwrapped to their causes).

    Returns:
        At most 300 characters: the causes joined by "; ", "command not found (x)" for a missing program, else
        the message (or the exception's type when it has none).
    """
    if isinstance(error, BaseExceptionGroup):
        text = "; ".join(dict.fromkeys(describe_error(inner) for inner in error.exceptions))
    elif isinstance(error, FileNotFoundError):
        text = f"command not found ({error.filename or error})"
    else:
        text = str(error) or type(error).__name__
    return redact.redacted(text)[:DESCRIBE_CHARS]  # (an address with a key in it is a common thing to fail on)


def preview_args(args: Mapping[str, JSON]) -> dict[str, JSON]:
    """A call's arguments as the trace shows them.

    Args:
        args: the arguments.

    Returns:
        At most 8 of them; strings, and other nested values as JSON, cut to 160 characters; values of keys that look
        secret ("api_key", "token", "password", ...) shown as "(hidden)", and any secret the process knows cut out.
    """
    out: dict[str, JSON] = {}
    for key, value in list(args.items())[:ARGS_MAX_KEYS]:
        if secrets.looks_secret(key):
            out[key] = HIDDEN
        elif isinstance(value, str):
            out[key] = clip(redact.redacted(value), ARGS_MAX_CHARS, flat=False)
        elif value is None or isinstance(value, bool | int | float):
            out[key] = value
        else:
            out[key] = clip(redact.redacted(json.dumps(value, ensure_ascii=False, default=str)), ARGS_MAX_CHARS,
                            flat=False)
    return out


def timeout_for(tool: Tool, args: Mapping[str, JSON], *, system: "SystemConfig", mcp: "McpConfig",
                server_timeout_s: float | None) -> float:
    """How long one call may take.

    Args:
        tool: the tool.
        args: the call's arguments (run_command may ask for a longer limit).
        system: the system settings (run_command's default and longest limits).
        mcp: the MCP settings (the default limit of external servers).
        server_timeout_s: the external server's own ``timeout`` setting, None when it has none.

    Returns:
        Seconds: run_command's limit (its ``timeout`` argument or ``system.command_timeout_s``, at most
        ``system.command_max_timeout_s``) plus 5; 20 for Orion's read-only tools, 60 for his others; an external
        server's own limit, else ``mcp.default_timeout_s``.
    """
    if tool.builtin and tool.remote == "run_command":
        limit = _seconds(args.get("timeout")) or float(system.command_timeout_s)
        return min(limit, float(system.command_max_timeout_s)) + COMMAND_GRACE_S
    if tool.builtin:
        return float(OWN_READ_ONLY_TIMEOUT_S if tool.read_only else OWN_TIMEOUT_S)
    return float(server_timeout_s or mcp.default_timeout_s)


def provenance(origin: Origin, *, tainted: bool) -> Provenance:
    """The provenance a call carries to Orion's tools.

    Args:
        origin: who asked ("brain", "hud", "app" or "user").
        tainted: whether the turn already used web or app content.

    Returns:
        "tainted" for the brain in a tainted turn; "hud" for the user's own command (as trusted as the HUD);
        else the origin.
    """
    if origin == "user":
        return "hud"
    return "tainted" if origin == "brain" and tainted else origin


def definition(tool: Tool) -> "mt.Tool":
    """A tool as MCP lists it to another app.

    Args:
        tool: the hub's tool.

    Returns:
        The MCP tool definition, with its annotations.
    """
    import mcp_types as mt

    hints = tool.hints
    return mt.Tool(name=tool.name, title=tool.title or None, description=tool.description,
                   input_schema=tool.parameters,
                   annotations=mt.ToolAnnotations(read_only_hint=hints.get("read_only"),
                                                  destructive_hint=hints.get("destructive"),
                                                  open_world_hint=hints.get("open_world"),
                                                  idempotent_hint=hints.get("idempotent")))


def encode_qvec(vector: Any) -> str:
    """A query vector as it travels in ``_meta``: base64 of its float16 values (1 KB for 512 dimensions).

    Args:
        vector: a 1-D array of numbers.

    Returns:
        The encoded text.
    """
    import numpy as np

    return base64.b64encode(np.asarray(vector, dtype=np.float16).tobytes()).decode("ascii")


def _hints(tool: Any) -> dict[str, bool | None]:
    """An MCP tool's behaviour hints as plain keys (read_only, destructive, open_world, idempotent), said ones only."""
    notes = getattr(tool, "annotations", None)
    if notes is None:
        return {}
    pairs = {"read_only": notes.read_only_hint, "destructive": notes.destructive_hint,
             "open_world": notes.open_world_hint, "idempotent": notes.idempotent_hint}
    return {key: value for key, value in pairs.items() if value is not None}


def _orion_meta(tool: Any) -> dict[str, Any]:
    """What Orion's own tools say about themselves in their ``_meta["orion"]`` (group, examples, taints)."""
    meta = getattr(tool, "meta", None) or {}
    ours = meta.get("orion") if isinstance(meta, dict) else None
    return ours if isinstance(ours, dict) else {}


def _seconds(value: Any) -> float | None:
    """A positive number of seconds from a setting or an argument, None for anything else."""
    seconds = number(value)
    return seconds if seconds is not None and seconds > 0 else None


def _expand(value: Any) -> Any:
    """``${VAR}`` filled in from the environment, throughout a server's settings (only when connecting; the secrets are
    the connectors layer's: ``Connectors.prepare``)."""
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand(item) for item in value]
    return value


def _process_part(settings: Mapping[str, Any] | None) -> dict[str, Any]:
    """What a server's connection depends on: its settings without the tools switched off, what its gestures do and
    whether it is lent (none of them needs a reconnect)."""
    return {key: value for key, value in (settings or {}).items() if key not in ("disabled_tools", "gestures", "lend")}


@dataclass(frozen=True, eq=False, slots=True)
class _Watch:
    """A resource being watched: who hears its text, and how often it is read again without an update."""

    on_text: Callable[[str], None]
    every: float | None = None




# ---------------------------------------------------------------------------------------------------- the trace
@dataclass(slots=True)
class Op:
    """An operation in progress.

    Attributes:
        id: its id (8 hex characters), on both of its events and its StepRecord.
        server: the server that runs it ("orion" for the brain's own steps).
        name: the tool's model-visible name.
        title: its title for people.
        source: who asked: "brain", "hud" or "app".
        client: the other app's name (source "app"), else "".
        turn: the turn it belongs to ("" for none).
        args: the arguments as the trace shows them.
        read_only: the tool's read-only hint (None when unsaid).
        at: when it started, in epoch seconds.
        started: when it started, on the monotonic clock.
        memory: ``{"id", "text", "profile"}`` once the call reported a memory change.
        taints: whether its success made its turn untrusted.
    """

    id: str
    server: str
    name: str
    title: str
    source: Origin
    client: str
    turn: str
    args: dict[str, JSON]
    read_only: bool | None
    at: float
    started: float
    memory: dict[str, JSON] | None = None
    taints: bool = False


class OpTrace:
    """Every operation (tool calls of the brain, the HUD and other apps; the brain's own steps) as ``tool`` events,
    a StepRecord each, the steps of each running turn, and a ring of the most recent ones."""

    def __init__(self, publish: Publish, hud: "HudConfig", *, recent: int,
                 on_record: Callable[[StepRecord], None] | None = None) -> None:
        """Start empty.

        Args:
            publish: the event bus's publish (thread-safe).
            hud: the live HUD settings (``tool_result_chars`` caps a result's preview).
            recent: how many records the ring keeps (``mcp.recent_calls``).
            on_record: called with every finished record (the runtime stores the brain's and other apps' ones).
        """
        self._publish = publish
        self._hud = hud
        self.on_record = on_record
        self._recent: deque[StepRecord] = deque(maxlen=recent)
        self._turns: OrderedDict[str, list[StepRecord]] = OrderedDict()
        self.count = 0

    def start(self, *, server: str, name: str, title: str, ctx: CallContext, args: Mapping[str, JSON],
              read_only: bool | None) -> Op:
        """Begin an operation and publish its start.

        Args:
            server: the server that runs it.
            name: the tool's model-visible name.
            title: its title for people ("" uses the name).
            ctx: who asks, and the turn.
            args: the arguments.
            read_only: the tool's read-only hint.

        Returns:
            The operation, to hand to ``end``.
        """
        op = Op(new_id(), server, name, title or name, ctx.origin, ctx.client, ctx.turn or "", preview_args(args),
                read_only, time.time(), time.monotonic())
        self._publish("tool", phase="start", id=op.id, turn=op.turn, name=op.name, title=op.title, server=op.server,
                      args=op.args, source=op.source, client=op.client, read_only=op.read_only)
        return op

    def ask(self, op: Op, question: str, text: str) -> None:
        """An operation waits for the user's yes (``orion.confirm``): the trace shows the question.

        Args:
            op: the operation.
            question: the question's id (the HUD answers with it).
            text: the question.
        """
        self._publish("tool", phase="ask", id=op.id, turn=op.turn, name=op.name, question=question, text=text)

    def end(self, op: Op, *, ok: bool, result: str, attempts: int = 1) -> StepRecord:
        """Finish an operation: publish its end, record it and hand the record on.

        A call that changed memory becomes a "memory" step ("Memory saved" for USER.md or MEMORY.md, else "Noted")
        whose arguments are the text saved.

        Args:
            op: the operation.
            ok: whether it succeeded.
            result: what it answered.
            attempts: 2 when it was tried again after its connection dropped.

        Returns:
            The record.
        """
        ms = round((time.monotonic() - op.started) * 1000)
        shown = clip(result, self._hud.tool_result_chars)
        first = result.removeprefix("Error:").strip().splitlines()
        error = "" if ok else clip(first[0] if first else "failed", ERROR_CHARS, flat=False)
        self._publish("tool", phase="end", id=op.id, turn=op.turn, name=op.name, title=op.title, server=op.server,
                      source=op.source, client=op.client, ok=ok, ms=ms, result=shown, error=error, attempts=attempts)
        memory = op.memory
        record = StepRecord(
            id=op.id, turn=op.turn, kind="memory" if memory else "tool", name=op.name,
            title=("Memory saved" if memory.get("profile") else "Noted") if memory else op.title, server=op.server,
            args={"text": memory.get("text", "")} if memory else op.args, source=op.source, client=op.client,
            at=op.at, ms=ms, ok=ok, result=shown, taints=op.taints, memory=memory)
        self.count += 1
        self._recent.append(record)
        if op.turn:
            self._turns.setdefault(op.turn, []).append(record)
            self._turns.move_to_end(op.turn)
            while len(self._turns) > TAINT_TURNS:
                self._turns.popitem(last=False)
        if self.on_record is not None:
            try:
                self.on_record(record)
            except Exception:
                log.warning("an operation record could not be handed on", exc_info=True)
        return record

    def record(self, *, server: str, name: str, title: str, ctx: CallContext, args: Mapping[str, JSON], ok: bool,
               result: str, ms: int = 0) -> StepRecord:
        """An operation that is over as soon as it begins (the brain's ``find_tools``, an unknown tool name).

        Args:
            server: the server it is shown under.
            name: the tool's name.
            title: its title for people.
            ctx: who asked, and the turn.
            args: the arguments.
            ok: whether it succeeded.
            result: what it answered.
            ms: how long it took, in milliseconds.

        Returns:
            The record.
        """
        op = self.start(server=server, name=name, title=title, ctx=ctx, args=args, read_only=True)
        op.started -= ms / 1000
        return self.end(op, ok=ok, result=result)

    def recent(self, limit: int) -> list[StepRecord]:
        """The latest operations, newest first.

        Args:
            limit: how many at most.

        Returns:
            The records.
        """
        return list(reversed(self._recent))[:max(0, limit)]

    def turn_steps(self, turn: str) -> list[StepRecord]:
        """A turn's operations so far, in the order they ended.

        Args:
            turn: the turn.

        Returns:
            Its records (until ``end_turn``).
        """
        return list(self._turns.get(turn, ()))

    def end_turn(self, turn: str) -> None:
        """Forget a finished turn's records.

        Args:
            turn: the turn.
        """
        self._turns.pop(turn, None)


# ---------------------------------------------------------------------------------------------------- the hub
@dataclass(eq=False, slots=True)
class _Link:
    """One supervised MCP connection.

    Attributes:
        name: the link's name ("orion", "tools", or the external server's).
        kind: "core", "tools" or "external".
        settings: an external server's settings (``${VAR}`` expanded only when connecting).
        status: where it stands.
        error: why it failed, in words.
        client: the MCP client while connected.
        listed: the tools it lists.
        up: set while connected.
        settled: set once its first attempt connected or failed.
        lost: set when a call found the connection closed.
        task: its supervising task.
        stopping: being stopped on purpose.
        since: when it last connected, on the monotonic clock.
        calls: calls in flight.
        grace: the timer that withdraws its tools when a reconnect takes too long.
        updated: the resources followed on this link (its intents feed, those watched), each with the event the
            message handler sets when the server says it changed.
        watchers: the tasks following them while connected.
        following: set while a connection's watchers may start (cleared first as the connection ends, so a watch
            asked for meanwhile waits for the next one).
        signin: it failed because the user must sign in to it (it is not retried until they do).
    """

    name: str
    kind: LinkKind
    settings: dict[str, Any] = field(default_factory=dict)
    status: LinkStatus = "connecting"
    error: str = ""
    client: Any = None
    listed: list[Any] = field(default_factory=list)
    up: asyncio.Event = field(default_factory=asyncio.Event)
    settled: asyncio.Event = field(default_factory=asyncio.Event)
    lost: asyncio.Event = field(default_factory=asyncio.Event)
    task: "asyncio.Task[None] | None" = None
    stopping: bool = False
    since: float = 0.0
    calls: int = 0
    grace: asyncio.TimerHandle | None = None
    updated: dict[str, asyncio.Event] = field(default_factory=dict)
    watchers: dict[str, "asyncio.Task[None]"] = field(default_factory=dict)
    following: bool = False
    signin: bool = False


@dataclass(eq=False, slots=True)
class _Server:
    """A server as the model and the Abilities page see it: a built-in group, or an external server.

    Attributes:
        name: its name.
        settings: its ``mcp_servers`` entry ({} for a built-in group without one).
        link: its connection (built-in groups share theirs); None while an external server is switched off.
        group: what a built-in group is called and does; None for an external server.
        tools: the tools it offers, wrapped.
        disabled: its tools switched off, by their name on the server.
        calls: the tool calls made to it since Orion started.
    """

    name: str
    settings: dict[str, Any]
    link: _Link | None
    group: GroupInfo | None = None
    tools: list[Tool] = field(default_factory=list)
    disabled: frozenset[str] = frozenset()
    calls: int = 0

    @property
    def enabled(self) -> bool:
        """Whether the server is switched on."""
        return server_on(self.settings)

    @property
    def status(self) -> LinkStatus:
        """Its link's status, or "disabled" while it is switched off."""
        return self.link.status if self.enabled and self.link is not None else "disabled"

    @property
    def error(self) -> str:
        """Why its link failed, while it is failing."""
        return self.link.error if self.link is not None and self.status in ("error", "reconnecting") else ""


class MCPHub:
    """Connections to tool servers, the toolbox the brain sees, and the single entry point for tool calls."""

    def __init__(self, cfg: "Config", groups: Mapping[str, GroupInfo], *, ops: OpTrace, spawn: Sequence[str] = (),
                 spawn_env: Mapping[str, str] | None = None, core: object | None = None,
                 core_groups: Mapping[str, GroupInfo] | None = None, on_change: Callable[[], None] | None = None,
                 on_event: Callable[[dict[str, JSON]], None] | None = None,
                 embed: Callable[[str], Any] | None = None,
                 on_intent: Callable[[str, "Intent"], Awaitable[None]] | None = None,
                 confirmer: "Confirmer | None" = None, activity: "Activity | None" = None,
                 config_file: Path | None = None) -> None:
        """Prepare the links; nothing connects until ``configure``.

        Args:
            cfg: the live settings (``mcp_servers``, ``mcp.*``, ``system.*``, the logs folder).
            groups: the built-in groups the tools process serves, by name.
            ops: the operation trace.
            spawn: the tools process's command line (``orion mcp-serve --hub``); empty: no tools process.
            spawn_env: environment variables for it (``ORION_CONFIG``).
            core: the in-process MCP server of Orion's own controls.
            core_groups: its groups, by name.
            on_change: called whenever a server's status or the toolbox changes (compare ``version``).
            on_event: receives the HUD events Orion's own tools publish, stamped with ``call`` and ``turn``.
            embed: makes the query vector of a tool's ``embed_arg`` (returns None while no model is loaded).
            on_intent: runs an intent a server pushed (``orion.intents.IntentRunner.run``); None: no server is
                followed.
            confirmer: asks the user before a risky call of the model (``orion.confirm``); None: never asks.
            activity: the log every call, refusal, block and question is recorded in; None: none is kept.
            config_file: where the settings are kept (hidden from a sandboxed server); None: the usual place.
        """
        self.cfg = cfg
        self.ops = ops
        self._spawn = tuple(spawn)
        self._spawn_env = dict(spawn_env or {})
        self._core = core
        self._on_change = on_change or (lambda: None)
        self._on_event = on_event or (lambda _event: None)
        self._embed = embed
        self._on_intent = on_intent
        self.confirmer = confirmer
        self._activity = activity
        self._config_file = config_file
        self._intents_last: dict[str, int] = {}  # the last intent id handled from each server, kept across reconnects
        self._intents_run: dict[str, str] = {}  # ...and the run of that server it came from (its feed's instance)
        self._watches: dict[str, dict[str, _Watch]] = {}  # server -> resource -> who hears its text, and how often
        self._intents_warned: set[str] = set()  # servers whose intents feed can't be followed, said once
        self._links: dict[str, _Link] = {}
        self._servers: dict[str, _Server] = {}
        self._own: dict[str, tuple[GroupInfo, _Link]] = {}
        if groups:
            tools = self._links[TOOLS] = _Link(TOOLS, "tools")
            self._own.update({name: (group, tools) for name, group in groups.items()})
        if core is not None and core_groups:
            here = self._links[CORE_SERVER] = _Link(CORE_SERVER, "core")
            self._own.update({name: (group, here) for name, group in core_groups.items()})
        self._tainted: OrderedDict[str, str] = OrderedDict()  # turn -> what first made it untrusted (a tool title)
        self._advertised: Mapping[str, Tool] = MappingProxyType({})
        self._signature: tuple[object, ...] = ()
        self._version = 0
        self.hooks: Hooks | None = None  # the connectors layer, attached once it exists (see ``Hooks``)

    # ------------------------------------------------------------------------------------------------ the toolbox
    @property
    def version(self) -> int:
        """A number that changes whenever the advertised toolbox does."""
        return self._version

    def tools(self) -> Mapping[str, Tool]:
        """The tools the model may use, by model-visible name: switched on, of servers connected (or reconnecting
        within the grace). The same read-only mapping until ``version`` changes."""
        return self._advertised

    def lent(self) -> list[Tool]:
        """The tools other apps may use through /mcp: Orion's own, and those of a server whose settings say
        ``lend: true``; switched on, of servers connected (or reconnecting within the grace)."""
        lending = {h.name for h in self._servers.values() if h.group is None and h.settings.get("lend") is True}
        return [tool for tool in self.tools().values() if tool.builtin or tool.server in lending]

    def offered(self) -> list[tuple[str, bool, Tool]]:
        """Everything known, switched off or not: (server, connected, tool)."""
        return [(h.name, h.status == "connected", tool) for h in self._servers.values() for tool in h.tools]

    def connected(self) -> int:
        """How many servers are connected now (each of his own groups counts)."""
        return sum(h.status == "connected" for h in self._servers.values())

    def status(self) -> list[ServerStatus]:
        """Every server and its tools, as the Abilities page lists them (tokens hidden)."""
        out: list[ServerStatus] = []
        host = profile()
        for h in self._servers.values():
            masked = masked_server(h.settings) if h.group is None else {}
            config: dict[str, JSON] = masked if isinstance(masked, dict) else {}
            out.append(ServerStatus(
                name=h.name, title=h.group.title if h.group else h.name,
                description=h.group.description if h.group else str(h.settings.get("description", "")),
                status=h.status, enabled=h.enabled, builtin=h.group is not None, error=h.error, config=config,
                tools=[ToolStatus(name=t.name, remote=t.remote, title=t.title, description=t.summary,
                                  enabled=t.remote not in h.disabled, hints=dict(t.hints),
                                  unavailable=host.missing(t.requires)) for t in h.tools],
                sign_in=h.enabled and h.link is not None and h.link.signin, calls=h.calls))
        return out

    # ------------------------------------------------------------------------------------------------ lifecycle
    async def configure(self, *, wait_s: float = 0.0) -> None:
        """Make the servers match ``mcp_servers``: connect new ones, reconnect changed ones, drop removed ones.

        Built-in groups are always listed; switching a group or a tool on or off restarts nothing.

        Args:
            wait_s: at startup, how long to wait for the links being started (``mcp.startup_wait_s``).
        """
        configured = self.cfg.mcp_servers or {}
        wanted: dict[str, dict[str, Any]] = {name: dict(configured.get(name) or {}) for name in self._own}
        wanted |= {name: dict(settings) for name, settings in configured.items()
                   if name not in self._own and isinstance(settings, dict)}
        for name in [n for n, h in self._servers.items() if h.group is None]:
            if _process_part(self._servers[name].settings) != _process_part(wanted.get(name)):
                await self._drop(name)
        for name, settings in wanted.items():
            if (handle := self._servers.get(name)) is not None:
                handle.settings, handle.disabled = settings, switched_off(settings)
            elif name in self._own:
                group, link = self._own[name]
                self._servers[name] = _Server(name, settings, link, group, disabled=switched_off(settings))
            else:
                link = None
                if server_on(settings):
                    link = self._links[name] = _Link(name, "external", settings)
                self._servers[name] = _Server(name, settings, link, disabled=switched_off(settings))
        starting = [link for link in dict.fromkeys(h.link for h in self._servers.values() if h.enabled and h.link)
                    if link.task is None]
        for link in starting:
            self._start(link)
        self._refresh()
        if wait_s > 0 and starting:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(wait_s):
                    await asyncio.gather(*(link.settled.wait() for link in starting))
            if slow := [link.name for link in starting if not link.settled.is_set()]:
                log.warning("MCP servers still starting in the background: %s", ", ".join(slow))

    async def restart(self, name: str) -> None:
        """Connect a server again (for a built-in group, the tools process they share restarts).

        Args:
            name: the server.

        Raises:
            ValueError: there is no such running server, or it is Orion's in-process controls.
        """
        handle = self._servers.get(name)
        if handle is None or handle.link is None:
            raise ValueError(f"There is no running server called {name}.")
        if handle.link.kind == "core":
            raise ValueError("Orion's own controls run inside him: restart Orion instead.")
        await self._stop_link(handle.link)
        self._start(handle.link)
        self._refresh()

    async def prepared(self, name: str, settings: Mapping[str, Any]) -> dict[str, Any]:
        """A server's settings as it connects with them: ``${VAR}`` from the environment and, with the connectors layer
        attached, its secrets and a plugin's fields. Tokens are filled in only here, never kept, so they never reach
        the HUD.

        Args:
            name: the server.
            settings: its ``mcp_servers`` entry.

        Returns:
            The settings to connect with.

        Raises:
            NeedsUser: something the user must do first (a field isn't set).
        """
        if self.hooks is None:
            return await asyncio.to_thread(secrets.expand_all, dict(settings), only=f"connector.{name}.")
        return await self.hooks.prepare(name, settings, _expand)

    async def read_resource(self, server: str, uri: str) -> str:
        """Read one resource of a connected server.

        Args:
            server: the server.
            uri: the resource.

        Returns:
            Its text.

        Raises:
            LookupError: the server isn't connected.
            Exception: the read failed (an MCPError, a timeout).
        """
        link = self._live_link(server)
        if link is None:
            raise LookupError(f"the {server} MCP server isn't connected")
        return await _read(link.client, uri)

    def watch(self, server: str, uri: str, on_text: Callable[[str], None], *, every: float | None = None) -> \
            Callable[[], None]:
        """Follow one resource of a server while it is connected, across reconnects: read it at once and again after
        each update it announces (the latest only: updates during a read make one more read, never a backlog).

        Args:
            server: the server.
            uri: the resource.
            on_text: called (on the loop) with its text after each read.
            every: also read it again after this many seconds without an update (None: only on updates).

        Returns:
            The function that stops following it.
        """
        watch = _Watch(on_text, every)
        self._watches.setdefault(server, {})[uri] = watch
        handle = self._servers.get(server)
        link = handle.link if handle is not None else None
        if link is not None and link.following and link.client is not None and uri not in link.watchers:
            self._start_watcher(link, link.client, uri)

        def stop() -> None:
            """Stop following it (unsubscribing, while the connection lasts)."""
            watches = self._watches.get(server, {})
            if watches.get(uri) is watch:
                del watches[uri]
            handle = self._servers.get(server)
            current = handle.link if handle is not None else None
            if current is not None and (task := current.watchers.pop(uri, None)) is not None:
                task.cancel()
            if current is not None:
                current.updated.pop(uri, None)

        return stop

    def _live_link(self, server: str) -> _Link | None:
        """A server's link while it is connected."""
        handle = self._servers.get(server)
        link = handle.link if handle is not None else None
        return link if link is not None and link.client is not None and link.status == "connected" else None

    def _start_watcher(self, link: _Link, client: Any, uri: str) -> None:
        """Start following a watched resource on a connection."""
        link.updated.setdefault(uri, asyncio.Event())
        task = asyncio.get_running_loop().create_task(self._watching(link, client, uri),
                                                      name=f"mcp-{link.name}-watch")
        link.watchers[uri] = task
        task.add_done_callback(lambda done: link.watchers.get(uri) is done and link.watchers.pop(uri, None))

    async def _watching(self, link: _Link, client: Any, uri: str) -> None:
        """Follow one watched resource while the connection lasts, its text to whoever watches it now; never raises
        but to be cancelled."""
        from mcp.shared.exceptions import MCPError
        from mcp_types.version import MODERN_PROTOCOL_VERSIONS

        def watch() -> _Watch | None:
            """Who watches it now."""
            return self._watches.get(link.name, {}).get(uri)

        async def read(text: str) -> None:
            """Hand a read on."""
            if (now := watch()) is not None:
                now.on_text(text)

        try:
            await self._follow(link, client, uri, read, lambda: getattr(watch(), "every", None))
        except asyncio.CancelledError:
            # stopped on purpose (not the connection ending), still connected: say so, without waiting long
            if (uri not in link.updated and link.client is client
                    and client.protocol_version not in MODERN_PROTOCOL_VERSIONS):
                with contextlib.suppress(Exception), warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    async with asyncio.timeout(2):
                        await client.unsubscribe_resource(uri)
            raise
        except (MCPError, LookupError, OSError, TimeoutError, RuntimeError, ValueError) as e:
            log.warning("MCP %s: %s can't be followed (%s)", link.name, uri, describe_error(e))

    async def _follow(self, link: _Link, client: Any, uri: str, read: Callable[[str], Awaitable[None]],
                      every: Callable[[], float | None]) -> None:
        """Subscribe to one resource of a connection (``resources/subscribe`` on a 2025-era one, a
        ``subscriptions/listen`` stream on a 2026-07-28 one), then hand its text to ``read`` at once, again after each
        update it announces (the latest only: updates during a read make one more read, never a backlog) and again
        after ``every()`` seconds without one (None: only on updates); until cancelled. A read that fails is logged.

        Raises:
            MCPError, OSError, TimeoutError, RuntimeError, ValueError: the subscription failed.
        """
        from mcp.shared.exceptions import MCPError
        from mcp_types.version import MODERN_PROTOCOL_VERSIONS

        due = link.updated.setdefault(uri, asyncio.Event())
        async with AsyncExitStack() as stack:
            if client.protocol_version in MODERN_PROTOCOL_VERSIONS:
                await stack.enter_async_context(client.listen(resource_subscriptions=[uri]))
            else:
                with warnings.catch_warnings():  # deprecated for 2026-07-28 servers only
                    warnings.simplefilter("ignore")
                    await client.subscribe_resource(uri)
            due.clear()
            while True:
                try:
                    text = await _read(client, uri)
                except (MCPError, TimeoutError, OSError) as e:  # the connection's own checks see a dead link
                    log.info("MCP %s: %s couldn't be read (%s)", link.name, uri, describe_error(e))
                else:
                    await read(text)
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(every()):
                        await due.wait()
                due.clear()

    async def stop(self) -> None:
        """Close every connection."""
        await asyncio.gather(*(self._stop_link(link) for link in self._links.values()))
        self._servers.clear()
        self._refresh()

    # ------------------------------------------------------------------------------------------------ calls
    async def call(self, server: str, tool: str, args: Mapping[str, JSON], ctx: CallContext) -> str:
        """Run a tool for whoever asks; never raises.

        Args:
            server: the server's name.
            tool: the tool's name on that server.
            args: its arguments.
            ctx: who asks ("brain", "hud", "app"), the turn and the other app's name.

        Returns:
            The result text, or "Error: ..." in words (its pictures, if any, named by a placeholder).
        """
        return (await self.call_rich(server, tool, args, ctx)).text

    async def call_rich(self, server: str, tool: str, args: Mapping[str, JSON], ctx: CallContext) -> ToolOutput:
        """``call``, with the pictures the result holds (a screenshot, a camera frame) prepared for the models.

        Args:
            server: the server's name.
            tool: the tool's name on that server.
            args: its arguments.
            ctx: who asks.

        Returns:
            The result's text and pictures; never raises.
        """
        handle = self._servers.get(server)
        known = next((t for t in handle.tools if t.remote == tool), None) if handle else None
        builtin = handle is not None and handle.group is not None
        await self._learn()
        if handle is not None:
            handle.calls += 1
        op = self.ops.start(server=server, name=known.name if known else shown_name(server, tool, builtin=builtin),
                            title=known.title if known else "", ctx=ctx, args=args,
                            read_only=known.hints.get("read_only") if known else None)
        text, attempts, pictures = "Error: stopped before it finished.", 1, tuple[Image, ...]()
        refused = False
        try:
            refusal = await self._guarded(known, args, ctx, op) if known is not None else ""
            if refusal:
                text, refused = refusal, True
            else:
                text, attempts, pictures = await self._dispatch(handle, known, tool, args, ctx, op)
        except Exception as e:
            log.debug("the %s tool of %s failed", tool, server, exc_info=True)
            text = f"Error: {describe_error(e)}"
        finally:
            text = redact.redacted(text)  # whatever came back, a result or an error: no secret reaches a model or the HUD
            ok = not text.startswith("Error")
            if ok and known is not None and known.taints and ctx.turn:
                self.taint(ctx.turn, known.title or known.name)
                op.taints = True
            record = self.ops.end(op, ok=ok, result=text, attempts=attempts)
            if known is not None and not refused:  # a refusal was recorded as one
                self._record("tool", known, ctx, args, title=known.title or known.name,
                             outcome="ok" if ok else "failed", ms=record["ms"])
        return ToolOutput(text, pictures if ok else ())

    async def call_named(self, req: ToolRequest, ctx: CallContext) -> str:
        """Run a tool by its model-visible name; never raises.

        Args:
            req: the tool's name and arguments.
            ctx: who asks, and the turn.

        Returns:
            The result text, or "Error: ..." in words (an unknown name is traced too).
        """
        return (await self.call_named_rich(req, ctx)).text

    async def call_named_rich(self, req: ToolRequest, ctx: CallContext) -> ToolOutput:
        """``call_named``, with the pictures the result holds.

        Args:
            req: the tool's name and arguments.
            ctx: who asks, and the turn.

        Returns:
            The result's text and pictures; never raises.
        """
        tool = self._advertised.get(req.name)
        if tool is None:
            text = f"Error: there is no tool called {req.name}."
            self.ops.record(server=CORE_SERVER, name=req.name, title=req.name, ctx=ctx, args=req.args, ok=False,
                            result=text)
            return ToolOutput(text)
        return await self.call_rich(tool.server, tool.remote, req.args, ctx)

    def taint(self, turn: str, source: str = "") -> None:
        """Mark a turn as having used web or app content (its later calls carry "tainted").

        Args:
            turn: the turn.
            source: what it read ("Read a web page"); the first one is kept, for the question before a risky call.
        """
        self._tainted[turn] = self._tainted.get(turn) or source
        self._tainted.move_to_end(turn)
        while len(self._tainted) > TAINT_TURNS:
            self._tainted.popitem(last=False)

    def is_tainted(self, turn: str | None) -> bool:
        """Whether a turn used web or app content.

        Args:
            turn: the turn (None: no turn).

        Returns:
            True once a tainting call of the turn succeeded.
        """
        return turn is not None and turn in self._tainted

    def end_turn(self, turn: str) -> None:
        """Forget a finished turn's taint and operations.

        Args:
            turn: the turn.
        """
        self._tainted.pop(turn, None)
        self.ops.end_turn(turn)

    # ------------------------------------------------------------------------------------------------ internals
    async def _learn(self) -> None:
        """Learn the secrets that changed since the last call (a keyring may take a moment to answer: off the loop)."""
        if redact.stale():
            await asyncio.to_thread(redact.refresh)

    def _sandboxed(self) -> bool:
        """Whether commands run in a sandbox (``safety.sandbox_commands``; a container is the box, so nothing is outside)."""
        return self.cfg.safety.sandbox_commands and not profile().container

    async def _guarded(self, known: Tool, args: Mapping[str, JSON], ctx: CallContext, op: Op) -> str:
        """What no model can talk the hub out of, in order: the channel's limits, the secrets in the arguments, then the
        user's yes where it is needed (``tools.policy``).

        Returns:
            "" when the call may run; otherwise the refusal the model reads.
        """
        safety = self.cfg.safety
        channel = channel_of(ctx.origin, ctx.source)
        where = _PLACES[channel]
        title = known.title or known.name
        widens = widening(known.name, args) if known.builtin else ""
        if widens == "outside_sandbox" and not self._sandboxed():
            widens = ""  # no sandbox to be outside of
        verdict = channel_verdict(safety.channels, channel, name=known.name, server=known.server, hints=known.hints)
        if verdict == "never":
            return self._refused(known, ctx, args, f"{title} can't be used {where}: it is one of the things that "
                                                  "channel may never do, and no yes lifts that.")
        if widens and (channel in UNATTENDED or ctx.origin == "app"):
            return self._refused(known, ctx, args, f"{_WIDENS[widens]} can't be asked for {where}: only the user at "
                                                  "the PC can allow that.")
        if ctx.origin != "hud" and redact.holds_secret(args):
            self._record("block", known, ctx, args, title=f"Blocked {title}: its arguments held a secret",
                         outcome="blocked")
            return ("Error: this call was blocked: its arguments contain a secret (a key, a token or a password), and "
                    "secrets are never sent anywhere. Nothing was done. Don't try to send it another way.")
        tainted = self.is_tainted(ctx.turn)
        reason = confirm_reason(origin=ctx.origin, tainted=tainted, name=known.name, hints=known.hints,
                                after_content=safety.confirm_after_content, always=safety.always_confirm,
                                never=safety.never_confirm, widens=widens, channel_asks=verdict == "confirm")
        if not reason or self.confirmer is None:
            return ""
        source = self._tainted.get(ctx.turn or "", "") if reason == "content" else ""
        question = self.confirmer.question(turn=ctx.turn or "", tool=known.name, title=known.title,
                                           server=known.server, args=args, reason=reason,
                                           source=f"“{source}”" if source else "another app",
                                           source_kind=ctx.source, device=ctx.device)
        self.ops.ask(op, question.id, question.text)
        answer = await self.confirmer.ask(question)
        by = self.confirmer.answered_by(question.id)
        self._record("ask", known, ctx, args, title=f"Asked first: {known.title or known.name}",
                     outcome={"yes": "approved", "no": "declined"}.get(answer, "expired"),
                     actor=_ANSWERED.get(by, f"you ({by})") if by else "", detail=f"{reason}: {question.action}")
        if answer == "yes":
            return ""
        if answer == "no":
            return ("Error: the user said no, so it wasn't done. Don't try another way to do the same thing: say it "
                    "wasn't done and ask what they would like instead.")
        return ("Error: the user didn't answer in time, so it wasn't done. Don't try another way to do the same thing: "
                "say it needs their OK and wasn't done.")

    def _refused(self, known: Tool, ctx: CallContext, args: Mapping[str, JSON], why: str) -> str:
        """A call a limit forbids: recorded, and the model told why (and not to look for another way)."""
        self._record("deny", known, ctx, args, title=f"Refused {known.title or known.name}: {why}", outcome="denied")
        return f"Error: {why} Don't try another way to do it: say it can't be done from here."

    def _source(self, ctx: CallContext) -> str:
        """Who asked, as the activity log says it (``activity.SOURCES``)."""
        if ctx.origin == "app":
            remote = (self.cfg.mcp_servers or {}).get(ctx.client) or {}
            return "gesture" if remote.get("intents") is True else "app"
        if ctx.origin == "hud":
            return "system" if ctx.client else "hud"  # (his own sends and questions through a chat server)
        return ctx.source if ctx.source in SOURCES else "system"

    def _record(self, kind: "Kind", known: Tool, ctx: CallContext, args: Mapping[str, JSON], *, title: str, outcome: str,
                ms: int = 0, actor: str | None = None, detail: str = "") -> None:
        """One entry of the activity log (nothing when there is none): who asked (or, with ``actor``, who acted: "" for
        nobody), what, and what came of it."""
        if self._activity is None:
            return
        who = actor if actor is not None else {"brain": "the brain", "hud": ctx.client or "you, in the HUD",
                                                "user": "you", "app": ctx.client or "another app"}.get(ctx.origin, ctx.origin)
        self._activity.record(kind, source=self._source(ctx), title=title, outcome=outcome, actor=who,
                              connector=known.server, tool=known.name, detail=detail or preview_args(args),
                              turn=ctx.turn or "", ms=ms)

    async def _dispatch(self, handle: _Server | None, known: Tool | None, tool: str, args: Mapping[str, JSON],
                        ctx: CallContext, op: Op) -> tuple[str, int, tuple[Image, ...]]:
        """Send one call (and its one retry); return (text, attempts, pictures). MCP errors other than a dropped
        connection or a timeout propagate to ``call_rich``, which words them."""
        from mcp.shared.exceptions import MCPError
        from mcp_types import CONNECTION_CLOSED, REQUEST_TIMEOUT

        if handle is None:
            return f"Error: there's no MCP server called {op.server}.", 1, ()
        if known is None:
            why = f"has no tool called {tool}" if handle.tools else f"is {self._why(handle)}"
            return f"Error: the {handle.name} MCP server {why}.", 1, ()
        if not handle.enabled or tool in handle.disabled:
            return f"Error: the {tool} tool is switched off. Switch it on in Customize, Connectors to use it.", 1, ()
        if known.requires and (missing := profile().missing(known.requires)):
            return f"Error: {tool} can't work on this machine: it needs {missing}.", 1, ()
        link = handle.link
        if link is None:
            return f"Error: the {handle.name} MCP server is switched off.", 1, ()
        meta = self._meta(known, args, ctx) if link.kind != "external" else None
        limit = timeout_for(known, args, system=self.cfg.system, mcp=self.cfg.mcp,
                            server_timeout_s=_seconds(handle.settings.get("timeout")))
        retry = known.read_only or known.hints.get("idempotent") is True
        deadline = asyncio.get_running_loop().time() + RECONNECT_GRACE_S
        failure = ""
        for attempt in (1, 2):
            client = await self._client(link, deadline)
            if client is None:
                return f"Error: the {handle.name} MCP server is {self._why(handle)}.", attempt, ()
            link.calls += 1
            try:
                result = await client.call_tool(tool, dict(args), read_timeout_seconds=limit, meta=meta)
            except MCPError as e:
                if e.code == CONNECTION_CLOSED:
                    if link.client is client and not link.stopping:  # a crash: reconnect at once, and let a retry
                        link.up.clear()  # wait for the new connection rather than reuse this one
                        link.status = "reconnecting"
                        link.lost.set()
                    failure = (f"Error: the {handle.name} MCP server stopped unexpectedly; it's being restarted. "
                               "Try again in a few seconds.")
                elif e.code == REQUEST_TIMEOUT:
                    failure = f"Error: {tool} didn't finish within {limit:g} seconds."
                else:
                    raise
            else:
                if link.kind != "external":
                    self._take_events(result, op, ctx)
                eyes = self.cfg.eyes
                pictures = result_images(result, label=known.title or known.name, max_side=eyes.image_max_side,
                                         quality=eyes.image_quality)
                return result_text(result), attempt, pictures
            finally:
                link.calls -= 1
            if not retry:
                return failure, attempt, ()
        return failure, 2, ()

    def _meta(self, tool: Tool, args: Mapping[str, JSON], ctx: CallContext) -> dict[str, Any]:
        """The request ``_meta`` of a call to Orion's own link: provenance, and the query vector."""
        meta: dict[str, Any] = {PROVENANCE_META: provenance(ctx.origin, tainted=self.is_tainted(ctx.turn)),
                                CONTEXT_META: {"turn": ctx.turn or "", "source": ctx.source, "device": ctx.device}}
        value = args.get(tool.embed_arg) if tool.embed_arg else None
        if self._embed is not None and isinstance(value, str) and value.strip():
            try:
                vector = self._embed(value)
            except Exception:
                log.debug("no query vector for %s", tool.name, exc_info=True)
                vector = None
            if vector is not None:
                meta[QVEC_META] = encode_qvec(vector)
        return meta

    def _take_events(self, result: Any, op: Op, ctx: CallContext) -> None:
        """Pass on the HUD events a call of Orion's own tools published, stamped with the call and the turn."""
        events = (getattr(result, "meta", None) or {}).get(EVENTS_META) or ()
        for event in events:
            if isinstance(event, dict) and event.get("type") == TAINT_EVENT:  # its answer is someone else's text
                if ctx.turn:
                    self.taint(ctx.turn, str(event.get("source") or "") or op.title)
                    op.taints = True
                continue
            if not isinstance(event, dict) or event.get("type") not in HUD_EVENTS:
                continue
            stamped: dict[str, JSON] = {**event, "call": op.id, "turn": ctx.turn or ""}
            if event["type"] == "memory_update":
                op.memory = {"id": str(event.get("id") or ""), "text": str(event.get("text") or ""),
                             "profile": bool(event.get("profile"))}
            self._on_event(stamped)

    async def _client(self, link: _Link, deadline: float) -> Any:
        """The link's client, waiting (until ``deadline``) while it connects or reconnects; None when it can't serve."""
        loop = asyncio.get_running_loop()
        while link.client is None or link.status != "connected":
            left = deadline - loop.time()
            if link.stopping or link.status not in ("connecting", "reconnecting") or left <= 0:
                return None
            try:
                async with asyncio.timeout(left):
                    await link.up.wait()
            except TimeoutError:
                return None
        return link.client

    @staticmethod
    def _why(handle: _Server) -> str:
        """Why a server can't take calls now, in words."""
        if not handle.enabled:
            return "switched off"
        status = handle.status
        if status == "error":
            return f"not connected: {handle.error}" if handle.error else "not connected"
        return "reconnecting" if status == "reconnecting" else "still starting"

    def _start(self, link: _Link) -> None:
        """Start a link's supervising task."""
        link.stopping = False
        link.task = asyncio.get_running_loop().create_task(self._run(link), name=f"mcp-{link.name}")

    async def _stop_link(self, link: _Link) -> None:
        """Stop a link and wait for its connection to close."""
        link.stopping = True
        if link.grace is not None:
            link.grace.cancel()
            link.grace = None
        task, link.task = link.task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        link.client, link.status, link.error, link.signin = None, "connecting", "", False
        link.up.clear()
        link.settled.clear()
        link.lost.clear()

    async def _drop(self, name: str) -> None:
        """Forget an external server and close its connection."""
        handle = self._servers.pop(name)
        if handle.link is not None:
            self._links.pop(name, None)
            await self._stop_link(handle.link)

    async def _run(self, link: _Link) -> None:
        """Keep a link connected until it is stopped (see the module docstring for the grace and the backoff)."""
        wait = 0.0
        while not link.stopping:
            try:
                async with AsyncExitStack() as stack:
                    client = await self._open(stack, link)
                    listed = await _list_tools(client)
                    link.client, link.listed, link.error, link.since = client, listed, "", time.monotonic()
                    link.status = "connected"
                    self._end_grace(link)
                    self._sort(link)
                    link.up.set()
                    link.settled.set()
                    log.info("MCP %s: connected, %d tools", link.name, len(listed))
                    self._refresh()
                    watcher = None
                    if (link.kind == "external" and link.settings.get("intents") is True
                            and (on_intent := self._on_intent) is not None):
                        watcher = asyncio.get_running_loop().create_task(
                            self._follow_intents(link, client, on_intent), name=f"mcp-{link.name}-intents")
                    link.following = True
                    for uri in self._watches.get(link.name, {}):
                        if uri not in link.watchers:
                            self._start_watcher(link, client, uri)
                    try:
                        await self._hold(link, client)
                    finally:
                        link.following = False  # first: no watch starts on this connection while it closes
                        followers = [task for task in (watcher, *link.watchers.values()) if task is not None]
                        link.watchers.clear()
                        for task in followers:
                            task.cancel()
                        await asyncio.gather(*followers, return_exceptions=True)
                return
            except Exception as e:
                needs = _needs_user(e)
                if needs is not None or link.signin:
                    self._wait_for_user(link, needs)
                    return  # no retry on a timer: a changed setting, a sign-in or a restart starts it again
                was_up = link.client is not None
                if was_up and time.monotonic() - link.since >= STABLE_AFTER_S:
                    wait = 0.0  # it had stayed up a good while: this crash gets an immediate retry too
                link.client, link.error = None, describe_error(e)
                link.up.clear()
                link.settled.set()
                if was_up:
                    self._begin_grace(link)
                elif link.status != "reconnecting":
                    link.status = "error"
                log.warning("MCP %s: %s (retrying in %gs)", link.name, link.error, wait,
                            exc_info=log.isEnabledFor(logging.DEBUG))
                self._refresh()
            if link.stopping:
                return
            await asyncio.sleep(wait)
            wait = min(max(2 * wait, BACKOFF_MIN_S), float(self.cfg.mcp.backoff_max_s))

    def _wait_for_user(self, link: _Link, needs: NeedsUser | None) -> None:
        """A link can't connect until the user does something: say what, withdraw its tools at once (there is no
        reconnect to wait for) and stop trying."""
        link.client, link.status = None, "error"
        link.error = str(needs) if needs is not None else f"{link.name} needs you to sign in (Customize, Connectors)."
        link.up.clear()
        link.settled.set()
        self._end_grace(link)
        log.info("MCP %s: %s", link.name, link.error)
        self._refresh()

    async def _hold(self, link: _Link, client: Any) -> None:
        """Keep a session open until the link is stopped, checking now and then that the server is still there.

        A server busy with a call isn't asked (many answer nothing else meanwhile, and the call notices a crash
        itself). A changed tool list is taken up.

        Raises:
            ConnectionError: a call found the connection closed.
            TimeoutError: the server didn't list its tools in time.
        """
        interval = None if link.kind == "core" else float(self.cfg.mcp.heartbeat_s)
        if interval is not None and (link.kind == "tools" or not link.settings.get("url")):
            interval *= STDIO_HEARTBEAT_FACTOR
        while not link.stopping:
            try:
                async with asyncio.timeout(interval):
                    await link.lost.wait()
            except TimeoutError:
                if link.calls == 0:
                    async with asyncio.timeout(HEARTBEAT_TIMEOUT_S):
                        listed = await _list_tools(client)
                        if not _offers(client, "tools") and _offers(client, "resources"):
                            await client.list_resources()  # still there? (no tools to list)
                    if listed != link.listed:
                        link.listed = listed
                        self._sort(link)
                        self._refresh()
                continue
            link.lost.clear()
            raise ConnectionError("the server stopped unexpectedly")

    def _begin_grace(self, link: _Link) -> None:
        """A connected link dropped: keep its tools offered for the grace while it reconnects."""
        link.status = "reconnecting"
        if link.grace is None:
            link.grace = asyncio.get_running_loop().call_later(RECONNECT_GRACE_S, self._grace_over, link)

    def _end_grace(self, link: _Link) -> None:
        """The link is back (or stopped): no more grace timer."""
        if link.grace is not None:
            link.grace.cancel()
            link.grace = None

    def _grace_over(self, link: _Link) -> None:
        """The reconnect took too long: withdraw the link's tools."""
        link.grace = None
        if link.status == "reconnecting":
            link.status = "error"
            self._refresh()

    async def _open(self, stack: AsyncExitStack, link: _Link) -> Any:
        """Connect a link and return its MCP client (closed with ``stack``).

        Raises:
            ValueError: an external server has neither ``command`` nor ``url``.
            RuntimeError: there is no tools process to start.
        """
        from mcp import Client, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp_types import Implementation

        from orion import __version__

        info = Implementation(name="orion", title="ORION", version=__version__)
        if link.kind == "core":
            return await stack.enter_async_context(Client(self._core, client_info=info, cache=None))
        if link.kind == "tools":
            if not self._spawn:
                raise RuntimeError("there is no tools process to start")
            params = StdioServerParameters(command=self._spawn[0], args=list(self._spawn[1:]),
                                           env={**secrets.strip_environ(os.environ), "PATH": local_path(),
                                                **self._spawn_env})  # (his own process: no token among its variables)
            errlog = await self._errlog(stack, link.name)
            return await stack.enter_async_context(Client(stdio_client(params, errlog=errlog), client_info=info,
                                                          cache=None))
        settings = await self.prepared(link.name, link.settings)
        handler = self._notifications(link)  # an external server may push intents (intents: true)
        if url := settings.get("url"):
            headers = {str(key): str(value) for key, value in (settings.get("headers") or {}).items()}
            auth = None if self.hooks is None else self.hooks.auth(link.name, settings,
                                                                   lambda: setattr(link, "signin", True))
            if settings.get("transport") == "sse":
                from mcp.client.sse import sse_client

                transport: Any = sse_client(str(url), headers=headers, auth=auth)
            else:
                from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

                http = await stack.enter_async_context(create_mcp_http_client(headers=headers, auth=auth))
                transport = streamable_http_client(str(url), http_client=http)
            return await stack.enter_async_context(Client(transport, client_info=info, message_handler=handler,
                                                          cache=None))
        if not settings.get("command"):
            raise ValueError("needs either `command` (a local server) or `url` (a remote one)")
        params = await asyncio.to_thread(self._launch, link.name, settings)
        errlog = await self._errlog(stack, link.name)
        return await stack.enter_async_context(Client(stdio_client(params, errlog=errlog), client_info=info,
                                                      message_handler=handler, cache=None))

    def _launch(self, name: str, settings: Mapping[str, Any]) -> "StdioServerParameters":
        """How a server run by a command is started: its program, and an environment of its own, never Orion's (the
        variables ``safety.env_allow`` names, and ``env_desktop`` too for a server he trusts; its declared env; for one of
        his own servers, where his settings are). A server nobody reviewed (``connectors.sandboxed``: a plugin's or the
        user's own, unless the user switched the sandbox off for it) runs in the sandbox instead, with its folder under
        ``data/connectors`` as its home and the only place it may write, and no desktop (in a container, which is the
        sandbox already, it only gets the folder as its home and no desktop).

        Args:
            name: the server.
            settings: its settings, with its secrets filled in (``prepared``).

        Returns:
            The parameters to start it with.

        Raises:
            SandboxError: it is flagged for the sandbox and none can be made here.
        """
        from mcp import StdioServerParameters

        from orion.config import CONFIG_PATH
        from orion.connectors import connector_data, sandboxed

        safety = self.cfg.safety
        path = local_path()
        command, args = str(settings["command"]), [str(arg) for arg in settings.get("args") or ()]
        own = command == "orion"  # one of Orion's own servers (discord-serve...): this very install, wherever it is
        if own:
            command, args = sys.executable, ["-m", "orion", *args]
        program = shutil.which(command, path=path) or command
        confined = sandboxed(self.cfg, name, settings)
        env = secrets.inherited(os.environ, [*safety.env_allow, *([] if confined else safety.env_desktop)])
        env |= {"PATH": path, "HOME": str(Path.home()), **(self._spawn_env if own else {}),
                **{str(key): str(value) for key, value in (settings.get("env") or {}).items()}}
        cwd = settings.get("cwd") or None
        if not confined:
            return StdioServerParameters(command=program, args=args, env=env, cwd=cwd)
        folder = connector_data(self.cfg, name)
        folder.mkdir(parents=True, exist_ok=True)
        env["HOME"] = str(folder)
        if profile().container:  # the container is the box: its folder is its home and where it works, its env is its own
            return StdioServerParameters(command=program, args=args, env=env, cwd=cwd or str(folder))
        jail = sandbox.jail_for(self.cfg, self._config_file or CONFIG_PATH, network=True, write=[folder], memory_mb=0)
        line = sandbox.wrap([program, *args], jail, env=env, cwd=Path(cwd) if cwd else folder)
        return StdioServerParameters(command=line[0], args=line[1:], env={}, cwd=None)

    @staticmethod
    def _notifications(link: _Link) -> Callable[[Any], Awaitable[None]]:
        """The message handler of a link's client: an update of a resource followed (its intents feed, one being
        watched) wakes its follower (it only sets an event: the handler runs on the connection's receive path, where
        waiting for a reply would deadlock)."""

        async def handle(message: Any) -> None:
            if getattr(message, "method", None) != "notifications/resources/updated":
                return
            if (due := link.updated.get(str(getattr(getattr(message, "params", None), "uri", "")))) is not None:
                due.set()

        return handle

    async def _follow_intents(self, link: _Link, client: Any,
                              on_intent: Callable[[str, "Intent"], Awaitable[None]]) -> None:
        """Follow a server's intents feed while the connection lasts (see ``orion.intents``): each read runs the
        intents due, in order, through ``on_intent`` (the first read too: what is fresh may run). Never raises but to
        be cancelled; a server that can't be followed is said once, and its tools still work."""
        from mcp.shared.exceptions import MCPError

        name = link.name

        async def run(text: str) -> None:
            """Run the intents due in one read of the feed."""
            feed = parse_feed(text)
            if feed.instance != self._intents_run.setdefault(name, feed.instance):  # it restarted: ids start over
                self._intents_last[name] = 0
                self._intents_run[name] = feed.instance
            ready, self._intents_last[name] = due(feed, self._intents_last.get(name, 0), time.time() * 1000)
            for intent in ready:
                try:
                    await on_intent(name, intent)
                except Exception:
                    log.warning("MCP %s: intent #%d failed", name, intent.id, exc_info=True)

        try:
            if getattr(client.server_capabilities, "resources", None) is None or not await _lists(client, INTENTS_URI):
                raise LookupError(f"it lists no {INTENTS_URI} resource")
            self._intents_warned.discard(name)
            log.info("MCP %s: following its intents", name)
            await self._follow(link, client, INTENTS_URI, run, lambda: None)
        except (MCPError, LookupError, OSError, TimeoutError, RuntimeError, ValueError) as e:
            if name not in self._intents_warned:
                self._intents_warned.add(name)
                log.warning("MCP %s: its intents can't be followed (%s); its tools still work", name,
                            describe_error(e))

    async def _errlog(self, stack: AsyncExitStack, name: str) -> TextIO:
        """The server's stderr log, data/logs/mcp-<name>.log, cut back when it grew too big (opened off the loop)."""
        path = self.cfg.logs_path / f"mcp-{_safe(name) or 'server'}.log"
        log_file = await asyncio.to_thread(_open_log, path)
        stack.callback(log_file.close)
        return log_file

    def _sort(self, link: _Link) -> None:
        """Hand the tools a link lists to the servers it stands for (Orion's own go by their group)."""
        for handle in self._servers.values():
            if handle.link is link:
                handle.tools = [self._wrap(handle, tool) for tool in link.listed
                                if link.kind == "external" or _orion_meta(tool).get("group") == handle.name]

    def _wrap(self, handle: _Server, listed: Any) -> Tool:
        """One listed MCP tool as the brain sees it."""
        builtin = handle.group is not None
        about = " ".join(str(listed.description or listed.title or listed.name).split())
        description = about
        if not builtin:  # tell the model where an external tool comes from
            extra = str(handle.settings.get("description") or "")
            source = f"{handle.name}: {extra}" if extra else handle.name
            description = clip(f"[{source}] {about}", self.cfg.mcp.description_chars, flat=False)
        ours = _orion_meta(listed) if builtin else {}
        hints = _hints(listed)
        override = ours.get("taints")
        notes = getattr(listed, "annotations", None)
        examples = ours.get("examples")
        embed_arg = ours.get("embed_arg")
        requires = ours.get("requires")
        return Tool(name=shown_name(handle.name, str(listed.name), builtin=builtin), description=description,
                    parameters=wrap_schema(getattr(listed, "input_schema", None)), server=handle.name,
                    remote=str(listed.name), title=str(listed.title or getattr(notes, "title", None) or ""),
                    summary=summary_of(about),
                    examples=tuple(str(e) for e in examples) if isinstance(examples, list) else (),
                    hints=hints, taints=taints(open_world=hints.get("open_world"), external=not builtin,
                                               override=override if isinstance(override, bool) else None),
                    embed_arg=embed_arg if isinstance(embed_arg, str) else "", builtin=builtin,
                    requires=tuple(str(r) for r in requires) if isinstance(requires, list) else ())

    def _refresh(self) -> None:
        """Recompute the advertised toolbox (a new ``version`` only when it differs) and tell ``on_change``. A tool
        this machine can't run (``Tool.requires``) is left out."""
        host = profile()
        tools: dict[str, Tool] = {}
        for handle in self._servers.values():
            if handle.enabled and handle.link is not None and handle.link.status in ("connected", "reconnecting"):
                for tool in handle.tools:
                    if tool.remote not in handle.disabled and not (tool.requires and host.missing(tool.requires)):
                        tools.setdefault(tool.name, tool)
        signature = tuple((t.name, t.server, t.description, json.dumps(t.parameters, sort_keys=True, default=str),
                           t.title, tuple(sorted(t.hints.items())), t.taints, t.embed_arg) for t in tools.values())
        if signature != self._signature:
            self._signature = signature
            self._advertised = MappingProxyType(tools)
            self._version += 1
        try:
            self._on_change()
        except Exception:
            log.warning("the toolbox listener failed", exc_info=True)


def _needs_user(error: BaseException) -> NeedsUser | None:
    """The ``NeedsUser`` an error is, or holds (anyio wraps what a connection raises in exception groups)."""
    if isinstance(error, NeedsUser):
        return error
    if isinstance(error, BaseExceptionGroup):
        return next((found for inner in error.exceptions if (found := _needs_user(inner)) is not None), None)
    return None


def _offers(client: Any, capability: str) -> bool:
    """Whether a connected server says it has this capability ("tools", "resources")."""
    capabilities = getattr(client, "server_capabilities", None)
    return capabilities is None or getattr(capabilities, capability, None) is not None


async def _lists(client: Any, uri: str) -> bool:
    """Whether a server lists the resource ``uri``, looked for page after page."""
    cursor: str | None = None
    while True:
        page = await client.list_resources(cursor=cursor)
        if any(str(resource.uri) == uri for resource in page.resources):
            return True
        cursor = page.next_cursor
        if not cursor:
            return False


async def _list_tools(client: Any) -> list[Any]:
    """Every tool a server lists, page after page; none from a server that has no tools at all."""
    if not _offers(client, "tools"):
        return []
    tools: list[Any] = []
    cursor: str | None = None
    while True:
        page = await client.list_tools(cursor=cursor)
        tools += page.tools
        cursor = page.next_cursor
        if not cursor:
            return tools


async def _read(client: Any, uri: str) -> str:
    """The text of one resource (its text contents, joined), read within ``HEARTBEAT_TIMEOUT_S``."""
    async with asyncio.timeout(HEARTBEAT_TIMEOUT_S):
        result = await client.read_resource(uri)
    return "".join(str(getattr(content, "text", "") or "") for content in getattr(result, "contents", ()))


def _open_log(path: Path) -> TextIO:
    """Open a server's log for appending, first keeping only its last ``LOG_KEEP_BYTES`` when it passed the limit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileNotFoundError):
        if path.stat().st_size > LOG_MAX_BYTES:
            with path.open("rb") as f:
                f.seek(-LOG_KEEP_BYTES, os.SEEK_END)
                tail = f.read()
            path.write_bytes(tail[tail.find(b"\n") + 1:])
    return path.open("a", encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------------------------------- lending
class Gateway:
    """/mcp: the running Orion's switched-on tools for other AI apps (an ASGI app), on this PC or a device paired
    with it (its key as a bearer token): his own, and those of the servers lent (``MCPHub.lent``).

    Sessions are stateful, so each call knows which app made it (its ``clientInfo``); the calls go through the hub
    as "app" operations, shown in the HUD as that app's rows. The session manager starts with the first request
    (the HUD's server runs without lifespan events) and stops with ``close``.
    """

    def __init__(self, hub: MCPHub) -> None:
        """Prepare the MCP server and its session manager.

        Args:
            hub: the hub whose tools are lent.
        """
        from mcp.server import Server
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
        from mcp.server.transport_security import TransportSecuritySettings

        from orion import __version__

        self._hub = hub
        server = Server("orion", version=__version__, title="ORION", instructions=INSTRUCTIONS,
                        on_list_tools=self._list_tools, on_call_tool=self._call_tool)
        # the HUD's envelope has checked each request's host and origin already, and that it is this PC or a device
        # paired with it (server.py)
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
        self._sessions = StreamableHTTPSessionManager(server, stateless=False, json_response=True,
                                                      security_settings=security)
        self._task: asyncio.Task[None] | None = None
        self._started = asyncio.Event()
        self._closing = asyncio.Event()

    async def __call__(self, scope: "Scope", receive: "Receive", send: "Send") -> None:
        """Serve one HTTP request to /mcp."""
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._serve(), name="mcp-gateway")
        if not self._started.is_set():
            waiter = asyncio.ensure_future(self._started.wait())
            await asyncio.wait({waiter, self._task}, return_when=asyncio.FIRST_COMPLETED)
            if not self._started.is_set():
                waiter.cancel()
                raise RuntimeError("the MCP gateway didn't start")
        await self._sessions.handle_request(scope, receive, send)

    async def close(self) -> None:
        """Stop the session manager (open sessions end)."""
        self._closing.set()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)

    async def _serve(self) -> None:
        """Run the session manager until ``close``."""
        async with self._sessions.run():
            self._started.set()
            await self._closing.wait()

    async def _list_tools(self, _ctx: Any, _params: Any) -> "mt.ListToolsResult":
        """List the tools lent."""
        import mcp_types as mt

        return mt.ListToolsResult(tools=[definition(tool) for tool in self._hub.lent()])

    async def _call_tool(self, ctx: Any, params: "mt.CallToolRequestParams") -> "mt.CallToolResult":
        """Run a lent tool for the calling app."""
        import mcp_types as mt

        tool = next((t for t in self._hub.lent() if t.name == params.name), None)
        if tool is None:
            text = f"Error: Orion has no tool called {params.name}, or it is switched off."
        else:
            text = await self._hub.call(tool.server, tool.remote, params.arguments or {},
                                        CallContext("app", client=_client_name(ctx)))
        return mt.CallToolResult(content=[mt.TextContent(type="text", text=text)], is_error=text.startswith("Error"))


def _client_name(ctx: Any) -> str:
    """The calling app's name: its clientInfo title or name, else its User-Agent, else "another app"."""
    from mcp_types import CLIENT_INFO_META_KEY

    session = getattr(ctx, "session", None)
    params = getattr(session, "client_params", None)
    info: Any = getattr(params, "client_info", None)
    name = ""
    if info is not None:
        name = str(getattr(info, "title", None) or getattr(info, "name", None) or "")
    elif isinstance(meta := getattr(ctx, "meta", None), Mapping) and isinstance(raw := meta.get(CLIENT_INFO_META_KEY),
                                                                                 Mapping):
        name = str(raw.get("title") or raw.get("name") or "")
    if not name and (request := getattr(ctx, "request", None)) is not None:
        name = str(getattr(request, "headers", {}).get("user-agent", "")).split("/", 1)[0]
    return " ".join(name.split())[:60] or "another app"

