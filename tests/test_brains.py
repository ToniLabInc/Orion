"""brains: his own tools for what OpenRouter cost and for benchmarks (the model never starts a benchmark itself: the
user is asked, with the plan in the question), the REST endpoints in the shapes the HUD is built against (a paid run
is refused without the user's yes), and `orion bench`. The benchmark service is a fake that plans for real; nothing
is run or paid."""

import asyncio
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from mcp import Client

from orion.base import CONTEXT_META, PROVENANCE_META, EventBus
from orion.bench import BenchBusy, Case, Expect, Plan, brain_plan
from orion.config import Config, default_config
from orion.confirm import Confirmer
from orion.llm import Usage
from orion.memory.store import Store
from orion.server import add_brains
from orion.spend import Spend
from orion.tools import CallInfo
from orion.tools.brains import asker, question_of, spend_text
from orion.tools.core import Controls, server

MEMORY = SimpleNamespace(db_timeout_s=5)
KEY = {"data": {"limit": 5, "limit_remaining": 3.25, "limit_reset": "monthly", "usage_daily": 0.5}}
REAL_CLIENT = httpx.Client  # before a test replaces it


class FakeBench:
    """The benchmark service as the tools, the REST layer and the command see it: it plans for real (so the numbers
    and the refusals are real) but starts nothing."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.started: list[tuple[list[str], str | None, int | None, str]] = []
        self.stopped: list[int] = []
        self.running: Any = None
        self.views: list[dict[str, Any]] = []  # what looking at run 7 answers, one per look
        self.looks = 0

    async def plan(self, brains: Sequence[str], suite: str | None = None, limit: int | None = None) -> Plan:
        cases = [Case(f"Request {n}", (Expect("set_timer", {}),), frozenset(), 2, {}, "Done.")
                 for n in range(limit or 3)]
        planned = [brain_plan(self.cfg, brain, cases, spent=0.0) for brain in dict.fromkeys(brains or ["claude"])]
        return Plan(suite or "builtin", tuple(cases), (), tuple(planned), {}, 1)  # type: ignore[arg-type]

    async def start(self, brains: Sequence[str], suite: str | None = None, limit: int | None = None, *,
                    source: str) -> dict[str, Any]:
        if self.running is not None:
            raise BenchBusy(f"Benchmark #{self.running.id} is running (1 of 5 cases): wait for it, or stop it.")
        self.started.append((list(brains), suite, limit, source))
        return {"id": 7}

    def stop(self, run_id: int) -> bool:
        self.stopped.append(run_id)
        return run_id in (7, getattr(self.running, "id", None))

    async def runs(self) -> list[dict[str, Any]]:
        return [view(3, "done", 1.0)] if self.views == [] else [self.views[-1]]

    async def run(self, run_id: int) -> dict[str, Any] | None:
        if run_id != 7 or not self.views:
            return view(run_id, "done", 1.0) if run_id == 3 else None
        found = self.views[min(self.looks, len(self.views) - 1)]
        self.looks += 1
        return found


def view(ident: int, status: str, progress: float, error: str = "") -> dict[str, Any]:
    return {"id": ident, "at": 1.0, "suite": "builtin", "brains": ["claude"], "status": status, "progress": progress,
            "results": [], "plan": {}, "error": error}


def rest_app(rt: Any) -> FastAPI:
    """The HUD's app with just the spend and benchmark endpoints, and the app's own handler for a refusal."""

    async def refused(request: Any, error: Exception) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, 404 if isinstance(error, LookupError) else 400)

    app = FastAPI(exception_handlers={ValueError: refused, LookupError: refused})
    add_brains(app, rt)
    return app


def test_who_asked_is_said_the_way_the_activity_log_says_it() -> None:
    assert asker(CallInfo("hud", None)) == "hud" and asker(CallInfo("app", None)) == "app"
    assert asker(CallInfo("brain", None, source="voice")) == "voice"
    assert asker(CallInfo("tainted", None, source="discord")) == "discord"
    assert asker(CallInfo("brain", None)) == "system"


def test_what_openrouter_cost_is_said_in_words() -> None:
    report = {"today": 0.5, "budget": 2.0, "currency": "USD",
              "by_day": [{"day": "2026-10-10", "cost": 0.25}, {"day": "2026-10-11", "cost": 0.5}],
              "by_purpose": [{"purpose": "job", "cost": 0.5}, {"purpose": "bench", "cost": 0.25}]}
    key = {"limit": 5.0, "limit_remaining": 3.25, "usage_daily": 0.5, "limit_reset": "monthly"}
    assert spend_text(report, key, key_set=True) == (
        "OpenRouter today: $0.50 of the $2.00 daily budget (the day starts at midnight on this PC). "
        "The last 2 days: $0.75 (jobs $0.50, benchmarks $0.25). "
        "The key: $3.25 left of its $5.00 limit, which starts again monthly; OpenRouter counts $0.50 today (its day "
        "is UTC's).")
    quiet = {"today": 0.0, "budget": 0.0, "currency": "USD", "by_day": [{"day": "2026-10-11", "cost": 0.0}],
             "by_purpose": []}
    unlimited = {"limit": None, "limit_remaining": None, "usage_daily": 0.0, "limit_reset": None}
    assert spend_text(quiet, unlimited, key_set=True) == (
        "OpenRouter today: $0.00, with no daily budget (the day starts at midnight on this PC). "
        "The last 1 days: $0.00. The key: no spending limit; OpenRouter counts $0.00 today (its day is UTC's).")
    assert spend_text(quiet, None, key_set=False).endswith("No OpenRouter key is set.")
    assert spend_text(quiet, None, key_set=True).endswith("The key's own limits couldn't be read.")


def test_the_question_before_a_benchmark_carries_its_plan() -> None:
    cfg = default_config()
    plan = asyncio.run(FakeBench(cfg).plan(["claude", "openrouter:anthropic/claude-haiku", "script"], None, 3))
    assert question_of(plan) == ("run a benchmark (claude: about 6 requests on your Claude plan; "
                                 "openrouter:anthropic/claude-haiku: about 6 paid requests to OpenRouter; "
                                 "script: free)")


class Sup:
    """The runtime's supervisor: what it is told to run, kept for the test to wait for."""

    def __init__(self) -> None:
        self.tasks: list[asyncio.Task[Any]] = []

    def spawn(self, job: Any, *, name: str) -> asyncio.Task[Any]:
        self.tasks.append(asyncio.get_running_loop().create_task(job, name=name))
        return self.tasks[-1]


class Room:
    """A running Orion's parts that his brain tools use, with a user who answers his questions as the test says."""

    def __init__(self, store: Store, key: bool = False) -> None:
        self.cfg = default_config()
        self.cfg.openrouter.api_key = "sk-t" if key else ""
        self.events: list[dict[str, Any]] = []
        self.bus = EventBus()
        self.bus.subscribe(self.events.append)
        self.confirmer = Confirmer(self.cfg, self.bus)
        self.yes: bool | None = True  # how the user answers a question at once; None: they haven't yet
        self.bus.subscribe(self.answer)
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json=KEY))
        self.spend = Spend(self.cfg, store, self.bus.publish, transport=transport)
        self.bench = FakeBench(self.cfg)
        self.supervisor = Sup()

    def answer(self, event: dict[str, Any]) -> None:
        if event["type"] == "confirm" and self.yes is not None:
            self.confirmer.answer(event["id"], self.yes, by="the test")

    async def call(self, tool: str, arguments: dict[str, Any] | None = None, *, who: str = "") -> tuple[bool, str]:
        """One call of one of his tools, as the hub makes it: who asks goes in the request's _meta."""
        meta: dict[str, Any] = {}
        if who:
            meta[PROVENANCE_META] = who.partition(":")[0]
            meta[CONTEXT_META] = {"turn": "t1", "source": who.partition(":")[2], "device": "hud-1"}
        async with Client(server(Controls(self))) as client:  # type: ignore[arg-type]
            result = await client.call_tool(tool, arguments or {}, meta=meta or None)
        return bool(result.is_error), result.content[0].text  # type: ignore[union-attr]


@pytest.fixture
def store(tmp_path):
    db = Store(tmp_path / "brains.db", MEMORY, role="main")
    db.ensure_schema()
    yield db
    db.close()


def test_what_openrouter_cost_is_a_tool_too(store: Store) -> None:
    room = Room(store, key=True)
    room.spend.record(model="m", purpose="job", usage=Usage(cost=0.5))
    room.cfg.openrouter.daily_budget_usd = 2.0

    async def run() -> tuple[bool, str]:
        return await room.call("openrouter_spend", {"days": 3})

    failed, said = asyncio.run(run())
    assert not failed and said.startswith("OpenRouter today: $0.50 of the $2.00 daily budget")
    assert "The last 3 days: $0.50 (jobs $0.50)." in said and "The key: $3.25 left of its $5.00 limit" in said


@pytest.mark.parametrize(("who", "yes", "started"), [
    ("brain:voice", True, [(["claude"], None, None, "voice")]),
    ("tainted:discord", True, [(["claude"], None, None, "discord")]),
    ("brain:voice", False, []),
])
def test_the_model_never_starts_a_benchmark_itself_the_user_is_asked_with_the_plan(
        store: Store, who: str, yes: bool, started: list) -> None:
    room = Room(store)
    room.yes = None  # the user hasn't answered yet

    async def run() -> tuple[bool, str]:
        out = await room.call("benchmark_brains", {"brains": ["claude"]}, who=who)
        await asyncio.sleep(0.01)
        assert room.bench.started == []  # the tool has answered, and the question waits: nothing has started
        (waiting,) = room.confirmer.pending()
        assert waiting["tool"] == "benchmark_brains"
        room.confirmer.answer(str(waiting["id"]), yes, by="the test")
        await asyncio.gather(*room.supervisor.tasks)
        return out

    failed, said = asyncio.run(run())
    assert not failed and "about 6 requests, at most 24" in said and "I've asked the user" in said
    (question,) = [e for e in room.events if e["type"] == "confirm"]
    assert question["text"] == "Sir, shall I run a benchmark (claude: about 6 requests on your Claude plan)?"
    assert (question["tool"], question["reason"], question["source_kind"], question["device"]) == (
        "benchmark_brains", "always", who.partition(":")[2], "hud-1")
    assert room.bench.started == started
    if not yes:
        assert any(e["type"] == "toast" and "you didn't say yes" in e["text"] for e in room.events)


def test_a_free_benchmark_is_asked_about_too_and_only_the_users_own_call_starts_directly(store: Store) -> None:
    """A model, or another AI app (a model too), is asked about even a free run; the user at the HUD is not."""
    room = Room(store)

    async def run() -> list[tuple[bool, str]]:
        out = [await room.call("benchmark_brains", {"brains": ["script"], "suite": "history", "limit": 2},
                               who="brain:hud")]
        await asyncio.gather(*room.supervisor.tasks)
        out.append(await room.call("benchmark_brains", {"brains": ["script"]}))  # another app: no _meta from the hub
        await asyncio.gather(*room.supervisor.tasks)
        out.append(await room.call("benchmark_brains", {"brains": ["claude"]}, who="hud:"))  # the user's own call
        return out

    asked, app, user = asyncio.run(run())
    assert "I've asked the user" in asked[1] and "free" in asked[1] and "I've asked the user" in app[1]
    assert room.bench.started == [(["script"], "history", 2, "hud"), (["script"], None, None, "app"),
                                  (["claude"], None, None, "hud")]
    assert not user[0] and user[1].startswith("Started benchmark #7 in the background")
    assert len([e for e in room.events if e["type"] == "confirm"]) == 2  # the user's own call asked nothing


def test_a_benchmark_that_cant_be_planned_or_started_says_why(store: Store) -> None:
    room = Room(store)
    room.bench.running = SimpleNamespace(id=3)

    async def run() -> list[tuple[bool, str]]:
        out = [await room.call("benchmark_brains", {"brains": ["claude"]}, who="brain:voice"),
               await room.call("benchmark_brains", {"brains": ["claude"]}),
               await room.call("benchmark_brains", {"action": "stop"}),
               await room.call("benchmark_brains", {"brains": ["gpt"]})]
        room.bench.running = None
        out.append(await room.call("benchmark_brains", {"action": "stop"}))
        return out

    busy_model, busy_app, stopping, unknown, none = asyncio.run(run())
    assert busy_model[0] and "Benchmark #3 is running" in busy_model[1] and room.supervisor.tasks == []
    assert busy_app[0] and "Benchmark #3 is running" in busy_app[1]
    assert unknown[0] and "There's no brain called 'gpt'" in unknown[1]
    assert stopping == (False, "Stopping benchmark #3; what it scored so far is kept.") and room.bench.stopped == [3]
    assert none == (False, "No benchmark is running.")


def test_the_scores_of_a_benchmark_are_a_tool_too(store: Store) -> None:
    room = Room(store)

    async def run() -> list[tuple[bool, str]]:
        return [await room.call("benchmark_results"), await room.call("benchmark_results", {"run": 3}),
                await room.call("benchmark_results", {"run": 99})]

    latest, numbered, missing = asyncio.run(run())
    assert latest == (False, "Benchmark #3 (builtin, done):") == numbered
    assert missing[0] and "There's no benchmark #99" in missing[1]


# ---------------------------------------------------------------------------------------------------- REST
def test_spend_over_rest_has_the_shape_the_hud_is_built_against(store: Store) -> None:
    room = Room(store, key=True)
    room.cfg.openrouter.daily_budget_usd = 2.0
    room.spend.record(model="m", purpose="job", usage=Usage(cost=0.5))
    with TestClient(rest_app(room)) as client:
        body = client.get("/api/spend", params={"days": 2}).json()
        assert set(body) == {"today", "budget", "currency", "by_day", "by_purpose", "key"}
        assert (body["today"], body["budget"], body["currency"]) == (0.5, 2.0, "USD") and len(body["by_day"]) == 2
        assert body["by_purpose"] == [{"purpose": "job", "cost": 0.5}]
        assert body["key"] == {"limit": 5.0, "limit_remaining": 3.25, "usage_daily": 0.5, "limit_reset": "monthly"}
        assert len(client.get("/api/spend").json()["by_day"]) == 30  # a month, unless told
        assert [client.get("/api/spend", params={"days": days}).status_code for days in (0, 366)] == [422, 422]
        room.cfg.openrouter.api_key = ""
        assert client.get("/api/spend").json()["key"] is None


def test_benchmarks_over_rest(store: Store) -> None:
    room = Room(store)
    room.bench.views = [view(7, "running", 0.5)]
    with TestClient(rest_app(room)) as client:
        assert client.get("/api/bench").json() == {"items": [view(7, "running", 0.5)]}
        assert client.get("/api/bench/7").json() == view(7, "running", 0.5)
        missing = client.get("/api/bench/99")
        assert (missing.status_code, missing.json()) == (404, {"detail": "There's no benchmark #99."})
        plan = client.post("/api/bench/plan", json={"brains": ["claude", "script"], "limit": 4}).json()
        assert plan["cases"] == 4 and plan["paid_requests"] == {"typical": 8, "most": 32} and plan["dry_run"] is True
        assert plan["text"].startswith("Benchmark on the built-in suite: 4 requests per brain") and room.bench.started == []
        # a brain that is free starts at once; one that costs is refused until the user says yes, with its plan
        assert client.post("/api/bench", json={"brains": ["script"], "suite": "history"}).json() == {"id": 7}
        assert room.bench.started == [(["script"], "history", None, "hud")]
        refused = client.post("/api/bench", json={"brains": ["claude"]})
        assert refused.status_code == 409 and refused.json()["plan"]["paid_requests"] == {"typical": 6, "most": 24}
        assert "about 6 requests, at most 24" in refused.json()["detail"]
        assert refused.json()["detail"].endswith("Send confirm: true to start it.") and len(room.bench.started) == 1
        assert client.post("/api/bench", json={"brains": ["claude"], "confirm": True}).json() == {"id": 7}
        assert room.bench.started[-1] == (["claude"], None, None, "hud")
        room.bench.running = SimpleNamespace(id=3)
        busy = client.post("/api/bench", json={"brains": ["script"]})
        assert busy.status_code == 409 and "Benchmark #3 is running" in busy.json()["detail"]
        unknown = client.post("/api/bench/plan", json={"brains": ["gpt"]})
        assert unknown.status_code == 400 and "There's no brain called 'gpt'" in unknown.json()["detail"]
        for bad in ({"limit": 0}, {"suite": "weird"}, {"brains": "claude"}, {"confirm": "yes"}):
            assert client.post("/api/bench", json=bad).status_code == 422
        assert client.post("/api/bench/7/stop").json() == {"ok": True}
        assert client.post("/api/bench/8/stop").status_code == 404 and room.bench.stopped == [7, 8]


# ---------------------------------------------------------------------------------------------------- the command
def command(monkeypatch: pytest.MonkeyPatch, room: Room, args: list[str], answer: str = "") -> Any:
    """Run `orion bench` against the REST app above instead of a running Orion."""
    from orion.__main__ import main

    app = rest_app(room)
    monkeypatch.setattr(httpx, "Client", lambda **kw: TestClient(app, base_url=kw["base_url"], headers=kw["headers"]))
    monkeypatch.setattr("orion.__main__.BENCH_POLL_S", 0)
    return CliRunner().invoke(main, ["bench", *args], input=answer)


def test_the_command_says_the_plan_asks_before_paid_requests_and_follows_the_run(
        store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    room = Room(store)
    room.bench.views = [view(7, "running", 0.25), view(7, "running", 0.75), view(7, "done", 1.0)]
    for declined in ("n\n", ""):  # no, and no answer at all (a pipe): nothing is started
        result = command(monkeypatch, room, ["--brain", "claude", "--limit", "4"], declined)
        assert result.exit_code == 1 and "Start it? [y/N]" in result.output and room.bench.started == []
    assert "on your Claude plan: about 8 requests, at most 32" in result.output
    result = command(monkeypatch, room, ["--brain", "claude", "--brain", "script", "--suite", "history"], "y\n")
    assert result.exit_code == 0 and room.bench.started == [(["claude", "script"], "history", None, "hud")]
    assert "25% of the requests asked" in result.output and "100% of the requests asked" in result.output
    assert result.output.rstrip().endswith("Benchmark #7 (builtin, done):")
    room.bench.started.clear()
    room.bench.views[:] = [view(7, "done", 1.0)]
    free = command(monkeypatch, room, ["--brain", "script"])  # nothing to pay: no question
    assert free.exit_code == 0 and "Start it?" not in free.output and len(room.bench.started) == 1
    yes = command(monkeypatch, room, ["--yes"])  # the default brain, paid: --yes is the answer
    assert yes.exit_code == 0 and room.bench.started[-1] == ([], None, None, "hud")


def test_the_command_says_what_went_wrong(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    room = Room(store)
    refused = command(monkeypatch, room, ["--brain", "gpt"])
    assert refused.exit_code == 1 and "There's no brain called 'gpt'" in refused.output
    room.bench.running = SimpleNamespace(id=3)
    busy = command(monkeypatch, room, ["--brain", "script"])
    assert busy.exit_code == 1 and "Benchmark #3 is running" in busy.output
    room.bench.running = None
    room.bench.views = [view(7, "running", 0.5), view(7, "stopped", 0.5)]
    failed = command(monkeypatch, room, ["--brain", "script"])
    assert failed.exit_code == 1 and "Benchmark #7 (builtin, stopped before the end)" in failed.output

    def down(**kwargs: Any) -> httpx.Client:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        return REAL_CLIENT(transport=httpx.MockTransport(refuse), base_url=kwargs["base_url"])

    from orion.__main__ import main

    monkeypatch.setattr(httpx, "Client", down)
    off = CliRunner().invoke(main, ["bench", "--brain", "script"])
    assert off.exit_code == 1 and "Orion isn't running" in off.output and "orion start" in off.output


def test_ctrl_c_stops_the_run_and_the_command_reports_what_it_scored(
        store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    room = Room(store)
    room.bench.views = [view(7, "running", 0.5), view(7, "stopped", 0.5)]
    pressed = iter([True])

    def sleep(seconds: float) -> None:
        if next(pressed, False):
            raise KeyboardInterrupt

    monkeypatch.setattr("orion.__main__.time.sleep", sleep)
    result = command(monkeypatch, room, ["--brain", "script"])
    assert room.bench.stopped == [7] and "stopping..." in result.output
    assert result.exit_code == 1 and "stopped before the end)" in result.output


def test_orion_check_reads_the_script_and_never_starts_ollama_for_it(tmp_path) -> None:
    from orion.__main__ import _check_brain

    cfg = default_config()
    cfg.brain.provider = "script"
    said: list[tuple[bool, str, str]] = []

    def check() -> list[tuple[bool, str, str]]:
        said.clear()
        asyncio.run(_check_brain(cfg, lambda ok, what, fix="": said.append((ok, what, fix))))
        return list(said)

    cfg.script.path = str(Path(__file__).resolve().parent.parent / "orion" / "llm" / "script_example.yaml")
    assert check() == [(True, "script script_example.yaml (7 rules): he answers from it, with no model", "")]
    cfg.script.path = str(tmp_path / "gone.yaml")
    ((ok, what, fix),) = check()
    assert not ok and "gone.yaml: No such file or directory" in what and fix == "fix script.path in data/config.yaml"
    (tmp_path / "gone.yaml").write_text("rules: [")
    ((ok, what, fix),) = check()
    assert not ok and "gone.yaml: line" in what
