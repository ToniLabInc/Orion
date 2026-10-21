"""jobs: cron schedules checked and said in English, jobs made, changed and found, a version-3 database upgraded
with them, and a run: its own model, its result kept, delivered where the job says, and a failure reported."""

import asyncio
import contextlib
import sqlite3
import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from orion.base import EventBus, LLMError
from orion.config import default_config
from orion.jobs import JobRunner, Jobs, ScheduleError, explain, next_run, parse_schedule
from orion.memory.store import CURRENT_VERSION, JOBS_SCHEMA, SCHEMA, Store

MEMORY = SimpleNamespace(db_timeout_s=5)


@pytest.fixture
def store(tmp_path) -> Iterator[Store]:
    db = Store(tmp_path / "jobs.db", MEMORY, role="main")
    db.ensure_schema()
    yield db
    db.close()


def test_schedules_are_checked_and_said_in_english(monkeypatch: pytest.MonkeyPatch) -> None:
    assert parse_schedule("  30 7   * * 1-5 ") == "30 7 * * 1-5"
    assert explain("30 7 * * 1-5") == "At 07:30 on Monday through Friday"
    for bad in ("every day", "30 7 * *", "61 7 * * *", "* * * * * *"):
        with pytest.raises(ScheduleError):
            parse_schedule(bad)
    from zoneinfo import ZoneInfo

    monkeypatch.setattr("orion.jobs.local_zone", lambda: ZoneInfo("UTC"))
    saturday_noon = 1791633600.0  # 2026-10-10 12:00 UTC
    assert next_run("30 7 * * 1-5", saturday_noon) == 1791790200.0  # Monday 12 October, 07:30 UTC


def test_jobs_are_made_changed_found_and_published(store: Store) -> None:
    bus = EventBus()
    seen: list[dict[str, Any]] = []
    bus.subscribe(seen.append)
    woken: list[int] = []
    jobs = Jobs(default_config(), store, bus)
    jobs.changed = lambda: woken.append(1)
    job = jobs.create("Morning briefing", "30 7 * * 1-5", "Brief me.")
    assert job["deliver"] == ["speak", "hud"] and job["explain"] == "At 07:30 on Monday through Friday"
    assert job["enabled"] and job["next_run"] and job["next_run"] > time.time()
    assert jobs.find("#1")["name"] == jobs.find("morning BRIEFING")["name"] == "Morning briefing"
    paused = jobs.update("1", enabled=False, deliver=["discord"])
    assert not paused["enabled"] and paused["next_run"] is None and paused["deliver"] == ["discord"]
    moved = jobs.update(1, schedule="0 8 * * *", enabled=True)
    assert moved["schedule"] == "0 8 * * *" and moved["next_run"]
    with pytest.raises(ValueError, match="discord"):
        jobs.create("x", "0 8 * * *", "y", deliver=["pigeon"])
    with pytest.raises(LookupError):
        jobs.find("nope")
    assert jobs.delete("#1") == "Morning briefing" and jobs.all() == []
    assert [e["type"] for e in seen] == ["jobs"] * 4 and len(woken) == 4


def test_a_version_3_database_gains_the_jobs_table(tmp_path) -> None:
    path = tmp_path / "v3.db"
    with contextlib.closing(sqlite3.connect(path)) as db:
        for statement in SCHEMA:
            if statement not in JOBS_SCHEMA:
                db.execute(statement)
        db.execute("PRAGMA user_version = 3")
    old = Store(path, MEMORY, role="main")
    old.ensure_schema()
    with old.reading() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == CURRENT_VERSION
    assert old.add_job("x", "0 8 * * *", "y", ["hud"], None) == 1
    old.close()


class Brain:
    def __init__(self, reply: str = "", error: Exception | None = None) -> None:
        self.reply, self.error = reply, error
        self.asked: list[dict[str, Any]] = []

    async def run_task(self, text: str, **kw: Any) -> tuple[str, None]:
        self.asked.append({"text": text, **kw})
        if self.error is not None:
            raise self.error
        return self.reply, None


class Model:
    closed = False

    async def close(self) -> None:
        self.closed = True


def runner(store: Store, brain: Brain, cfg: Any = None) -> tuple[JobRunner, list[tuple[str, str, bool]], list[str]]:
    cfg = cfg or default_config()
    said: list[tuple[str, str, bool]] = []
    built: list[str] = []
    jobs = Jobs(cfg, store, EventBus())

    def make(provider: str) -> Model:
        built.append(provider)
        return Model()

    return JobRunner(cfg, jobs, brain, EventBus(), make_llm=make,  # type: ignore[arg-type]
                     announce=lambda text, title, spoken: said.append((text, title, spoken))), said, built


def test_a_run_answers_on_its_own_model_and_delivers_where_the_job_says(store: Store) -> None:
    cfg = default_config()
    cfg.jobs.provider = "openrouter"
    brain = Brain("Sunny, two meetings.")
    run, said, built = runner(store, brain, cfg)
    job_id = store.add_job("Briefing", "30 7 * * *", "Brief me.", ["hud"], None)
    asyncio.run(run.run(store.job(job_id)))  # type: ignore[arg-type]
    assert built == ["openrouter"] and said == [("Sunny, two meetings.", "Briefing", False)]
    assert "Brief me." in brain.asked[0]["text"] and brain.asked[0]["source"] == "job"
    assert brain.asked[0]["limit"] == cfg.jobs.max_tool_rounds
    job = store.job(job_id)
    assert job is not None and job["last_status"] == "ok" and job["last_text"] == "Sunny, two meetings."


def test_a_failed_run_is_kept_and_said(store: Store) -> None:
    run, said, built = runner(store, Brain(error=LLMError("Usage limit reached.", "limit")))
    job_id = store.add_job("Check", "0 * * * *", "Check the build.", ["speak"], None)
    asyncio.run(run.run(store.job(job_id)))  # type: ignore[arg-type]
    assert built == ["claude"]  # jobs.provider "brain": the conversation's provider, its own instance
    job = store.job(job_id)
    assert job is not None and job["last_status"] == "error" and "Usage limit reached." in job["last_text"]
    assert said == [("The job Check failed: The model couldn't answer: Usage limit reached.", "Check", True)]
