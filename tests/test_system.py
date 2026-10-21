"""tools/system: run_command's sandbox and clean environment, the programs he starts for the desktop, and the places the
file tools won't read or change."""

import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

import orion
from orion import config, sandbox, tools
from orion.host import HostProfile
from orion.tools import CallInfo, ctx, system

REAL_ROOT = Path(orion.__file__).resolve().parent.parent
WORKS = sandbox.status().works
needs_bwrap = pytest.mark.skipif(not WORKS, reason="bubblewrap can't make a sandbox on this machine")
SECRET = "sk-ant-oat01-" + "Zx9Qm4Lp7R" * 4


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A fake home outside /tmp, with a planted key, as the sandbox's tests have; the tools see it as theirs."""
    folder = Path(tempfile.mkdtemp(prefix=".sandbox-test-", dir=REAL_ROOT))
    mine = folder / "home"
    for name in (".ssh", ".config/autostart", "Downloads", "Documents"):
        (mine / name).mkdir(parents=True)
    (mine / ".ssh" / "id_rsa").write_text("PLANTED-PRIVATE-KEY")
    (mine / "Documents" / "note.txt").write_text("a note")
    monkeypatch.setenv("HOME", str(mine))
    monkeypatch.setattr(system, "HOME", mine)
    monkeypatch.setattr(ctx, "_path", folder / "data" / "config.yaml")
    monkeypatch.setattr(ctx, "_settings", None)
    cfg = ctx.config
    cfg.data_dir = str(folder / "data")
    cfg.safety.sandbox_network = False  # (the doors to Orion's services are the sandbox's own tests)
    cfg.safety.sandbox_writable = [str(mine / "Downloads")]
    try:
        yield mine
    finally:
        shutil.rmtree(folder, ignore_errors=True)


@pytest.fixture
def brain(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A call the hub made for the model (outside one, a call counts as another app's)."""
    token = tools._call.set(CallInfo("brain", None))
    yield
    tools._call.reset(token)


@needs_bwrap
def test_a_command_runs_in_the_sandbox_with_no_token_and_no_key(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", SECRET)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("LC_TIME", "el_GR.UTF-8")
    out = system.run_command("env | sort; cat ~/.ssh/id_rsa; echo made > ~/Downloads/f.txt && echo wrote; echo x > ~/y.txt")
    assert out.startswith("exit code ")
    assert SECRET not in out and "TOKEN" not in out and "PLANTED" not in out and "DISPLAY" not in out
    assert "LC_TIME=el_GR.UTF-8" in out and f"HOME={home}" in out and "SUDO_ASKPASS=/bin/false" in out
    assert "id_rsa: No such file" in out and "wrote" in out and "Read-only file system" in out
    assert (home / "Downloads" / "f.txt").exists() and not (home / "y.txt").exists()


def test_what_a_command_inherits_is_the_allow_list_and_a_prefix_never_lets_a_secret_in(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {"CLAUDE_CODE_OAUTH_TOKEN": SECRET, "OPENROUTER_API_KEY": "sk-or-1", "LC_ALL": "C", "LC_API_KEY": "x" * 20,
                        "DISPLAY": ":0", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus", "TZ": "UTC"}.items():
        monkeypatch.setenv(name, value)
    inside = system._command_env(desktop=False)
    assert inside["LC_ALL"] == "C" and inside["TZ"] == "UTC" and inside["HOME"] == str(home)
    assert not {"CLAUDE_CODE_OAUTH_TOKEN", "OPENROUTER_API_KEY", "LC_API_KEY", "DISPLAY", "DBUS_SESSION_BUS_ADDRESS"} & inside.keys()
    outside = system._command_env(desktop=True)  # as the user does: the desktop too, still no token
    assert outside["DISPLAY"] == ":0" and "DBUS_SESSION_BUS_ADDRESS" in outside
    assert not {"CLAUDE_CODE_OAUTH_TOKEN", "OPENROUTER_API_KEY", "LC_API_KEY"} & outside.keys()


def test_the_programs_he_starts_for_the_desktop_inherit_no_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", SECRET)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    env = system._desktop_env()
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env and env["WAYLAND_DISPLAY"] == "wayland-0" and "PATH" in env
    done = system._run(["bash", "-c", "echo ${CLAUDE_CODE_OAUTH_TOKEN:-none} $WAYLAND_DISPLAY"])
    assert done.stdout == "none wayland-0\n"


def test_outside_the_sandbox_a_command_still_has_no_token_but_reads_as_the_user_does(
        home: Path, brain: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", SECRET)
    monkeypatch.setenv("DISPLAY", ":7")
    out = system.run_command("env; cat ~/.ssh/id_rsa", outside_sandbox=True)
    assert "PLANTED-PRIVATE-KEY" in out and "DISPLAY=:7" in out and SECRET not in out and "TOKEN" not in out


def test_another_app_cannot_ask_for_a_command_outside_the_sandbox(home: Path) -> None:
    with pytest.raises(ToolError, match="Another app can't run a command outside the sandbox"):
        system.run_command("id", outside_sandbox=True)  # (outside a call from the hub, a call counts as an app's)


def test_with_the_sandbox_off_or_in_a_container_a_command_runs_as_it_is(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx.config.safety.sandbox_commands = False
    assert "PLANTED-PRIVATE-KEY" in system.run_command("cat ~/.ssh/id_rsa")
    ctx.config.safety.sandbox_commands = True
    monkeypatch.setattr(system, "profile", lambda: HostProfile("linux", True, "", "", False, "", "", False, False, False,
                                                                False, "", "", "container"))
    monkeypatch.setattr(sandbox, "status", lambda **_: pytest.fail("a container isn't probed for a sandbox"))
    out = system.run_command("cat ~/.ssh/id_rsa; env | grep -c TOKEN")
    assert "PLANTED-PRIVATE-KEY" in out and out.rstrip().endswith("0")  # the container is the box; the environment is clean


def test_a_machine_that_cannot_make_a_sandbox_says_so_instead_of_running_without_one(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "_status", (sandbox.time.monotonic() + 10**6, sandbox.Status("", False, 8, "bubblewrap isn't "
                                                                                                         "installed")))
    with pytest.raises(ToolError, match="can't make one: bubblewrap isn't installed.*turn the sandbox off"):
        system.run_command("echo hi")


@needs_bwrap
def test_a_command_reads_the_users_profile_but_its_noise_is_left_out(home: Path) -> None:
    (home / ".profile").write_text('export FROM_PROFILE="set by ~/.profile"; echo "profile chatter"; echo oops >&2\n')
    out = system.run_command('echo "$FROM_PROFILE"; echo \'quoted "args" $HOME\'; exit 3')
    assert out == 'exit code 3\nset by ~/.profile\nquoted "args" $HOME'


@needs_bwrap
def test_a_command_that_runs_out_of_time_is_stopped_and_a_long_output_is_cut(home: Path) -> None:
    assert system.run_command("sleep 30", timeout=1) == "Command timed out after 1 s; its process group was stopped."
    ctx.config.system.command_output_chars = 500
    out = system.run_command("seq 1 100000; exit 4")
    assert out.startswith("exit code 4\n1\n2\n") and "\n...\n" in out and out.endswith("100000") and len(out) < 700
    assert system.run_command("true") == "exit code 0\n(no output)"


# ---------------------------------------------------------------------------------------------------- the file tools
def test_the_file_tools_do_not_read_what_the_sandbox_hides(home: Path) -> None:
    assert system.read_file("~/.ssh/id_rsa").startswith("That is one of the places he keeps hidden")
    assert system.read_file(str(home / ".ssh" / "id_rsa")) == system.read_file("~/.ssh/id_rsa")
    assert system.read_file("~/Documents/note.txt") == "a note"
    data = Path(ctx.config.data_path)
    (data / "vault").mkdir(parents=True)
    (data / "vault" / "openrouter.api_key").write_text("PLANTED-VAULT-VALUE")
    assert "hidden" in system.read_file(str(data / "vault" / "openrouter.api_key"))
    (data / "skills" / "demo").mkdir(parents=True)
    (data / "skills" / "demo" / "SKILL.md").write_text("# a skill")
    assert system.read_file(str(data / "skills" / "demo" / "SKILL.md")) == "# a skill"  # his skills are there to read
    (home / "Downloads" / "peek").symlink_to(home / ".ssh" / "id_rsa")
    assert "hidden" in system.read_file("~/Downloads/peek")  # a link to it from somewhere harmless leads there too


def test_the_file_tools_do_not_change_his_code_his_skills_or_what_starts_programs(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = home / "Documents" / "Orion"
    (repo / "orion").mkdir(parents=True)
    monkeypatch.setattr(config, "ROOT", repo)
    (ctx.config.data_path / "skills").mkdir(parents=True)
    for target in ("~/.bashrc", "~/.config/autostart/evil.desktop", "~/.config/systemd/user/x.service", "~/.local/bin/ls",
                   str(repo / "orion" / "policy.py"), str(ctx.config.data_path / "skills" / "new.md"), "~/.ssh/authorized_keys",
                   str(ctx.config.data_path / "vault" / "x")):
        assert system.write_file(target, "x").startswith(("He doesn't change that", "That is one of the places")), target
        assert not Path(os.path.expanduser(target)).exists(), target
    assert system.write_file("~/Documents/mine.txt", "mine") == f"Wrote 4 characters to {home}/Documents/mine.txt."
    assert system.write_file("~/shopping.txt", "milk") == f"Wrote 4 characters to {home}/shopping.txt."  # the home is open
    assert system.write_file("~/Documents/mine.txt", "!", append=True).startswith("Wrote 1 characters")
    assert (home / "Documents" / "mine.txt").read_text() == "mine!"
