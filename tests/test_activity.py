"""activity: entries are redacted, written in the background, published with their id, filtered and pruned."""

import time
from types import SimpleNamespace

import pytest

from orion.activity import Activity
from orion.memory.store import Store

MEMORY = SimpleNamespace(db_timeout_s=5)


@pytest.fixture
def store(tmp_path):
    db = Store(tmp_path / "orion.db", MEMORY, role="main")
    db.ensure_schema()
    yield db
    db.close()


def _wait(store: Store) -> None:
    """Until the writer thread has done everything queued so far."""
    store.submit(lambda: None).result(timeout=5)


def test_entries_are_redacted_published_and_listed_newest_first(store) -> None:
    events: list[tuple[str, dict]] = []
    log = Activity(store, lambda kind, **data: events.append((kind, data)),
                   lambda text: text.replace("hunter2", "[secret]"))
    log.record("tool", source="discord", title="Ran a shell command", tool="run_command", connector="system",
               detail={"command": "echo hunter2"}, outcome="ok", actor="the brain", ms=12)
    log.record("ask", source="hud", title="Asked before deleting files", outcome="declined", actor="you, in the HUD")
    _wait(store)
    rows = log.recent()
    assert [r["kind"] for r in rows] == ["ask", "tool"]
    assert rows[1]["detail"] == "command: echo [secret]" and rows[1]["ms"] == 12
    for _ in range(50):  # the event follows the write
        if len(events) == 2:
            break
        time.sleep(0.01)
    assert [e[1]["item"]["id"] for e in events] == [rows[1]["id"], rows[0]["id"]]
    assert "hunter2" not in repr(events)


def test_filters_pages_and_prunes(store) -> None:
    log = Activity(store, lambda *_a, **_k: None, lambda text: text)
    for i in range(5):
        log.record("job", source="job", title=f"Ran job {i}", outcome="ok")
    log.record("alert", source="watcher", title="The battery is low")
    _wait(store)
    assert [r["title"] for r in log.recent(kinds=["alert"])] == ["The battery is low"]
    assert len(log.recent(sources=["job"], limit=3)) == 3
    newest = log.recent(limit=2)
    assert [r["title"] for r in log.recent(before=newest[-1]["id"], limit=2)] == ["Ran job 3", "Ran job 2"]
    assert [r["title"] for r in log.recent(text="JOB 4")] == ["Ran job 4"]
    assert log.prune(-1) == 6 and log.recent() == []  # everything is older than "a day in the future"


def test_unknown_kinds_and_sources_are_refused(store) -> None:
    log = Activity(store, lambda *_a, **_k: None, lambda text: text)
    with pytest.raises(ValueError):
        log.record("hack", source="hud", title="x")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        log.record("tool", source="somewhere", title="x")
