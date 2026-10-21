"""bench, whole runs: the real Brain on a scripted brain and on a fake OpenRouter. Scores, isolation from the
conversation and the tools, what the brain under test reads, the plan before any paid request, the budget, errors,
giving up on a brain, stopping, and the record kept. Nothing real is run or paid."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import yaml

from orion.base import EventBus, StepRecord, Tool, ToolCall
from orion.bench import INTERRUPTED, Bench, BenchBusy, plan_text, results_text
from orion.config import default_config
from orion.llm import LLM, OpenRouterLLM, Piece, Prompt, Usage
from orion.memory import MemorySystem
from orion.skills import Skills
from orion.spend import Spend


class NoModel:
    """An embedder without a model."""

    id = ""
    loaded = False

    def load(self) -> bool:
        return False

    def encode(self, texts: object) -> None:
        return None

    def encode_query(self, text: str) -> None:
        return None


class Supervisor:
    """The runtime's way of running background work."""

    def spawn(self, job: Any, *, name: str) -> "asyncio.Task[Any]":
        return asyncio.get_running_loop().create_task(job, name=name)


class World:
    """The parts of a running Orion that a benchmark uses: a real memory, a real spend meter and the real Brain the
    benchmark builds itself; the rest are recorders. Its scripted brain follows these rules."""

    def __init__(self, tmp_path: Path, toolbox: Mapping[str, Tool], rules: list[dict[str, Any]]) -> None:
        self.cfg = default_config()
        script = tmp_path / "bench-script.yaml"
        script.write_text(yaml.safe_dump({"rules": rules}))
        self.cfg.script.path = str(script)
        self.memory = MemorySystem.open(self.cfg, NoModel(), role="main")  # type: ignore[arg-type]
        self.skills = Skills(tmp_path / "skills")
        self.embedder = NoModel()
        self.hub = SimpleNamespace(tools=lambda: toolbox, version=1)
        self.events: list[dict[str, Any]] = []
        self.bus = EventBus()
        self.bus.subscribe(self.events.append)
        self.spend = Spend(self.cfg, self.memory.store, self.bus.publish)
        self.supervisor = Supervisor()
        self.idles = 0
        self.turns = SimpleNamespace(idle=self.idle)
        self.logged: list[dict[str, Any]] = []
        self.activity = SimpleNamespace(record=lambda kind, **entry: self.logged.append({"kind": kind, **entry}))
        self.rewarmed = 0
        self.brain = SimpleNamespace(rewarm=self.rewarm)
        self.bench = Bench(self, redact=lambda text: text.replace("hunter2", "[secret]"))  # type: ignore[arg-type]

    async def idle(self) -> None:
        self.idles += 1

    async def rewarm(self) -> None:
        self.rewarmed += 1

    def bench_events(self) -> list[dict[str, Any]]:
        return [event for event in self.events if event["type"] == "bench"]

    def flush(self) -> None:
        self.memory.store.submit(lambda: None).result(timeout=5)

    def count(self, table: str) -> int:
        self.flush()
        with self.memory.store.reading() as db:
            return int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


@pytest.fixture
def world(tmp_path: Path, toolbox: Mapping[str, Tool]) -> Iterator[Callable[..., World]]:
    made: list[World] = []

    def build(rules: list[dict[str, Any]] | None = None, tools: Mapping[str, Tool] | None = None) -> World:
        made.append(World(tmp_path, tools or toolbox, rules or [{"when": ".", "say": "Hello."}]))
        return made[-1]

    yield build
    for found in made:
        found.memory.close()


def call(name: str, **args: Any) -> dict[str, Any]:
    return {"name": name, "args": args}


def asks(when: str, *calls: dict[str, Any], then: str = "Done.") -> dict[str, Any]:
    """A rule: this request makes one round of these calls, then the answer."""
    return {"when": when, "rounds": [{"tools": list(calls)}, {"say": then}]}


async def finish(w: World, brains: list[str], suite: Any = None, limit: int | None = None) -> dict[str, Any]:
    """Start a run and wait for it; its record."""
    started = await w.bench.start(brains, suite, limit, source="hud")
    run = w.bench.running
    assert run is not None and run.task is not None
    await run.task
    final = await w.bench.run(started["id"])
    assert final is not None
    await asyncio.sleep(0)  # the re-warm he was told to do
    return final


SCORED = [
    asks("timer for 5 minutes", call("set_timer", duration="5 minutes"),
         call("set_reminder", message="x", when="in 5 minutes")),  # right, but a call he must not make too
    asks("25 minute timer", call("set_timer", duration="1500 seconds", label="pomodoro")),  # the same length
    asks("take the pizza out", call("set_timer", duration="10 minutes")),  # a timer for a reminder
    asks("call mum", call("set_reminder", message="Call mum", when="tomorrow at 9")),
    asks("timers and reminders do I have", call("list_reminders")),
    asks("pasta timer", call("cancel_timer_or_reminder", which="pizza")),  # the wrong one
    asks("milk", call("todo", action="add", item="milk")),
    {"when": ".", "say": "I can't do that."},  # the to-do list: answered without looking
]


def test_a_run_scores_each_case_and_leaves_the_conversation_and_the_tools_alone(world: Callable[..., World]) -> None:
    w = world(SCORED)
    store = w.memory.store
    conv = store.current_conv()
    store.log_message("user", "Good evening", source="hud", conv=conv.id, turn="t0", ts=1.0)
    store.log_message("assistant", "Good evening, sir.", source="hud", conv=conv.id, turn="t0", ts=2.0)
    w.flush()

    run = asyncio.run(finish(w, ["script"], "builtin", 8))
    assert (run["id"], run["suite"], run["brains"], run["status"], run["progress"], run["error"]) == (
        1, "builtin", ["script"], "done", 1.0, "")
    (score,) = run["results"]
    assert (score["brain"], score["label"], score["paid"], score["cases"], score["answered"]) == (
        "script", "Script (bench-script.yaml)", "", 8, 8)
    assert (score["passed"], score["tool_accuracy"], score["args_accuracy"], score["forbidden"]) == (4, 0.75, 0.625, 1)
    assert score["rounds"] == 1.88 and score["cost"] == 0.0 and score["errors"] == 0 and score["skipped"] == 0  # 15/8
    assert 0 <= score["p50_first_token_ms"] <= score["p95_first_token_ms"]
    assert 0 <= score["p50_total_ms"] <= score["p95_total_ms"] and score["p50_total_ms"] >= score["p50_first_token_ms"]
    assert [f["say"] for f in score["failures"]] == [
        "Set a timer for 5 minutes", "Remind me in 10 minutes to take the pizza out", "Cancel the pasta timer",
        "What's on my to-do list?"]
    assert score["failures"][0] == {"say": "Set a timer for 5 minutes",
                                    "expected": 'set_timer(duration="5 minutes"); never set_reminder',
                                    "got": 'set_timer(duration="5 minutes"); set_reminder(message="x", when="in 5 '
                                           'minutes")'}
    assert score["failures"][2]["expected"] == 'cancel_timer_or_reminder(which="pasta")'
    assert score["failures"][2]["got"] == 'cancel_timer_or_reminder(which="pizza")'
    assert score["failures"][3]["got"] == "no tool"
    assert score["failures"][1]["got"] == 'set_timer(duration="10 minutes")'
    # the progress came as events, then the same record that was kept
    progress = [event["progress"] for event in w.bench_events()]
    assert progress[0] == 0.0 and progress == sorted(progress) and progress[-1] == 1.0
    last = {k: v for k, v in w.bench_events()[-1].items() if k not in ("type", "ts")}
    assert last == run and asyncio.run(w.bench.runs()) == [run]
    # nothing else happened: no turn events, no messages, no tool calls kept, the conversation as it was
    assert {event["type"] for event in w.events} == {"bench"} and w.idles == 8 and w.rewarmed == 0
    assert w.count("messages") == 2 and w.count("steps") == 0 and w.count("spend") == 0
    assert [m["content"] for m in store.history(conv.id, 10)] == ["Good evening", "Good evening, sir."]
    (entry,) = w.logged
    assert (entry["kind"], entry["source"], entry["outcome"], entry["title"]) == (
        "bench", "hud", "ok", "Benchmark of Script (bench-script.yaml): done")
    assert "4 of 8 passed" in entry["detail"] and "1 forbidden calls" in entry["detail"] and entry["ms"] >= 0


class Recorder(LLM):
    """The brain under test, as the benchmark's Brain asks it: it keeps every prompt, calls set_timer once, answers."""

    provider = "fake"

    def __init__(self, cfg: Any, prompts: list[Prompt]) -> None:
        super().__init__(cfg)
        self.prompts = prompts

    @property
    def model(self) -> str:
        return "recorder"

    @property
    def all_tools(self) -> bool:
        return True

    async def stream(self, prompt: Prompt) -> AsyncIterator[Piece]:
        self.prompts.append(prompt)
        if any(m["role"] == "tool" for m in prompt.messages):
            yield "Done."
        else:
            yield [ToolCall("c1", "set_timer", {"duration": "5 minutes"})]

    async def json(self, prompt: str, schema: Mapping[str, Any], *, temperature: float) -> dict[str, Any]:
        return {}


def test_the_brain_under_test_reads_what_a_job_reads_and_the_tools_answer_from_the_case(
        world: Callable[..., World], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        toolbox: Mapping[str, Tool]) -> None:
    suite = tmp_path / "suite.yaml"
    suite.write_text("defaults: {max_rounds: 2, result: Fine.}\ncases:\n  - say: Set a timer for five minutes\n"
                     "    expect: [{tool: set_timer, args: {duration: 5 minutes}}]\n"
                     "    results: {set_timer: 'Timer #1 started. The password is hunter2.'}")
    monkeypatch.setattr("orion.bench.SUITE_PATH", suite)
    prompts: list[Prompt] = []
    monkeypatch.setattr("orion.bench.build_llm", lambda cfg, provider, *, meter, purpose: Recorder(cfg, prompts))
    w = world()
    w.memory.store.log_message("user", "An earlier chat", source="hud", conv=w.memory.store.current_conv().id,
                               turn="t0", ts=1.0)
    run = asyncio.run(finish(w, ["script"]))
    assert run["status"] == "done" and run["results"][0]["passed"] == 1
    first, second = prompts
    assert first.conv == -1 and first.history == () and first.turn.startswith("bench-1-0-0")  # not the conversation
    assert "ORION" in first.system and first.snapshot.startswith("[memory]")  # the persona and the memory snapshot
    assert {tool.name for tool in first.tools.tools} == set(toolbox)  # every tool, unrouted (so no find_tools)
    (request,) = first.messages
    assert request["raw"] == "Set a timer for five minutes"
    assert "This reply will be shown on screen." in request["content"]
    reply, result = second.messages[1:]
    assert reply["tool_calls"][0]["name"] == "set_timer"
    assert result["content"] == "Timer #1 started. The password is [secret]."  # from the case, through the redactor
    assert second.system == first.system and second.snapshot == first.snapshot  # one epoch, so caches keep working


def test_the_plan_says_how_many_requests_are_paid_and_where_before_anything_starts(
        world: Callable[..., World]) -> None:
    w = world()
    w.cfg.openrouter.api_key = "sk-test"
    brains = ["claude", "openrouter:anthropic/claude-haiku", "openrouter", "ollama:qwen3.5:9b", "script", "claude"]
    view = asyncio.run(w.bench.plan(brains, "builtin", 3)).view()
    assert (view["suite"], view["cases"], view["dry_run"], view["skipped"]) == ("builtin", 3, True, [])
    assert [(b["brain"], b["paid"], b["where"], b["ready"]) for b in view["brains"]] == [
        ("claude", "plan", "your Claude plan", True), ("openrouter:anthropic/claude-haiku", "money", "OpenRouter", True),
        ("openrouter", "", "OpenRouter", True),  # google/gemma-4-31b-it:free costs nothing
        ("ollama:qwen3.5:9b", "", "this PC", True), ("script", "", "no model", True)]  # "claude" asked twice: once
    assert view["brains"][0]["requests"] == {"typical": 6, "most": 24}  # 3 cases of 2 rounds; at most 6 + 2 each
    assert view["brains"][0]["label"] == "Claude Sonnet" and view["brains"][3]["label"] == "qwen3.5:9b"
    assert view["paid_requests"] == {"typical": 12, "most": 48}  # the plan and the money, not the free ones
    words = plan_text(view)
    assert words.splitlines()[0] == ("Benchmark on the built-in suite: 3 requests per brain, a dry run (no tool "
                                     "really runs).")
    assert "- claude (Claude Sonnet, your Claude plan): on your Claude plan: about 6 requests, at most 24." in words
    assert "(anthropic/claude-haiku, OpenRouter): paid: about 6 requests, at most 24." in words
    assert "- script (Script (bench-script.yaml), no model): free." in words and "Your requests are sent" not in words
    assert [b["cloud"] for b in view["brains"]] == [True, True, True, False, False]


def test_a_plan_refuses_what_it_cant_run_and_names_what_it_leaves_out(
        world: Callable[..., World], toolbox: Mapping[str, Tool]) -> None:
    w = world(tools={name: tool for name, tool in toolbox.items() if name != "set_timer"})

    def plan(brains: list[str], suite: Any = None, limit: int | None = None) -> Any:
        return asyncio.run(w.bench.plan(brains, suite, limit))

    for args, words in ((["gpt"], "There's no brain called 'gpt'"), ((["claude:gpt"],), "claude:gpt:"),
                        ((["script"], "weird"), "no 'weird' suite"), ((["script"], "builtin", 0), "at least one case"),
                        ((["script"], "history"), "no request of yours is long enough")):
        with pytest.raises(ValueError, match=words):
            plan(*(args if isinstance(args, tuple) else (args,)))
    cut = plan(["script"], None, 3)  # no suite: bench.suite; set_timer isn't here: its cases are left out, not failed
    assert cut.suite == "builtin" and [c.say for c in cut.cases] == [
        "Remind me in 10 minutes to take the pizza out", "Remind me to call mum tomorrow at 9",
        "What timers and reminders do I have?"]
    assert list(cut.skipped) == [("Set a timer for 5 minutes", "needs set_timer, which isn't here"),
                                 ("Start a 25 minute timer for my pomodoro", "needs set_timer, which isn't here")]
    assert ("Left out: 2 requests whose tools aren't here (needs set_timer, which isn't here; needs set_timer"
            in plan_text(cut.view()))
    assert plan([], None, 1).brains[0].brain == "claude"  # none asked: the brain he thinks with
    w.cfg.bench.suite = "history"
    with pytest.raises(ValueError, match="no request of yours is long enough"):
        plan([])


def test_an_openrouter_brain_whose_budget_is_spent_is_left_out_of_the_run(world: Callable[..., World]) -> None:
    w = world([{"when": ".", "say": "Hello."}])
    w.cfg.openrouter.daily_budget_usd = 1.0
    w.spend.record(model="anthropic/claude-haiku", purpose="conversation", usage=Usage(cost=1.0))
    haiku = "openrouter:anthropic/claude-haiku"

    async def run() -> Any:
        planned = await w.bench.plan([haiku, "openrouter", "script"], "builtin", 2)
        with pytest.raises(ValueError, match=r"No brain can run: openrouter:anthropic/claude-haiku: today's "
                                             r"OpenRouter budget is spent \(\$1\.00 of \$1\.00\)\."):
            await w.bench.start([haiku], "builtin", 2, source="hud")
        return planned, await finish(w, [haiku, "script"], "builtin", 2)

    planned, final = asyncio.run(run())
    view = planned.view()
    assert [(b["brain"], b["ready"]) for b in view["brains"]] == [(haiku, False), ("openrouter", True), ("script", True)]
    assert view["paid_requests"] == {"typical": 0, "most": 0}
    assert "Won't run: today's OpenRouter budget is spent" in plan_text(view)
    skipped, ran = final["results"]
    assert (skipped["cases"], skipped["skipped"]) == (0, 2)
    assert skipped["error"] == "today's OpenRouter budget is spent ($1.00 of $1.00)"
    assert (skipped["tool_accuracy"], skipped["p50_total_ms"], skipped["cost"]) == (0.0, 0.0, 0.0)  # numbers, not null
    assert ran["cases"] == 2 and final["status"] == "done" and final["progress"] == 1.0


def sse(*events: dict[str, Any]) -> httpx.Response:
    lines = [f"data: {json.dumps(event)}" for event in events]
    return httpx.Response(200, text="\n\n".join([*lines, "data: [DONE]"]))


def fake_openrouter(monkeypatch: pytest.MonkeyPatch, w: World, posts: list[dict[str, Any]]) -> None:
    """OpenRouter on a mock transport: it calls set_timer for five minutes, then answers; each request costs 0.0042."""

    def reply(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"data": {"limit": None, "limit_remaining": None, "limit_reset": None,
                                                      "usage_daily": 0.0}})
        body = json.loads(request.content)
        posts.append(body)
        usage = {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 5, "cost": 0.0042}}
        if any(m["role"] == "tool" for m in body["messages"]):
            return sse({"choices": [{"delta": {"content": "Done."}}]}, usage)
        asked = {"index": 0, "id": "c1", "function": {"name": "set_timer", "arguments": '{"duration": "5 minutes"}'}}
        return sse({"choices": [{"delta": {"tool_calls": [asked]}}]}, usage)

    def build(cfg: Any, provider: str, *, meter: Any, purpose: str) -> LLM:
        assert (provider, meter, purpose) == ("openrouter", w.spend, "bench")  # the benchmark's own, and metered
        return OpenRouterLLM(cfg, meter=meter, purpose=purpose, transport=httpx.MockTransport(reply))

    async def models(url: str) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr("orion.bench.build_llm", build)
    monkeypatch.setattr("orion.llm.openrouter_models", models)


def test_a_run_respects_the_openrouter_budget_and_counts_its_requests_as_benchmarks(
        world: Callable[..., World], monkeypatch: pytest.MonkeyPatch) -> None:
    w = world()
    w.cfg.openrouter.api_key = "sk-test"
    w.cfg.openrouter.daily_budget_usd = 0.01
    posts: list[dict[str, Any]] = []
    fake_openrouter(monkeypatch, w, posts)
    final = asyncio.run(finish(w, ["openrouter:anthropic/claude-haiku"], "builtin", 4))
    (score,) = final["results"]
    # case 1: two requests (0.0084); case 2: its first passes the check (0.0126), its second is refused before it is sent
    assert final["status"] == "done" and score["paid"] == "money"
    assert (score["cases"], score["answered"], score["errors"], score["skipped"], score["passed"]) == (2, 1, 1, 2, 1)
    assert "daily budget is spent" in score["error"] and score["tool_accuracy"] == 0.5
    assert score["cost"] == pytest.approx(0.0126) and w.spend.today() == pytest.approx(0.0126) and len(posts) == 3
    assert w.count("spend") == 3
    with w.memory.store.reading() as db:
        assert {r[0] for r in db.execute("SELECT purpose FROM spend")} == {"bench"}
    assert [e["today"] for e in w.events if e["type"] == "spend"] == [0.0042, 0.0084, 0.0126]
    assert w.bench_events()[-1]["results"][0]["error"] == score["error"]


def test_a_brain_that_cant_go_on_is_given_up_on_and_the_run_goes_on_without_it(world: Callable[..., World]) -> None:
    w = world([{"when": ".", "fail": "billing"}])

    def stopped(brains: list[str]) -> list[dict[str, Any]]:
        return asyncio.run(finish(w, brains, "builtin", 5))["results"]

    (score,) = stopped(["script"])  # an error only a person can mend: the first case is the last
    assert (score["cases"], score["answered"], score["errors"], score["skipped"]) == (1, 0, 1, 4)
    assert score["error"] == "The scripted brain failed on purpose (billing), as its script says."
    assert (score["tool_accuracy"], score["args_accuracy"], score["rounds"], score["p95_total_ms"]) == (
        0.0, 0.0, 0.0, 0.0)
    assert score["failures"][0]["got"] == "error: " + score["error"]
    assert asyncio.run(w.bench.run(1))["progress"] == 1.0  # the cases skipped are counted as done
    w.cfg.script.path = str(Path(w.cfg.script.path).with_name("gone.yaml"))  # no script at all: kind "model"
    (score,) = stopped(["script"])
    assert (score["cases"], score["skipped"]) == (1, 4) and "has no script at" in score["error"]


def test_a_brain_that_fails_a_few_requests_in_a_row_is_given_up_on_but_one_that_recovers_is_not(
        tmp_path: Path, world: Callable[..., World]) -> None:
    failing = [{"when": "timer for 5 minutes", "fail": "timeout"}, {"when": "take the pizza out", "fail": "timeout"},
               {"when": ".", "say": "Hello."}]
    w = world(failing)
    w.cfg.bench.stop_after_errors = 2

    def run(limit: int) -> dict[str, Any]:
        return asyncio.run(finish(w, ["script"], "builtin", limit))["results"][0]

    mixed = run(4)  # timeout, answer, timeout, answer: never two in a row
    assert (mixed["cases"], mixed["errors"], mixed["skipped"], mixed["error"]) == (4, 2, 0, "")
    (tmp_path / "all-fail.yaml").write_text("rules:\n  - {when: '.', fail: timeout}")
    w.cfg.script.path = str(tmp_path / "all-fail.yaml")
    hopeless = run(6)
    assert (hopeless["cases"], hopeless["errors"], hopeless["skipped"]) == (2, 2, 4)
    assert "failed on purpose (timeout)" in hopeless["error"]


def test_a_bug_in_a_run_fails_the_run_and_says_so(world: Callable[..., World],
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(cfg: Any, provider: str, *, meter: Any, purpose: str) -> LLM:
        raise RuntimeError("boom")

    monkeypatch.setattr("orion.bench.build_llm", explode)
    w = world()
    final = asyncio.run(finish(w, ["script"], "builtin", 2))
    assert (final["status"], final["error"]) == ("failed", "RuntimeError: boom") and w.bench.running is None
    assert w.logged[-1]["outcome"] == "failed"
    assert w.logged[-1]["title"] == "Benchmark of Script (bench-script.yaml): failed"


def test_one_run_at_a_time_and_a_stopped_run_keeps_what_it_scored(world: Callable[..., World]) -> None:
    w = world([{"when": ".", "delay_ms": 400, "say": "Slow."}])

    async def run() -> dict[str, Any]:
        started = await w.bench.start(["script"], "builtin", 5, source="voice")
        running = w.bench.running
        assert running is not None and running.task is not None
        await asyncio.sleep(0.1)  # inside the first case's wait
        assert [r["status"] for r in await w.bench.runs()] == ["running"]
        with pytest.raises(BenchBusy, match=r"Benchmark #1 is running \(0 of 5 cases\)"):
            await w.bench.start(["script"], source="hud")
        assert not w.bench.stop(started["id"] + 1) and w.bench.stop(started["id"])
        await asyncio.wait({running.task})
        assert not w.bench.stop(started["id"]) and w.bench.running is None
        final = await w.bench.run(started["id"])
        assert final is not None
        return final

    final = asyncio.run(run())
    assert (final["status"], final["error"], final["progress"]) == ("stopped", "", 0.0)
    assert w.logged[-1]["outcome"] == ""  # you ended it: not a failure
    assert asyncio.run(finish(w, ["script"], "builtin", 1))["id"] == 2  # and the next one can start


def test_a_benchmark_of_ollama_gives_the_conversation_its_cache_back_also_when_stopped(
        world: Callable[..., World], monkeypatch: pytest.MonkeyPatch) -> None:
    """Ollama keeps one conversation's start cached: a benchmark of it takes that slot, so the conversation is read
    into it again once the run ends or is stopped (no real Ollama is ever asked here)."""
    prompts: list[Prompt] = []
    monkeypatch.setattr("orion.bench.build_llm", lambda cfg, provider, *, meter, purpose: Recorder(cfg, prompts))
    w = world()
    assert asyncio.run(finish(w, ["ollama:qwen3.5:9b"], "builtin", 2))["status"] == "done" and w.rewarmed == 1

    async def stopped() -> dict[str, Any] | None:
        started = await w.bench.start(["ollama"], "builtin", 30, source="hud")
        running = w.bench.running
        # stopped before its task has taken a first step: it must still be finished, or no run could ever start again
        assert running is not None and running.task is not None and w.bench.stop(running.id)
        await asyncio.wait({running.task})
        await asyncio.sleep(0)
        assert w.bench.running is None
        return await w.bench.run(started["id"])

    final = asyncio.run(stopped())
    assert final is not None and (final["status"], final["error"]) == ("stopped", "") and w.rewarmed == 2
    assert asyncio.run(finish(w, ["ollama"], "builtin", 1))["status"] == "done"


def test_runs_are_kept_newest_first_the_oldest_forgotten_and_one_cut_short_by_a_restart_is_failed(
        world: Callable[..., World]) -> None:
    w = world()
    w.cfg.bench.keep_runs = 2
    store = w.memory.store
    old = store.add_bench_run(at=1.0, suite="builtin", brains=["claude"],
                              results={"progress": 0.5, "results": [], "plan": {}, "error": ""})
    Bench(w, redact=lambda text: text)  # type: ignore[arg-type]  # Orion starts again: that run will never end

    async def run() -> tuple[Any, ...]:
        interrupted = await w.bench.run(old)
        for _ in range(3):
            await finish(w, ["script"], "builtin", 1)
        return interrupted, [r["id"] for r in await w.bench.runs()], await w.bench.run(old), await w.bench.run(999)

    interrupted, ids, forgotten, nothing = asyncio.run(run())
    assert (interrupted["status"], interrupted["error"], interrupted["progress"]) == ("failed", INTERRUPTED, 0.5)
    assert ids == [4, 3] and forgotten is None and nothing is None and w.count("bench_runs") == 2
    with pytest.raises(ValueError, match="can't be asked for by 'nowhere'"):
        asyncio.run(w.bench.start(["script"], source="nowhere"))


def step(name: str, result: str = "ok", *, kind: str = "tool") -> StepRecord:
    return StepRecord(id="", turn="", kind=kind, name=name, title=name, server="", args={},  # type: ignore[typeddict-item]
                      source="brain", client="", at=1.0, ms=1, ok=True, result=result, taints=False, memory=None)


def test_your_own_requests_are_replayed_with_the_tools_they_used_then(world: Callable[..., World]) -> None:
    w = world([asks("timer for ten minutes", call("set_timer", duration="10 minutes")),
               asks("sister", call("memory", action="add", target="user", text="The user's sister is Maya.")),
               {"when": ".", "say": "Ha. Why did the tool cross the road?"}])
    store = w.memory.store
    conv = store.current_conv()

    def said(ident: int, text: str, source: str, *steps: StepRecord) -> None:
        turn = f"h{ident}"
        store.log_message("user", text, source=source, conv=conv.id, turn=turn, ts=float(ident))
        for found in steps:
            store.log_step({**found, "turn": turn, "id": f"{turn}-{found['name']}"}, conv.id)  # type: ignore[typeddict-item]

    said(1, "Set a timer for ten minutes please", "hud", step("set_timer", "Timer #4 started for 10 minutes."))
    said(2, "Remember that my sister is Maya", "voice", step("memory", "Saved.", kind="memory"))
    said(3, "Run the nightly briefing job now", "job")  # a job's request is not one of yours
    said(4, "Turn on the lamp in the study", "hud", step("turn_on_lamp"))  # a tool he doesn't have any more
    said(5, "ok", "hud")  # too short to mean anything
    said(6, "Tell me something funny please", "discord")
    w.flush()
    plan = asyncio.run(w.bench.plan(["script"], "history", 10))
    assert [c.say for c in plan.cases] == ["Tell me something funny please", "Remember that my sister is Maya",
                                           "Set a timer for ten minutes please"]
    assert plan.skipped == (("Turn on the lamp in the study", "needs turn_on_lamp, which isn't here"),)
    final = asyncio.run(finish(w, ["script"], "history", 10))
    (score,) = final["results"]
    assert (final["suite"], score["cases"], score["passed"], score["tool_accuracy"], score["args_accuracy"]) == (
        "history", 3, 3, 1.0, 1.0)
    assert w.count("messages") == 6 and w.count("steps") == 3  # the log is what it was
    assert "Your requests are sent" not in plan_text(plan.view())  # the script is no one's server
    w.cfg.bench.history_requests = 2
    assert len(asyncio.run(w.bench.plan(["claude"], "history")).cases) == 2  # bench.history_requests, by default
    cloud = plan_text(asyncio.run(w.bench.plan(["claude", "openrouter", "ollama"], "history", 1)).view())
    assert cloud.count("Your requests are sent to") == 2  # your own words go to Claude and OpenRouter, not to Ollama
    assert "Your requests are sent to your Claude plan." in cloud and "sent to OpenRouter." in cloud
    assert "Benchmark #1 (history, done):" in results_text(final)
