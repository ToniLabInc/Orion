"""Orion's memory, opened for one process: SQLite for logs and metadata (store.py), plain Markdown for what he
knows (vault.py), the unit index over both (index.py), and the one write path for curated memory (writer.py).

``MemorySystem.open`` gives each process its role:
- "main" (Orion himself) owns the index: it records an owner lease (its process id), syncs changed files after the
  watcher reports them and indexes each finished turn, both on the database's writer thread;
- "tools" (his tools process) reads the index and writes curated memory; Orion's watcher picks the files up;
- "serve" (``orion mcp-serve`` for other apps) reads like "tools", and syncs the index itself before a search when
  no Orion owns it (the lease's process is gone).

The rest of Orion calls only the few operations here plus the parts' own APIs (``store``, ``vault``, ``index``,
``writer``). Forgetting is here too: one memory of the neural map (``forget``, ``confirm`` for an unverified one), or
everything (``wipe``, run with no process holding the memory open).
"""

import contextlib
import logging
import os
import sqlite3
import time
from collections.abc import Callable, Collection, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

from orion.base import JSON, Query
from orion.memory.index import Index, RecallResult, SyncReport
from orion.memory.store import MetaChange, Role, Store
from orion.memory.vault import ARCHIVE, ENTRY_NODES, FILES, EntryKind, Vault
from orion.memory.writer import Writer

if TYPE_CHECKING:
    from orion.config import Config
    from orion.inference import Embedder

log = logging.getLogger("orion.memory")

LEASE: Final = "index_owner"  # the settings row holding the owner's process id
CHANGE_KINDS: Final = ("user", "orion", "facts", "note")  # what the per-turn change list reports (no unverified)
# What forgetting everything empties: the conversations and their traces, and the memory's metadata and index.
WIPED_TABLES: Final = ("steps", "messages", "conversations", "item_concepts", "mem_items", "index_files", "mem_meta")
# Settings rows learned from conversations, or pointing into the emptied log.
WIPED_SETTINGS: Final = ("home_city", "last_weather_city", "reviewed_message_id", "indexed_message_id", LEASE)


class MemorySystem:
    """The memory parts of one process and the operations the rest of Orion needs from them.

    Attributes:
        store: the database.
        vault: the memory folder.
        index: the unit index.
        writer: the write path for curated memory.
        role: "main", "tools" or "serve".
        on_indexed: called (on the database's writer thread) after a sync changed the index; the runtime refreshes
            the speech vocabulary and tells the HUD.
    """

    def __init__(self, cfg: "Config", store: Store, vault: Vault, index: Index, writer: Writer, *, role: Role) -> None:
        """Hold the opened parts (use ``open``).

        Args:
            cfg: the live settings.
            store: the database.
            vault: the memory folder.
            index: the unit index.
            writer: the write path.
            role: the process's role.
        """
        self.cfg = cfg
        self.store = store
        self.vault = vault
        self.index = index
        self.writer = writer
        self.role = role
        self.on_indexed: Callable[[SyncReport], None] | None = None

    @classmethod
    def open(cls, cfg: "Config", embedder: "Embedder", *, role: Role) -> "MemorySystem":
        """Open (and, the first time, create) the memory for a process.

        Args:
            cfg: the live settings.
            embedder: the process's embedder (not loaded here).
            role: "main", "tools" or "serve".

        Returns:
            The memory system.

        Raises:
            OrionError: the database has another schema version (see ``Store.ensure_schema``).
            sqlite3.Error: the database could not be opened or created.
        """
        store = Store(cfg.data_path / "orion.db", cfg.memory, role=role)
        store.ensure_schema()
        vault = Vault(cfg.vault_path, cfg.retrieval)
        index = Index(store, vault, embedder, cfg)
        system = cls(cfg, store, vault, index, Writer(store, vault, index, embedder, cfg), role=role)
        if role == "main":
            store.set_setting(LEASE, str(os.getpid()))
        return system

    # ------------------------------------------------------------------------------------------------ the owner
    def warm(self) -> SyncReport:
        """Get the index ready (main, in a worker thread): load the embedder, re-embed if the model changed, sync the
        files and the log, and load the vectors.

        Returns:
            What the sync changed.
        """
        started = time.monotonic()
        self.index.embedder.load()
        embedded = self.index.ensure_embedder()
        files = self.index.sync_files()
        turns = self.index.sync_messages()
        self.index.load()
        report = SyncReport(files.files, files.units_added + embedded, files.units_removed, turns, self.index.version,
                            (time.monotonic() - started) * 1000)
        self._indexed(report)
        return report

    def on_files_changed(self, paths: Collection[Path]) -> None:
        """Memory files changed (the watcher, which debounces bursts): index them on the writer thread (main, or
        serve as the owner).

        Args:
            paths: the changed files.
        """
        if self.role != "tools":
            self.store.submit(self._sync_files, set(paths))

    def after_turn(self) -> None:
        """A turn finished: index it (queued on the database's writer thread, never waited for)."""
        if self.role == "main":
            self.store.submit(self._sync_messages)

    def reindex(self) -> None:
        """Rebuild the index from scratch (``Apply.REINDEX``), on the writer thread."""
        if self.role != "tools":
            self.store.submit(self._rebuild)

    def refresh(self) -> None:
        """Before a search in "serve": sync the index when no Orion owns it (stat-only when nothing changed)."""
        if self.role == "serve" and not self._owner_alive():
            files = self.index.sync_files()
            turns = self.index.sync_messages()
            if files.files or turns:
                self._indexed(SyncReport(files.files, files.units_added, files.units_removed, turns,
                                         self.index.version, files.ms))

    # ------------------------------------------------------------------------------------------------ reading
    def recall(self, q: Query, *, conv: int, exclude: Collection[int]) -> RecallResult:
        """The memories worth putting before a reply (synchronous, a few milliseconds; see ``Index.recall``).

        Args:
            q: the request and its vector.
            conv: the current conversation.
            exclude: units already recalled in this context epoch.

        Returns:
            The memories recalled.
        """
        return self.index.recall(q, conv=conv, exclude=exclude)

    def changes_since(self, ts: float) -> list[MetaChange]:
        """What memory learned, replaced or removed since a moment (unverified entries never listed).

        Args:
            ts: epoch seconds.

        Returns:
            The changes, oldest first.
        """
        return self.store.meta_changes(ts, CHANGE_KINDS)

    def home_city(self) -> str:
        """The user's city: from the settings, or as they told Orion ("" when unknown)."""
        return self.cfg.user.city or self.store.get_setting("home_city")

    def graph(self, skills: Sequence[tuple[str, str]], user_label: str) -> dict[str, JSON]:
        """The neural map (see ``Vault.graph``), facts.md and unverified.md entries placed by the file they were meant
        for."""
        keys = [unit.key for kind in ("facts", "unverified") for unit in self.vault.entries(kind)]
        targets = {key: row.target for key, row in self.store.meta_rows(keys).items() if row.target}
        return self.vault.graph(skills, user_label, targets=targets)

    def node(self, node_id: str) -> dict[str, JSON] | None:
        """One node of the map with its text (see ``Vault.node``), and when an entry was learned and from where."""
        node = self.vault.node(node_id)
        if node is None or node.get("kind") not in ENTRY_NODES:
            return node
        unit = self.vault.entry(node_id.partition(":")[2])
        if unit is not None and (meta := self.store.meta_rows([unit.key]).get(unit.key)):
            node = {**node, "learned_at": meta.learned_at, "source": meta.source}
        return node

    def forget(self, node_id: str) -> str:
        """Forget one memory of the neural map for good (the user, at the HUD): an entry of USER.md, MEMORY.md,
        facts.md or unverified.md, a journal day or a note. The index follows the files.

        Args:
            node_id: an id from ``graph()``, such as "fact:3fa2b1c9d0" or "day:2026-10-04".

        Returns:
            What was forgotten.

        Raises:
            LookupError: there is no such memory (any more).
            ValueError: that kind of node can't be forgotten on its own (a hub, a concept, a skill).
            RuntimeError: the memory was busy.
        """
        kind, _, rest = node_id.partition(":")
        if kind in ENTRY_NODES:
            change = self.writer.forget(self._entry(rest))
            if not change.ok:
                raise RuntimeError(change.message)
            self.on_files_changed([self.vault.file(cast(EntryKind, change.kind))])
            return change.message
        if kind in ("day", "note"):
            if (path := self.vault.page(kind, rest)) is None:
                raise LookupError(f"No memory {node_id}.")
            with self.vault.locked():
                path.unlink(missing_ok=True)
            self.store.meta_forget([f"file:{path.relative_to(self.vault.root).as_posix()}"])
            self.on_files_changed([path])
            return f"Forgot {path.name}."
        raise ValueError("Only a memory itself can be forgotten: a fact, a note of his, a journal day or a note.")

    def confirm(self, node_id: str) -> str:
        """Trust an unverified memory the user confirmed at the HUD (see ``Writer.confirm``).

        Args:
            node_id: an "unverified:..." id from ``graph()``.

        Returns:
            Where it went.

        Raises:
            LookupError: there is no such unverified memory (any more).
            ValueError: the node isn't an unverified memory.
            RuntimeError: the memory was busy.
        """
        kind, _, rest = node_id.partition(":")
        if kind != "unverified":
            raise ValueError("Only an unverified memory can be confirmed.")
        change = self.writer.confirm(self._entry(rest))
        if not change.ok:
            raise RuntimeError(change.message)
        self.on_files_changed([self.vault.file("unverified"), self.vault.file(cast(EntryKind, change.kind))])
        return change.message

    def _entry(self, prefix: str) -> str:
        """The full key of the entry whose key starts with ``prefix`` (a node id's tail).

        Raises:
            LookupError: no entry has such a key.
        """
        if (unit := self.vault.entry(prefix)) is None:
            raise LookupError("That memory is no longer there.")
        return unit.key

    def vocabulary(self, n: int) -> list[str]:
        """The trusted concepts most worth priming the speech recogniser with (see ``Index.vocabulary``)."""
        return self.index.vocabulary(n)

    def close(self) -> None:
        """Give up the owner lease and close the database."""
        if self.role == "main" and self.store.get_setting(LEASE) == str(os.getpid()):
            self.store.set_setting(LEASE, "")
        self.store.close()

    # ------------------------------------------------------------------------------------------------ internals
    def _sync_files(self, paths: Collection[Path]) -> None:
        """Index changed files (on the writer thread)."""
        report = self.index.sync_files(paths)
        if report.files:
            self._indexed(report)

    def _sync_messages(self) -> None:
        """Index finished turns (on the writer thread)."""
        if turns := self.index.sync_messages():
            self._indexed(SyncReport(0, 0, 0, turns, self.index.version, 0.0))

    def _rebuild(self) -> None:
        """Rebuild the index (on the writer thread)."""
        self._indexed(self.index.rebuild())

    def _indexed(self, report: SyncReport) -> None:
        """Tell the listener the index changed."""
        if self.on_indexed is not None:
            try:
                self.on_indexed(report)
            except Exception:
                log.warning("the index listener failed", exc_info=True)

    def _owner_alive(self) -> bool:
        """Whether the process holding the owner lease still runs."""
        lease = self.store.get_setting(LEASE)
        return lease.isdigit() and Path(f"/proc/{lease}").exists() and int(lease) != os.getpid()


def wipe(cfg: "Config") -> list[str]:
    """Forget everything: every conversation and its trace, the memory files (USER.md, MEMORY.md, facts.md,
    unverified.md, archive.md, the journal and the notes), what is recorded about them, the memory index, what was
    learned in passing (the home city), and the logs. Settings, timers, reminders, tasks, skills and models stay. The
    vault writes fresh empty files at the next start.

    Run it only while no process has the memory open: ``Runtime.stop`` does, after everything else has stopped, and
    ``orion memory reset`` does while Orion is stopped.

    Args:
        cfg: the settings (where the data lives).

    Returns:
        What was cleared, in words, for the log or the console.
    """
    done: list[str] = []
    db_path = cfg.data_path / "orion.db"
    if db_path.is_file():
        with contextlib.closing(sqlite3.connect(db_path, timeout=cfg.memory.db_timeout_s, isolation_level=None)) as db:
            db.execute("BEGIN IMMEDIATE")
            for table in WIPED_TABLES:
                db.execute(f"DELETE FROM {table}")
            db.execute(f"DELETE FROM settings WHERE key IN ({', '.join('?' * len(WIPED_SETTINGS))})", WIPED_SETTINGS)
            db.execute("COMMIT")
            db.execute("INSERT INTO mem_fts(mem_fts) VALUES ('rebuild')")  # the full-text index, from no items
            db.execute("VACUUM")  # rewrite the file, so no deleted text stays in its free pages
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        done.append("conversations and the memory index")
    root = cfg.vault_path
    files = [root / name for name in (*FILES.values(), ARCHIVE)]
    files += [*(root / "journal").glob("*.md"), *(root / "notes").glob("*.md")]
    gone = 0
    for path in files:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
            gone += 1
    done.append(f"{gone} memory file{'' if gone == 1 else 's'}")
    logs = cfg.logs_path
    if logs.is_dir():
        for path in logs.glob("*.log*"):
            if path.suffix == ".log":
                path.write_bytes(b"")  # emptied in place: a running handler keeps appending to it
            else:
                path.unlink(missing_ok=True)  # rotated copies (orion.log.1, ...)
        done.append("the logs")
    log.info("forgot everything: %s", ", ".join(done))
    return done

