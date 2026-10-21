"""bench: arguments compared as the tools read them, a case judged on tools, arguments, forbidden calls and rounds, the
built-in suite checked against the real tools, your requests turned into cases, the dry-run hub that runs nothing and
the clock that times a case after its model is ready. (Whole runs are in test_bench_runs.) Nothing real is run."""

import asyncio
import random
from collections.abc import AsyncIterator, Mapping
from typing import Any

import numpy as np
import pytest
from jsonschema import Draft202012Validator

from orion.base import CallContext, LLMError, StepRecord, Tool, ToolOutput, ToolRequest, ToolSet
from orion.bench import (
    SUITE_PATH,
    Call,
    Case,
    DryHub,
    Expect,
    Verdict,
    _Probe,
    arg_matches,
    failure,
    history_cases,
    judge,
    load_suite,
    percentile,
)
from orion.config import default_config
from orion.llm import LLM, Piece, Prompt, Usage
from orion.memory.store import MessageRow

STR = {"type": "string"}
ARRAY = {"type": "array", "items": STR}
ENUM = {"enum": ["pause", "play_pause", "next"]}
INT = {"type": "integer"}
BOOL = {"type": "boolean"}
PROMPT = Prompt("Be brief.", "", 1, "t1", (), ({"role": "user", "content": "hi", "turn": "t1"},),
                ToolSet({}, 0).view(()))


# ---------------------------------------------------------------------------------------------------- matching
@pytest.mark.parametrize(("expected", "given", "schema", "ok"), [
    # a length of time comes to the same seconds, however it is said
    ("5 minutes", "5 minutes", STR, True), ("5 minutes", "300 seconds", STR, True),
    ("5 minutes", "five minutes", STR, True), ("5 minutes", "5", STR, True), ("5 minutes", 5, STR, True),
    ("5 minutes", "50 minutes", STR, False), ("10 minutes", "in 10 minutes", STR, True),
    ("1 hour", "60 minutes", STR, True), ("5 minutes", "soon", STR, False),
    # other text must be found in what was given, whatever its case, spacing and outer punctuation
    ("milk", "Buy milk.", STR, True), ("pizza", "the  PIZZA out", STR, True), ("milk", "bread", STR, False),
    ("milk", None, STR, False), ("milk", ["milk"], STR, False), ("milk", True, STR, False),
    (["mum", "mom"], "Call Mom", STR, True), (["mum", "mom"], "Call dad", STR, False),
    ("london", "London, UK", {"anyOf": [STR, {"type": "null"}]}, True),
    # a choice must be the very word
    ("pause", "pause", ENUM, True), ("PAUSE", "Pause", ENUM, True), ("pause", "play_pause", ENUM, False),
    (["pause", "play_pause"], "play_pause", ENUM, True), (["pause"], "next", ENUM, False),
    ("a", "A", {"anyOf": [{"enum": ["a", "b"]}, {"type": "null"}]}, True),
    ("a", "ab", {"anyOf": [{"enum": ["a", "b"]}, {"type": "null"}]}, False),
    # numbers and yes/no, also written as text
    (40, 40, INT, True), (40, "40", INT, True), (40, 40.0, INT, True), (40, 41, INT, False),
    (40, "forty", INT, False), (40, True, INT, False), (40, None, INT, False),
    (False, False, BOOL, True), (False, "false", BOOL, True), (False, "off", BOOL, True), (False, 0, BOOL, True),
    (True, "no", BOOL, False), (True, "maybe", BOOL, False), (True, None, BOOL, False),
    # a list for a list parameter: every item there; a list for anything else: the choices
    (["weather"], ["vitals", "weather"], ARRAY, True), (["weather", "timers"], ["weather"], ARRAY, False),
    (["weather"], "weather", ARRAY, False),
    # a map: each key given and matching
    ({"a": "x"}, {"a": "box", "b": 1}, {"properties": {"a": STR}}, True), ({"a": "x"}, {"b": "x"}, {}, False),
    ({"a": 1}, "a", {}, False),
])
def test_arguments_are_compared_as_the_tool_reads_them(expected: Any, given: Any, schema: dict, ok: bool) -> None:
    assert arg_matches(expected, given, schema) is ok


def test_percentiles_are_numpys_by_interpolation() -> None:
    rng = random.Random(7)
    for size in (1, 2, 5, 40, 301):
        values = [rng.uniform(0, 5000) for _ in range(size)]
        for q in (0, 50, 95, 100):
            assert percentile(values, q) == pytest.approx(float(np.percentile(values, q)))
    assert percentile([], 50) is None and percentile([3.0], 95) == 3.0


# ---------------------------------------------------------------------------------------------------- judging
def timer_case(**changes: Any) -> Case:
    base: dict[str, Any] = {"say": "Set a timer for 5 minutes", "expect": (Expect("set_timer", {"duration": "5 minutes"}),),
                            "forbid": frozenset({"set_reminder"}), "max_rounds": 2, "results": {}, "result": "Done."}
    return Case(**(base | changes))


def schemas(tool: str) -> dict:
    return {"properties": {"duration": STR, "label": STR}}


def good(duration: str = "300 seconds", **more: Any) -> Call:
    return Call("set_timer", {"duration": duration, **more}, valid=True)


@pytest.mark.parametrize(("calls", "rounds", "error", "tools_ok", "args_ok", "forbidden", "rounds_ok", "passed"), [
    ([good()], 2, "", True, True, 0, True, True),
    ([good("10 minutes")], 2, "", True, False, 0, True, False),  # the right tool, the wrong length of time
    ([Call("set_reminder", {"message": "x", "when": "now"}, valid=True)], 2, "", False, False, 1, True, False),
    ([Call("set_timer", {"duration": "300 seconds"}, valid=False)], 2, "", True, False, 0, True, False),  # invalid
    ([good(), Call("set_reminder", {}, valid=True), Call("set_reminder", {}, valid=True)], 3, "", True, True, 2, False,
     False),
    ([good()], 4, "", True, True, 0, False, False),  # right, but too many rounds
    ([good()], 2, "Claude isn't signed in", False, False, 0, True, False),  # a request that failed is right on no count
    ([], 1, "", False, False, 0, True, False),  # said it did it without calling anything
])
def test_a_case_is_judged_on_tools_arguments_forbidden_calls_and_rounds(
        calls: list[Call], rounds: int, error: str, tools_ok: bool, args_ok: bool, forbidden: int, rounds_ok: bool,
        passed: bool) -> None:
    verdict = judge(timer_case(), calls, rounds, error, schemas)
    assert verdict == Verdict(tools_ok, args_ok, forbidden, rounds_ok, error) and verdict.passed is passed


def test_a_request_that_needs_no_tool_is_answered_without_one() -> None:
    plain = timer_case(say="Tell me a joke", expect=(), max_rounds=1)
    assert judge(plain, [], 1, "", schemas).passed
    assert not judge(plain, [good()], 2, "", schemas).tools_ok
    unknown = Call("turn_on_the_lights", {}, valid=False)  # a tool that doesn't exist counts as a call
    assert not judge(plain, [unknown], 2, "", schemas).passed


def test_each_expected_call_needs_a_call_of_its_own() -> None:
    two = timer_case(expect=(Expect("set_timer", {"duration": "5 minutes"}), Expect("set_timer", {"duration": "10 minutes"})),
                     max_rounds=3)
    assert judge(two, [good("10 minutes"), good("5 minutes")], 2, "", schemas).args_ok
    assert not judge(two, [good("5 minutes"), good("5 minutes")], 2, "", schemas).args_ok
    assert not judge(two, [good("5 minutes")], 2, "", schemas).tools_ok or not judge(two, [good()], 2, "", schemas).args_ok


def test_a_failure_says_what_was_asked_what_was_expected_and_what_he_did() -> None:
    case = timer_case()
    wrong = judge(case, [good("10 minutes", label="x" * 200)], 2, "", schemas)
    said = failure(case, [good("10 minutes", label="x" * 200)], 2, wrong)
    assert said["say"] == "Set a timer for 5 minutes"
    assert said["expected"] == 'set_timer(duration="5 minutes"); never set_reminder'
    assert said["got"].startswith('set_timer(duration="10 minutes", label="xxx') and said["got"].endswith('…")')
    slow = failure(case, [good()], 4, judge(case, [good()], 4, "", schemas))
    assert slow["got"] == 'set_timer(duration="300 seconds"); 4 rounds (at most 2)'
    broken = failure(case, [], 0, judge(case, [], 0, "Claude isn't signed in.", schemas))
    assert broken["got"] == "error: Claude isn't signed in."
    unknown = failure(case, [Call("nope", {}, valid=False)], 2, judge(case, [Call("nope", {}, False)], 2, "", schemas))
    assert unknown["got"] == "nope() (invalid)"


# ---------------------------------------------------------------------------------------------------- the suite
def test_the_suite_file_is_checked() -> None:
    cases, result = load_suite("defaults: {max_rounds: 3, result: Ok.}\ncases:\n  - {say: Hi}\n"
                               "  - {say: Timer, expect: [{tool: set_timer, args: {duration: 5}}], max_rounds: 2,"
                               " forbid: [power], results: {set_timer: Started.}}")
    assert result == "Ok." and [c.say for c in cases] == ["Hi", "Timer"]
    assert (cases[0].max_rounds, cases[0].expect, cases[0].result) == (3, (), "Ok.")
    assert cases[1] == Case("Timer", (Expect("set_timer", {"duration": 5}),), frozenset({"power"}), 2,
                            {"set_timer": "Started."}, "Ok.")
    for text, words in (("defaults: {max_rounds: 3}\ncases: [{say: x}]", "defaults.result"),
                        ("defaults: {max_rounds: 3, result: x}\ncases: []", "cases"),
                        ("defaults: {max_rounds: 0, result: x}\ncases: [{say: x}]", "defaults.max_rounds"),
                        ("defaults: {max_rounds: 3, result: x}\ncases: [{say: x, expct: []}]", "cases.0.expct"),
                        ("defaults: {max_rounds: 3, result: x}\ncases: [{say: x, expect: [{args: {}}]}]",
                         "cases.0.expect.0.tool"),
                        ("cases: [", "isn't YAML")):
        with pytest.raises(ValueError, match=words):
            load_suite(text)


def concrete(expected: Any, schema: Mapping[str, Any]) -> Any:
    """One value that satisfies an expectation: the first of a list of choices, every item of a list parameter."""
    if isinstance(expected, list):
        if schema.get("type") == "array":
            return [concrete(item, schema.get("items") or {}) for item in expected]
        return concrete(expected[0], schema)
    if isinstance(expected, Mapping):
        properties = schema.get("properties") or {}
        return {key: concrete(value, properties.get(key) or {}) for key, value in expected.items()}
    return expected


def test_the_built_in_suite_matches_the_real_tools(toolbox: Mapping[str, Tool]) -> None:
    """About forty requests; every tool a case expects, forbids or answers for is a real tool (but for the documents'
    group, which another package adds: its cases are checked as soon as it is there) and every expected argument is
    one the tool takes, in a form it accepts."""
    cases, _ = load_suite(SUITE_PATH.read_text(encoding="utf-8"))
    later = {"documents_search", "documents_status"}
    assert 38 <= len(cases) <= 45 and len({case.say for case in cases}) == len(cases)
    for case in cases:
        named = {e.tool for e in case.expect} | case.forbid | set(case.results)
        assert named - later <= toolbox.keys(), (case.say, named - later - toolbox.keys())
        assert set(case.results) <= {e.tool for e in case.expect}, case.say
        for expect in case.expect:
            if expect.tool not in toolbox:
                continue
            schema = toolbox[expect.tool].parameters
            args = concrete(dict(expect.args), schema)
            partial = {**schema, "required": []}  # a case lists only the arguments that matter
            Draft202012Validator(partial).validate(args)  # raises with the case's argument at fault
            assert arg_matches(dict(expect.args), args, schema), (case.say, expect)
            assert set(args) <= set(schema["properties"]), (case.say, expect)
    assert all(not case.expect for case in cases if case.max_rounds == 1)
    assert sum(not case.expect for case in cases) == 2 and max(case.typical_rounds() for case in cases) == 3


def message(ident: int, text: str, turn: str) -> MessageRow:
    return MessageRow(id=ident, role="user", content=text, source="hud", ts=float(ident), conv=1, turn=turn)


def step(turn: str, name: str, result: str = "ok", *, kind: str = "tool") -> StepRecord:
    return StepRecord(id=f"{turn}-{name}", turn=turn, kind=kind, name=name, title=name, server="", args={},  # type: ignore[typeddict-item]
                      source="brain", client="", at=1.0, ms=1, ok=True, result=result, taints=False, memory=None)


def test_your_latest_requests_become_cases_with_the_tools_used_then() -> None:
    requests = [
        (message(9, "  Set a timer   for ten minutes ", "t9"), [step("t9", "set_timer", "Timer #4 started.")]),
        (message(8, "thanks", "t8"), []),  # too short to mean anything alone
        (message(7, "What's the weather in Paris?", "t7"), [step("t7", "find_tools"), step("t7", "get_weather", "Rain.")]),
        (message(6, "set a timer for ten minutes", "t6"), [step("t6", "set_timer")]),  # said before: only the latest
        (message(5, "Remember my sister is Maya", "t5"), [step("t5", "memory", "Saved.", kind="memory")]),
        (message(4, "Tell me a joke please", "t4"), []),
        (message(3, "Open the usual page and lock the screen", "t3"),
         [step("t3", "open", "Opened."), step("t3", "lock_screen"), step("t3", "open", "Again.")]),
        (message(2, "An older request than the limit", "t2"), []),
    ]
    cases = history_cases(requests, min_words=3, result="Done.")
    assert [c.say for c in cases] == ["Set a timer for ten minutes", "What's the weather in Paris?",
                                      "Remember my sister is Maya", "Tell me a joke please",
                                      "Open the usual page and lock the screen", "An older request than the limit"]
    timer, weather, memory, joke, pages, _ = cases
    assert [e.tool for e in timer.expect] == ["set_timer"] and timer.results == {"set_timer": "Timer #4 started."}
    assert [e.tool for e in weather.expect] == ["get_weather"]  # find_tools was how he searched, not what was asked
    assert [e.tool for e in memory.expect] == ["memory"] and joke.expect == ()
    assert [e.tool for e in pages.expect] == ["open", "lock_screen"] and pages.results["open"] == "Opened."  # first
    assert all(c.max_rounds is None and c.result == "Done." and not c.forbid for c in cases)


# ---------------------------------------------------------------------------------------------------- the dry run
def test_the_dry_hub_runs_nothing_and_answers_as_the_tool_would(toolbox: Mapping[str, Tool]) -> None:
    cfg = default_config()
    hub = DryHub(cfg, toolbox, redact=lambda text: text.replace("hunter2", "[secret]"))
    case = timer_case(results={"set_timer": "Timer #1 started. hunter2"}, result="Fine.")
    ctx = CallContext("brain", "t1", source="bench")
    hub.begin("t1", case)

    async def run() -> list[ToolOutput]:
        args = {"duration": "5 minutes", "label": "pasta"}
        out = [await hub.call_named_rich(ToolRequest("set_timer", args), ctx)]
        args["label"] = "changed afterwards"  # what the brain later does with its dict can't change the record
        out.append(await hub.call_named_rich(ToolRequest("set_timer", {"label": "no length"}), ctx))
        out.append(await hub.call_named_rich(ToolRequest("set_timer", {"duration": ["5 minutes"]}), ctx))
        out.append(await hub.call_named_rich(ToolRequest("volume", {"action": "set", "level": "40"}), ctx))  # a slip
        out.append(await hub.call_named_rich(ToolRequest("volume", {"action": "louder"}), ctx))
        out.append(await hub.call_named_rich(ToolRequest("turn_on_the_lights", {}), ctx))
        return out

    ok, missing, wrong, forgiven, choice, nothing = asyncio.run(run())
    assert ok.text == "Timer #1 started. [secret]" and forgiven.text == "Fine."
    assert missing.text.startswith("Error: Invalid arguments for set_timer: arguments: 'duration' is a required")
    assert wrong.text.startswith("Error: Invalid arguments for set_timer: duration:")
    assert choice.text.startswith("Error: Invalid arguments for volume: action:") and "louder" in choice.text
    assert nothing.text == "Error: there is no tool called turn_on_the_lights."
    hub.ops.record(server="orion", name="turn_on_the_lights", title="turn_on_the_lights", ctx=ctx, args={"x": 1},
                   ok=False, result="Error")  # the brain's own record of a name no tool has
    hub.ops.record(server="orion", name="find_tools", title="Find tools", ctx=ctx, args={}, ok=True, result="-")
    calls = hub.end("t1")
    assert [(c.tool, c.valid) for c in calls] == [
        ("set_timer", True), ("set_timer", False), ("set_timer", False), ("volume", True), ("volume", False),
        ("turn_on_the_lights", False), ("turn_on_the_lights", False)]
    assert calls[0].args == {"duration": "5 minutes", "label": "pasta"} and hub.schema("nope") == {}
    assert hub.schema("set_timer") is toolbox["set_timer"].parameters and hub.ops.turn_steps("t1") == []


# ---------------------------------------------------------------------------------------------------- the clock
class Fake(LLM):
    """A model that is slow to get ready, then answers in two beats, and keeps a log of what was done to it."""

    provider = "fake"

    def __init__(self, cfg: Any, log: list[str], *, ready_s: float = 0.3,
                 usages: tuple[Usage | None, ...] = (Usage(cost=0.01),)) -> None:
        super().__init__(cfg)
        self.log, self.ready_s, self.usages = log, ready_s, list(usages)
        self.prompts: list[Prompt] = []

    @property
    def model(self) -> str:
        return "fake"

    async def warm_up(self, prompt: Prompt) -> None:
        self.prompts.append(prompt)
        self.log.append("warm_up")

    async def ready(self, prompt: Prompt) -> None:
        await asyncio.sleep(self.ready_s)
        self.log.append("ready")

    async def stream(self, prompt: Prompt) -> AsyncIterator[Piece]:
        await asyncio.sleep(0.03)
        yield "Hi"
        await asyncio.sleep(0.03)
        if self.usages:
            self.usage = self.usages.pop(0)  # what the provider reports after each request

    async def json(self, prompt: str, schema: Mapping[str, Any], *, temperature: float) -> dict[str, Any]:
        return {}

    async def close(self) -> None:
        self.log.append("close")


def test_the_probe_times_a_case_from_its_first_request_once_the_model_is_ready() -> None:
    cfg = default_config()
    log: list[str] = []
    made: list[Fake] = []

    def make() -> LLM:
        same = Usage(cost=0.01)  # Claude: one usage for the whole answer, however many rounds it took
        made.append(Fake(cfg, log, usages=(same, same)))
        return made[-1]

    probe = _Probe(cfg, make)

    async def run() -> tuple:
        await probe.fresh()  # the first model is used as it is
        async for _ in probe.stream(PROMPT):
            pass
        async for _ in probe.stream(PROMPT):  # a second round of the same case: the model is already ready
            pass
        first = probe.timing()
        await probe.fresh()
        return first, probe.timing(), len(made), list(log)

    first, fresh, models, seen = asyncio.run(run())
    assert first.rounds == 2 and first.cost == 0.01  # one usage reported twice is one price
    assert first.first_ms is not None and 25 <= first.first_ms < 250  # the 300 ms of readiness are not in it
    assert first.total_ms is not None and first.first_ms + 25 <= first.total_ms < 250 + 150
    assert (fresh.rounds, fresh.cost, fresh.first_ms, fresh.total_ms) == (0, 0.0, None, None)
    assert models == 2 and seen == ["warm_up", "ready", "close"]  # a new model for the next case, the old one closed
    assert made[0].prompts[0].messages == () and made[0].prompts[0].system == "Be brief."  # loaded with the start


def test_each_round_that_reports_its_own_price_adds_to_the_cases() -> None:
    cfg = default_config()
    # OpenRouter: a new price each request; a request that sent no usage costs nothing
    probe = _Probe(cfg, lambda: Fake(cfg, [], ready_s=0, usages=(Usage(cost=0.01), Usage(cost=0.5), None)))

    async def run() -> float:
        for _ in range(3):
            async for _ in probe.stream(PROMPT):
                pass
        return probe.timing().cost

    assert asyncio.run(run()) == pytest.approx(0.51)


def test_a_model_that_is_not_ready_in_time_is_a_time_out() -> None:
    cfg = default_config()
    cfg.brain.first_chunk_timeout_s = 0.05
    probe = _Probe(cfg, lambda: Fake(cfg, [], ready_s=2))
    with pytest.raises(LLMError, match=r"wasn't ready within 0 s") as e:
        asyncio.run(anext(probe.stream(PROMPT)))
    assert e.value.kind == "timeout"
