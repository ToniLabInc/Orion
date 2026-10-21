"""Orion's own tools, served over the Model Context Protocol.

Each module here holds groups of tools: system, web, organizer, and memory with skills. One MCP server serves them all
in one background process (``orion mcp-serve --hub``), which Orion reaches through the same hub as any
external server; the hub shows each group as a server of its own. ``core.py`` adds Orion's own controls, served
inside his process, and ``orion mcp-serve`` lends the built-in tools to other AI apps.

A tool is a plain function, sync or async: the SDK builds its input schema from the type hints (what a parameter means
goes in its ``Field(description=...)``), validates the arguments and runs sync tools on worker threads. ``Group.tool``
adds what people and the hub need: a title, MCP's behaviour hints, routing examples, whether it taints its turn, and
which argument the hub should send a query vector for.

Per call, the server's extension reads what the hub put in the request ``_meta`` (who asks, a query vector) into
``current_call()``, and returns the HUD events a tool ``publish``-ed in the result's ``_meta``. It also learns the
secrets the vault gained and hides what it holds from the result's texts (the hub does the same, and is the one that
limits the channels: lending directly, ``orion mcp-serve``, there is no hub, so the extension refuses what the "app"
channel may never use). Lending to another app trusts none of it: every call counts as "app" and no events go back.

The MCP SDK loads when a server is built; importing this package costs only the standard library.
"""

import base64
import contextlib
import functools
import importlib
import logging
import os
import threading
import time
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

from orion.base import (
    CONTEXT_META,
    EVENTS_META,
    JSON,
    PROVENANCE_META,
    QVEC_META,
    GroupInfo,
    Provenance,
    server_on,
    switched_off,
)
from orion.tools.policy import trusted_provenance

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

    from orion.config import Config, Settings
    from orion.memory import MemorySystem
    from orion.skills import Skills

log = logging.getLogger("orion.tools")

type Role = Literal["tools", "serve"]  # the hub's private tools process, or `orion mcp-serve` for other apps

INSTRUCTIONS: Final = "ORION's tools, on this Linux PC."
EXTENSION_ID: Final = "dev.orion/calls"  # the extension that carries provenance in and HUD events out
WARMUP_DELAY_S: Final = 3  # the tools process warms up its slow imports this long after it starts
WARMUP_IMPORTS: Final = ("ddgs", "trafilatura", "dateparser")  # libraries whose first import takes 0.3-1.5 s
GROUPS: Final[Mapping[str, GroupInfo]] = MappingProxyType({info.name: info for info in (
    GroupInfo("system", "PC control", "This Linux PC: open and close apps, websites, files and folders, find, read and "
                                      "write files, run commands, volume, media playback, lock the screen, power, and "
                                      "the PC's health."),
    GroupInfo("web", "The internet", "The internet: web search, news headlines, reading web pages, and the weather."),
    GroupInfo("organizer", "Timers, reminders and tasks", "Timers, reminders and the user's to-do list."),
    GroupInfo("memory", "Memory", "Long-term memory: facts about the user, Orion's own notes, the journal of past "
                                  "conversations, and notes the user asked him to keep."),
    GroupInfo("skills", "Skills", "Skills: saved step-by-step routines Orion can run, and new ones the user "
                                  "teaches him."),
    GroupInfo("calendar", "Calendar", "The user's calendars (CalDAV, or a secret iCal link): their agenda, adding and "
                                      "deleting events."),
)})


@dataclass(frozen=True, slots=True)
class CallInfo:
    """What the hub said about the call being run.

    Attributes:
        provenance: who it is for: "brain", "tainted" (the brain, after web or app content in the turn), "hud" or
            "app" (anything not trusted, and every call of an app borrowing the tools).
        qvec: the query vector of the tool's ``embed_arg``, unit length; None when the hub sent none.
        turn: the turn the call belongs to ("" outside one, or when lending).
        source: where the turn's request came from ("voice", "hud", "discord", "job"; "").
        device: the HUD the request came from or that carries the voice ("" for this machine).
    """

    provenance: Provenance
    qvec: "NDArray[np.float32] | None"
    turn: str = ""
    source: str = ""
    device: str = ""


_OUTSIDE: Final = CallInfo("app", None)  # outside a call: trusted with nothing
_call: ContextVar[CallInfo] = ContextVar("orion_call", default=_OUTSIDE)
_events: ContextVar[list[dict[str, JSON]] | None] = ContextVar("orion_events", default=None)


def current_call() -> CallInfo:
    """What the hub said about the call being run (sync tools see it on their worker thread too).

    Returns:
        The call's info; outside a call, "app" provenance and nothing else.
    """
    return _call.get()


def publish(kind: str, /, **data: JSON) -> None:
    """Tell the HUD something changed (a reminder set, a memory saved): the event travels back to Orion with the
    result of the tool being run, and he publishes it. Outside a call (or when lending), it goes nowhere.

    Args:
        kind: the event type ("reminders", "memory_update", ...).
        **data: its fields.
    """
    events = _events.get()
    if events is not None:
        events.append({"type": kind, **data})


def decode_qvec(value: object) -> "NDArray[np.float32] | None":
    """A query vector from ``_meta["orion/qvec"]`` (base64 of float16 values).

    Args:
        value: what the request carried.

    Returns:
        The vector at unit length, as float32; None for anything that isn't a finite, non-zero vector.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        raw = base64.b64decode(value, validate=True)
    except ValueError:
        return None
    if not raw or len(raw) % 2:
        return None
    import numpy as np

    vector = np.frombuffer(raw, dtype=np.float16).astype(np.float32)
    norm = float(np.linalg.norm(vector))
    return vector / norm if np.isfinite(vector).all() and norm > 0 else None


class Group:
    """A group of Orion's tools, shown on the Abilities page as a server of its own."""

    def __init__(self, info: GroupInfo) -> None:
        """Start an empty group.

        Args:
            info: its name, title and description (``GROUPS``, or the core's).
        """
        self.info = info
        self.tools: dict[str, tuple[Callable[..., Any], dict[str, Any]]] = {}  # name -> (function, how it registers)

    @property
    def name(self) -> str:
        """The group's server name ("system")."""
        return self.info.name

    @property
    def title(self) -> str:
        """Its title for people ("PC control")."""
        return self.info.title

    @property
    def description(self) -> str:
        """What its tools do."""
        return self.info.description

    def tool[F: Callable[..., Any]](self, *, title: str, read_only: bool = False, destructive: bool = False,
                                    open_world: bool = False, idempotent: bool | None = None,
                                    taints: bool | None = None, name: str = "", examples: tuple[str, ...] = (),
                                    embed_arg: str = "", requires: tuple[str, ...] = ()) -> Callable[[F], F]:
        """Register a function as a tool of this group; its docstring tells the model what it does.

        Args:
            title: a plain name for people ("Start a timer").
            read_only: it changes nothing (MCP hint; the hub may run it alongside others and retry it).
            destructive: it may destroy something (MCP hint).
            open_world: it reaches outside the PC (MCP hint; such a tool taints its turn unless ``taints`` says no).
            idempotent: calling it twice does no more than once (MCP hint; None when unsaid).
            taints: whether a successful call makes the rest of its turn untrusted (None: as ``open_world``).
            name: the tool's name ("" uses the function's).
            examples: things a user might say to use it (tool routing ranks against them).
            embed_arg: the argument the hub sends a query vector for ("" for none).
            requires: what the machine must have for it to work (``orion.host.NEEDS``); elsewhere it isn't offered.

        Returns:
            A decorator that registers the function and returns it unchanged.
        """

        def register(fn: F) -> F:
            """Add the function to the group."""
            self.tools[name or fn.__name__] = (fn, {
                "title": title,
                "description": " ".join((fn.__doc__ or "").split()),
                "hints": {"read_only": read_only, "destructive": destructive, "open_world": open_world,
                          "idempotent": idempotent},
                "meta": {"orion": {"group": self.name, "examples": list(examples), "taints": taints,
                                    "embed_arg": embed_arg, "requires": list(requires)}},
            })
            return fn

        return register


class ToolsServer(Protocol):
    """The MCP server ``build_server`` makes: the SDK's MCPServer, with failures that read as actionable text."""

    name: str

    def run(self, transport: Literal["stdio", "sse", "streamable-http"] = "stdio") -> None:
        """Serve until the client goes away."""


class Context:
    """What the built-in tools work with, opened when a tool first needs it (the PC's tools need none of it).

    Safe to use from the tools' worker threads.
    """

    def __init__(self) -> None:
        """Start with nothing open."""
        self._path: Path | None = None
        self._role: Role = "tools"
        self._lock = threading.RLock()
        self._settings: Settings | None = None
        self._memory: MemorySystem | None = None
        self._skills: Skills | None = None

    def open(self, config_path: Path, *, role: Role = "tools") -> None:
        """Point the context at a config.yaml (closing nothing: the parts open again when next needed).

        Args:
            config_path: the settings file.
            role: "tools" (the hub's process) or "serve" (lending to other apps).
        """
        with self._lock:
            self._path, self._role = config_path, role
            self._settings = self._memory = self._skills = None

    @property
    def settings(self) -> "Settings":
        """The settings (role "tools": read, never written).

        Raises:
            RuntimeError: ``open`` was never called.
        """
        with self._lock:
            if self._settings is None:
                if self._path is None:
                    raise RuntimeError("the tools have no settings: call ctx.open(config_path) first")
                from orion.config import Settings

                self._settings = Settings.open(self._path, role="tools")
            return self._settings

    @property
    def config(self) -> "Config":
        """The live settings (``settings.cfg``)."""
        return self.settings.cfg

    @property
    def memory(self) -> "MemorySystem":
        """Orion's memory, opened for this process's role (its embedder loads only if a call needs it)."""
        with self._lock:
            if self._memory is None:
                from orion.inference import Embedder
                from orion.memory import MemorySystem

                cfg = self.config
                self._memory = MemorySystem.open(cfg, Embedder.from_config(cfg), role=self._role)
            return self._memory

    @property
    def skills(self) -> "Skills":
        """The skill store."""
        with self._lock:
            if self._skills is None:
                from orion.skills import Skills

                self._skills = Skills.open(self.config)
            return self._skills

    def refresh(self) -> list[str]:
        """Take up settings changed since the last call (one ``stat`` when nothing changed); the keys that changed."""
        settings = self._settings
        return settings.refresh_if_changed() if settings is not None else []


ctx = Context()


@functools.cache
def _classes() -> tuple[type[Any], type[Any]]:
    """The SDK-based server and extension classes, defined on first use so that importing tools stays light."""
    import mcp_types as mt
    from mcp.server.mcpserver import Extension, MCPServer
    from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
    from pydantic import ValidationError

    from orion import redact

    class Calls(Extension):
        """Per call: provenance and query vector in from the request ``_meta``; HUD events out in the result's
        ``_meta`` (none when lending)."""

        identifier = EXTENSION_ID

        def __init__(self, *, lend: bool) -> None:
            """Remember whether the tools are lent to another app (then nothing in ``_meta`` is trusted)."""
            self.lend = lend

        async def intercept_tool_call(self, params: Any, request: Any, call_next: Any) -> Any:
            """Run the tool with the call's info set, and attach the events it published."""
            changed = ctx.refresh()  # settings changed since the last call
            if {"safety.secret_patterns", "safety.redact_min_chars"} & set(changed):
                safety = ctx.config.safety
                redact.active().configure(safety.secret_patterns, min_chars=safety.redact_min_chars)
            redact.refresh()  # secrets the vault gained since
            if self.lend and (why := _lent_refusal(str(getattr(params, "name", "")))):
                return mt.CallToolResult(content=[mt.TextContent(type="text", text=why)], is_error=True)
            meta = getattr(params, "meta", None) or {}
            context = meta.get(CONTEXT_META) if not self.lend else None
            where = context if isinstance(context, dict) else {}
            info = CallInfo(provenance=trusted_provenance(meta.get(PROVENANCE_META), lend=self.lend),
                            qvec=None if self.lend else decode_qvec(meta.get(QVEC_META)),
                            turn=str(where.get("turn") or ""), source=str(where.get("source") or ""),
                            device=str(where.get("device") or ""))
            events: list[dict[str, JSON]] = []
            call_token, events_token = _call.set(info), _events.set(None if self.lend else events)
            try:
                result = await call_next(request)  # sync tools run on a thread that gets a copy of this context
            finally:
                _events.reset(events_token)
                _call.reset(call_token)
            if isinstance(result, mt.CallToolResult):
                result.content = [mt.TextContent(type="text", text=redact.redacted(block.text))
                                  if isinstance(block, mt.TextContent) else block for block in result.content]
                if events:
                    result.meta = {**(result.meta or {}), EVENTS_META: events}
            return result

    class Server(MCPServer):
        """The SDK's MCPServer, with failures the model can act on: "ConnectError: All connection attempts failed"
        instead of a bare "Error executing tool read_webpage", and which argument was wrong, without pydantic's
        links."""

        async def call_tool(self, name: str, arguments: dict[str, Any], context: Any = None) -> Any:
            """Run a tool; a failure comes back as an error result in plain words."""
            try:
                return await super().call_tool(name, arguments, context)
            except ToolError as e:
                cause = e.__cause__
                if isinstance(cause, ValidationError):
                    text = f"Invalid arguments for {name}: " + "; ".join(
                        f"{'.'.join(map(str, err['loc'])) or 'arguments'}: {err['msg']}" for err in cause.errors())
                elif isinstance(e, UnexpectedToolError) and cause is not None:
                    text = f"{type(cause).__name__}: {cause}"
                    log.warning("the %s tool failed: %s", name, text)
                    log.debug("where the %s tool failed", name, exc_info=cause)
                else:  # a failure the tool saw coming, or an unknown tool
                    text = str(cause or e)
                return mt.CallToolResult(content=[mt.TextContent(type="text", text=text)], is_error=True)

    return Server, Calls


def _lent_refusal(name: str) -> str:
    """Why a tool lent directly to another app (``orion mcp-serve``) is refused: the "app" channel may never use it."""
    from orion.tools.policy import channel_verdict

    for group in builtin_groups().values():
        if name in group.tools:
            hints = group.tools[name][1]["hints"]
            if channel_verdict(ctx.config.safety.channels, "app", name=name, server=group.name, hints=hints) == "never":
                return f"Error: {name} can't be used for another app: it is one of the things that channel may never do."
    return ""


def build_server(groups: Iterable[Group], *, switched_off: Mapping[str, Collection[str]] | None = None,
                 name: str = "orion-tools", lend: bool = False) -> ToolsServer:
    """One MCP server with the tools of these groups.

    Args:
        groups: the groups to serve.
        switched_off: tools to leave out, by group name (the hub's own process serves everything: it filters).
        name: the server's name.
        lend: whether the tools are lent to another app (nothing in ``_meta`` is trusted, no events go back).

    Returns:
        The server.
    """
    import mcp_types as mt

    from orion import __version__

    server_class, extension_class = _classes()
    chosen = list(groups)
    server = server_class(name, version=__version__, extensions=[extension_class(lend=lend)],
                          instructions=" ".join([INSTRUCTIONS, *(group.description for group in chosen)]))
    for group in chosen:
        off = (switched_off or {}).get(group.name, ())
        for tool, (fn, options) in group.tools.items():
            if tool in off:
                continue
            hints = options["hints"]
            server.add_tool(fn, name=tool, title=options["title"], description=options["description"],
                            annotations=mt.ToolAnnotations(read_only_hint=hints["read_only"],
                                                           destructive_hint=hints["destructive"],
                                                           open_world_hint=hints["open_world"],
                                                           idempotent_hint=hints["idempotent"]),
                            meta=options["meta"], structured_output=False)  # replies in words, not {"result": ...}
    return server


def builtin_groups() -> dict[str, Group]:
    """Orion's built-in groups of tools, by name (imports their modules; their heavy libraries load on use).

    Returns:
        system, web, organizer, memory, skills and calendar.
    """
    from orion.tools import calendar, memory, organizer, system, web

    found = (system.group, web.group, organizer.group, memory.group, memory.skills_group, calendar.group)
    return {group.name: group for group in found}


def serve(*, groups: Sequence[str] = (), lend: bool) -> None:
    """Serve the built-in tools over stdio until the client goes away (stdout carries the protocol).

    The hub's private process (``lend=False``) serves every group and tool, switched off or not: the hub filters,
    so switching needs no restart; it trusts the provenance the hub sends and returns HUD events. Lending to another
    app (``lend=True``, ``orion mcp-serve [groups]``) serves only what is switched on, and every call counts as
    "app".

    Args:
        groups: the groups to serve (empty: all).
        lend: whether the tools are lent to another app.

    Raises:
        SystemExit: a group name is unknown.
    """
    from orion.config import CONFIG_PATH

    ctx.open(CONFIG_PATH, role="serve" if lend else "tools")
    _protect()
    available = builtin_groups()
    if unknown := [name for name in groups if name not in available]:
        raise SystemExit(f"Unknown group(s): {', '.join(unknown)}. Choose from: {', '.join(available)}")
    chosen = [available[name] for name in groups or available]
    off: dict[str, frozenset[str]] = {}
    if lend:
        servers = ctx.config.mcp_servers
        chosen = [group for group in chosen if server_on(servers.get(group.name) or {})]
        off = {group.name: switched_off(servers.get(group.name)) for group in chosen}
    name = "orion" if lend else "orion-tools"
    server = build_server(chosen, switched_off=off, name=name, lend=lend)
    if not lend and ctx.config.tools.warm_up:
        warm_up(WARMUP_DELAY_S)
    log.info("serving %s over stdio", ", ".join(group.name for group in chosen))
    server.run("stdio")


def _protect() -> None:
    """Give this process his vault (the calendar's secrets are filled in here) and hide what it holds from its logs and
    results."""
    from orion import secrets

    cfg = ctx.config
    try:
        vault = secrets.open_vault(cfg)
    except secrets.VaultError as e:
        log.warning("no vault in this process: %s", e)
        vault = None
    secrets.protect(cfg.safety, vault, os.environ)


def warm_up(delay_s: float) -> threading.Thread:
    """Warm the tools process up in the background, at low priority: the slow imports, the PC tools' baselines and
    the memory (opened, its model not loaded). Trades some memory for first calls that aren't 0.3-1.5 s slower.

    Args:
        delay_s: how long to wait first, so the start of Orion isn't slowed down.

    Returns:
        The (daemon) thread.
    """

    def run() -> None:
        """Lower this thread's priority, wait, then run each step (a failure is logged and skipped)."""
        with contextlib.suppress(OSError):
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)  # Linux: per-thread nice
        time.sleep(delay_s)
        steps: list[tuple[str, Callable[[], object]]] = [
            *((module, functools.partial(importlib.import_module, module)) for module in WARMUP_IMPORTS),
            ("PC tools", _prime_system), ("memory", lambda: ctx.memory)]
        for label, step in steps:
            try:
                step()
            except Exception:
                log.warning("warm-up step %s failed", label, exc_info=True)

    thread = threading.Thread(target=run, name="tools-warm-up", daemon=True)
    thread.start()
    return thread


def _prime_system() -> None:
    """Let the PC tools take their baselines (CPU counters, the installed apps' index)."""
    from orion.tools import system

    system.prime()
