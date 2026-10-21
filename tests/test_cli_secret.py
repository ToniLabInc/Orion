"""`orion secret` and the security lines of `orion check`: a value is asked for with nothing shown or read from standard input
and never taken from the command line, only names are printed, every change is recorded by name, and the check says what is
wrong with the vault, the sandbox and the channels."""

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

from orion import __main__ as cli
from orion import landlock, sandbox, secrets
from orion.base import OrionError
from orion.config import default_config
from orion.memory.store import Store
from orion.secrets import VaultError

TOKEN = "discord-bot-token-Zx9Qm4Lp7Rk2Vb8Nc5Td1Wh6"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """The settings of a throwaway Orion: its vault is files in its own folder (never the real keyring)."""
    path = tmp_path / "data" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("secrets:\n  store: files\n"
                    "mcp_servers:\n  discord:\n    command: orion\n    env:\n"
                    "      DISCORD_TOKEN: ${secret:connector.discord.discord_token}\n")
    return tmp_path


def run(*args: str, input: str | None = None) -> Any:
    return CliRunner().invoke(cli.main, ["secret", *args], input=input)


def logged(home: Path) -> list[dict[str, Any]]:
    store = Store(home / "data" / "orion.db", default_config().memory, role="serve")
    try:
        store.ensure_schema()
        return store.activity(kinds=["secret"])
    finally:
        store.close()


def test_a_secret_is_set_from_standard_input_listed_by_name_and_deleted(home: Path) -> None:
    done = run("set", "connector.discord.discord_token", input=TOKEN + "\n")
    assert done.exit_code == 0 and done.output == "Set connector.discord.discord_token.\n"
    assert secrets.Vault("files", home / "data" / "vault").get("connector.discord.discord_token") == TOKEN
    listed = run("list")
    assert listed.exit_code == 0 and TOKEN not in listed.output
    assert listed.output == "connector.discord.discord_token  set  used by discord\n"
    gone = run("delete", "connector.discord.discord_token")
    assert gone.exit_code == 0 and gone.output == "Deleted connector.discord.discord_token.\n"
    assert run("list").output == "connector.discord.discord_token  NOT SET  used by discord\n"  # the setting still refers to it
    again = run("delete", "connector.discord.discord_token")
    assert again.exit_code == 1 and "isn't set" in again.output


def test_a_value_on_the_command_line_is_refused_so_it_never_reaches_a_process_list_or_a_history(home: Path) -> None:
    done = run("set", "a.b", TOKEN)
    assert done.exit_code == 2 and "unexpected extra argument" in done.output
    assert "a.b" not in run("list").output and TOKEN not in run("list").output


def test_a_terminal_is_asked_with_nothing_shown_and_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[dict[str, Any]] = []
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(cli.click, "prompt", lambda text, **kw: asked.append({"text": text, **kw}) or TOKEN)
    assert cli._secret_value("a.b") == TOKEN
    assert asked == [{"text": "Value of a.b", "hide_input": True, "confirmation_prompt": True}]


@pytest.mark.parametrize(("typed", "value"), [("tok\n", "tok"), ("tok\r\n", "tok"), ("tok", "tok"), ("a\nb\n", "a\nb"),
                                              (" tok \n", " tok ")])
def test_standard_input_loses_only_its_last_line_break(monkeypatch: pytest.MonkeyPatch, typed: str, value: str) -> None:
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: False, read=lambda: typed))
    assert cli._secret_value("a.b") == value


def test_an_empty_value_and_a_name_that_is_none_are_refused_and_nothing_is_set(home: Path) -> None:
    empty = run("set", "a.b", input="\n")
    assert empty.exit_code == 1 and "The value is empty." in empty.output
    wrong = run("set", "Not A Name", input=TOKEN)
    assert wrong.exit_code == 1 and "isn't a secret's name" in wrong.output and TOKEN not in wrong.output
    assert run("list").output == "connector.discord.discord_token  NOT SET  used by discord\n"


def test_a_vault_that_cannot_open_says_why_and_exits(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (home / "data" / "config.yaml").write_text("secrets:\n  store: keyring\n")

    def none() -> None:
        raise VaultError("no keyring answers on this machine (no Secret Service): set secrets.store to files")

    monkeypatch.setattr(secrets, "_keyring", none)  # (and never the real one)
    for args in (["list"], ["set", "a.b"], ["delete", "a.b"]):
        done = run(*args, input=TOKEN)
        assert done.exit_code == 1 and "no keyring answers" in done.output and TOKEN not in done.output


def test_a_change_is_recorded_by_name_only(home: Path) -> None:
    run("set", "a.b", input=TOKEN)
    run("delete", "a.b")
    rows = logged(home)
    assert [(r["title"], r["source"], r["actor"], r["outcome"]) for r in rows] == [
        ("Deleted the secret a.b", "user", "you, in the terminal", "ok"),
        ("Set the secret a.b", "user", "you, in the terminal", "ok")]
    assert TOKEN not in repr(rows)


def test_a_log_that_cannot_be_written_is_said_and_the_secret_is_kept_anyway(home: Path,
                                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(self: Store) -> None:
        raise OrionError("the database has another version")

    monkeypatch.setattr(Store, "ensure_schema", broken)
    done = CliRunner().invoke(cli.main, ["secret", "set", "a.b"], input=TOKEN)
    assert done.exit_code == 0 and "Set a.b." in done.output
    assert "not written to the activity log: the database has another version" in done.stderr
    assert secrets.Vault("files", home / "data" / "vault").get("a.b") == TOKEN
    monkeypatch.setattr(Store, "ensure_schema", lambda self: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    assert "not written to the activity log: locked" in CliRunner().invoke(cli.main, ["secret", "delete", "a.b"]).stderr


# ---------------------------------------------------------------------------------------------------- orion check
class Report:
    """What `check` would print: (ok, what, fix)."""

    def __init__(self) -> None:
        self.lines: list[tuple[bool, str, str]] = []

    def __call__(self, ok: bool, what: str, fix: str = "") -> None:
        self.lines.append((ok, what, fix))


@pytest.fixture
def machine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The settings of a throwaway Orion and a machine whose sandbox is whatever the test says."""
    cfg = default_config()
    cfg.data_dir = str(tmp_path / "data")
    cfg.secrets.store = "files"

    def on(container: bool = False, works: bool = True, version: int = 8, why: str = "") -> None:
        monkeypatch.setattr("orion.host.profile", lambda **kw: SimpleNamespace(container=container))
        monkeypatch.setattr(sandbox, "status", lambda **kw: sandbox.Status("/usr/bin/bwrap" if works else "", works, version,
                                                                           why))

    on()
    return cfg, on


def test_check_reports_the_vault_and_what_the_settings_still_hold_as_plain_text(machine, capsys) -> None:
    cfg, _ = machine
    report = Report()
    cli._check_security(cfg, report)
    assert report.lines[0] == (True, "secrets vault (files): 0 secrets (`orion secret list`)", "")
    assert not any("plain text" in what for _, what, _ in report.lines)
    cfg.openrouter.api_key = "sk-or-v1-" + "0123456789abcdef" * 4
    report = Report()
    cli._check_security(cfg, report)
    assert (False, "1 secret in the settings as plain text", "start Orion: he moves them into the vault") in report.lines


def test_check_says_when_the_vault_cannot_open(machine, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg, _ = machine
    cfg.secrets.store = "keyring"

    def none() -> None:
        raise VaultError("no keyring answers on this machine (no Secret Service): set secrets.store to files")

    monkeypatch.setattr(secrets, "_keyring", none)
    report = Report()
    cli._check_security(cfg, report)
    assert report.lines[0][:2] == (False, "secrets vault (keyring)") and "set secrets.store to files" in report.lines[0][2]


def test_check_says_how_commands_are_kept_in_bounds(machine) -> None:
    cfg, on = machine
    report = Report()
    cli._check_security(cfg, report)
    assert (True, "command sandbox (bubblewrap, Landlock 8)", "") in report.lines
    assert len(report.lines) == 2  # the vault and the sandbox: nothing else is wrong

    on(container=True)
    report = Report()
    cli._check_security(cfg, report)
    assert (True, "commands run in this container, which is their sandbox", "") in report.lines

    on()
    cfg.safety.sandbox_commands = False
    report = Report()
    cli._check_security(cfg, report)
    assert report.lines[-1][:2] == (False, "commands run as you do: the sandbox is off")

    cfg.safety.sandbox_commands = True
    on(works=False, version=0, why="bubblewrap isn't installed (sudo apt install bubblewrap)")
    report = Report()
    cli._check_security(cfg, report)
    assert report.lines[-1] == (False, "command sandbox (bubblewrap, Landlock none)",
                                "bubblewrap isn't installed (sudo apt install bubblewrap)")


def test_check_says_a_kernel_too_old_for_the_network_and_how_to_fix_it(machine) -> None:
    cfg, on = machine
    on(version=landlock.NEEDS_SCOPES - 1)
    report = Report()
    cli._check_security(cfg, report)
    assert report.lines[-1][0] is False and "needs Landlock 6 (Linux 6.12), this kernel has 5" in report.lines[-1][1]
    cfg.safety.sandbox_network = False  # the network is off for commands: nothing to worry about
    report = Report()
    cli._check_security(cfg, report)
    assert all(ok for ok, _, _ in report.lines)


def test_check_lists_what_each_channel_may_never_use(machine, capsys: pytest.CaptureFixture[str]) -> None:
    cfg, _ = machine
    cli._check_security(cfg, Report())
    out = capsys.readouterr().out
    assert "  · discord may never use: power, run_command, mcp_servers" in out
    assert "  · job may never use: power, mcp_servers, plugins, benchmark_brains" in out
    assert "  · app may never use: mcp_servers, plugins" in out
    assert "voice may never" not in out and "hud may never" not in out
