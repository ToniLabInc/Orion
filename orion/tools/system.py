"""PC control tools for this machine: apps, files, commands, volume, media, the screen lock, power and its health.

Tools that need a desktop, a sound server or systemd say so (``requires``; ``orion.host``), so on a server or in a
container they aren't offered; the volume works through PipeWire's wpctl or PulseAudio's pactl, whichever is here.

Commands run in a sandbox (``orion.sandbox``): the PC read-only to them but for the folders the user chose, keys and
sign-ins hidden, an environment of their own, no desktop; with ``outside_sandbox`` a command runs as the user does, which
needs the user's yes every time (the hub asks; ``orion.mcp``). A command stops after ``system.command_max_timeout_s`` at
most, and what it prints is cut to ``system.command_output_chars``. The file tools reach the whole PC, but not the places
the sandbox hides, and ``write_file`` changes neither Orion's own code and settings nor a file that starts programs.
Nothing waits a fixed time: CPU readings are measured from the previous reading (``prime`` takes the first ones during
warm-up), media players and the screen lock are driven over D-Bus (one connection per process, through jeepney), and power
actions are scheduled with systemd-run, so the tool returns at once and he can still say goodbye. The programs he starts
for the desktop never inherit a variable that looks secret.
"""

import configparser
import contextlib
import difflib
import fnmatch
import functools
import getpass
import logging
import os
import re
import shlex
import subprocess
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal

import psutil
from pydantic import Field

from orion import sandbox
from orion.base import TRUNCATED
from orion.host import profile
from orion.secrets import inherited, strip_environ
from orion.service import Program, Vitals, local_path, which
from orion.tools import GROUPS, Group, ctx, current_call
from orion.tools.policy import ProcRow, Refusal, close_victims

if TYPE_CHECKING:
    from jeepney.io.blocking import DBusConnection

log = logging.getLogger("orion.tools.system")
group = Group(GROUPS["system"])

HOME: Final = Path.home()
APP_DIRS: Final = (
    Path("/usr/share/applications"), Path("/usr/local/share/applications"), HOME / ".local/share/applications",
    Path("/var/lib/flatpak/exports/share/applications"), HOME / ".local/share/flatpak/exports/share/applications",
    Path("/var/lib/snapd/desktop/applications"),
)
FILE_TYPES: Final = frozenset({  # "report.pdf" is a file, not a website (a few are also domains, but rarely meant)
    "pdf", "txt", "md", "doc", "docx", "odt", "rtf", "csv", "xls", "xlsx", "ods", "ppt", "pptx", "odp", "jpg", "jpeg",
    "png", "gif", "webp", "svg", "heic", "mp3", "wav", "flac", "ogg", "m4a", "mp4", "mkv", "avi", "mov", "webm", "zip",
    "tar", "gz", "7z", "rar", "iso", "json", "xml", "yaml", "yml", "log", "py"})
CPU_MIN_INTERVAL_S: Final = 0.2  # a CPU reading needs at least this long since the previous one
PROCESS_SAMPLE_S: Final = 0.2  # the first top_processes reading waits this long when nothing primed it
KILL_AFTER_S: Final = 3.0  # close_app kills what hasn't exited this long after being asked to
STORAGE_TIMEOUT_S: Final = 8.0  # each spot of storage_report is counted for at most this long
READ_LIMIT_BYTES: Final = 200_000  # read_file reads at most this much of a file
DBUS_TIMEOUT_S: Final = 2.0  # a D-Bus call that takes longer has failed
# What `bash -l` does before a command, run quietly: its startup files may print (cloud-init's warnings, an input
# method's log lines, which in the sandbox find no journal to write to), and the model should read the command's output
# alone. The command comes as the first argument, so nothing needs quoting.
SHELL_START: Final = ('{ [ -r /etc/profile ] && . /etc/profile; for f in ~/.bash_profile ~/.bash_login ~/.profile; do '
                      '[ -r "$f" ] && { . "$f"; break; }; done; } >/dev/null 2>&1; eval "$1"')
MPRIS: Final = "org.mpris.MediaPlayer2"
MPRIS_PATH: Final = "/org/mpris/MediaPlayer2"
PLAYER: Final = "org.mpris.MediaPlayer2.Player"
LOCKERS: Final = (("org.gnome.ScreenSaver", "/org/gnome/ScreenSaver"),
                  ("org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver"))
MEDIA_METHODS: Final = {"play_pause": "PlayPause", "play": "Play", "pause": "Pause", "next": "Next",
                        "previous": "Previous", "stop": "Stop"}
POWER_COMMANDS: Final = {"suspend": ["systemctl", "suspend"], "restart": ["systemctl", "reboot"],
                         "shutdown": ["systemctl", "poweroff"]}


def _desktop_env() -> dict[str, str]:
    """What the programs he starts for the desktop (the volume, an app, the screen lock) run with: Orion's environment
    without anything that looks secret, so an app he opens never inherits a token; the desktop's own variables stay."""
    return {**strip_environ(os.environ), "PATH": local_path()}


def _run(cmd: list[str], timeout: float = 10) -> subprocess.CompletedProcess[str]:
    """Run a short program and capture what it prints (its exit status is the caller's to read)."""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False, env=_desktop_env())


def _spawn(cmd: list[str]) -> None:
    """Start a program in its own session, without waiting for it."""
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                     start_new_session=True, env=_desktop_env())


def resolve(path: str) -> Path:
    """The path the user means: "~/Downloads", "$HOME/x", or a name relative to the home folder.

    Args:
        path: what the model passed.

    Returns:
        The absolute path, symlinks resolved.
    """
    raw = os.path.expandvars(path.strip())
    p = HOME / raw[1:].lstrip("/") if raw.startswith("~") else Path(raw)
    return (p if p.is_absolute() else HOME / p).resolve()


# ---------------------------------------------------------------------------------------------------- readings
class _Readings:
    """What the health tools remember between calls: the vitals reader, the last CPU reading and whether the programs
    were read before (so no call sleeps a fixed time)."""

    def __init__(self) -> None:
        """Start with nothing measured."""
        self.lock = threading.Lock()
        self.vitals: Vitals | None = None
        self.cpu_at = 0.0  # monotonic time of the last whole-CPU reading (0: none yet)
        self.programs_read = False

    def reader(self) -> Vitals:
        """The vitals reader, made on first use for the configured disk."""
        if self.vitals is None:
            self.vitals = Vitals(ctx.config.system.disk_path)
        return self.vitals

    def cpu_ready(self) -> None:
        """Make sure the next CPU reading spans at least CPU_MIN_INTERVAL_S (waiting only the remainder)."""
        now = time.monotonic()
        if not self.cpu_at:
            psutil.cpu_percent(None)
            self.cpu_at = now
        if (left := CPU_MIN_INTERVAL_S - (now - self.cpu_at)) > 0:
            time.sleep(left)
        self.cpu_at = time.monotonic()

    def programs(self, count: int, by: Literal["cpu", "memory"]) -> list[Program]:
        """The heaviest programs (``Vitals.programs``); a first reading is all zeros, so it is taken again after a
        moment."""
        if not self.programs_read:
            self.reader().programs(0, by=by)
            time.sleep(PROCESS_SAMPLE_S)
            self.programs_read = True
        return self.reader().programs(count, by=by)


_readings = _Readings()


def prime() -> None:
    """Take the first CPU and process readings and index the installed apps (the tools process's warm-up)."""
    with _readings.lock:
        psutil.cpu_percent(None)
        _readings.cpu_at = time.monotonic()
        _readings.programs(0, by="cpu")
    _desktop_apps()


# ---------------------------------------------------------------------------------------------------- D-Bus
class _Bus:
    """One session-bus connection per process, opened on first use and reopened after a failure."""

    def __init__(self) -> None:
        """Start unconnected."""
        self.lock = threading.Lock()
        self.connection: DBusConnection | None = None

    def call(self, destination: str, path: str, interface: str, method: str, signature: str = "",
             body: tuple[Any, ...] = ()) -> tuple[Any, ...]:
        """Call a D-Bus method and return the reply's body.

        Raises:
            OSError: the bus can't be reached.
            jeepney.DBusErrorResponse: the service answered with an error.
        """
        from jeepney import DBusAddress, new_method_call
        from jeepney.io.blocking import open_dbus_connection
        from jeepney.wrappers import unwrap_msg  # not re-exported at the top level (jeepney 0.9)

        message = new_method_call(DBusAddress(path, bus_name=destination, interface=interface), method, signature,
                                  body)
        with self.lock:
            if self.connection is None:
                self.connection = open_dbus_connection(bus="SESSION")
            try:
                reply = self.connection.send_and_get_reply(message, timeout=DBUS_TIMEOUT_S)
            except (OSError, TimeoutError):
                with contextlib.suppress(OSError):
                    self.connection.close()
                self.connection = None
                raise
        return tuple(unwrap_msg(reply))

    def property(self, destination: str, path: str, interface: str, name: str) -> Any:
        """A D-Bus property's value (its variant unwrapped)."""
        (variant,) = self.call(destination, path, "org.freedesktop.DBus.Properties", "Get", "ss", (interface, name))
        return variant[1]


_bus = _Bus()


# ---------------------------------------------------------------------------------------------------- applications
def _desktop_apps() -> list[dict[str, str]]:
    """The installed applications, read again only when an applications folder changed (an app came or went)."""
    stamp = tuple((str(d), d.stat().st_mtime_ns) if d.is_dir() else (str(d), 0) for d in APP_DIRS)
    return _read_desktop_apps(stamp)


@functools.lru_cache(maxsize=1)
def _read_desktop_apps(stamp: tuple[tuple[str, int], ...]) -> list[dict[str, str]]:
    """Parse every visible application's .desktop file (``stamp`` is the folders' state, the cache key)."""
    apps: list[dict[str, str]] = []
    seen: set[str] = set()
    for folder in APP_DIRS:
        if not folder.is_dir():
            continue
        for file in sorted(folder.glob("*.desktop")):
            if file.name in seen:
                continue
            parser = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                parser.read(file, encoding="utf-8")
                entry = parser["Desktop Entry"]
            except (configparser.Error, KeyError, UnicodeDecodeError, OSError):
                continue
            if entry.get("NoDisplay", "false").lower() == "true" or entry.get("Type", "Application") != "Application":
                continue
            seen.add(file.name)
            apps.append({"name": entry.get("Name", file.stem), "exec": entry.get("Exec", ""), "path": str(file),
                         "keywords": f"{entry.get('Keywords', '')};{entry.get('GenericName', '')}".lower()})
    return apps


def _find_app(name: str) -> dict[str, str] | None:
    """The installed application a name means best, when it means one well enough (``system.app_match_min_score``)."""
    q = name.lower().strip()
    best, best_score = None, 0.0
    for app in _desktop_apps():
        n = app["name"].lower()
        score = difflib.SequenceMatcher(None, q, n).ratio()
        if q == n:
            score = 2.0
        elif q in n.split() or n.startswith(q):
            score = max(score, 1.2)
        elif q in n:
            score = max(score, 0.95)
        elif q in app["keywords"]:
            score = max(score, 0.8)
        if score > best_score:
            best, best_score = app, score
    return best if best_score >= ctx.config.system.app_match_min_score else None


@group.tool(name="open", title="Open an app, site, file or folder", requires=("desktop",),
            examples=("Open Firefox", "Open youtube.com", "Open my Downloads folder"))
def open_target(target: Annotated[str, Field(description='An app name ("firefox", "terminal", "files", "spotify"), a '
                                                         'URL or domain ("youtube.com"), or a file or folder path '
                                                         '("~/Downloads").')]) -> str:
    """Open an application, website, file or folder on the PC."""
    t = target.strip()
    dotted = re.fullmatch(r"[\w.-]+\.([a-zA-Z]{2,})(/\S*)?", t)  # "youtube.com", "news.bbc.co.uk/sport" or "notes.txt"
    file_name = bool(dotted) and (dotted.group(1).lower() in FILE_TYPES or (HOME / t).exists())
    if re.match(r"^https?://", t) or (dotted and not file_name):
        url = t if t.startswith("http") else f"https://{t}"
        _spawn(["xdg-open", url])
        return f"Opened {url} in the browser."
    if file_name and "/" not in t:
        if (HOME / t).exists():
            return open_target(f"~/{t}")
        return f"There's no {t} in the home folder. Find it with find_files, then open it by its full path."
    if "/" in t or t.startswith("~"):
        path = resolve(t)
        if not path.exists():
            return f"There is no {path}."
        _spawn(["xdg-open", str(path)])
        return f"Opened {path}."
    app = _find_app(t)
    if app is None:
        if (HOME / t).exists():  # "Downloads"
            return open_target(f"~/{t}")
        return f"No installed application matches '{target}'."
    _spawn(["gio", "launch", app["path"]])
    return f"Launched {app['name']}."


@group.tool(destructive=True, title="Close a running app", examples=("Close Firefox", "Quit Spotify"))
def close_app(name: Annotated[str, Field(description='Application or process name, e.g. "firefox".')]) -> str:
    """Close (terminate) a running application. The desktop, the sound system and Orion himself are never
    closed."""
    app = _find_app(name) if len(name.strip()) >= 3 else None
    executables = frozenset({shlex.split(app["exec"] or "x")[0]} if app else ())
    me = psutil.Process()
    rows: list[ProcRow] = []
    processes: dict[int, psutil.Process] = {}
    for proc in psutil.process_iter(["pid", "name", "username", "exe"]):
        info = proc.info
        rows.append(ProcRow(int(info["pid"]), info["name"] or "", os.path.basename(info["exe"] or ""),
                            info["username"] or ""))
        processes[int(info["pid"])] = proc
    chosen = close_victims(rows, name, me=me.pid, ancestors=frozenset(p.pid for p in me.parents()),
                           user=getpass.getuser(), exec_names=executables)
    if isinstance(chosen, Refusal):
        return chosen.reason
    if not chosen:
        return f"No running application matches '{name}'."
    victims = [processes[pid] for pid in chosen]
    for proc in victims:
        with contextlib.suppress(psutil.Error):
            proc.terminate()
    _, alive = psutil.wait_procs(victims, timeout=KILL_AFTER_S)
    for proc in alive:
        with contextlib.suppress(psutil.Error):
            proc.kill()
    return f"Closed {len(victims)} process(es) of {name}."


# ---------------------------------------------------------------------------------------------------- files
@group.tool(read_only=True, title="Find files by name", examples=("Find my PDF invoices", "Where is my notes file?"))
def find_files(query: Annotated[str, Field(description='Words in the file name, or a pattern like "*.pdf".')],
               folder: Annotated[str, Field(description="Where to search (default: home).")] = "~") -> str:
    """Find files or folders by name in the user's home directory (shallow matches first)."""
    system = ctx.config.system
    root = resolve(folder)
    pattern = query.lower() if any(c in query for c in "*?[") else None
    words = query.lower().split()
    skip = frozenset(system.find_skip_dirs)
    hits: list[Path] = []
    deadline = time.monotonic() + system.find_timeout_s
    queue: deque[Path] = deque([root])
    while queue and len(hits) < system.find_max_results and time.monotonic() < deadline:
        try:
            with os.scandir(queue.popleft()) as entries:
                listed = list(entries)
        except OSError:
            continue
        for entry in listed:
            lower = entry.name.lower()
            if lower.startswith("."):
                continue
            if (fnmatch.fnmatch(lower, pattern) if pattern else all(w in lower for w in words)):
                hits.append(Path(entry.path))
                if len(hits) >= system.find_max_results:
                    break
            with contextlib.suppress(OSError):
                if entry.is_dir(follow_symlinks=False) and entry.name not in skip:
                    queue.append(Path(entry.path))
    if not hits:
        return f"No files matching '{query}' under {root}."
    lines = []
    for hit in hits:
        try:
            st = hit.stat()
            kind = "folder" if hit.is_dir() else f"{st.st_size / 1e6:.1f} MB"
            lines.append(f"{hit} ({kind}, modified {time.strftime('%d %b %Y', time.localtime(st.st_mtime))})")
        except OSError:
            lines.append(str(hit))
    return "\n".join(lines)


@group.tool(read_only=True, open_world=True, title="Read a text file",
            examples=("Read my todo file", "What does ~/notes.txt say?"))
def read_file(path: Annotated[str, Field(description="Path to the file.")]) -> str:
    """Read a text file (code, notes, logs, config) and return its contents."""
    limit = ctx.config.system.read_file_chars
    p = resolve(path)
    if why := _kept_out(p, write=False):
        return why
    if not p.is_file():
        return f"No such file: {p}"
    with p.open("rb") as f:
        data = f.read(READ_LIMIT_BYTES)
    if b"\x00" in data[:4096]:
        return f"{p} is a binary file; I can only read text files."
    text = data.decode("utf-8", errors="replace")
    return text[:limit] + (TRUNCATED if len(text) > limit else "")


def _kept_out(path: Path, *, write: bool) -> str:
    """Why a file tool may not read or change a path ("" when it may): the places the sandbox hides are not read, and
    Orion's own code, his skills and the files that start programs are not changed (``sandbox.keeps_out``)."""
    return sandbox.keeps_out(path, sandbox.jail_for(ctx.config, ctx.settings.path), write=write)


def _protected_files() -> frozenset[Path]:
    """Orion's own settings file and its baseline, which write_file never touches."""
    from orion.config import BASE_NAME

    settings = ctx.settings.path.resolve()
    return frozenset({settings, settings.with_name(BASE_NAME)})


@group.tool(destructive=True, title="Write or append to a file",
            examples=("Save that to ~/shopping.txt", "Add a line to my notes file"))
def write_file(path: Annotated[str, Field(description="Path to the file.")],
               content: Annotated[str, Field(description="Text to write.")],
               append: Annotated[bool, Field(description="Append instead of overwriting.")] = False) -> str:
    """Create, overwrite or append to a text file. Orion's own settings are changed with the settings tools
    only."""
    p = resolve(path)
    if p in _protected_files():
        return "That is Orion's own settings file: change settings with the settings tools, or in Settings."
    if why := _kept_out(p, write=True):
        return why
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a" if append else "w", encoding="utf-8") as f:
        f.write(content)
    return f"Wrote {len(content)} characters to {p}."


# ---------------------------------------------------------------------------------------------------- the PC
@group.tool(read_only=True, title="Check the PC's health", examples=("How is the PC doing?", "How hot is the GPU?"))
def system_status() -> str:
    """Report the PC's health: CPU, memory, GPU, temperatures, disk, battery and uptime."""
    with _readings.lock:
        _readings.cpu_ready()
        s = _readings.reader().snapshot(slow=True)
    parts = [f"CPU {s['cpu']:.0f}%" + (f" at {s['cpu_temp']:.0f}°C" if s["cpu_temp"] else "")]
    parts.append(f"RAM {s['ram_used']:.1f} of {s['ram_total']:.1f} GB")
    if gpu := s["gpu"]:
        parts.append(f"GPU {gpu['name']} {gpu['util']:.0f}% load, {gpu['mem_used'] / 1024:.1f} of "
                     f"{gpu['mem_total'] / 1024:.1f} GB VRAM, {gpu['temp']:.0f}°C")
    parts.append(f"disk {s['disk']:.0f}% full ({s['disk_free']:.0f} GB free)")
    if battery := s["battery"]:
        parts.append(f"battery {battery['percent']:.0f}% ({'charging' if battery['plugged'] else 'on battery'})")
    hours = int(s["uptime"] // 3600)
    parts.append(f"up {hours} h {int(s['uptime'] % 3600 // 60)} min")
    return "; ".join(parts) + "."


@group.tool(title="Read or change the volume", examples=("Turn the volume down a bit", "Set the volume to 40", "Mute"),
            requires=("sound",))
def volume(action: Literal["get", "set", "up", "down", "mute", "unmute"],
           level: Annotated[int, Field(ge=0, le=100, description='Percent for "set", or the step for "up" and '
                                                                 '"down" (0: the usual step).')] = 0) -> str:
    """Get or change the speaker volume."""
    step = level or ctx.config.system.volume_step
    if profile().sound == "pactl":
        return _volume_pactl(action, level, step)
    sink = "@DEFAULT_AUDIO_SINK@"
    if action == "set":
        _run(["wpctl", "set-volume", "-l", "1.0", sink, f"{level}%"])
    elif action in ("up", "down"):
        _run(["wpctl", "set-volume", "-l", "1.0", sink, f"{step}%{'+' if action == 'up' else '-'}"])
    elif action in ("mute", "unmute"):
        _run(["wpctl", "set-mute", sink, "1" if action == "mute" else "0"])
    out = _run(["wpctl", "get-volume", sink]).stdout.strip()  # "Volume: 0.40 [MUTED]"
    match = re.search(r"([\d.]+)", out)
    percent = round(float(match.group(1)) * 100) if match else "?"
    return f"Volume is {percent}%{' (muted)' if 'MUTED' in out else ''}."


def _volume_pactl(action: str, level: int, step: int) -> str:
    """The volume tool through PulseAudio's pactl (a sound server without WirePlumber), kept within 0-100%."""
    sink = "@DEFAULT_SINK@"

    def percent() -> int | None:
        out = _run(["pactl", "get-sink-volume", sink]).stdout  # "Volume: front-left: 26214 /  40% / -23.88 dB, ..."
        match = re.search(r"(\d+)%", out)
        return int(match.group(1)) if match else None

    if action == "set":
        _run(["pactl", "set-sink-volume", sink, f"{level}%"])
    elif action in ("up", "down"):
        now = percent() or 0
        _run(["pactl", "set-sink-volume", sink, f"{max(0, min(100, now + (step if action == 'up' else -step)))}%"])
    elif action in ("mute", "unmute"):
        _run(["pactl", "set-sink-mute", sink, "1" if action == "mute" else "0"])
    muted = "yes" in _run(["pactl", "get-sink-mute", sink]).stdout.lower()
    value = percent()
    return f"Volume is {'?' if value is None else value}%{' (muted)' if muted else ''}."


def _players() -> list[str]:
    """The media players on the session bus, the one playing first."""
    (names,) = _bus.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "ListNames")
    players = [str(n) for n in names if str(n).startswith(f"{MPRIS}.")]
    playing = []
    for player in players:
        with contextlib.suppress(Exception):
            if _bus.property(player, MPRIS_PATH, PLAYER, "PlaybackStatus") == "Playing":
                playing.append(player)
    return playing + [p for p in players if p not in playing]


@group.tool(title="Control media playback", examples=("Pause the music", "Next song", "What song is this?"),
            requires=("dbus",))
def media(action: Literal["play_pause", "play", "pause", "next", "previous", "stop", "status"]) -> str:
    """Control music or video playback in any media player (Spotify, the browser, VLC...)."""
    from mcp.server.mcpserver.exceptions import ToolError  # nothing was done: the call's error, not a result

    try:
        players = _players()
        if not players:
            raise ToolError("No media player is running.")
        player = players[0]
        if action != "status":
            _bus.call(player, MPRIS_PATH, PLAYER, MEDIA_METHODS[action])
        status = _bus.property(player, MPRIS_PATH, PLAYER, "PlaybackStatus")  # D-Bus keeps order: no waiting
        metadata = _bus.property(player, MPRIS_PATH, PLAYER, "Metadata")
    except ToolError:
        raise
    except (OSError, TimeoutError) as e:
        raise ToolError(f"The media players can't be reached: {e}") from None
    except Exception as e:  # jeepney's DBusErrorResponse: the player refused
        log.debug("media %s failed", action, exc_info=True)
        raise ToolError(f"The media player refused: {e}") from None
    title = (metadata.get("xesam:title") or ("", ""))[1]
    artists = (metadata.get("xesam:artist") or ("", []))[1]
    app = player.removeprefix(f"{MPRIS}.").split(".")[0]
    what = f"{title}" + (f" by {', '.join(artists)}" if artists else "") if title else "unknown track"
    return f"{app}: {status or 'unknown'}, {what}."


@group.tool(read_only=True, title="List the heaviest programs",
            examples=("Why is my PC slow?", "What is using my memory?"))
def top_processes(sort_by: Literal["cpu", "memory"] = "cpu",
                  count: Annotated[int, Field(ge=1, le=20, description="How many programs to list.")] = 8) -> str:
    """List the programs using the most CPU or memory right now (all of a program's processes added up; CPU as a share
    of the whole processor, all its cores)."""
    with _readings.lock:
        top = _readings.programs(count, sort_by)
    return "\n".join(f"{p['name']}: CPU {p['cpu']:.1f}%, memory {p['mem']:.2f} GB" for p in top)


def _folder_size(folder: Path) -> tuple[float, bool]:
    """A folder's total size in bytes, and whether counting stopped early at its deadline."""
    total, stack = 0, [folder]
    deadline = time.monotonic() + STORAGE_TIMEOUT_S
    while stack:
        if time.monotonic() > deadline:
            return total, True
        try:
            with os.scandir(stack.pop()) as entries:
                listed = list(entries)
        except OSError:
            continue
        for entry in listed:
            with contextlib.suppress(OSError):
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
    return total, False


@group.tool(read_only=True, title="Report disk space", examples=("How much space is left?", "What is filling my disk?"))
def storage_report() -> str:
    """How full the disk is, and how much the usual clean-up spots take: Downloads, the trash and caches."""
    disk = psutil.disk_usage(str(HOME))
    places = {"Downloads": HOME / "Downloads", "the trash": HOME / ".local/share/Trash", "caches": HOME / ".cache"}
    spots = {label: path for label, path in places.items() if path.is_dir()}
    with ThreadPoolExecutor(max_workers=3) as pool:
        sizes = dict(zip(spots, pool.map(_folder_size, spots.values()), strict=True))
    listed = ", ".join(f"{label} {size / 1e9:.1f} GB" for label, (size, _) in sizes.items())
    early = any(cut for _, cut in sizes.values())
    return (f"Disk {disk.percent:.0f}% full: {disk.free / 1e9:.0f} GB free of {disk.total / 1e9:.0f} GB. {listed}."
            + (" (Some folders are very large: counting stopped early.)" if early else ""))


def _command_env(*, desktop: bool) -> dict[str, str]:
    """A command's whole environment: the variables ``safety.env_allow`` names (and, for one that runs as the user does,
    ``env_desktop``: the display and the session's bus), a PATH, the home folder, and no sudo prompt. Nothing else
    of Orion's environment, so ``env`` shows no token."""
    safety = ctx.config.safety
    allow = [*safety.env_allow, *(safety.env_desktop if desktop else ())]
    return {**inherited(os.environ, allow), "PATH": local_path(), "HOME": str(HOME),
            "SUDO_ASKPASS": "/bin/false"}  # sudo -A fails, never prompts


@group.tool(destructive=True, open_world=True, title="Run a shell command",
            examples=("Run git status in my project", "Which ports are open?"))
def run_command(command: Annotated[str, Field(description="The bash command line.")],
                timeout: Annotated[int, Field(description="Seconds before the command's process group is stopped "
                                                          "(0: the usual limit).")] = 0,
                outside_sandbox: Annotated[bool, Field(description="Run it without the sandbox, with all of the user's "
                                                                   "access. The user is asked every time, and it is "
                                                                   "never possible from Discord or a job: only when "
                                                                   "the sandbox is what stops the task.")] = False) -> str:
    """Run a shell command on the PC (bash) and return its output. Use it for anything the other tools can't do: git,
    processes, disk usage, network diagnostics and so on. It runs in a sandbox: the PC is read-only to it except the
    folders the user chose, keys and sign-ins are hidden, /tmp is empty for each command, and there is no sudo and no
    desktop. No password prompt is available."""
    from mcp.server.mcpserver.exceptions import ToolError

    cfg = ctx.config
    system = cfg.system
    if outside_sandbox and current_call().provenance == "app":
        raise ToolError("Another app can't run a command outside the sandbox.")
    seconds = max(1, min(int(timeout) or system.command_timeout_s, system.command_max_timeout_s))
    limit = system.command_output_chars
    unconfined = outside_sandbox or not cfg.safety.sandbox_commands or profile().container  # (a container is the box)
    jail = None if unconfined else sandbox.jail_for(cfg, ctx.settings.path)
    try:
        done = sandbox.run(["bash", "-c", SHELL_START, "bash", command], jail=jail,
                           env=_command_env(desktop=unconfined), cwd=HOME,
                           timeout_s=seconds, keep_bytes=limit)
    except sandbox.SandboxError as e:
        raise ToolError(str(e)) from None
    if done.timed_out:
        return f"Command timed out after {seconds} s; its process group was stopped."
    text = done.output.decode("utf-8", errors="replace").strip()
    if done.cut or len(text) > limit:
        half = limit // 2
        text = f"{text[:half]}\n...\n{text[-half:]}"
    return f"exit code {done.code}\n{text or '(no output)'}"


def _lock_with(name: str, path: str) -> bool:
    """Ask one desktop's screen saver to lock; False when it isn't there or refuses."""
    try:
        _bus.call(name, path, name, "Lock")
    except Exception:  # not this desktop's locker
        log.debug("%s could not lock the screen", name, exc_info=True)
        return False
    return True


@group.tool(destructive=True, title="Lock the screen", examples=("Lock the screen",), requires=("desktop",))
def lock_screen() -> str:
    """Lock the screen."""
    if any(_lock_with(name, path) for name, path in LOCKERS):
        return "Screen locked."
    result = _run(["loginctl", "lock-session"])
    return "Screen locked." if result.returncode == 0 else f"The screen couldn't be locked: {result.stderr.strip()}"


def _logout_command() -> list[str]:
    """How to log out of this desktop: GNOME's own way (no dialog), else logind ends the graphical session."""
    if "GNOME" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper() and which("gnome-session-quit"):
        return ["gnome-session-quit", "--logout", "--no-prompt"]
    session = os.environ.get("XDG_SESSION_ID") or _run(
        ["loginctl", "show-user", getpass.getuser(), "--property=Display", "--value"]).stdout.strip()
    return ["loginctl", "terminate-session", session] if session else ["loginctl", "terminate-user",
                                                                       getpass.getuser()]


@group.tool(destructive=True, title="Suspend, restart, shut down or log out",
            examples=("Put the PC to sleep", "Restart the computer"), requires=("power",))
def power(action: Literal["suspend", "restart", "shutdown", "logout"]) -> str:
    """Suspend, restart, shut down or log out of the PC (after a few seconds, so Orion can say so first)."""
    delay = ctx.config.system.power_delay_s
    command = POWER_COMMANDS[action] if action != "logout" else _logout_command()
    if which("systemd-run"):
        _run(["systemd-run", "--user", "--collect", f"--on-active={delay:g}s", "--timer-property=AccuracySec=100ms",
              *command])
    else:
        _spawn(["sh", "-c", f"sleep {delay:g}; exec {shlex.join(command)}"])
    return f"{action.capitalize()} in {delay:g} seconds."

