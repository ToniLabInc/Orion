"""The host system, implemented once for the CLI, the runtime and the tools process.

- Programs: `which` looks in ~/.local/bin first (install.sh puts ollama, claude and orion there, and a service's
  PATH may lack it), then PATH; `local_path` gives child processes the same PATH.
- systemd: readiness (`sd_notify`), `systemctl --user` (`systemctl`, `asystemctl`, `unit_active`), and keeping the
  installed units and the app-menu launcher current (`refresh_units`, with ollama.service from `OllamaUnit`).
- Desktop notifications (`notify`), which never wait.
- The local Ollama service and its models over its HTTP API (`Ollama`).
- The PC's vital signs for the HUD and the system_status tool (`Vitals`).

asyncio (55 ms to import), httpx, psutil and NVML load on first use. Values the settings own (Ollama's address and
unit options, timeouts, the disk to report) come in as arguments.
"""

from __future__ import annotations

import contextlib
import functools
import ipaddress
import json
import logging
import os
import shutil
import socket
import subprocess
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, TypedDict
from urllib.parse import urlsplit

from orion.base import write_atomic

if TYPE_CHECKING:
    import httpx

    from orion.base import Publish
    from orion.config import Config

log = logging.getLogger("orion.service")

type Runner = Callable[..., subprocess.CompletedProcess[str]]  # subprocess.run, or a fake in tests
type Spawner = Callable[..., object]  # subprocess.Popen, or a fake in tests
type KvCacheType = Literal["f16", "q8_0", "q4_0"]

UNDER_SERVICE: Final = os.environ.get("ORION_SERVICE") == "1"  # started by systemd (orion.service sets it)
ORION_UNIT: Final = "orion.service"
OLLAMA_UNIT: Final = "ollama.service"
LAUNCHER: Final = "orion-hud.desktop"  # the app-menu entry `orion setup` writes (--uninstall removes it)
SYSTEMCTL_TIMEOUT_S: Final = 10.0  # systemctl --user answers in milliseconds; a hang means systemd is in trouble
OLLAMA_CONNECT_S: Final = 1.0  # a local Ollama accepts a connection at once, or it isn't running
OLLAMA_POLL_S: Final = 0.25  # how often a starting Ollama is asked whether it answers yet
NVML_RETRY_S: Final = 60.0  # a GPU library that failed to open is tried again after this long, not never
MIB: Final = 2**20  # GPU memory is shown in MiB, as nvidia-smi counts it
GB: Final = 1e9  # RAM and disk are shown in GB
CPU_SENSORS: Final = ("coretemp", "k10temp", "zenpower", "acpitz")  # Intel, AMD (two drivers), the ACPI zone
NOT_INSTALLED: Final = "THIS PC needs Ollama: run ./install.sh --local-brain"
NOT_RUNNING: Final = "Ollama isn't running: it starts when THIS PC is his brain."

# His own unit and the app-menu entry, with @ROOT@ for the repository folder (`refresh_units` fills it in).
ORION_TEMPLATE: Final = """\
# Started and stopped manually: `orion start` / `orion stop` (or the Orion app menu entry).
# To launch on login instead:  systemctl --user enable orion
[Unit]
Description=ORION personal assistant
Documentation=file://@ROOT@/README.md
After=pipewire.service graphical-session.target
PartOf=graphical-session.target

[Service]
# He says when his HUD answers (a second or two), so `systemctl start` returns then; the models load after, shown
# on the HUD's welcome screen. A first start may download a speech model, so it isn't timed out.
Type=notify
TimeoutStartSec=infinity
WorkingDirectory=@ROOT@
ExecStart=@ROOT@/.venv/bin/orion run
Environment=PYTHONUNBUFFERED=1
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=ORION_SERVICE=1
# Idle OpenMP threads sleep instead of spinning on a core.
Environment=OMP_WAIT_POLICY=PASSIVE
Restart=on-failure
RestartSec=5

[Install]
WantedBy=graphical-session.target
"""

LAUNCHER_TEMPLATE: Final = """\
[Desktop Entry]
Type=Application
Name=ORION
GenericName=Personal AI Assistant
Comment=Start ORION and open his HUD
Exec=@ROOT@/.venv/bin/orion start --open
Icon=@ROOT@/orion/hud/icon.svg
Terminal=false
Categories=Utility;
Keywords=orion;assistant;ai;
Actions=stop;

[Desktop Action stop]
Name=Stop ORION
Exec=@ROOT@/.venv/bin/orion stop
"""

# The unit of C §10 with its placeholders. OLLAMA_MODELS is quoted so that a data_dir with spaces stays one value.
OLLAMA_TEMPLATE: Final = """\
# Runs only for the local brain, while Orion runs. Rendered from data/config.yaml (ollama.*, data_dir) by
# `orion setup` and whenever Orion starts Ollama; edit the settings, not the installed copy.
[Unit]
Description=Ollama local LLM server (Orion brain)
PartOf=orion.service

[Service]
ExecStart=%h/.local/bin/ollama serve
Environment=OLLAMA_HOST=@HOST@
Environment=OLLAMA_FLASH_ATTENTION=@FLASH_ATTENTION@
Environment=OLLAMA_KV_CACHE_TYPE=@KV_CACHE_TYPE@
Environment=OLLAMA_NUM_PARALLEL=@NUM_PARALLEL@
Environment=OLLAMA_MAX_LOADED_MODELS=@MAX_LOADED_MODELS@
Environment="OLLAMA_MODELS=@MODELS_DIR@"
Restart=on-failure
RestartSec=3
"""


# ---------------------------------------------------------------------------------------------------- programs
def local_path() -> str:
    """PATH with ~/.local/bin once, in front: for program lookups and child processes (external MCP servers).

    Returns:
        The PATH string.
    """
    local = str(Path.home() / ".local" / "bin")
    rest = [p for p in os.environ.get("PATH", os.defpath).split(os.pathsep) if p and p != local]
    return os.pathsep.join([local, *rest])


@functools.cache
def which(name: str) -> str | None:
    """Where a program is, ~/.local/bin first. Cached: call `which.cache_clear()` after installing something.

    Args:
        name: The program, such as "ollama", "claude" or "notify-send".

    Returns:
        Its full path, or None when it is not installed.
    """
    return shutil.which(name, path=local_path())


# ---------------------------------------------------------------------------------------------------- systemd
def sd_notify(*states: str) -> None:
    """Tell systemd how Orion is doing (orion.service is Type=notify); nothing outside systemd.

    A socket error is logged, never raised: readiness is a courtesy to `systemctl start`, not a reason to fail.

    Args:
        states: Assignments such as "READY=1", "STATUS=Loading speech recognition" or "STOPPING=1".
    """
    address = os.environ.get("NOTIFY_SOCKET")
    if not address or not states:
        return
    if address.startswith("@"):  # an abstract socket
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC) as sock:
            sock.connect(address)
            sock.sendall("\n".join(states).encode())
    except OSError as e:
        log.warning("could not tell systemd %s: %s", ", ".join(states), e)


def systemctl(
    *args: str, timeout_s: float = SYSTEMCTL_TIMEOUT_S, run: Runner = subprocess.run
) -> subprocess.CompletedProcess[str]:
    """Run `systemctl --user <args>` and wait for it.

    A missing systemctl and a timeout come back as failures (exit codes 127 and 124, the reason in stderr), so
    callers only check `returncode`.

    Args:
        args: The arguments after --user, such as ("start", "ollama").
        timeout_s: The longest wait.
        run: Runs the command (subprocess.run; tests pass a fake).

    Returns:
        The finished process, with text output.
    """
    argv = ["systemctl", "--user", *args]
    try:
        return run(argv, capture_output=True, text=True, timeout=timeout_s, check=False)
    except FileNotFoundError:
        return subprocess.CompletedProcess(argv, 127, "", "systemctl was not found: Orion needs a systemd desktop")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", f"systemctl {' '.join(args)} took over {timeout_s:g} s")


async def asystemctl(
    *args: str, timeout_s: float = SYSTEMCTL_TIMEOUT_S, run: Runner = subprocess.run
) -> tuple[int, str]:
    """`systemctl` for async code: it waits in a worker thread, never on the event loop.

    Args:
        args: The arguments after --user, such as ("stop", "ollama").
        timeout_s: The longest wait.
        run: Runs the command (subprocess.run; tests pass a fake).

    Returns:
        The exit code, and what systemctl printed (stdout, then stderr).
    """
    import asyncio  # already loaded wherever a coroutine runs; importing it at the top would cost every CLI command

    done = await asyncio.to_thread(systemctl, *args, timeout_s=timeout_s, run=run)
    return done.returncode, "\n".join(s for s in (done.stdout.strip(), done.stderr.strip()) if s)


def unit_active(unit: str, *, run: Runner = subprocess.run) -> bool:
    """Is a user unit up, or on its way up (Orion counts as running while his models load)?

    Args:
        unit: The unit, such as "orion" or "ollama".
        run: Runs systemctl (subprocess.run; tests pass a fake).

    Returns:
        True while it is active or activating.
    """
    return systemctl("is-active", unit, run=run).stdout.strip() in ("active", "activating")


# ---------------------------------------------------------------------------------------------------- the units
@dataclass(frozen=True, slots=True)
class OllamaUnit:
    """What ollama.service gives Ollama: the ollama.* settings it reads from its environment when it starts.

    Attributes:
        host: ollama.host, with its scheme ("http://127.0.0.1:11434").
        models_dir: Where Ollama keeps models: <data_path>/models/ollama, absolute.
        flash_attention, kv_cache_type, num_parallel, max_loaded_models: The ollama.* settings of those names.
    """

    host: str
    models_dir: Path
    flash_attention: bool
    kv_cache_type: KvCacheType
    num_parallel: int
    max_loaded_models: int

    def __post_init__(self) -> None:
        """Refuse a relative models folder: a user unit runs in the home folder, so it would point elsewhere.

        Raises:
            ValueError: `models_dir` is relative.
        """
        if not self.models_dir.is_absolute():
            raise ValueError(f"Ollama's models folder must be an absolute path, not {str(self.models_dir)!r}")

    @classmethod
    def from_config(cls, cfg: Config) -> OllamaUnit:
        """The unit's values from the settings (ollama.* and `models_dir`, which respects data_dir).

        Args:
            cfg: The settings.

        Returns:
            The values.

        Raises:
            ValueError: The models folder is relative.
        """
        o = cfg.ollama
        return cls(o.host, Path(cfg.models_dir) / "ollama", o.flash_attention, o.kv_cache_type, o.num_parallel,
                   o.max_loaded_models)

    @property
    def address(self) -> str:
        """host:port as OLLAMA_HOST takes it: ollama.host without its scheme ("127.0.0.1:11434")."""
        return urlsplit(self.host).netloc or self.host

    @property
    def local(self) -> bool:
        """True for an Ollama on this PC (a loopback or all-interfaces address): the only kind Orion runs."""
        name = urlsplit(self.host).hostname or ""
        if name == "localhost":
            return True
        try:
            ip = ipaddress.ip_address(name)
        except ValueError:
            return False
        return ip.is_loopback or ip.is_unspecified


def render_ollama_unit(u: OllamaUnit) -> str:
    """The text of ollama.service (pure).

    OLLAMA_HOST has no scheme, OLLAMA_FLASH_ATTENTION is 1 or 0, OLLAMA_MODELS is the models folder. There is no
    OLLAMA_KEEP_ALIVE: the keep-alive goes with every request.

    Args:
        u: The unit's values.

    Returns:
        The unit file's text.
    """
    values = {
        "@HOST@": u.address,
        "@FLASH_ATTENTION@": "1" if u.flash_attention else "0",
        "@KV_CACHE_TYPE@": u.kv_cache_type,
        "@NUM_PARALLEL@": str(u.num_parallel),
        "@MAX_LOADED_MODELS@": str(u.max_loaded_models),
        "@MODELS_DIR@": str(u.models_dir),
    }
    text = OLLAMA_TEMPLATE
    for placeholder, value in values.items():  # escaped so systemd reads each value back unchanged
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("%", "%%")
        text = text.replace(placeholder, escaped)
    return text


def _write_if_changed(path: Path, text: str) -> bool:
    """Write `text` to `path` only if it differs, in one step; True if written."""
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except (FileNotFoundError, UnicodeDecodeError):
        pass
    write_atomic(path, text)
    return True


def refresh_units(root: Path, ollama: OllamaUnit, *, install: bool = False, run: Runner = subprocess.run) -> list[str]:
    """Install, or bring up to date, the systemd units and the app-menu launcher for this version.

    - orion.service and the launcher come from ORION_TEMPLATE and LAUNCHER_TEMPLATE with @ROOT@ filled in.
      With `install` (`orion setup`, which install.sh runs) they are written where they are missing too; otherwise
      they are only kept current where they are installed.
    - ollama.service is rendered from the settings for an Ollama on this PC, once Ollama is installed or its unit
      exists. A remote Ollama is not Orion's to run: its unit is left alone.
    - `systemctl --user daemon-reload` runs once if a unit changed; Ollama takes new values when it next starts.

    Args:
        root: The repository folder (where the units point).
        ollama: The values for ollama.service (`OllamaUnit.from_config(cfg)`).
        install: Write orion.service and the launcher even where they are missing.
        run: Runs systemctl (subprocess.run; tests pass a fake).

    Returns:
        The names of the files that changed, such as ["ollama.service"].

    Raises:
        OSError: A file could not be written.
    """
    home = Path.home()
    units = home / ".config" / "systemd" / "user"
    changed: list[str] = []
    service_text = ORION_TEMPLATE.replace("@ROOT@", str(root))
    if (install or (units / ORION_UNIT).is_file()) and _write_if_changed(units / ORION_UNIT, service_text):
        changed.append(ORION_UNIT)
    unit = units / OLLAMA_UNIT
    if ollama.local and (unit.is_file() or which("ollama")) and _write_if_changed(unit, render_ollama_unit(ollama)):
        changed.append(OLLAMA_UNIT)
    if changed and (reload := systemctl("daemon-reload", run=run)).returncode:
        log.warning("systemctl --user daemon-reload failed: %s", reload.stderr.strip())
    launcher = home / ".local" / "share" / "applications" / LAUNCHER
    if (install or launcher.is_file()) and _write_if_changed(launcher, LAUNCHER_TEMPLATE.replace("@ROOT@", str(root))):
        changed.append(LAUNCHER)
    return changed


# ---------------------------------------------------------------------------------------------------- notifications
def notify(title: str, body: str, *, spawn: Spawner = subprocess.Popen) -> None:
    """Show a desktop notification through notify-send without waiting (safe on the event loop); none without it.

    Args:
        title: The title.
        body: The text.
        spawn: Starts the process (subprocess.Popen; tests pass a fake).
    """
    if (exe := which("notify-send")) is None:
        return
    argv = [exe, "--app-name=Orion", "--icon=dialog-information", "--", title, body]
    try:
        spawn(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        log.debug("no desktop notification: %s", e)


# ---------------------------------------------------------------------------------------------------- Ollama
class OllamaError(RuntimeError):
    """Ollama could not do what was asked; the message says why, in words for the HUD.

    Attributes:
        status: the HTTP status Ollama refused with (0 when it didn't answer).
    """

    def __init__(self, message: str, status: int = 0) -> None:
        """Keep the reason and the status."""
        super().__init__(message)
        self.status = status


class LLMModel(TypedDict):
    """An installed Ollama model, as the HUD's models page lists it."""

    name: str
    size: int  # bytes on disk
    params: str  # "4.0B"
    quant: str  # "Q4_K_M"
    capabilities: list[str]  # Ollama's words: "completion", "tools", "thinking", "vision", "embedding"…
    loaded: bool


def tagged(name: str) -> str:
    """A model name as Ollama lists it, with a tag: "llama3.2" is "llama3.2:latest"."""
    return name if ":" in name.rsplit("/", 1)[-1] else f"{name}:latest"


def _model_name(entry: Mapping[str, Any]) -> str:
    """The name of one model in an Ollama listing (/api/tags, /api/ps)."""
    return str(entry.get("model") or entry.get("name") or "")


def error_detail(text: str, status: int) -> str:
    """A refusal's own words from its body (``{"error": "..."}`` as Ollama says it, ``{"error": {"message"}}`` as
    OpenRouter does), else the body's start, else the HTTP status."""
    try:
        error = json.loads(text)["error"]
        detail = error["message"] if isinstance(error, dict) else error
    except (ValueError, KeyError, TypeError):
        detail = ""
    return str(detail or text[:300].strip() or f"HTTP {status}")


class Ollama:
    """The local Ollama service, one implementation for the CLI (under asyncio.run) and the runtime.

    It speaks Ollama's HTTP API through one httpx.AsyncClient, made on first use and closed by `aclose()`, and starts
    and stops its service through `systemctl --user`. Ollama runs only while THIS PC is Orion's brain.
    """

    def __init__(
        self,
        host: str,
        *,
        timeout_s: float,
        client: httpx.AsyncClient | None = None,
        run: Runner = subprocess.run,
    ) -> None:
        """Prepare to talk to Ollama; nothing connects yet.

        Args:
            host: ollama.host, such as "http://127.0.0.1:11434".
            timeout_s: The longest wait for one answer, ollama.api_timeout_s (a model download waits as long as it
                takes).
            client: A client to share (tests pass one on httpx.MockTransport); it is never closed here.
            run: Runs systemctl (subprocess.run; tests pass a fake).
        """
        self.host = host.rstrip("/")
        self.timeout_s = timeout_s
        self._client = client
        self._own_client = client is None
        self._run = run
        self._capabilities: dict[str, list[str]] = {}  # by digest: what a downloaded model can do never changes

    def _http(self) -> httpx.AsyncClient:
        """The HTTP client, made on first use."""
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_s, connect=OLLAMA_CONNECT_S))
        return self._client

    async def _call(self, method: str, path: str, **options: Any) -> httpx.Response:
        """Send one request to the API and return its successful response.

        Args:
            method: The HTTP method.
            path: The API path, such as "/api/tags".
            options: httpx request options (json, timeout).

        Returns:
            The response.

        Raises:
            OllamaError: Ollama isn't running (NOT_RUNNING), the request failed, or Ollama refused it (its reason).
        """
        import httpx

        try:
            response = await self._http().request(method, self.host + path, **options)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise OllamaError(NOT_RUNNING) from e
        except httpx.HTTPError as e:
            raise OllamaError(f"Ollama: {e}") from e
        if response.is_error:
            raise OllamaError(error_detail(response.text, response.status_code), response.status_code)
        return response

    @contextlib.asynccontextmanager
    async def lines(self, path: str, body: Mapping[str, Any], **options: Any) -> AsyncIterator[AsyncIterator[str]]:
        """POST a streamed request and yield its non-blank lines as they come (a chat answer, a download).

        Args:
            path: The API path, such as "/api/chat".
            body: The request's JSON.
            options: httpx request options (timeout).

        Raises:
            OllamaError: Ollama isn't running (NOT_RUNNING), the request or the stream failed, or Ollama refused it
                (its reason and status).
        """
        import httpx

        try:
            async with self._http().stream("POST", self.host + path, json=body, **options) as response:
                if response.is_error:
                    text = (await response.aread()).decode(errors="replace")
                    raise OllamaError(error_detail(text, response.status_code), response.status_code)
                yield (line async for line in response.aiter_lines() if line.strip())
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise OllamaError(NOT_RUNNING) from e
        except httpx.HTTPError as e:
            raise OllamaError(f"Ollama: {e}") from e

    @staticmethod
    def installed() -> bool:
        """Is Ollama on this PC (`install.sh --local-brain` puts it in ~/.local/bin)?"""
        return which("ollama") is not None

    async def answers(self, timeout_s: float = 1.0) -> bool:
        """Is Ollama up? True if GET /api/version succeeds within `timeout_s` seconds."""
        try:
            await self._call("GET", "/api/version", timeout=timeout_s)
        except OllamaError:
            return False
        return True

    async def start(self, timeout_s: float) -> bool:
        """Start Ollama's service unless it answers already, then wait until it answers.

        Args:
            timeout_s: The longest wait for it to answer after starting (ollama.start_timeout_s).

        Returns:
            True if this call started it (so it is the caller's to stop), False if it was running.

        Raises:
            OllamaError: Ollama is not installed, its service failed to start, or it never answered in time.
        """
        if await self.answers():
            return False
        if not self.installed():
            raise OllamaError(NOT_INSTALLED)
        code, out = await asystemctl("start", "ollama", run=self._run)
        if code:
            raise OllamaError(f"Ollama didn't start: {out or f'systemctl exited with {code}'}")
        if not await self.wait(timeout_s):
            raise OllamaError(f"Ollama didn't answer within {timeout_s:g} seconds: see journalctl --user -u ollama")
        return True

    async def wait(self, timeout_s: float) -> bool:
        """Wait until Ollama answers (it is starting).

        Args:
            timeout_s: The longest wait.

        Returns:
            Whether it answered in time.
        """
        import asyncio

        deadline = asyncio.get_running_loop().time() + timeout_s
        while not await self.answers():
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(OLLAMA_POLL_S)
        return True

    async def stop(self) -> None:
        """Stop Ollama's service, freeing the GPU; a failure is only logged (there is nothing more to do)."""
        code, out = await asystemctl("stop", "ollama", run=self._run)
        if code:
            log.warning("could not stop Ollama: %s", out or f"systemctl exited with {code}")

    async def unload(self, model: str) -> None:
        """Free a model's GPU memory now (keep_alive 0); nothing to do if Ollama isn't running or lacks it."""
        try:
            await self._call("POST", "/api/generate", json={"model": model, "keep_alive": 0, "stream": False})
        except OllamaError as e:
            log.debug("could not unload %s: %s", model, e)

    async def models(self) -> tuple[list[LLMModel], str]:
        """The installed models with what each can do and whether it is loaded.

        Returns:
            The models sorted by name and "", or no models and why Ollama couldn't say.
        """
        import asyncio

        try:
            listed: list[Mapping[str, Any]] = (await self._call("GET", "/api/tags")).json().get("models") or []
            running = {_model_name(m): m for m in (await self._call("GET", "/api/ps")).json().get("models") or []}
        except OllamaError as e:
            return [], str(e)
        except (ValueError, AttributeError) as e:  # not the JSON Ollama sends
            return [], f"Ollama: {e}"
        found = await asyncio.gather(*(self._describe(m, running) for m in listed))
        return sorted(found, key=lambda m: m["name"]), ""

    async def capabilities(self, name: str) -> list[str]:
        """What one installed model can do ("tools", "vision"...), [] when Ollama can't say.

        Args:
            name: the model.

        Returns:
            Ollama's words for it.
        """
        try:
            shown = (await self._call("POST", "/api/show", json={"model": name})).json()
        except (OllamaError, ValueError, AttributeError) as e:
            log.debug("no capabilities for %s: %s", name, e)
            return []
        return [str(c) for c in shown.get("capabilities") or []]

    async def _describe(self, entry: Mapping[str, Any], running: Mapping[str, Mapping[str, Any]]) -> LLMModel:
        """One model of /api/tags as the HUD lists it; `running` is /api/ps by name. Capabilities: once a digest."""
        name = _model_name(entry)
        digest = str(entry.get("digest") or name)
        if digest not in self._capabilities:
            try:
                shown = (await self._call("POST", "/api/show", json={"model": name})).json()
                self._capabilities[digest] = [str(c) for c in shown.get("capabilities") or []]
            except (OllamaError, ValueError, AttributeError) as e:  # asked again next time
                log.debug("no capabilities for %s: %s", name, e)
        details: Mapping[str, Any] = entry.get("details") or {}
        return LLMModel(
            name=name,
            size=int(entry.get("size") or 0),
            params=str(details.get("parameter_size") or ""),
            quant=str(details.get("quantization_level") or ""),
            capabilities=list(self._capabilities.get(digest, ())),
            loaded=name in running,
        )

    async def pull(self, name: str, publish: Publish, throttle_s: float = 0.25) -> bool:
        """Download a model, reporting progress as `model_pull` events.

        Progress events carry Ollama's status words with `completed` and `total` bytes, at most one per `throttle_s`
        (the final "success" always); then one event says "done", or "error" with the reason.

        Args:
            name: The model, such as "qwen3.5:4b".
            publish: Sends an event: publish("model_pull", name=..., status=..., ...).
            throttle_s: The least time between two progress events.

        Returns:
            True when the model is ready, False after an "error" event.
        """
        import asyncio

        import httpx

        loop = asyncio.get_running_loop()
        last, status = float("-inf"), ""
        timeout = httpx.Timeout(self.timeout_s, connect=OLLAMA_CONNECT_S, read=None)  # checking a big file is quiet
        try:
            async with self.lines("/api/pull", {"model": name, "stream": True}, timeout=timeout) as lines:
                async for line in lines:
                    progress = json.loads(line)
                    if not isinstance(progress, dict):
                        continue
                    if progress.get("error"):
                        raise OllamaError(str(progress["error"]))
                    status = str(progress.get("status") or "")
                    if status == "success" or loop.time() - last >= throttle_s:
                        last = loop.time()
                        publish("model_pull", name=name, status=status, completed=int(progress.get("completed") or 0),
                                total=int(progress.get("total") or 0))
            if status != "success":
                raise OllamaError("the download stopped before it finished")
        except (OllamaError, ValueError) as e:
            publish("model_pull", name=name, status="error", error=str(e) or type(e).__name__)
            return False
        publish("model_pull", name=name, status="done")
        return True

    async def delete(self, name: str) -> None:
        """Delete an installed model.

        Args:
            name: The model.

        Raises:
            OllamaError: Ollama isn't running, or it refused (an unknown model, say).
        """
        await self._call("DELETE", "/api/delete", json={"model": name})

    async def aclose(self) -> None:
        """Close the HTTP client made here (a shared one stays open); a later call makes a new one."""
        client = self._client
        if self._own_client:
            self._client = None
            if client is not None:
                await client.aclose()


# ---------------------------------------------------------------------------------------------------- vital signs
class GpuStats(TypedDict):
    """The NVIDIA GPU's state."""

    name: str
    util: float  # % load
    mem_used: float  # MiB
    mem_total: float  # MiB
    temp: float  # °C


class Battery(TypedDict):
    """The battery's state."""

    percent: float
    plugged: bool | None


class FastVitals(TypedDict):
    """Readings that cost microseconds, taken every few seconds."""

    cpu: float  # % since the previous reading
    ram: float  # % in use
    ram_used: float  # GB
    ram_total: float  # GB
    gpu: GpuStats | None  # None without an NVIDIA GPU


class SlowVitals(TypedDict):
    """Readings that cost more (disk, battery, sensors), taken less often and reused in between."""

    disk: float  # % full
    disk_free: float  # GB
    battery: Battery | None  # None without a battery
    cpu_temp: float | None  # °C; None without a known sensor


class Program(TypedDict):
    """A program the PC runs, its processes added up under its executable's name (a browser's tabs count as the
    browser)."""

    name: str
    cpu: float  # % of the whole CPU (all its cores), since the previous reading in this process
    mem: float  # GB


class Stats(FastVitals, SlowVitals):
    """Everything the HUD's VITALS shows (the web app's `Stats`)."""

    uptime: float  # seconds since the PC booted
    top: list[Program]  # the busiest programs, by CPU then memory (empty until they are first read)


class Vitals:
    """PC vital signs for the HUD (main process) and the system_status tool (tools process).

    Fast readings (CPU, RAM, GPU) cost microseconds; slow ones (disk, battery, temperatures) are reused by the fast
    snapshots in between. NVIDIA's library opens on the first GPU reading and, if it can't, is tried again a minute
    later rather than never.
    """

    def __init__(self, disk_path: str, gpu_index: int = 0) -> None:
        """Take the CPU baseline, so the first reading means something.

        Args:
            disk_path: The disk to report (system.disk_path), such as "/".
            gpu_index: Which NVIDIA GPU to report.
        """
        import psutil

        self.disk_path = disk_path
        self.gpu_index = gpu_index
        self._boot: float = float(psutil.boot_time())
        self._gpu: Any = None  # the NVML device handle
        self._gpu_name = ""
        self._gpu_retry_at = 0.0  # monotonic time of the next attempt to open NVML
        self._slow: SlowVitals | None = None
        self._top: list[Program] = []
        self._cores = psutil.cpu_count() or 1
        psutil.cpu_percent(None)

    @property
    def uptime_s(self) -> float:
        """Seconds since the PC booted."""
        return time.time() - self._boot

    def fast(self) -> FastVitals:
        """CPU (since the previous reading), RAM and GPU now."""
        import psutil

        vm = psutil.virtual_memory()
        return FastVitals(
            cpu=psutil.cpu_percent(None), ram=vm.percent, ram_used=vm.used / GB, ram_total=vm.total / GB, gpu=self.gpu()
        )

    def slow(self) -> SlowVitals:
        """Disk, battery and CPU temperature now, kept for the fast snapshots that follow."""
        import psutil

        try:
            usage = psutil.disk_usage(self.disk_path)
            disk, disk_free = usage.percent, usage.free / GB
        except OSError:
            disk, disk_free = 0.0, 0.0
        self._slow = SlowVitals(disk=disk, disk_free=disk_free, battery=_battery(), cpu_temp=_cpu_temp())
        return self._slow

    def snapshot(self, *, slow: bool, programs: int = 0) -> Stats:
        """All vital signs as the HUD shows them, with fresh slow readings if `slow` and that many of the busiest
        `programs` read again (0: the last ones)."""
        slow_part = self.slow() if slow or self._slow is None else self._slow
        top = self.programs(programs, by="cpu") if programs else self._top
        return Stats(**self.fast(), **slow_part, uptime=self.uptime_s, top=top)

    def programs(self, count: int, *, by: Literal["cpu", "memory"]) -> list[Program]:
        """The programs using the most CPU since the previous reading (then the most memory), or the most memory.
        psutil keeps each process between readings, so one new since the last reads 0 % this time (the first reading
        is all zeros). Tens of milliseconds for a few hundred processes."""
        import psutil

        usage: dict[str, list[float]] = {}
        for proc in psutil.process_iter(["name", "exe", "cpu_percent", "memory_info"]):
            info = proc.info  # another user's process: its name alone, no executable
            if info["cpu_percent"] is None or info["memory_info"] is None:
                continue
            totals = usage.setdefault(Path(info["exe"]).name if info["exe"] else info["name"] or "?", [0.0, 0.0])
            totals[0] += info["cpu_percent"]
            totals[1] += info["memory_info"].rss
        found = [Program(name=name, cpu=round(cpu / self._cores, 1), mem=round(rss / GB, 2))
                 for name, (cpu, rss) in usage.items()]
        found.sort(key=(lambda p: (p["cpu"], p["mem"])) if by == "cpu" else (lambda p: p["mem"]), reverse=True)
        self._top = found[:count]
        return self._top

    def gpu(self) -> GpuStats | None:
        """The NVIDIA GPU's load, memory and temperature now; None without an NVIDIA GPU or driver."""
        if (handle := self._nvml()) is None:
            return None
        import pynvml

        try:
            memory = pynvml.nvmlDeviceGetMemoryInfo(handle, version=pynvml.nvmlMemory_v2)  # v2: no driver reserve
            return GpuStats(
                name=self._gpu_name,
                util=float(pynvml.nvmlDeviceGetUtilizationRates(handle).gpu),
                mem_used=memory.used / MIB,
                mem_total=memory.total / MIB,
                temp=float(pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)),
            )
        except pynvml.NVMLError as e:  # the driver went away (an update, say): open it again later
            log.debug("GPU reading failed: %s", e)
            self._gpu, self._gpu_retry_at = None, time.monotonic() + NVML_RETRY_S
            return None

    def _nvml(self) -> Any:
        """The GPU's NVML handle, opened on first use and retried a minute after a failure; None meanwhile."""
        if self._gpu is not None:
            return self._gpu
        now = time.monotonic()
        if now < self._gpu_retry_at:
            return None
        try:
            import pynvml
        except ImportError as e:  # nvidia-ml-py is a dependency, but a broken install should only cost the readings
            log.debug("no GPU readings: %s", e)
            self._gpu_retry_at = now + NVML_RETRY_S
            return None
        try:
            pynvml.nvmlInit()
            handle = pynvml.nvmlDeviceGetHandleByIndex(self.gpu_index)
            self._gpu_name = str(pynvml.nvmlDeviceGetName(handle))
        except (pynvml.NVMLError, OSError) as e:  # no driver (NVMLError_LibraryNotFound), no such GPU, a driver fault
            log.debug("no GPU readings: %s", e)
            self._gpu_retry_at = now + NVML_RETRY_S
            return None
        self._gpu = handle
        return handle


def _battery() -> Battery | None:
    """The battery's charge, whether it is plugged in, and the time left; None without a battery."""
    import psutil

    try:
        bat = psutil.sensors_battery()
    except (OSError, RuntimeError):
        return None
    if bat is None:
        return None
    return Battery(percent=float(bat.percent), plugged=bat.power_plugged)


def _cpu_temp() -> float | None:
    """The hottest CPU core in °C, from the first known sensor driver; None without one."""
    import psutil

    try:
        temps = psutil.sensors_temperatures()
    except (OSError, RuntimeError, AttributeError):  # AttributeError: no sensor support on this platform
        return None
    for key in CPU_SENSORS:
        if readings := temps.get(key):
            return max(float(t.current) for t in readings)
    return None
