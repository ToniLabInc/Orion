"""The unit index: everything Orion remembers, as dated units he can search and recall.

Units are the vault's (an entry, a journal entry, a note section, an archive line) and the conversation log's (one
turn: the user's words and his reply). Each is a row of ``mem_items`` with its plain text, date keys and concepts (for
FTS5 keyword search), its vector (meaning search) and where it came from. The index is derived: ``rebuild`` makes it
again from the files and the log, and the owner (Orion's own process) keeps it current with ``sync_files`` (after
the watcher sees a file change) and ``sync_messages`` (after each turn). Searches never re-index.

Two ways to read it:
- ``search`` (the memory_search tool): recall-first. Keyword, meaning, concept and time lists fused by reciprocal rank,
  whole units, at most two per file.
- ``recall`` (before each reply): precision-first. Only trusted, dated, non-core units of other conversations can be
  recalled, and each must pass a gate: both rankers agree, it names a concept the request names, it falls in the time
  asked about, or its meaning is very close. Most turns recall nothing, in a few milliseconds.

Vectors live in RAM as one float32 matrix (rows appended as units change, deleted rows masked, compacted when a fifth
is dead), reloaded only when another process changed the index. ``parse_span`` reads time phrases ("yesterday
evening", "last week", "in September"); ``vocabulary`` ranks the trusted concepts for Whisper's prompt;
``calibrate_recall`` tunes the gates on labelled cases.
"""

import json
import math
import re
import threading
import time
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from functools import partial
from itertools import batched
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

import numpy as np

from orion.base import TRUSTED, Query, clip, local_zone
from orion.memory.store import Store
from orion.memory.vault import (
    CORE,
    FILES,
    KIND_OF_FILE,
    ConceptSet,
    Unit,
    Vault,
    concept_set,
    is_concept,
    link_text,
    links_in,
    local_ts,
)

if TYPE_CHECKING:
    import sqlite3

    from numpy.typing import NDArray

    from orion.config import Config, RetrievalConfig
    from orion.inference import Embedder

type Scope = Literal["all", "notes", "journal", "conversations", "history"]
type Gate = Literal["agree", "concept", "span", "strong"]
type VecRow = tuple[int, bytes | None, str, float, int | None, str]  # a written unit: id, vector, kind, ts, conv, source

RRF_K: Final = 60  # reciprocal rank fusion: 1 / (RRF_K + rank)
FTS_MAX_TERMS: Final = 24  # the most words of a request put to the keyword search
RECALL_MIN_TERMS: Final = 2  # a request with fewer content words (and no time or concept) recalls nothing
TURN_MIN_REPLY_CHARS: Final = 40  # a shorter reply makes the turn a command ("Done, sir."): never recalled
SEARCH_PER_FILE: Final = 2  # memory_search shows at most this many units of one file (or conversation)
COMPACT_AT: Final = 0.2  # the vector matrix is rebuilt once this share of its rows is dead
MESSAGE_BATCH: Final = 500  # log messages indexed per query
DAY_S: Final = 86400.0

EPISODIC: Final = frozenset({"journal", "turn", "command"})  # kinds whose rank decays with age
RECALLABLE: Final = frozenset({"journal", "note", "facts", "turn"})  # kinds recall may inject
SPAN_KINDS: Final = ("journal", "turn", "note")  # kinds a time phrase lists
SPAN_GATE_KINDS: Final = frozenset({"journal", "turn"})  # kinds the span gate admits
SCOPES: Final[Mapping[str, frozenset[str]]] = {
    "all": frozenset({"journal", "note", "facts", "unverified", "turn", "command"}),
    "notes": frozenset({"note"}),
    "journal": frozenset({"journal"}),
    "conversations": frozenset({"turn", "command"}),
    "history": frozenset({"archive"}),
}
CORE_KINDS: Final = frozenset(CORE)
ENTRY_KINDS: Final = frozenset(FILES)
_TRUSTED_ONLY: Final = "i.source NOT IN ('tainted', 'app')"  # the SQL filter (on alias i) for trusted units
_KIND_CODES: Final[Mapping[str, int]] = {kind: i for i, kind in enumerate(
    ("user", "orion", "facts", "unverified", "journal", "note", "archive", "turn", "command"))}

_STOP_TEXT: Final = """
a about above after again against all am an and any are as at be because been before being below between both but by
can could did do does doing down during each few for from further had has have having he her here hers herself him
himself his how i if in into is it its itself just me more most my myself no nor not now of off on once only or other
our ours ourselves out over own same she should so some such than that the their theirs them themselves then there
these they this those through to too under until up very was we were what when where which while who whom why will
with would you your yours yourself yourselves ok okay please tell know remember think thing things something anything
said say says talk talked talking told ask asked anything orion user users sir hey hi hello let lets get got go
going yeah yes
"""
STOPWORDS: Final = frozenset(_STOP_TEXT.split())  # no meaning of their own ("user": in nearly every memory)
_WORD: Final = re.compile(r"\w+", re.UNICODE)
_SENTENCE: Final = re.compile(r"(?<=[.!?])\s+")
_NUMBERS: Final = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                   "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
_COUNT: Final = r"(\d{1,2}|a couple of|a few|an?|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
_MONTHS: Final = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
                  "november", "december")
_WEEKDAYS: Final = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_PARTS: Final[Mapping[str, tuple[int, int]]] = {"morning": (5, 12), "afternoon": (12, 18), "evening": (18, 24),
                                                "night": (18, 29)}  # night ends at 05:00 the next day
_MONTH: Final = r"(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|" \
                r"apr|jun|jul|aug|sep|sept|oct|nov|dec)"
_ORDINAL: Final = r"(\d{1,2})(?:st|nd|rd|th)?"
_UPSERT: Final = ("INSERT INTO mem_items (key, kind, path, anchor, text, keys, ts, conv, source, vec) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET kind = excluded.kind, "
                  "path = excluded.path, anchor = excluded.anchor, text = excluded.text, keys = excluded.keys, "
                  "ts = excluded.ts, conv = excluded.conv, source = excluded.source, vec = excluded.vec RETURNING id")


# ---------------------------------------------------------------------------------------------------- shapes
@dataclass(frozen=True, slots=True)
class Span:
    """A stretch of time a request is about.

    Attributes:
        start: its start, in epoch seconds.
        end: its end, in epoch seconds (exclusive).
        hard: whether it filters what is listed (a time question); a soft span ("recently") only adds a list.
        label: the words it came from ("yesterday evening").
    """

    start: float
    end: float
    hard: bool
    label: str


@dataclass(frozen=True, slots=True)
class Hit:
    """One unit found.

    Attributes:
        id: its row id.
        key: its key (an entry's content key).
        kind: "user", "orion", "facts", "unverified", "journal", "note", "archive", "turn" or "command".
        path: its file (relative to the memory folder), or "conv:<n>" for a conversation turn.
        ts: its date, in epoch seconds.
        text: its plain text.
        source: where it came from (see ``base.Provenance``).
        score: its fused rank score (higher is better).
    """

    id: int
    key: str
    kind: str
    path: str
    ts: float
    text: str
    source: str
    score: float

    def tag(self) -> str:
        """A short label saying what and when: "2 Oct · journal", "28 Sep · chat", "note: drone-pid",
        "unverified · from a web page", "fact", "until 2 Oct"."""
        day = _day_label(self.ts)
        if self.kind == "journal":
            return f"{day} · journal"
        if self.kind in ("turn", "command"):
            return f"{day} · chat"
        if self.kind == "note":
            return f"note: {Path(self.path).stem}"
        if self.kind == "unverified":
            return "unverified · from a web page or another app"
        if self.kind == "archive":
            return f"until {day}"
        return {"user": "about the user", "orion": "his notes"}.get(self.kind, "fact")


@dataclass(frozen=True, slots=True)
class Candidate:
    """What the gates know about one candidate of a recall (pure input to ``gate``).

    Attributes:
        id: its row id.
        kind: its kind.
        ts: its date.
        source: where it came from.
        bm25_rank: its rank in the keyword list (0 = best), None when absent.
        cos: its cosine to the request, None without vectors.
        cos_rank: its rank in the meaning list, None when absent.
        concept: whether it is linked to a concept the request names.
        in_span: whether its date falls in the request's hard span.
    """

    id: int
    kind: str
    ts: float
    source: str
    bm25_rank: int | None
    cos: float | None
    cos_rank: int | None
    concept: bool
    in_span: bool


@dataclass(frozen=True, slots=True)
class RecallResult:
    """The memories recalled before a reply.

    Attributes:
        items: 0 to ``retrieval.recall_max_items`` units, within ``retrieval.recall_chars``.
        ms: how long it took.
    """

    items: tuple[Hit, ...]
    ms: float


@dataclass(frozen=True, slots=True)
class SyncReport:
    """What one sync changed.

    Attributes:
        files: files read again.
        units_added: units inserted or changed.
        units_removed: units deleted.
        turns: conversation turns indexed.
        version: the index version after it.
        ms: how long it took.
    """

    files: int
    units_added: int
    units_removed: int
    turns: int
    version: int
    ms: float


@dataclass(frozen=True, slots=True)
class EvalCase:
    """One labelled recall case for ``calibrate_recall``.

    Attributes:
        query: the request.
        now: when it is asked (local, aware).
        expect: words each unit that should be recalled contains; () when nothing should be.
    """

    query: str
    now: datetime
    expect: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RecallCalibration:
    """The best gate thresholds found on labelled cases, and how they did.

    Attributes:
        min_cosine: ``retrieval.recall_min_cosine``.
        rank_window: ``retrieval.recall_rank_window``.
        solo_cosine: ``retrieval.recall_solo_cosine``.
        precision: share of recalled units that were expected.
        recall: share of expected units recalled.
        false_injection: share of cases that should recall nothing but did.
    """

    min_cosine: float
    rank_window: int
    solo_cosine: float
    precision: float
    recall: float
    false_injection: float


@dataclass(slots=True)
class _Row:
    """A unit as the index stores it (one part, when a long unit is split), with its vector once embedded."""

    key: str
    kind: str
    path: str
    anchor: str
    text: str
    keys: str
    ts: float
    conv: int | None
    source: str
    concepts: tuple[str, ...]
    vec: bytes | None = None


# A changed file for ``_apply_files``: its path, its stat (None when gone), its rows, the rows gone, the rows to write.
type _FileDiff = tuple[str, tuple[int, int] | None, dict[str, _Row], list["sqlite3.Row"], list[_Row]]


# ---------------------------------------------------------------------------------------------------- pure parts
def content_terms(text: str) -> list[str]:
    """The words of a request that carry its meaning: lower-cased, longer than one character, stopwords removed.

    Args:
        text: the request.

    Returns:
        The words, in order, without repeats.
    """
    return list(dict.fromkeys(w for w in _WORD.findall(text.lower()) if len(w) > 1 and w not in STOPWORDS))


def fts_query(text: str, max_terms: int = FTS_MAX_TERMS) -> str:
    """A safe FTS5 query: the request's content words, each quoted, OR-ed (the user's text never becomes syntax).

    Args:
        text: the request.
        max_terms: the most words used.

    Returns:
        '"talk" OR "routers"', or "" when no word is left.
    """
    terms = content_terms(text)[:max_terms]
    return " OR ".join(f'"{term}"' for term in terms)


def rrf(lists: Sequence[Sequence[int]], k: int = RRF_K) -> dict[int, float]:
    """Reciprocal rank fusion: each id scores the sum of 1 / (k + rank + 1) over the lists it appears in.

    Args:
        lists: ranked lists of ids (best first).
        k: the damping constant.

    Returns:
        id -> fused score.
    """
    scores: dict[int, float] = {}
    for ranked in lists:
        for rank, item in enumerate(ranked):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank + 1)
    return scores


def recency_prior(age_days: float, half_life_days: float, weight: float) -> float:
    """The recency boost of an episodic unit: ``weight`` when new, halving every ``half_life_days``.

    Args:
        age_days: its age.
        half_life_days: the half-life.
        weight: the boost of a brand-new unit.

    Returns:
        ``weight * 2^(-age / half_life)`` (a unit from the future counts as new).
    """
    return weight * 2.0 ** (-max(0.0, age_days) / half_life_days)


def gate(c: Candidate, *, span_hard: bool, window: int, min_cosine: float, solo_cosine: float) -> Gate | None:
    """Whether a recall candidate may be injected, and why.

    Only trusted journal entries, notes, facts and turns are eligible. Then, first that passes:
    - "agree": both rankers put it in their top ``window`` and its cosine is at least ``min_cosine``;
    - "concept": it is linked to a concept the request names, and the keyword ranker puts it in its top
      ``2 * window`` or its cosine is at least ``min_cosine``;
    - "span": the request asks about a time, it falls in it, and it is a journal entry or a turn;
    - "strong": its cosine is at least ``solo_cosine``.

    Args:
        c: the candidate.
        span_hard: whether the request has a hard time span.
        window: ``retrieval.recall_rank_window``.
        min_cosine: ``retrieval.recall_min_cosine``.
        solo_cosine: ``retrieval.recall_solo_cosine``.

    Returns:
        The gate passed, or None.
    """
    if c.kind not in RECALLABLE or c.source not in TRUSTED:
        return None
    cos = c.cos if c.cos is not None else -1.0
    if (c.bm25_rank is not None and c.cos_rank is not None and c.bm25_rank < window and c.cos_rank < window
            and cos >= min_cosine):
        return "agree"
    if c.concept and ((c.bm25_rank is not None and c.bm25_rank < 2 * window) or cos >= min_cosine):
        return "concept"
    if span_hard and c.in_span and c.kind in SPAN_GATE_KINDS:
        return "span"
    if cos >= solo_cosine:
        return "strong"
    return None


def pack(hits: Sequence[Hit], *, budget: int, max_items: int) -> list[Hit]:
    """Fit recalled units in a character budget: whole units in order; one that doesn't fit is skipped; only a first
    unit longer than the whole budget is cut at a word boundary.

    Args:
        hits: the units, best first.
        budget: the most characters of text.
        max_items: the most units.

    Returns:
        The units kept.
    """
    out: list[Hit] = []
    used = 0
    for hit in hits:
        if len(out) >= max_items:
            break
        if used + len(hit.text) <= budget:
            out.append(hit)
            used += len(hit.text)
        elif not out:
            out.append(Hit(hit.id, hit.key, hit.kind, hit.path, hit.ts, clip(hit.text, budget, flat=False),
                           hit.source, hit.score))
            used = budget
    return out


def vocabulary_scores(rows: Iterable[tuple[str, int, float]], now: float,
                      half_life_days: float) -> list[tuple[str, float]]:
    """Rank concepts for the speech recogniser: often named and recently named first.

    Args:
        rows: (concept, how many units link it, newest of those dates).
        now: epoch seconds.
        half_life_days: how fast a concept's weight halves with age.

    Returns:
        (concept, log1p(count) * 2^(-age / half-life)), best first.
    """
    scored = [(name, math.log1p(count) * 2.0 ** (-max(0.0, now - newest) / DAY_S / half_life_days))
              for name, count, newest in rows]
    return sorted(scored, key=lambda item: item[1], reverse=True)


def parse_span(text: str, now: datetime) -> Span | None:
    """The time a request is about, in local time; the first rule that matches wins.

    Explicit dates ("2026-10-02", "2 October", "October 2nd", "the 2nd of October": the latest past one), "the day
    before yesterday", "yesterday" (with a part of the day), "last night", "this morning/afternoon/evening",
    "tonight", "today", "N days ago", "last week", "N weeks ago", "this week", "last month", "this month",
    "N months ago", a weekday after on/last/this/past/since, a month name ("in September", "September 2025"), and the
    vague "recently", "lately", "the other day", "a while ago" (soft spans).

    Args:
        text: the request.
        now: the current local time, aware of its zone.

    Returns:
        The span, or None when the request names no time.
    """
    t = " ".join(text.lower().split())
    zone = now.tzinfo or local_zone()
    today = now.date()
    for rule in (_explicit_date, _relative_days, _parts_of_today, _days_ago, _weeks, _months, _weekday, _month_name):
        if (found := rule(t, now, today, zone)) is not None:
            return found
    return _vague(t, now)


def calibrate_recall(cases: Sequence[EvalCase],
                     candidates: Callable[[EvalCase], Sequence[tuple[Candidate, str, bool]]]) -> RecallCalibration:
    """Find the gate thresholds that recall precisely on labelled cases.

    Sweeps ``recall_min_cosine`` 0.15 to 0.60, ``recall_rank_window`` 5, 10 and 15, and ``recall_solo_cosine`` 0.40
    to 0.80, and keeps the most precise setting that recalls at least half of what is expected and falsely injects
    into at most a tenth of the cases that expect nothing (the best recall breaks ties).

    Args:
        cases: the labelled cases.
        candidates: each case's candidates as (features, text, the request had a hard span).

    Returns:
        The best setting found (zeros when no case expects anything).
    """
    features = [(case, list(candidates(case))) for case in cases]
    best: RecallCalibration | None = None
    for min_cosine in np.arange(0.15, 0.601, 0.05):
        for window in (5, 10, 15):
            for solo in np.arange(0.40, 0.801, 0.05):
                tried = _score_setting(features, float(min_cosine), window, float(solo))
                if tried.recall < 0.5 or tried.false_injection > 0.1:
                    continue
                if best is None or (tried.precision, tried.recall) > (best.precision, best.recall):
                    best = tried
    return best or RecallCalibration(0.0, 0, 0.0, 0.0, 0.0, 1.0)


def _score_setting(features: Sequence[tuple[EvalCase, list[tuple[Candidate, str, bool]]]], min_cosine: float,
                   window: int, solo: float) -> RecallCalibration:
    """How one gate setting does on the labelled cases."""
    recalled = right = expected = found = empty = false = 0
    for case, items in features:
        passing = [text for c, text, hard in items
                   if gate(c, span_hard=hard, window=window, min_cosine=min_cosine, solo_cosine=solo)]
        recalled += len(passing)
        right += sum(1 for text in passing if any(word in text for word in case.expect))
        expected += len(case.expect)
        found += sum(1 for word in case.expect if any(word in text for text in passing))
        if not case.expect:
            empty += 1
            false += bool(passing)
    return RecallCalibration(min_cosine=round(min_cosine, 2), rank_window=window, solo_cosine=round(solo, 2),
                             precision=right / recalled if recalled else 1.0,
                             recall=found / expected if expected else 1.0,
                             false_injection=false / empty if empty else 0.0)


# ---------------------------------------------------------------------------------------------------- time phrases
def _day(day: date, zone: tzinfo, label: str) -> Span:
    """A whole local day as a hard span."""
    return Span(local_ts(day, zone), local_ts(day + timedelta(days=1), zone), True, label)


def _count(word: str) -> int:
    """A spoken count: digits, "one" to "twelve", "a"/"an" (1), "a couple of" (2), "a few" (3)."""
    if word.isdigit():
        return int(word)
    return {"a couple of": 2, "a few": 3}.get(word, _NUMBERS.get(word, 0))


def _month_number(word: str) -> int:
    """1-12 for a month name or its abbreviation."""
    return next(i for i, name in enumerate(_MONTHS, 1) if name.startswith(word[:3]))


def _latest_past(year: int, month: int, day: int, today: date) -> date | None:
    """That date this year, or last year when it is still ahead; None for an impossible date."""
    for candidate_year in (year, year - 1):
        try:
            found = date(candidate_year, month, day)
        except ValueError:
            return None
        if found <= today:
            return found
    return None


def _explicit_date(t: str, now: datetime, today: date, zone: tzinfo) -> Span | None:
    """"2026-10-02", "2 October", "2nd of October", "October 2nd"."""
    if match := re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", t):
        try:
            return _day(date(int(match.group(1)), int(match.group(2)), int(match.group(3))), zone, match.group(0))
        except ValueError:
            return None
    match = (re.search(rf"\b(?:the\s+)?{_ORDINAL}\s+(?:of\s+)?{_MONTH}\b", t)
             or re.search(rf"\b{_MONTH}\s+(?:the\s+)?{_ORDINAL}\b", t))
    if match is None:
        return None
    first, second = match.group(1), match.group(2)
    day, month = (int(first), second) if first.isdigit() else (int(second), first)
    if not 1 <= day <= 31:
        return None
    found = _latest_past(today.year, _month_number(month), day, today)
    return _day(found, zone, match.group(0)) if found else None


def _relative_days(t: str, now: datetime, today: date, zone: tzinfo) -> Span | None:
    """"the day before yesterday", "yesterday" (morning, afternoon, evening), "last night"."""
    if "day before yesterday" in t:
        return _day(today - timedelta(days=2), zone, "the day before yesterday")
    yesterday = today - timedelta(days=1)
    if "last night" in t:
        return Span(local_ts(yesterday, zone, 18), local_ts(today, zone, 5), True, "last night")
    if match := re.search(r"\byesterday(?:\s+(?:in the\s+)?(morning|afternoon|evening|night))?\b", t):
        if part := match.group(1):
            start, end = _PARTS[part]
            return Span(local_ts(yesterday, zone, start), local_ts(yesterday, zone, end), True, match.group(0))
        return _day(yesterday, zone, "yesterday")
    return None


def _parts_of_today(t: str, now: datetime, today: date, zone: tzinfo) -> Span | None:
    """"this morning/afternoon/evening", "tonight", "today", "earlier today"."""
    if match := re.search(r"\bthis (morning|afternoon|evening)\b", t):
        start, end = _PARTS[match.group(1)]
        return Span(local_ts(today, zone, start), local_ts(today, zone, end), True, match.group(0))
    if re.search(r"\btonight\b", t):
        return Span(local_ts(today, zone, 18), local_ts(today, zone, 24), True, "tonight")
    if re.search(r"\b(?:earlier )?today\b", t):
        return Span(local_ts(today, zone), now.timestamp(), True, "today")
    return None


def _days_ago(t: str, now: datetime, today: date, zone: tzinfo) -> Span | None:
    """"3 days ago", "three days ago", "a couple of days ago"."""
    if (match := re.search(rf"\b{_COUNT}\s+days?\s+ago\b", t)) and (n := _count(match.group(1))):
        return _day(today - timedelta(days=n), zone, match.group(0))
    return None


def _weeks(t: str, now: datetime, today: date, zone: tzinfo) -> Span | None:
    """"last week", "2 weeks ago", "this week" (Monday to Monday)."""
    monday = today - timedelta(days=today.weekday())
    if "this week" in t:
        return Span(local_ts(monday, zone), now.timestamp(), True, "this week")
    back = 1 if "last week" in t else 0
    if not back and (match := re.search(rf"\b{_COUNT}\s+weeks?\s+ago\b", t)):
        back = _count(match.group(1))
    if back:
        start = monday - timedelta(weeks=back)
        return Span(local_ts(start, zone), local_ts(start + timedelta(weeks=1), zone), True, f"{back} week(s) ago")
    return None


def _month_span(year: int, month: int, zone: tzinfo, label: str) -> Span:
    """A whole calendar month as a hard span."""
    start = date(year, month, 1)
    end = date(year + (month == 12), month % 12 + 1, 1)
    return Span(local_ts(start, zone), local_ts(end, zone), True, label)


def _months(t: str, now: datetime, today: date, zone: tzinfo) -> Span | None:
    """"last month", "this month", "2 months ago" (calendar months)."""
    if "this month" in t:
        return Span(local_ts(today.replace(day=1), zone), now.timestamp(), True, "this month")
    back = 1 if "last month" in t else 0
    if not back and (match := re.search(rf"\b{_COUNT}\s+months?\s+ago\b", t)):
        back = _count(match.group(1))
    if back:
        index = today.year * 12 + today.month - 1 - back
        return _month_span(index // 12, index % 12 + 1, zone, f"{back} month(s) ago")
    return None


def _weekday(t: str, now: datetime, today: date, zone: tzinfo) -> Span | None:
    """"on Monday", "last Monday", "this Monday", "past Monday", "since Monday": the latest such day before today
    ("this" is today when it is that day); "since" runs to now."""
    match = re.search(r"\b(on|last|this|past|since)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", t)
    if match is None:
        return None
    word, weekday = match.group(1), _WEEKDAYS.index(match.group(2))
    back = (today.weekday() - weekday) % 7
    if back == 0 and word != "this":
        back = 7
    day = today - timedelta(days=back)
    if word == "since":
        return Span(local_ts(day, zone), now.timestamp(), True, match.group(0))
    return _day(day, zone, match.group(0))


def _month_name(t: str, now: datetime, today: date, zone: tzinfo) -> Span | None:
    """"in September", "September 2025" (the latest past one when no year is said)."""
    match = re.search(rf"\b(?:in\s+{_MONTH}(?:\s+(\d{{4}}))?|{_MONTH}\s+(\d{{4}}))\b", t)
    if match is None:
        return None
    name = match.group(1) or match.group(3)
    year_text = match.group(2) or match.group(4)
    month = _month_number(name)
    year = int(year_text) if year_text else today.year - (month > today.month)
    return _month_span(year, month, zone, match.group(0))


def _vague(t: str, now: datetime) -> Span | None:
    """"recently", "lately", "the other day" (the last week) and "a while ago" (one week to two months back): soft."""
    stamp = now.timestamp()
    if re.search(r"\b(?:recently|lately|the other day)\b", t):
        return Span(stamp - 7 * DAY_S, stamp, False, "recently")
    if "a while ago" in t:
        return Span(stamp - 60 * DAY_S, stamp - 7 * DAY_S, False, "a while ago")
    return None


def _day_label(ts: float) -> str:
    """A local date as "2 Oct"."""
    local = time.localtime(ts)
    return f"{local.tm_mday} {time.strftime('%b', local)}"


def _time_keys(ts: float, zone: tzinfo) -> str:
    """Words for a unit's date, so a keyword search for "Friday", "October" or "evening" finds it."""
    local = datetime.fromtimestamp(ts, zone)
    hour = local.hour
    part = "morning" if 5 <= hour < 12 else "afternoon" if 12 <= hour < 18 else "evening" if hour < 23 else "night"
    if hour < 5:
        part = "night"
    return f"{local:%A %B} {local.day} {local.year} {local:%Y-%m-%d} {part}"


def _split(text: str, limit: int) -> list[str]:
    """A long text in parts of at most ``limit`` characters, cut at paragraphs, then sentences, then words."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    pieces = [p for block in text.split("\n\n") for p in (_SENTENCE.split(block) if len(block) > limit else [block])]
    for piece in pieces:
        while len(piece) > limit:
            head = piece[:limit].rsplit(" ", 1)[0] or piece[:limit]
            parts.append(head.strip())
            piece = piece[len(head):].strip()
        if current and len(current) + 2 + len(piece) > limit:
            parts.append(current)
            current = ""
        current = f"{current}\n\n{piece}" if current else piece
    if current:
        parts.append(current)
    return [part for part in parts if part.strip()]


def _blob(vector: "NDArray[np.float32]") -> bytes:
    """A vector as stored: float16 bytes."""
    return np.asarray(vector, dtype=np.float16).tobytes()


# ---------------------------------------------------------------------------------------------------- vectors in RAM
class _Vectors:
    """The units' vectors as one growable float32 matrix with per-row metadata; deleted rows are masked."""

    def __init__(self) -> None:
        """Start empty."""
        self.lock = threading.Lock()
        self.dim = 0
        self.size = 0
        self.dead = 0
        self.vecs = np.zeros((0, 0), dtype=np.float32)
        self.ids = np.zeros(0, dtype=np.int64)
        self.kinds = np.zeros(0, dtype=np.uint8)
        self.ts = np.zeros(0, dtype=np.float64)
        self.conv = np.zeros(0, dtype=np.int64)
        self.trusted = np.zeros(0, dtype=bool)
        self.alive = np.zeros(0, dtype=bool)
        self.row_of: dict[int, int] = {}

    def load(self, rows: Sequence[tuple[int, bytes, str, float, int | None, str]]) -> None:
        """Replace everything with these rows (id, vector bytes, kind, ts, conv, source)."""
        with self.lock:
            self.size = self.dead = 0
            self.row_of.clear()
            self._reserve(len(rows), self._width(rows))
            self._append(rows)

    def upsert(self, rows: Sequence[tuple[int, bytes, str, float, int | None, str]]) -> None:
        """Add rows, masking the previous row of the same ids."""
        with self.lock:
            self._kill(row[0] for row in rows)
            self._reserve(self.size + len(rows), self._width(rows))
            self._append(rows)
            if self.size and self.dead / self.size > COMPACT_AT:
                self._compact()

    def remove(self, ids: Iterable[int]) -> None:
        """Mask rows."""
        with self.lock:
            self._kill(ids)

    def nearest(self, query: "NDArray[np.float32]", allow: Callable[["_Vectors"], "NDArray[np.bool_]"],
                limit: int) -> list[tuple[int, float]]:
        """The rows closest to a unit query among those ``allow`` admits.

        Args:
            query: a unit vector.
            allow: builds the mask of admissible rows (over ``size`` rows) from this object's metadata arrays.
            limit: how many.

        Returns:
            (id, cosine), best first.
        """
        with self.lock:
            if not self.size or query.shape[0] != self.dim:
                return []
            mask = self.alive[:self.size] & allow(self)
            if not mask.any():
                return []
            scores = self.vecs[:self.size] @ query
            scores[~mask] = -np.inf
            take = min(limit, int(mask.sum()))
            top = np.argpartition(-scores, take - 1)[:take]
            top = top[np.argsort(-scores[top])]
            return [(int(self.ids[i]), float(scores[i])) for i in top]

    def _width(self, rows: Sequence[tuple[int, bytes, str, float, int | None, str]]) -> int:
        """The vector width of these rows (the current one when there are none)."""
        return len(rows[0][1]) // 2 if rows else self.dim

    def _reserve(self, needed: int, dim: int) -> None:
        """Grow the arrays (doubling) to hold ``needed`` rows of width ``dim``; a new width starts afresh."""
        if dim != self.dim:
            self.dim, self.size, self.dead = dim, 0, 0
            self.row_of.clear()
            self.vecs = np.zeros((0, dim), dtype=np.float32)
        capacity = self.vecs.shape[0]
        if needed <= capacity:
            return
        grown = max(needed, capacity * 2, 64)
        self.vecs = _grow(self.vecs, (grown, self.dim))
        self.ids = _grow(self.ids, (grown,))
        self.kinds = _grow(self.kinds, (grown,))
        self.ts = _grow(self.ts, (grown,))
        self.conv = _grow(self.conv, (grown,))
        self.trusted = _grow(self.trusted, (grown,))
        self.alive = _grow(self.alive, (grown,))

    def _append(self, rows: Sequence[tuple[int, bytes, str, float, int | None, str]]) -> None:
        """Write rows at the end (capacity already reserved)."""
        for row_id, blob, kind, ts, conv, source in rows:
            if len(blob) // 2 != self.dim:
                continue
            i = self.size
            self.vecs[i] = np.frombuffer(blob, dtype=np.float16)
            self.ids[i], self.kinds[i], self.ts[i] = row_id, _KIND_CODES.get(kind, 255), ts
            self.conv[i], self.trusted[i], self.alive[i] = -1 if conv is None else conv, source in TRUSTED, True
            self.row_of[row_id] = i
            self.size += 1

    def _kill(self, ids: Iterable[int]) -> None:
        """Mask the rows of these ids."""
        for row_id in ids:
            if (i := self.row_of.pop(row_id, None)) is not None:
                self.alive[i] = False
                self.dead += 1

    def _compact(self) -> None:
        """Drop masked rows."""
        keep = np.flatnonzero(self.alive[:self.size])
        for name in ("vecs", "ids", "kinds", "ts", "conv", "trusted", "alive"):
            array = getattr(self, name)
            array[:len(keep)] = array[keep]
        self.size, self.dead = len(keep), 0
        self.row_of = {int(row_id): i for i, row_id in enumerate(self.ids[:self.size])}


def _grow(array: np.ndarray, shape: tuple[int, ...]) -> np.ndarray:
    """A bigger copy of an array (the old values at the start)."""
    out = np.zeros(shape, dtype=array.dtype)
    out[:array.shape[0]] = array
    return out


# ---------------------------------------------------------------------------------------------------- the index
class Index:
    """The unit index over one database and one memory folder (see the module docstring)."""

    def __init__(self, store: Store, vault: Vault, embedder: "Embedder", cfg: "Config") -> None:
        """Prepare; nothing is read until the index is first used.

        Args:
            store: the database.
            vault: the memory folder.
            embedder: the process's embedder (loaded when vectors are first needed).
            cfg: the live settings (``retrieval.*``).
        """
        self.store = store
        self.vault = vault
        self.embedder = embedder
        self.cfg = cfg
        self._vectors = _Vectors()
        self._loaded_version = -1  # the index version the vectors in RAM were loaded at
        self._concepts: tuple[int, ConceptSet | None] = (-1, None)  # (index version, the trusted concepts)
        self._load_lock = threading.Lock()

    @property
    def retrieval(self) -> "RetrievalConfig":
        """The live retrieval settings."""
        return self.cfg.retrieval

    @property
    def version(self) -> int:
        """The index version: it changes with every sync, in any process."""
        return int(self.store.get_setting("index_version", "0") or 0)

    # ------------------------------------------------------------------------------------------------ syncing
    def sync_files(self, paths: Collection[Path] | None = None) -> SyncReport:
        """Index the memory files that changed since their last sync (their stat differs).

        Call it on the database's writer thread (the owner) or a worker thread. Entries found without metadata are
        recorded as written by hand; entries gone from a file are marked superseded ("edited by hand").

        Args:
            paths: the files to look at (None: every memory file, and the removal of files that are gone).

        Returns:
            What changed.
        """
        started = time.monotonic()
        with self.store.reading() as db:
            known = {str(r[0]): (int(r[1]), int(r[2])) for r in db.execute("SELECT path, mtime_ns, size FROM "
                                                                             "index_files")}
        root = self.vault.root
        files = self.vault.files() if paths is None else [p for p in paths if p.suffix == ".md"]
        changed: list[tuple[str, Path, tuple[int, int] | None]] = []
        for path in files:
            if not path.is_relative_to(root):
                continue
            rel = path.relative_to(root).as_posix()
            try:
                st = path.stat()
                stamp: tuple[int, int] | None = (st.st_mtime_ns, st.st_size)
            except FileNotFoundError:
                stamp = None
            if stamp != known.get(rel):
                changed.append((rel, path, stamp))
        if paths is None:
            present = {path.relative_to(root).as_posix() for path in files}
            changed += [(rel, root / rel, None) for rel in known if rel not in present]
        if not changed:
            return SyncReport(0, 0, 0, 0, self._loaded_version, (time.monotonic() - started) * 1000)
        meta = self._file_meta([rel for rel, _, stamp in changed if stamp is not None])
        indexed: dict[str, dict[str, sqlite3.Row]] = {}
        with self.store.reading() as db:
            for row in db.execute("SELECT id, key, path, text, keys, ts, source, kind FROM mem_items WHERE path IN "
                                  "(SELECT value FROM json_each(?))", (json.dumps([rel for rel, _, _ in changed]),)):
                indexed.setdefault(str(row["path"]), {})[str(row["key"])] = row
        diffs: list[_FileDiff] = []
        for rel, path, stamp in changed:
            new = {row.key: row for row in (self._rows_of(self.vault.units(path), meta) if stamp else ())}
            had = indexed.get(rel, {})
            fresh = [row for key, row in new.items() if key not in had or (had[key]["text"], had[key]["keys"],
                     float(had[key]["ts"]), had[key]["source"]) != (row.text, row.keys, row.ts, row.source)]
            diffs.append((rel, stamp, new, [row for key, row in had.items() if key not in new], fresh))
        self._embed([row for *_, fresh in diffs for row in fresh])
        added, removed, version = self.store.write(lambda db: self._apply_files(db, diffs))
        self._after_write(added, removed, version)
        return SyncReport(len(changed), len(added), len(removed), 0, version, (time.monotonic() - started) * 1000)

    def sync_messages(self) -> int:
        """Index the conversation turns logged since the last sync (a turn's later messages update its unit).

        Returns:
            How many turns were indexed.
        """
        done = 0
        while True:
            last = int(self.store.get_setting("indexed_message_id", "0") or 0)
            with self.store.reading() as db:
                fresh = db.execute("SELECT id, turn FROM messages WHERE id > ? ORDER BY id LIMIT ?",
                                   (last, MESSAGE_BATCH)).fetchall()
                if not fresh:
                    return done
                turns = {str(r["turn"]) for r in fresh if r["turn"]}
                rows = db.execute("SELECT id, role, content, source, ts, conv, turn FROM messages WHERE turn IN "
                                  "(SELECT value FROM json_each(?)) ORDER BY id", (json.dumps(list(turns)),)).fetchall()
            tainted = self.store.tainted_turns(turns)
            concepts = self._concept_set()
            units = [unit for turn, messages in _group_turns(rows).items()
                     for unit in self._turn_rows(turn, messages, turn in tainted, concepts)]
            self._embed(units)
            newest = int(fresh[-1]["id"])
            added, removed, version = self.store.write(partial(self._apply_turns, turns=turns, rows=units,
                                                               newest=newest))
            self._after_write(added, removed, version)
            done += len(turns)
            if len(fresh) < MESSAGE_BATCH:
                return done

    def rebuild(self) -> SyncReport:
        """Make the whole index again from the memory files and the conversation log (and re-embed everything).

        Returns:
            What the rebuild indexed.
        """
        started = time.monotonic()

        def clear(db: "sqlite3.Connection") -> None:
            """Empty the index tables and reset its progress."""
            for statement in ("DELETE FROM item_concepts", "DELETE FROM mem_items", "DELETE FROM index_files",
                              "INSERT INTO mem_fts(mem_fts) VALUES ('rebuild')"):
                db.execute(statement)
            db.executemany("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                           [("indexed_message_id", "0"), ("embedder", self.embedder.id)])

        self.store.write(clear)
        with self._vectors.lock:
            self._loaded_version = -1
        files = self.sync_files()
        turns = self.sync_messages()
        return SyncReport(files.files, files.units_added, files.units_removed, turns, self.version,
                          (time.monotonic() - started) * 1000)

    def ensure_embedder(self) -> int:
        """Re-embed every unit when the vectors were made by another model (or none); call it from the owner.

        Returns:
            How many units were embedded.
        """
        model = self.embedder.id
        if not model or self.store.get_setting("embedder") == model:
            return 0
        with self.store.reading() as db:
            items = db.execute("SELECT id, text FROM mem_items").fetchall()
        done = 0
        for batch in batched(items, MESSAGE_BATCH):
            vectors = self.embedder.encode([str(r["text"]) for r in batch])
            if vectors is None:
                return done
            pairs = [(_blob(v), int(r["id"])) for r, v in zip(batch, vectors, strict=True)]
            self.store.write(lambda db, rows=pairs: db.executemany("UPDATE mem_items SET vec = ? WHERE id = ?", rows))
            done += len(batch)

        def finish(db: "sqlite3.Connection") -> int:
            """Record the model and bump the version."""
            db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('embedder', ?)", (model,))
            return _bump(db)

        version = self.store.write(finish)
        self._reload(version)
        return done

    # ------------------------------------------------------------------------------------------------ reading
    def search(self, q: Query, *, scope: Scope = "all", span: Span | None = None, k: int,
               include_core: bool) -> list[Hit]:
        """Search memory for the memory_search tool (recall-first).

        Args:
            q: the request and its vector.
            scope: "all" (journal, notes, facts, unverified entries, turns), "notes", "journal", "conversations" or
                "history" (archived entries).
            span: the time asked about (a hard span filters; a soft one adds a list).
            k: how many units at most (``retrieval.search_results``).
            include_core: also search USER.md and MEMORY.md (callers without the snapshot, such as other apps).

        Returns:
            Whole units, best first, at most two per file, each cut to ``retrieval.result_chars``.
        """
        kinds = set(SCOPES.get(scope, SCOPES["all"]))
        if include_core and scope == "all":
            kinds |= CORE_KINDS
        lists, _, _ = self._lists(q, kinds, span, conv=None, trusted_only=False)
        fused = rrf(list(lists.values()))
        rows = self._fetch(fused)
        scores = self._scores(fused, rows)
        hits = sorted((self._hit(rows[i], score) for i, score in scores.items()), key=lambda h: h.score,
                      reverse=True)
        per_path: dict[str, int] = {}
        limit = self.retrieval.result_chars
        out: list[Hit] = []
        for hit in hits:
            if len(out) >= k:
                break
            if per_path.get(hit.path, 0) >= SEARCH_PER_FILE:
                continue
            per_path[hit.path] = per_path.get(hit.path, 0) + 1
            text = clip(hit.text, limit, flat=False)
            out.append(Hit(hit.id, hit.key, hit.kind, hit.path, hit.ts, text, hit.source, hit.score))
        self._touch(out)
        return out

    def recall(self, q: Query, *, conv: int, exclude: Collection[int], now: datetime | None = None) -> RecallResult:
        """Find the 0-3 memories worth putting before a reply (precision-first; see ``gate``).

        Args:
            q: the request and its vector.
            conv: the current conversation (its own turns are never recalled: they are in the history).
            exclude: units already recalled in this context epoch.
            now: the current local time (for time phrases).

        Returns:
            The memories recalled.
        """
        started = time.monotonic()
        now = now or datetime.now(local_zone())
        span = parse_span(q.text, now)
        mentioned = self._mentioned(q.text)
        if len(content_terms(q.text)) < RECALL_MIN_TERMS and span is None and not mentioned:
            return RecallResult((), (time.monotonic() - started) * 1000)
        found, rows = self._candidates(q, conv=conv, span=span, exclude=exclude, mentioned=mentioned)
        r = self.retrieval
        passing = {c.id for c in found if gate(c, span_hard=span is not None and span.hard,
                                                window=r.recall_rank_window, min_cosine=r.recall_min_cosine,
                                                solo_cosine=r.recall_solo_cosine)}
        fused = {i: score for i, score in rrf(self._ranked_lists(found)).items() if i in passing}
        scores = self._scores(fused, {i: rows[i] for i in fused})
        hits = sorted((self._hit(rows[i], score) for i, score in scores.items()), key=lambda h: h.score,
                      reverse=True)
        items = tuple(pack(hits, budget=r.recall_chars, max_items=r.recall_max_items))
        self._touch(items)
        return RecallResult(items, (time.monotonic() - started) * 1000)

    def candidates(self, q: Query, *, conv: int, span: Span | None) -> list[Candidate]:
        """Every recall candidate with the features the gates weigh (``orion memory explain`` and calibration).

        Args:
            q: the request and its vector.
            conv: the current conversation (its turns are left out).
            span: the time asked about.

        Returns:
            The candidates, unordered.
        """
        return self._candidates(q, conv=conv, span=span, exclude=(), mentioned=self._mentioned(q.text))[0]

    def texts(self, ids: Sequence[int]) -> dict[int, str]:
        """The plain text of these units (``orion memory explain`` and calibration show what the gates weighed).

        Args:
            ids: unit row ids.

        Returns:
            Row id -> text, for the units that exist.
        """
        return {i: str(row["text"]) for i, row in self._fetch(ids).items()}

    def vectors(self, keys: Collection[str]) -> dict[str, "NDArray[np.float32]"]:
        """The stored vectors of these units (for the writer's duplicate check), when made by today's model.

        Args:
            keys: unit keys.

        Returns:
            key -> float32 vector, for the keys that have one.
        """
        model = self.embedder.id
        if not keys or not model or self.store.get_setting("embedder") != model:
            return {}
        with self.store.reading() as db:
            return {str(row[0]): np.frombuffer(row[1], dtype=np.float16).astype(np.float32) for row in db.execute(
                "SELECT key, vec FROM mem_items WHERE vec IS NOT NULL AND key IN (SELECT value FROM json_each(?))",
                (json.dumps(list(keys)),))}

    def vocabulary(self, n: int, now: float | None = None) -> list[str]:
        """The trusted concepts most worth priming the speech recogniser with (untrusted names never reach it).

        Args:
            n: how many (``stt.vocabulary_max``).
            now: epoch seconds.

        Returns:
            Concept names, best first.
        """
        if n <= 0:
            return []
        with self.store.reading() as db:
            rows = db.execute("SELECT ic.concept, COUNT(*), MAX(i.ts) FROM item_concepts ic JOIN mem_items i ON "
                              f"i.id = ic.item_id WHERE i.kind != 'archive' AND {_TRUSTED_ONLY} "
                              "GROUP BY ic.concept COLLATE NOCASE").fetchall()
        stop = self.vault.not_concepts()
        usable = [(str(r[0]), int(r[1]), float(r[2])) for r in rows if is_concept(str(r[0]), stop)]
        ranked = vocabulary_scores(usable, now or time.time(), self.retrieval.recency_half_life_days)
        return [name for name, _ in ranked[:n]]

    def load(self) -> None:
        """Load the vectors into memory now (the owner's warm-up), so the first recall doesn't wait for them."""
        self._ensure_vectors()

    # ------------------------------------------------------------------------------------------------ internals
    def _candidates(self, q: Query, *, conv: int, span: Span | None, exclude: Collection[int],
                    mentioned: Sequence[str]) -> tuple[list[Candidate], dict[int, "sqlite3.Row"]]:
        """The recall candidates (see ``candidates``) and their rows, which ``recall`` ranks without fetching again."""
        lists, cosines, concept_ids = self._lists(q, set(RECALLABLE), span, conv=conv, trusted_only=True,
                                                  mentioned=mentioned)
        bm25 = {item: rank for rank, item in enumerate(lists.get("bm25", []))}
        meaning = {item: rank for rank, item in enumerate(lists.get("cos", []))}
        rows = self._fetch([i for i in dict.fromkeys(i for ranked in lists.values() for i in ranked)
                            if i not in exclude])
        hard = span if span is not None and span.hard else None
        found = [Candidate(id=i, kind=str(row["kind"]), ts=float(row["ts"]), source=str(row["source"]),
                           bm25_rank=bm25.get(i), cos=cosines.get(i), cos_rank=meaning.get(i), concept=i in concept_ids,
                           in_span=hard is not None and hard.start <= float(row["ts"]) < hard.end)
                 for i, row in rows.items()]
        return found, rows

    def _lists(self, q: Query, kinds: set[str], span: Span | None, *, conv: int | None, trusted_only: bool,
               mentioned: Sequence[str] | None = None) -> tuple[dict[str, list[int]], dict[int, float], set[int]]:
        """The ranked candidate lists (keyword, meaning, concept, time) for one request.

        Returns:
            (list name -> ids best first, id -> cosine, ids linked to a named concept).
        """
        limit = self.retrieval.search_candidates
        hard = span if span is not None and span.hard else None
        where, args = _filter(kinds, hard, conv, trusted_only)
        lists: dict[str, list[int]] = {}
        query = fts_query(q.text)
        with self.store.reading() as db:
            if query:
                lists["bm25"] = [int(r[0]) for r in db.execute(
                    f"SELECT i.id FROM mem_fts JOIN mem_items i ON i.id = mem_fts.rowid WHERE mem_fts MATCH ? "
                    f"AND {where} ORDER BY bm25(mem_fts, 1.0, 0.4) LIMIT ?", (query, *args, limit))]
            names = self._mentioned(q.text) if mentioned is None else list(mentioned)
            concept_ids: set[int] = set()
            if names:
                ranked = [int(r[0]) for r in db.execute(
                    f"SELECT DISTINCT i.id FROM item_concepts ic JOIN mem_items i ON i.id = ic.item_id WHERE "
                    f"ic.concept IN (SELECT value FROM json_each(?)) AND {where} ORDER BY i.ts DESC LIMIT ?",
                    (json.dumps(names), *args, limit))]
                lists["concept"] = ranked
                concept_ids = set(ranked)
            if span is not None:
                span_kinds = [kind for kind in SPAN_KINDS if kind in kinds]
                if span_kinds:
                    span_where, span_args = _filter(set(span_kinds), span, conv, trusted_only)
                    lists["span"] = [int(r[0]) for r in db.execute(
                        f"SELECT i.id FROM mem_items i WHERE {span_where} ORDER BY (i.kind = 'journal') DESC, "
                        f"i.ts DESC LIMIT ?", (*span_args, limit))]
        cosines: dict[int, float] = {}
        if q.vec is not None:
            self._ensure_vectors()
            near = self._vectors.nearest(q.vec, _mask(kinds, hard, conv, trusted_only), limit)
            floor = self.retrieval.search_min_similarity
            cosines = {i: c for i, c in near}
            lists["cos"] = [i for i, c in near if c >= floor]
        return lists, cosines, concept_ids

    @staticmethod
    def _ranked_lists(found: Sequence[Candidate]) -> list[list[int]]:
        """The candidates' rank lists again (keyword, meaning), for fusing the ones that passed a gate."""
        bm25 = sorted((c for c in found if c.bm25_rank is not None), key=lambda c: c.bm25_rank or 0)
        meaning = sorted((c for c in found if c.cos_rank is not None), key=lambda c: c.cos_rank or 0)
        rest = [c for c in found if c.bm25_rank is None and c.cos_rank is None]
        return [[c.id for c in bm25], [c.id for c in meaning], [c.id for c in sorted(rest, key=lambda c: -c.ts)]]

    def _scores(self, fused: Mapping[int, float], rows: Mapping[int, "sqlite3.Row"]) -> dict[int, float]:
        """Fused scores of the fetched units, with the recency boost of episodic ones and the usage boost applied."""
        ids = [i for i in fused if i in rows]
        if not ids:
            return {}
        uses = self._use_counts(rows)
        r = self.retrieval
        now = time.time()
        out: dict[int, float] = {}
        for i in ids:
            row = rows[i]
            score = fused[i]
            if row["kind"] in EPISODIC:
                score *= 1 + recency_prior((now - float(row["ts"])) / DAY_S, r.recency_half_life_days,
                                           r.recency_weight)
            out[i] = score * (1 + r.usage_weight * math.log1p(uses.get(i, 0)))
        return out

    def _fetch(self, ids: Collection[int]) -> dict[int, "sqlite3.Row"]:
        """Rows of these units."""
        with self.store.reading() as db:
            return {int(row["id"]): row for row in db.execute(
                "SELECT id, key, kind, path, ts, text, source FROM mem_items WHERE id IN (SELECT value FROM json_each(?))",
                (json.dumps(list(ids)),))}

    def _use_counts(self, rows: Mapping[int, "sqlite3.Row"]) -> dict[int, int]:
        """How often each entry was used (from its metadata; other kinds count 0)."""
        keys = {str(row["key"]): i for i, row in rows.items() if row["kind"] in ENTRY_KINDS}
        if not keys:
            return {}
        return {keys[key]: meta.use_count for key, meta in self.store.meta_rows(list(keys)).items()}

    @staticmethod
    def _hit(row: "sqlite3.Row", score: float) -> Hit:
        """A fetched row as a Hit."""
        return Hit(int(row["id"]), str(row["key"]), str(row["kind"]), str(row["path"]), float(row["ts"]),
                   str(row["text"]), str(row["source"]), score)

    def _touch(self, hits: Iterable[Hit]) -> None:
        """Count a use of the entries among these hits (queued on the database's writer; never waits)."""
        if keys := [hit.key for hit in hits if hit.kind in ENTRY_KINDS]:
            self.store.submit(self.store.meta_touch, keys, time.time())

    def _mentioned(self, text: str) -> list[str]:
        """The trusted concepts a text names (one pass of the concepts' pattern)."""
        concepts = self._concept_set()
        if concepts.regex is None:
            return []
        found = (concepts.canonical.get(m.group(0).casefold()) for m in concepts.regex.finditer(text))
        return list(dict.fromkeys(name for name in found if name))

    def _concept_set(self) -> ConceptSet:
        """The trusted concepts as a ConceptSet, read again only when the index version changed."""
        version = self.version
        cached_version, cached = self._concepts
        if cached is not None and cached_version == version:
            return cached
        with self.store.reading() as db:
            rows = db.execute("SELECT DISTINCT ic.concept FROM item_concepts ic JOIN mem_items i ON i.id = ic.item_id "
                              f"WHERE {_TRUSTED_ONLY} AND i.kind != 'archive'").fetchall()
        stop = self.vault.not_concepts()
        concepts = concept_set(tuple(str(r[0]) for r in rows if is_concept(str(r[0]), stop)))
        self._concepts = (version, concepts)
        return concepts

    def _ensure_vectors(self) -> None:
        """Load the vectors into RAM when another process changed the index since they were loaded."""
        version = self.version
        if version != self._loaded_version:
            self._reload(version)

    def _reload(self, version: int) -> None:
        """Load every unit's vector (turns older than ``retrieval.turn_vector_days`` stay out of RAM)."""
        with self._load_lock:
            days = self.retrieval.turn_vector_days
            oldest = time.time() - days * DAY_S if days else 0.0
            with self.store.reading() as db:
                rows = db.execute("SELECT id, vec, kind, ts, conv, source FROM mem_items WHERE vec IS NOT NULL AND "
                                  "(kind NOT IN ('turn', 'command') OR ts >= ?)", (oldest,)).fetchall()
            self._vectors.load([(int(r[0]), bytes(r[1]), str(r[2]), float(r[3]), r[4], str(r[5])) for r in rows])
            self._loaded_version = version

    def _after_write(self, added: Sequence[VecRow], removed: Collection[int], version: int) -> None:
        """Bring the vectors in RAM up to date after this process's own sync (no full reload)."""
        if self._loaded_version < 0:
            return  # nothing loaded yet: the first search loads everything
        if version != self._loaded_version + 1:
            self._reload(version)  # someone else synced too
            return
        self._vectors.remove(removed)
        self._vectors.upsert([(i, blob, kind, ts, conv, source) for i, blob, kind, ts, conv, source in added if blob])
        self._loaded_version = version

    def _file_meta(self, rels: Sequence[str]) -> dict[str, Any]:
        """The metadata the changed files' units need: entry rows by key, note sources by file."""
        keys = [f"file:{rel}" for rel in rels if rel.startswith("notes/")]
        for rel in rels:
            if "/" not in rel and rel != "archive.md":
                keys += [unit.key for unit in self.vault.units(self.vault.root / rel)]
        return dict(self.store.meta_rows(keys)) if keys else {}

    def _rows_of(self, units: Sequence[Unit], meta: Mapping[str, Any]) -> list[_Row]:
        """The index rows of one file's units (long ones split, dated, sourced)."""
        zone = local_zone()
        limit = self.retrieval.unit_max_chars
        rows: list[_Row] = []
        for unit in units:
            ts, source = unit.ts, "hand"
            if unit.kind in ENTRY_KINDS:
                if (row := meta.get(unit.key)) is not None:
                    ts, source = row.learned_at, row.source
            elif unit.kind == "journal":
                source = "review"
            elif unit.kind == "note" and (row := meta.get(f"file:{unit.path}")) is not None:
                source = row.source
            parts = [unit.plain] if unit.kind in ENTRY_KINDS else _split(unit.plain, limit)
            keys = f"{_time_keys(ts, zone)} {' '.join(unit.concepts)}".strip()
            for n, part in enumerate(parts):
                suffix = f":{n}" if n else ""
                rows.append(_Row(unit.key + suffix, unit.kind, unit.path, unit.anchor + suffix, part, keys, ts, None,
                                 source, unit.concepts))
        return rows

    def _turn_rows(self, turn: str, messages: Sequence["sqlite3.Row"], tainted: bool,
                   concepts: ConceptSet) -> list[_Row]:
        """The index rows of one conversation turn: the user's words and the reply."""
        said = " ".join(str(m["content"]) for m in messages if m["role"] == "user").strip()
        reply = " ".join(str(m["content"]) for m in messages if m["role"] == "assistant").strip()
        if not said and not reply:
            return []
        lines = (f"User: {said}" if said else "", f"Orion: {reply}" if reply else "")
        text = "\n".join(line for line in lines if line)
        first = messages[0]
        ts, conv = float(first["ts"]), int(first["conv"])
        kind = "command" if len(reply) < TURN_MIN_REPLY_CHARS else "turn"
        names = tuple(dict.fromkeys(links_in(link_text(text, concepts))))
        keys = f"{_time_keys(ts, local_zone())} {' '.join(names)}".strip()
        source = "tainted" if tainted else "brain"
        parts = _split(text, self.retrieval.unit_max_chars)
        return [_Row(f"turn:{turn}" + (f":{n}" if n else ""), kind, f"conv:{conv}", turn + (f":{n}" if n else ""),
                     part, keys, ts, conv, source, names) for n, part in enumerate(parts)]

    def _embed(self, rows: Sequence[_Row]) -> None:
        """Give the rows their vectors as stored, in one batch (none without a model); never inside a write, so the
        model doesn't hold the database."""
        if rows and (vectors := self.embedder.encode([row.text for row in rows])) is not None:
            for row, vector in zip(rows, vectors, strict=True):
                row.vec = _blob(vector)

    def _apply_files(self, db: "sqlite3.Connection", diffs: Sequence[_FileDiff]) -> tuple[list[VecRow], set[int], int]:
        """In one transaction: apply each changed file's diff, reconcile entry metadata, record the files' stats and
        bump the version."""
        added: list[VecRow] = []
        removed: set[int] = set()
        now = time.time()
        for rel, stamp, new, gone, fresh in diffs:
            removed |= {int(r["id"]) for r in gone}
            db.executemany("DELETE FROM mem_items WHERE id = ?", [(int(r["id"]),) for r in gone])
            added += [_put(db, row) for row in fresh]
            self._reconcile_meta(db, rel, new, gone, now)
            if stamp is None:
                db.execute("DELETE FROM index_files WHERE path = ?", (rel,))
            else:
                db.execute("INSERT OR REPLACE INTO index_files (path, mtime_ns, size) VALUES (?, ?, ?)", (rel, *stamp))
        return added, removed, _bump(db)

    @staticmethod
    def _reconcile_meta(db: "sqlite3.Connection", rel: str, new: Mapping[str, _Row], gone: Sequence["sqlite3.Row"],
                        now: float) -> None:
        """Entries found without metadata were written by hand; entries gone from their file were edited away."""
        entries = {key: row for key, row in new.items() if row.kind in ENTRY_KINDS and ":" not in key}
        if entries:
            known = {str(r[0]) for r in db.execute("SELECT key FROM mem_meta WHERE key IN (SELECT value FROM "
                                                   "json_each(?))", (json.dumps(list(entries)),))}
            db.executemany("INSERT OR IGNORE INTO mem_meta (key, kind, source, learned_at, text) VALUES "
                           "(?, ?, 'hand', ?, ?)", [(key, row.kind, now, row.text) for key, row in entries.items()
                                                    if key not in known])
        vanished = [str(r["key"]) for r in gone if r["kind"] in ENTRY_KINDS]
        if vanished:
            db.executemany("UPDATE mem_meta SET superseded_at = ?, why = 'edited by hand' WHERE key = ? AND "
                           "superseded_at IS NULL AND kind = ?",
                           [(now, key, KIND_OF_FILE[rel]) for key in vanished])

    def _apply_turns(self, db: "sqlite3.Connection", turns: Collection[str], rows: Sequence[_Row],
                     newest: int) -> tuple[list[VecRow], set[int], int]:
        """In one transaction: replace the units of these turns and record how far the log is indexed."""
        removed = {int(r[0]) for turn in turns for r in db.execute(  # the turn's unit and its parts, by the key index
            "DELETE FROM mem_items WHERE key = ? OR (key > ? AND key < ?) RETURNING id",
            (f"turn:{turn}", f"turn:{turn}:", f"turn:{turn};"))}
        added = [_put(db, row) for row in rows]
        db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('indexed_message_id', ?)", (str(newest),))
        return added, removed, _bump(db)


# ---------------------------------------------------------------------------------------------------- helpers
def _bump(db: "sqlite3.Connection") -> int:
    """Increase the index version inside the running transaction and return it."""
    row = db.execute("SELECT value FROM settings WHERE key = 'index_version'").fetchone()
    version = int(row[0] or 0) + 1 if row is not None else 1
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('index_version', ?)", (str(version),))
    return version


def _put(db: "sqlite3.Connection", row: _Row) -> VecRow:
    """Insert or update one unit row and its concepts; return what the vectors in RAM need of it."""
    item = int(db.execute(_UPSERT, (row.key, row.kind, row.path, row.anchor, row.text, row.keys, row.ts, row.conv,
                                    row.source, row.vec)).fetchone()[0])
    db.execute("DELETE FROM item_concepts WHERE item_id = ?", (item,))
    db.executemany("INSERT OR IGNORE INTO item_concepts (concept, item_id) VALUES (?, ?)",
                   [(name, item) for name in row.concepts])
    return item, row.vec, row.kind, row.ts, row.conv, row.source


def _group_turns(rows: Sequence["sqlite3.Row"]) -> dict[str, list["sqlite3.Row"]]:
    """Messages grouped by turn, in log order."""
    out: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        out.setdefault(str(row["turn"]), []).append(row)
    return out


def _filter(kinds: Collection[str], span: Span | None, conv: int | None,
            trusted_only: bool) -> tuple[str, list[object]]:
    """The SQL filter (on alias i) for kinds, a time span, the current conversation and trust."""
    clauses = ["i.kind IN (SELECT value FROM json_each(?))"]
    args: list[object] = [json.dumps(sorted(kinds))]
    if span is not None:
        clauses.append("i.ts >= ? AND i.ts < ?")
        args += [span.start, span.end]
    if conv is not None:
        clauses.append("(i.conv IS NULL OR i.conv != ?)")
        args.append(conv)
    if trusted_only:
        clauses.append(_TRUSTED_ONLY)
    return " AND ".join(clauses), args


def _mask(kinds: Collection[str], span: Span | None, conv: int | None,
          trusted_only: bool) -> Callable[[_Vectors], "NDArray[np.bool_]"]:
    """The same filter as ``_filter``, for the rows of the vector matrix."""
    codes = np.array(sorted(_KIND_CODES[k] for k in kinds if k in _KIND_CODES), dtype=np.uint8)

    def allow(v: _Vectors) -> "NDArray[np.bool_]":
        """Admissible rows."""
        n = v.size
        mask = np.isin(v.kinds[:n], codes)
        if span is not None:
            mask &= (v.ts[:n] >= span.start) & (v.ts[:n] < span.end)
        if conv is not None:
            mask &= v.conv[:n] != conv
        if trusted_only:
            mask &= v.trusted[:n]
        return mask

    return allow
