"""The Markdown memory: plain files a person can read and edit, parsed into dated units, and the graph drawn from them.

    data/memory/
      USER.md        what he knows about the user            core: always in his context, capped
      MEMORY.md      his own notes and lessons                core: always in his context, capped
      facts.md       trusted entries that no longer fit the core files      (created on first use)
      unverified.md  what web pages or other apps claimed     never recalled, never promoted (created on first use)
      journal/       one file per day of "## HH:MM" entries written by the memory review
      notes/         notes the user asked him to keep
      archive.md     entries removed or replaced, one line each, so nothing is lost silently

Everything stays plain Markdown; dates, sources and usage live in SQLite (``store.py``). This module:
- parses every file into ``Unit`` s (one entry, one journal entry, one note section, one archive line) behind a
  per-file cache keyed by the file's stamp, so the index, the snapshot, the writer and the graph share one parse;
- rewrites the entry files (the hand-written head before the first bullet survives) and appends to the journal,
  the notes and the archive, under a lock shared with the other process (``locked``);
- links concepts written as ``[[wiki links]]``: one compiled, longest-first alternation per concept set, so linking
  a text is a single left-to-right pass;
- draws the neural map with ids that stay the same when other entries change, and serves one node's text on demand.

The pure parts (``normalise``, ``entry_key``, ``strip_links``, ``links_in``, ``is_concept``, ``concept_set``,
``link_text``, ``short_ids``, ``slugify``, ``parse_units``, ``parse_archive``) are what the tests check. Times are
local: journal headings, note dates and archive lines are read with ``local_ts``, which applies the daylight-saving
offset of that date.
"""

import contextlib
import hashlib
import itertools
import os
import re
import threading
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from functools import lru_cache
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal

from filelock import FileLock, Timeout

from orion.base import FRONT_MATTER, JSON, OrionError, clip, file_stamp, local_zone, write_atomic

if TYPE_CHECKING:
    from orion.config import RetrievalConfig

type EntryKind = Literal["user", "orion", "facts", "unverified"]  # the files of one entry per line
type UnitKind = Literal["user", "orion", "facts", "unverified", "journal", "note", "archive"]

FILES: Final[Mapping[EntryKind, str]] = MappingProxyType({
    "user": "USER.md", "orion": "MEMORY.md", "facts": "facts.md", "unverified": "unverified.md"})
KIND_OF_FILE: Final[Mapping[str, EntryKind]] = MappingProxyType({name: kind for kind, name in FILES.items()})
CORE: Final[tuple[EntryKind, ...]] = ("user", "orion")  # always in his context, capped, always present
ENTRY_NODES: Final = frozenset({"fact", "insight", "unverified"})  # the neural map's nodes that are entries
HEADERS: Final[Mapping[EntryKind, str]] = MappingProxyType({
    "user": "# About the user\n\nWhat Orion knows about you. One fact per line: edit or delete freely.\n\n",
    "orion": "# Orion's notes\n\nWhat Orion has learned about this PC, how you like things done, and "
                  "lessons from past tasks. One entry per line.\n\n",
    "facts": "# More facts\n\nTrusted entries that no longer fit in USER.md or MEMORY.md. Orion looks them up "
             "when they matter. One per line: edit or delete freely.\n\n",
    "unverified": "# Unverified\n\nWhat web pages or other apps claimed. Orion can search it but never treats it "
                  "as known about you. One per line: delete freely.\n\n",
})
ARCHIVE: Final = "archive.md"
CONCEPT_MAX_CHARS: Final = 48  # the longest [[link]] target that counts as a concept
LOCK_TIMEOUT_S: Final = 2.0  # how long a write waits for the other process's memory lock before giving up
LABEL_CHARS: Final = 46  # the longest node label on the neural map; longer entries are cut
_COUNTED: Final = ("fact", "insight", "unverified", "concept", "day", "note", "skill")  # kinds the map counts
SEP: Final = " · "  # the middle dot that separates the fields of an archive line and the journal's topics

_LINK: Final = re.compile(r"\[\[([^\[\]\n]+)\]\]")  # [[target]], [[target|shown words]], [[target#part]]
_CREATED: Final = re.compile(r"^created:\s*(\d{4}-\d\d-\d\d)(?:[ T](\d{1,2}):(\d\d))?", re.MULTILINE)
_CLOCK: Final = re.compile(r"(\d{1,2}):(\d\d)")
_TOPICS: Final = re.compile(r"^Topics:\s*(.*)$", re.IGNORECASE)
_ARCHIVE_LINE: Final = re.compile(r"^- (\d{4}-\d\d-\d\d) (\d\d):(\d\d) · ([^·]+) · ([^:]+): (.*)$")
_SPACES: Final = re.compile(r"\s+")
_ENTRY_ID: Final = re.compile(r"[0-9a-f]{10}")


class VaultBusy(OrionError):
    """The memory files stayed locked by another writer for longer than the timeout."""


@dataclass(frozen=True, slots=True)
class Unit:
    """One piece of memory as the files hold it: an entry, a journal entry, a note section or an archive line.

    Attributes:
        key: for entries, ``entry_key`` of the text (the same fact has the same key in every file and process);
            for the rest, a digest of the path and the anchor, so editing the words keeps the key.
        kind: "user", "orion", "facts", "unverified", "journal", "note" or "archive".
        path: the file, relative to the memory folder, with forward slashes ("journal/2026-10-02.md").
        anchor: where it sits in the file: an entry's ``key[:12]``; a journal entry's "HH:MM" ("HH:MM#2" for a
            second one at that minute); a note section's heading slug ("" for the text before any section); an
            archive line's number.
        text: the text as written, with its [[links]] (a note section starts with the note's title).
        plain: the text with the links shown as plain words.
        ts: when it was written, in epoch seconds: a journal entry's day and time, a note's ``created`` date, an
            archive line's date; for entries (and when nothing better is written) the file's modification time.
        concepts: the concepts it links (a journal entry's "Topics:" line included), first spelling kept.
    """

    key: str
    kind: UnitKind
    path: str
    anchor: str
    text: str
    plain: str
    ts: float
    concepts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ConceptSet:
    """The known concepts, ready to link: their names and one compiled pattern that finds any of them.

    Attributes:
        names: the concepts, each in the spelling first seen.
        regex: one case-insensitive alternation, longest name first, that matches a whole-word mention outside a
            link; None when there are no concepts.
        canonical: each name case-folded -> its spelling in ``names``.
    """

    names: tuple[str, ...]
    regex: re.Pattern[str] | None
    canonical: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class Archived:
    """One line of archive.md: an entry that was removed or replaced.

    Attributes:
        line: its line number in the file.
        ts: when it was archived, in epoch seconds (the file's time when the line has no date).
        kind: the entry file it left, None when the line doesn't say.
        why: the reason written on the line ("removed", "replaced", "updated", "demoted"; "" when unknown).
        text: the archived entry.
    """

    line: int
    ts: float
    kind: EntryKind | None
    why: str
    text: str


# ---------------------------------------------------------------------------------------------------- pure parts
def strip_links(text: str) -> str:
    """Show links as plain words: [[Raspberry Pi]] -> Raspberry Pi, [[Raspberry Pi|my Pi]] -> my Pi.

    Args:
        text: Markdown that may contain [[links]].

    Returns:
        The text with each link replaced by the words it shows ([[A#part]] shows A).
    """
    return _LINK.sub(_shown, text) if "[[" in text else text


def _shown(match: re.Match[str]) -> str:
    """The words a link shows: its alias after "|", else its target without a "#part"."""
    inner = match.group(1)
    return inner.split("|", 1)[1] if "|" in inner else inner.split("#", 1)[0]


def links_in(text: str) -> list[str]:
    """The targets of the links in a text, in order: "[[A|alias]] [[B#part]]" gives ["A", "B"].

    Args:
        text: Markdown that may contain [[links]].

    Returns:
        Each link's target, stripped; links with an empty target are left out.
    """
    if "[[" not in text:
        return []
    targets = (m.group(1).split("|", 1)[0].split("#", 1)[0].strip() for m in _LINK.finditer(text))
    return [target for target in targets if target]


def normalise(text: str) -> str:
    """The form two entries share when they say the same thing.

    Args:
        text: an entry, with or without links.

    Returns:
        The text with links shown as words, case-folded, runs of white space made one space, and trailing full
        stops, exclamation marks and spaces dropped.
    """
    return _SPACES.sub(" ", strip_links(text)).casefold().strip().rstrip(" .!")


def entry_key(text: str) -> str:
    """The content key of an entry: the same fact gets the same key, however it is linked or capitalised.

    Args:
        text: the entry.

    Returns:
        The SHA-1 of ``normalise(text)``, 40 hex characters. Entry ids shown to the model are its shortest unique
        prefix of at least 4 characters.
    """
    return hashlib.sha1(normalise(text).encode(), usedforsecurity=False).hexdigest()


def is_concept(name: str, stop: Collection[str] = (), max_chars: int = CONCEPT_MAX_CHARS) -> bool:
    """Whether a link target is worth a concept (a node on the map, a word to link elsewhere).

    Args:
        name: the target.
        stop: names never made concepts, already case-folded (``retrieval.not_concepts``).
        max_chars: the longest name allowed.

    Returns:
        True for 2 to ``max_chars`` characters that are not only digits, hold none of ``[]|#`` or a line break,
        and are not in ``stop`` (compared case-folded).
    """
    name = name.strip()
    return (2 <= len(name) <= max_chars and not name.isdigit() and not any(c in name for c in "[]|#\n")
            and name.casefold() not in stop)


@lru_cache(maxsize=16)
def concept_set(names: tuple[str, ...]) -> ConceptSet:
    """Prepare concepts for linking.

    Args:
        names: the concepts; repeats (compared case-folded) keep their first spelling.

    Returns:
        The set, with the pattern compiled once (cached for the 16 most recent sets).
    """
    first: dict[str, str] = {}
    for name in names:
        if stripped := name.strip():
            first.setdefault(stripped.casefold(), stripped)
    if not first:
        return ConceptSet((), None, {})
    alternation = "|".join(re.escape(name) for name in sorted(first.values(), key=len, reverse=True))
    regex = re.compile(rf"(?<![\w\[])(?:{alternation})(?![\w\]])", re.IGNORECASE)
    return ConceptSet(tuple(first.values()), regex, MappingProxyType(first))


def link_text(text: str, concepts: ConceptSet) -> str:
    """Link the first mention of each known concept, leaving the wording as it is.

    One left-to-right pass of the concepts' pattern over the text outside existing links. The longest name is
    tried first, so "Pi" is never linked inside "Raspberry Pi". A mention in another case keeps its words as the
    link's alias ([[Raspberry Pi|raspberry pi]]). Concepts the text already links are left alone, so linking is
    idempotent and ``strip_links`` gives the original text back.

    Args:
        text: Markdown.
        concepts: the concepts to link.

    Returns:
        The linked text.
    """
    regex = concepts.regex
    if regex is None or not text:
        return text
    canonical = concepts.canonical
    done = {target.casefold() for target in links_in(text)}

    def link(match: re.Match[str]) -> str:
        """The link for one mention, or the mention itself when its concept is already linked."""
        word = match.group(0)
        name = canonical.get(word.casefold())
        if name is None or name.casefold() in done:
            return word
        done.add(name.casefold())
        return f"[[{name}]]" if word == name else f"[[{name}|{word}]]"

    out: list[str] = []
    pos = 0
    for existing in _LINK.finditer(text):
        out += (regex.sub(link, text[pos:existing.start()]), existing.group(0))
        pos = existing.end()
    out.append(regex.sub(link, text[pos:]))
    return "".join(out)


def short_ids(keys: Sequence[str], min_len: int = 4) -> list[str]:
    """The ids shown to the model for entry keys: each key's shortest prefix of at least ``min_len`` characters that
    no other key shares ("3fa2b1", "3fa2c9", "9c01aa" -> "3fa2b", "3fa2c", "9c01").

    Args:
        keys: entry keys (hex digests); repeats get the same id.

    Returns:
        One id per key, in order.
    """
    unique = sorted(set(keys))
    lengths: dict[str, int] = {}
    for before, after in itertools.pairwise(unique):
        shared = next((i for i, (a, b) in enumerate(zip(before, after, strict=False)) if a != b),
                      min(len(before), len(after)))
        for key in (before, after):
            lengths[key] = max(lengths.get(key, min_len), shared + 1)
    return [key[:max(min_len, lengths.get(key, min_len))] for key in keys]


def slugify(text: str, max_len: int = 60) -> str:
    """A safe file-name or anchor slug: "../../etc/passwd" -> "etcpasswd", "" -> "note".

    Args:
        text: a title or heading.
        max_len: the longest slug.

    Returns:
        Lower-case word characters joined by single hyphens, at most ``max_len`` characters, "note" when nothing
        is left.
    """
    slug = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    return re.sub(r"[\s_]+", "-", slug)[:max_len].strip("-") or "note"


def parse_units(rel: str, text: str, mtime: float) -> tuple[Unit, ...]:
    """Parse one memory file into its units.

    - Entry files (USER.md, MEMORY.md, facts.md, unverified.md): every line starting with "- " or "* " after the
      head; a repeated entry counts once.
    - The journal: every "## HH:MM" section, dated by the file's name and that time; a "Topics:" line adds its links
      to the concepts and leaves the text; another heading starts a section dated at noon; a hand-written file
      without headings is one unit at noon.
    - Notes: the ``created:`` date of the front matter (else the file's time); one unit per "## " section, plus the
      text before the first section; each starts with the note's "# " title.
    - archive.md: one unit per "- YYYY-MM-DD HH:MM · FILE · why: text" line; a line that doesn't parse keeps its
      words and the file's time.

    Args:
        rel: the file's path relative to the memory folder, with forward slashes.
        text: its contents.
        mtime: its modification time, in epoch seconds.

    Returns:
        The units, in file order.
    """
    if (entry := KIND_OF_FILE.get(rel)) is not None:
        return _entry_units(rel, entry, text, mtime)
    if rel == ARCHIVE:
        return _archive_units(rel, text, mtime)
    if rel.startswith("journal/"):
        return _journal_units(rel, text, mtime)
    return _note_units(rel, text, mtime)


def _digest(text: str) -> str:
    """The SHA-1 of a text, 40 hex characters (the key of a unit that is not an entry)."""
    return hashlib.sha1(text.encode(), usedforsecurity=False).hexdigest()


def _unique(names: Iterable[str]) -> tuple[str, ...]:
    """Concept names without repeats (compared case-folded), first spelling kept, structurally valid only."""
    first: dict[str, str] = {}
    for name in names:
        stripped = name.strip()
        if is_concept(stripped):
            first.setdefault(stripped.casefold(), stripped)
    return tuple(first.values())


def _unit(kind: UnitKind, rel: str, anchor: str, text: str, ts: float, extra: Sequence[str] = ()) -> Unit:
    """A unit that is not an entry: keyed by its path and anchor, so its key survives edits of its words."""
    return Unit(_digest(f"{rel}\0{anchor}"), kind, rel, anchor, text, strip_links(text), ts,
                _unique([*links_in(text), *extra]))


def local_ts(day: date, zone: tzinfo, hour: int = 0, minute: int = 0) -> float:
    """Epoch seconds of a wall-clock time on a day in a zone (the daylight-saving offset of that moment applies; an
    hour of 24 or more runs into the next days)."""
    return (datetime(day.year, day.month, day.day, tzinfo=zone) + timedelta(hours=hour, minutes=minute)).timestamp()


def _iso_date(text: str) -> date | None:
    """A "YYYY-MM-DD" date, or None when the text is not one."""
    try:
        return date.fromisoformat(text) if len(text) == 10 else None
    except ValueError:
        return None


def _clock(text: str) -> tuple[int, int] | None:
    """(hour, minute) of an "H:MM" or "HH:MM" text, None when it is not a valid time."""
    match = _CLOCK.fullmatch(text)
    if match is None:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    return (hour, minute) if hour < 24 and minute < 60 else None


def _anchors(bases: Iterable[str]) -> Iterator[str]:
    """Make repeated anchors unique: "09:12", "09:12#2", ... in order."""
    seen: dict[str, int] = {}
    for base in bases:
        seen[base] = n = seen.get(base, 0) + 1
        yield base if n == 1 else f"{base}#{n}"


def _entry_units(rel: str, kind: EntryKind, text: str, mtime: float) -> tuple[Unit, ...]:
    """The bullet entries of an entry file, each keyed by its content; repeats count once."""
    units: list[Unit] = []
    seen: set[str] = set()
    for line in text.splitlines():
        if not line.startswith(("- ", "* ")) or not (body := line[2:].strip()):
            continue
        key = entry_key(body)
        if key not in seen:
            seen.add(key)
            units.append(Unit(key, kind, rel, key[:12], body, strip_links(body), mtime, _unique(links_in(body))))
    return tuple(units)


def _journal_units(rel: str, text: str, mtime: float) -> tuple[Unit, ...]:
    """The entries of one day's journal file (see ``parse_units``)."""
    day = _iso_date(PurePosixPath(rel).stem)
    zone = local_zone()
    noon = local_ts(day, zone, 12) if day else mtime
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in text.splitlines():
        if line.startswith("## "):
            sections.append((line[3:].strip(), []))
        elif not (len(sections) == 1 and line.startswith("# ")):  # the file's "# Thursday 02 October" title
            sections[-1][1].append(line)
    drafts: list[tuple[str, str, float, list[str]]] = []
    for heading, lines in sections:
        topics: list[str] = []
        body: list[str] = []
        for line in lines:
            if (match := _TOPICS.match(line)) is not None:
                topics += links_in(match.group(1))
            else:
                body.append(line)
        words = "\n".join(body).strip()
        if (clock := _clock(heading)) is not None:
            base, ts = f"{clock[0]:02d}:{clock[1]:02d}", local_ts(day, zone, *clock) if day else mtime
        elif heading:
            base, ts, words = slugify(heading), noon, f"{heading}\n{words}".strip()
        else:
            base, ts = "", noon
        if strip_links(words).strip():
            drafts.append((base, words, ts, topics))
    anchors = _anchors(base for base, *_ in drafts)
    return tuple(_unit("journal", rel, anchor, words, ts, topics)
                 for anchor, (_, words, ts, topics) in zip(anchors, drafts, strict=True))


def _note_units(rel: str, text: str, mtime: float) -> tuple[Unit, ...]:
    """The sections of one note (see ``parse_units``)."""
    front = FRONT_MATTER.match(text)
    ts = mtime
    if front is not None and (created := _CREATED.search(front.group(1))) is not None:
        day = _iso_date(created.group(1))
        clock = (int(created.group(2)), int(created.group(3))) if created.group(2) else (12, 0)
        if day is not None and clock[0] < 24 and clock[1] < 60:
            ts = local_ts(day, local_zone(), *clock)
    title = ""
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in (text[front.end():] if front else text).splitlines():
        if line.startswith("## "):
            sections.append((line[3:].strip(), []))
        elif not title and len(sections) == 1 and line.startswith("# "):
            title = line[2:].strip()
        else:
            sections[-1][1].append(line)
    title = title or PurePosixPath(rel).stem
    drafts: list[tuple[str, str]] = []
    for heading, lines in sections:
        words = "\n".join(lines).strip()
        if heading or words:
            drafts.append((slugify(heading) if heading else "", "\n".join(p for p in (title, heading, words) if p)))
    if not drafts:
        drafts.append(("", title))
    anchors = _anchors(base for base, _ in drafts)
    return tuple(_unit("note", rel, anchor, words, ts) for anchor, (_, words) in zip(anchors, drafts, strict=True))


def parse_archive(text: str, mtime: float) -> tuple[Archived, ...]:
    """Read archive.md's lines ("- YYYY-MM-DD HH:MM · FILE · why: text").

    Args:
        text: the archive's contents.
        mtime: its modification time, the date of a line that doesn't parse.

    Returns:
        One record per "- " line with words, in file order.
    """
    out: list[Archived] = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.startswith("- "):
            continue
        ts, kind, why, words = mtime, None, "", line[2:].strip()
        if (match := _ARCHIVE_LINE.match(line)) is not None:
            kind, why, words = KIND_OF_FILE.get(match.group(4).strip()), match.group(5).strip(), match.group(6).strip()
            day, hour, minute = _iso_date(match.group(1)), int(match.group(2)), int(match.group(3))
            if day is not None and hour < 24 and minute < 60:
                ts = local_ts(day, local_zone(), hour, minute)
        if words:
            out.append(Archived(number, ts, kind, why, words))
    return tuple(out)


def _archive_units(rel: str, text: str, mtime: float) -> tuple[Unit, ...]:
    """One unit per archived line, dated by the line (see ``parse_units``)."""
    return tuple(_unit("archive", rel, str(item.line), item.text, item.ts) for item in parse_archive(text, mtime))


def _head(text: str) -> str:
    """What an entry file holds before its first bullet, ending in one blank line; "" when that is blank."""
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith(("- ", "* ")):
            break
        lines.append(line)
    head = "\n".join(lines).rstrip()
    return f"{head}\n\n" if head.strip() else ""


# ---------------------------------------------------------------------------------------------------- the vault
class Vault:
    """The memory folder: cached parsing, entry rewrites, journal, notes and archive, concepts and the graph.

    Thread-safe. Writes take ``locked()``, which excludes the other threads of this process and the other process
    (Orion and his tools process both write here); it is re-entrant within a thread, so a caller that already holds
    it (the writer, for a batch of operations) can call any write method.
    """

    def __init__(self, root: Path, retrieval: "RetrievalConfig") -> None:
        """Open the folder, creating it, journal/, notes/, USER.md and MEMORY.md (with their headers) when missing.

        Args:
            root: the memory folder (``cfg.vault_path``).
            retrieval: the live retrieval settings; ``not_concepts`` is read at use time.
        """
        self.root = root
        self._retrieval = retrieval
        self._cache: dict[Path, tuple[tuple[int, int, int], tuple[Unit, ...]]] = {}
        self._mutex = threading.Lock()  # guards the caches and memos below
        self._concepts: tuple[tuple[object, ...], ConceptSet] | None = None
        self._graph: tuple[tuple[object, ...], dict[str, JSON]] | None = None
        self._skills: dict[str, str] = {}
        self._user_label = "You"
        self._lock = FileLock(root / ".lock", mode=0o600)  # per thread: re-entrant, and other threads wait too
        for sub in ("journal", "notes"):
            (root / sub).mkdir(parents=True, exist_ok=True)
        for kind in CORE:
            path = root / FILES[kind]
            if not path.exists():
                with contextlib.suppress(FileExistsError), path.open("x", encoding="utf-8") as f:
                    f.write(HEADERS[kind])

    # ------------------------------------------------------------------------------------------------ reading
    def file(self, kind: EntryKind) -> Path:
        """The path of an entry file (it may not exist yet).

        Args:
            kind: "user", "orion", "facts" or "unverified".

        Returns:
            ``root / FILES[kind]``.
        """
        return self.root / FILES[kind]

    def files(self) -> list[Path]:
        """Every file that holds memory, in a fixed order: the entry files, the journal, the notes, the archive.

        Returns:
            The existing files: USER.md, MEMORY.md, facts.md, unverified.md, journal/*.md and notes/*.md (sorted by
            name), archive.md.
        """
        out = [path for path in map(self.file, FILES) if path.is_file()]
        out += sorted((self.root / "journal").glob("*.md")) + sorted((self.root / "notes").glob("*.md"))
        if (archive := self.root / ARCHIVE).is_file():
            out.append(archive)
        return out

    def units(self, path: Path) -> tuple[Unit, ...]:
        """The units of one file, parsed again only when the file changed (one ``stat`` otherwise).

        Args:
            path: a file inside the memory folder.

        Returns:
            Its units; () when it does not exist.
        """
        try:
            st = path.stat()
        except FileNotFoundError:
            with self._mutex:
                self._cache.pop(path, None)
            return ()
        return self._parsed(path, st)

    def entries(self, kind: EntryKind) -> tuple[Unit, ...]:
        """The entries of one entry file, in file order.

        Args:
            kind: "user", "orion", "facts" or "unverified".

        Returns:
            Its entries; () when the file does not exist.
        """
        return self.units(self.file(kind))

    def entry(self, prefix: str) -> Unit | None:
        """The entry whose key starts with ``prefix`` (a node id's tail), looked for in every entry file.

        Args:
            prefix: the start of an entry key.

        Returns:
            The first such entry; None when there is none (or the prefix is empty).
        """
        return next((unit for kind in FILES for unit in self.entries(kind) if prefix and unit.key.startswith(prefix)),
                    None)

    def page(self, kind: str, stem: str) -> Path | None:
        """The file of a journal day or a note on the map.

        Args:
            kind: "day" or "note".
            stem: the file's name without ".md" ("2026-10-04", "drone-pid").

        Returns:
            The file; None when it doesn't exist or the stem isn't a plain name.
        """
        path = self.root / ("journal" if kind == "day" else "notes") / f"{stem}.md"
        return path if "/" not in stem and path.is_file() else None

    def journal_today(self, max_chars: int, now: datetime) -> str:
        """Today's journal for the snapshot: the newest entries that fit, oldest first, one per line.

        Args:
            max_chars: the budget (``brain.snapshot_journal_chars``); 0 gives "".
            now: the local time that says which day is today.

        Returns:
            Lines like "[09:12] The user asked about Tailscale."; when even the newest entry is longer than the
            budget, it alone, cut at a word boundary.
        """
        if max_chars <= 0:
            return ""
        lines: list[str] = []
        used = 0
        for unit in reversed(self.units(self.root / "journal" / f"{now:%Y-%m-%d}.md")):
            clock = unit.anchor.split("#", 1)[0]
            prefix = f"[{clock}] " if _clock(clock) else ""
            line = prefix + " ".join(unit.plain.split())
            if used + len(line) + bool(lines) > max_chars:
                if not lines:
                    lines.append(clip(line, max_chars, flat=False))
                break
            lines.append(line)
            used += len(line) + (len(lines) > 1)
        return "\n".join(reversed([line for line in lines if line]))

    def recent_notes(self, n: int) -> list[tuple[Path, float]]:
        """The notes changed most recently.

        Args:
            n: how many.

        Returns:
            Up to ``n`` (note file, modification time in epoch seconds), newest first.
        """
        notes = [(path, st.st_mtime) for path, st in self._stats((self.root / "notes").glob("*.md"))]
        return sorted(notes, key=lambda item: item[1], reverse=True)[:n]

    def concepts(self) -> ConceptSet:
        """The concepts linked in trusted memory: USER.md, MEMORY.md, facts.md, the journal and the notes.

        Text from unverified.md never defines a concept, and ``retrieval.not_concepts`` are left out. Rebuilt only
        when one of those files or the stop list changed (one ``stat`` per file otherwise).

        Returns:
            The concept set, first spelling kept.
        """
        stop = self.not_concepts()
        stats = self._stats(path for path in self.files()
                            if path.name != ARCHIVE and path != self.file("unverified"))
        key: tuple[object, ...] = (tuple((path, file_stamp(st)) for path, st in stats), stop)
        with self._mutex:
            if self._concepts is not None and self._concepts[0] == key:
                return self._concepts[1]
        names = (name for path, st in stats for unit in self._parsed(path, st) for name in unit.concepts)
        concepts = concept_set(_unique(name for name in names if is_concept(name, stop)))
        with self._mutex:
            self._concepts = (key, concepts)
        return concepts

    # ------------------------------------------------------------------------------------------------ writing
    @contextlib.contextmanager
    def locked(self, timeout: float = LOCK_TIMEOUT_S) -> Iterator[None]:
        """Hold the memory lock: no other thread of this process and no other process writes meanwhile.

        Re-entrant within a thread; never waits longer than ``timeout`` (filelock: a lock file, flock'ed). Call it
        from worker threads, never on the event loop.

        Args:
            timeout: the longest wait, in seconds.

        Raises:
            VaultBusy: the lock stayed taken for the whole timeout.
        """
        try:
            self._lock.acquire(timeout=timeout)
        except Timeout:
            raise VaultBusy("Memory is busy with another change; try again in a moment.") from None
        try:
            yield
        finally:
            self._lock.release()

    def write_entries(self, kind: EntryKind, texts: Sequence[str]) -> None:
        """Rewrite an entry file as its head plus one bullet per entry.

        The hand-written head (title and introduction before the first bullet) is kept; a missing or blank head
        becomes the default header. facts.md and unverified.md are not created just to hold nothing.

        Args:
            kind: the file.
            texts: the entries, in order; white space inside an entry becomes single spaces, empty ones are dropped.

        Raises:
            VaultBusy: the memory lock could not be taken.
        """
        path = self.file(kind)
        with self.locked():
            try:
                head = _head(path.read_text(encoding="utf-8", errors="replace")) or HEADERS[kind]
            except FileNotFoundError:
                if not texts and kind in ("facts", "unverified"):
                    return
                head = HEADERS[kind]
            body = "".join(f"- {flat}\n" for text in texts if (flat := " ".join(text.split())))
            write_atomic(path, head + body)
            self._forget(path)

    def append_journal(self, text: str, concepts: Sequence[str], when: datetime) -> Unit:
        """Add a "## HH:MM" entry to the day's journal file, linking known concepts.

        Topics the text doesn't mention are listed on a "Topics:" line, so the entry still links them.

        Args:
            text: what was talked about (one to three sentences).
            concepts: the conversation's topics; the ones that aren't concepts are dropped.
            when: the local time of the entry; it names the file and the heading.

        Returns:
            The entry as written.

        Raises:
            ValueError: the text is empty.
            VaultBusy: the memory lock could not be taken.
        """
        if not text.strip():
            raise ValueError("A journal entry needs some text.")
        stop = self.not_concepts()
        topics = _unique(name for name in concepts if is_concept(name, stop))
        path = self.root / "journal" / f"{when:%Y-%m-%d}.md"
        with self.locked():
            body = self.link(text.strip(), topics)
            linked = {target.casefold() for target in links_in(body)}
            if missing := [name for name in topics if name.casefold() not in linked]:
                body += "\n\nTopics: " + SEP.join(f"[[{name}]]" for name in missing)
            path.parent.mkdir(parents=True, exist_ok=True)
            fresh = not path.exists()
            with path.open("a", encoding="utf-8") as f:
                f.write((f"# {when:%A %d %B %Y}\n\n" if fresh else "") + f"## {when:%H:%M}\n\n{body}\n\n")
            self._forget(path)
            return self.units(path)[-1]

    def save_note(self, text: str, title: str, when: datetime) -> Path:
        """Save a note as its own file under notes/, with a ``created`` date and known concepts linked.

        Args:
            text: the note.
            title: its title; "" uses the first seven words of the text.
            when: the local time it was created.

        Returns:
            The new file (a number is added to the name when one with that title exists).

        Raises:
            VaultBusy: the memory lock could not be taken.
        """
        title = " ".join(title.split()) or " ".join(strip_links(text).split()[:7]) or "Note"
        notes = self.root / "notes"
        with self.locked():
            slug = slugify(title)
            path, n = notes / f"{slug}.md", 2
            while path.exists():
                path, n = notes / f"{slug}-{n}.md", n + 1
            write_atomic(path, f"---\ncreated: {when:%Y-%m-%d %H:%M}\ntags: [orion]\n---\n\n# {title}\n\n"
                                f"{self.link(text.strip())}\n")
            self._forget(path)
        return path

    def append_archive(self, kind: EntryKind, text: str, why: str, when: datetime) -> None:
        """Record a removed or replaced entry in archive.md, so nothing disappears silently.

        Args:
            kind: the file it left.
            text: the entry (written with its links shown as words).
            why: one word such as "removed", "updated" or "demoted".
            when: the local time.

        Raises:
            VaultBusy: the memory lock could not be taken.
        """
        reason = " ".join(why.replace(":", " ").replace("·", " ").split()) or "removed"
        line = f"- {when:%Y-%m-%d %H:%M}{SEP}{FILES[kind]}{SEP}{reason}: {' '.join(strip_links(text).split())}\n"
        path = self.root / ARCHIVE
        with self.locked(), path.open("a", encoding="utf-8") as f:
            f.write(line)
        self._forget(path)

    def link(self, text: str, extra: Sequence[str] = ()) -> str:
        """Link the known concepts, and these extra ones, in a text.

        Args:
            text: Markdown.
            extra: more concept names (the ones that aren't concepts are dropped).

        Returns:
            The linked text (see ``link_text``).
        """
        concepts = self.concepts()
        if extra:
            stop = self.not_concepts()
            concepts = concept_set(concepts.names + tuple(name.strip() for name in extra if is_concept(name, stop)))
        return link_text(text, concepts)

    # ------------------------------------------------------------------------------------------------ the map
    def graph(self, skills: Sequence[tuple[str, str]], user_label: str, *,
              targets: Mapping[str, str] | None = None) -> dict[str, JSON]:
        """The neural map: the user, Orion, what he knows, the concepts joining it, and his skills.

        Node ids stay the same when other entries change: "you", "orion", "fact:<key[:10]>" (about the user),
        "insight:<key[:10]>" (his notes), "unverified:<key[:10]>", "day:YYYY-MM-DD", "note:<file stem>",
        "concept:<name, case-folded>" and "skill:<name>". Nodes carry no text; ``node(id)`` gives it. Memoised
        until a file, the skills, the label or the targets change.

        Args:
            skills: (name, description) of each skill.
            user_label: the user's node label (their name, or "You").
            targets: for facts.md and unverified.md entries, the file each was meant for ("user" or "orion"),
                by entry key; an entry without one counts as about the user.

        Returns:
            ``{"nodes": [{id, label, kind, degree}], "links": [{source, target}], "counts": {kind: n}}``; the
            same object until something changes, so callers must not modify it.
        """
        stop = self.not_concepts()
        stats = self._stats(self.files())
        key: tuple[object, ...] = (tuple((path, file_stamp(st)) for path, st in stats), tuple(skills), user_label,
                                   tuple(sorted((targets or {}).items())), stop)
        with self._mutex:
            self._skills, self._user_label = dict(skills), user_label
            if self._graph is not None and self._graph[0] == key:
                return self._graph[1]
        graph = self._build_graph(stats, skills, user_label, targets or {}, stop)
        with self._mutex:
            self._graph = (key, graph)
        return graph

    def node(self, node_id: str) -> dict[str, JSON] | None:
        """One node of the map with its text, fetched when the HUD opens it.

        Args:
            node_id: an id from ``graph()``.

        Returns:
            ``{id, kind, label, text}``, plus ``path`` for entries, days and notes; None for an unknown id.
        """
        kind, _, rest = node_id.partition(":")
        with self._mutex:
            skills, user_label = dict(self._skills), self._user_label
        if node_id == "you":
            return {"id": "you", "kind": "you", "label": user_label,
                    "text": "What Orion knows about you lives in USER.md."}
        if node_id == "orion":
            return {"id": "orion", "kind": "orion", "label": "ORION",
                    "text": "His own notes live in MEMORY.md; the journal records your conversations."}
        if kind in ENTRY_NODES and _ENTRY_ID.fullmatch(rest):
            if (unit := self.entry(rest)) is None:
                return None
            return {"id": node_id, "kind": kind, "label": clip(unit.plain, LABEL_CHARS), "text": unit.plain,
                    "path": unit.path}
        if kind in ("day", "note"):
            if (path := self.page(kind, rest)) is None:
                return None
            units = self.units(path)
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except FileNotFoundError:
                return None
            if kind == "day":
                body = "\n".join(line for line in text.splitlines() if not line.startswith("# "))
                return {"id": node_id, "kind": kind, "label": _day_label(rest), "text": strip_links(body).strip(),
                        "path": units[0].path if units else ""}
            front = FRONT_MATTER.match(text)
            title = units[0].text.split("\n", 1)[0] if units else rest
            return {"id": node_id, "kind": kind, "label": title,
                    "text": strip_links(text[front.end():] if front else text).strip(),
                    "path": units[0].path if units else ""}
        if kind == "concept" and rest:
            for path in self.files():
                for unit in self.units(path):
                    if name := next((c for c in unit.concepts if c.casefold() == rest), ""):
                        return {"id": node_id, "kind": kind, "label": name, "text": ""}
            return None
        if kind == "skill" and rest in skills:
            return {"id": node_id, "kind": kind, "label": rest, "text": skills[rest]}
        return None

    def not_concepts(self) -> frozenset[str]:
        """The names never made concepts, case-folded (``retrieval.not_concepts``, read now).

        Returns:
            The stop list ``is_concept`` takes.
        """
        return frozenset(name.casefold().strip() for name in self._retrieval.not_concepts)

    # ------------------------------------------------------------------------------------------------ internals
    def _stats(self, paths: Iterable[Path]) -> list[tuple[Path, os.stat_result]]:
        """(path, stat) for each file that still exists."""
        out: list[tuple[Path, os.stat_result]] = []
        for path in paths:
            with contextlib.suppress(FileNotFoundError):
                out.append((path, path.stat()))
        return out

    def _parsed(self, path: Path, st: os.stat_result) -> tuple[Unit, ...]:
        """The units of a file whose stat is known: from the cache when the stamp matches, else parsed now."""
        stamp = file_stamp(st)
        with self._mutex:
            hit = self._cache.get(path)
        if hit is not None and hit[0] == stamp:
            return hit[1]
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return ()
        units = parse_units(path.relative_to(self.root).as_posix(), text, st.st_mtime)
        with self._mutex:
            self._cache[path] = (stamp, units)
        return units

    def _forget(self, path: Path) -> None:
        """Drop a file's cached parse after this process wrote it (a write within one clock tick keeps its size)."""
        with self._mutex:
            self._cache.pop(path, None)

    def _build_graph(self, stats: Sequence[tuple[Path, os.stat_result]], skills: Sequence[tuple[str, str]],
                     user_label: str, targets: Mapping[str, str], stop: frozenset[str]) -> dict[str, JSON]:
        """Build the map from the parsed files (see ``graph``)."""
        nodes: dict[str, tuple[str, str]] = {}  # id -> (label, kind)
        links: dict[tuple[str, str], None] = {}  # ordered and without repeats
        concept_names: dict[str, str] = {}

        def add(node_id: str, label: str, kind: str, hub: str = "") -> None:
            """Add a node once, linked to a hub."""
            nodes.setdefault(node_id, (label, kind))
            if hub:
                links[(node_id, hub)] = None

        def join(node_id: str, names: Iterable[str]) -> None:
            """Link a node to each concept it names."""
            for name in names:
                if is_concept(name, stop):
                    folded = name.casefold()
                    concept_names.setdefault(folded, name)
                    add(f"concept:{folded}", concept_names[folded], "concept")
                    links[(node_id, f"concept:{folded}")] = None

        add("you", user_label, "you")
        add("orion", "ORION", "orion")
        links[("you", "orion")] = None
        journal, notes = self.root / "journal", self.root / "notes"
        for path, st in stats:
            units = self._parsed(path, st)
            entry = KIND_OF_FILE.get(path.name) if path.parent == self.root else None
            if entry is not None:
                for unit in units:
                    about_him = entry == "orion" or (entry in ("facts", "unverified")
                                                      and targets.get(unit.key) == "orion")
                    kind = "unverified" if entry == "unverified" else "insight" if about_him else "fact"
                    node_id = f"{kind}:{unit.key[:10]}"
                    add(node_id, clip(unit.plain, LABEL_CHARS), kind, "orion" if about_him else "you")
                    join(node_id, unit.concepts)
            elif path.parent == journal and units:
                add(f"day:{path.stem}", _day_label(path.stem), "day", "orion")
                join(f"day:{path.stem}", (name for unit in units for name in unit.concepts))
            elif path.parent == notes and units:
                add(f"note:{path.stem}", units[0].text.split("\n", 1)[0], "note", "you")
                join(f"note:{path.stem}", (name for unit in units for name in unit.concepts))
        for name, _ in skills:
            add(f"skill:{name}", name, "skill", "orion")
        degree = dict.fromkeys(nodes, 0)
        for source, target in links:
            degree[source] += 1
            degree[target] += 1
        return {
            "nodes": [{"id": node_id, "label": label, "kind": kind, "degree": degree[node_id]}
                      for node_id, (label, kind) in nodes.items()],
            "links": [{"source": source, "target": target} for source, target in links],
            "counts": {kind: sum(1 for _, k in nodes.values() if k == kind) for kind in _COUNTED},
        }


def _day_label(stem: str) -> str:
    """A journal day's label: "Thu 02 Oct" for "2026-10-02", the stem itself when it is not a date."""
    day = _iso_date(stem)
    return f"{day:%a %d %b}" if day else stem
