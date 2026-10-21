"""memory/store: the schema of a new file, the HUD history with its operations, memory changes."""

import contextlib
import sqlite3
from collections.abc import Iterator
from types import SimpleNamespace

import pytest

from orion.base import OrionError, StepRecord
from orion.memory.store import CURRENT_VERSION, SCHEMA, MetaRow, Store

MEMORY = SimpleNamespace(db_timeout_s=5)
STEP = StepRecord(id="op1", turn="t1", kind="tool", name="open", title="Open", server="orion-tools",
                  args={"target": "firefox"}, source="brain", client="", at=11.0, ms=40, ok=True, result="Opened.",
                  taints=False, memory=None)


@pytest.fixture
def store(tmp_path) -> Iterator[Store]:
    """A new database for Orion himself (the writer thread), with the current schema."""
    db = Store(tmp_path / "new.db", MEMORY, role="main")
    db.ensure_schema()
    yield db
    db.close()


def test_only_a_new_file_gets_the_schema(tmp_path, store) -> None:
    """A new file is created at the current version; an older layout (no longer upgraded) or a newer Orion's is
    refused (version 3 is upgraded: tests/test_jobs.py)."""
    with store.reading() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == CURRENT_VERSION
    for version, says in ((0, "too old"), (2, "too old"), (CURRENT_VERSION + 1, "newer")):
        path = tmp_path / f"v{version}.db"
        with contextlib.closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, content TEXT)")
            db.execute(f"PRAGMA user_version = {version}")
        other = Store(path, MEMORY, role="tools")
        with pytest.raises(OrionError, match=says):
            other.ensure_schema()
        other.close()


def test_a_version_5_database_from_before_stopped_runs_learns_that_status(tmp_path) -> None:
    """A benchmark you stop is "stopped", not "failed". The table of a database made before that only knew the old three
    statuses (its CHECK stays whatever it was made with), so opening it rebuilds the table and keeps its runs."""
    path = tmp_path / "v5.db"
    old_check = "status IN ('running', 'done', 'failed', 'stopped')"
    with contextlib.closing(sqlite3.connect(path)) as db:
        for statement in SCHEMA:
            db.execute(statement.replace(old_check, "status IN ('running', 'done', 'failed')"))
        db.execute("INSERT INTO bench_runs (at, suite, brains, status, results) VALUES (1.0, 'builtin', '[\"claude\"]', "
                   "'failed', '{\"error\": \"boom\"}')")
        db.execute("INSERT INTO bench_runs (at, suite, brains, status, results) VALUES (1.5, 'builtin', '[]', "
                   "'failed', '{\"error\": \"Stopped before the end.\"}')")  # how a stopped run used to be kept
        db.execute("PRAGMA user_version = 5")
        db.commit()
        with pytest.raises(sqlite3.IntegrityError):  # the bug: this database refuses what Orion now writes
            db.execute("UPDATE bench_runs SET status = 'stopped'")
    old = Store(path, MEMORY, role="main")
    old.ensure_schema()
    with old.reading() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == CURRENT_VERSION
        tables = db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'bench_runs%'").fetchall()
        assert [t[0] for t in tables] == ["bench_runs"]  # no leftover of the rebuild
    failed, was_stopped = old.bench_run(1), old.bench_run(2)
    assert failed is not None and (failed["status"], failed["results"]) == ("failed", {"error": "boom"})
    assert was_stopped is not None and (was_stopped["status"], was_stopped["results"]) == ("stopped", {"error": ""})
    assert old.save_bench_run(1, status="stopped", results={"error": ""})
    kept = old.bench_run(1)
    assert kept is not None and kept["status"] == "stopped" and kept["brains"] == ["claude"]
    assert old.add_bench_run(at=2.0, suite="history", brains=["ollama"], results={}) == 3
    with pytest.raises(sqlite3.IntegrityError):  # and the table still guards its statuses
        old.save_bench_run(3, status="exploded", results={})
    old.close()


def test_the_hud_history_brings_back_each_turns_operations(store) -> None:
    """Steps come back grouped by turn, other apps' ones apart, with their JSON decoded; taint and skills read."""
    conv = store.current_conv().id
    store.log_message("user", "Open Firefox.", source="hud", conv=conv, turn="t1", ts=10.0)
    store.log_message("assistant", "Done, sir.", source="hud", conv=conv, turn="t1", ts=12.0)
    store.log_step(STEP, conv)
    app = StepRecord({**STEP, "id": "op2", "turn": "", "kind": "memory", "source": "app", "client": "Claude Desktop",
                      "at": 11.5, "memory": {"id": "3fa2", "text": "Owns a yacht.", "profile": False}})
    store.log_step(app, conv)
    store.log_step(StepRecord({**STEP, "id": "op3", "name": "use_skill", "args": {"name": "focus"},
                               "taints": True}), conv)
    history = store.hud_history(conv, 40)
    assert [m["content"] for m in history.messages] == ["Open Firefox.", "Done, sir."]
    assert history.steps["t1"][0] == STEP and history.external == [app]
    assert store.tainted_turns({"t1", "t2"}) == {"t1"} and store.skill_last_used() == {"focus": 11.0}
    with pytest.raises(sqlite3.IntegrityError):  # the HUD's own calls are not stored
        store.log_step(StepRecord({**STEP, "id": "op4", "source": "hud"}), conv)


def test_memory_changes_since_a_moment(store) -> None:
    """A replacement is "~", a removal "-", a new entry "+"; unverified entries only when asked; usage is kept."""
    store.meta_write([MetaRow("old", "user", None, "brain", 5.0, "Lives in Paris."),
                      MetaRow("gone", "orion", None, "brain", 5.0, "Uses Chrome."),
                      MetaRow("web", "unverified", "user", "tainted", 15.0, "Owns a yacht.")], [])
    store.meta_write([MetaRow("new", "user", None, "brain", 20.0, "Lives in Athens.")],
                     [("old", 20.0, "new", "updated"), ("gone", 21.0, None, "removed")])
    store.meta_write([MetaRow("tea", "user", None, "hud", 22.0, "Likes tea.")], [])
    assert [(c.op, c.text) for c in store.meta_changes(10.0, {"user", "orion"})] == [
        ("~", "Lives in Athens."), ("-", "Uses Chrome."), ("+", "Likes tea.")]
    assert ("+", "Owns a yacht.") in [(c.op, c.text) for c in store.meta_changes(10.0, {"user", "unverified"})]
    store.meta_touch(["tea"], 30.0)
    store.meta_write([MetaRow("tea", "user", None, "hud", 22.0, "Likes tea.")], [])
    assert (store.meta_rows(["tea"])["tea"].use_count, store.meta_rows(["tea"])["tea"].last_used) == (1, 30.0)
