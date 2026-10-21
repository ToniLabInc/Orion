"""calendar: a secret iCal link read and its repeats expanded in this machine's zone, events listed for the model and
the HUD, a source that fails named, and nothing set up said plainly (no network: the link is served by a fake)."""

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from orion.config import default_config
from orion.tools import calendar as cal

ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
X-WR-TIMEZONE:Europe/Athens
BEGIN:VEVENT
UID:standup-1
SUMMARY:Standup
DTSTART;TZID=Europe/Athens:20261012T093000
DTEND;TZID=Europe/Athens:20261012T094500
RRULE:FREQ=DAILY;COUNT=5
LOCATION:Room 4
END:VEVENT
BEGIN:VEVENT
UID:trip-1
SUMMARY:Trip to Rhodes
DTSTART;VALUE=DATE:20261014
DTEND;VALUE=DATE:20261016
END:VEVENT
END:VCALENDAR
"""
ATHENS = ZoneInfo("Europe/Athens")


@pytest.fixture
def link(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Serve ICS at any link (failing for a link with 'broken' in it); the zone is Athens."""
    asked: list[str] = []

    def get(url: str, **_: Any) -> httpx.Response:
        asked.append(url)
        if "broken" in url:
            raise httpx.ConnectError("no route")
        return httpx.Response(200, text=ICS, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(cal, "local_zone", lambda: ATHENS)
    cal.SOURCES.forget()
    return asked


def test_a_link_is_read_with_its_repeats_and_whole_days(link: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORK_ICS", "https://example.test/work.ics")
    cfg = default_config().calendar
    cfg.sources = {"work": {"ics_url": "${WORK_ICS}"}, "old": {"ics_url": "https://example.test/broken.ics"}}
    first, last = datetime(2026, 10, 13, tzinfo=ATHENS), datetime(2026, 10, 15, tzinfo=ATHENS)
    events, problems = cal.SOURCES.agenda(cfg, first, last)
    assert link[0] == "https://example.test/work.ics" and list(problems) == ["old"]
    assert [(e.title, e.start[:16], e.all_day) for e in events] == [
        ("Standup", "2026-10-13T09:30", False),
        ("Trip to Rhodes", "2026-10-14T00:00", True),
        ("Standup", "2026-10-14T09:30", False),
    ]
    standups = [e for e in events if e.title == "Standup"]
    assert len(standups) == 2 and standups[0].location == "Room 4" and standups[0].id.startswith("work/standup-1@")
    cal.SOURCES.agenda(cfg, first, last)
    assert len([u for u in link if "work" in u]) == 1  # the cache answered the second time
    line = cal.describe(standups[0])
    assert line.startswith("- Tue 13 Oct 09:30–09:45: Standup at Room 4 [work/standup-1@")


def test_the_agenda_tool_says_what_is_set_up_and_what_failed(link: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = default_config()
    monkeypatch.setattr(cal.ctx, "refresh", lambda: None, raising=False)
    monkeypatch.setattr(type(cal.ctx), "config", property(lambda self: cfg))
    assert cal.calendar_agenda() == "No calendar is set up (Customize, Connectors, Calendar)."
    cfg.calendar.sources = {"broken": {"ics_url": "https://example.test/broken.ics"}}
    said = cal.calendar_agenda("today", 1)
    assert said.startswith("Nothing on the calendar") and "(broken couldn't be read:" in said
    assert cal.calendar_add("Lunch", "tomorrow at 13:00").startswith("Error: no calendar can take new events")
