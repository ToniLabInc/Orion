"""tools/organizer: spoken durations, spoken times (in a fixed time zone, across a clock change), which timer or
reminder the user means (an id that isn't running is never another one), and time added to a timer or taken off."""

import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from orion.config import default_config
from orion.memory.store import Store
from orion.tools import organizer
from orion.tools.organizer import find_pending, parse_duration, parse_when, spoken_duration

ATHENS = ZoneInfo("Europe/Athens")


def at(*when: int, fold: int = 0) -> datetime:
    """A wall-clock time in Athens (fold=1: the second 03:xx on the night the clocks go back)."""
    return datetime(*when, tzinfo=ATHENS, fold=fold)


@pytest.mark.parametrize(("words", "seconds"), [
    ("5 minutes", 300), ("1h30m", 5400), ("90 seconds", 90), ("half an hour", 1800), ("an hour and a half", 5400),
    ("five minutes", 300), ("10", 600), ("2 hours", 7200), ("the 10-minute timer", 600), ("soon", None),
])
def test_parse_duration(words: str, seconds: float | None) -> None:
    """Numbers in words or digits, units long or short, and a bare number in minutes."""
    assert parse_duration(words) == seconds


@pytest.mark.parametrize(("words", "now", "due"), [
    ("in 20 minutes", at(2026, 10, 3, 22, 10), at(2026, 10, 3, 22, 30)),
    ("tomorrow at 9", at(2026, 10, 3, 22, 10), at(2026, 10, 4, 9, 0)),
    ("at 7am", at(2026, 10, 3, 8, 0), at(2026, 10, 4, 7, 0)),  # already past today: tomorrow
    ("tonight at 11", at(2026, 10, 3, 18, 0), at(2026, 10, 3, 23, 0)),
    ("next monday", at(2026, 10, 3, 22, 10), at(2026, 10, 5, 9, 0)),  # a day without a time: 9:00
    ("at midnight", at(2026, 10, 3, 22, 10), at(2026, 10, 4, 0, 0)),
    ("on the 15th at 10", at(2026, 10, 15, 0, 4), at(2026, 10, 15, 10, 0)),  # just after midnight: still today
    ("on the 31st at 10", at(2026, 1, 31, 12, 0), at(2026, 3, 31, 10, 0)),  # February has no 31st
    ("on the 1st at 9", at(2026, 12, 2, 12, 0), at(2027, 1, 1, 9, 0)),
    ("in 20 minutes", at(2026, 10, 25, 3, 50), at(2026, 10, 25, 3, 10, fold=1)),  # the clocks go back at 04:00
    ("blah", at(2026, 10, 3, 22, 10), None),
])
def test_parse_when(words: str, now: datetime, due: datetime | None) -> None:
    """Spoken times land on the right local moment."""
    found = parse_when(words, now, default_hour=9)
    assert found == due and (found is None or due is None or found.fold == due.fold)


def test_the_right_timer_or_reminder_is_found() -> None:
    """By length, name, number or kind; two timers and "the timer" ask which one."""
    now = at(2026, 10, 3, 12, 0)
    t = now.timestamp()
    items = [{"id": 2, "text": "10-minute timer", "kind": "timer", "ts": t - 60, "due": t + 540},
             {"id": 3, "text": "pasta timer", "kind": "timer", "ts": t, "due": t + 480},
             {"id": 4, "text": "call the dentist", "kind": "reminder", "ts": t, "due": t + 86400}]
    assert find_pending(items, "the timer that you put for 10 minutes", now=now)[0]["id"] == 2
    assert find_pending(items, "pasta", now=now)[0]["id"] == 3
    assert find_pending(items, "#4", now=now)[0]["id"] == 4 and find_pending(items, "timer 3", now=now)[0]["id"] == 3
    assert find_pending(items, "the dentist reminder", now=now)[0]["id"] == 4
    target, question = find_pending(items, "the timer", now=now)
    assert target is None and "#2 10-minute timer: 9 min 0 s left" in question and "#3 pasta timer" in question
    assert find_pending(items[:1], "", now=now)[0]["id"] == 2 and find_pending(items[:1], "the timer", now=now)[0]
    assert spoken_duration(600) == "10-minute" and spoken_duration(5400) == "1 hour 30-minute"


def test_an_id_that_isnt_running_is_never_another_one() -> None:
    """"#9" with only #2 running is an error, not #2 (the only one left would otherwise be cancelled)."""
    t = time.time()
    items = [{"id": 2, "text": "", "kind": "timer", "ts": t, "due": t + 60},
             {"id": 3, "text": "pasta timer", "kind": "timer", "ts": t, "due": t + 480}]
    for which in ("#9", "9", "timer 9", "number 9", "reminder #9"):
        with pytest.raises(LookupError, match="#9"):
            find_pending(items[:1], which)
    with pytest.raises(LookupError):
        find_pending([], "#2")
    assert find_pending(items, "number 3")[0]["id"] == 3 and find_pending(items, "2")[0]["id"] == 2


def test_time_is_added_to_a_timer_or_taken_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """change_timer moves the timer's end (never into the past) and says when it ends; a cancel or a change of an id
    that isn't running is the call's error."""
    store = Store(tmp_path / "orion.db", default_config().memory, role="tools")
    store.ensure_schema()
    events: list[str] = []
    monkeypatch.setattr(organizer, "ctx", SimpleNamespace(memory=SimpleNamespace(store=store)))
    monkeypatch.setattr(organizer, "publish", lambda kind, **data: events.append(kind))
    now = time.time()
    pasta = store.add_reminder("pasta", now + 600, "timer")
    store.add_reminder("call mum", now + 30, "reminder")

    def due() -> float:
        """When the pasta timer ends now."""
        return next(item["due"] for item in store.pending_reminders() if item["id"] == pasta)

    assert organizer.change_timer("pasta", "5 minutes").startswith(f"Timer #{pasta} (pasta timer) now ends at ")
    assert abs(due() - (now + 900)) < 1
    assert "(0 min 0 s left)" in organizer.change_timer(f"#{pasta}", "-1 hour")
    assert due() <= time.time()
    assert organizer.change_timer("pasta", "a bit") == "Could not understand the time 'a bit'."
    with pytest.raises(ToolError, match="#99"):
        organizer.change_timer("#99", "1 minute")
    with pytest.raises(ToolError, match="#99"):
        organizer.cancel_timer_or_reminder("#99")
    assert events == ["reminders", "reminders"]
    store.close()
