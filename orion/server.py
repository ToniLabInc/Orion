"""The HUD's server: the HUD itself, its live event stream, the REST API behind its pages, and /mcp for other AI apps.

Orion has full access to the PC, so every request passes the ``Envelope`` first. By default the server listens on
127.0.0.1, and answers his own pages there: a web page elsewhere in the browser can't drive him (the Host and Origin
must be his own: no DNS rebinding, no other site), nor show his pages in a frame to steer clicks, and his pages load
nothing from elsewhere. With ``server.host`` open to your network, a device that isn't this PC must also be on it
(``server.lan_only``), name this PC (an address, its hostname, ``server.names``) and be paired (``orion.pairing``)
before /api, /ws or /mcp answer it; until then it gets the HUD's files and the pairing endpoints alone, which is how
its HUD shows the pairing screen. Each WebSocket client gets an ``Outbox``: important events stay in order and are
never dropped, while fast-changing ones (levels, stats, the hands' live view) are coalesced, so a slow tab never
backs up the rest. A tab too slow to keep up is closed (4408) and reconnects to a fresh ``init``. The live view of
the hands goes only to a tab that shows it (it says so: ``{"type": "hands_view", "on": true}``).

Every event a HUD receives is redacted on its way out (``encode``): a secret Orion knows never reaches a screen, whatever
part published it. The security pages' endpoints are here too: the activity log, the vault's secrets (names, set or
not, what uses them: never a value) and the channels' limits.

``serve`` runs Orion: the vault first (its secrets leave config.yaml before the file is written), the settings, the
Runtime, the HUD, then the models in the background.
"""

import asyncio
import contextlib
import ipaddress
import json
import logging
import re
import signal
import socket
import time
from collections import deque
from collections.abc import Awaitable, Callable, Collection, Iterator, Mapping, MutableMapping
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal
from urllib.parse import urlsplit

import numpy as np

from orion import images, redact, sandbox, vaulting
from orion.base import CORE_SERVER, CallContext, Event, Image
from orion.config import Settings, hud_schema, meta, secret_sources
from orion.pairing import COOKIE, COOKIE_MAX_AGE_S, Pairing
from orion.service import OllamaError, sd_notify
from orion.timing import STATS_TURNS, report
from orion.voice import STT_MODELS, audio_devices, stt_label, stt_options, voice_cards

if TYPE_CHECKING:
    from fastapi import FastAPI
    from starlette.requests import HTTPConnection

    from orion.activity import Activity
    from orion.config import Config, ServerConfig
    from orion.runtime import Runtime
    from orion.surfaces import Surface
    from orion.tools.core import ExitAction

log = logging.getLogger("orion.server")

type Scope = MutableMapping[str, Any]
type Message = MutableMapping[str, Any]
type Receive = Callable[[], Awaitable[Message]]
type Send = Callable[[Message], Awaitable[None]]
type ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]
type Lane = Literal["latest", "append"]
type Address = ipaddress.IPv4Address | ipaddress.IPv6Address
type Peer = Literal["local", "device", "unpaired"]

DIST: Final = Path(__file__).parent / "hud" / "dist"  # the HUD, built from web/ (see web/README.md)
# every page is index.html; the app routes itself (and sends an old /neural on to /memory)
PAGES: Final = ("/", "/memory", "/neural", "/automations", "/automations/{tab}", "/settings", "/settings/{tab}",
                "/activity", "/customize", "/customize/{rest:path}")
LOCAL_NAMES: Final = frozenset({"localhost", "127.0.0.1", "[::1]"})
WS_NOT_LOCAL: Final = 4403  # close code: not asked for by his own pages on this PC
WS_UNPAIRED: Final = 4401  # close code: a device on the network, not paired yet
OPEN_TO_ALL: Final = frozenset({("GET", "/api/pairing"), ("POST", "/api/pairing")})  # what an unpaired device may ask
WS_TOO_SLOW: Final = 4408  # close code: the client fell too far behind; it reconnects to a fresh init
WS_PING_S: Final = 20.0
OUTBOX_LIMIT: Final = 1000  # events waiting for one client before it counts as too slow
AUDIO_BACKLOG_BYTES: Final = 480_000  # 10 s of his voice (24 kHz, 16-bit) waiting for one browser at most
MIC_FRAME_KIND: Final = 1  # the first byte of a binary frame from a browser's microphone, then float32 samples...
MIC_INT16_KIND: Final = 3  # ...or int16 samples (what the HUD sends: half the bytes)
PREVIEW_CHARS: Final = 300  # the longest voice sample text
COALESCE: Final[Mapping[str, tuple[Lane, str]]] = {  # type -> (lane, the field that keys it, "" for the type alone)
    "level": ("latest", "source"), "hearing": ("latest", ""), "stats": ("latest", ""), "weather": ("latest", ""),
    "capabilities": ("latest", ""), "reminders": ("latest", ""), "todos": ("latest", ""), "boot": ("latest", ""),
    "model_pull": ("latest", "name"), "memory_indexed": ("latest", ""), "assistant_delta": ("append", "id"),
    "hands": ("latest", "server"), "spend": ("latest", ""), "bench": ("latest", "id"),
    "connectors": ("latest", ""), "plugins": ("latest", ""), "documents": ("latest", ""),
}
_CSP_PARTS: Final = (  # what his pages may load, and from where: from Orion alone
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",  # the animation and graph libraries add <style> elements of their own
    "img-src 'self' data: blob:",  # an image in a reply would otherwise be fetched from its site the moment it shows
    "font-src 'self' data:",
    "media-src 'self' blob:",  # a voice's sample, played from memory
    "connect-src 'self' ws://localhost:* ws://127.0.0.1:*",  # /ws, for any browser ([::1] can't be named: 'self' it is)
                                                           # (and the device's own host: ``security``)
    "worker-src 'self' blob:",
    "frame-ancestors 'none'",  # no other site shows his pages in a frame
    "base-uri 'none'",
    "object-src 'none'",
)
CSP: Final = "; ".join(_CSP_PARTS)
NOT_BUILT: Final = """<!doctype html><meta charset="utf-8"><title>ORION</title><link rel="icon" href="data:,">
<body style="background:#0b0606;color:#f0d8d4;font:15px/1.6 monospace;padding:2em">
<h1>ORION</h1><p>His HUD hasn't been built. In the folder Orion is in, run:</p>
<pre>uv run --only-group web npm --prefix web ci
uv run --only-group web npm --prefix web run build</pre><p>then reload this page.</p>"""
_IMMUTABLE: Final = b"public, max-age=31536000, immutable"  # the HUD's hashed assets never change under one name
_NO_CACHE: Final = b"no-cache"  # everything else is asked about every time (instant, on this PC)
_REFUSED: Final = "Orion only answers his own pages, on this PC or a device paired with it."
_UNPAIRED: Final = "This device isn't paired with Orion yet: on the PC, open Settings, General, Devices, and pair it."
_PLAIN_HOST: Final = re.compile(r"^[A-Za-z0-9.-]+(:\d{1,5})?$")  # a host CSP can name (no IPv6 literal)


# ---------------------------------------------------------------------------------------------------- the envelope
def same_site(headers: Mapping[str, str]) -> bool:
    """Whether a request comes from the page it is for: any Origin is this same host, and the browser doesn't say
    it is cross-site (another web page has its own origin).

    Args:
        headers: the request's headers, names in lower case.
    """
    if headers.get("sec-fetch-site") == "cross-site":
        return False
    origin = headers.get("origin")
    if origin is None:
        return True
    parts = urlsplit(origin)
    return parts.scheme in ("http", "https") and parts.netloc == headers.get("host", "")


def host_name(host: str) -> str:
    """The name in a Host header, without its port ("[::1]" keeps its brackets)."""
    return host.split("]")[0] + "]" if host.startswith("[") else host.rsplit(":", 1)[0]


def from_this_pc(headers: Mapping[str, str], names: Collection[str] = ()) -> bool:
    """Whether a request on this PC comes from Orion's own pages.

    The host must be a local name, or one of this PC's own (``names``), not another name pointed at this PC (DNS
    rebinding), and the request must be same-site.

    Args:
        headers: the request's headers, names in lower case.
        names: this PC's own names besides the local ones (its hostname, with .local; server.names), in lower case.

    Returns:
        True to answer it.
    """
    name = host_name(headers.get("host", "")).lower()
    return (name in LOCAL_NAMES or name in names) and same_site(headers)


def address_of(scope: Scope) -> Address | None:
    """The client's address (an IPv4 one mapped into IPv6 as itself); None when there is none."""
    client = scope.get("client")
    try:
        address = ipaddress.ip_address(client[0]) if client else None
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def on_lan(address: Address) -> bool:
    """Whether an address is on a local network: private, loopback or link-local."""
    return address.is_private or address.is_loopback or address.is_link_local


def key_of(request: "HTTPConnection") -> str:
    """The pairing key a request carries: a bearer token (an app), else its cookie; "" for none."""
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return request.cookies.get(COOKIE, "")


def security(host: str) -> list[tuple[bytes, bytes]]:
    """The headers every response gets: the CSP, its /ws also named for the host asked for (a device's), and no
    framing.

    Args:
        host: the request's Host header (checked already).
    """
    policy = CSP
    if _PLAIN_HOST.match(host) and host_name(host).lower() not in LOCAL_NAMES:
        policy = policy.replace("connect-src 'self'", f"connect-src 'self' ws://{host} wss://{host}", 1)
    return [(b"content-security-policy", policy.encode()), (b"x-frame-options", b"DENY")]


def own_names() -> tuple[str, str]:
    """This PC's own names: its hostname, and the same with .local (how your network finds it)."""
    hostname = socket.gethostname().lower()
    return hostname, f"{hostname}.local"


def open_to_network(server: "ServerConfig") -> bool:
    """Whether the HUD answers your network too (``server.host`` isn't a loopback address)."""
    return not ipaddress.ip_address(server.host).is_loopback


class Envelope:
    """The security envelope, as plain ASGI (see the module docstring): who is answered, and the CSP, frame and cache
    headers of every response. A request it lets through to /api, /ws or /mcp says who asked in
    ``scope["state"]["peer"]``: "local" (his own pages on this PC), "device" (a paired one) or "unpaired" (one of
    the pairing endpoints)."""

    def __init__(self, app: ASGIApp, *, server: "ServerConfig", pairing: Pairing, names: Collection[str]) -> None:
        """Wrap the app.

        Args:
            app: the HUD's app.
            server: the live server settings (``names``, ``lan_only``: their changes apply at once).
            pairing: the devices paired.
            names: this PC's own names (its hostname, with .local).
        """
        self.app = app
        self.server = server
        self.pairing = pairing
        self.names = frozenset(name.lower() for name in names)

    def _own(self) -> frozenset[str]:
        """Every name of this PC: its own, and server.names."""
        return self.names | {name.lower() for name in self.server.names}

    def _ours(self, host: str) -> bool:
        """Whether a Host header from the network names this PC: an address, or one of its names."""
        name = host_name(host).strip("[]").lower()
        try:
            ipaddress.ip_address(name)
        except ValueError:
            return name in self._own()
        return True

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Check the request, then pass it on with the headers added to its response."""
        from starlette.requests import HTTPConnection

        kind = scope["type"]
        if kind not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        request = HTTPConnection(scope)
        headers = request.headers
        host = headers.get("host", "")
        address = address_of(scope)
        local = address is not None and address.is_loopback
        path = str(scope.get("path", ""))
        if not local and (address is None or (self.server.lan_only and not on_lan(address)) or not self._ours(host)):
            await _refuse(scope, receive, send, host, unpaired=False)  # off the network, or not this PC's name
            return
        if path.startswith(("/api/", "/mcp/")) or path in ("/mcp", "/ws"):
            if local:
                peer: Peer | None = "local" if from_this_pc(headers, self._own()) else None
            elif not same_site(headers):
                peer = None
            elif await asyncio.to_thread(self.pairing.device, key_of(request)) is not None:  # it may note the use
                peer = "device"
            else:
                peer = "unpaired"
            if peer is None or (peer == "unpaired" and (scope.get("method"), path) not in OPEN_TO_ALL):
                await _refuse(scope, receive, send, host, unpaired=peer == "unpaired")
                return
            scope.setdefault("state", {})["peer"] = peer
        if kind != "http":
            await self.app(scope, receive, send)
            return
        assets = path.startswith("/assets/")
        added = security(host)

        async def secured(message: Message) -> None:
            """Add the envelope's headers to the response's start."""
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or ())
                if not any(name.lower() == b"cache-control" for name, _ in headers):
                    headers.append((b"cache-control", _IMMUTABLE if assets and message["status"] == 200 else _NO_CACHE))
                message = {**message, "headers": [*headers, *added]}
            await send(message)

        await self.app(scope, receive, secured)


async def _refuse(scope: Scope, receive: Receive, send: Send, host: str, *, unpaired: bool) -> None:
    """Refuse a request: 403 (or 401 for a device not paired yet), or for a WebSocket a close with its code."""
    from starlette.responses import JSONResponse
    from starlette.websockets import WebSocket

    if scope["type"] == "websocket":
        # Let in only to be told why it is closed: a browser never learns why a handshake was refused, and the HUD
        # would keep trying. Nothing is sent.
        websocket = WebSocket(scope, receive, send)
        await websocket.accept()
        await websocket.close(WS_UNPAIRED if unpaired else WS_NOT_LOCAL)
        return
    response = JSONResponse({"detail": _UNPAIRED if unpaired else _REFUSED}, 401 if unpaired else 403)
    response.raw_headers += [(b"cache-control", _NO_CACHE), *security(host)]
    await response(scope, receive, send)


# ---------------------------------------------------------------------------------------------------- the outbox
class _Slot:
    """One queued event; a coalesced event is replaced inside its slot, keeping its place."""

    __slots__ = ("event", "key")

    def __init__(self, event: Event, key: str) -> None:
        """Hold the event and its coalescing key ("" for none)."""
        self.event = event
        self.key = key


class Outbox:
    """One WebSocket client's queue: events in order, fast-changing ones coalesced (see the module docstring).

    Attributes:
        limit: events waiting before the client counts as too slow.
        overflowed: set once more than ``limit`` events waited; the connection is then closed (4408).
        hands: whether the client shows the hands' live view (only then does it get its frames).
    """

    def __init__(self, limit: int = OUTBOX_LIMIT) -> None:
        """Start empty.

        Args:
            limit: events waiting before the client counts as too slow.
        """
        self.limit = limit
        self.overflowed = False
        self.hands = False
        self._queue: deque[_Slot] = deque()
        self._audio: deque[bytes] = deque()  # his voice for this browser, sent before events (it is timed)
        self._audio_bytes = 0
        self._pending: dict[str, _Slot] = {}
        self._ready = asyncio.Event()

    def push(self, event: Event) -> None:
        """Queue an event (an EventBus sink, on the loop). A newer "latest" event replaces the pending one in place;
        a reply delta joins the pending one only when that is the last in the queue, so text never jumps a tool.

        Args:
            event: the event; it is shared with other clients and never changed here.
        """
        kind = str(event.get("type", ""))
        if kind == "hands" and not self.hands:
            return  # the live view is only for a tab that shows it
        rule = COALESCE.get(kind)
        key = ""
        if rule is not None:
            lane, field = rule
            key = f"{kind}:{event.get(field, '')}" if field else kind
            slot = self._pending.get(key)
            if slot is not None and lane == "latest":
                slot.event = event
                return
            if slot is not None and self._queue and self._queue[-1] is slot:
                slot.event = {**slot.event, "text": f"{slot.event.get('text', '')}{event.get('text', '')}"}
                return
        slot = _Slot(event, key)
        self._queue.append(slot)
        if key:
            self._pending[key] = slot
        if len(self._queue) > self.limit:
            self.overflowed = True
        self._ready.set()

    def push_audio(self, frame: bytes) -> None:
        """Queue a binary frame of his voice (this browser carries the voice). A browser that falls more than
        ``AUDIO_BACKLOG_BYTES`` behind loses the oldest frames: late speech is worse than a gap.

        Args:
            frame: the frame, sent as it is.
        """
        self._audio.append(frame)
        self._audio_bytes += len(frame)
        while self._audio_bytes > AUDIO_BACKLOG_BYTES and len(self._audio) > 1:
            self._audio_bytes -= len(self._audio.popleft())
        self._ready.set()

    async def get(self) -> str | bytes:
        """The next thing to send: a frame of his voice first, else an event as compact JSON text (waits for one).

        Returns:
            The frame, or the encoded event.
        """
        while not self._queue and not self._audio:
            self._ready.clear()
            await self._ready.wait()
        if self._audio:
            frame = self._audio.popleft()
            self._audio_bytes -= len(frame)
            return frame
        slot = self._queue.popleft()
        if slot.key and self._pending.get(slot.key) is slot:
            del self._pending[slot.key]
        return encode(slot.event)

    def __len__(self) -> int:
        """How many events wait."""
        return len(self._queue)


def encode(event: Mapping[str, Any]) -> str:
    """An event as compact JSON text (a value JSON can't hold is written as text), with every secret Orion knows cut out of
    its texts first (field by field, so the JSON stays whole)."""
    return json.dumps(redact.scrubbed(event, redact.redacted), separators=(",", ":"), ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------------------------------- the app
def create_app(rt: "Runtime", pairing: Pairing) -> "FastAPI":
    """The HUD's app over the running Orion (wrap it in the ``Envelope``).

    Args:
        rt: the running Orion.
        pairing: the devices paired.

    Returns:
        The app; ``app.state.gateway`` is the /mcp gateway (``close`` it on shutdown).
    """
    from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel, Field, StrictBool

    from orion.customize import add_customize, for_device
    from orion.llm import openrouter_models
    from orion.llm.claude import CLAUDE_MODELS, claude_status
    from orion.mcp import Gateway

    class Switch(BaseModel):
        """A switch, on or off."""

        enabled: StrictBool

    class Pull(BaseModel):
        """A model to download."""

        name: Annotated[str, Field(min_length=1)]

    class ToolCall(BaseModel):
        """A tool to use from the HUD."""

        server: str
        tool: str
        args: dict[str, Any] = Field(default_factory=dict)

    async def refused(_request: Request, error: Exception) -> Response:
        """Something Orion won't or can't do (``Refused``, another ValueError, a LookupError): 400 (404 for
        something that isn't there), with its reason."""
        return JSONResponse({"detail": str(error)}, 404 if isinstance(error, LookupError) else 400)

    app = FastAPI(title="ORION", docs_url=None, redoc_url=None, openapi_url=None,
                  exception_handlers={ValueError: refused, LookupError: refused})
    controls = rt.controls

    # ------------------------------------------------------------------------------------------------ the HUD
    async def hud() -> Response:
        """Every page of the HUD is this one file: the app shows the page its address names."""
        if not (DIST / "index.html").is_file():
            return HTMLResponse(NOT_BUILT, status_code=503)
        return FileResponse(DIST / "index.html")

    for page in PAGES:
        app.add_api_route(page, hud, methods=["GET"], include_in_schema=False)

    add_pairing(app, pairing, rt.cfg.server, rt.activity)

    # ------------------------------------------------------------------------------------------------ memory
    # Endpoints that read files or the database are plain functions: FastAPI runs them on its worker threads.
    @app.get("/api/neural")
    def neural() -> dict[str, Any]:
        """The memory graph the neural map draws."""
        skills = [(skill.name, skill.description) for skill in rt.skills.all().values()]
        return rt.memory.graph(skills, rt.cfg.user.name or "You")

    @app.get("/api/neural/node/{node_id:path}")
    def neural_node(node_id: str) -> dict[str, Any]:
        """One node of the map with its text, and when and how a memory was learned."""
        node = rt.memory.node(node_id)
        if node is None:
            raise HTTPException(404, f"No memory node {node_id}.")
        return node

    def change_memory(change: Callable[[str], str], node_id: str) -> dict[str, Any]:
        """Forget or confirm one memory of the map (memory that was busy: 409)."""
        try:
            return {"ok": True, "message": change(node_id)}
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from None

    @app.delete("/api/neural/node/{node_id:path}")
    def forget_node(node_id: str) -> dict[str, Any]:
        """Forget one memory of the map for good (an entry, a journal day or a note)."""
        return change_memory(rt.memory.forget, node_id)

    @app.post("/api/neural/node/{node_id}/confirm")
    def confirm_node(node_id: str) -> dict[str, Any]:
        """Trust an unverified memory: it moves to the file it was meant for."""
        return change_memory(rt.memory.confirm, node_id)

    @app.post("/api/memory/forget")
    async def forget_everything() -> dict[str, bool]:
        """Forget everything he knows: he restarts with a blank memory (settings, timers, tasks and skills stay)."""
        rt.forget_everything()
        return {"ok": True}

    @app.post("/api/memory/review")
    async def review() -> dict[str, Any]:
        """Review the conversation for his memory now (once the answer under way is over: his own tool)."""
        result = await rt.hub.call(CORE_SERVER, "memory_review_now", {}, CallContext("hud"))
        return {"ok": not result.startswith("Error"), "result": result}

    # ------------------------------------------------------------------------------------------------ settings
    @app.get("/api/settings")
    async def settings_page(request: Request) -> dict[str, Any]:
        """The settings, how to edit each, and what to choose from."""
        cfg = rt.cfg
        refresh = request.query_params.get("devices") == "refresh"
        devices = await asyncio.to_thread(audio_devices, refresh=refresh)
        tts = rt.voice.tts
        voices = voice_cards(tts.voices()) if tts is not None else []
        config = controls.masked_settings()
        user = config.get("user")
        if isinstance(user, dict) and not user.get("city"):
            user["city"] = rt.memory.home_city()  # the city he learned, until one is set here
        problems = rt.settings.problems
        broken = rt.settings.broken
        return {
            "config": config, "schema": await _schema(rt, voices, devices), "restart": rt.settings.restart_pending(),
            "problems": [asdict(p) for p in problems],
            "broken": asdict(broken) if broken else None,
            "secrets": secret_sources(cfg), "devices": devices, "voices": voices, "loading": tts is None,
            "stt_models": await asyncio.to_thread(stt_options, cfg.models_dir, cfg.stt),
            "claude_models": [{"value": name, "detail": detail} for name, detail in CLAUDE_MODELS.items()],
            "paths": {"config": str(rt.settings.path), "data": str(cfg.data_path), "memory": str(cfg.vault_path)},
        }

    @app.put("/api/settings")
    async def save_settings(patch: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
        """Save a settings change (either shape) and apply it."""
        if (broken := rt.settings.broken) is not None:
            raise HTTPException(409, f"{rt.settings.path.name} has a problem to fix first: {broken.message}")
        return await controls.change_settings(patch, actor="hud")

    @app.post("/api/restart")
    async def restart() -> dict[str, bool]:
        """Restart Orion."""
        controls.exit("restart")
        return {"ok": True}

    @app.post("/api/shutdown")
    async def shutdown() -> dict[str, bool]:
        """Shut Orion down."""
        log.info("shutdown asked for from the HUD")
        controls.exit("stop")
        return {"ok": True}

    # ------------------------------------------------------------------------------------------------ models
    @app.get("/api/models")
    async def models() -> dict[str, Any]:
        """The brain and the ears as they run, with the models installed."""
        if rt.brain_error and rt.cfg.brain.provider == "claude":  # signed in to Claude Code since? Then try again
            status = await claude_status(fresh=True)
            if status.installed and status.logged_in:
                rt.supervisor.spawn(rt.retry_brain(), name="retry the brain")
        installed, error = await rt.ollama.models()
        return {
            "llm": {"embed": rt.embedder.id, "installed": installed, "error": error, "brain_error": rt.brain_error,
                    "ollama": {"installed": rt.ollama.installed(), "running": not error}},
            "stt": rt.voice.info["stt"] or {},
            "gpu": await asyncio.to_thread(rt.vitals.gpu),
        }

    @app.post("/api/models/pull")
    async def pull_model(body: Pull) -> dict[str, bool]:
        """Download a model for the brain on this PC (``model_pull`` events say how far it got)."""
        controls.pull_model(body.name.strip())
        return {"ok": True}

    @app.delete("/api/models/llm/{name:path}")
    async def delete_model(name: str) -> dict[str, bool]:
        """Delete a model of the brain on this PC."""
        try:
            await controls.delete_model(name)
        except OllamaError as e:
            raise HTTPException(404 if e.status == 404 else 502, f"Ollama couldn't delete {name}: {e}") from None
        return {"ok": True}

    @app.get("/api/openrouter/models")
    async def openrouter() -> dict[str, Any]:
        """OpenRouter's models that can use tools, free ones first (cached for ten minutes)."""
        try:
            listed = await openrouter_models(rt.cfg.openrouter.models_url)
        except Exception as e:
            log.warning("OpenRouter's model list failed: %s", e, exc_info=True)
            raise HTTPException(502, f"Could not reach OpenRouter: {e}") from None
        return {"models": listed, "current": rt.cfg.openrouter.model}

    @app.get("/api/claude")
    async def claude(refresh: bool = False) -> dict[str, Any]:
        """Whether Claude Code is installed and signed in, never who the account is."""
        return (await claude_status(fresh=refresh)).as_dict()

    @app.post("/api/voice/preview")
    async def preview_voice(body: Annotated[dict[str, Any], Body()]) -> Response:
        """A sample of a voice, as WAV."""
        tts = rt.voice.tts
        if tts is None:
            raise HTTPException(503, "The voice is still loading.")
        cfg = rt.cfg
        voice = str(body.get("voice") or cfg.tts.voice)
        if voice not in tts.voices():
            raise HTTPException(400, f"Unknown voice: {voice}")
        text = str(body.get("text") or cfg.prompts.voice_preview.format(honorific=cfg.user.honorific))
        speed = body.get("speed")
        try:
            wav = await rt.voice.preview(text[:PREVIEW_CHARS], voice,
                                         float(speed) if isinstance(speed, int | float) else None)
        except Exception as e:
            log.warning("a voice sample failed", exc_info=True)
            raise HTTPException(502, f"Could not load that voice: {e}") from None
        return Response(wav, media_type="audio/wav")

    # ------------------------------------------------------------------------------------------------ customize
    add_customize(app, rt)  # connectors, skills, plugins and the sign-in's way back (orion/customize.py)

    # ------------------------------------------------------------------------------------------------ jobs
    # The same service as his jobs tools (orion/jobs.py): a bad schedule is a 400 with its reason, a missing job 404.
    class JobBody(BaseModel):
        """A new job."""

        name: str
        schedule: str
        prompt: str
        deliver: list[str] | None = None
        enabled: StrictBool = True

    class JobPatch(BaseModel):
        """A job's changes (only what is given)."""

        name: str | None = None
        schedule: str | None = None
        prompt: str | None = None
        deliver: list[str] | None = None
        enabled: StrictBool | None = None

    class Schedule(BaseModel):
        """A schedule to explain."""

        schedule: str

    @app.get("/api/jobs")
    def list_jobs() -> list[dict[str, Any]]:
        """Every scheduled job, with its schedule in English."""
        return rt.jobs.all()

    @app.post("/api/jobs")
    def create_job(body: JobBody) -> dict[str, Any]:
        """Make a job."""
        return rt.jobs.create(body.name, body.schedule, body.prompt, body.deliver, enabled=body.enabled)

    @app.post("/api/jobs/explain")
    def explain_job(body: Schedule) -> dict[str, Any]:
        """A schedule in English, and when it fires next (the editor checks as you type)."""
        from orion.jobs import explain, next_run, parse_schedule

        schedule = parse_schedule(body.schedule)
        return {"schedule": schedule, "explain": explain(schedule), "next_run": next_run(schedule, time.time())}

    @app.put("/api/jobs/{job_id}")
    def update_job(job_id: int, body: JobPatch) -> dict[str, Any]:
        """Change a job."""
        return rt.jobs.update(job_id, name=body.name, schedule=body.schedule, prompt=body.prompt,
                              deliver=body.deliver, enabled=body.enabled)

    @app.delete("/api/jobs/{job_id}")
    def delete_job(job_id: int) -> dict[str, bool]:
        """Delete a job."""
        rt.jobs.delete(job_id)
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/run")
    def run_job_now(job_id: int) -> dict[str, bool]:
        """Run a job now (its result comes as usual)."""
        rt.run_job(rt.jobs.find(job_id))
        return {"ok": True}

    @app.get("/api/calendar")
    def calendar(days: int = 7) -> dict[str, Any]:
        """The agenda from today over some days, for the HUD's calendar card (the calendar tools read the same)."""
        from orion.tools.calendar import SOURCES, window

        cfg = rt.cfg.calendar
        first, last = window("today", max(1, min(days, 31)))
        events, problems = SOURCES.agenda(cfg, first, last) if cfg.sources else ([], {})
        return {"sources": [{"name": name, "ok": name not in problems, "error": problems.get(name, "")}
                            for name in cfg.sources],
                "events": [event.as_dict() for event in events]}

    @app.delete("/api/calendar/sources/{name}")
    async def remove_calendar(name: str) -> dict[str, Any]:
        """Remove a calendar source."""
        return await rt.remove_calendar(name)

    @app.get("/api/watchers")
    def watchers() -> dict[str, Any]:
        """The watchers: each rule (its limit, whether this machine has its reading, the last reading) and the warnings
        on now."""
        stats = rt.last_stats
        sensors: dict[str, Any] = {}
        if stats is not None:
            battery, gpu = stats.get("battery"), stats.get("gpu")
            if battery is not None:
                sensors["battery"] = {"percent": battery["percent"], "plugged": battery["plugged"]}
            temperatures = {k: v for k, v in (("cpu", stats.get("cpu_temp")), ("gpu", gpu["temp"] if gpu else None))
                            if v is not None}
            if temperatures:
                sensors["temperatures"] = temperatures
            sensors["disk"] = {"percent": stats["disk"], "free_gb": stats["disk_free"]}
            sensors["memory"] = {"percent": stats["ram"]}
        rules = [{**rule, "detail": (f"warns {'above' if rule['above'] else 'below'} {rule['limit']:g}{rule['unit']}"
                                     if rule["enabled"] else "off") if rule["available"] else
                  "this machine has no such reading"} for rule in rt.watchers.status()]
        return {"sensors": sensors, "rules": rules, "alerts": [alert.as_dict() for alert in rt.watchers.active()]}

    # ------------------------------------------------------------------------------------------------ timings
    @app.get("/api/timings")
    def timings(limit: int = 50) -> dict[str, Any]:
        """The latest turns' timings, newest first, and each brain's median and 95th percentile of every stage over
        the latest ``STATS_TURNS`` turns."""
        return dict(report(rt.memory.store.timings(STATS_TURNS), limit))

    # ------------------------------------------------------------------------------------------------ backups
    @app.get("/api/backups")
    async def list_backups() -> dict[str, Any]:
        """The backups (archives, and restic's snapshots when restic is set up), the schedule and what is kept."""
        return await rt.backups.listing()

    @app.post("/api/backups")
    async def back_up_now() -> dict[str, str]:
        """Back up now: the archive's name comes back at once and the ``backup`` events say how it went (a backup
        already under way is the one named)."""
        rt.backups.start("hud")
        return {"name": rt.backups.name}

    # ------------------------------------------------------------------------------------------------ documents
    @app.get("/api/documents")
    async def documents() -> dict[str, Any]:
        """How the folders of documents are doing: for each its documents, pieces, those that could not be read and
        whether it is still being read; and the last problem (the ``documents`` events have the same shape)."""
        return await asyncio.to_thread(rt.documents.snapshot)

    # ------------------------------------------------------------------------------------------------ tools
    @app.get("/api/tools")
    async def tools() -> list[Any]:
        """Every MCP server with its tools (tokens hidden)."""
        return list(rt.hub.status())

    @app.post("/api/tools/call")
    async def call_tool(body: ToolCall) -> dict[str, Any]:
        """Use a tool directly from the HUD, with the same access he has."""
        started = time.monotonic()
        result = await rt.hub.call(body.server, body.tool, body.args, CallContext("hud"))
        return {"ok": not result.startswith("Error"), "result": result,
                "ms": round((time.monotonic() - started) * 1000)}

    @app.put("/api/tools/{server}/{tool}")
    async def switch_tool(server: str, tool: str, body: Switch) -> dict[str, Any]:
        """Switch one tool on or off."""
        return await controls.switch_tool(server, tool, body.enabled, actor="hud")

    @app.put("/api/mcp/{name}/enabled")
    async def switch_server(name: str, body: Switch) -> dict[str, Any]:
        """Switch a whole server on or off (its switched-off tools, env and headers stay as they are)."""
        return await controls.switch_server(name, body.enabled, actor="hud")

    @app.put("/api/mcp/{name}")
    async def save_server(name: str, body: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
        """Add a server or change its settings."""
        return await controls.save_server(name, body, actor="hud")

    @app.delete("/api/mcp/{name}")
    async def remove_server(name: str) -> dict[str, Any]:
        """Remove an added server."""
        return await controls.remove_server(name, actor="hud")

    @app.post("/api/mcp/{name}/restart")
    async def restart_server(name: str) -> dict[str, bool]:
        """Connect a server again."""
        await controls.restart_server(name, actor="hud")
        return {"ok": True}

    # ------------------------------------------------------------------------------------------------ gestures
    @app.get("/api/gestures")
    async def gestures() -> dict[str, Any]:
        """The gesture remotes, what each gesture does, and what it could do (for the Gestures page)."""
        return await controls.gestures_page()

    @app.put("/api/gestures/{server}")
    async def save_gestures(server: str, body: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
        """Save what each of a remote's gestures does (every gesture: one left out does nothing)."""
        return await controls.save_gestures(server, body.get("bindings"), actor="hud")

    @app.delete("/api/gestures/{server}")
    async def reset_gestures(server: str) -> dict[str, Any]:
        """A remote's gestures do the defaults again."""
        return await controls.reset_gestures(server, actor="hud")

    # ------------------------------------------------------------------------------------------------ security
    class SecretBody(BaseModel):
        """A secret's value."""

        value: Annotated[str, Field(min_length=1)]

    class ChannelBody(BaseModel):
        """What a channel may never do, and what it always asks about."""

        never: list[str]
        confirm: list[str]

    def listed(text: str) -> list[str]:
        """A comma-separated query value as a list."""
        return [part for part in (piece.strip() for piece in text.split(",")) if part]

    @app.get("/api/activity")
    def activity_page(before: int | None = None, limit: int = 50, kind: str = "", source: str = "",
                      q: str = "") -> dict[str, Any]:
        """What he did and who allowed it, newest first, a page at a time (``next``: pass it as ``before``)."""
        limit = max(1, min(limit, 200))
        rows = rt.activity.recent(before=before, limit=limit, kinds=listed(kind), sources=listed(source), text=q)
        return {"items": rows, "next": rows[-1]["id"] if len(rows) == limit else None}

    @app.get("/api/secrets")
    def secrets_page() -> dict[str, Any]:
        """The vault: whether it works, and each secret's name, whether it is set and what uses it (never a value)."""
        return rt.secrets_view()

    @app.put("/api/secrets/{name}")
    def put_secret(name: str, body: SecretBody) -> dict[str, bool]:
        """Keep a secret in the vault (a new value for a name replaces the old one)."""
        rt.keep_secret(name, body.value.strip())
        return {"ok": True}

    @app.delete("/api/secrets/{name}")
    def delete_secret(name: str) -> dict[str, bool]:
        """Forget a secret."""
        rt.forget_secret(name)
        return {"ok": True}

    @app.get("/api/safety")
    async def safety_page() -> dict[str, Any]:
        """What each channel may never do and always asks about, the sandbox, and what a limit can name."""
        await asyncio.to_thread(sandbox.status)  # (looked at again after a minute: a trial sandbox starts)
        return rt.safety_view()

    @app.put("/api/safety/channels/{channel}")
    async def put_channel(channel: str, body: ChannelBody) -> dict[str, Any]:
        """Set what a channel may never do and always asks about."""
        return await rt.set_channel(channel, body.never, body.confirm)


    # ------------------------------------------------------------------------------------------------ spend, benchmarks
    add_brains(app, rt)

    # ------------------------------------------------------------------------------------------------ /mcp
    gateway = Gateway(rt.hub)
    app.state.gateway = gateway
    app.add_route("/mcp", gateway, include_in_schema=False)  # his own tools for other AI apps on this PC

    # ------------------------------------------------------------------------------------------------ /ws
    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        """The live event stream: ``init`` first, then every event; the HUD's messages back."""
        await websocket.accept()
        outbox = Outbox()
        here = websocket.scope.get("state", {}).get("peer") == "local"
        # first: nothing published from here on is missed (a device hears the connectors as ones it can't sign in to)
        unsubscribe = rt.bus.subscribe(outbox.push if here else lambda event: outbox.push(for_device(event)))
        loop = asyncio.get_running_loop()
        surface = rt.surfaces.add(
            local=here, send=outbox.push,
            send_audio=lambda frame: loop.call_soon_threadsafe(outbox.push_audio, frame))
        rt.hud_connected(True)

        async def deliver() -> None:
            """Send queued frames and events as they come; a client that fell too far behind is closed (it
            reconnects)."""
            with contextlib.suppress(WebSocketDisconnect, RuntimeError):
                while True:
                    item = await outbox.get()
                    if outbox.overflowed:
                        await websocket.close(code=WS_TOO_SLOW)
                        return
                    if isinstance(item, bytes):
                        await websocket.send_bytes(item)
                    else:
                        await websocket.send_text(item)

        sender: asyncio.Task[None] | None = None
        try:
            await websocket.send_text(encode({"type": "init", "ts": time.time(), "surface": surface.id,
                                              **rt.snapshot()}))
            sender = asyncio.create_task(deliver(), name="ws-send")
            while True:
                received = await websocket.receive()
                if received.get("type") == "websocket.disconnect":
                    break
                if data := received.get("bytes"):
                    if data[0] == MIC_INT16_KIND and (len(data) - 1) % 2 == 0:
                        samples = np.frombuffer(data, dtype="<i2", offset=1).astype(np.float32) / 32768.0
                        rt.voice.feed(surface.id, samples)
                    elif data[0] == MIC_FRAME_KIND and (len(data) - 1) % 4 == 0:
                        rt.voice.feed(surface.id, np.frombuffer(data, dtype="<f4", offset=1))
                    continue
                try:
                    message = json.loads(received.get("text") or "")
                except ValueError:
                    continue  # not JSON: ignored rather than dropping the connection
                if isinstance(message, dict) and message.get("type") == "hands_view":
                    outbox.hands = message.get("on") is True
                    rt.hands_view(id(outbox), outbox.hands, picture=message.get("picture") is True)
                elif isinstance(message, dict):
                    _handle(rt, message, surface)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            unsubscribe()
            rt.surfaces.remove(surface.id)
            if outbox.hands:
                rt.hands_view(id(outbox), False)
            rt.hud_connected(False)
            if sender is not None:
                sender.cancel()

    # Last: the HUD's own files (scripts, styles, fonts, icon) answer whatever address nothing above took.
    app.mount("/", StaticFiles(directory=DIST, check_dir=False), name="hud")
    return app


def add_pairing(app: "FastAPI", pairing: Pairing, server: "ServerConfig", activity: "Activity | None" = None) -> None:
    """The pairing endpoints: whether this device may use the HUD; a code to show on this PC; pairing with it (open to
    a device not paired yet: the envelope lets these two through); unpairing (on this PC alone). They are plain
    functions: FastAPI runs them on its worker threads, where devices.json is written.

    Args:
        app: the HUD's app.
        pairing: the devices paired.
        server: the live server settings.
        activity: the log pairings, refused codes and unpairings are recorded in; None: none is kept.
    """
    from fastapi import Body, HTTPException, Request
    from fastapi.responses import JSONResponse, Response

    lan_name = own_names()[1]

    def record(title: str, outcome: str, actor: str = "") -> None:
        """One entry of the activity log (a login: a device's, or a try that failed)."""
        if activity is not None:
            activity.record("login", source="hud", title=title, outcome=outcome, actor=actor)

    def peer(request: Request) -> str:
        """Who asks (the envelope says): "local", "device" or "unpaired"."""
        return str(request.scope.get("state", {}).get("peer", ""))

    def on_this_pc(request: Request) -> None:
        """Only his own pages on this PC pair and unpair devices.

        Raises:
            HTTPException: a device asked.
        """
        if peer(request) != "local":
            raise HTTPException(403, "Devices are paired and unpaired on the PC itself: Settings, General, Devices.")

    @app.get("/api/pairing")
    def pairing_state(request: Request) -> dict[str, Any]:
        """Whether this device may use the HUD; on this PC also the devices paired and when the code waiting ends."""
        local = peer(request) == "local"
        return {"local": local, "paired": peer(request) in ("local", "device"), "open": open_to_network(server),
                "tls": bool(server.tls_cert), "url": hud_url(server, lan_name),
                "devices": pairing.devices() if local else [], "code_ends": pairing.waiting() if local else None}

    @app.post("/api/pairing/code")
    def pairing_code(request: Request) -> dict[str, Any]:
        """A new pairing code, to show on this PC (any code before stops working)."""
        on_this_pc(request)
        code, ends = pairing.start()
        return {"code": code, "ends": ends}

    @app.post("/api/pairing")
    def pair(body: Annotated[dict[str, Any], Body()]) -> Response:
        """Pair this device with the code shown on the PC: its key comes back as a cookie, and to an app that asks
        (``app: true``) in the answer, to send as a bearer token."""
        name = str(body.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "Give this device a name, to know it by on the PC.")
        done = pairing.pair(str(body.get("code") or ""), name)
        if done is None:
            record(f"A pairing code from “{name[:60]}” was refused", "denied", name[:60])
            raise HTTPException(403, "That isn't the code on the PC, or it stopped working: show a new one there.")
        key, device = done
        record(f"Paired “{device.name}”", "ok", device.name)
        answer: dict[str, Any] = {"paired": True, "id": device.id, "name": device.name}
        if body.get("app") is True:
            answer["key"] = key
        response = JSONResponse(answer)
        response.set_cookie(COOKIE, key, max_age=COOKIE_MAX_AGE_S, path="/", httponly=True, samesite="strict",
                            secure=bool(server.tls_cert))
        return response

    @app.delete("/api/pairing/devices")
    def unpair_all(request: Request) -> dict[str, Any]:
        """Unpair every device."""
        on_this_pc(request)
        gone = pairing.unpair(None)
        record(f"Unpaired every device ({gone})", "ok", "you, at the PC")
        return {"unpaired": gone}

    @app.delete("/api/pairing/devices/{device_id}")
    def unpair(request: Request, device_id: str) -> dict[str, Any]:
        """Unpair one device."""
        on_this_pc(request)
        gone = pairing.unpair(device_id)
        record("Unpaired a device", "ok" if gone else "failed", "you, at the PC")
        return {"unpaired": gone}


def add_brains(app: "FastAPI", rt: "Runtime") -> None:
    """What OpenRouter cost and the benchmarks of the brains, over REST: the same services as his own tools
    (``orion.tools.brains``). A benchmark that makes paid requests (your Claude plan, OpenRouter) says its plan first:
    asking to start it without ``confirm`` is refused (409) with the plan in the answer, so nothing is spent before
    the user has seen how many requests.

    Args:
        app: the HUD's app.
        rt: the running Orion (its ``spend`` and ``bench``).
    """
    from fastapi import HTTPException, Query
    from fastapi.responses import JSONResponse, Response
    from pydantic import BaseModel, Field, StrictBool

    from orion.bench import BenchBusy, plan_text

    class Run(BaseModel):
        """A benchmark to plan or start: the brains ("claude", "ollama:qwen3.5:9b"; none: the one he thinks with),
        the suite (None: ``bench.suite``), how many requests at most, and, for one that costs, the user's yes."""

        brains: list[str] = []
        suite: Literal["builtin", "history"] | None = None
        limit: Annotated[int, Field(ge=1)] | None = None
        confirm: StrictBool = False

    @app.get("/api/spend")
    async def spend(days: Annotated[int, Query(ge=1, le=365)] = 30) -> dict[str, Any]:
        """What OpenRouter cost today against the budget and over the last days, and the key's own limits."""
        report = await rt.spend.report(days)
        key = await rt.spend.key_info()
        return {**report, "key": key.as_dict() if key is not None else None}

    @app.get("/api/bench")
    async def bench_runs() -> dict[str, Any]:
        """The latest benchmark runs, newest first."""
        return {"items": await rt.bench.runs()}

    @app.get("/api/bench/{run_id}")
    async def bench_run(run_id: int) -> dict[str, Any]:
        """One benchmark run."""
        found = await rt.bench.run(run_id)
        if found is None:
            raise HTTPException(404, f"There's no benchmark #{run_id}.")
        return found

    @app.post("/api/bench/plan")
    async def bench_plan(body: Run) -> dict[str, Any]:
        """What a run would do and how many requests it would make, where they are paid: nothing starts."""
        view = (await rt.bench.plan(body.brains, body.suite, body.limit)).view()
        return {**view, "text": plan_text(view)}

    @app.post("/api/bench", response_model=None)
    async def bench_start(body: Run) -> Response | dict[str, Any]:
        """Start a benchmark in the background: its progress comes as ``bench`` events."""
        plan = await rt.bench.plan(body.brains, body.suite, body.limit)
        if any(brain.paid and brain.ready for brain in plan.brains) and not body.confirm:
            view = plan.view()
            return JSONResponse({"detail": f"{plan_text(view)}\nSend confirm: true to start it.", "plan": view}, 409)
        try:
            run = await rt.bench.start(body.brains, body.suite, body.limit, source="hud")
        except BenchBusy as e:
            raise HTTPException(409, str(e)) from None
        return {"id": run["id"]}

    @app.post("/api/bench/{run_id}/stop")
    async def bench_stop(run_id: int) -> dict[str, bool]:
        """Stop a benchmark under way (what it scored so far is kept)."""
        if not rt.bench.stop(run_id):
            raise HTTPException(404, f"Benchmark #{run_id} isn't running.")
        return {"ok": True}


def _handle(rt: "Runtime", message: Mapping[str, Any], surface: "Surface") -> None:
    """A message from a HUD: chat (with pictures), VOICE on or off and MIC (his own tools), STOP, NEW CHAT (at once,
    cutting the answer under way short), the answer to a question before a risky action, and what its device offers
    and does (``orion.surfaces``: hello, sharing, taking the voice, talking, answering a request); anything else is
    ignored.

    Args:
        rt: the running Orion.
        message: the message.
        surface: the HUD it came from.
    """
    kind = message.get("type")
    if kind not in ("answer", "sharing", "hello"):
        rt.surfaces.touch(surface.id)
    if kind == "chat":
        pictures = _pictures(rt, message.get("images"))
        rt.turns.submit(str(message.get("text") or "")[:rt.cfg.hud.max_message_chars], "hud", device=surface.id,
                        pictures=pictures)
    elif kind in ("voice", "mute"):
        tool, args = ("spoken_replies", {"on": bool(message.get("on"))}) if kind == "voice" else (
            "microphone", {"muted": bool(message.get("value"))})
        rt.supervisor.spawn(rt.hub.call(CORE_SERVER, tool, args, CallContext("hud")), name=f"hud {tool}")
    elif kind == "stop":
        rt.turns.interrupt()
        rt.voice.stand_by()
    elif kind == "reset":
        rt.turns.new_conversation()
    elif kind == "confirm":
        rt.confirmer.answer(str(message.get("id") or ""), message.get("yes") is True, by="the HUD")
    elif kind == "hello":
        rt.surfaces.hello(surface.id, message.get("device"), message.get("caps"))
    elif kind == "sharing":
        rt.surfaces.sharing(surface.id, message.get("what"))
    elif kind == "answer":
        rt.surfaces.answered(surface.id, dict(message))
    elif kind == "voice_take":
        rt.supervisor.spawn(rt.take_voice(surface, message.get("on") is True), name="take the voice")
    elif kind == "talk":
        rt.voice.talk(surface.id)


def _pictures(rt: "Runtime", sent: object) -> list["Image"]:
    """The pictures a HUD sent with a message (data: URLs), prepared for the models; unreadable ones are left out,
    and at most ``hud.max_pictures``."""
    if not isinstance(sent, list):
        return []
    eyes = rt.cfg.eyes
    out: list[Image] = []
    for item in sent[:rt.cfg.hud.max_pictures]:
        if not isinstance(item, str):
            continue
        try:
            out.append(images.from_base64(item, label="a picture you sent", max_side=eyes.image_max_side,
                                          quality=eyes.image_quality))
        except ValueError:
            log.info("a picture sent from the HUD couldn't be read")
    return out


async def _schema(rt: "Runtime", voices: list[Any], devices: Mapping[str, list[str]]) -> dict[str, Any]:
    """The Settings pages' schema with the choices of the moment filled in (voices, devices, models)."""
    from orion.llm.claude import CLAUDE_MODELS

    cfg = rt.cfg
    schema: dict[str, Any] = dict(hud_schema())
    ollama: list[dict[str, Any]] | None = None
    for key, spec in schema.items():
        hud = meta(key).hud
        source = hud.choices_from if hud is not None else ""
        if not source:
            continue
        if source == "voices":
            spec["choices"] = [{"value": v["id"], "label": f"{v['name']} ({v['accent']} {v['gender']})"}
                               for v in voices]
        elif source in ("devices.input", "devices.output"):
            spec["choices"] = [{"value": name, "label": name} for name in devices[source.split(".")[1]]]
        elif source == "stt_models":
            spec["choices"] = [{"value": name, "label": f"{name}: {stt_label(name, cfg.stt)}"} for name in STT_MODELS]
        elif source == "claude_models":
            spec["choices"] = [{"value": name, "label": f"{name.capitalize()}: {detail}"}
                               for name, detail in CLAUDE_MODELS.items()]
        elif source == "ollama_models":
            if ollama is None:
                installed, _ = await rt.ollama.models()
                ollama = [{"value": m["name"], "label": " ".join(filter(None, (m["name"], m["params"], m["quant"])))}
                          for m in installed]
            spec["choices"] = ollama
        elif source == "openrouter_models":
            spec["choices"] = [{"value": cfg.openrouter.model, "label": cfg.openrouter.model}]
    return schema


# ---------------------------------------------------------------------------------------------------- serving
def hud_url(server: "ServerConfig", name: str = "localhost") -> str:
    """The HUD's address: https:// with server.tls_cert, else http://; on this PC, or at another of its names."""
    return f"{'https' if server.tls_cert else 'http'}://{name}:{server.port}"


def _log_to_file(cfg: "Config") -> None:
    """Log to data/logs/orion.log as well (rotated), quieting the chatty libraries."""
    from logging.handlers import RotatingFileHandler

    folder = cfg.logs_path
    folder.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(folder / "orion.log", maxBytes=cfg.logging.file_max_bytes,
                                  backupCount=cfg.logging.file_backups, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    for name in cfg.logging.quiet:
        logging.getLogger(name).setLevel(logging.WARNING)


async def serve(*, microphone: bool, banner: str = "") -> "ExitAction | None":
    """Run Orion until he is stopped: settings, the Runtime, the HUD at once, the models in the background.

    Args:
        microphone: whether to open the microphone at all.
        banner: printed once the HUD's address is known.

    Returns:
        "restart" when a restart was asked for, "stop" for a shutdown from the HUD or a tool, None for a signal.
    """
    import uvicorn

    from orion.runtime import Runtime

    settings = Settings.open(None, role="main")
    vault, vault_error = await asyncio.to_thread(vaulting.start, settings)  # (before the file: it keeps no secret)
    await asyncio.to_thread(settings.sync_file)
    if vault is not None:
        await asyncio.to_thread(vaulting.scrub_backup, settings.path, vault)
    cfg = settings.cfg
    _log_to_file(cfg)
    rt = Runtime(settings, microphone=microphone, vault=vault, vault_error=vault_error)
    rt.build()
    pairing = Pairing(cfg.data_path, cfg.server)
    app = create_app(rt, pairing)
    names = own_names()

    class HudServer(uvicorn.Server):
        """uvicorn, with Orion's own signal handling and readiness reports to systemd."""

        @contextlib.contextmanager
        def capture_signals(self) -> Iterator[None]:
            """Leave the signals to ``serve``."""
            yield

        async def startup(self, sockets: Any = None) -> None:
            """Start listening, then tell systemd the HUD answers (the models load from here on)."""
            await super().startup(sockets)
            if self.started:
                sd_notify("READY=1", "STATUS=loading models")

    tls = {"ssl_certfile": cfg.server.tls_cert, "ssl_keyfile": cfg.server.tls_key} if cfg.server.tls_cert else {}
    envelope = Envelope(app, server=cfg.server, pairing=pairing, names=names)
    server = HudServer(uvicorn.Config(envelope, host=cfg.server.host, port=cfg.server.port, log_level="warning",
                                      ws_ping_interval=WS_PING_S, lifespan="off", proxy_headers=False, **tls))

    def stop_serving() -> None:
        """Stop the server; ``serve`` then stops Orion."""
        server.should_exit = True

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_serving)
    rt.on_exit = stop_serving
    if banner:
        network = (f"; from your network {hud_url(cfg.server, names[1])} (pair each device in Settings, General)"
                   if open_to_network(cfg.server) else "")
        print(f"{banner}  HUD: {hud_url(cfg.server)}{network}\n", flush=True)
    rt.supervisor.spawn(rt.start(), name="startup")
    try:
        await server.serve()
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
        with contextlib.suppress(Exception):
            await app.state.gateway.close()
        await rt.stop()
    return rt.exit_action
