"""Organizer tools: timers, reminders and the to-do list.

Timers and reminders live in the database, so they survive a restart (announced late, never lost); a change tells
Orion to show the lists again from there. A day said without a time means ``reminders.default_hour`` (9:00), never
the middle of the night. The date logic is pure: the current
local time comes in as an aware datetime, so "in 20 minutes" stays right across a daylight-saving change and
"tomorrow" is this PC's tomorrow, not UTC's. ``dateparser`` loads with the first reminder (the tools process warms it
up).
"""

import calendar
import math
import re
import time
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta, tzinfo
from datetime import time as clock
from typing import Annotated, Final, Literal

from pydantic import Field

from orion.base import local_now
from orion.memory.store import ReminderRow
from orion.tools import GROUPS, Group, ctx, publish

group = Group(GROUPS["organizer"])

BARE_NUMBER_UNIT_S: Final = 60.0  # a bare number given as a duration means minutes
SAME_LENGTH_S: Final = 5.0  # a timer whose length is this close to the one named is that timer

_NUMBERS: Final = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty-five": 45, "forty": 40, "fifty": 50,
    "sixty": 60, "ninety": 90,
}
_UNITS: Final = {"s": 1, "sec": 1, "second": 1, "m": 60, "min": 60, "minute": 60, "h": 3600, "hr": 3600, "hour": 3600}
_NUMBER_WORDS: Final = re.compile(r"\b(" + "|".join(sorted(_NUMBERS, key=len, reverse=True)) + r")\b")
_AMOUNT: Final = re.compile(r"(\d+(?:\.\d+)?)\s*(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h)(?![a-z])")
_AM_PM: Final = r"\s*(?:am|pm|a\.m\.|p\.m\.)"
_LATER_IN_THE_DAY: Final = re.compile(r"\b(?:tonight|(?:this |in the )?(?:evening|afternoon))\b", re.IGNORECASE)
_MIDNIGHT: Final = re.compile(rf"\d:\d|\d{_AM_PM}|midnight", re.IGNORECASE)  # a time said: no default hour
_DAY_OF_MONTH: Final = re.compile(r"\b(?:on\s+)?(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)\b", re.IGNORECASE)
_NEXT_WEEKDAY: Final = re.compile(r"\bnext\s+(?=(?:mon|tues|wednes|thurs|fri|satur|sun)day\b)", re.IGNORECASE)
_BARE_HOUR: Final = re.compile(rf"\b(at)\s+(\d{{1,2}})(?!\s*[:.\d])({_AM_PM})?", re.IGNORECASE)
_HOUR_MINUTE: Final = re.compile(rf"\b(\d{{1,2}})(:\d\d)\b(?!{_AM_PM})", re.IGNORECASE)
_ID: Final = re.compile(r"(?:#|number\s*|timer\s*#?|reminder\s*#?)?(\d+)")  # "#2", "2", "number 2", "timer 2"
_FILLER: Final = frozenset({  # words that say which item without naming it ("cancel THE TIMER that you SET")
    "the", "a", "an", "my", "that", "this", "one", "you", "your", "i", "me", "set", "put", "started", "for", "timer",
    "timers", "reminder", "reminders", "remind", "cancel", "stop", "delete", "remove", "please", "minute", "minutes",
    "second", "seconds", "hour", "hours", "min", "mins", "about", "of", "to", "it"})


# ---------------------------------------------------------------------------------------------------- pure parts
def parse_duration(text: str) -> float | None:
    """A spoken length of time in seconds.

    Args:
        text: "5 minutes", "1h30m", "90 seconds", "half an hour", "an hour and a half", "five minutes", "the 10-minute
            timer", or a bare number (minutes).

    Returns:
        The seconds, or None when no length is said ("soon").
    """
    t = text.lower().replace("half an hour", "30 minutes")
    t = _NUMBER_WORDS.sub(lambda m: str(_NUMBERS[m.group(1)]), t)
    t = re.sub(r"(\d)-(?=[a-z])", r"\1 ", t)
    t = re.sub(r"\ban?\s+(hour|minute|second)", r"1 \1", t)
    t = re.sub(r"(\d+)\s*(hours?|minutes?)\s+and\s+a\s+half", r"\1.5 \2", t)
    total = sum(float(n) * _UNITS[unit if unit == "s" else unit.rstrip("s")] for n, unit in _AMOUNT.findall(t))
    if total:
        return total
    if re.fullmatch(r"\s*\d+(?:\.\d+)?\s*", t):
        return float(t) * BARE_NUMBER_UNIT_S
    return None


def spoken_duration(secs: float) -> str:
    """A length as an adjective: 600 -> "10-minute", 5400 -> "1 hour 30-minute", 45 -> "45-second"."""
    hours, rest = divmod(round(secs), 3600)
    minutes, seconds = divmod(rest, 60)
    parts = ([f"{hours} hour"] if hours else []) + ([f"{minutes}-minute"] if minutes else [])
    parts += [f"{seconds}-second"] if seconds and not hours else []
    return " ".join(parts) or "0-second"


def parse_when(text: str, now: datetime, *, default_hour: int) -> datetime | None:
    """When a reminder is due, from what the user said.

    Args:
        text: "in 20 minutes", "tomorrow at 9", "tonight at 8", "friday 18:00", "next monday", "on the 15th at 10"...
        now: the current local time, aware of its zone.
        default_hour: the hour of a day said without a time (``reminders.default_hour``).

    Returns:
        The moment, in ``now``'s zone (a time already past today means tomorrow; "the 31st" skips short months);
        None when no time is said.
    """
    import dateparser

    zone = now.tzinfo or UTC
    if (match := re.fullmatch(r"\s*in\s+(.+)", text, re.IGNORECASE)) and (secs := parse_duration(match.group(1))):
        return _after(now, secs)  # a span of time: real seconds, right across a clock change
    later = bool(_LATER_IN_THE_DAY.search(text))
    when = _NEXT_WEEKDAY.sub("", _LATER_IN_THE_DAY.sub(" ", text))
    when = _BARE_HOUR.sub(lambda m: f"{m.group(1)} {m.group(2)}:00{m.group(3) or ''}", when)  # "at 9" -> "at 9:00"
    if later:  # "tonight at 8" -> 20:00
        when = _HOUR_MINUTE.sub(lambda m: f"{int(m.group(1)) + 12 if 1 <= int(m.group(1)) <= 11 else m.group(1)}"
                                          f"{m.group(2)}", when)
    when = " ".join(when.split())
    base = now.astimezone(zone).replace(tzinfo=None)  # this PC's wall clock: dateparser's own is UTC
    day = None
    if (match := _DAY_OF_MONTH.search(when)) and 1 <= int(match.group(1)) <= 31:  # dateparser reads "the 3rd" as March
        day = int(match.group(1))
        when = f"{when[:match.start()]} {_month_with(day, base.date()):%Y-%m-%d} {when[match.end():]}".strip()
    found = dateparser.parse(when, languages=["en"], settings={
        "PREFER_DATES_FROM": "future", "RETURN_AS_TIMEZONE_AWARE": False, "RELATIVE_BASE": base})
    if found is not None and found.time() == clock.min and not _MIDNIGHT.search(when):
        found = found.replace(hour=default_hour)  # "on friday"
    if found is None or (found < base - timedelta(seconds=30) and "ago" not in when):
        if secs := parse_duration(when):  # "20 minutes": dateparser reads a bare length as that long ago
            return _after(now, secs)
        if found is None:
            return None
        if day:  # "on the 1st at 9" when it is the 1st at 10: next month's
            found = datetime.combine(_month_with(day, base.date() + timedelta(days=1)), found.time())
        else:  # "at 7am" when it is past 7: tomorrow
            found += timedelta(days=math.ceil((base - found).total_seconds() / 86400))
    return _local(found, zone)


def _after(now: datetime, secs: float) -> datetime:
    """``now`` plus real elapsed seconds, in ``now``'s zone (03:50 + 20 min on the night clocks go back is 03:10)."""
    zone = now.tzinfo or UTC
    return (now.astimezone(UTC) + timedelta(seconds=secs)).astimezone(zone)


def _local(wall: datetime, zone: tzinfo) -> datetime:
    """A wall-clock time in a zone: an hour skipped by a clock change moves forward, a repeated one is the first."""
    return wall.replace(tzinfo=zone).astimezone(UTC).astimezone(zone)


def _month_with(day: int, start: date) -> date:
    """The first date on or after ``start`` that falls on this day of the month ("the 31st" skips short months)."""
    year, month = start.year, start.month
    while not (day <= calendar.monthrange(year, month)[1] and (year, month, day) >= (start.year, start.month,
                                                                                      start.day)):
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return date(year, month, day)


def human_time(due: datetime, now: datetime) -> str:
    """When something is due, as said aloud: "today at 14:30 (in 5 minutes)", "Friday 03 October at 09:00".

    Args:
        due: the moment.
        now: the current local time.

    Returns:
        The words.
    """
    local = due.astimezone(now.tzinfo or UTC)
    delta = (due - now).total_seconds()
    day = "today" if local.date() == now.date() else f"{local:%A %d %B}"
    if abs(delta) >= 3600:
        return f"{day} at {local:%H:%M}"
    minutes = max(1, round(delta / 60))
    relative = f"in {minutes} minute{'s' * (minutes != 1)}" if delta >= 0 else "overdue"
    return f"{day} at {local:%H:%M} ({relative})"


def timer_label(item: ReminderRow) -> str:
    """A timer's name ("pasta"), "" when it has none."""
    return re.sub(r"\s*timer$", "", item["text"], flags=re.IGNORECASE).strip()


def timer_name(item: ReminderRow) -> str:
    """"pasta timer", or "10-minute timer" for one without a name."""
    return f"{timer_label(item) or spoken_duration(item['due'] - item['ts'])} timer"


def describe(item: ReminderRow, now: datetime) -> str:
    """One pending timer or reminder, as listed: "#2 pasta timer: 8 min 30 s left".

    Args:
        item: the timer or reminder.
        now: the current local time.

    Returns:
        The line.
    """
    due = datetime.fromtimestamp(item["due"], now.tzinfo or UTC)
    left = item["due"] - now.timestamp()
    if item["kind"] == "timer":
        when = f"{int(left // 60)} min {int(left % 60)} s left" if 0 <= left < 3600 else human_time(due, now)
        return f"#{item['id']} {timer_name(item)}: {when}"
    return f"#{item['id']} reminder \"{item['text']}\": {human_time(due, now)}"


def find_pending(items: Sequence[ReminderRow], which: str, *,
                 now: datetime | None = None) -> tuple[ReminderRow | None, str]:
    """The pending timer or reminder the user means.

    Args:
        items: what is pending.
        which: "#2", "timer 3", "the pasta timer", "the 10 minute one", "the dentist reminder", or "" for the only one.
        now: the current local time (for listing candidates).

    Returns:
        (the item, ""), or (None, the candidates to ask about).

    Raises:
        LookupError: ``which`` is only an id ("#2", "timer 2") and nothing pending has it: never another item.
    """
    words = which.lower().strip()
    if (named := _ID.fullmatch(words)) is not None:
        ident = int(named.group(1))
        if (found := next((item for item in items if item["id"] == ident), None)) is None:
            raise LookupError(f"There's no timer or reminder #{ident} running.")
        return found, ""
    secs = parse_duration(words)
    if (not secs and (match := re.search(r"(?:^|#|number\s*|\s)(\d+)\b", words))
            and (hit := next((item for item in items if item["id"] == int(match.group(1))), None)) is not None):
        return hit, ""
    kind = "timer" if "timer" in words else "reminder" if "remind" in words else None
    candidates = [item for item in items if kind is None or item["kind"] == kind] or list(items)
    if secs:
        candidates = [item for item in candidates if item["kind"] == "timer"
                      and abs((item["due"] - item["ts"]) - secs) < SAME_LENGTH_S] or candidates
    named = [word for word in re.findall(r"[a-z]+", words) if word not in _FILLER and word not in _NUMBERS]
    if named:
        candidates = [item for item in candidates if any(word in item["text"].lower() for word in named)] or candidates
    if len(candidates) == 1:
        return candidates[0], ""
    when = now or local_now()
    return None, "Several match; ask the user which one:\n" + "\n".join(describe(item, when) for item in candidates)


# ---------------------------------------------------------------------------------------------------- the tools
@group.tool(title="Start a timer", examples=("Set a timer for 12 minutes for the pasta", "Timer for 5 minutes"))
def set_timer(duration: Annotated[str, Field(description='Length of the timer, e.g. "5 minutes", "90 seconds", '
                                                         '"1h30m".')],
              label: Annotated[str, Field(description='Optional name, e.g. "pasta".')] = "") -> str:
    """Start a countdown timer; Orion says when it is done."""
    secs = parse_duration(duration)
    if not secs:
        return f"Could not understand the duration '{duration}'."
    name = " ".join(label.split())
    timer_id = ctx.memory.store.add_reminder(name, time.time() + secs, "timer")
    publish("reminders")
    minutes, seconds = divmod(int(secs), 60)
    length = (f"{minutes} min {seconds} s" if minutes and seconds else f"{minutes} min" if minutes
              else f"{seconds} seconds")
    return f"Timer #{timer_id}{f' ({name})' if name else ''} started for {length}."


@group.tool(title="Set a reminder", examples=("Remind me tomorrow at 9 to call the dentist",
                                              "Remind me in 20 minutes to stretch"))
def set_reminder(message: Annotated[str, Field(description="What to remind the user about.")],
                 when: Annotated[str, Field(description='When, e.g. "in 20 minutes", "tonight at 8", "tomorrow at 9am",'
                                                        ' "friday 18:00", "on the 15th at 10". A day without a time '
                                                        'means 9:00.')]) -> str:
    """Schedule a spoken reminder at a specific time."""
    now = local_now()
    due = parse_when(when, now, default_hour=ctx.config.reminders.default_hour)
    if due is None:
        return f"Could not understand the time '{when}'. Ask the user to rephrase."
    reminder_id = ctx.memory.store.add_reminder(" ".join(message.split()), due.timestamp(), "reminder")
    publish("reminders")
    return f"Reminder #{reminder_id} set for {human_time(due, now)}."


@group.tool(read_only=True, title="List timers and reminders",
            examples=("What timers are running?", "What reminders do I have?"))
def list_reminders() -> str:
    """List the running timers and the scheduled reminders, with the time left."""
    now = local_now()
    items = ctx.memory.store.pending_reminders()
    return "\n".join(describe(item, now) for item in items) or "No timers or reminders are running."


@group.tool(title="Cancel a timer or reminder", examples=("Cancel the pasta timer", "Stop the timer"))
def cancel_timer_or_reminder(which: Annotated[str, Field(description='Its number ("#2"), its name ("pasta"), its '
                                                                     'length ("10 minutes"), or empty for the only '
                                                                     'one running.')] = "") -> str:
    """Cancel (stop, delete) a running timer or a scheduled reminder."""
    items = ctx.memory.store.pending_reminders()
    target, why = _pending(items, which)
    if target is None:
        return why if items else "No timers or reminders are running, so there is nothing to cancel."
    ctx.memory.store.finish_reminder(target["id"])
    publish("reminders")
    what = timer_name(target) if target["kind"] == "timer" else f"reminder \"{target['text']}\""
    return f"Cancelled #{target['id']} {what}."


@group.tool(title="Add or take time off a timer", examples=("Add 5 minutes to the pasta timer",
                                                             "Take 2 minutes off the timer"))
def change_timer(which: Annotated[str, Field(description='Its number ("#2"), its name ("pasta"), its length ("10 '
                                                         'minutes"), or empty for the only one running.')] = "",
                 by: Annotated[str, Field(description='The time to add, e.g. "5 minutes"; with a minus sign, the time '
                                                      'to take off, e.g. "-2 minutes".')] = "") -> str:
    """Add time to a running timer, or take some off (taking off more than is left ends it at once)."""
    change = parse_duration(by.strip().lstrip("+-"))
    if not change:
        return f"Could not understand the time '{by}'."
    if by.strip().startswith("-"):
        change = -change
    timers = [item for item in ctx.memory.store.pending_reminders() if item["kind"] == "timer"]
    target, why = _pending(timers, which)
    if target is None:
        return why if timers else "No timer is running."
    now = local_now()
    due = max(now.timestamp(), target["due"] + change)
    ctx.memory.store.move_reminder(target["id"], due)
    publish("reminders")
    left = due - now.timestamp()
    return (f"Timer #{target['id']} ({timer_name(target)}) now ends at {datetime.fromtimestamp(due, now.tzinfo):%H:%M} "
            f"({int(left // 60)} min {int(left % 60)} s left).")


def _pending(items: Sequence[ReminderRow], which: str) -> tuple[ReminderRow | None, str]:
    """``find_pending`` for a tool: an id that isn't pending is the call's error."""
    from mcp.server.mcpserver.exceptions import ToolError

    try:
        return find_pending(items, which)
    except LookupError as e:
        raise ToolError(str(e)) from None


@group.tool(title="Manage your to-do list", idempotent=False,
            examples=("Add buy milk to my to-do list", "What is on my list?", "Tick off buy milk"))
def todo(action: Literal["add", "list", "complete", "remove"],
         item: Annotated[str, Field(description="The task (for add), or its number or some of its words (for "
                                                "complete and remove).")] = "") -> str:
    """Manage the user's to-do list."""
    text = " ".join(item.split())
    store = ctx.memory.store
    if action == "add":
        if not text:
            return "What should I add? The to-do item is empty."
        todo_id = store.add_todo(text)
        publish("todos")
        return f"Added to-do #{todo_id}: {text}"
    items = store.open_todos()
    if action == "list":
        return "\n".join(f"#{t['id']}: {t['text']}" for t in items) or "The to-do list is empty."
    if not text:
        return "Say which to-do (its number or some of its words)."
    target = (next((t for t in items if text.strip("# ") == str(t["id"])), None)
              or next((t for t in items if text.lower() in t["text"].lower()), None))
    if target is None:
        return f"No to-do matches '{text}'."
    if action == "complete":
        store.set_todo_done(target["id"])
    else:
        store.delete_todo(target["id"])
    publish("todos")
    return f"{'Completed' if action == 'complete' else 'Removed'}: {target['text']}"
