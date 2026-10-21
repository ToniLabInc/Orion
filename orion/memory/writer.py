"""The only way curated memory changes: operations on entries by id, checked, deduplicated, routed by provenance and
written atomically.

Callers: the memory tool (in the tools process, on a worker thread), the memory review (in Orion's process, in a
worker thread) and note saving. One ``apply`` holds the memory lock for its whole batch: the files first, then one
database transaction for the metadata (the same order in every process, so two writers never deadlock). Each touched
file is rewritten once.

Rules:
- Provenance: the user's own words (through the brain or the HUD), the review and hand edits are trusted; anything
  after web or app content in a turn ("tainted") or from another app ("app") is not. Untrusted additions go to
  unverified.md (searchable, labelled, never recalled, never promoted); untrusted changes to core or facts entries are
  refused. Text that reads like instructions to a model, or like a shell command, is kept apart the same way; invisible
  control characters are refused outright, and so is a secret (a key, a password, a token: memory is read by models, so
  the vault is where a secret lives).
- Duplicates: the same text, or a meaning at least ``memory.same_fact_similarity`` close, is already known; at least
  ``memory.similar_fact_similarity`` close, the model is asked to update the old entry instead (or to force).
- Size: a trusted addition that would overflow USER.md or MEMORY.md goes to facts.md at once (no "it's full" rounds).
- Ids: entries are named by the shortest unique prefix of their content key ("[#3fa2]"); an id that fits several gets
  the candidates back, never a guess.
- Nothing is lost silently: an update or a removal writes the old text to archive.md.
"""

import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final, Literal, Protocol

import numpy as np

from orion import redact
from orion.base import JSON, TRUSTED, Provenance, clip, local_zone
from orion.memory.store import MetaRow, Store
from orion.memory.vault import (
    CORE,
    FILES,
    LOCK_TIMEOUT_S,
    EntryKind,
    Vault,
    VaultBusy,
    entry_key,
    normalise,
    short_ids,
    strip_links,
)

if TYPE_CHECKING:
    from numpy.typing import NDArray

    from orion.config import Config
    from orion.inference import Embedder
    from orion.memory.index import Index

type OpName = Literal["ADD", "UPDATE", "DELETE", "NOOP"]
type Target = Literal["user", "orion"]
type Status = Literal["saved", "overflow", "unverified", "updated", "removed", "demoted", "promoted", "noop",
                      "similar", "refused", "error"]

ENTRY_MAX_CHARS: Final = 300  # one entry is one short fact
EVENT_TEXT_CHARS: Final = 140  # the longest text a memory_update event carries
PREVIEW_CHARS: Final = 80  # how much of a similar entry the model is shown
UNTRUSTED_REFUSAL: Final = ("Core memory only changes from what the user tells you directly; this turn used web or "
                            "app content.")

_INVISIBLE: Final = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\U000e0000-\U000e007f"
                               "\x00-\x08\x0b-\x1f\x7f]")
_INSTRUCTION: Final = re.compile(
    r"\b(?:ignore|disregard|forget)\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|earlier|your)\s+"
    r"(?:instructions|messages|prompts?|rules)\b|\bsystem\s+prompt\b|\bfrom\s+now\s+on\b|\byou\s+(?:must|should)\s+"
    r"always\b|\balways\s+(?:run|execute|open|call|send|visit)\b|\bdo\s+not\s+tell\b|\bnew\s+instructions\b",
    re.IGNORECASE)
_SHELL: Final = re.compile(r"`|\$\(|\brm\s+-rf\b|\|\s*(?:ba)?sh\b|\bcurl\s|\bwget\s|\bsudo\s|\bchmod\s+\+x\b",
                           re.IGNORECASE)
_URL: Final = re.compile(r"https?://|\bwww\.", re.IGNORECASE)
_HEX: Final = re.compile(r"[0-9a-f]{2,40}")


class _Keyed(Protocol):
    """An entry as ``resolve`` reads it: its key and its plain text."""

    @property
    def key(self) -> str:
        """The content key."""

    @property
    def plain(self) -> str:
        """The text, links shown as words."""


@dataclass(frozen=True, slots=True)
class Op:
    """One change asked of memory.

    Attributes:
        op: "ADD", "UPDATE", "DELETE" or "NOOP".
        target: the file an addition is for: "user" (USER.md) or "orion" (MEMORY.md).
        id: the entry an update or removal is about ("3fa2", "#3fa2" or "[#3fa2]").
        text: the new text (ADD, UPDATE).
        old_text: words of the entry, when no id is given (older callers).
        force: add even though a similar entry exists.
        demote: (the review) a removal moves the entry to facts.md instead of the archive.
        vec: the text's vector, when the hub sent one (saves embedding it again).
    """

    op: OpName
    target: Target = "user"
    id: str = ""
    text: str = ""
    old_text: str = ""
    force: bool = False
    demote: bool = False
    vec: "NDArray[np.float32] | None" = None


@dataclass(frozen=True, slots=True)
class Change:
    """What one operation did.

    Attributes:
        op: the operation.
        status: "saved", "overflow", "unverified", "updated", "removed", "demoted", "promoted", "noop", "similar",
            "refused" or "error".
        kind: the file written ("user", "orion", "facts", "unverified", "note"; "" when none was).
        target: the file meant: "user", "orion" or "notes".
        key: the entry's content key ("" when none).
        id: its id as shown to the model ("" when none).
        text: the entry's text.
        message: the result, for the model.
    """

    op: OpName
    status: Status
    kind: str
    target: str
    key: str
    id: str
    text: str
    message: str

    @property
    def ok(self) -> bool:
        """Whether the operation was carried out (or there was nothing to do)."""
        return self.status not in ("refused", "error")

    def event(self, source: Provenance) -> dict[str, JSON]:
        """The ``memory_update`` HUD event for this change (the hub adds ``call`` and ``turn``).

        Args:
            source: where the change came from.

        Returns:
            ``{id, target, action, status, text, result, ok, profile, source}``.
        """
        action = "save" if self.kind == "note" else {"ADD": "add", "UPDATE": "update", "DELETE": "remove",
                                                      "NOOP": "add"}[self.op]
        text = clip(self.text, EVENT_TEXT_CHARS, flat=False)
        return {"id": self.id, "target": self.target, "action": action, "status": self.status, "text": text,
                "result": self.message, "ok": self.ok, "profile": self.kind in CORE, "source": source}


@dataclass(frozen=True, slots=True)
class Scan:
    """What the injection scan found in a text.

    Attributes:
        reject: why it is refused outright (invisible characters), None when it isn't.
        quarantine: why it is kept apart in unverified.md (instructions, shell, links from untrusted sources).
    """

    reject: str | None
    quarantine: str | None


@dataclass(slots=True)
class _Entry:
    """An entry while a batch works on it."""

    kind: EntryKind
    key: str
    text: str
    plain: str


# ---------------------------------------------------------------------------------------------------- pure parts
def scan(text: str, *, untrusted: bool = False) -> Scan:
    """Look for what memory must not keep as a fact (a secret is one the process's redactor knows, ``orion.redact``).

    Args:
        text: the entry.
        untrusted: whether it comes from web or app content (then any link is suspect too).

    Returns:
        The verdict.
    """
    if _INVISIBLE.search(text):
        return Scan("It holds invisible control characters, which are never saved.", None)
    if redact.holds_secret(text):
        return Scan("It holds a secret (a key, a token or a password), which is never saved in memory: "
                    "the vault keeps secrets.", None)
    if _INSTRUCTION.search(text):
        return Scan(None, "it reads like instructions to an assistant")
    if _SHELL.search(text):
        return Scan(None, "it holds a shell command")
    if untrusted and _URL.search(text):
        return Scan(None, "it links to a web address")
    return Scan(None, None)


def route(target: Target, source: Provenance, quarantined: bool) -> EntryKind:
    """The file an addition goes to.

    Args:
        target: the file meant ("user" or "orion").
        source: where it came from.
        quarantined: whether the scan kept it apart.

    Returns:
        The target for a trusted, clean addition; "unverified" otherwise.
    """
    return target if source in TRUSTED and not quarantined else "unverified"


def dedup(cos: float | None, *, exact: bool, force: bool, same: float,
          similar: float) -> Literal["noop", "similar", "new"]:
    """Whether a new entry repeats a saved one.

    Args:
        cos: the best cosine to a saved entry (None without vectors).
        exact: whether a saved entry has the same normalised text.
        force: whether the caller insists it is new.
        same: ``memory.same_fact_similarity``.
        similar: ``memory.similar_fact_similarity``.

    Returns:
        "noop" (already known), "similar" (ask to update instead), or "new".
    """
    if exact or (cos is not None and cos >= same):
        return "noop"
    if cos is not None and cos >= similar and not force:
        return "similar"
    return "new"


def resolve[E: _Keyed](ref: str, entries: Sequence[E], *, by_text: bool = False) -> E | list[E] | None:
    """The entry an id (or, for older callers, some words) means.

    Args:
        ref: an id ("3fa2", "#3fa2", "[#3fa2]") or, with ``by_text``, words of the entry.
        entries: the entries to look in.
        by_text: match a normalised substring instead of a key prefix.

    Returns:
        The entry; the candidates when several match; None when none does.
    """
    if by_text:
        needle = normalise(ref)
        matches = [e for e in entries if needle and needle in normalise(e.plain)]
    else:
        prefix = ref.strip().strip("[]").lstrip("#").strip().lower()
        if not _HEX.fullmatch(prefix):
            return None
        matches = [e for e in entries if e.key.startswith(prefix)]
    if not matches:
        return None
    return matches[0] if len(matches) == 1 else matches


def used_chars(entries: Iterable[_Keyed]) -> int:
    """How much of a file's cap entries use.

    Args:
        entries: the file's entries.

    Returns:
        Their plain text plus a line break each, in characters.
    """
    return sum(len(entry.plain) + 1 for entry in entries)


# ---------------------------------------------------------------------------------------------------- the writer
class _Batch:
    """The entry files while one ``apply`` works on them, and what it will write."""

    def __init__(self, vault: Vault) -> None:
        """Start with nothing read."""
        self.vault = vault
        self.files: dict[EntryKind, list[_Entry]] = {}
        self.dirty: set[EntryKind] = set()
        self.archive: list[tuple[EntryKind, str, str]] = []
        self.meta: list[MetaRow] = []
        self.superseded: list[tuple[str, float, str | None, str]] = []

    def entries(self, kind: EntryKind) -> list[_Entry]:
        """The entries of one file (read on first use)."""
        if kind not in self.files:
            self.files[kind] = [_Entry(kind, u.key, u.text, u.plain) for u in self.vault.entries(kind)]
        return self.files[kind]

    def all(self) -> list[_Entry]:
        """Every entry of every entry file."""
        return [entry for kind in FILES for entry in self.entries(kind)]

    def short_id(self, key: str) -> str:
        """An entry's id among every entry's key (the new one included)."""
        keys = [entry.key for entry in self.all()]
        if key not in keys:
            keys.append(key)
        return short_ids(keys)[keys.index(key)]

    def add(self, kind: EntryKind, text: str) -> _Entry:
        """Append an entry to a file."""
        entry = _Entry(kind, entry_key(text), text, strip_links(text))
        self.entries(kind).append(entry)
        self.dirty.add(kind)
        return entry

    def drop(self, entry: _Entry) -> None:
        """Take an entry out of its file."""
        self.files[entry.kind] = [e for e in self.entries(entry.kind) if e is not entry]
        self.dirty.add(entry.kind)

    def replace(self, entry: _Entry, text: str) -> _Entry:
        """Put new text in an entry's place."""
        new = _Entry(entry.kind, entry_key(text), text, strip_links(text))
        self.files[entry.kind] = [new if e is entry else e for e in self.entries(entry.kind)]
        self.dirty.add(entry.kind)
        return new

    def write(self, when: datetime) -> None:
        """Rewrite each touched file once and append the archive lines (the vault lock is held)."""
        for kind in FILES:
            if kind in self.dirty:
                self.vault.write_entries(kind, [entry.text for entry in self.files[kind]])
        for kind, text, why in self.archive:
            self.vault.append_archive(kind, text, why, when)


class Writer:
    """Applies memory operations (see the module docstring). Call it from worker threads, never the event loop."""

    def __init__(self, store: Store, vault: Vault, index: "Index", embedder: "Embedder", cfg: "Config") -> None:
        """Hold the parts.

        Args:
            store: the database (metadata).
            vault: the memory files.
            index: the unit index (the saved entries' vectors).
            embedder: embeds texts the index has no vector for.
            cfg: the live settings (``memory.*``).
        """
        self.store = store
        self.vault = vault
        self.index = index
        self.embedder = embedder
        self.cfg = cfg

    def apply(self, ops: Sequence[Op], *, source: Provenance, now: float | None = None) -> list[Change]:
        """Carry out operations as one batch.

        Args:
            ops: the operations, in order (each sees the effect of the ones before).
            source: where they come from (decides what may reach the core files).
            now: epoch seconds (tests pass a fixed time).

        Returns:
            One change per operation; all "error" when the memory stayed busy.
        """
        stamp = time.time() if now is None else now
        when = datetime.fromtimestamp(stamp, local_zone())
        try:
            with self.vault.locked(LOCK_TIMEOUT_S):
                batch = _Batch(self.vault)
                changes = [self._one(op, batch, source, stamp) for op in ops]
                batch.write(when)
                self.store.meta_write(batch.meta, batch.superseded)
        except VaultBusy:
            return [Change(op.op, "error", "", op.target, "", "", op.text, "Memory is busy with another change; try "
                           "again in a moment.") for op in ops]
        return changes

    def save_note(self, text: str, title: str, *, source: Provenance, now: float | None = None) -> Change:
        """Save a note as its own file under notes/ (an untrusted one is kept, but never recalled).

        Args:
            text: the note.
            title: its title ("" uses its first words).
            source: where it came from.
            now: epoch seconds.

        Returns:
            What was saved.
        """
        body = text.strip()
        if not body:
            return Change("ADD", "error", "", "notes", "", "", "", "The note is empty.")
        checked = scan(body)
        if checked.reject:
            return Change("ADD", "refused", "", "notes", "", "", body, checked.reject)
        stamp = time.time() if now is None else now
        try:
            path = self.vault.save_note(body, title, datetime.fromtimestamp(stamp, local_zone()))
        except VaultBusy:
            return Change("ADD", "error", "", "notes", "", "", body, "Memory is busy; try again in a moment.")
        rel = path.relative_to(self.vault.root).as_posix()
        key = f"file:{rel}"
        self.store.meta_write([MetaRow(key, "note", None, source, stamp, " ".join(body.split())[:ENTRY_MAX_CHARS])],
                              [])
        name = " ".join(title.split()) or path.stem
        return Change("ADD", "saved", "note", "notes", key, path.stem, name, f"Saved the note \"{name}\" ({rel}).")

    def forget(self, key: str) -> Change:
        """Forget one entry for good, as the user asks at the HUD: out of its file with no archive line, and nothing
        kept about where it came from (unlike a removal by Orion, which archive.md keeps).

        Args:
            key: the entry's content key.

        Returns:
            What was forgotten, or why nothing was.
        """
        try:
            with self.vault.locked(LOCK_TIMEOUT_S):
                batch = _Batch(self.vault)
                entry = next((e for e in batch.all() if e.key == key), None)
                if entry is None:
                    return Change("DELETE", "error", "", "", key, "", "", "That memory is no longer there.")
                batch.drop(entry)
                batch.write(datetime.now(local_zone()))
                self.store.meta_forget([key])
        except VaultBusy:
            return Change("DELETE", "error", "", "", key, "", "", "Memory is busy; try again in a moment.")
        return Change("DELETE", "removed", entry.kind, entry.kind, key, "", entry.plain, f"Forgot: {entry.plain}")

    def confirm(self, key: str, *, now: float | None = None) -> Change:
        """Trust an unverified entry because the user confirmed it at the HUD: it moves to the file it was meant for
        (USER.md or MEMORY.md, or facts.md when that one is full), learned from the HUD from now on.

        Args:
            key: the entry's content key (an entry of unverified.md).
            now: epoch seconds.

        Returns:
            Where it went, or why it didn't move.
        """
        stamp = time.time() if now is None else now
        try:
            with self.vault.locked(LOCK_TIMEOUT_S):
                batch = _Batch(self.vault)
                entry = next((e for e in batch.entries("unverified") if e.key == key), None)
                if entry is None:
                    return Change("UPDATE", "error", "", "", key, "", "", "That memory is no longer unverified.")
                meta = self.store.meta_rows([key]).get(key)
                target: Target = "orion" if meta is not None and meta.target == "orion" else "user"
                kind: EntryKind = target if self._fits(batch, target, entry.plain) else "facts"
                batch.drop(entry)
                new = batch.add(kind, entry.text)
                batch.write(datetime.fromtimestamp(stamp, local_zone()))
                self.store.meta_write([MetaRow(new.key, kind, target if kind == "facts" else None, "hud", stamp,
                                               new.plain)], [])
        except VaultBusy:
            return Change("UPDATE", "error", "", "", key, "", "", "Memory is busy; try again in a moment.")
        return Change("UPDATE", "promoted", kind, target, new.key, "", new.plain,
                      f"Confirmed, now in {FILES[kind]}: {new.plain}")

    # ------------------------------------------------------------------------------------------------ internals
    def _fits(self, batch: _Batch, kind: Target, plain: str) -> bool:
        """Whether one more entry fits in USER.md (``memory.user_chars``) or MEMORY.md (``memory.self_chars``)."""
        cap = self.cfg.memory.user_chars if kind == "user" else self.cfg.memory.self_chars
        return used_chars(batch.entries(kind)) + len(plain) + 1 <= cap

    def _one(self, op: Op, batch: _Batch, source: Provenance, now: float) -> Change:
        """Carry out one operation on the batch."""
        if op.op == "ADD":
            return self._add(op, batch, source, now)
        if op.op in ("UPDATE", "DELETE"):
            found = self._resolve(op, batch)
            if isinstance(found, Change):
                return found
            if source not in TRUSTED and found.kind in ("user", "orion", "facts"):
                return Change(op.op, "refused", "", op.target, found.key, batch.short_id(found.key), found.plain,
                              UNTRUSTED_REFUSAL)
            if op.op == "UPDATE":
                return self._update(op, found, batch, source, now)
            return self._delete(op, found, batch, now)
        return Change(op.op, "noop", "", op.target, "", "", "", "Nothing to change.")

    def _clean(self, op: Op, text: str, source: Provenance) -> tuple[str, Scan] | Change:
        """The text of an addition or update, or the change refusing it."""
        if not text:
            return Change(op.op, "error", "", op.target, "", "", "", "There is no text to save.")
        checked = scan(text, untrusted=source not in TRUSTED)
        if checked.reject:
            return Change(op.op, "refused", "", op.target, "", "", text, checked.reject)
        if len(strip_links(text)) > ENTRY_MAX_CHARS:
            return Change(op.op, "error", "", op.target, "", "", text, f"Too long for one entry: save one short "
                          f"fact per entry (at most {ENTRY_MAX_CHARS} characters).")
        return text, checked

    def _add(self, op: Op, batch: _Batch, source: Provenance, now: float) -> Change:
        """An addition: checked, deduplicated, routed, capped (see the module docstring)."""
        cleaned = self._clean(op, " ".join(op.text.split()), source)
        if isinstance(cleaned, Change):
            return cleaned
        text, checked = cleaned
        kind = route(op.target, source, checked.quarantine is not None)
        family = [e for e in batch.all() if e.kind != "unverified" or kind == "unverified"]
        key = entry_key(text)
        plain = strip_links(text)
        twin = next((e for e in family if e.key == key), None)
        best, cos = (twin, None) if twin else self._closest(plain, op.vec, family)
        memory = self.cfg.memory
        verdict = dedup(cos, exact=twin is not None, force=op.force, same=memory.same_fact_similarity,
                        similar=memory.similar_fact_similarity)
        if verdict != "new" and best is not None:
            known = batch.short_id(best.key)
            if verdict == "noop":
                return Change("ADD", "noop", best.kind, op.target, best.key, known, best.plain,
                              f"Already known [#{known}]: {best.plain}")
            quoted = clip(best.plain, PREVIEW_CHARS, flat=False)
            return Change("ADD", "similar", best.kind, op.target, best.key, known, best.plain,
                          f"Similar to [#{known}] \"{quoted}\": update it with id {known}, or add "
                          "again with force=true if it is really new.")
        status: Status = "unverified" if kind == "unverified" else "saved"
        if kind in CORE and not self._fits(batch, kind, plain):
            kind, status = "facts", "overflow"
        entry = batch.add(kind, self.vault.link(text))
        target = op.target if kind in ("facts", "unverified") else None
        batch.meta.append(MetaRow(entry.key, kind, target, source, now, entry.plain))
        known = batch.short_id(entry.key)
        why = checked.quarantine or "it came from web or app content"
        message = {
            "saved": f"Saved to {FILES[kind]} as [#{known}].",
            "overflow": f"{FILES[op.target]} is full, so it went to facts.md as [#{known}] (searched when it matters).",
            "unverified": f"Saved to unverified.md as [#{known}]: {why}, so it is kept apart and never treated as "
                          "known about the user.",
        }[status]
        return Change("ADD", status, kind, op.target, entry.key, known, entry.plain, message)

    def _resolve(self, op: Op, batch: _Batch) -> _Entry | Change:
        """The entry an update or removal means, or the change explaining why it can't be found."""
        entries = batch.all()
        if op.id.strip():
            found = resolve(op.id, entries)
            what = f"the id {op.id.strip()}"
        elif op.old_text.strip():
            found = resolve(op.old_text, entries, by_text=True)
            what = f"\"{clip(op.old_text.strip(), PREVIEW_CHARS, flat=False)}\""
        else:
            return Change(op.op, "error", "", op.target, "", "", "", "Say which entry: its id, e.g. [#3fa2].")
        if found is None:
            return Change(op.op, "error", "", op.target, "", "", "", f"No entry matches {what}.")
        if isinstance(found, list):
            listed = "; ".join(f"[#{batch.short_id(e.key)}] {clip(e.plain, PREVIEW_CHARS, flat=False)}"
                               for e in found[:6])
            return Change(op.op, "error", "", op.target, "", "", "", f"Several entries match {what}: {listed}. Use "
                          "the full id.")
        return found

    def _update(self, op: Op, entry: _Entry, batch: _Batch, source: Provenance, now: float) -> Change:
        """An update: in place, or (the review) a facts entry promoted to its core file."""
        cleaned = self._clean(op, " ".join(op.text.split()), source)
        if isinstance(cleaned, Change):
            return cleaned
        text, checked = cleaned
        old_id = batch.short_id(entry.key)
        if checked.quarantine and entry.kind != "unverified":
            return Change("UPDATE", "refused", "", op.target, entry.key, old_id, text,
                          f"Not saved: {checked.quarantine}.")
        if entry_key(text) == entry.key:
            return Change("UPDATE", "noop", entry.kind, op.target, entry.key, old_id, entry.plain,
                          f"[#{old_id}] already says that.")
        linked = self.vault.link(text)
        status: Status = "updated"
        if source == "review" and entry.kind == "facts" and self._fits(batch, op.target, strip_links(linked)):
            batch.drop(entry)
            new = batch.add(op.target, linked)
            status = "promoted"
        else:
            new = batch.replace(entry, linked)
        batch.archive.append((entry.kind, entry.text, "updated"))
        batch.superseded.append((entry.key, now, new.key, "updated"))
        target = op.target if new.kind in ("facts", "unverified") else None
        batch.meta.append(MetaRow(new.key, new.kind, target, source, now, new.plain))
        new_id = batch.short_id(new.key)
        verb = f"Moved to {FILES[new.kind]} and updated" if status == "promoted" else "Updated"
        return Change("UPDATE", status, new.kind, op.target, new.key, new_id, new.plain,
                      f"{verb} [#{new_id}]: {entry.plain} -> {new.plain}")

    def _delete(self, op: Op, entry: _Entry, batch: _Batch, now: float) -> Change:
        """A removal (archived), or (the review) a core entry demoted to facts.md."""
        known = batch.short_id(entry.key)
        batch.drop(entry)
        if op.demote and entry.kind in CORE:
            batch.add("facts", entry.text)
            meta = self.store.meta_rows([entry.key]).get(entry.key)
            batch.meta.append(MetaRow(entry.key, "facts", entry.kind, meta.source if meta else "hand",
                                      meta.learned_at if meta else now, entry.plain,
                                      use_count=meta.use_count if meta else 0,
                                      last_used=meta.last_used if meta else None))
            return Change("DELETE", "demoted", "facts", entry.kind, entry.key, known, entry.plain,
                          f"Moved [#{known}] to facts.md: {entry.plain}")
        batch.archive.append((entry.kind, entry.text, "removed"))
        batch.superseded.append((entry.key, now, None, "removed"))
        return Change("DELETE", "removed", entry.kind, op.target, entry.key, known, entry.plain,
                      f"Removed [#{known}]: {entry.plain}")

    def _closest(self, plain: str, vec: "NDArray[np.float32] | None",
                 family: Sequence[_Entry]) -> tuple[_Entry | None, float | None]:
        """The saved entry closest in meaning to a text, and its cosine (None, None without vectors)."""
        if not family:
            return None, None
        stored = self.index.vectors([e.key for e in family])
        missing = [e for e in family if e.key not in stored]
        texts = [e.plain for e in missing] + ([plain] if vec is None else [])
        fresh = self.embedder.encode(texts) if texts else np.zeros((0, 0), dtype=np.float32)
        if fresh is None:
            return None, None
        vectors = {**stored, **{e.key: fresh[i] for i, e in enumerate(missing)}}
        query = vec if vec is not None else fresh[-1]
        rows = [(e, vectors[e.key]) for e in family if e.key in vectors and vectors[e.key].shape == query.shape]
        if not rows:
            return None, None
        cosines = np.stack([row for _, row in rows]) @ query  # unit rows: the dot product is the cosine
        best = int(np.argmax(cosines))
        return rows[best][0], float(cosines[best])
