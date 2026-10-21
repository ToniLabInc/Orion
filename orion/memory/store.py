"""The database: one SQLite file (data/orion.db) shared by Orion and his tools process.

It keeps what must survive a restart and is not Markdown: the conversation log with its conversations and turns,
the operation trace (``steps``), reminders and timers, to-dos, small settings, the metadata of memory entries (when
and from where each was learned, what replaced it, how often it is used) and the unit index's tables. ``index.py``
owns those last tables; their schema lives here so the schema has one home.

Concurrency:
- Role "main" (Orion): one write connection confined to a single "db-writer" thread, so commits, checkpoints and
  waits for the other process never stall the event loop or the audio threads. Writes run in the order they were
  submitted. Reads use a query-only connection per thread, which sees everything committed.
- Roles "tools" and "serve": one connection behind a lock (their tools already run on worker threads).
Write-ahead logging lets readers and the writer of either process work at the same time.

Schema version 6 (``PRAGMA user_version``): a new file gets it at first open, a version-3, -4 or -5 file is
upgraded (it gains what it lacks of the jobs table and version 5's tables: every upgrade statement creates only what
is missing; a version-5 file's benchmark table is rebuilt to know the "stopped" status), and a file of another
version is refused.
"""

import asyncio
import contextlib
import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, TypedDict, cast

from orion.base import JSON, Conv, OrionError, Provenance, StepKind, StepRecord

if TYPE_CHECKING:
    from orion.config import MemoryConfig
    from orion.timing import Timing

log = logging.getLogger("orion.store")

type Role = Literal["main", "tools", "serve"]  # Orion; his tools process; `orion mcp-serve` for other apps
type ConvReason = Literal["start", "new_chat", "idle_gap"]  # why a conversation began
type MetaKind = Literal["user", "orion", "facts", "unverified", "note"]  # what a memory-metadata row describes

CURRENT_VERSION: Final = 6
BUSY_RETRY_S: Final = 20  # how long opening a brand-new file keeps retrying while another process sets it up
BUSY_POLL_S: Final = 0.05  # the pause between those retries

JOBS_SCHEMA: Final[tuple[str, ...]] = (  # version 4: scheduled jobs (orion/jobs.py)
    """CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY, name TEXT NOT NULL, schedule TEXT NOT NULL, prompt TEXT NOT NULL,
        deliver TEXT NOT NULL DEFAULT '[]', enabled INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL,
        next_run REAL, last_run REAL, last_status TEXT NOT NULL DEFAULT '', last_text TEXT NOT NULL DEFAULT '')""",
    "CREATE INDEX IF NOT EXISTS jobs_due ON jobs(enabled, next_run)",
)
# benchmark runs of the brains (orion/bench.py): results holds the run's plan, scores and error as JSON. "stopped" is a
# run you ended (version 6; before it, one you ended was "failed" with the error "Stopped before the end.").
BENCH_RUNS: Final = """bench_runs (
        id INTEGER PRIMARY KEY, at REAL NOT NULL, suite TEXT NOT NULL, brains TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL CHECK (status IN ('running', 'done', 'failed', 'stopped')),
        results TEXT NOT NULL DEFAULT '{}')"""
V5_SCHEMA: Final[tuple[str, ...]] = (  # version 5: each new table of Orion 5, in one place (CREATE ... IF NOT EXISTS)
    # the activity log (orion/activity.py): what he did and who allowed it, newest last
    """CREATE TABLE IF NOT EXISTS activity (
        id INTEGER PRIMARY KEY, at REAL NOT NULL, kind TEXT NOT NULL, source TEXT NOT NULL,
        actor TEXT NOT NULL DEFAULT '', connector TEXT NOT NULL DEFAULT '', tool TEXT NOT NULL DEFAULT '',
        title TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', outcome TEXT NOT NULL DEFAULT '',
        turn TEXT NOT NULL DEFAULT '', ms INTEGER NOT NULL DEFAULT 0)""",
    "CREATE INDEX IF NOT EXISTS activity_at ON activity(at)",
    # what OpenRouter cost, request by request (orion/spend.py)
    """CREATE TABLE IF NOT EXISTS spend (
        id INTEGER PRIMARY KEY, at REAL NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL, purpose TEXT NOT NULL,
        prompt_tokens INTEGER NOT NULL DEFAULT 0, completion_tokens INTEGER NOT NULL DEFAULT 0,
        cost REAL NOT NULL DEFAULT 0)""",
    "CREATE INDEX IF NOT EXISTS spend_at ON spend(at)",
    f"CREATE TABLE IF NOT EXISTS {BENCH_RUNS}",
    # each turn's timings (orion/timing.py): how long hearing, thinking, the tools and speaking took
    """CREATE TABLE IF NOT EXISTS timings (
        turn TEXT PRIMARY KEY, at REAL NOT NULL, source TEXT NOT NULL, brain TEXT NOT NULL, instant INTEGER NOT NULL,
        heard_ms INTEGER NOT NULL, think_ms INTEGER NOT NULL, tools_ms INTEGER NOT NULL, speak_ms INTEGER NOT NULL,
        total_ms INTEGER NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS timings_at ON timings(at)",
)
TIMINGS_KEPT: Final = 10_000  # turns whose timings are kept (about 1 MB; each brain's speeds read the latest 200)
STOPPED_RUNS: Final[tuple[str, ...]] = (  # version 6: a CHECK can't be altered, so the table is made again (the whole
    # upgrade is one transaction)
    "ALTER TABLE bench_runs RENAME TO bench_runs_v5",
    f"CREATE TABLE {BENCH_RUNS}",
    """INSERT INTO bench_runs
        SELECT id, at, suite, brains,
               CASE WHEN status = 'failed' AND json_extract(results, '$.error') = 'Stopped before the end.'
                    THEN 'stopped' ELSE status END,
               CASE WHEN status = 'failed' AND json_extract(results, '$.error') = 'Stopped before the end.'
                    THEN json_set(results, '$.error', '') ELSE results END
        FROM bench_runs_v5""",
    "DROP TABLE bench_runs_v5",
)
UPGRADES: Final[dict[int, tuple[str, ...]]] = {  # from a version: what brings it to the current one
    3: (*JOBS_SCHEMA, *V5_SCHEMA),
    4: V5_SCHEMA,
    5: STOPPED_RUNS,
}
SCHEMA: Final[tuple[str, ...]] = (
    """CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY, role TEXT NOT NULL, content TEXT NOT NULL, source TEXT, ts REAL NOT NULL,
        conv INTEGER NOT NULL DEFAULT 0, turn TEXT)""",
    "CREATE INDEX IF NOT EXISTS messages_conv ON messages(conv, id)",
    "CREATE INDEX IF NOT EXISTS messages_turn ON messages(turn)",
    """CREATE TABLE IF NOT EXISTS conversations (
        id INTEGER PRIMARY KEY, started REAL NOT NULL,
        reason TEXT NOT NULL CHECK (reason IN ('start', 'new_chat', 'idle_gap')))""",
    """CREATE TABLE IF NOT EXISTS steps (
        id TEXT PRIMARY KEY,
        turn TEXT,
        conv INTEGER NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('tool', 'memory', 'skill', 'recall')),
        name TEXT NOT NULL,
        title TEXT NOT NULL,
        server TEXT NOT NULL DEFAULT '',
        args TEXT NOT NULL DEFAULT '{}',
        source TEXT NOT NULL CHECK (source IN ('brain', 'app')),
        client TEXT NOT NULL DEFAULT '',
        ok INTEGER NOT NULL,
        result TEXT NOT NULL DEFAULT '',
        at REAL NOT NULL,
        ms INTEGER NOT NULL DEFAULT 0,
        taints INTEGER NOT NULL DEFAULT 0,
        memory TEXT) WITHOUT ROWID""",
    "CREATE INDEX IF NOT EXISTS steps_turn ON steps(turn)",
    "CREATE INDEX IF NOT EXISTS steps_conv ON steps(conv, at)",
    """CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY, text TEXT NOT NULL, due REAL NOT NULL,
        kind TEXT NOT NULL DEFAULT 'reminder', done INTEGER NOT NULL DEFAULT 0, ts REAL NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS reminders_open ON reminders(done, due)",
    """CREATE TABLE IF NOT EXISTS todos (
        id INTEGER PRIMARY KEY, text TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0, ts REAL NOT NULL)""",
    *JOBS_SCHEMA,
    *V5_SCHEMA,
    "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)",
    """CREATE TABLE IF NOT EXISTS mem_meta (
        key TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        target TEXT,
        source TEXT NOT NULL,
        learned_at REAL NOT NULL,
        superseded_at REAL,
        superseded_by TEXT,
        why TEXT,
        last_used REAL,
        use_count INTEGER NOT NULL DEFAULT 0,
        text TEXT NOT NULL) WITHOUT ROWID""",
    "CREATE INDEX IF NOT EXISTS mem_meta_learned ON mem_meta(learned_at)",
    "CREATE INDEX IF NOT EXISTS mem_meta_superseded ON mem_meta(superseded_at)",
    """CREATE TABLE IF NOT EXISTS mem_items (
        id INTEGER PRIMARY KEY,
        key TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL,
        path TEXT NOT NULL,
        anchor TEXT NOT NULL,
        text TEXT NOT NULL,
        keys TEXT NOT NULL DEFAULT '',
        ts REAL NOT NULL,
        conv INTEGER,
        source TEXT NOT NULL DEFAULT 'hand',
        vec BLOB)""",
    "CREATE INDEX IF NOT EXISTS mem_items_kind_ts ON mem_items(kind, ts)",
    "CREATE INDEX IF NOT EXISTS mem_items_path ON mem_items(path)",
    """CREATE VIRTUAL TABLE IF NOT EXISTS mem_fts USING fts5(
        text, keys, content='mem_items', content_rowid='id', tokenize='porter unicode61 remove_diacritics 2')""",
    """CREATE TRIGGER IF NOT EXISTS mem_items_ai AFTER INSERT ON mem_items BEGIN
        INSERT INTO mem_fts(rowid, text, keys) VALUES (new.id, new.text, new.keys); END""",
    """CREATE TRIGGER IF NOT EXISTS mem_items_ad AFTER DELETE ON mem_items BEGIN
        INSERT INTO mem_fts(mem_fts, rowid, text, keys) VALUES ('delete', old.id, old.text, old.keys); END""",
    """CREATE TRIGGER IF NOT EXISTS mem_items_au AFTER UPDATE OF text, keys ON mem_items BEGIN
        INSERT INTO mem_fts(mem_fts, rowid, text, keys) VALUES ('delete', old.id, old.text, old.keys);
        INSERT INTO mem_fts(rowid, text, keys) VALUES (new.id, new.text, new.keys); END""",
    """CREATE TABLE IF NOT EXISTS item_concepts (
        concept TEXT NOT NULL COLLATE NOCASE,
        item_id INTEGER NOT NULL REFERENCES mem_items(id) ON DELETE CASCADE,
        PRIMARY KEY (concept, item_id)) WITHOUT ROWID""",
    "CREATE INDEX IF NOT EXISTS item_concepts_item ON item_concepts(item_id)",
    """CREATE TABLE IF NOT EXISTS index_files (
        path TEXT PRIMARY KEY, mtime_ns INTEGER NOT NULL, size INTEGER NOT NULL) WITHOUT ROWID""",
)
_JOB_COLUMNS: Final = ("id, name, schedule, prompt, deliver, enabled, created, next_run, last_run, last_status, "
                       "last_text")
_ACTIVITY_COLUMNS: Final = "id, at, kind, source, actor, connector, tool, title, detail, outcome, turn, ms"
_BENCH_COLUMNS: Final = "id, at, suite, brains, status, results"
_TIMING_COLUMNS: Final = "turn, at, source, brain, instant, heard_ms, think_ms, tools_ms, speak_ms, total_ms"
_LOG_TIMING: Final = (
    f"INSERT INTO timings ({_TIMING_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(turn) DO UPDATE SET "
    "at = excluded.at, source = excluded.source, brain = excluded.brain, instant = excluded.instant, "
    "heard_ms = excluded.heard_ms, think_ms = excluded.think_ms, tools_ms = excluded.tools_ms, "
    "speak_ms = excluded.speak_ms, total_ms = excluded.total_ms")
_MESSAGE_COLUMNS: Final = "id, role, content, COALESCE(source, '') AS source, ts, conv, COALESCE(turn, '') AS turn"
_STEP_COLUMNS: Final = "id, turn, kind, name, title, server, args, source, client, at, ms, ok, result, taints, memory"
_META_COLUMNS: Final = ("key, kind, target, source, learned_at, text, superseded_at, superseded_by, why, last_used, "
                        "use_count")


class MessageRow(TypedDict):
    """One logged message.

    Keys:
        id: its row id (the HUD shows user messages and announcements as "m<id>").
        role: "user" or "assistant" (an announcement is an assistant message with source "announcement").
        content: the text.
        source: "hud", "voice" or "announcement" ("" when the log doesn't say).
        ts: when it was said, in epoch seconds.
        conv: its conversation.
        turn: its turn id ("" when it has none).
    """

    id: int
    role: str
    content: str
    source: str
    ts: float
    conv: int
    turn: str


class ReminderRow(TypedDict):
    """A pending reminder or timer: id, text, when it is due, kind ("reminder" or "timer") and when it was set."""

    id: int
    text: str
    due: float
    kind: str
    ts: float


class TodoRow(TypedDict):
    """An open to-do: id, text and when it was added."""

    id: int
    text: str
    ts: float


class JobRow(TypedDict):
    """A scheduled job (``orion.jobs``).

    Keys:
        id: its number.
        name: its name.
        schedule: its cron expression.
        prompt: what Orion is asked each time it runs.
        deliver: where its result goes ("speak", "hud", "discord").
        enabled: whether it runs.
        created: when it was made (epoch seconds).
        next_run: when it runs next (None: never).
        last_run: when it last ran (None: never).
        last_status: "ok", "error" or "" (never ran).
        last_text: what it last answered, or why it failed.
    """

    id: int
    name: str
    schedule: str
    prompt: str
    deliver: list[str]
    enabled: bool
    created: float
    next_run: float | None
    last_run: float | None
    last_status: str
    last_text: str


class ActivityRow(TypedDict):
    """One entry of the activity log (``orion.activity``).

    Keys:
        id: its number (newer entries have larger ones).
        at: when (epoch seconds).
        kind: what happened ("tool", "ask", "deny", "block", "alert", "job", ...: ``orion.activity.KINDS``).
        source: who or what asked ("voice", "hud", "discord", "job", "watcher", "gesture", "app", "user", "system").
        actor: who acted or answered, in a few words ("the brain", "the HUD on Firefox", a job's name; "" unsaid).
        connector: the connector involved ("" none).
        tool: the tool involved ("" none).
        title: one line for people.
        detail: more, already redacted ("" none).
        outcome: "ok", "failed", "denied", "blocked", "approved", "declined", "expired" ("" none).
        turn: the turn it belongs to ("" none).
        ms: how long it took (0 unsaid).
    """

    id: int
    at: float
    kind: str
    source: str
    actor: str
    connector: str
    tool: str
    title: str
    detail: str
    outcome: str
    turn: str
    ms: int


class SpendRow(TypedDict):
    """What one paid request cost (``orion.spend``).

    Keys:
        at: when (epoch seconds).
        provider: who charged ("openrouter").
        model: the model asked.
        purpose: what it was for ("conversation", "job", "review", "bench").
        prompt_tokens: tokens read.
        completion_tokens: tokens written.
        cost: the price, in US dollars.
    """

    at: float
    provider: str
    model: str
    purpose: str
    prompt_tokens: int
    completion_tokens: int
    cost: float


class BenchRow(TypedDict):
    """A benchmark run (``orion.bench``).

    Keys:
        id: its number.
        at: when it started (epoch seconds).
        suite: "builtin" or "history".
        brains: the brains it scores ("claude", "ollama:qwen3.5:9b"...).
        status: "running", "done", "failed" (it went wrong) or "stopped" (you ended it; its scores so far are kept).
        results: its plan, scores and error (``orion.bench``'s shape).
    """

    id: int
    at: float
    suite: str
    brains: list[str]
    status: str
    results: dict[str, JSON]


def _job(row: "sqlite3.Row") -> JobRow:
    """A jobs row as a JobRow (its deliver list decoded, enabled a bool)."""
    try:
        deliver = json.loads(row["deliver"] or "[]")
    except ValueError:
        deliver = []
    return JobRow(id=row["id"], name=row["name"], schedule=row["schedule"], prompt=row["prompt"],
                  deliver=[str(d) for d in deliver] if isinstance(deliver, list) else [],
                  enabled=bool(row["enabled"]), created=row["created"], next_run=row["next_run"],
                  last_run=row["last_run"], last_status=row["last_status"], last_text=row["last_text"])


@dataclass(frozen=True, slots=True)
class HudHistory:
    """What the HUD shows of a conversation after a (re)connect.

    Attributes:
        conv: the conversation.
        messages: its latest user and assistant messages, oldest first.
        steps: the operations of those messages' turns, by turn id, in the order they started.
        external: other apps' operations in this conversation since the oldest of those messages.
    """

    conv: int
    messages: list[MessageRow]
    steps: dict[str, list[StepRecord]]
    external: list[StepRecord]


@dataclass(frozen=True, slots=True)
class MetaRow:
    """The metadata of one memory entry (the Markdown files hold only its text).

    Attributes:
        key: the entry's content key (``vault.entry_key``), the same as its unit's key.
        kind: the file it lives in: "user", "orion", "facts", "unverified" or "note".
        target: for facts.md and unverified.md, the file it was meant for ("user" or "orion"); None otherwise.
        source: where it came from (see ``base.Provenance``).
        learned_at: when it was written, in epoch seconds.
        text: the entry, links shown as words.
        superseded_at: when it was removed or replaced; None while it is current.
        superseded_by: the key of the entry that replaced it; None when it was removed.
        why: the reason it was superseded ("removed", "updated", "demoted", "edited by hand", ...).
        last_used: when it was last recalled or found, in epoch seconds.
        use_count: how often it was recalled or found.
    """

    key: str
    kind: MetaKind
    target: str | None
    source: Provenance
    learned_at: float
    text: str
    superseded_at: float | None = None
    superseded_by: str | None = None
    why: str | None = None
    last_used: float | None = None
    use_count: int = 0


@dataclass(frozen=True, slots=True)
class MetaChange:
    """One change of memory, as the per-turn "what changed" list tells the model.

    Attributes:
        op: "+" learned, "~" replaced an entry that existed before, "-" removed.
        kind: the file ("user", "orion", "facts", "unverified" or "note").
        key: the entry's key.
        text: the entry (the removed one, for "-").
        ts: when it changed, in epoch seconds.
        source: where the entry came from.
    """

    op: Literal["+", "~", "-"]
    kind: MetaKind
    key: str
    text: str
    ts: float
    source: Provenance


class Store:
    """The database for one process (see the module docstring for roles and threads).

    Every public method may be called from any thread. Writes in role "main" run on the writer thread: called from
    another thread they wait for it, so on the event loop use ``submit`` (fire and forget, in order) or ``await
    call(...)`` instead. A read on another thread sees what is committed; a read that must see this process's own
    earlier ``submit`` goes through ``call`` too.
    """

    def __init__(self, path: Path, memory: "MemoryConfig", *, role: Role) -> None:
        """Open (or create) the file; the schema is checked by ``ensure_schema``.

        Args:
            path: the database file (``<data>/orion.db``); its folder is created when missing.
            memory: the memory settings (``db_timeout_s``: how long a write waits for the other process).
            role: "main", "tools" or "serve".
        """
        self.path = path
        self.role = role
        self._timeout_s = float(memory.db_timeout_s)
        self._closed = False
        self._lock = threading.RLock()  # roles tools and serve: guards the one connection
        self._local = threading.local()  # role main: each thread's read connection
        self._readers: list[sqlite3.Connection] = []
        self._readers_lock = threading.Lock()
        self._conv: Conv | None = None  # role main allocates conversation ids itself
        self._next_conv: int | None = None
        self._conv_lock = threading.Lock()
        self._writer_ident = 0
        path.parent.mkdir(parents=True, exist_ok=True)
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="db-writer" if role == "main" else "db")
        if role == "main":
            self._db = self._pool.submit(self._open_writer).result()
        else:
            self._db = self._connect(query_only=False)
            _retry_busy(lambda: self._db.execute("PRAGMA journal_mode=WAL").fetchall())

    # ------------------------------------------------------------------------------------------------ plumbing
    def write[T](self, work: Callable[[sqlite3.Connection], T]) -> T:
        """Run ``work`` in one write transaction (``BEGIN IMMEDIATE`` ... ``COMMIT``) and return its result.

        Role main: on the writer thread (waiting for it when called elsewhere). Other roles: under the lock. Inside
        a running transaction (``work`` writing more), it joins that transaction.

        Args:
            work: receives the write connection.

        Returns:
            What ``work`` returned.

        Raises:
            OrionError: the store is closed.
            sqlite3.Error: the transaction failed and was rolled back.
        """
        return self._on_writer(lambda db: _transaction(db, work))

    def submit[**P, T](self, fn: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> Future[T]:
        """Queue a store call on the store's thread without waiting (in role main, writes keep this order).

        A failure is logged; whoever keeps the future can still read it.

        Args:
            fn: a store method or a function using it.
            *args: its positional arguments.
            **kwargs: its keyword arguments.

        Returns:
            The future of its result.
        """
        future = self._pool.submit(fn, *args, **kwargs)
        future.add_done_callback(_log_failure)
        return future

    async def call[**P, T](self, fn: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
        """Run a store call on the store's thread and await it (it sees every earlier ``submit``).

        Args:
            fn: a store method or a function using it.
            *args: its positional arguments.
            **kwargs: its keyword arguments.

        Returns:
            Its result.
        """
        return await asyncio.wrap_future(self.submit(fn, *args, **kwargs))

    @contextlib.contextmanager
    def reading(self) -> Iterator[sqlite3.Connection]:
        """A connection to read with, for the caller's thread.

        Role main: this thread's query-only connection (the write connection on the writer thread, so a read inside
        a write sees it). Other roles: the one connection, held under the lock until the block ends.

        Yields:
            The connection; rows come back as ``sqlite3.Row``.

        Raises:
            OrionError: the store is closed.
        """
        if self._closed:
            raise OrionError("The database is closed.")
        if self.role != "main":
            with self._lock:
                yield self._db
            return
        if threading.get_ident() == self._writer_ident:
            yield self._db
            return
        db: sqlite3.Connection | None = getattr(self._local, "db", None)
        if db is None:
            db = self._connect(query_only=True)
            self._local.db = db
            with self._readers_lock:
                self._readers.append(db)
        yield db

    def ensure_schema(self) -> None:
        """Give a new file the current schema; safe to call from every process (one of them creates it).

        Raises:
            OrionError: the file has another schema: a newer Orion's, or an older one (no longer upgraded).
        """
        def create(db: sqlite3.Connection) -> None:
            """Create the schema, unless another process just did; upgrade a file one version behind; refuse a file
            that is neither new nor upgradable."""
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            if version == CURRENT_VERSION:
                return
            if version in UPGRADES:
                for statement in UPGRADES[version]:
                    db.execute(statement)
                db.execute(f"PRAGMA user_version = {CURRENT_VERSION}")
                return
            if version or db.execute("SELECT 1 FROM sqlite_master").fetchone() is not None:
                who = "a newer Orion; update Orion" if version > CURRENT_VERSION else "an Orion too old to upgrade"
                raise OrionError(f"{self.path.name} has schema {version}, written by {who} (this one reads "
                                 f"{CURRENT_VERSION}).")
            for statement in SCHEMA:
                db.execute(statement)
            db.execute(f"PRAGMA user_version = {CURRENT_VERSION}")

        with self.reading() as db:
            current = int(db.execute("PRAGMA user_version").fetchone()[0]) == CURRENT_VERSION
        if not current:
            self.write(create)

    def checkpoint(self) -> None:
        """Fold the write-ahead log back into the file without waiting for readers (call it when idle)."""
        self._on_writer(lambda db: db.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchall())

    def data_version(self) -> int:
        """A number that changes whenever another connection commits (another process, or this one's writer).

        Returns:
            ``PRAGMA data_version`` of this thread's read connection.
        """
        with self.reading() as db:
            return int(db.execute("PRAGMA data_version").fetchone()[0])

    def close(self) -> None:
        """Run what is queued, optimise the query planner's statistics and close every connection. Idempotent."""
        if self._closed:
            return
        if self.role == "main":
            self._pool.submit(self._shutdown, self._db).result()
        else:
            with self._lock:
                self._shutdown(self._db)
        self._closed = True
        self._pool.shutdown(wait=True)
        with self._readers_lock:
            for db in self._readers:
                db.close()
            self._readers.clear()

    # ------------------------------------------------------------------------------------------------ conversations
    def current_conv(self) -> Conv:
        """The conversation in progress.

        Role main starts one (reason "start") when the file has none; the other roles report the newest one, or
        ``Conv(0, 0.0)`` before Orion ever ran.

        Returns:
            The conversation.
        """
        with self._conv_lock:
            if self._conv is not None:
                return self._conv
        with self.reading() as db:
            row = db.execute("SELECT id, started FROM conversations ORDER BY id DESC LIMIT 1").fetchone()
        if row is not None:
            conv = Conv(int(row["id"]), float(row["started"]))
            if self.role == "main":
                with self._conv_lock:
                    self._conv = self._conv or conv
                    return self._conv
            return conv
        return self.new_conv("start", time.time()) if self.role == "main" else Conv(0, 0.0)

    def new_conv(self, reason: ConvReason, now: float) -> Conv:
        """Start a conversation.

        Role main allocates the id itself and queues the row, so this returns at once; messages logged with it
        afterwards are written after the row. The other roles insert it and wait.

        Args:
            reason: why: "start", "new_chat" or "idle_gap".
            now: when, in epoch seconds.

        Returns:
            The new conversation.
        """
        if self.role != "main":
            return Conv(self.write(lambda db: _insert_conv(db, None, now, reason)), now)
        with self._conv_lock:
            if self._next_conv is None:
                with self.reading() as db:
                    self._next_conv = int(db.execute("SELECT COALESCE(MAX(id), 0) FROM conversations")
                                          .fetchone()[0]) + 1
            conv = Conv(self._next_conv, now)
            self._next_conv += 1
            self._conv = conv
        self.submit(self.write, lambda db: _insert_conv(db, conv.id, now, reason))
        return conv

    def log_message(self, role: str, content: str, *, source: str, conv: int, turn: str, ts: float) -> int:
        """Append a message to the log.

        Args:
            role: "user" or "assistant".
            content: the text.
            source: "hud", "voice" or "announcement".
            conv: its conversation.
            turn: its turn id ("" for none).
            ts: when it was said, in epoch seconds.

        Returns:
            Its row id.
        """
        return self.write(lambda db: int(cast(int, db.execute(
            "INSERT INTO messages (role, content, source, ts, conv, turn) VALUES (?, ?, ?, ?, ?, ?)",
            (role, content, source, ts, conv, turn or None)).lastrowid)))

    def history(self, conv: int, limit: int) -> list[MessageRow]:
        """The latest messages of a conversation, oldest first (the brain's history after a restart).

        Args:
            conv: the conversation.
            limit: how many at most.

        Returns:
            The messages.
        """
        with self.reading() as db:
            rows = db.execute(f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE conv = ? ORDER BY id DESC LIMIT ?",
                              (conv, limit)).fetchall()
        return [MessageRow(**row) for row in reversed(rows)]

    def messages_after(self, message_id: int, limit: int) -> list[MessageRow]:
        """Messages logged after one, oldest first (the memory review reads unreviewed ones).

        Args:
            message_id: the last message already handled (0 for all).
            limit: how many at most.

        Returns:
            The messages.
        """
        with self.reading() as db:
            rows = db.execute(f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE id > ? ORDER BY id LIMIT ?",
                              (message_id, limit)).fetchall()
        return [MessageRow(**row) for row in rows]

    def count_messages_after(self, message_id: int) -> int:
        """How many messages were logged after one.

        Args:
            message_id: the last message already handled.

        Returns:
            The count.
        """
        with self.reading() as db:
            return int(db.execute("SELECT COUNT(*) FROM messages WHERE id > ?", (message_id,)).fetchone()[0])

    def hud_history(self, conv: int, limit: int) -> HudHistory:
        """A conversation as the HUD rebuilds it: its latest messages, their turns' operations and other apps' ones.

        Args:
            conv: the conversation.
            limit: how many messages at most (other apps' operations are capped at the same number).

        Returns:
            The history.
        """
        with self.reading() as db:
            rows = db.execute(f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE conv = ? AND role IN "
                              "('user', 'assistant') ORDER BY id DESC LIMIT ?", (conv, limit)).fetchall()
            messages = [MessageRow(**row) for row in reversed(rows)]
            turns = {message["turn"] for message in messages if message["turn"]}
            steps: dict[str, list[StepRecord]] = {}
            for row in db.execute(f"SELECT {_STEP_COLUMNS} FROM steps WHERE turn IN (SELECT value FROM json_each(?)) "
                                  "ORDER BY at", (json.dumps(list(turns)),)):
                steps.setdefault(str(row["turn"]), []).append(_step(row))
            oldest = messages[0]["ts"] if messages else 0.0
            external = [_step(row) for row in reversed(db.execute(
                f"SELECT {_STEP_COLUMNS} FROM steps WHERE conv = ? AND turn IS NULL AND at >= ? "
                "ORDER BY at DESC LIMIT ?", (conv, oldest, limit)).fetchall())]
        return HudHistory(conv, messages, steps, external)

    def tainted_turns(self, turns: Collection[str]) -> set[str]:
        """Which of these turns used web or app content (an operation that taints its turn succeeded).

        Args:
            turns: turn ids.

        Returns:
            The tainted ones.
        """
        with self.reading() as db:
            return {str(row[0]) for row in db.execute("SELECT DISTINCT turn FROM steps WHERE taints = 1 AND turn IN "
                                                      "(SELECT value FROM json_each(?))", (json.dumps(list(turns)),))}

    # ------------------------------------------------------------------------------------------------ steps
    def log_step(self, rec: StepRecord, conv: int) -> None:
        """Store one operation of the trace (a brain or another app's; a second write with its id replaces it).

        Args:
            rec: the operation, as ``mcp.OpTrace`` or the turn runner built it.
            conv: the conversation it happened in.

        Raises:
            sqlite3.IntegrityError: its source is not "brain" or "app", or its kind is unknown.
        """
        memory = rec["memory"]
        values = (rec["id"], rec["turn"] or None, conv, rec["kind"], rec["name"], rec["title"], rec["server"],
                  json.dumps(rec["args"], ensure_ascii=False, separators=(",", ":")), rec["source"], rec["client"],
                  int(rec["ok"]), rec["result"], rec["at"], int(rec["ms"]), int(rec["taints"]),
                  None if memory is None else json.dumps(memory, ensure_ascii=False, separators=(",", ":")))
        self.write(lambda db: db.execute(
            "INSERT OR REPLACE INTO steps (id, turn, conv, kind, name, title, server, args, source, client, ok, "
            "result, at, ms, taints, memory) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", values))

    def skill_last_used(self) -> dict[str, float]:
        """When each skill was last used: started on its own (a "skill" step) or by ``use_skill``.

        Returns:
            Skill name -> epoch seconds.
        """
        with self.reading() as db:
            rows = db.execute(
                "SELECT CASE WHEN kind = 'skill' THEN name ELSE json_extract(args, '$.name') END AS skill, MAX(at) "
                "FROM steps WHERE kind = 'skill' OR (name = 'use_skill' AND ok = 1) GROUP BY skill").fetchall()
        return {str(row[0]): float(row[1]) for row in rows if row[0]}

    # ------------------------------------------------------------------------------------------------ organizer
    def add_reminder(self, text: str, due: float, kind: str = "reminder") -> int:
        """Save a reminder or a timer.

        Args:
            text: what to say when it is due.
            due: when, in epoch seconds.
            kind: "reminder" or "timer".

        Returns:
            Its id.
        """
        return self.write(lambda db: int(cast(int, db.execute(
            "INSERT INTO reminders (text, due, kind, ts) VALUES (?, ?, ?, ?)",
            (text, due, kind, time.time())).lastrowid)))

    def pending_reminders(self) -> list[ReminderRow]:
        """Reminders and timers not yet done, soonest first.

        Returns:
            The rows.
        """
        with self.reading() as db:
            rows = db.execute("SELECT id, text, due, kind, ts FROM reminders WHERE done = 0 ORDER BY due").fetchall()
        return [ReminderRow(**row) for row in rows]

    def due_reminders(self, now: float) -> list[ReminderRow]:
        """Reminders and timers due by now and not yet done, soonest first.

        Args:
            now: epoch seconds.

        Returns:
            The rows.
        """
        with self.reading() as db:
            rows = db.execute("SELECT id, text, due, kind, ts FROM reminders WHERE done = 0 AND due <= ? "
                              "ORDER BY due", (now,)).fetchall()
        return [ReminderRow(**row) for row in rows]

    def move_reminder(self, reminder_id: int, due: float) -> bool:
        """Give a pending reminder or timer another due time (time added to a timer, or taken off).

        Args:
            reminder_id: its id.
            due: when it is due now, in epoch seconds.

        Returns:
            Whether a pending one was moved.
        """
        return self.write(lambda db: db.execute("UPDATE reminders SET due = ? WHERE id = ? AND done = 0",
                                                (due, reminder_id)).rowcount > 0)

    def finish_reminder(self, reminder_id: int) -> bool:
        """Mark a reminder or timer done (said, or cancelled).

        Args:
            reminder_id: its id.

        Returns:
            Whether a pending one was marked.
        """
        return self.write(lambda db: db.execute("UPDATE reminders SET done = 1 WHERE id = ? AND done = 0",
                                                (reminder_id,)).rowcount > 0)

    # ------------------------------------------------------------------------------------------------ jobs
    def add_job(self, name: str, schedule: str, prompt: str, deliver: list[str], next_run: float | None) -> int:
        """Save a scheduled job.

        Args:
            name: its name.
            schedule: its cron expression.
            prompt: what Orion is asked each time.
            deliver: where its result goes ("speak", "hud", "discord").
            next_run: when it runs next (epoch seconds), None when it never will.

        Returns:
            Its id.
        """
        return self.write(lambda db: int(cast(int, db.execute(
            "INSERT INTO jobs (name, schedule, prompt, deliver, created, next_run) VALUES (?, ?, ?, ?, ?, ?)",
            (name, schedule, prompt, json.dumps(deliver), time.time(), next_run)).lastrowid)))

    def update_job(self, job_id: int, **fields: object) -> bool:
        """Change some of a job's fields (name, schedule, prompt, deliver, enabled, next_run, last_run, last_status,
        last_text).

        Args:
            job_id: the job.
            **fields: the new values.

        Returns:
            Whether the job exists.

        Raises:
            ValueError: an unknown field.
        """
        allowed = {"name", "schedule", "prompt", "deliver", "enabled", "next_run", "last_run", "last_status",
                   "last_text"}
        if unknown := set(fields) - allowed:
            raise ValueError(f"jobs have no {', '.join(sorted(unknown))}")
        if not fields:
            return self.job(job_id) is not None
        values = {k: (json.dumps(v) if k == "deliver" else int(bool(v)) if k == "enabled" else v)
                  for k, v in fields.items()}
        assignments = ", ".join(f"{key} = ?" for key in values)
        return self.write(lambda db: db.execute(f"UPDATE jobs SET {assignments} WHERE id = ?",
                                                (*values.values(), job_id)).rowcount > 0)

    def delete_job(self, job_id: int) -> bool:
        """Delete a job.

        Args:
            job_id: the job.

        Returns:
            Whether it existed.
        """
        return self.write(lambda db: db.execute("DELETE FROM jobs WHERE id = ?", (job_id,)).rowcount > 0)

    # ------------------------------------------------------------------------------------------------ activity
    def log_activity(self, row: ActivityRow) -> int:
        """Add one entry to the activity log (its id is ignored: the log numbers it).

        Returns:
            Its id.
        """
        values = (row["at"], row["kind"], row["source"], row["actor"], row["connector"], row["tool"], row["title"],
                  row["detail"], row["outcome"], row["turn"], row["ms"])
        return self.write(lambda db: int(db.execute(
            "INSERT INTO activity (at, kind, source, actor, connector, tool, title, detail, outcome, turn, ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", values).lastrowid or 0))

    def activity(self, *, before: int | None = None, limit: int = 50, kinds: Collection[str] = (),
                 sources: Collection[str] = (), text: str = "") -> list[ActivityRow]:
        """Entries of the activity log, newest first.

        Args:
            before: only entries older than this id (the next page); None for the newest.
            limit: at most this many.
            kinds: only these kinds (empty: all).
            sources: only these sources (empty: all).
            text: only entries whose title, detail, tool or connector contain it (case-insensitive; "" all).

        Returns:
            The entries.
        """
        where, params = ["1"], []
        if before is not None:
            where.append("id < ?")
            params.append(before)
        if kinds:
            where.append(f"kind IN ({', '.join('?' * len(kinds))})")
            params.extend(kinds)
        if sources:
            where.append(f"source IN ({', '.join('?' * len(sources))})")
            params.extend(sources)
        if text:
            where.append("(instr(lower(title), ?) OR instr(lower(detail), ?) OR instr(lower(tool), ?) "
                         "OR instr(lower(connector), ?))")
            params.extend([text.lower()] * 4)
        with self.reading() as db:
            rows = db.execute(f"SELECT {_ACTIVITY_COLUMNS} FROM activity WHERE {' AND '.join(where)} "
                              "ORDER BY id DESC LIMIT ?", (*params, max(1, limit))).fetchall()
        return [ActivityRow(**dict(row)) for row in rows]  # type: ignore[typeddict-item]

    def prune_activity(self, older_than: float) -> int:
        """Forget the activity entries from before a time (epoch seconds); returns how many went."""
        return self.write(lambda db: db.execute("DELETE FROM activity WHERE at < ?", (older_than,)).rowcount)

    def job(self, job_id: int) -> JobRow | None:
        """One job, None when there is none with this id."""
        with self.reading() as db:
            row = db.execute(f"SELECT {_JOB_COLUMNS} FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _job(row) if row is not None else None

    def jobs(self) -> list[JobRow]:
        """Every job, in the order they were made."""
        with self.reading() as db:
            rows = db.execute(f"SELECT {_JOB_COLUMNS} FROM jobs ORDER BY id").fetchall()
        return [_job(row) for row in rows]

    def due_jobs(self, now: float) -> list[JobRow]:
        """The enabled jobs due by now, soonest first."""
        with self.reading() as db:
            rows = db.execute(f"SELECT {_JOB_COLUMNS} FROM jobs WHERE enabled = 1 AND next_run IS NOT NULL "
                              "AND next_run <= ? ORDER BY next_run", (now,)).fetchall()
        return [_job(row) for row in rows]

    def next_job_run(self) -> float | None:
        """When the next enabled job runs (epoch seconds), None when none will."""
        with self.reading() as db:
            row = db.execute("SELECT MIN(next_run) FROM jobs WHERE enabled = 1 AND next_run IS NOT NULL").fetchone()
        return float(row[0]) if row is not None and row[0] is not None else None

    def add_todo(self, text: str) -> int:
        """Add a to-do.

        Args:
            text: what to do.

        Returns:
            Its id.
        """
        return self.write(lambda db: int(cast(int, db.execute(
            "INSERT INTO todos (text, ts) VALUES (?, ?)", (text, time.time())).lastrowid)))

    def open_todos(self) -> list[TodoRow]:
        """The to-dos not yet done, oldest first.

        Returns:
            The rows.
        """
        with self.reading() as db:
            rows = db.execute("SELECT id, text, ts FROM todos WHERE done = 0 ORDER BY ts, id").fetchall()
        return [TodoRow(**row) for row in rows]

    def set_todo_done(self, todo_id: int, done: bool = True) -> bool:
        """Tick a to-do off, or back on.

        Args:
            todo_id: its id.
            done: True to tick it off.

        Returns:
            Whether such a to-do exists.
        """
        return self.write(lambda db: db.execute("UPDATE todos SET done = ? WHERE id = ?",
                                                (int(done), todo_id)).rowcount > 0)

    def delete_todo(self, todo_id: int) -> bool:
        """Delete a to-do.

        Args:
            todo_id: its id.

        Returns:
            Whether it existed.
        """
        return self.write(lambda db: db.execute("DELETE FROM todos WHERE id = ?", (todo_id,)).rowcount > 0)

    def get_setting(self, key: str, default: str = "") -> str:
        """A small stored value (the index's progress, the review's mark, the home city learned, ...).

        Args:
            key: its name.
            default: what to return when it is not set.

        Returns:
            The value.
        """
        with self.reading() as db:
            row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return default if row is None or row[0] is None else str(row[0])

    def set_setting(self, key: str, value: str) -> None:
        """Store a small value.

        Args:
            key: its name.
            value: the value.
        """
        self.write(lambda db: db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value)))

    # ------------------------------------------------------------------------------------------------ spend
    def log_spend(self, row: SpendRow) -> int:
        """Record what one paid request cost.

        Returns:
            Its id.
        """
        values = (row["at"], row["provider"], row["model"], row["purpose"], row["prompt_tokens"],
                  row["completion_tokens"], row["cost"])
        return self.write(lambda db: int(db.execute(
            "INSERT INTO spend (at, provider, model, purpose, prompt_tokens, completion_tokens, cost) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", values).lastrowid or 0))

    def spend_total(self, since: float) -> float:
        """What paid requests cost since a moment (epoch seconds), in US dollars."""
        with self.reading() as db:
            return float(db.execute("SELECT COALESCE(SUM(cost), 0) FROM spend WHERE at >= ?", (since,)).fetchone()[0])

    def spend_rows(self, since: float) -> list[tuple[float, str, float]]:
        """Each paid request since a moment (epoch seconds), oldest first.

        Returns:
            (at, purpose, cost) for each.
        """
        with self.reading() as db:
            rows = db.execute("SELECT at, purpose, cost FROM spend WHERE at >= ? ORDER BY at", (since,)).fetchall()
        return [(float(at), str(purpose), float(cost)) for at, purpose, cost in rows]

    # ------------------------------------------------------------------------------------------------ benchmarks
    def add_bench_run(self, *, at: float, suite: str, brains: Sequence[str], results: Mapping[str, JSON]) -> int:
        """Record a benchmark run that starts now (status "running").

        Args:
            at: when it starts (epoch seconds).
            suite: "builtin" or "history".
            brains: the brains it scores.
            results: its plan and empty scores.

        Returns:
            Its id.
        """
        values = (at, suite, json.dumps(list(brains)), json.dumps(results, ensure_ascii=False))
        return self.write(lambda db: int(db.execute(
            "INSERT INTO bench_runs (at, suite, brains, status, results) VALUES (?, ?, ?, 'running', ?)",
            values).lastrowid or 0))

    def save_bench_run(self, run_id: int, *, status: str, results: Mapping[str, JSON]) -> bool:
        """Record how a benchmark run ended.

        Args:
            run_id: the run.
            status: "done", "failed" (it went wrong) or "stopped" (you ended it).
            results: its plan, scores and error.

        Returns:
            Whether the run exists.
        """
        values = (status, json.dumps(results, ensure_ascii=False), run_id)
        return self.write(lambda db: db.execute("UPDATE bench_runs SET status = ?, results = ? WHERE id = ?",
                                                values).rowcount > 0)

    def bench_runs(self, limit: int) -> list[BenchRow]:
        """The latest benchmark runs, newest first.

        Args:
            limit: how many at most.

        Returns:
            The runs.
        """
        with self.reading() as db:
            rows = db.execute(f"SELECT {_BENCH_COLUMNS} FROM bench_runs ORDER BY id DESC LIMIT ?",
                              (max(1, limit),)).fetchall()
        return [_bench(row) for row in rows]

    def bench_run(self, run_id: int) -> BenchRow | None:
        """One benchmark run, None when there is none with this id."""
        with self.reading() as db:
            row = db.execute(f"SELECT {_BENCH_COLUMNS} FROM bench_runs WHERE id = ?", (run_id,)).fetchone()
        return _bench(row) if row is not None else None

    def prune_bench_runs(self, keep: int) -> int:
        """Delete all but the latest ``keep`` benchmark runs; returns how many went."""
        return self.write(lambda db: db.execute(
            "DELETE FROM bench_runs WHERE id NOT IN (SELECT id FROM bench_runs ORDER BY id DESC LIMIT ?)",
            (max(1, keep),)).rowcount)

    def fail_unfinished_bench_runs(self, error: str) -> int:
        """Mark the benchmark runs still "running" (their Orion stopped mid-run) as failed, saying why.

        Args:
            error: why, in words (kept in the run's ``error``).

        Returns:
            How many there were.
        """
        return self.write(lambda db: db.execute(
            "UPDATE bench_runs SET status = 'failed', results = json_set(results, '$.error', ?) "
            "WHERE status = 'running'", (error,)).rowcount)

    def recent_requests(self, limit: int, sources: Collection[str]) -> list[tuple[MessageRow, list[StepRecord]]]:
        """The latest requests (user messages from these sources), each with the tool calls the brain made in its
        turn: what the history benchmark replays.

        Args:
            limit: how many requests at most.
            sources: where they came from ("voice", "hud", "discord").

        Returns:
            (request, its turn's brain tool calls in the order they ran), newest request first.
        """
        with self.reading() as db:
            rows = db.execute(f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE role = 'user' AND turn IS NOT NULL "
                              f"AND source IN (SELECT value FROM json_each(?)) ORDER BY id DESC LIMIT ?",
                              (json.dumps(list(sources)), max(1, limit))).fetchall()
            requests = [MessageRow(**row) for row in rows]
            steps: dict[str, list[StepRecord]] = {}
            # kind "memory" is a call of the memory tool that saved something
            for row in db.execute(f"SELECT {_STEP_COLUMNS} FROM steps WHERE kind IN ('tool', 'memory') AND "
                                  "source = 'brain' AND turn IN (SELECT value FROM json_each(?)) ORDER BY at",
                                  (json.dumps([request["turn"] for request in requests]),)):
                steps.setdefault(str(row["turn"]), []).append(_step(row))
        return [(request, steps.get(request["turn"], [])) for request in requests]

    # ------------------------------------------------------------------------------------------------ timings
    def log_timing(self, row: "Timing") -> None:
        """Keep one turn's timings (a second write for the turn updates it); beyond ``TIMINGS_KEPT`` turns the
        oldest go.

        Args:
            row: the timings (``orion.timing``).
        """
        values = (row["turn"], row["at"], row["source"], row["brain"], int(row["instant"]), row["heard_ms"],
                  row["think_ms"], row["tools_ms"], row["speak_ms"], row["total_ms"])

        def work(db: sqlite3.Connection) -> None:
            """Insert the row (a turn written twice keeps its row), then drop the oldest past the limit: rowids
            grow with each insert and a row is never replaced by a new one, so they have no gaps."""
            db.execute(_LOG_TIMING, values)
            db.execute("DELETE FROM timings WHERE rowid <= (SELECT MAX(rowid) FROM timings) - ?", (TIMINGS_KEPT,))

        self.write(work)

    def timings(self, limit: int) -> list["Timing"]:
        """The latest turns' timings, newest first.

        Args:
            limit: how many at most.

        Returns:
            The rows.
        """
        with self.reading() as db:
            rows = db.execute(f"SELECT {_TIMING_COLUMNS} FROM timings ORDER BY at DESC LIMIT ?",
                              (max(1, limit),)).fetchall()
        return [{"turn": row["turn"], "at": row["at"], "source": row["source"], "brain": row["brain"],
                 "instant": bool(row["instant"]), "heard_ms": row["heard_ms"], "think_ms": row["think_ms"],
                 "tools_ms": row["tools_ms"], "speak_ms": row["speak_ms"], "total_ms": row["total_ms"]}
                for row in rows]

    # ------------------------------------------------------------------------------------------------ memory metadata
    def meta_rows(self, keys: Collection[str]) -> dict[str, MetaRow]:
        """The metadata of these entries.

        Args:
            keys: entry keys.

        Returns:
            Key -> row, for the keys that have one.
        """
        with self.reading() as db:
            return {str(row["key"]): MetaRow(**row) for row in db.execute(
                f"SELECT {_META_COLUMNS} FROM mem_meta WHERE key IN (SELECT value FROM json_each(?))",
                (json.dumps(list(keys)),))}

    def meta_write(self, upserts: Sequence[MetaRow], superseded: Sequence[tuple[str, float, str | None, str]]) -> None:
        """Record new and changed entries and the ones they replaced, in one transaction.

        An upsert replaces a row's facts (kind, target, source, learned_at, text and the superseded fields) and keeps
        its usage (``last_used``, ``use_count``) unless the new row says more.

        Args:
            upserts: rows to insert or replace.
            superseded: (key, when, replaced by key or None, why) for each entry removed or replaced.
        """
        rows = [(r.key, r.kind, r.target, r.source, r.learned_at, r.text, r.superseded_at, r.superseded_by, r.why,
                 r.last_used, r.use_count) for r in upserts]
        marks = [(at, by, why, key) for key, at, by, why in superseded]

        def work(db: sqlite3.Connection) -> None:
            """Write the rows."""
            db.executemany(
                f"INSERT INTO mem_meta ({_META_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET kind = excluded.kind, target = excluded.target, "
                "source = excluded.source, learned_at = excluded.learned_at, text = excluded.text, "
                "superseded_at = excluded.superseded_at, superseded_by = excluded.superseded_by, why = excluded.why, "
                "last_used = MAX(COALESCE(last_used, excluded.last_used), COALESCE(excluded.last_used, last_used)), "
                "use_count = MAX(use_count, excluded.use_count)", rows)
            db.executemany("UPDATE mem_meta SET superseded_at = ?, superseded_by = ?, why = ? WHERE key = ?", marks)

        if rows or marks:
            self.write(work)

    def meta_changes(self, since: float, kinds: Collection[str]) -> list[MetaChange]:
        """What memory learned, replaced or removed since a moment (the per-turn change list).

        An entry added and removed again within the window is left out. An entry that replaced one that existed
        before the window is "~" and stands for both; one that only appeared is "+"; one removed without a
        listed replacement is "-".

        Args:
            since: epoch seconds (the conversation's start, or the last snapshot).
            kinds: the files to report (unverified is left out unless named).

        Returns:
            The changes, oldest first.
        """
        with self.reading() as db:
            rows = [MetaRow(**row) for row in db.execute(
                f"SELECT {_META_COLUMNS} FROM mem_meta WHERE kind IN (SELECT value FROM json_each(?)) "
                "AND (learned_at > ? OR superseded_at > ?)", (json.dumps(list(kinds)), since, since))]
        current = {row.key for row in rows if row.superseded_at is None}
        replaced = {row.superseded_by for row in rows
                    if row.superseded_by and row.learned_at <= since and (row.superseded_at or 0.0) > since}
        changes: list[MetaChange] = []
        for row in rows:
            if row.superseded_at is None:
                if row.learned_at > since:
                    op: Literal["+", "~", "-"] = "~" if row.key in replaced else "+"
                    changes.append(MetaChange(op, row.kind, row.key, row.text, row.learned_at, row.source))
            elif row.learned_at <= since < row.superseded_at and row.superseded_by not in current:
                changes.append(MetaChange("-", row.kind, row.key, row.text, row.superseded_at, row.source))
        return sorted(changes, key=lambda change: change.ts)

    def meta_forget(self, keys: Collection[str]) -> None:
        """Delete everything recorded about entries the user forgot (where and when they were learned, their use).

        Args:
            keys: entry keys.
        """
        if keys:
            self.write(lambda db: db.execute("DELETE FROM mem_meta WHERE key IN (SELECT value FROM json_each(?))",
                                             (json.dumps(list(keys)),)))

    def meta_touch(self, keys: Collection[str], now: float) -> None:
        """Count a use of these entries (recalled or found): ``last_used`` = now, ``use_count`` + 1.

        Args:
            keys: entry keys.
            now: epoch seconds.
        """
        if keys:
            self.write(lambda db: db.execute("UPDATE mem_meta SET last_used = ?, use_count = use_count + 1 WHERE key "
                                             "IN (SELECT value FROM json_each(?))", (now, json.dumps(list(keys)))))

    # ------------------------------------------------------------------------------------------------ internals
    def _connect(self, *, query_only: bool) -> sqlite3.Connection:
        """A connection with the PRAGMAs every connection needs (autocommit mode: transactions are explicit)."""
        db = sqlite3.connect(self.path, timeout=self._timeout_s, isolation_level=None, check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous = NORMAL")  # with WAL: safe, and a commit doesn't wait for the disk
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA temp_store = MEMORY")
        if query_only:
            db.execute("PRAGMA query_only = 1")
        return db

    def _open_writer(self) -> sqlite3.Connection:
        """On the writer thread: open the write connection and switch the file to write-ahead logging."""
        self._writer_ident = threading.get_ident()
        db = self._connect(query_only=False)
        _retry_busy(lambda: db.execute("PRAGMA journal_mode=WAL").fetchall())
        return db

    def _on_writer[T](self, work: Callable[[sqlite3.Connection], T]) -> T:
        """Run ``work`` on the write connection: on the writer thread (role main) or under the lock."""
        if self._closed:
            raise OrionError("The database is closed.")
        if self.role != "main":
            with self._lock:
                return work(self._db)
        if threading.get_ident() == self._writer_ident:
            return work(self._db)
        return self._pool.submit(work, self._db).result()

    @staticmethod
    def _shutdown(db: sqlite3.Connection) -> None:
        """Let SQLite refresh its planner statistics, then close (errors at this point are not worth raising)."""
        with contextlib.suppress(sqlite3.Error):
            db.execute("PRAGMA optimize")
        db.close()


# ---------------------------------------------------------------------------------------------------- helpers
def _retry_busy[T](work: Callable[[], T]) -> T:
    """Run ``work`` again while another process holds the file: on a brand-new file every process starting at once
    tries to switch it to write-ahead logging, and SQLite doesn't wait for that."""
    deadline = time.monotonic() + BUSY_RETRY_S
    while True:
        try:
            return work()
        except sqlite3.OperationalError as e:
            code = getattr(e, "sqlite_errorcode", 0) & 0xFF
            if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or time.monotonic() >= deadline:
                raise
            time.sleep(BUSY_POLL_S)


def _transaction[T](db: sqlite3.Connection, work: Callable[[sqlite3.Connection], T]) -> T:
    """Run ``work`` between ``BEGIN IMMEDIATE`` and ``COMMIT`` (rolled back if it raises); join a running one."""
    if db.in_transaction:
        return work(db)
    db.execute("BEGIN IMMEDIATE")
    try:
        result = work(db)
    except BaseException:
        db.execute("ROLLBACK")
        raise
    db.execute("COMMIT")
    return result


def _log_failure(future: Future[object]) -> None:
    """Log a queued store call that failed (fire-and-forget callers never look at the future)."""
    if not future.cancelled() and (error := future.exception()) is not None:
        log.error("a database call failed: %s", error, exc_info=error)


def _insert_conv(db: sqlite3.Connection, conv_id: int | None, started: float, reason: ConvReason) -> int:
    """Insert a conversation row (``conv_id`` None lets SQLite choose) and return its id."""
    cursor = db.execute("INSERT INTO conversations (id, started, reason) VALUES (?, ?, ?)", (conv_id, started, reason))
    return conv_id if conv_id is not None else int(cast(int, cursor.lastrowid))


def _bench(row: sqlite3.Row) -> BenchRow:
    """A bench_runs row as a BenchRow (its brains and results decoded)."""
    brains, results = json.loads(row["brains"] or "[]"), json.loads(row["results"] or "{}")
    return BenchRow(id=int(row["id"]), at=float(row["at"]), suite=str(row["suite"]),
                    brains=[str(brain) for brain in brains] if isinstance(brains, list) else [],
                    status=str(row["status"]), results=results if isinstance(results, dict) else {})


def _step(row: sqlite3.Row) -> StepRecord:
    """A steps row as the StepRecord the HUD receives (JSON columns decoded, "" for an external turn)."""
    memory = json.loads(row["memory"]) if row["memory"] else None
    return StepRecord(id=str(row["id"]), turn=str(row["turn"] or ""), kind=cast(StepKind, row["kind"]),
                      name=str(row["name"]), title=str(row["title"]), server=str(row["server"]),
                      args=cast(dict[str, JSON], json.loads(row["args"] or "{}")),
                      source=cast(Literal["brain", "app"], row["source"]), client=str(row["client"]),
                      at=float(row["at"]), ms=int(row["ms"]), ok=bool(row["ok"]), result=str(row["result"]),
                      taints=bool(row["taints"]), memory=cast(dict[str, JSON] | None, memory))
