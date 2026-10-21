"""Scheduled jobs: requests Orion runs on his own at the times you set ("every weekday at 7:30, brief me", "every
hour, tell me if the build is red").

A job is a name, a cron schedule (``minute hour day month weekday``, read with cronsim, in this machine's time
zone), the request to run, and where the result goes ("speak", "hud", "discord"). ``Jobs`` is the one place jobs
change: his MCP tools (``tools/automation.py``) and the HUD's REST endpoints both go through it, and every change is
published (``jobs`` events).

``JobRunner`` runs a job when it falls due (the runtime's scheduler asks it): on its own model (``jobs.provider``:
the conversation's brain, or a cheaper one), outside the conversation (``Brain.run_task``: no history, the
conversation untouched), with at most ``jobs.max_tool_rounds`` tool rounds and ``jobs.run_timeout_s``. Risky calls
after web content still wait for your yes, asked in the HUD and on Discord. The result is announced where the job says
(spoken, shown, sent), kept with the job and noted in the conversation, so "what did the briefing say?" works.
A job missed while Orion was off runs when he starts within ``jobs.catch_up_s``; later than that it waits for its
next time.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime
from typing import TYPE_CHECKING, Final

from orion.base import JSON, EventBus, LLMError, local_now, local_zone, new_id

if TYPE_CHECKING:
    from orion.brain import Brain
    from orion.config import Config
    from orion.llm import LLM
    from orion.memory.store import JobRow, Store

log = logging.getLogger("orion.jobs")

DELIVERIES: Final = ("speak", "hud", "discord")
NAME_CHARS: Final = 80
PROMPT_CHARS: Final = 4000
RESULT_CHARS: Final = 4000  # what is kept of a job's answer


class ScheduleError(ValueError):
    """A schedule that isn't a cron expression; the message says what is wrong."""


def parse_schedule(text: str) -> str:
    """A cron schedule, checked: five fields (minute hour day month weekday), as cronsim reads them.

    Args:
        text: what was given ("30 7 * * 1-5").

    Returns:
        The schedule, its spaces tidied.

    Raises:
        ScheduleError: it isn't one.
    """
    from cronsim import CronSim, CronSimError

    fields = text.split()
    if len(fields) != 5:
        raise ScheduleError(f"a schedule has five fields (minute hour day month weekday), e.g. '30 7 * * 1-5'; "
                            f"'{text.strip()}' has {len(fields)}")
    schedule = " ".join(fields)
    try:
        CronSim(schedule, local_now())
    except CronSimError as e:
        raise ScheduleError(f"'{schedule}' isn't a schedule: {e}") from e
    return schedule


def explain(schedule: str) -> str:
    """A schedule in English ("At 07:30 on Monday through Friday").

    Args:
        schedule: a checked cron schedule.

    Returns:
        The explanation.
    """
    from cronsim import CronSim

    return str(CronSim(schedule, local_now()).explain())


def next_run(schedule: str, after: float) -> float | None:
    """When a schedule fires next after a moment, in this machine's time zone.

    Args:
        schedule: a checked cron schedule.
        after: epoch seconds.

    Returns:
        Epoch seconds, None when it never will (a date that doesn't exist).
    """
    from cronsim import CronSim

    start = datetime.fromtimestamp(after, local_zone())
    try:
        return next(iter(CronSim(schedule, start))).timestamp()
    except StopIteration:
        return None


def clean_deliver(deliver: Iterable[object] | None, default: Iterable[str]) -> list[str]:
    """Where a result goes: the known ways, in their order; the default when none is given."""
    if deliver is None:
        return [d for d in DELIVERIES if d in set(default)]
    given = {str(d) for d in deliver}
    if unknown := given - set(DELIVERIES):
        raise ValueError(f"a result can go to {', '.join(DELIVERIES)}; not to {', '.join(sorted(unknown))}")
    return [d for d in DELIVERIES if d in given]


def view(job: "JobRow") -> dict[str, JSON]:
    """A job as the HUD and the tools show it: its row, with its schedule in English."""
    try:
        said = explain(job["schedule"])
    except Exception:  # noqa: BLE001 (a schedule saved by hand that cronsim can't read: shown as it is)
        said = job["schedule"]
    return {**job, "explain": said}  # type: ignore[dict-item]


class Jobs:
    """The jobs (see the module docstring). Use it on the event loop; the database work is quick."""

    def __init__(self, cfg: "Config", store: "Store", bus: EventBus) -> None:
        """Hold the store.

        Args:
            cfg: the live settings (``jobs.deliver`` for a new job's default).
            store: the database.
            bus: the HUD's event bus.
        """
        self.cfg = cfg
        self.store = store
        self.bus = bus
        self.changed: Callable[[], None] = lambda: None  # the scheduler looks again (set by the runtime)

    def all(self) -> list[dict[str, JSON]]:
        """Every job, as shown."""
        return [view(job) for job in self.store.jobs()]

    def find(self, ref: object) -> "JobRow":
        """A job by its number ("3", "#3") or its name (any case).

        Raises:
            LookupError: there is none.
        """
        text = str(ref).strip().lstrip("#")
        if text.isdigit() and (job := self.store.job(int(text))) is not None:
            return job
        for job in self.store.jobs():
            if job["name"].casefold() == text.casefold():
                return job
        raise LookupError(f"there's no job called {ref}")

    def create(self, name: str, schedule: str, prompt: str, deliver: Iterable[object] | None = None,
               enabled: bool = True) -> dict[str, JSON]:
        """Make a job.

        Args:
            name: its name.
            schedule: a cron schedule.
            prompt: what Orion is asked each time.
            deliver: where its result goes (default ``jobs.deliver``).
            enabled: whether it runs.

        Returns:
            The job, as shown.

        Raises:
            ValueError: an empty name or request, a bad schedule or delivery.
        """
        name, prompt = name.strip()[:NAME_CHARS], prompt.strip()[:PROMPT_CHARS]
        if not name or not prompt:
            raise ValueError("a job needs a name and a request")
        checked = parse_schedule(schedule)
        ways = clean_deliver(deliver, self.cfg.jobs.deliver)
        job_id = self.store.add_job(name, checked, prompt, ways, next_run(checked, time.time()) if enabled else None)
        if not enabled:
            self.store.update_job(job_id, enabled=False)
        return self._done(job_id)

    def update(self, ref: object, *, name: str | None = None, schedule: str | None = None, prompt: str | None = None,
               deliver: Iterable[object] | None = None, enabled: bool | None = None) -> dict[str, JSON]:
        """Change a job: only what is given.

        Raises:
            LookupError: no such job.
            ValueError: an empty name or request, a bad schedule or delivery.
        """
        job = self.find(ref)
        changes: dict[str, object] = {}
        if name is not None:
            if not name.strip():
                raise ValueError("a job needs a name")
            changes["name"] = name.strip()[:NAME_CHARS]
        if prompt is not None:
            if not prompt.strip():
                raise ValueError("a job needs a request")
            changes["prompt"] = prompt.strip()[:PROMPT_CHARS]
        if deliver is not None:
            changes["deliver"] = clean_deliver(deliver, ())
        schedule = parse_schedule(schedule) if schedule is not None else job["schedule"]
        if schedule != job["schedule"]:
            changes["schedule"] = schedule
        on = job["enabled"] if enabled is None else enabled
        if enabled is not None:
            changes["enabled"] = enabled
        if "schedule" in changes or enabled is not None:
            changes["next_run"] = next_run(schedule, time.time()) if on else None
        self.store.update_job(job["id"], **changes)
        return self._done(job["id"])

    def delete(self, ref: object) -> str:
        """Delete a job; returns its name.

        Raises:
            LookupError: no such job.
        """
        job = self.find(ref)
        self.store.delete_job(job["id"])
        self._done(None)
        return job["name"]

    def finished(self, job_id: int, *, ok: bool, text: str, at: float) -> None:
        """A run ended: keep its result and when it runs next.

        Args:
            job_id: the job.
            ok: whether it succeeded.
            text: its answer, or why it failed.
            at: when it started (epoch seconds).
        """
        self.store.update_job(job_id, last_run=at, last_status="ok" if ok else "error", last_text=text[:RESULT_CHARS])
        self._done(None)

    def advance(self, job: "JobRow", now: float) -> None:
        """A job falls due: its next time is set before it runs, so it never runs twice for one."""
        self.store.update_job(job["id"], next_run=next_run(job["schedule"], now))

    def _done(self, job_id: int | None) -> dict[str, JSON]:
        """Publish the jobs and wake the scheduler; return one job as shown."""
        self.bus.publish("jobs", items=self.all())
        self.changed()
        if job_id is None:
            return {}
        job = self.store.job(job_id)
        if job is None:
            raise LookupError(f"job {job_id} is gone")
        return view(job)


class JobRunner:
    """Runs jobs (see the module docstring)."""

    def __init__(self, cfg: "Config", jobs: Jobs, brain: "Brain", bus: EventBus, *,
                 make_llm: Callable[[str], "LLM"], announce: Callable[[str, str, bool], None],
                 send: Callable[[str, str], Awaitable[str]] | None = None) -> None:
        """Prepare; nothing runs until a job is due.

        Args:
            cfg: the live settings (``jobs.*``).
            jobs: the jobs.
            brain: runs the request (``Brain.run_task``).
            bus: the HUD's event bus.
            make_llm: builds a model for a provider (each job run gets its own; the conversation's is never used).
            announce: says or shows a result (text, title, spoken).
            send: sends a result to Discord (title, text), returning what happened; None when there is no Discord.
        """
        self.cfg = cfg
        self.jobs = jobs
        self.brain = brain
        self.bus = bus
        self.make_llm = make_llm
        self.announce = announce
        self.send = send
        self.running: set[int] = set()

    async def run(self, job: "JobRow", *, manual: bool = False) -> None:
        """Run a job now and deliver its result (a job already running isn't started twice).

        Args:
            job: the job.
            manual: asked for now (run now), not because it fell due.
        """
        if job["id"] in self.running:
            return
        self.running.add(job["id"])
        started = time.time()
        self.bus.publish("job_run", id=job["id"], name=job["name"], phase="start", manual=manual)
        cfg = self.cfg.jobs
        provider = self.cfg.brain.provider if cfg.provider == "brain" else cfg.provider
        llm = self.make_llm(provider)
        ok, text = False, ""
        try:
            request = self.cfg.prompts.job_request.format(name=job["name"], prompt=job["prompt"],
                                                          when=explain(job["schedule"]))
            async with asyncio.timeout(cfg.run_timeout_s):
                text, _ = await self.brain.run_task(request, llm=llm, turn=f"job-{job['id']}-{new_id()[:6]}",
                                                    source="job", limit=cfg.max_tool_rounds)
            ok = bool(text)
            text = text or "(no answer)"
        except TimeoutError:
            text = f"It didn't finish within {cfg.run_timeout_s:g} seconds."
        except LLMError as e:
            text = f"The model couldn't answer: {e}"
        except Exception as e:
            log.exception("job %s failed", job["name"])
            text = f"It failed ({type(e).__name__}: {e})."
        finally:
            self.running.discard(job["id"])
            await llm.close()
        log.info("job %s %s in %.1f s", job["name"], "done" if ok else "failed", time.time() - started)
        self.jobs.finished(job["id"], ok=ok, text=text, at=started)
        self.bus.publish("job_run", id=job["id"], name=job["name"], phase="end", ok=ok, text=text[:RESULT_CHARS])
        await self._deliver(job, text if ok else f"The job {job['name']} failed: {text}")

    async def _deliver(self, job: "JobRow", text: str) -> None:
        """Send a result where the job says."""
        ways = set(job["deliver"])
        if "speak" in ways or "hud" in ways:
            self.announce(text, job["name"], "speak" in ways)
        if "discord" in ways:
            if self.send is None:
                log.info("job %s: no Discord to send its result to", job["name"])
            else:
                said = await self.send(job["name"], text)
                if said.startswith("Error"):
                    log.warning("job %s: Discord: %s", job["name"], said)
