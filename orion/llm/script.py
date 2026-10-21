"""A brain with no model: replies and tool calls replayed from a YAML file (``script.path``), so the questions before
risky actions, jobs, the fallback brain and the benchmark can be tried end to end without your plan, a key or a GPU.

The script is a list of rules, tried in order; the first whose ``when`` (a regular expression, any case) is found in
the request answers it::

    sees: false                          # whether it "reads" pictures (what ``sees()`` says)
    rules:
      - when: "weather"
        delay_ms: 300                    # a wait before each round's first piece, as a model thinks
        rounds:
          - say: "Let me look."          # streamed word by word...
            tools:                       # ...then the round's tool calls, run by the brain as any model's are
              - {name: get_weather, args: {location: Athens}}
          - say: "Sunny, 24 degrees."
      - when: "out of credits"
        fail: billing                    # fails before its first word: the fallback brain takes over
      - when: "long-term memory"         # a JSON request (the memory review) is answered by a json rule
        json: {ops: [], journal: "", concepts: []}

A rule answers with ``rounds``, or one ``say``, or a ``fail`` (limit, billing, auth, unavailable or timeout: the
kinds of error ``brain.fallback_on`` names), or ``json``. The brain asks again after running each round's tools, and
the next round answers: its own messages so far say which, so the brain keeps no state of its own, and any number of
requests (a job, a benchmark) can share it. A round may ``fail`` after its words, as a model that breaks mid-answer;
past its last round a rule says no more. A request no rule matches is an error that says so. The file is read again,
in a worker thread, whenever it changes.
"""

import asyncio
import copy
import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from orion.base import JSON, LLMError, ToolCall, clip, file_stamp, new_id
from orion.llm import LLM, SCRIPT, Piece, Prompt

if TYPE_CHECKING:
    from orion.config import Config

type Failure = Literal["limit", "billing", "auth", "unavailable", "timeout"]

FAILED: Final = "The scripted brain failed on purpose ({kind}), as its script says."
REQUEST_CHARS: Final = 80  # a request as an error about it quotes it
_WORD: Final = re.compile(r"\S+\s*")  # a reply streams one word (with the space after it) at a time
_LOADER: Final = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


# ---------------------------------------------------------------------------------------------------- the file
class _Strict(BaseModel):
    """A part of a script: every key known, types as written."""

    model_config = ConfigDict(extra="forbid", strict=True)


class _CallSpec(_Strict):
    """A tool call: the tool's name as the model sees it, and its arguments."""

    name: Annotated[str, Field(min_length=1)]
    args: dict[str, Any] = {}


class _RoundSpec(_Strict):
    """One round: words, then tool calls; or words, then a failure."""

    say: str = ""
    tools: list[_CallSpec] = []
    fail: Failure | None = None
    delay_ms: Annotated[int, Field(ge=0)] | None = None

    @model_validator(mode="after")
    def _one_way(self) -> "_RoundSpec":
        """A round does something, and a round that fails calls no tools."""
        if not (self.say or self.tools or self.fail):
            raise ValueError("a round says something, calls tools or fails")
        if self.fail and self.tools:
            raise ValueError("a round that fails calls no tools")
        return self


class _RuleSpec(_Strict):
    """One rule: when it answers, and how."""

    when: Annotated[str, Field(min_length=1)]
    rounds: Annotated[list[_RoundSpec], Field(min_length=1)] | None = None
    say: str = ""
    fail: Failure | None = None
    answer: dict[str, Any] | None = Field(default=None, alias="json")
    delay_ms: Annotated[int, Field(ge=0)] = 0

    @field_validator("when")
    @classmethod
    def _pattern(cls, when: str) -> str:
        """``when`` is a regular expression."""
        try:
            re.compile(when)
        except re.error as e:
            raise ValueError(f"when isn't a regular expression: {e}") from None
        return when

    @model_validator(mode="after")
    def _one_answer(self) -> "_RuleSpec":
        """A rule answers one way."""
        ways = [name for name, given in (("rounds", self.rounds is not None), ("say", bool(self.say)),
                                         ("fail", self.fail is not None), ("json", self.answer is not None)) if given]
        if len(ways) != 1:
            raise ValueError(f"a rule answers with one of rounds, say, fail or json; this one has "
                             f"{' and '.join(ways) or 'none'}")
        return self


class _ScriptSpec(_Strict):
    """A whole script."""

    sees: bool = False
    rules: Annotated[list[_RuleSpec], Field(min_length=1)]


@dataclass(frozen=True, slots=True)
class Round:
    """One round as it is played.

    Attributes:
        say: the words streamed first.
        calls: the tool calls that end the round, as (name, arguments).
        fail: the kind of failure raised after the words, None for none.
        delay_s: the wait before the first piece.
    """

    say: str
    calls: tuple[tuple[str, Mapping[str, Any]], ...]
    fail: Failure | None
    delay_s: float


@dataclass(frozen=True, slots=True)
class Rule:
    """One rule as it is played.

    Attributes:
        when: the pattern looked for in the request.
        rounds: what it answers, round by round (none for a JSON rule).
        answer: what it answers a JSON request, None for a rule that streams.
    """

    when: re.Pattern[str]
    rounds: tuple[Round, ...]
    answer: Mapping[str, Any] | None


@dataclass(frozen=True, slots=True)
class Script:
    """A script, read and checked.

    Attributes:
        sees: whether it says it reads pictures.
        rules: its rules, in order.
    """

    sees: bool
    rules: tuple[Rule, ...]

    def rule(self, text: str, *, json: bool) -> Rule | None:
        """The first rule that answers a request.

        Args:
            text: the request (a JSON request's whole prompt).
            json: whether the request wants one JSON object (only rules with ``json`` answer it) or a streamed
                answer (only the others do).

        Returns:
            The rule, None when none matches.
        """
        return next((rule for rule in self.rules if (rule.answer is not None) == json and rule.when.search(text)),
                    None)


def parse_script(text: str) -> Script:
    """Read and check a script.

    Args:
        text: the file's YAML.

    Returns:
        The script.

    Raises:
        ValueError: what is wrong and where ("line 3, column 5: ...", "rules.2.rounds.1: ...").
    """
    try:
        data = yaml.load(text, Loader=_LOADER)
    except yaml.MarkedYAMLError as e:
        mark = e.problem_mark or e.context_mark
        where = f"line {mark.line + 1}, column {mark.column + 1}: " if mark else ""
        raise ValueError(f"{where}{e.problem or e.context or 'invalid YAML'}") from None
    except yaml.YAMLError as e:
        raise ValueError(str(e)) from None
    try:
        spec = _ScriptSpec.model_validate(data)
    except ValidationError as e:
        error = e.errors()[0]
        where = ".".join(str(part) for part in error["loc"]) or "the script"
        raise ValueError(f"{where}: {error['msg'].removeprefix('Value error, ')}") from None
    return Script(spec.sees, tuple(_rule(rule) for rule in spec.rules))


def _rule(spec: _RuleSpec) -> Rule:
    """A checked rule as it is played: a lone ``say`` or ``fail`` is a rule of one round."""
    rounds = spec.rounds if spec.rounds is not None else (
        [_RoundSpec(say=spec.say)] if spec.say else [_RoundSpec(fail=spec.fail)] if spec.fail else [])
    played = tuple(Round(say=r.say, calls=tuple((call.name, call.args) for call in r.tools), fail=r.fail,
                         delay_s=(r.delay_ms if r.delay_ms is not None else spec.delay_ms) / 1000) for r in rounds)
    return Rule(re.compile(spec.when, re.IGNORECASE), played, spec.answer)


def request_of(prompt: Prompt) -> str:
    """The words of the request a prompt answers: its first user message, without the per-turn tail.

    Args:
        prompt: the prompt.

    Returns:
        The words ("" when it has no user message).
    """
    for message in prompt.messages:
        if message["role"] == "user":
            return message["raw"] if "raw" in message else message["content"]
    return ""


# ---------------------------------------------------------------------------------------------------- the brain
class ScriptLLM(LLM):
    """The brain with no model (see the module docstring)."""

    provider = SCRIPT
    speaker = "The script"

    def __init__(self, cfg: "Config") -> None:
        """Nothing is read until the script is first needed.

        Args:
            cfg: the live settings (``script.path``, read again at each use).
        """
        super().__init__(cfg)
        self._loaded: tuple[Path, tuple[int, int, int], Script] | None = None  # the file, its stamp, its script

    @property
    def model(self) -> str:
        """``script.path``."""
        return self.cfg.script.path

    @property
    def all_tools(self) -> bool:
        """A script names its tools itself: it is offered every tool, unrouted."""
        return True

    async def warm_up(self, prompt: Prompt) -> None:
        """Read and check the script now, so a mistake in it shows at once.

        Raises:
            LLMError: there is no script, or it can't be used (kind "model": the fallback brain doesn't hide it).
        """
        self._sees = (await self._script()).sees

    async def stream(self, prompt: Prompt) -> AsyncIterator[Piece]:
        """Play the round of the rule that answers this request (see ``LLM.stream``).

        Raises:
            LLMError: no script, no rule for the request, or a round that fails as the script says.
        """
        script = await self._script()
        self._sees = script.sees
        request = request_of(prompt)
        rule = script.rule(request, json=False)
        if rule is None:
            raise LLMError(f"The script {self.cfg.script_path.name} has no rule for “{clip(request, REQUEST_CHARS)}”.")
        played = sum(message["role"] == "assistant" for message in prompt.messages)
        if played >= len(rule.rounds):
            return  # past its last round: the answer ends
        step = rule.rounds[played]
        if step.delay_s:
            await asyncio.sleep(step.delay_s)
        for word in _WORD.findall(step.say):
            yield word
            await asyncio.sleep(0)  # as a stream does: a STOP can come between two words
        if step.fail is not None:
            raise LLMError(FAILED.format(kind=step.fail), step.fail)
        if step.calls:
            yield [ToolCall(f"call_{new_id()}", name, copy.deepcopy(dict(args))) for name, args in step.calls]

    async def json(self, prompt: str, schema: Mapping[str, JSON], *, temperature: float) -> dict[str, JSON]:
        """The ``json`` of the first JSON rule found in the request (see ``LLM.json``).

        Raises:
            LLMError: no script, or no JSON rule for the request.
        """
        rule = (await self._script()).rule(prompt, json=True)
        if rule is None or rule.answer is None:
            raise LLMError(f"The script {self.cfg.script_path.name} has no json rule for this request "
                           f"(“{clip(prompt, REQUEST_CHARS)}”).")
        return copy.deepcopy(dict(rule.answer))

    async def _script(self) -> Script:
        """The script, read again (in a worker thread) when its file or ``script.path`` changed."""
        return await asyncio.to_thread(self._current)

    def _current(self) -> Script:
        """The script as the file holds it now (one stat when it hasn't changed).

        Raises:
            LLMError: there is no script, or it can't be used.
        """
        path = self.cfg.script_path
        try:
            stamp = file_stamp(path.stat())
            loaded = self._loaded
            if loaded is not None and loaded[0] == path and loaded[1] == stamp:
                return loaded[2]
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as e:
            raise LLMError(f"The scripted brain has no script at {path} ({getattr(e, 'strerror', None) or e}): set "
                           "script.path in data/config.yaml.", "model") from None
        try:
            script = parse_script(text)
        except ValueError as e:
            raise LLMError(f"The script {path.name} can't be used: {e}", "model") from None
        self._loaded = (path, stamp, script)
        return script
