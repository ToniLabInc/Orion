"""Benchmarks of the brains: the same requests asked of each brain, scored on whether it called the right tools with
the right arguments, how many rounds it took, how fast it answered and what it cost.

Two suites:
- "builtin" (``orion/bench/suite.yaml``): about forty requests over Orion's own tools (timers, reminders, weather, the
  web, memory, media, volume, apps, the calendar, jobs, documents...), each with the calls a good brain makes
  (``expect``: a tool, and the arguments that matter), the calls it must not make (``forbid``), the rounds it may
  take, and what each tool answers (``results``).
- "history": your own latest requests, each expecting the tools the brain called for it then, which answer what they
  answered then.

It is a dry run: no tool really runs. ``DryHub`` lists the tools the running Orion has and answers every call from
the case, after checking its arguments against the tool's schema as the tool would (the same refusal comes back), and
records what was asked. Each brain runs the normal round loop (``Brain.run_task``: the system text, memory snapshot,
skills and tool routing a job gets) on a brain of the benchmark's own, so the conversation, the memory and the HUD's
turns are untouched. Each case gets a fresh model (nothing carries over between requests, as in a new conversation),
made ready off the clock (the model loaded with the start of the prompt, a Claude session started, as a spoken
question finds them): the clock runs from its first request to its last answer.

Paid requests: a run says how many it may make before it starts (``plan``: each case is one request at least, and at
most ``brain.max_tool_rounds`` + 2), OpenRouter's stay within its daily budget (``orion.spend``), and a brain that
can't go on (signed out, over budget, no such model) stops at its first such error, one that fails
``bench.stop_after_errors`` requests in a row (time-outs, say) at the last. One run at a time, and it waits between
cases while Orion answers you, so it never competes with you for a model.

Scores per brain: tool accuracy (the cases whose expected tools were all called; for a case that expects none, no
call at all), argument accuracy (each expected call matched by a call of its own whose arguments fit the tool, compared
as the tool reads them: numbers, lengths of time, choices and words), forbidden calls, the mean rounds, the first
token and the whole answer at the median and the 95th percentile, and the cost. A score is always a number: 0 where
nothing was measured, and ``answered`` (the cases a brain answered at all) says whether anything was.
"""

import asyncio
import copy
import dataclasses
import json
import logging
import math
import re
import time
from collections.abc import AsyncIterator, Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from orion.base import (
    JSON,
    CallContext,
    LLMError,
    LLMErrorKind,
    StepRecord,
    Tool,
    ToolOutput,
    ToolRequest,
    clip,
    number,
)
from orion.llm import LLM, PROVIDERS, SCRIPT, Piece, Prompt, build_llm, label

if TYPE_CHECKING:
    from orion.config import Config
    from orion.inference import Embedder
    from orion.memory import MemorySystem
    from orion.memory.store import BenchRow, MessageRow
    from orion.skills import Skills
    from orion.spend import Spend

log = logging.getLogger("orion.bench")

type Suite = Literal["builtin", "history"]
type Status = Literal["running", "done", "failed", "stopped"]

SUITE_PATH: Final = Path(__file__).with_name("bench") / "suite.yaml"
SUITES: Final[tuple[Suite, ...]] = ("builtin", "history")
HISTORY_SOURCES: Final = ("voice", "hud", "discord")  # requests you made (not jobs, not announcements)
HISTORY_SCAN: Final = 4  # requests read per one wanted: short ones, repeats and those whose tools are gone are left out
SOURCE: Final = "bench"  # a benchmark case's source, as its tool calls carry it
STOPS: Final[frozenset[LLMErrorKind]] = frozenset({"auth", "billing", "limit", "model", "unavailable"})  # end a brain
FAILURES_KEPT: Final = 50  # failed cases listed per brain
SHOWN_CHARS: Final = 60  # an argument as a failure shows it
INTERRUPTED: Final = "Orion stopped during the run."
_OUTER: Final = " .,;:!?\"'“”‘’()[]"  # punctuation around text that doesn't change what it says
_TRUE: Final = frozenset({"true", "yes", "1", "on"})
_FALSE: Final = frozenset({"false", "no", "0", "off"})
_BARE_NUMBER: Final = re.compile(r"\s*\d+(?:\.\d+)?\s*")


# ---------------------------------------------------------------------------------------------------- cases
class _Strict(BaseModel):
    """A part of the suite file: every key known, types as written."""

    model_config = ConfigDict(extra="forbid", strict=True)


class _ExpectSpec(_Strict):
    """A call a good brain makes: its tool, and the arguments that matter."""

    tool: Annotated[str, Field(min_length=1)]
    args: dict[str, Any] = {}


class _CaseSpec(_Strict):
    """One request of the suite."""

    say: Annotated[str, Field(min_length=1)]
    expect: list[_ExpectSpec] = []
    forbid: list[str] = []
    max_rounds: Annotated[int, Field(ge=1)] | None = None
    results: dict[str, str] = {}


class _DefaultsSpec(_Strict):
    """What a case doesn't say."""

    max_rounds: Annotated[int, Field(ge=1)]
    result: Annotated[str, Field(min_length=1)]


class _SuiteSpec(_Strict):
    """The suite file."""

    defaults: _DefaultsSpec
    cases: Annotated[list[_CaseSpec], Field(min_length=1)]


@dataclass(frozen=True, slots=True)
class Expect:
    """A call a good brain makes.

    Attributes:
        tool: the tool's name, as the model sees it.
        args: the arguments that matter (``arg_matches`` compares each).
    """

    tool: str
    args: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Case:
    """One request, and what a good answer does.

    Attributes:
        say: the request.
        expect: the calls a good brain makes (none: it answers without a tool).
        forbid: tools it must not call.
        max_rounds: the most rounds a good answer takes (None: not checked, the history suite).
        results: what each tool answers.
        result: what any other tool answers.
    """

    say: str
    expect: tuple[Expect, ...]
    forbid: frozenset[str]
    max_rounds: int | None
    results: Mapping[str, str]
    result: str

    def needs(self) -> set[str]:
        """The tools the case can't be run without (its expected ones)."""
        return {expect.tool for expect in self.expect}

    def typical_rounds(self) -> int:
        """The rounds a good answer takes: its ``max_rounds``, else a round per expected call and the answer."""
        return self.max_rounds if self.max_rounds is not None else len(self.expect) + 1


def load_suite(text: str) -> tuple[list[Case], str]:
    """Read and check a suite file.

    Args:
        text: the file's YAML.

    Returns:
        The cases, and what a tool answers when a case gives nothing for it.

    Raises:
        ValueError: what is wrong and where ("cases.3.expect.0.tool: ...").
    """
    try:
        spec = _SuiteSpec.model_validate(yaml.safe_load(text))
    except yaml.YAMLError as e:
        raise ValueError(f"the suite isn't YAML: {e}") from None
    except ValidationError as e:
        error = e.errors()[0]
        raise ValueError(f"{'.'.join(map(str, error['loc'])) or 'the suite'}: {error['msg']}") from None
    defaults = spec.defaults
    cases = [Case(say=case.say, expect=tuple(Expect(e.tool, e.args) for e in case.expect),
                  forbid=frozenset(case.forbid), max_rounds=case.max_rounds or defaults.max_rounds,
                  results=dict(case.results), result=defaults.result) for case in spec.cases]
    return cases, defaults.result


def history_cases(requests: Sequence[tuple["MessageRow", Sequence[StepRecord]]], *, min_words: int,
                  result: str) -> list[Case]:
    """Your latest requests as cases: each expects the tools the brain called for it then (not their arguments: those
    were that brain's own words), which answer what they answered then. Requests of fewer than ``min_words`` words
    and repeats (the latest kept) are left out.

    Args:
        requests: requests with their turns' tool calls, newest first (``Store.recent_requests``).
        min_words: the fewest words a request has.
        result: what a tool answers when it didn't answer then.

    Returns:
        The cases, newest first.
    """
    from orion.brain import FIND_TOOLS

    seen: set[str] = set()
    cases: list[Case] = []
    for message, steps in requests:
        say = " ".join(message["content"].split())
        if len(say.split()) < min_words or say.casefold() in seen:
            continue
        seen.add(say.casefold())
        called = [step for step in steps if step["name"] != FIND_TOOLS.name]
        results: dict[str, str] = {}
        for step in called:
            results.setdefault(step["name"], step["result"])
        cases.append(Case(say=say, expect=tuple(Expect(name, {}) for name in results), forbid=frozenset(),
                          max_rounds=None, results=results, result=result))
    return cases


# ---------------------------------------------------------------------------------------------------- matching
def arg_matches(expected: Any, given: Any, schema: Mapping[str, Any]) -> bool:
    """Whether an argument a brain gave matches the one a case expects, as the tool would read it.

    - A list: for a parameter that takes a list, each expected item must match one given; otherwise it lists the
      choices, any of which will do.
    - A mapping: each of its keys must be given and match.
    - A number or a yes/no: equal ("5" and "true" written as text count).
    - Text: a choice (the schema's enum) must be the same word; a length of time ("5 minutes") must come to the same
      seconds as Orion reads it (``organizer.parse_duration``: "300 seconds", "five minutes", "5"); any other text
      must be found in what was given, case, spaces and outer punctuation aside ("milk" in "Buy milk.").

    Args:
        expected: what the case expects.
        given: what the brain gave.
        schema: the parameter's JSON schema ({} when unknown).

    Returns:
        True when they match.
    """
    kind = _kind(schema)
    if isinstance(expected, list):
        if kind != "array":
            return any(arg_matches(choice, given, schema) for choice in expected)
        items = schema.get("items") if isinstance(schema.get("items"), Mapping) else {}
        return isinstance(given, list) and all(any(arg_matches(want, item, items) for item in given)
                                               for want in expected)
    if isinstance(expected, Mapping):
        properties = schema.get("properties") if isinstance(schema.get("properties"), Mapping) else {}
        return isinstance(given, Mapping) and all(
            key in given and arg_matches(want, given[key], properties.get(key) or {}) for key, want in expected.items())
    if isinstance(expected, bool):
        return _yes_no(given) is expected
    if isinstance(expected, int | float):
        found = _number(given)
        return found is not None and math.isclose(found, float(expected), rel_tol=1e-9, abs_tol=1e-9)
    if isinstance(expected, str):
        if isinstance(given, bool) or not isinstance(given, str | int | float):
            return False
        text = str(given)
        if _enum(schema):
            return _words(text) == _words(expected)
        if (seconds := _duration(expected)) is not None:
            from orion.tools.organizer import parse_duration

            found = parse_duration(text)
            return found is not None and math.isclose(found, seconds)
        return _words(expected) in _words(text)
    return bool(expected == given)


def _kind(schema: Mapping[str, Any]) -> str:
    """A parameter's JSON type ("string", "array"...; its non-null one for a nullable parameter; "" when unsaid)."""
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), "")
    if not kind and isinstance(options := schema.get("anyOf"), list):
        kind = next((_kind(option) for option in options if isinstance(option, Mapping) and
                     option.get("type") != "null"), "")
    return kind if isinstance(kind, str) else ""


def _enum(schema: Mapping[str, Any]) -> bool:
    """Whether a parameter takes one of a fixed list of words (an enum, maybe nullable)."""
    if "enum" in schema:
        return True
    options = schema.get("anyOf")
    return isinstance(options, list) and any(isinstance(o, Mapping) and "enum" in o for o in options)


def _words(text: str) -> str:
    """Text as it is compared: lower case, one space between words, no punctuation around it."""
    return " ".join(text.casefold().split()).strip(_OUTER)


def _duration(text: str) -> float | None:
    """The seconds a text says, when it says a length of time with its unit ("5 minutes"; a bare "5" doesn't)."""
    from orion.tools.organizer import parse_duration

    return None if _BARE_NUMBER.fullmatch(text) else parse_duration(text)


def _number(value: Any) -> float | None:
    """A number given as a number or as text; None for anything else."""
    if isinstance(value, str):
        try:
            return number(float(value))
        except ValueError:
            return None
    return number(value)


def _yes_no(value: Any) -> bool | None:
    """A yes or no given as a boolean or as a word; None for anything else."""
    if isinstance(value, bool):
        return value
    words = str(value).strip().casefold() if isinstance(value, str | int) else ""
    return True if words in _TRUE else False if words in _FALSE else None


# ---------------------------------------------------------------------------------------------------- the dry run
@dataclass(frozen=True, slots=True)
class Call:
    """A tool call a brain made in a case.

    Attributes:
        tool: the tool's name, as the model gave it.
        args: its arguments, as the model gave them.
        valid: whether they fit the tool's schema (a call to a tool that doesn't exist never does).
    """

    tool: str
    args: Mapping[str, Any]
    valid: bool


class DryHub:
    """A hub that runs nothing (see the module docstring). Its trace takes the brain's own steps (``find_tools``, a
    name no tool has) and shows nothing in the HUD. Use it on the event loop.

    Attributes:
        ops: the brain's own steps.
    """

    def __init__(self, cfg: "Config", tools: Mapping[str, Tool], *, redact: Callable[[str], str]) -> None:
        """Hold the tools.

        Args:
            cfg: the live settings (the trace's).
            tools: the running Orion's tools, by the name the model sees.
            redact: what a result passes through before a model reads it (as the real hub's results do).
        """
        from orion.mcp import OpTrace

        self.ops = OpTrace(lambda kind, /, **data: None, cfg.hud, recent=1, on_record=self._recorded)
        self.tools: Mapping[str, Tool] = dict(tools)
        self._redact = redact
        self._validators: dict[str, Any] = {}
        self._cases: dict[str, tuple[Case, list[Call]]] = {}

    def begin(self, turn: str, case: Case) -> None:
        """A case starts: its calls are answered from it and recorded.

        Args:
            turn: the case's turn id.
            case: the case.
        """
        self._cases[turn] = (case, [])

    def end(self, turn: str) -> list[Call]:
        """A case is over.

        Args:
            turn: its turn id.

        Returns:
            The calls its brain made, in order.
        """
        self.ops.end_turn(turn)
        return self._cases.pop(turn)[1]

    async def call_named_rich(self, req: ToolRequest, ctx: CallContext) -> ToolOutput:
        """Answer a call from its case, as the tool would: its arguments checked, then the case's result.

        Args:
            req: the tool's name and arguments.
            ctx: who asks (its turn names the case).

        Returns:
            The case's result for the tool, or the refusal the tool would give.
        """
        case, calls = self._cases[ctx.turn or ""]
        tool = self.tools.get(req.name)
        problem = self._problem(tool, req.args) if tool is not None else f"there is no tool called {req.name}."
        calls.append(Call(req.name, copy.deepcopy(req.args), valid=not problem))
        if problem:
            return ToolOutput(f"Error: Invalid arguments for {req.name}: {problem}" if tool is not None else
                              f"Error: {problem}")
        return ToolOutput(self._redact(case.results.get(req.name, case.result)))

    def end_turn(self, turn: str) -> None:
        """Nothing to forget: ``end`` did when the case was over."""

    def schema(self, tool: str) -> Mapping[str, Any]:
        """A tool's argument schema ({} for a tool that doesn't exist)."""
        found = self.tools.get(tool)
        return found.parameters if found is not None else {}

    def _recorded(self, record: StepRecord) -> None:
        """One of the brain's own steps: a name no tool has is a call of its case too (``find_tools`` is not)."""
        from orion.brain import FIND_TOOLS

        entry = self._cases.get(record["turn"])
        if entry is not None and record["name"] != FIND_TOOLS.name and record["name"] not in self.tools:
            entry[1].append(Call(record["name"], dict(record["args"]), valid=False))

    def _problem(self, tool: Tool, args: Mapping[str, Any]) -> str:
        """What is wrong with a call's arguments, as the tool would say it ("" when nothing). A number or a yes/no
        written as text ("40", "yes") is forgiven first, as the tools' own validation forgives it."""
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import SchemaError, best_match

        validator = self._validators.get(tool.name)
        if validator is None:
            try:
                Draft202012Validator.check_schema(tool.parameters)
                validator = Draft202012Validator(tool.parameters)
            except SchemaError:
                log.info("the schema of %s isn't JSON Schema: its arguments aren't checked", tool.name)
                validator = False
            self._validators[tool.name] = validator
        if validator is False:
            return ""
        error = best_match(validator.iter_errors(_lax(tool.parameters, args)))
        if error is None:
            return ""
        return f"{'.'.join(map(str, error.absolute_path)) or 'arguments'}: {error.message}"


def _lax(schema: Mapping[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
    """Arguments as the tools' own validation reads them: a whole number, a number or a yes/no written as text is the
    number or the yes/no (nothing else is changed: a list for a text is still wrong)."""
    properties = schema.get("properties") or {}
    out = dict(args)
    for key, value in args.items():
        kind = _kind(properties.get(key) or {})
        if kind == "integer" and isinstance(value, str) and (found := _number(value)) is not None and found.is_integer():
            out[key] = int(found)
        elif kind == "number" and isinstance(value, str) and (found := _number(value)) is not None:
            out[key] = found
        elif kind == "boolean" and isinstance(value, str) and (said := _yes_no(value)) is not None:
            out[key] = said
    return out


@dataclass(slots=True)
class _Clock:
    """One case's timing, as the probe takes it (monotonic seconds)."""

    started: float | None = None
    first: float | None = None
    ended: float | None = None
    rounds: int = 0
    cost: float = 0.0
    counted: object = None  # the last usage added (Claude reports one for the whole answer)


@dataclass(frozen=True, slots=True)
class Timing:
    """How a case went on the clock.

    Attributes:
        rounds: the model's rounds (requests).
        first_ms: from its first request to its first piece (text or tool calls); None when none came.
        total_ms: from its first request to the end of its last answer; None when it didn't end.
        cost: what it cost, in US dollars.
    """

    rounds: int
    first_ms: float | None
    total_ms: float | None
    cost: float


class _Probe(LLM):
    """The brain under test, as the benchmark's brain thinks with it: a fresh model for each case, made ready off the
    clock (loaded with the prompt's start, its session started), then timed from its first request to its last
    answer, its rounds and cost counted."""

    def __init__(self, cfg: "Config", make: Callable[[], LLM]) -> None:
        """Build the first model.

        Args:
            cfg: the settings the model runs with.
            make: builds a fresh model.
        """
        super().__init__(cfg)
        self._make = make
        self._inner = make()
        self._clock = _Clock()
        self._unused = True  # the model has been asked nothing yet

    @property
    def model(self) -> str:
        """The model's name."""
        return self._inner.model

    @property
    def all_tools(self) -> bool:
        """Whether the model is shown every tool."""
        return self._inner.all_tools

    def current(self) -> LLM:
        """The model itself."""
        return self._inner

    def sees(self) -> bool:
        """Whether the model reads pictures."""
        return self._inner.sees()

    def guard[T](self, items: AsyncIterator[T], *, whole: bool = False) -> AsyncIterator[T]:
        """No outer limits: each round is guarded inside ``stream``, after the model is ready."""
        return items

    async def fresh(self) -> None:
        """A model for the next case that has been asked nothing: nothing carries over between requests (the last one
        is closed)."""
        if not self._unused:
            await self._inner.close()
            self._inner = self._make()
        self._clock, self._unused = _Clock(), True

    def timing(self) -> Timing:
        """How the case so far went on the clock."""
        clock = self._clock
        start = clock.started
        return Timing(rounds=clock.rounds, cost=clock.cost,
                      first_ms=(clock.first - start) * 1000 if start is not None and clock.first is not None else None,
                      total_ms=(clock.ended - start) * 1000 if start is not None and clock.ended is not None else None)

    async def stream(self, prompt: Prompt) -> AsyncIterator[Piece]:
        """One round of the model, timed (its first round gets the model ready first, off the clock).

        Raises:
            LLMError: the model couldn't get ready (in time), or failed.
        """
        inner, clock = self._inner, self._clock
        self._unused = False
        if clock.started is None:
            limit = self.cfg.brain.first_chunk_timeout_s
            try:
                async with asyncio.timeout(limit):
                    await inner.warm_up(dataclasses.replace(prompt, messages=()))
                    await inner.ready(prompt)
            except TimeoutError:
                raise LLMError(f"{inner.speaker} wasn't ready within {limit:.0f} s.", "timeout") from None
            clock.started = time.monotonic()
        async for piece in inner.guard(inner.stream(prompt)):
            if clock.first is None:
                clock.first = time.monotonic()
            yield piece
        clock.rounds += 1
        clock.ended = time.monotonic()
        usage = inner.usage
        if usage is not None and usage is not clock.counted:
            clock.counted = usage
            clock.cost += usage.cost

    async def json(self, prompt: str, schema: Mapping[str, JSON], *, temperature: float) -> dict[str, JSON]:
        """The model's JSON answer (a benchmark asks none)."""
        return await self._inner.json(prompt, schema, temperature=temperature)

    async def warm_up(self, prompt: Prompt) -> None:
        """Get the model ready."""
        await self._inner.warm_up(prompt)

    async def prepare(self, prompt: Prompt) -> None:
        """Get the model ready without waiting."""
        await self._inner.prepare(prompt)

    def abort(self) -> None:
        """Stop the model's answer."""
        self._inner.abort()

    async def close(self) -> None:
        """Close the model."""
        await self._inner.close()


# ---------------------------------------------------------------------------------------------------- scoring
@dataclass(frozen=True, slots=True)
class Verdict:
    """How one case went on one brain.

    Attributes:
        tools_ok: every expected tool was called (none expected: no call at all); never with an error.
        args_ok: every expected call was made with its arguments (each matched by a call of its own); never with an
            error: a request that failed is not right on any count.
        forbidden: the calls of tools it must not call (also those made before an error).
        rounds_ok: within the case's rounds.
        error: the model's error ("" for none).
    """

    tools_ok: bool
    args_ok: bool
    forbidden: int
    rounds_ok: bool
    error: str

    @property
    def passed(self) -> bool:
        """Whether the case passed: right tools, right arguments, nothing forbidden, in time, no error."""
        return self.args_ok and not self.forbidden and self.rounds_ok and not self.error


def judge(case: Case, calls: Sequence[Call], rounds: int, error: str,
          schema: Callable[[str], Mapping[str, Any]]) -> Verdict:
    """Score one case.

    Args:
        case: the case.
        calls: the calls the brain made.
        rounds: its rounds.
        error: its model's error ("" for none).
        schema: a tool's argument schema, by its name.

    Returns:
        The verdict.
    """
    called = {call.tool for call in calls}
    tools_ok = not error and (all(expect.tool in called for expect in case.expect) if case.expect else not calls)
    used: set[int] = set()
    args_ok = tools_ok
    for expect in case.expect:
        match = next((i for i, call in enumerate(calls) if i not in used and call.tool == expect.tool and call.valid
                      and arg_matches(expect.args, call.args, schema(expect.tool))), None)
        if match is None:
            args_ok = False
            break
        used.add(match)
    return Verdict(tools_ok=tools_ok, args_ok=args_ok, forbidden=sum(call.tool in case.forbid for call in calls),
                   rounds_ok=case.max_rounds is None or rounds <= case.max_rounds, error=error)


def describe(tool: str, args: Mapping[str, Any]) -> str:
    """A call in a few words: ``set_timer(duration="5 minutes")``, each argument cut short."""

    def shown(value: Any) -> str:
        """An argument as JSON, cut to ``SHOWN_CHARS`` (a text is cut inside its quotes)."""
        if isinstance(value, str):
            return json.dumps(clip(value, SHOWN_CHARS, flat=False), ensure_ascii=False)
        return clip(json.dumps(value, ensure_ascii=False, default=str), SHOWN_CHARS)

    return f"{tool}({', '.join(f'{key}={shown(value)}' for key, value in args.items())})"


def failure(case: Case, calls: Sequence[Call], rounds: int, verdict: Verdict) -> dict[str, JSON]:
    """A failed case as the results list it: what was asked, what a good answer does, what this brain did.

    Returns:
        ``{say, expected, got}``.
    """
    expected = "; ".join(describe(e.tool, e.args) for e in case.expect) or "no tool"
    if case.forbid:
        expected += f"; never {', '.join(sorted(case.forbid))}"
    if verdict.error:
        got = f"error: {verdict.error}"
    else:
        got = "; ".join(describe(c.tool, c.args) + ("" if c.valid else " (invalid)") for c in calls) or "no tool"
        if not verdict.rounds_ok:
            got += f"; {rounds} rounds (at most {case.max_rounds})"
    return {"say": case.say, "expected": expected, "got": got}


def percentile(values: Sequence[float], q: float) -> float | None:
    """The q-th percentile of some values, by linear interpolation between the closest ranks (numpy's default).

    Args:
        values: the values.
        q: the percentile, 0 to 100.

    Returns:
        It, or None for no values.
    """
    if not values:
        return None
    ordered = sorted(values)
    place = (len(ordered) - 1) * q / 100
    low, high = math.floor(place), math.ceil(place)
    return ordered[low] + (ordered[high] - ordered[low]) * (place - low)


@dataclass(slots=True)
class _Tally:
    """One brain's scores as its cases come in."""

    brain: str
    label: str
    paid: str  # where its requests are paid: "plan", "money" or "" (free)
    cases: int = 0
    passed: int = 0
    tools_ok: int = 0
    args_ok: int = 0
    forbidden: int = 0
    errors: int = 0
    skipped: int = 0
    rounds: list[int] = field(default_factory=list)
    firsts: list[float] = field(default_factory=list)
    totals: list[float] = field(default_factory=list)
    cost: float = 0.0
    error: str = ""
    failures: list[dict[str, JSON]] = field(default_factory=list)

    def add(self, case: Case, calls: Sequence[Call], timing: Timing, verdict: Verdict) -> None:
        """Count one case."""
        self.cases += 1
        self.passed += verdict.passed
        self.tools_ok += verdict.tools_ok
        self.args_ok += verdict.args_ok
        self.forbidden += verdict.forbidden
        self.errors += bool(verdict.error)
        self.cost += timing.cost
        if not verdict.error:
            self.rounds.append(timing.rounds)
            if timing.first_ms is not None:
                self.firsts.append(timing.first_ms)
            if timing.total_ms is not None:
                self.totals.append(timing.total_ms)
        if not verdict.passed and len(self.failures) < FAILURES_KEPT:
            self.failures.append(failure(case, calls, timing.rounds, verdict))

    def view(self) -> dict[str, JSON]:
        """The scores, as the HUD and the run's record keep them: accuracies 0 to 1, times in ms, all numbers (0
        where nothing was measured: ``cases`` and ``answered`` say whether anything was)."""

        def share(count: int) -> float:
            """A fraction of the cases run."""
            return round(count / self.cases, 4) if self.cases else 0.0

        def ms(values: Sequence[float], q: float) -> float:
            """A percentile of times, in ms to a tenth."""
            found = percentile(values, q)
            return round(found, 1) if found is not None else 0.0

        return {"brain": self.brain, "label": self.label, "paid": self.paid, "cases": self.cases,
                "answered": len(self.rounds), "passed": self.passed, "tool_accuracy": share(self.tools_ok),
                "args_accuracy": share(self.args_ok), "forbidden": self.forbidden,
                "rounds": round(sum(self.rounds) / len(self.rounds), 2) if self.rounds else 0.0,
                "p50_first_token_ms": ms(self.firsts, 50), "p95_first_token_ms": ms(self.firsts, 95),
                "p50_total_ms": ms(self.totals, 50), "p95_total_ms": ms(self.totals, 95),
                "cost": round(self.cost, 6), "errors": self.errors, "skipped": self.skipped, "error": self.error,
                "failures": list(self.failures)}


# ---------------------------------------------------------------------------------------------------- the plan
@dataclass(frozen=True, slots=True)
class BrainPlan:
    """One brain of a run.

    Attributes:
        brain: as asked ("claude", "ollama:qwen3.5:9b", "script:my.yaml").
        provider: its provider.
        cfg: the settings it runs with (the live ones, its provider's model or script set as asked).
        label: its name for people.
        paid: where its requests are paid: "plan" (your Claude plan), "money" (OpenRouter), "" (free).
        where: where it runs, in words.
        cloud: whether it runs on someone else's servers (Claude, OpenRouter, free models too): your requests go there.
        typical: the requests a good brain makes.
        most: the most requests it may make.
        ready: whether it can run (an OpenRouter budget already spent can't).
        why: why not ("" when it can).
    """

    brain: str
    provider: str
    cfg: "Config"
    label: str
    paid: Literal["plan", "money", ""]
    where: str
    cloud: bool
    typical: int
    most: int
    ready: bool
    why: str

    def view(self) -> dict[str, JSON]:
        """The brain's plan for the HUD and the CLI."""
        return {"brain": self.brain, "label": self.label, "paid": self.paid, "where": self.where, "cloud": self.cloud,
                "requests": {"typical": self.typical, "most": self.most}, "ready": self.ready, "why": self.why}


@dataclass(frozen=True, slots=True)
class Plan:
    """What a run will do, said before it starts.

    Attributes:
        suite: the suite.
        cases: the cases it runs, on every brain.
        skipped: the cases left out, as (request, why).
        brains: the brains.
        tools: the toolbox it was made for (a run uses these tools).
        version: that toolbox's version.
    """

    suite: Suite
    cases: tuple[Case, ...]
    skipped: tuple[tuple[str, str], ...]
    brains: tuple[BrainPlan, ...]
    tools: Mapping[str, Tool]
    version: int

    def view(self) -> dict[str, JSON]:
        """The plan for the HUD and the CLI: ``paid_requests`` sums the brains that cost money or your plan."""
        paid = [brain for brain in self.brains if brain.paid and brain.ready]
        return {"suite": self.suite, "cases": len(self.cases), "dry_run": True,
                "skipped": [{"say": say, "why": why} for say, why in self.skipped],
                "brains": [brain.view() for brain in self.brains],
                "paid_requests": {"typical": sum(b.typical for b in paid), "most": sum(b.most for b in paid)}}


def brain_plan(cfg: "Config", brain: str, cases: Sequence[Case], *, spent: float) -> BrainPlan:
    """One brain of a run: its settings, where its requests are paid, how many it may make, and whether it can run.

    Args:
        cfg: the live settings.
        brain: "provider" or "provider:model" ("script:path" for another script).
        cases: the cases it runs.
        spent: what OpenRouter cost today.

    Returns:
        The brain's plan.

    Raises:
        ValueError: no such provider, or a model its settings refuse.
    """
    provider, _, model = brain.strip().partition(":")
    if provider not in PROVIDERS:
        raise ValueError(f"There's no brain called {provider or brain!r}: say {', '.join(PROVIDERS)}, with a model "
                         "after a colon if you like (ollama:qwen3.5:9b).")
    view = _with_model(cfg, provider, model.strip()) if model.strip() else cfg
    name = view.claude.model if provider == "claude" else view.openrouter.model if provider == "openrouter" else ""
    paid: Literal["plan", "money", ""] = (
        "plan" if provider == "claude" else "money" if provider == "openrouter" and not name.endswith(":free") else "")
    where = {"claude": "your Claude plan", "openrouter": "OpenRouter", "ollama": "this PC", SCRIPT: "no model"}
    budget = cfg.openrouter.daily_budget_usd
    over = paid == "money" and 0 < budget <= spent
    rounds = cfg.brain.max_tool_rounds + 2  # _rounds asks at most this many times per request
    return BrainPlan(brain=brain.strip(), provider=provider, cfg=view, label=label(view, provider), paid=paid,
                     where=where[provider], cloud=provider in ("claude", "openrouter"),
                     typical=sum(case.typical_rounds() for case in cases), most=len(cases) * rounds, ready=not over,
                     why=f"today's OpenRouter budget is spent (${spent:.2f} of ${budget:.2f})" if over else "")


def _with_model(cfg: "Config", provider: str, model: str) -> "Config":
    """The settings with another model (or script) for one provider, checked as its settings check it; the other
    sections stay the live ones.

    Raises:
        ValueError: the model is one the provider's settings refuse.
    """
    option = "path" if provider == SCRIPT else "model"
    section = getattr(cfg, provider)
    try:
        changed = type(section).model_validate({**section.model_dump(), option: model}, strict=True)
    except ValidationError as e:
        error = e.errors()[0]
        raise ValueError(f"{provider}:{model}: {error['msg']}") from None
    return cfg.model_copy(update={provider: changed})


def plan_text(plan: Mapping[str, Any]) -> str:
    """A plan in words, as the CLI and Orion's tool say it before a run.

    Args:
        plan: ``Plan.view()``.

    Returns:
        The words.
    """
    history = plan["suite"] == "history"
    suite = "your latest requests" if history else "the built-in suite"
    lines = [f"Benchmark on {suite}: {plan['cases']} requests per brain, a dry run (no tool really runs)."]
    for brain in plan["brains"]:
        requests = brain["requests"]
        cost = {"plan": f"on your Claude plan: about {requests['typical']} requests, at most {requests['most']}",
                "money": f"paid: about {requests['typical']} requests, at most {requests['most']}",
                "": "free"}[brain["paid"]]
        sent = f" Your requests are sent to {brain['where']}." if history and brain["cloud"] else ""
        state = "" if brain["ready"] else f" Won't run: {brain['why']}."
        lines.append(f"- {brain['brain']} ({brain['label']}, {brain['where']}): {cost}.{sent}{state}")
    if skipped := plan["skipped"]:
        lines.append(f"Left out: {len(skipped)} requests whose tools aren't here ("
                     f"{'; '.join(item['why'] for item in skipped[:3])}{'...' if len(skipped) > 3 else ''}).")
    return "\n".join(lines)


def results_text(run: Mapping[str, Any]) -> str:
    """A run's scores in words, as the CLI and Orion's tool say them.

    Args:
        run: a run's view (``Bench.runs``).

    Returns:
        The words.
    """

    def seconds(ms: float) -> str:
        """A time to say."""
        return f"{ms / 1000:.2f} s" if ms >= 100 else f"{ms:.1f} ms"

    status = {"running": f"running, {run['progress']:.0%} done", "done": "done", "failed": "failed",
              "stopped": "stopped before the end"}[run["status"]]
    lines = [f"Benchmark #{run['id']} ({run['suite']}, {status})" + (f": {run['error']}" if run["error"] else ":")]
    for brain in run["results"]:
        head = f"- {brain['brain']} ({brain['label']})"
        if not brain["cases"]:
            lines.append(f"{head}: no request asked" + (f": {brain['error']}" if brain["error"] else "") + ".")
            continue
        cost = f"; ${brain['cost']:.4f}" if brain["paid"] == "money" else ""
        said = (f"{brain['rounds']:g} rounds; first token {seconds(brain['p50_first_token_ms'])} (95%: "
                f"{seconds(brain['p95_first_token_ms'])}), whole answer {seconds(brain['p50_total_ms'])} (95%: "
                f"{seconds(brain['p95_total_ms'])})") if brain["answered"] else "no answer"
        lines.append(f"{head}: {brain['passed']} of {brain['cases']} passed; right tools "
                     f"{brain['tool_accuracy']:.0%}, right arguments {brain['args_accuracy']:.0%}, "
                     f"{brain['forbidden']} forbidden calls; {said}{cost}"
                     + (f"; {brain['errors']} errors" if brain["errors"] else "")
                     + (f"; stopped: {brain['error']}" if brain["error"] else "") + ".")
        lines += [f"  · “{f['say']}”: expected {f['expected']}; got {f['got']}." for f in brain["failures"][:5]]
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------------- the service
class _Tools(Protocol):
    """What a run needs of the hub (``mcp.MCPHub``)."""

    @property
    def version(self) -> int:
        """The toolbox's version."""

    def tools(self) -> Mapping[str, Tool]:
        """The tools the model may use."""


class _Turns(Protocol):
    """What a run needs of the turn runner (``brain.TurnRunner``)."""

    async def idle(self) -> None:
        """Return once no turn runs and its reply has been said."""


class _Spawner(Protocol):
    """The runtime's supervisor."""

    def spawn[T](self, job: Coroutine[Any, Any, T], *, name: str) -> "asyncio.Task[T]":
        """Run background work, kept referenced and logged if it fails."""


class _Activity(Protocol):
    """The activity log (``orion.activity.Activity``)."""

    def record(self, kind: Any, *, source: str, title: str, outcome: str = "", detail: str = "",
               ms: int = 0) -> None:
        """Add an entry."""


class _Rewarm(Protocol):
    """The conversation's brain (``brain.Brain``)."""

    async def rewarm(self) -> None:
        """Read the conversation again into a local model's cache (a benchmark took it)."""


class Host(Protocol):
    """What the benchmark needs of the running Orion (``runtime.Runtime`` provides it)."""

    cfg: "Config"
    memory: "MemorySystem"
    skills: "Skills"
    embedder: "Embedder"
    hub: _Tools
    spend: "Spend"
    bus: Any  # the event bus (``publish``)
    supervisor: _Spawner
    turns: _Turns
    activity: _Activity
    brain: _Rewarm


@dataclass(eq=False, slots=True)
class _Run:
    """A run under way."""

    id: int
    at: float
    plan: Plan
    source: str
    tallies: list[_Tally]
    done: int = 0
    status: Status = "running"
    error: str = ""
    task: "asyncio.Task[None] | None" = None

    @property
    def total(self) -> int:
        """The cases over every brain."""
        return len(self.plan.cases) * len(self.plan.brains)

    def stored(self) -> dict[str, JSON]:
        """What its record's results keep: the progress, the scores, the plan and the error."""
        return {"progress": round(self.done / self.total, 4) if self.total else 1.0,
                "results": [tally.view() for tally in self.tallies], "plan": self.plan.view(), "error": self.error}

    def view(self) -> dict[str, JSON]:
        """The run as the HUD shows it (a BenchRun)."""
        return {"id": self.id, "at": self.at, "suite": self.plan.suite, "brains": [b.brain for b in self.plan.brains],
                "status": self.status, **self.stored()}


def run_view(row: "BenchRow") -> dict[str, JSON]:
    """A recorded run as the HUD shows it (a BenchRun).

    Args:
        row: its record.

    Returns:
        ``{id, at, suite, brains, status, progress, results, plan, error}``.
    """
    results = row["results"]
    return {"id": row["id"], "at": row["at"], "suite": row["suite"], "brains": list(row["brains"]),
            "status": row["status"], "progress": results.get("progress", 1.0), "results": results.get("results", []),
            "plan": results.get("plan", {}), "error": results.get("error", "")}


class BenchBusy(ValueError):
    """A benchmark is running already: one at a time."""


class Bench:
    """Benchmarks of the brains (see the module docstring). Use it on the event loop."""

    def __init__(self, host: Host, *, redact: Callable[[str], str]) -> None:
        """Hold the running Orion; runs a previous Orion left unfinished are marked failed.

        Args:
            host: the running Orion.
            redact: what a tool result passes through before a model reads it (the real hub's redaction).
        """
        self.host = host
        self._redact = redact
        self._run: _Run | None = None
        store = host.memory.store
        store.submit(store.fail_unfinished_bench_runs, INTERRUPTED)

    @property
    def running(self) -> _Run | None:
        """The run under way, None when none is."""
        return self._run

    async def plan(self, brains: Sequence[str], suite: Suite | None = None, limit: int | None = None) -> Plan:
        """What a run of these brains on this suite would do: its cases, those left out, and each brain's requests.

        Args:
            brains: the brains ("claude", "ollama:qwen3.5:9b", ...); none: the one he thinks with.
            suite: "builtin" or "history" (None: ``bench.suite``).
            limit: the most cases (None: the whole suite, or ``bench.history_requests`` of yours).

        Returns:
            The plan.

        Raises:
            ValueError: an unknown brain or suite, a bad limit, or nothing to run.
        """
        host = self.host
        suite = suite or host.cfg.bench.suite
        if suite not in SUITES:
            raise ValueError(f"There's no {suite!r} suite: say builtin or history.")
        if limit is not None and limit < 1:
            raise ValueError("A benchmark runs at least one case.")
        if suite == "history":
            limit = host.cfg.bench.history_requests if limit is None else limit
            cases = await asyncio.to_thread(self._history, limit)
        else:
            cases = await asyncio.to_thread(self._builtin)
        tools, version = dict(host.hub.tools()), host.hub.version
        runnable: list[Case] = []
        skipped: list[tuple[str, str]] = []
        for case in cases:
            if limit is not None and len(runnable) >= limit:
                break  # the first cases that can run: those after are not left out, only not asked for
            if missing := sorted(case.needs() - tools.keys()):
                skipped.append((case.say, f"needs {', '.join(missing)}, which isn't here"))
            else:
                runnable.append(case)
        if not runnable:
            raise ValueError("Nothing to run: " + ("no request of yours is long enough to replay." if suite == "history"
                                                   and not cases else "the requests' tools aren't here."))
        spent = host.spend.today()
        asked = dict.fromkeys(brains or [host.cfg.brain.provider])
        planned = [brain_plan(host.cfg, brain, runnable, spent=spent) for brain in asked]
        return Plan(suite=suite, cases=tuple(runnable), skipped=tuple(skipped), brains=tuple(planned), tools=tools,
                    version=version)

    async def start(self, brains: Sequence[str], suite: Suite | None = None, limit: int | None = None, *,
                    source: str) -> dict[str, JSON]:
        """Start a run in the background (see ``plan``); its progress comes as ``bench`` events.

        Args:
            brains: the brains (none: the one he thinks with).
            suite: "builtin" or "history" (None: ``bench.suite``).
            limit: the most cases.
            source: who asked, as the activity log says it (``activity.SOURCES``: "hud", "voice", "app"...).

        Returns:
            The run, as it starts.

        Raises:
            BenchBusy: a run is under way.
            ValueError: as ``plan``; or no brain can run (an OpenRouter budget already spent, say).
        """
        from orion.activity import SOURCES

        if self._run is not None:
            raise BenchBusy(f"Benchmark #{self._run.id} is running ({self._run.done} of {self._run.total} cases): "
                            "wait for it, or stop it.")
        if source not in SOURCES:
            raise ValueError(f"A benchmark can't be asked for by {source!r}.")
        plan = await self.plan(brains, suite, limit)
        if self._run is not None:  # one started while the plan was made
            raise BenchBusy(f"Benchmark #{self._run.id} is running: wait for it, or stop it.")
        if not any(brain.ready for brain in plan.brains):
            raise ValueError("No brain can run: " + "; ".join(f"{b.brain}: {b.why}" for b in plan.brains) + ".")
        store, at = self.host.memory.store, time.time()
        run = _Run(0, at, plan, source, [_Tally(brain.brain, brain.label, brain.paid) for brain in plan.brains])
        self._run = run
        try:
            run.id = await store.call(store.add_bench_run, at=at, suite=plan.suite,
                                      brains=[b.brain for b in plan.brains], results=run.stored())
        except BaseException:
            self._run = None
            raise
        run.task = self.host.supervisor.spawn(self._go(run), name=f"benchmark {run.id}")
        run.task.add_done_callback(lambda _task: self._ended(run))
        self._publish(run)
        return run.view()

    def stop(self, run_id: int) -> bool:
        """Stop a run under way (what it scored so far is kept, as failed).

        Args:
            run_id: the run.

        Returns:
            Whether it was running.
        """
        run = self._run
        if run is None or run.id != run_id or run.task is None or run.task.done():
            return False
        run.task.cancel()
        return True

    async def runs(self) -> list[dict[str, JSON]]:
        """The latest runs, newest first (the one under way as it is now).

        Returns:
            Their views.
        """
        store = self.host.memory.store
        rows = await store.call(store.bench_runs, self.host.cfg.bench.keep_runs)  # sees the run saved a moment ago
        live = self._run
        return [live.view() if live is not None and row["id"] == live.id else run_view(row) for row in rows]

    async def run(self, run_id: int) -> dict[str, JSON] | None:
        """One run (the one under way as it is now).

        Args:
            run_id: the run.

        Returns:
            Its view, None when there is none.
        """
        live = self._run
        if live is not None and live.id == run_id:
            return live.view()
        store = self.host.memory.store
        row = await store.call(store.bench_run, run_id)
        return run_view(row) if row is not None else None

    # ------------------------------------------------------------------------------------------------ internals
    def _builtin(self) -> list[Case]:
        """The built-in suite's cases (in a worker thread: it reads the file).

        Raises:
            ValueError: the suite file is broken.
        """
        return load_suite(SUITE_PATH.read_text(encoding="utf-8"))[0]

    def _history(self, wanted: int) -> list[Case]:
        """Candidates for ``wanted`` cases of your own latest requests, newest first (in a worker thread: it reads the
        database). It reads a few times as many requests as wanted: short ones and repeats are left out, and so are
        those whose tools aren't here any more (``plan`` does that), and ``wanted`` are still to run.

        Raises:
            ValueError: the suite file is broken.
        """
        result = load_suite(SUITE_PATH.read_text(encoding="utf-8"))[1]
        store = self.host.memory.store
        return history_cases(store.recent_requests(wanted * HISTORY_SCAN, HISTORY_SOURCES),
                             min_words=self.host.cfg.bench.history_min_words, result=result)

    async def _go(self, run: _Run) -> None:
        """Run every brain on every case, then record how it went."""
        from orion.brain.context import Context

        host = self.host
        started = time.monotonic()
        try:
            context = Context(host.cfg, host.memory, host.skills, host.embedder)
            hub = DryHub(host.cfg, run.plan.tools, redact=self._redact)
            for index, (brain, tally) in enumerate(zip(run.plan.brains, run.tallies, strict=True)):
                await self._brain(run, index, brain, tally, context, hub)
            run.status = "done"
        except asyncio.CancelledError:
            run.status = "stopped"
            raise
        except Exception as e:
            log.exception("benchmark #%d failed", run.id)
            run.status, run.error = "failed", f"{type(e).__name__}: {e}"
        finally:
            self._finish(run, time.monotonic() - started)

    async def _brain(self, run: _Run, index: int, plan: BrainPlan, tally: _Tally, context: Any,
                     hub: DryHub) -> None:
        """Run one brain on every case. A brain that can't run has all its cases skipped; one that can't go on (its
        error is one only a person can mend, or it failed ``bench.stop_after_errors`` requests in a row) the rest."""
        from orion.brain import Brain

        cases = run.plan.cases
        if not plan.ready:
            tally.error, tally.skipped = plan.why, len(cases)
            run.done += len(cases)
            self._publish(run)
            return
        host = self.host

        def make() -> LLM:
            """A fresh model of this brain, its requests counted as the benchmark's."""
            return build_llm(plan.cfg, plan.provider, meter=host.spend, purpose="bench")

        probe = _Probe(plan.cfg, make)
        failed = 0  # requests failed in a row
        try:
            brain = await asyncio.to_thread(Brain, host.cfg, host.memory, host.skills, host.embedder, context, hub,
                                            probe, host.supervisor.spawn)
            brain.set_tools(run.plan.tools, run.plan.version)
            for n, case in enumerate(cases):
                await host.turns.idle()  # never while Orion answers you
                await probe.fresh()
                turn = f"bench-{run.id}-{index}-{n}"
                hub.begin(turn, case)
                error, kind = "", None
                try:
                    await brain.run_task(case.say, llm=probe, turn=turn, source=SOURCE,
                                         limit=host.cfg.brain.max_tool_rounds)
                except LLMError as e:
                    error, kind = str(e) or type(e).__name__, e.kind
                calls = hub.end(turn)
                timing = probe.timing()
                tally.add(case, calls, timing, judge(case, calls, timing.rounds, error, hub.schema))
                run.done += 1
                failed = failed + 1 if error else 0
                if kind in STOPS or failed >= host.cfg.bench.stop_after_errors:
                    tally.error, tally.skipped = error, len(cases) - n - 1
                    run.done += tally.skipped
                    self._publish(run)
                    return
                self._publish(run)
        finally:
            await probe.close()

    def _ended(self, run: _Run) -> None:
        """A run's task is over. One that never got to finish itself (it was stopped before its first step) is
        finished here, so a run can never be left holding the next one back."""
        if self._run is run:
            run.status = "stopped"
            self._finish(run, 0.0)

    def _finish(self, run: _Run, seconds: float) -> None:
        """A run is over, or stopped: keep it, tell the HUD and the activity log, and give the conversation its cache
        in Ollama back if a benchmark of Ollama took Ollama's one cache slot."""
        host = self.host
        store = host.memory.store
        self._run = None
        store.submit(store.save_bench_run, run.id, status=run.status, results=run.stored())
        store.submit(store.prune_bench_runs, host.cfg.bench.keep_runs)
        self._publish(run)
        if any(brain.provider == "ollama" for brain in run.plan.brains):
            host.supervisor.spawn(host.brain.rewarm(), name="read the conversation again")
        host.activity.record("bench", source=run.source, outcome={"done": "ok", "failed": "failed"}.get(run.status, ""),
                             title=f"Benchmark of {', '.join(b.label for b in run.plan.brains)}: {run.status}",
                             detail=results_text(run.view()), ms=round(seconds * 1000))

    def _publish(self, run: _Run) -> None:
        """Tell the HUD how a run stands (a ``bench`` event)."""
        self.host.bus.publish("bench", **run.view())
