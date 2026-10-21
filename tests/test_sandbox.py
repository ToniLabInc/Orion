"""sandbox: the command line it builds, what this machine can do, and (where bubblewrap works, in a fake home with a
planted SSH key) that a command can't read what is hidden, can't see Orion's environment, can write only where it
may, has no network or only the allowed doors, and leaves nothing running after its time."""

import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

import orion
from orion import config, landlock, sandbox
from orion.config import default_config
from orion.sandbox import Completed, Jail, SandboxError, Status

REAL_ROOT = Path(orion.__file__).resolve().parent.parent  # (config.ROOT is moved by the tests; this isn't)
ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
WORKS = sandbox.status().works
DOORS = sandbox.status().network
needs_bwrap = pytest.mark.skipif(not WORKS, reason="bubblewrap can't make a sandbox on this machine")
needs_doors = pytest.mark.skipif(not (WORKS and DOORS), reason="no bubblewrap, or a kernel without Landlock 6")
ARITY = {"--bind": 2, "--ro-bind": 2, "--dev-bind": 2, "--tmpfs": 1, "--dev": 1, "--proc": 1, "--setenv": 2, "--chdir": 1}


def sh(script: str, jail: Jail | None, home: Path, *, timeout: float = 30, keep: int = 4000,
       env: dict[str, str] | None = None) -> Completed:
    return sandbox.run(["bash", "-c", script], jail=jail, env={**ENV, "HOME": str(home), **(env or {})}, cwd=home,
                       timeout_s=timeout, keep_bytes=keep)


def said(done: Completed) -> str:
    return done.output.decode(errors="replace")


@pytest.fixture
def home() -> Iterator[Path]:
    """A fake home outside /tmp (the sandbox's /tmp is private, so a home there would not be there) with a planted
    key and netrc, an autostart folder, folders to write in and one to only read."""
    folder = Path(tempfile.mkdtemp(prefix=".sandbox-test-", dir=REAL_ROOT))
    mine = folder / "home"
    for name in (".ssh", ".config/autostart", "Downloads", "Documents", "ro"):
        (mine / name).mkdir(parents=True)
    (mine / ".ssh" / "id_rsa").write_text("PLANTED-PRIVATE-KEY")
    (mine / ".netrc").write_text("machine example password PLANTED-NETRC")
    (mine / ".bashrc").write_text("# fake")
    (mine / "ro" / "a.txt").write_text("readable")
    try:
        yield mine
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def jail_of(home: Path, **changes: object) -> Jail:
    base = {"write": (home / "Downloads", home / "Documents"), "hidden": (home / ".ssh", home / ".netrc"),
            "network": False, "memory_mb": 1024}
    return Jail(**{**base, **changes})  # type: ignore[arg-type]  # (the keyword arguments are Jail's own)


# ---------------------------------------------------------------------------------------------------- the command line
def line_of(jail: Jail, env: dict[str, str] | None = None) -> list[str]:
    return sandbox._command("/usr/bin/bwrap", ["ls", "-l"], jail, env=env or {}, cwd=Path("/work"))


def groups(line: list[str]) -> list[tuple[str, ...]]:
    """The bubblewrap options, each with its arguments, in the order given."""
    flags, found, i = line[1:line.index("--")], [], 0
    while i < len(flags):
        found.append(tuple(flags[i:i + 1 + ARITY.get(flags[i], 0)]))
        i += 1 + ARITY.get(flags[i], 0)
    return found


def helper_of(line: list[str]) -> list[str]:
    """What runs first inside the sandbox: the interpreter, the helper and its options."""
    start = line.index("--") + 1
    return line[start:line.index("--", start)]


def test_the_command_line_is_a_clean_private_world(tmp_path: Path) -> None:
    line = line_of(Jail(network=False), {"B": "2", "A": "1"})
    options = groups(line)
    assert line[0] == "/usr/bin/bwrap" and line[-3:] == ["--", "ls", "-l"]
    for flag in ("--die-with-parent", "--new-session", "--unshare-all", "--unshare-user", "--disable-userns",
                 "--clearenv"):
        assert (flag,) in options  # (the user namespace asked for outright: no namespace, no sandbox)
    assert options.index(("--setenv", "A", "1")) < options.index(("--setenv", "B", "2")) and ("--share-net",) not in options
    assert [("--dev", "/dev"), ("--proc", "/proc"), ("--tmpfs", "/tmp"), ("--tmpfs", "/var/tmp"), ("--tmpfs", "/run"),
            ("--chdir", "/work")] == [g for g in options if g[0] in ("--dev", "--proc", "--tmpfs", "--chdir")][:6]
    assert options.index(("--clearenv",)) < options.index(("--ro-bind", "/", "/"))


def test_what_is_written_hidden_and_shown_again_is_mounted_in_that_order(tmp_path: Path) -> None:
    folder, hidden, secret, data = tmp_path / "w", tmp_path / "h", tmp_path / "s.txt", tmp_path / "d"
    for path in (folder, hidden, data / "skills"):
        path.mkdir(parents=True)
    secret.write_text("x")
    options = groups(line_of(Jail(write=(folder,), hidden=(hidden, secret, tmp_path / "missing", data),
                                  read=(data / "skills",))))
    order = [options.index(("--ro-bind", "/", "/")), options.index(("--bind", str(folder), str(folder))),
             options.index(("--tmpfs", str(hidden))), options.index(("--dev-bind", "/dev/null", str(secret))),
             options.index(("--tmpfs", str(data))), options.index(("--ro-bind", str(data / "skills"), str(data / "skills")))]
    assert order == sorted(order) and not any(str(tmp_path / "missing") in g for g in options)
    # (a file is hidden by the empty device: a bind of /dev/null opened with devices allowed, as bubblewrap's other
    # binds are not; the folder by an empty one)


def test_the_network_comes_with_the_doors_to_orions_services_shut() -> None:
    off, on = line_of(Jail(network=False, memory_mb=0)), line_of(Jail(network=True, blocked=frozenset({8931, 8765})))
    assert ("--share-net",) not in groups(off) and helper_of(off)[4:] == []  # no memory limit, no doors: nothing to say
    assert ("--share-net",) in groups(on) and helper_of(on)[4:] == ["--block", "8765,8931"]
    nothing = helper_of(line_of(Jail(network=True)))
    assert nothing[4:] == ["--block", ""]  # no port of his own to keep it from: the scopes still apply
    assert helper_of(line_of(Jail(network=False, memory_mb=512)))[1:] == ["-I", "-S", landlock.__file__,
                                                                          "--memory-mb", "512"]


def test_a_read_only_overlay_is_made_only_inside_a_folder_that_may_be_written(tmp_path: Path) -> None:
    writable, outside = tmp_path / "w", tmp_path / "o"
    for folder in (writable / "keep", outside):
        folder.mkdir(parents=True)
    options = groups(line_of(Jail(write=(writable,), readonly=(writable / "keep", outside))))
    assert ("--ro-bind", str(writable / "keep"), str(writable / "keep")) in options
    assert not any(str(outside) in g for g in options)  # it is read-only already


def test_a_hidden_folder_inside_a_hidden_folder_is_hidden_once(tmp_path: Path) -> None:
    outer, inner = tmp_path / "data", tmp_path / "data" / "vault"
    inner.mkdir(parents=True)
    options = groups(line_of(Jail(hidden=(inner, outer))))
    assert ("--tmpfs", str(outer)) in options and not any(g[-1] == str(inner) for g in options)
    assert sandbox._uncovered([str(outer), str(inner), str(tmp_path / "x")]) == [str(outer), str(tmp_path / "x")]


def test_a_folder_it_was_given_inside_a_hidden_one_is_shown_again_after_the_hiding(tmp_path: Path) -> None:
    data, own = tmp_path / "data", tmp_path / "data" / "connectors" / "x"
    own.mkdir(parents=True)
    options = groups(line_of(Jail(write=(own,), hidden=(data,))))
    hide, show = options.index(("--tmpfs", str(data))), len(options) - 1 - options[::-1].index(("--bind", str(own), str(own)))
    assert hide < show  # (a connector's own folder lives in his data folder, which is hidden)


def test_a_link_to_a_hidden_place_hides_what_it_points_to(tmp_path: Path) -> None:
    target = tmp_path / "keys"
    target.mkdir()
    (tmp_path / ".ssh").symlink_to(target)
    assert ("--tmpfs", str(target)) in groups(line_of(Jail(hidden=(tmp_path / ".ssh",))))


# ---------------------------------------------------------------------------------------------------- the settings' jail
def test_the_jail_the_settings_describe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = default_config().model_copy(deep=True)
    cfg.data_dir = str(tmp_path / "orion-data")
    cfg.server.port = 8765
    cfg.ollama.host = "http://localhost:11434"
    cfg.mcp_servers = {"browser": {"url": "http://127.0.0.1:8931/mcp"},
                       "ha": {"url": "http://homeassistant.local:8123/mcp"}, "mail": {"command": "uvx"},
                       "odd": {"url": "http://[::1]:9000/x"}}
    config_file = tmp_path / "elsewhere" / "config.yaml"
    jail = sandbox.jail_for(cfg, config_file)
    assert jail.blocked == {8765, 11434, 8931, 9000}  # his own services on this PC, not Home Assistant's
    assert tmp_path / ".ssh" in jail.hidden and tmp_path / ".config/gcloud" in jail.hidden
    assert {cfg.data_path.resolve(), cfg.secrets_path.resolve(), config_file.resolve()} <= set(jail.hidden)
    assert jail.write == (tmp_path / "Downloads", tmp_path / "Documents") and jail.network and jail.memory_mb == 2048
    assert jail.read == (cfg.data_path.resolve() / "skills", cfg.data_path.resolve() / "plugins")
    assert jail.readonly == (config.ROOT,)  # his own code: read-only even inside a folder that may be written
    mine = sandbox.jail_for(cfg, config_file, network=False, write=[tmp_path / "connectors" / "x"], memory_mb=0)
    assert not mine.network and mine.write == (tmp_path / "connectors" / "x",) and mine.memory_mb == 0


# ---------------------------------------------------------------------------------------------------- this machine
def test_what_this_machine_can_do_says_what_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)
    missing = sandbox.status(fresh=True)
    assert not missing.works and missing.bwrap == "" and "apt install bubblewrap" in missing.why
    with pytest.raises(SandboxError, match="can't make one: bubblewrap isn't installed"):
        sandbox.require(Jail(network=False))
    monkeypatch.undo()
    fresh = sandbox.status(fresh=True)
    assert fresh.works is WORKS and fresh.landlock == landlock.abi() and fresh.works == (not fresh.why)


def test_the_network_needs_a_kernel_that_can_shut_the_doors(monkeypatch: pytest.MonkeyPatch) -> None:
    old = Status("/usr/bin/bwrap", True, 4, "")
    monkeypatch.setattr(sandbox, "_status", (sandbox.time.monotonic() + 10**6, old))
    assert not old.network and sandbox.require(Jail(network=False)) == "/usr/bin/bwrap"  # no network: nothing to shut
    with pytest.raises(SandboxError, match=r"Landlock is version 4.*needs 6 \(Linux 6\.12\).*Turn the network off"):
        sandbox.require(Jail(network=True))
    broken = Status("/usr/bin/bwrap", False, 8, "user namespaces are off")
    monkeypatch.setattr(sandbox, "_status", (sandbox.time.monotonic() + 10**6, broken))
    with pytest.raises(SandboxError, match="user namespaces are off"):
        sandbox.wrap(["true"], Jail(network=False), env={}, cwd=Path("/"))


# ---------------------------------------------------------------------------------------------------- the helper
def helper(*args: str) -> Completed:
    return sandbox.run([sys.executable, "-I", "-S", landlock.__file__, *args], jail=None, env=ENV, cwd=Path("/"),
                       timeout_s=20, keep_bytes=2000)


def test_the_helper_applies_what_it_is_told() -> None:
    done = helper("--memory-mb", "256", "--", "bash", "-c", "ulimit -d; ulimit -c")
    assert said(done) == "262144\n0\n" and done.code == 0


@pytest.mark.parametrize("bad", [("--memry-mb", "1", "--", "true"), ("--memory-mb", "--", "true"), ("--",), ("true",),
                                 ("--memory-mb", "1", "true")])
def test_the_helper_refuses_what_it_does_not_understand(bad: tuple[str, ...]) -> None:
    """A protection that was asked for and not understood is never dropped quietly: the program doesn't run."""
    done = helper(*bad)
    assert done.code == 1 and "usage:" in said(done)


def test_the_helper_says_why_a_program_did_not_start() -> None:
    assert helper("--", "no-such-program").code == 127 and "No such file" in said(helper("--", "no-such-program"))
    assert helper("--", "/etc").code == 126


# ---------------------------------------------------------------------------------------------------- inside it
@needs_bwrap
def test_a_command_can_not_read_what_is_hidden_and_does_not_see_orions_environment(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "sk-ant-oat01-" + "Zx9Qm4Lp7R" * 4
    monkeypatch.setenv("ORION_SANDBOX_TEST_TOKEN", secret)
    with subprocess.Popen(["sleep", "30"], env={**os.environ}) as orion:  # (a process of his, with a token in its environment)
        read_orion = f"tr '\\0' '\\n' < /proc/{orion.pid}/environ | grep -c ORION_SANDBOX_TEST_TOKEN"
        free = said(sh(f"cat ~/.ssh/id_rsa; {read_orion}", None, home))  # as you could yourself, outside it
        out = said(sh(f"cat ~/.ssh/id_rsa; ls -A ~/.ssh | wc -l; wc -c < ~/.netrc; {read_orion}; cat ~/ro/a.txt; env",
                      jail_of(home), home))
        orion.kill()
    assert "PLANTED-PRIVATE-KEY" in free and free.rstrip().endswith("1"), free
    assert "PLANTED" not in out and secret not in out and "ORION_SANDBOX_TEST_TOKEN" not in out
    assert "id_rsa: No such file or directory" in out and "\n0\n0\n" in out  # the key isn't there; the netrc is empty
    assert f"/proc/{orion.pid}/environ: No such file or directory" in out  # none of his processes is there to read
    assert "readable" in out  # the rest of the home is there to read
    assert "HOME=" in out and "LANG=C.UTF-8" in out and "TOKEN" not in out and "DBUS" not in out


@needs_bwrap
def test_a_command_writes_only_where_it_may(home: Path) -> None:
    out = said(sh("echo a > ~/Downloads/new.txt && echo wrote-downloads; echo b > ~/Documents/n.txt && echo wrote-documents; "
                  "echo c > ~/ro/b.txt; echo d > ~/.bashrc; echo e > ~/.netrc; echo f > ~/.ssh/x; "
                  "mkdir ~/newdir; echo g > /usr/bin/x; echo h > /tmp/t && cat /tmp/t", jail_of(home), home))
    assert "wrote-downloads" in out and "wrote-documents" in out and out.rstrip().endswith("h")
    assert out.count("Read-only file system") >= 4 and (home / "Downloads" / "new.txt").read_text() == "a\n"
    assert not (home / "ro" / "b.txt").exists() and (home / ".bashrc").read_text() == "# fake"
    assert not (home / "newdir").exists() and (home / ".netrc").read_text().startswith("machine")
    assert not (home / ".ssh" / "x").exists() and not (home / "Downloads" / "t").exists()


@needs_bwrap
def test_his_own_code_stays_read_only_even_inside_a_folder_that_may_be_written(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = home / "Documents" / "Orion"
    (repo / "orion").mkdir(parents=True)
    (repo / "orion" / "policy.py").write_text("# the rules")
    monkeypatch.setattr(config, "ROOT", repo)
    monkeypatch.setenv("HOME", str(home))
    cfg = default_config().model_copy(deep=True)
    cfg.data_dir = str(home / "orion-data")
    jail = sandbox.jail_for(cfg, home / "orion-data" / "config.yaml", network=False)
    out = said(sh(f"echo mine > ~/Documents/mine.txt && echo wrote-beside; echo evil > {repo}/orion/policy.py; "
                  f"mkdir {repo}/orion/new; rm {repo}/orion/policy.py; cat {repo}/orion/policy.py", jail, home))
    assert "wrote-beside" in out and out.count("Read-only file system") == 3 and out.rstrip().endswith("# the rules")
    assert (repo / "orion" / "policy.py").read_text() == "# the rules" and not (repo / "orion" / "new").exists()


@needs_bwrap
def test_orions_own_folder_and_vault_are_hidden_but_his_skills_are_readable(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(home))
    data = home / "orion-data"
    for name in ("vault", "skills/demo", "memory", "plugins"):
        (data / name).mkdir(parents=True)
    (data / "vault" / "openrouter.api_key").write_text("PLANTED-VAULT-VALUE")
    (data / "config.yaml").write_text("secret: PLANTED-CONFIG")
    (data / "memory" / "USER.md").write_text("personal facts")
    (data / "skills" / "demo" / "run.sh").write_text("echo skill-script-ran")
    cfg = default_config().model_copy(deep=True)
    cfg.data_dir = str(data)
    jail = sandbox.jail_for(cfg, data / "config.yaml", network=False, write=[])
    out = said(sh(f"ls -A {data}; cat {data}/vault/openrouter.api_key {data}/config.yaml {data}/memory/USER.md 2>&1; "
                  f"bash {data}/skills/demo/run.sh; echo x > {data}/skills/demo/new && echo wrote-skill", jail, home))
    assert "PLANTED" not in out and "personal facts" not in out and "skill-script-ran" in out
    assert out.splitlines()[:2] == ["plugins", "skills"]  # all that is there: what he lets it read, and not a file more
    assert "wrote-skill" not in out and not (data / "skills" / "demo" / "new").exists()  # readable, not writable


@needs_bwrap
def test_no_network_means_no_network_and_nothing_shared_with_the_pc(home: Path) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    abstract = socket.socket(socket.AF_UNIX)
    abstract.bind("\0orion-test-" + os.urandom(4).hex())
    abstract.listen()
    name = abstract.getsockname().decode().lstrip("\0")
    try:
        script = (f"exec 3<>/dev/tcp/127.0.0.1/{listener.getsockname()[1]} && echo REACHED-LISTENER || echo no-listener; "
                  f"{shlex.quote(sys.executable)} -c \"import socket; s = socket.socket(socket.AF_UNIX); "
                  f"s.connect('\\0{name}'); print('REACHED-ABSTRACT')\" 2>&1 | tail -1; cat /proc/net/dev | tail -n +3 | cut -d: -f1")
        out = said(sh(script, jail_of(home, network=False), home))
        assert "REACHED" not in out and "no-listener" in out and out.split()[-1] == "lo"  # its own, empty loopback
        reached = said(sh(script, None, home))
        assert "REACHED-LISTENER" in reached and "REACHED-ABSTRACT" in reached  # outside it, both are there
    finally:
        listener.close()
        abstract.close()


@needs_doors
def test_with_the_network_on_orions_services_and_the_desktops_sockets_stay_out_of_reach(home: Path) -> None:
    hud, other = socket.socket(), socket.socket()
    for server in (hud, other):
        server.bind(("127.0.0.1", 0))
        server.listen()
    abstract = socket.socket(socket.AF_UNIX)
    abstract.bind("\0orion-test-" + os.urandom(4).hex())
    abstract.listen()
    name = abstract.getsockname().decode().lstrip("\0")
    ports = hud.getsockname()[1], other.getsockname()[1]
    try:
        script = (f"(exec 3<>/dev/tcp/127.0.0.1/{ports[0]}) 2>&1 | grep -o 'Permission denied' | head -1; "
                  f"(exec 3<>/dev/tcp/127.0.0.1/{ports[1]}) 2>/dev/null && echo reached-other; "
                  f"{shlex.quote(sys.executable)} -c \"import socket; s = socket.socket(socket.AF_UNIX); "
                  f"s.connect('\\0{name}'); print('REACHED-ABSTRACT')\" 2>&1 | tail -1; "
                  "getent hosts localhost > /dev/null && echo names-resolve")
        out = said(sh(script, jail_of(home, network=True, blocked=frozenset({ports[0]})), home))
        assert out.splitlines()[0] == "Permission denied"  # the HUD's port: closed to it, by the kernel
        assert "reached-other" in out  # any other service is as it was (a dev server it started itself, say)
        assert "REACHED-ABSTRACT" not in out and "PermissionError" in out  # the desktop's abstract sockets: closed too
        assert "names-resolve" in out
    finally:
        for server in (hud, other, abstract):
            server.close()


@needs_bwrap
def test_a_command_gets_no_privileges_no_sessions_and_a_limit_on_memory(home: Path) -> None:
    out = said(sh("sudo -n true 2>&1 | head -1; unshare -r true 2>&1 | head -1; ls /run/user 2>&1 | head -1; "
                  "ls /tmp/.X11-unix 2>&1 | head -1; ls /proc | grep -c '^[0-9]'; "
                  f"{shlex.quote(sys.executable)} -c 'x = bytearray(1500 * 1024 * 1024)' 2>&1 | tail -1", jail_of(home), home))
    assert "no new privileges" in out and "unshare failed" in out
    assert out.count("No such file or directory") == 2 and "MemoryError" in out
    assert int(out.splitlines()[-2]) <= 8  # its own processes: none of Orion's, so not their environment either


@needs_bwrap
def test_what_it_started_is_gone_when_its_time_is(home: Path) -> None:
    done = sh("sleep 3011 & sleep 3011", jail_of(home), home, timeout=1.0)
    assert done.timed_out and done.code is None
    survivors = subprocess.run(["pgrep", "-fc", "^sleep 3011$"], capture_output=True, text=True, check=False)
    assert survivors.stdout.strip() == "0"  # killed with the sandbox, though they weren't in its process group


@needs_bwrap
def test_a_long_output_is_cut_to_its_start_and_end_and_a_failure_is_its_exit_status(home: Path) -> None:
    done = sh("seq 1 200000; exit 3", jail_of(home), home, keep=500)
    text = said(done)
    assert done.cut and done.code == 3 and len(done.output) <= 1000
    assert text.startswith("1\n2\n") and text.rstrip().endswith("200000")


def test_a_program_runs_without_a_jail_with_the_environment_it_is_given(tmp_path: Path) -> None:
    done = sandbox.run(["bash", "-c", "echo $ONLY; echo ${SECRET:-none}; sleep 5"], jail=None,
                       env={**ENV, "ONLY": "mine"}, cwd=tmp_path, timeout_s=0.8, keep_bytes=100)
    assert done.timed_out and said(done) == "mine\nnone\n"
    assert sandbox.run(["true"], jail=None, env=ENV, cwd=tmp_path, timeout_s=5, keep_bytes=10) == Completed(
        0, b"", False, False)
    with pytest.raises(OSError):
        sandbox.run(["/no/such/program"], jail=None, env=ENV, cwd=tmp_path, timeout_s=5, keep_bytes=10)
