"""host: what a machine has, read from its environment and programs, and the tools the hub offers on it (a desktop
PC, a server, a container: nothing assumed)."""

import asyncio
from pathlib import Path

import pytest

from orion import host
from orion.base import CallContext, GroupInfo
from orion.config import default_config
from orion.mcp import MCPHub, OpTrace
from orion.tools import Group, build_server


def machine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, env: dict[str, str], programs: set[str],
            platform: str = "linux", container: bool = False) -> host.HostProfile:
    """The profile of a pretend machine: these variables, these programs on the PATH, no sensors."""
    for name in ("XDG_SESSION_TYPE", "WAYLAND_DISPLAY", "DISPLAY", "XDG_CURRENT_DESKTOP", "DBUS_SESSION_BUS_ADDRESS"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    host._uid_dir.cache_clear()
    monkeypatch.setattr(host.sys, "platform", platform)
    monkeypatch.setattr(host.shutil, "which", lambda name: f"/usr/bin/{name}" if name in programs else None)
    monkeypatch.setattr(host, "in_container", lambda: container)
    monkeypatch.setattr(host, "_sensors", lambda: (False, False))
    monkeypatch.setattr(host, "_nvidia", lambda: False)
    monkeypatch.setattr(host, "_power", lambda platform, container: not container and "systemctl" in programs)
    monkeypatch.setattr(host, "_sandbox", lambda container: "container" if container else "bwrap")  # (a probe of its own)
    return host.detect()


def test_a_gnome_wayland_desktop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "bus").touch()
    (tmp_path / "pipewire-0").touch()
    p = machine(monkeypatch, tmp_path, env={"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0",
                                            "XDG_CURRENT_DESKTOP": "ubuntu:GNOME"},
                programs={"xdg-open", "wpctl", "wl-copy", "wl-paste", "systemctl"})
    assert (p.session, p.opener, p.sound, p.clipboard, p.screen) == ("wayland", "xdg-open", "wpctl", "wl", "portal")
    assert all(p.has(need) for need in ("desktop", "dbus", "sound", "power", "clipboard", "screen"))
    assert p.missing(["desktop", "battery"]) == host.NEEDS["battery"]


def test_a_server_in_a_container_has_none_of_the_desktop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    p = machine(monkeypatch, tmp_path, env={}, programs={"systemctl"}, container=True)
    assert (p.session, p.opener, p.sound, p.clipboard, p.screen, p.dbus, p.power) == ("", "", "", "", "", False, False)
    assert p.missing(["desktop", "sound"]) == f"{host.NEEDS['desktop']}; {host.NEEDS['sound']}"
    assert p.sandbox == "container" and p.as_dict()["sandbox"] == "container"  # the container is the boundary


def test_how_commands_are_kept_in_bounds_is_a_fact_about_the_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    from orion import sandbox

    assert host._sandbox(True) == "container"  # no probe inside a container: bubblewrap couldn't run there anyway
    for works, said in ((True, "bwrap"), (False, "")):
        monkeypatch.setattr(sandbox, "status", lambda *, fresh=False, works=works: sandbox.Status("/usr/bin/bwrap", works, 8, ""))
        assert host._sandbox(False) == said


def test_restic_is_a_feature_of_the_machine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The encrypted off-site copy of the backups needs the restic program; a machine without it says so in words."""
    without = machine(monkeypatch, tmp_path, env={}, programs=set())
    with_it = machine(monkeypatch, tmp_path, env={}, programs={"restic"})
    assert (without.restic, with_it.restic) == ("", "/usr/bin/restic")
    assert not without.has("restic") and with_it.has("restic")
    assert without.missing(["restic"]) == host.NEEDS["restic"] and with_it.missing(["restic"]) == ""


def test_other_desktops_and_systems(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    kde = machine(monkeypatch, tmp_path, env={"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "KDE",
                                              "DISPLAY": ":0"},
                  programs={"xdg-open", "pactl", "spectacle", "xclip"})
    assert (kde.sound, kde.screen, kde.clipboard) == ("pactl", "spectacle", "xclip")  # Xwayland's clipboard
    sway = machine(monkeypatch, tmp_path, env={"WAYLAND_DISPLAY": "wayland-1", "XDG_CURRENT_DESKTOP": "sway"},
                   programs={"xdg-open", "grim"})
    assert sway.screen == "grim"
    x11 = machine(monkeypatch, tmp_path, env={"DISPLAY": ":0"}, programs={"xdg-open", "xsel"})
    assert (x11.session, x11.clipboard, x11.screen) == ("x11", "xsel", "mss")
    mac = machine(monkeypatch, tmp_path, env={}, programs=set(), platform="darwin")
    assert (mac.opener, mac.clipboard, mac.screen, mac.sound) == ("open", "darwin", "mss", "")


def test_the_hub_offers_only_what_this_machine_can_run(monkeypatch: pytest.MonkeyPatch) -> None:
    group = Group(GroupInfo("demo", "Demo", "A demo group."))

    @group.tool(title="Open an app", requires=("desktop",))
    def open_app(name: str) -> str:
        """Open an app."""
        return f"Opened {name}."

    @group.tool(title="Run a command")
    def run(command: str) -> str:
        """Run a command."""
        return "ok"

    server = host.HostProfile("linux", True, "", "", False, "", "", False, False, False, False, "", "")
    monkeypatch.setattr("orion.mcp.profile", lambda: server)
    cfg = default_config()
    hub = MCPHub(cfg, {}, ops=OpTrace(lambda kind, **data: None, cfg.hud, recent=10), core=build_server([group]),
                 core_groups={"demo": group.info})

    async def go() -> tuple[set[str], str, list[dict[str, object]]]:
        await hub.configure(wait_s=5)
        offered = set(hub.tools())
        refused = await hub.call("demo", "open_app", {"name": "firefox"}, CallContext("hud"))
        status = [dict(t) for s in hub.status() for t in s["tools"]]
        await hub.stop()
        return offered, refused, status

    offered, refused, status = asyncio.run(go())
    assert offered == {"run"}
    assert refused == f"Error: open_app can't work on this machine: it needs {host.NEEDS['desktop']}."
    assert {t["name"]: t["unavailable"] for t in status} == {"open_app": host.NEEDS["desktop"], "run": ""}
