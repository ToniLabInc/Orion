"""What this machine has, so Orion works wherever he runs: a desktop PC, a laptop, a server, a container.

``detect`` looks once (a few milliseconds: environment variables, programs on the PATH, the sensors psutil sees) and
says what is here. Each tool may declare what it needs (``Group.tool(requires=...)``); the hub offers the model only
the tools this machine can run, and the Abilities page shows the others with the reason. Nothing is assumed: a server
in Docker has no desktop, sound server or battery, so opening apps, the volume, the screen lock and power actions are
not offered there, and the rest works the same.

The needs a tool may declare (``NEEDS``):
- "desktop": a graphical session to open apps and files in (Wayland or X11, with xdg-open);
- "dbus": the desktop's session bus (media players over MPRIS, the screen lock);
- "sound": a sound server whose volume can be changed (PipeWire's wpctl, or PulseAudio's pactl);
- "power": systemd's logind outside a container (suspend, restart, shut down, log out);
- "battery", "temperatures", "nvidia": the sensors (the watchers use them);
- "clipboard": a program that reads and writes this machine's clipboard;
- "screen": a way to capture this machine's screen;
- "restic": the restic program, which keeps an encrypted copy of his backups off this machine.

How his commands are kept in bounds here is a fact about the machine too (``HostProfile.sandbox``), though no tool needs
it: "bwrap" where bubblewrap can make a sandbox, "container" inside a container (which is the boundary), "" for
neither (``orion.sandbox``).
"""

import functools
import os
import shutil
import sys
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final, Literal

from orion.base import JSON

type Need = Literal["desktop", "dbus", "sound", "power", "battery", "temperatures", "nvidia", "clipboard", "screen",
                    "restic"]

NEEDS: Final[dict[str, str]] = {  # what each need means, as the Abilities page says what is missing
    "desktop": "a desktop session (Wayland or X11)",
    "dbus": "the desktop's D-Bus",
    "sound": "a sound server (PipeWire or PulseAudio)",
    "power": "systemd on the machine itself (not in a container)",
    "battery": "a battery",
    "temperatures": "temperature sensors",
    "nvidia": "an NVIDIA graphics card",
    "clipboard": "a clipboard program (wl-clipboard on Wayland, xclip or xsel on X11)",
    "screen": "a way to capture the screen",
    "restic": "restic (it keeps an encrypted copy of the backups off this machine)",
}
CACHE_S: Final = 60.0  # a profile is looked at again after this long (a desktop session may start after Orion)


@dataclass(frozen=True, slots=True)
class HostProfile:
    """What this machine has (see the module docstring).

    Attributes:
        platform: ``sys.platform`` ("linux", "darwin", "win32"...).
        container: Orion runs inside a container (Docker, Podman).
        session: the graphical session: "wayland", "x11", or "" for none.
        desktop: the desktop's name (``XDG_CURRENT_DESKTOP``, e.g. "ubuntu:GNOME"), "" when unknown.
        dbus: a session bus can be reached.
        opener: the program that opens files and links ("xdg-open", "open"), "" for none.
        sound: the volume's program ("wpctl", "pactl"), "" for none.
        power: systemd's logind can suspend and power off this machine.
        battery: a battery is reported.
        temperatures: temperature sensors are reported.
        nvidia: an NVIDIA GPU answers through NVML.
        clipboard: how this machine's clipboard is read and written ("wl", "xclip", "xsel", "darwin", "win32"),
            "" for none.
        screen: how this machine's screen is captured ("portal", "grim", "spectacle", "mss"), "" for none.
        sandbox: how commands are kept in bounds: "bwrap", "container" or "" (see the module docstring).
        restic: the path of the restic program, "" for none.
    """

    platform: str
    container: bool
    session: str
    desktop: str
    dbus: bool
    opener: str
    sound: str
    power: bool
    battery: bool
    temperatures: bool
    nvidia: bool
    clipboard: str
    screen: str
    sandbox: str = ""
    restic: str = ""

    def has(self, need: str) -> bool:
        """Whether this machine meets a tool's need (an unknown need is never met)."""
        match need:
            case "desktop":
                return bool(self.session and self.opener)
            case "dbus":
                return self.dbus
            case "sound":
                return bool(self.sound)
            case "power":
                return self.power
            case "battery":
                return self.battery
            case "temperatures":
                return self.temperatures
            case "nvidia":
                return self.nvidia
            case "clipboard":
                return bool(self.clipboard)
            case "screen":
                return bool(self.screen)
            case "restic":
                return bool(self.restic)
        return False

    def missing(self, needs: Iterable[str]) -> str:
        """What a tool needs that this machine lacks, in words ("" when it has everything).

        Args:
            needs: the tool's needs.

        Returns:
            E.g. "a desktop session (Wayland or X11)".
        """
        return "; ".join(NEEDS.get(need, need) for need in needs if not self.has(need))

    def as_dict(self) -> dict[str, JSON]:
        """The profile for the HUD (Settings, This machine)."""
        return dict(asdict(self))


@functools.cache
def _uid_dir() -> Path:
    """This user's runtime folder (``XDG_RUNTIME_DIR``, else /run/user/<uid>)."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime)
    getuid = getattr(os, "getuid", None)
    return Path(f"/run/user/{getuid()}") if getuid is not None else Path()


def in_container() -> bool:
    """Whether Orion runs inside a container (Docker's and Podman's marker files, or the init process's cgroup)."""
    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        return True
    try:
        cgroup = Path("/proc/1/cgroup").read_text(errors="replace")
    except OSError:
        return False
    return any(word in cgroup for word in ("docker", "containerd", "kubepods", "libpod"))


def _session() -> str:
    """The graphical session: "wayland", "x11" or ""."""
    kind = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if kind in ("wayland", "x11"):
        return kind
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    return "x11" if os.environ.get("DISPLAY") else ""


def _dbus() -> bool:
    """Whether a session bus can be reached."""
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    if address.startswith("unix:path="):
        return Path(address.removeprefix("unix:path=").split(",")[0]).exists()
    return bool(address) or (_uid_dir() / "bus").exists()


def _opener(platform: str, session: str) -> str:
    """The program that opens files and links here."""
    if platform == "darwin":
        return "open"
    if platform == "win32":
        return "start"
    return "xdg-open" if session and shutil.which("xdg-open") else ""


def _sound(platform: str) -> str:
    """The program the volume is read and changed with."""
    if platform != "linux":
        return ""
    for program in ("wpctl", "pactl"):
        if shutil.which(program) and (program != "wpctl" or (_uid_dir() / "pipewire-0").exists()):
            return program
    return ""


def _power(platform: str, container: bool) -> bool:
    """Whether systemd's logind can act on this machine."""
    return platform == "linux" and not container and Path("/run/systemd/system").is_dir() and bool(
        shutil.which("systemctl"))


def _sensors() -> tuple[bool, bool]:
    """(battery, temperatures): what psutil reports."""
    import psutil

    try:
        battery = psutil.sensors_battery() is not None
    except Exception:  # noqa: BLE001 (a sensor read that fails means no sensor)
        battery = False
    reader = getattr(psutil, "sensors_temperatures", None)
    try:
        temperatures = bool(reader()) if reader is not None else False
    except Exception:  # noqa: BLE001
        temperatures = False
    return battery, temperatures


def _nvidia() -> bool:
    """Whether an NVIDIA GPU answers through NVML."""
    try:
        import pynvml

        pynvml.nvmlInit()
        try:
            return pynvml.nvmlDeviceGetCount() > 0
        finally:
            pynvml.nvmlShutdown()
    except Exception:  # noqa: BLE001 (no driver, no library, no GPU: all mean none)
        return False


def _clipboard(platform: str, session: str) -> str:
    """How this machine's clipboard is read and written (pyperclip does it, through these programs on Linux)."""
    if platform in ("darwin", "win32"):
        return platform
    if session == "wayland" and shutil.which("wl-copy") and shutil.which("wl-paste"):
        return "wl"
    if session in ("x11", "wayland") and os.environ.get("DISPLAY"):  # X11, or Xwayland under Wayland
        for program in ("xclip", "xsel"):
            if shutil.which(program):
                return program
    return ""


def _screen(platform: str, session: str, desktop: str, dbus: bool) -> str:
    """How this machine's screen is captured: the desktop's own way on Wayland (the screenshot portal, which asks
    once; KDE's Spectacle; grim on wlroots compositors), mss on X11, macOS and Windows."""
    if platform in ("darwin", "win32"):
        return "mss"
    if not session:
        return ""
    if session == "x11":
        return "mss"
    names = desktop.upper()
    if "KDE" in names and shutil.which("spectacle"):
        return "spectacle"
    if "GNOME" not in names and "KDE" not in names and shutil.which("grim"):
        return "grim"  # wlroots compositors: Sway, Hyprland, river...
    return "portal" if dbus else ""


def _sandbox(container: bool) -> str:
    """How commands are kept in bounds here: the container itself, bubblewrap when it can make a sandbox, or nothing."""
    if container:
        return "container"
    from orion import sandbox

    return "bwrap" if sandbox.status().works else ""


def detect() -> HostProfile:
    """Look at this machine now (see the module docstring).

    Returns:
        What it has.
    """
    platform = sys.platform
    container = in_container()
    session = _session()
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "")
    dbus = _dbus()
    battery, temperatures = _sensors()
    return HostProfile(platform=platform, container=container, session=session, desktop=desktop, dbus=dbus,
                       opener=_opener(platform, session), sound=_sound(platform), power=_power(platform, container),
                       battery=battery, temperatures=temperatures, nvidia=_nvidia(),
                       clipboard=_clipboard(platform, session), screen=_screen(platform, session, desktop, dbus),
                       sandbox=_sandbox(container), restic=shutil.which("restic") or "")


_cached: tuple[float, HostProfile] | None = None


def profile(*, fresh: bool = False) -> HostProfile:
    """This machine's profile, looked at again after ``CACHE_S``.

    Args:
        fresh: look again now.

    Returns:
        The profile.
    """
    global _cached
    now = time.monotonic()
    if fresh or _cached is None or now - _cached[0] > CACHE_S:
        _cached = (now, detect())
    return _cached[1]
