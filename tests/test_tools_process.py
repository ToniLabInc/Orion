"""The tools process: its results are redacted, it learns a secret the vault gains while it runs, a tool lent straight to
another app obeys what the "app" channel may never do, and the calendar fills in a secret from the vault."""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from orion import redact, secrets
from orion.base import GroupInfo
from orion.redact import Redactor
from orion.tools import Group, build_server, calendar, ctx, system

SECRET = "Zx9-Qm4_Lp7Rk2Vb8Nc5Td1Wh6Yg3Jf0Ae"


@pytest.fixture
def opened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The tools' context opened on a throwaway settings file."""
    path = tmp_path / "data" / "config.yaml"
    path.parent.mkdir()
    monkeypatch.setattr(ctx, "_path", path)
    monkeypatch.setattr(ctx, "_settings", None)
    return path


def demo() -> Group:
    group = Group(GroupInfo("demo", "Demo", "A demo group."))

    @group.tool(title="Read a file", read_only=True)
    def read(name: str) -> str:
        """Reads something."""
        return f"{name}: the key is {SECRET}!"

    return group


def call(server: Any, name: str, args: dict[str, Any]) -> tuple[bool, str]:
    async def run() -> tuple[bool, str]:
        async with Client(server) as client:
            result = await client.call_tool(name, args)
        return bool(result.is_error), result.content[0].text  # type: ignore[union-attr]

    return asyncio.run(run())


def test_what_a_tool_answers_is_redacted_before_it_leaves_the_process(opened: Path) -> None:
    server = build_server([demo()])
    with redact.installed(Redactor((), min_chars=8)) as redactor:
        assert call(server, "read", {"name": "a"}) == (False, f"a: the key is {SECRET}!")  # knowing nothing yet
        redactor.learn([SECRET])
        assert call(server, "read", {"name": "a"}) == (False, "a: the key is [secret]!")


def test_a_secret_the_vault_gains_while_the_process_runs_is_hidden_from_the_next_call(
        opened: Path, tmp_path: Path) -> None:
    vault = secrets.Vault("files", tmp_path / "v")
    server = build_server([demo()])
    redactor = Redactor((), min_chars=8, source=secrets.Known(vault, {}))
    with redact.installed(redactor):
        assert call(server, "read", {"name": "a"})[1].endswith(f"{SECRET}!")
        vault.set("calendar.home.password", SECRET)  # (set in the HUD: another process)
        assert call(server, "read", {"name": "a"})[1] == "a: the key is [secret]!"


def test_a_change_of_the_patterns_reaches_the_running_process(opened: Path) -> None:
    server = build_server([demo()])
    opened.write_text("safety:\n  secret_patterns: []\n")
    assert ctx.config.safety.secret_patterns == []
    with redact.installed(Redactor((), min_chars=8)):
        assert call(server, "read", {"name": "zzz-abcdefghij"})[1].startswith("zzz-abcdefghij:")
        opened.write_text("safety:\n  secret_patterns: ['\\bzzz-[a-z]{10}\\b']\n")
        assert call(server, "read", {"name": "zzz-abcdefghij"})[1].startswith("[secret]:")


def test_a_tool_lent_straight_to_another_app_obeys_the_app_channels_limits(opened: Path) -> None:
    """(`orion mcp-serve` has no hub: the extension refuses what the "app" channel may never use. `volume` is the example
    because a limit that failed to hold would only read the volume.)"""
    ctx.config.safety.channels["app"]["never"] = ["volume"]
    lent = build_server([system.group], lend=True)
    refused = call(lent, "volume", {"action": "get"})
    assert refused[0] and refused[1] == ("Error: volume can't be used for another app: it is one of the things that channel "
                                          "may never do.")
    ctx.config.safety.channels["app"]["never"] = []
    assert "refused" not in call(lent, "volume", {"action": "get"})[1] and "can't be used for another app" not in (
        call(lent, "volume", {"action": "get"})[1])
    private = build_server([system.group])  # the hub's own process: the hub limits the channels, not this
    ctx.config.safety.channels["app"]["never"] = ["volume"]
    assert "can't be used for another app" not in call(private, "volume", {"action": "get"})[1]


def test_the_calendar_fills_in_a_secret_from_the_vault_and_says_when_it_cannot(tmp_path: Path) -> None:
    vault = secrets.Vault("files", tmp_path / "v")
    vault.set("calendar.home.password", "app-pass-word")
    with secrets.installed(vault):
        assert calendar.expand("${secret:calendar.home.password}") == "app-pass-word"
        with pytest.raises(calendar.CalendarError, match="calendar.work.password isn't set"):
            calendar.expand("${secret:calendar.work.password}")
    with secrets.installed(None), pytest.raises(calendar.CalendarError, match="no vault is open"):
        calendar.expand("${secret:calendar.home.password}")
