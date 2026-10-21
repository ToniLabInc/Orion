"""The sandbox: what a command, or a connector nobody reviewed, may see and do on this PC (G3, G4).

A program in the sandbox runs under bubblewrap, as the user but in a world of its own:

- the PC is read-only to it: the system and the home folder as they are, except the folders the user lets it write in
  (``safety.sandbox_writable``) and, for a connector, its own folder; Orion's own code is read-only even inside those;
- what is secret is not there: keys and keyrings, browser profiles, cloud and container logins, Claude's and Discord's
  own settings (``safety.sandbox_hidden``), and Orion's data folder, his vault and his settings (always), except his
  skills, which it may read to run their scripts;
- an environment of its own: only what the allow-list names (``orion.secrets.inherited``): no token of Orion's, and no
  session bus, so no keyring and no desktop; ``/tmp`` and ``/run`` are empty, so no X, Wayland, D-Bus or Docker socket;
- processes of its own (it sees none of Orion's, so not their environment either), killed with Orion and with the
  command's time limit; no terminal to type into; no further user namespaces; no sudo (no new privileges);
- the network, when ``safety.sandbox_network`` says so, but with the doors to Orion's own services shut: Landlock
  (``orion.landlock``, run first inside) keeps it from connecting to the HUD's port (whoever connects there is taken
  for the user), the browser's or the model's, and to the desktop's abstract sockets. A kernel too old for that (Linux
  6.12) can't give a networked sandbox those guarantees: it refuses to run one, and the network can be turned off
  (``--unshare-net``: no network, and nothing shared with the PC at all).

``wrap`` builds the command line (pure: the tests read it), ``run`` runs a program with it and keeps what it prints,
``status`` says whether this machine can, and ``jail_for`` makes the jail the settings describe. Inside a container
the container is the boundary: ``host.profile().sandbox`` says "container" and commands run in it with the clean
environment alone. ``orion.tools.system.run_command`` and ``orion.mcp`` are its users; document conversion
(``orion.documents``) is the third: no network, one file read-only.
"""

import contextlib
import logging
import os
import selectors
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

from orion import landlock
from orion.base import OrionError

if TYPE_CHECKING:
    from orion.config import Config

log = logging.getLogger("orion.sandbox")

PROBE_TTL_S: Final = 60.0  # what this machine can do is looked at again after this long (bwrap may be installed)
PROBE_TIMEOUT_S: Final = 10.0
READ_CHUNK: Final = 8192  # bytes read from a program's output at a time
LOCALHOST: Final = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})
# Where a program that runs later, outside the sandbox, would pick up something planted: startup files, autostart and
# user services. No folder that is, holds or lies in one may be written by a command (``safety.sandbox_writable`` is
# checked): the home folder itself, then, is never writable, only the folders in it that hold none.
AUTORUN: Final = ("~/.bashrc", "~/.profile", "~/.bash_profile", "~/.bash_login", "~/.bash_logout", "~/.zshrc",
                  "~/.zprofile", "~/.zshenv", "~/.zlogin", "~/.xinitrc", "~/.xprofile", "~/.xsessionrc",
                  "~/.pam_environment", "~/.config/autostart", "~/.config/systemd", "~/.config/environment.d",
                  "~/.config/fish", "~/.local/bin", "~/.local/share/applications")


class SandboxError(OrionError):
    """The sandbox can't be made on this machine; the message says why and what to do."""


@dataclass(frozen=True, slots=True)
class Status:
    """What this machine can do.

    Attributes:
        bwrap: the path of bubblewrap, "" when it isn't installed.
        works: whether a sandbox started here (user namespaces may be off, or blocked by AppArmor).
        landlock: the kernel's Landlock version (0: none).
        why: what is missing, in words ("" when a sandbox works).
    """

    bwrap: str
    works: bool
    landlock: int
    why: str

    @property
    def network(self) -> bool:
        """Whether a sandbox may have the network: Landlock closes Orion's own doors only from version 6 on."""
        return self.landlock >= landlock.NEEDS_SCOPES


@dataclass(frozen=True, slots=True)
class Jail:
    """What a sandboxed program may see and do (see the module docstring).

    Attributes:
        write: folders it may write in (those that exist; a connector's own folder, say).
        readonly: paths read-only even inside a folder it may write in (Orion's own code).
        hidden: paths it can't see (those that exist).
        read: paths made visible again, read-only, inside hidden ones (Orion's skills).
        network: whether it has the network.
        blocked: the TCP ports of Orion's own services it may never connect to (with the network on).
        memory_mb: the most memory it may use (0: no limit).
    """

    write: tuple[Path, ...] = ()
    readonly: tuple[Path, ...] = ()
    hidden: tuple[Path, ...] = ()
    read: tuple[Path, ...] = ()
    network: bool = True
    blocked: frozenset[int] = frozenset()
    memory_mb: int = 0


@dataclass(frozen=True, slots=True)
class Completed:
    """What a program did.

    Attributes:
        code: its exit status (None when it was stopped at its time limit).
        output: what it printed (stdout and stderr), cut to its beginning and its end when it printed a lot.
        cut: whether the middle was left out.
        timed_out: whether it ran out of time (its process group was killed).
    """

    code: int | None
    output: bytes
    cut: bool
    timed_out: bool


# ---------------------------------------------------------------------------------------------------- this machine
_status: tuple[float, Status] | None = None


def status(*, fresh: bool = False) -> Status:
    """Whether a sandbox can be made here, looked at again after ``PROBE_TTL_S`` (a trial one is started: about 10 ms).

    Args:
        fresh: look again now.
    """
    global _status
    now = time.monotonic()
    if fresh or _status is None or now - _status[0] > PROBE_TTL_S:
        _status = (now, _probe())
    return _status[1]


def _probe() -> Status:
    """Start a trial sandbox with the options ``wrap`` always uses."""
    version = landlock.abi()
    path = shutil.which("bwrap") or ""
    if not path:
        return Status("", False, version, "bubblewrap isn't installed (sudo apt install bubblewrap)")
    trial = Jail(network=False)
    try:
        done = subprocess.run(_command(path, ["/bin/true"], trial, env={}, cwd=Path("/")), capture_output=True, text=True,
                              timeout=PROBE_TIMEOUT_S, check=False, env={})
    except (OSError, subprocess.TimeoutExpired) as e:
        return Status(path, False, version, f"bubblewrap didn't start: {e}")
    if done.returncode:
        reason = done.stderr.strip().splitlines()[-1:] or [f"exit status {done.returncode}"]
        return Status(path, False, version, f"bubblewrap can't make a sandbox here ({reason[0]}): this kernel or "
                                            "AppArmor may forbid user namespaces")
    return Status(path, True, version, "")


def require(jail: Jail) -> str:
    """The path of bubblewrap, once a sandbox like this one can be made here.

    Raises:
        SandboxError: it can't, and why (no bubblewrap, no user namespaces, a kernel too old for the network's doors).
    """
    found = status()
    if not found.works:
        raise SandboxError(f"Commands run in a sandbox, and this machine can't make one: {found.why}. Fix that, or "
                           "turn the sandbox off in Settings, Safety.")
    if jail.network and not found.network:
        raise SandboxError(f"The sandbox can't have the network on this machine: its kernel's Landlock is version "
                           f"{found.landlock} and keeping the sandbox away from Orion's own services needs "
                           f"{landlock.NEEDS_SCOPES} (Linux 6.12). Turn the network off for commands in Settings, "
                           "Safety, or update the kernel.")
    return found.bwrap


# ---------------------------------------------------------------------------------------------------- the command line
def wrap(argv: Sequence[str], jail: Jail, *, env: Mapping[str, str], cwd: Path) -> list[str]:
    """The command line that runs a program in a jail (see ``run``; pure but for looking at which paths exist).

    Args:
        argv: the program and its arguments.
        jail: what it may see and do.
        env: its whole environment (nothing else is inherited).
        cwd: its working folder.

    Returns:
        The bubblewrap command line.

    Raises:
        SandboxError: no sandbox like this one can be made here.
    """
    return _command(require(jail), argv, jail, env=env, cwd=cwd)


def _command(bwrap: str, argv: Sequence[str], jail: Jail, *, env: Mapping[str, str], cwd: Path) -> list[str]:
    """The bubblewrap command line (``wrap`` without the check that it can run).

    Mounts are made in the order given and later ones cover earlier ones: the PC read-only, then what may be written,
    then what is read-only after all, then what is hidden, then what is shown again inside it.
    """
    # (--unshare-all only tries the user namespace: asked for outright, no namespace means no sandbox, not a weaker one)
    flags = ["--die-with-parent", "--new-session", "--unshare-all", "--unshare-user", "--disable-userns", "--clearenv"]
    if jail.network:
        flags.append("--share-net")
    for name, value in sorted(env.items()):
        flags += ["--setenv", name, value]
    flags += ["--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp", "--tmpfs", "/var/tmp",
              "--tmpfs", "/run"]
    if (dns := Path("/run/systemd/resolve")).is_dir():  # /etc/resolv.conf points here on most desktops
        flags += ["--ro-bind", str(dns), str(dns)]
    writable = _existing(jail.write)
    flags += [part for path in writable for part in ("--bind", path, path)]
    flags += [part for path in _existing(jail.readonly)  # only inside a writable folder: elsewhere it is already
              if any(Path(path).is_relative_to(folder) for folder in writable) for part in ("--ro-bind", path, path)]
    hidden = _uncovered(_existing(jail.hidden))
    for path in hidden:
        flags += ["--tmpfs", path] if Path(path).is_dir() else ["--dev-bind", "/dev/null", path]  # (empty and unwritable)
    flags += [part for path in writable  # a folder it was given inside a hidden one (a connector's, in his data folder)
              if any(Path(path).is_relative_to(folder) for folder in hidden) for part in ("--bind", path, path)]
    flags += [part for path in _existing(jail.read) for part in ("--ro-bind", path, path)]
    flags += ["--chdir", str(cwd), "--"]
    inside = [sys.executable, "-I", "-S", landlock.__file__]
    if jail.memory_mb:
        inside += ["--memory-mb", str(jail.memory_mb)]
    if jail.network:
        inside += ["--block", ",".join(map(str, sorted(jail.blocked)))]
    return [bwrap, *flags, *inside, "--", *argv]


def _uncovered(paths: Sequence[str]) -> list[str]:
    """The paths that aren't inside another folder of the list (hiding that one hides them too)."""
    folders = [Path(path) for path in paths if Path(path).is_dir()]
    return [path for path in paths if not any(Path(path) != folder and Path(path).is_relative_to(folder)
                                               for folder in folders)]


def _existing(paths: Sequence[Path]) -> list[str]:
    """The paths that exist (each once, a link followed too: hiding a link would leave what it points to)."""
    found: dict[str, None] = {}
    for path in paths:
        for candidate in (path, path.resolve()):
            if candidate.exists():
                found[str(candidate)] = None
    return list(found)


# ---------------------------------------------------------------------------------------------------- running
def run(argv: Sequence[str], *, jail: Jail | None, env: Mapping[str, str], cwd: Path, timeout_s: float,
        keep_bytes: int) -> Completed:
    """Run a program (in a jail, or as it is when ``jail`` is None) and keep what it prints.

    The program has no input, its own process group, and `timeout_s` to finish: then everything it started is killed.
    Output beyond twice ``keep_bytes`` is cut to its first and last ``keep_bytes``.

    Args:
        argv: the program and its arguments.
        jail: what it may see and do; None: nothing is confined (the environment is still only what is given).
        env: its whole environment.
        cwd: its working folder.
        timeout_s: how long it may run.
        keep_bytes: how much of the start and of the end of its output is kept.

    Returns:
        What it did.

    Raises:
        SandboxError: a jail was asked for and none can be made here.
        OSError: the program (or bubblewrap) couldn't be started.
    """
    command = wrap(argv, jail, env=env, cwd=cwd) if jail is not None else list(argv)
    deadline = time.monotonic() + timeout_s
    output, cut = b"", False
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                          cwd=None if jail is not None else cwd, start_new_session=True,
                          env={} if jail is not None else dict(env)) as process:
        assert process.stdout is not None  # stdout=PIPE
        try:
            with selectors.DefaultSelector() as streams:
                streams.register(process.stdout, selectors.EVENT_READ)
                while streams.get_map():
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise subprocess.TimeoutExpired(command, timeout_s)
                    for key, _ in streams.select(left):
                        chunk = os.read(key.fd, READ_CHUNK)
                        if not chunk:
                            streams.unregister(key.fileobj)
                            continue
                        output += chunk
                        if len(output) > 2 * keep_bytes:
                            output, cut = output[:keep_bytes] + output[-keep_bytes:], True
            process.wait(timeout=max(0.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)  # killing only the first process could leave its children
            process.wait()
            return Completed(None, output, cut, timed_out=True)
    return Completed(process.returncode, output, cut, timed_out=False)


# ---------------------------------------------------------------------------------------------------- the settings' jails
def jail_for(cfg: "Config", config_file: Path, *, network: bool | None = None, write: Sequence[Path] | None = None,
             memory_mb: int | None = None) -> Jail:
    """The jail the settings describe: for a command (the defaults, ``safety.sandbox_*``), or for a connector (its own
    folder to write in, the network, no memory limit).

    Args:
        cfg: the settings.
        config_file: where they are kept (hidden, unless inside the data folder).
        network: whether it has the network (``safety.sandbox_network`` by default).
        write: the folders it may write in (``safety.sandbox_writable`` by default).
        memory_mb: its memory limit, 0 for none (``safety.sandbox_memory_mb`` by default).

    Returns:
        The jail.
    """
    from orion.config import ROOT

    safety = cfg.safety
    data, vault = cfg.data_path.resolve(), cfg.secrets_path.resolve()
    hidden = [Path(path).expanduser() for path in safety.sandbox_hidden] + [data, vault, config_file.resolve()]
    return Jail(
        write=tuple(Path(path).expanduser() for path in (safety.sandbox_writable if write is None else write)),
        readonly=(ROOT,), hidden=tuple(hidden),
        read=(data / "skills", data / "plugins"),
        network=safety.sandbox_network if network is None else network, blocked=local_ports(cfg),
        memory_mb=safety.sandbox_memory_mb if memory_mb is None else memory_mb)


def keeps_out(path: Path, jail: Jail, *, write: bool) -> str:
    """Why a file tool may not read or change a path, by the same rules a command in the sandbox lives by: "" when it
    may. What is hidden is not read; and not changed, nor are the folders shown read-only (his skills), nor his own
    code, nor a file that starts programs (``AUTORUN``).

    Args:
        path: what the tool was asked to touch.
        jail: the jail of the settings (``jail_for``).
        write: whether it would be changed.

    Returns:
        The reason, in words for the model.
    """
    found = path.resolve()

    def inside(places: Sequence[Path]) -> bool:
        return any(found.is_relative_to(place.resolve()) for place in places)

    if inside(jail.hidden) and not (inside(jail.read) and not write):
        return ("That is one of the places he keeps hidden (keys, sign-ins, his own data): the user can open it, he "
                "can't. They choose what is hidden in Settings, Safety.")
    autorun = [Path(each).expanduser() for each in AUTORUN]
    if write and (inside(jail.read) or inside(jail.readonly) or inside(autorun)):
        return ("He doesn't change that: it is part of Orion, or a file that starts programs (such as ~/.bashrc or "
                "~/.config/autostart). The user can edit it.")
    return ""


def local_ports(cfg: "Config") -> frozenset[int]:
    """The TCP ports of Orion's own services on this PC: the HUD's, the model server's, and those of the servers he
    reaches on this PC (the browser he drives): what a program in the sandbox may never connect to."""
    urls = [cfg.ollama.host] + [str(settings.get("url") or "") for settings in (cfg.mcp_servers or {}).values()
                                if isinstance(settings, dict)]
    ports = {cfg.server.port}
    for url in urls:
        parts = urlsplit(url)
        with contextlib.suppress(ValueError):
            if parts.hostname in LOCALHOST and parts.port:
                ports.add(parts.port)
    return frozenset(ports)
