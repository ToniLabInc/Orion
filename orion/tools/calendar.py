"""Your calendars: read them, add events, delete events. Any provider: CalDAV (iCloud, Fastmail, Nextcloud,
Radicale, Yahoo… with an app password) or a calendar's secret iCal link (Google, Outlook, Proton: read-only).

Each source is a name under ``calendar.sources`` in the settings (a secret: never shown to the HUD or the AI)::

    calendar:
      sources:
        home: {caldav_url: https://caldav.icloud.com/, username: you@icloud.com, password: "${ICLOUD_APP_PASSWORD}"}
        work: {ics_url: "${WORK_ICS_URL}"}

A password or a secret link is kept in his vault, and the settings say ``${secret:calendar.home.password}`` (``orion.vaulting``);
``${VAR}``s come from the environment. CalDAV goes through the caldav library (a source may name ``calendars`` to
read, and be ``read_only``); a link is fetched and expanded with icalendar, recurring-ical-events and x-wr-timezone
(Google's links name their zone that way). What a source answers is kept ``calendar.cache_s``. Events are in this
machine's time zone. Reading taints the turn: anyone who sends an invitation writes the text of an event.
"""

import logging
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, Annotated, Any, Final

from pydantic import Field

from orion import secrets
from orion.base import JSON, clip, local_now, local_zone
from orion.secrets import VaultError
from orion.tools import GROUPS, Group, ctx

if TYPE_CHECKING:
    from orion.config import CalendarConfig

log = logging.getLogger("orion.tools.calendar")

group = Group(GROUPS["calendar"])

LIST_EVENTS: Final = 60  # events one answer lists at most
TITLE_CHARS: Final = 160
NOTE_CHARS: Final = 400


class CalendarError(RuntimeError):
    """A calendar couldn't be read or changed; the message says why."""


@dataclass(frozen=True, slots=True)
class Event:
    """One event (one occurrence of a repeating one).

    Attributes:
        id: "<source>/<uid>[@<occurrence>]": what ``calendar_delete`` takes.
        source: the source's name.
        calendar: the calendar's name within it ("" for a link).
        title: its summary.
        start: when it starts (local time, ISO).
        end: when it ends (local time, ISO).
        all_day: it fills whole days.
        location: where ("" when unsaid).
    """

    id: str
    source: str
    calendar: str
    title: str
    start: str
    end: str
    all_day: bool
    location: str

    def as_dict(self) -> dict[str, JSON]:
        """For the HUD."""
        return dict(asdict(self))


def expand(value: str) -> str:
    """``${secret:NAME}`` filled in from his vault, ``${VAR}`` and ``$VAR`` from the environment.

    Raises:
        CalendarError: a secret that isn't set or can't be read.
    """
    try:
        return secrets.expand(value)
    except VaultError as e:
        raise CalendarError(str(e)) from None


def _local(value: date | datetime, zone: tzinfo) -> tuple[datetime, bool]:
    """A start or end as a local, aware datetime, and whether it is a whole day."""
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=zone)
        return moment.astimezone(zone), False
    return datetime(value.year, value.month, value.day, tzinfo=zone), True


def _events_of(components: Iterable[Any], source: str, calendar: str, zone: tzinfo) -> list[Event]:
    """VEVENTs (already expanded to occurrences) as Events."""
    found: list[Event] = []
    for component in components:
        if getattr(component, "name", "") != "VEVENT":
            continue
        start_value = component.get("DTSTART")
        if start_value is None:
            continue
        start, all_day = _local(start_value.dt, zone)
        end_value = component.get("DTEND")
        if end_value is not None:
            end, _ = _local(end_value.dt, zone)
        elif (duration := component.get("DURATION")) is not None:
            end = start + duration.dt
        else:
            end = start + (timedelta(days=1) if all_day else timedelta(0))
        uid = str(component.get("UID") or "")
        occurrence = component.get("RECURRENCE-ID")
        key = f"{source}/{uid}" + (f"@{_local(occurrence.dt, zone)[0]:%Y%m%dT%H%M}" if occurrence is not None else "")
        found.append(Event(id=key, source=source, calendar=calendar,
                           title=clip(str(component.get("SUMMARY") or "(no title)"), TITLE_CHARS),
                           start=start.isoformat(), end=end.isoformat(), all_day=all_day,
                           location=clip(str(component.get("LOCATION") or ""), TITLE_CHARS)))
    return found


class Sources:
    """The sources of the settings, read with a short cache (one per tools process, and one in Orion's own)."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str, str], tuple[float, list[Event]]] = {}
        self._lock = threading.Lock()

    def agenda(self, cfg: "CalendarConfig", start: datetime, end: datetime, *, fresh: bool = False,
               ) -> tuple[list[Event], dict[str, str]]:
        """Every source's events between two moments, in time order, and the sources that failed (name → why).

        Args:
            cfg: the calendar settings.
            start: from (aware).
            end: to (aware).
            fresh: read again even what the cache holds.

        Returns:
            (events, problems).
        """
        events: list[Event] = []
        problems: dict[str, str] = {}
        for name, spec in cfg.sources.items():
            key = (name, start.isoformat(), end.isoformat())
            with self._lock:
                cached = self._cache.get(key)
            if cached is not None and not fresh and time.monotonic() - cached[0] < cfg.cache_s:
                events += cached[1]
                continue
            try:
                found = self._read(name, spec, start, end, cfg.timeout_s)
            except CalendarError as e:
                problems[name] = str(e)
                continue
            with self._lock:
                self._cache[key] = (time.monotonic(), found)
            events += found
        return sorted(events, key=lambda e: (e.start, e.title)), problems

    def forget(self) -> None:
        """Read everything again next time (an event was added or deleted)."""
        with self._lock:
            self._cache.clear()

    def _read(self, name: str, spec: Mapping[str, Any], start: datetime, end: datetime,
              timeout_s: float) -> list[Event]:
        """One source's events (see ``agenda``)."""
        zone = local_zone()
        if spec.get("ics_url"):
            return self._read_link(name, expand(str(spec["ics_url"])), start, end, timeout_s, zone)
        if spec.get("caldav_url"):
            found: list[Event] = []
            for calendar in self._calendars(name, spec, timeout_s):
                try:
                    results = calendar.search(start=start, end=end, event=True, expand=True)
                except Exception as e:  # caldav raises many kinds (errors, HTTP, parsing)
                    raise CalendarError(f"{name} couldn't be searched ({e})") from e
                label = str(getattr(calendar, "name", "") or "")
                for result in results:
                    found += _events_of([result.icalendar_component], name, label, zone)
            return found
        raise CalendarError(f"{name} has neither caldav_url nor ics_url")

    @staticmethod
    def _read_link(name: str, url: str, start: datetime, end: datetime, timeout_s: float,
                   zone: tzinfo) -> list[Event]:
        """A secret iCal link's events, repeats expanded."""
        import httpx
        import icalendar
        import recurring_ical_events
        import x_wr_timezone

        try:
            response = httpx.get(url, timeout=timeout_s, follow_redirects=True)
            response.raise_for_status()
            calendar = x_wr_timezone.to_standard(icalendar.Calendar.from_ical(response.text))
            occurrences = recurring_ical_events.of(calendar).between(start, end)
        except httpx.HTTPError as e:
            raise CalendarError(f"{name}'s link couldn't be read ({e})") from e
        except ValueError as e:
            raise CalendarError(f"{name}'s link isn't a calendar ({e})") from e
        return _events_of(occurrences, name, "", zone)

    @staticmethod
    def _calendars(name: str, spec: Mapping[str, Any], timeout_s: float) -> list[Any]:
        """A CalDAV source's calendars (only those it names, when it names some)."""
        import caldav

        try:
            client = caldav.DAVClient(url=expand(str(spec["caldav_url"])), username=expand(str(spec.get("username")
                                                                                                or "")) or None,
                                      password=expand(str(spec.get("password") or "")) or None, timeout=int(timeout_s))
            calendars = client.get_calendars()
        except Exception as e:  # authentication, network, discovery: said plainly
            raise CalendarError(f"{name} couldn't be reached ({e})") from e
        wanted = {str(n).casefold() for n in spec.get("calendars") or ()}
        return [c for c in calendars if not wanted or str(getattr(c, "name", "") or "").casefold() in wanted]


SOURCES: Final = Sources()


def window(start_text: str, days: int) -> tuple[datetime, datetime]:
    """The moments an agenda covers: from the start of a day ("today", "friday", "2026-10-14") for some days."""
    from orion.tools.organizer import parse_when

    now = local_now()
    when = now if start_text.strip().lower() in ("", "today", "now") else parse_when(start_text, now, default_hour=0)
    if when is None:
        raise ValueError(f"'{start_text}' isn't a day I can read")
    first = when.replace(hour=0, minute=0, second=0, microsecond=0)
    return first, first + timedelta(days=max(1, days))


def describe(event: Event) -> str:
    """One event as a line for the model."""
    start, end = datetime.fromisoformat(event.start), datetime.fromisoformat(event.end)
    if event.all_day:
        when = f"{start:%a %d %b}, all day"
    else:
        when = f"{start:%a %d %b %H:%M}–{end:%H:%M}"
    where = f" at {event.location}" if event.location else ""
    return f"- {when}: {event.title}{where} [{event.id}]"


@group.tool(read_only=True, open_world=True, taints=True, title="Read the calendar",
            examples=("What's on my calendar tomorrow?", "Am I free on Friday afternoon?", "What's my week like?"))
def calendar_agenda(start: Annotated[str, Field(description='The first day: "today", "tomorrow", "friday", '
                                                            '"2026-10-14".')] = "today",
                    days: Annotated[int, Field(ge=1, le=31, description="How many days.")] = 1,
                    query: Annotated[str, Field(description="Only events whose title or place has these words.")] = ""
                    ) -> str:
    """The events in the user's calendars over some days (each with its id, for calendar_delete)."""
    cfg = ctx.config.calendar
    if not cfg.sources:
        return "No calendar is set up (Customize, Connectors, Calendar)."
    try:
        first, last = window(start, days)
    except ValueError as e:
        return f"Error: {e}"
    events, problems = SOURCES.agenda(cfg, first, last)
    if query.strip():
        words = query.casefold().split()
        events = [e for e in events if all(w in f"{e.title} {e.location}".casefold() for w in words)]
    lines = [describe(e) for e in events[:LIST_EVENTS]]
    span = f"{first:%a %d %b}" + (f" to {last - timedelta(days=1):%a %d %b}" if days > 1 else "")
    head = f"Events {span}:" if lines else f"Nothing on the calendar {span}."
    notes = [f"({name} couldn't be read: {why})" for name, why in problems.items()]
    return "\n".join([head, *lines, *notes])


@group.tool(open_world=True, idempotent=False, title="Add a calendar event",
            examples=("Put lunch with Sam on Thursday at 1", "Block Friday 9 to 11 for the report"))
def calendar_add(title: Annotated[str, Field(description="What it is.")],
                 start: Annotated[str, Field(description='When it starts: "thursday at 13:00", "2026-10-15 09:30", '
                                                         '"tomorrow" for a whole day.')],
                 end: Annotated[str, Field(description="When it ends (empty: after duration_min).")] = "",
                 duration_min: Annotated[int, Field(ge=1, le=10080)] = 60,
                 all_day: bool = False,
                 location: str = "",
                 notes: str = "",
                 calendar: Annotated[str, Field(description="Which source to put it in (empty: the usual one).")] = ""
                 ) -> str:
    """Add an event to the user's calendar (a CalDAV source; links are read-only)."""
    from orion.tools.organizer import parse_when

    cfg = ctx.config.calendar
    name = calendar or cfg.write_to or next((n for n, s in cfg.sources.items() if s.get("caldav_url")), "")
    spec = cfg.sources.get(name)
    if spec is None or not spec.get("caldav_url"):
        return "Error: no calendar can take new events (a CalDAV source is needed; links are read-only)."
    if spec.get("read_only"):
        return f"Error: {name} is read-only in the settings."
    now = local_now()
    begin = parse_when(start, now, default_hour=9)
    if begin is None:
        return f"Error: '{start}' isn't a time I can read."
    finish = parse_when(end, now, default_hour=begin.hour) if end else None
    try:
        target = Sources._calendars(name, spec, cfg.timeout_s)[0]
    except (CalendarError, IndexError) as e:
        return f"Error: {e or f'{name} has no calendar'}."
    try:
        if all_day:
            day = begin.date()
            target.add_event(dtstart=day, dtend=(finish.date() if finish else day) + timedelta(days=1),
                             summary=clip(title, TITLE_CHARS), location=location or None,
                             description=clip(notes, NOTE_CHARS) or None)
        else:
            stop = finish or begin + timedelta(minutes=duration_min)
            target.add_event(dtstart=begin, dtend=stop, summary=clip(title, TITLE_CHARS), location=location or None,
                             description=clip(notes, NOTE_CHARS) or None)
    except Exception as e:  # noqa: BLE001 (the server refused it: the model hears why)
        return f"Error: {name} didn't take the event ({e})."
    SOURCES.forget()
    when = f"{begin:%a %d %b}, all day" if all_day else f"{begin:%a %d %b %H:%M}"
    return f"Added “{title}” on {when} to {name}."


@group.tool(destructive=True, open_world=True, title="Delete a calendar event",
            examples=("Cancel my 3pm on Friday",))
def calendar_delete(event: Annotated[str, Field(description="The event's id from calendar_agenda.")]) -> str:
    """Delete an event from the user's calendar (find its id with calendar_agenda first)."""
    cfg = ctx.config.calendar
    name, _, uid = event.partition("/")
    uid = uid.split("@")[0]
    spec = cfg.sources.get(name)
    if spec is None or not uid:
        return f"Error: {event} isn't an event id from calendar_agenda."
    if not spec.get("caldav_url") or spec.get("read_only"):
        return f"Error: {name} is read-only."
    try:
        for calendar in Sources._calendars(name, spec, cfg.timeout_s):
            try:
                found = calendar.get_event_by_uid(uid)
            except Exception:  # noqa: BLE001 (not in this calendar: the next one may have it)
                log.debug("no %s in %s", uid, getattr(calendar, "name", ""))
                continue
            found.delete()
            SOURCES.forget()
            return f"Deleted the event from {name}."
    except CalendarError as e:
        return f"Error: {e}."
    return f"Error: no event {uid} in {name}."
