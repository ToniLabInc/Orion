"""llm.fallback: the stand-in brain takes a round the main one fails before its first word, says so once per outage,
gives the main brain back after the retry delay, and never hides a half-said answer. Fake models, no network."""

import asyncio
from collections.abc import AsyncIterator, Mapping

import pytest

from orion.base import JSON, LLMError, ToolCall, ToolSet
from orion.config import default_config
from orion.llm import LLM, Piece, Prompt, make_llm
from orion.llm.fallback import FallbackLLM, Switch, members

PROMPT = Prompt("Be brief.", "", 1, "t1", (), ({"role": "user", "content": "hi", "turn": "t1"},),
                ToolSet({}, 0).view(()))


class Fake(LLM):
    """A model that answers with set pieces, or fails with a set error (before or after its first word)."""

    provider = "fake"

    def __init__(self, cfg, name: str, pieces: list[Piece], error: LLMError | None = None, *,
                 after_words: bool = False) -> None:
        super().__init__(cfg)
        self.name, self.pieces, self.error, self.after_words = name, pieces, error, after_words
        self.asked = 0
        self.warmed = 0

    @property
    def model(self) -> str:
        return self.name

    async def stream(self, prompt: Prompt) -> AsyncIterator[Piece]:
        self.asked += 1
        if self.error is not None and not self.after_words:
            raise self.error
        for piece in self.pieces:
            yield piece
        if self.error is not None:
            raise self.error

    async def json(self, prompt: str, schema: Mapping[str, JSON], *, temperature: float) -> dict[str, JSON]:
        self.asked += 1
        if self.error is not None:
            raise self.error
        return {"from": self.name}

    async def warm_up(self, prompt: Prompt) -> None:
        self.warmed += 1
        if self.error is not None:
            raise self.error


def make(primary: Fake, standin: Fake, *, cfg=None) -> tuple[FallbackLLM, list[Switch]]:
    """A FallbackLLM over two fakes, and the switches it reported."""
    cfg = cfg or primary.cfg
    switches: list[Switch] = []
    return FallbackLLM(cfg, primary, lambda: standin, labels=("Main", "Stand-in"), on_switch=switches.append), \
        switches


async def answer(llm: LLM) -> list[Piece]:
    return [piece async for piece in llm.stream(PROMPT)]


@pytest.fixture
def cfg():
    cfg = default_config()
    cfg.brain.fallback_on = ["limit", "auth", "unavailable"]
    cfg.brain.fallback_retry_min = 30
    return cfg


def test_a_limit_before_the_first_word_is_answered_by_the_stand_in(cfg) -> None:
    primary = Fake(cfg, "main", ["never"], LLMError("Usage limit reached.", "limit"))
    standin = Fake(cfg, "stand-in", ["Hello", " there"])
    llm, switches = make(primary, standin)
    assert asyncio.run(answer(llm)) == ["Hello", " there"]
    assert llm.on_fallback and llm.current() is standin and llm.model == "stand-in"
    assert [(s.to, s.announce, s.reason) for s in switches] == [("fallback", True, "Usage limit reached.")]


def test_tool_calls_come_through_the_stand_in(cfg) -> None:
    calls = [ToolCall("c1", "get_weather", {})]
    llm, _ = make(Fake(cfg, "main", [], LLMError("down", "unavailable")), Fake(cfg, "stand-in", [calls]))
    assert asyncio.run(answer(llm)) == [calls]


def test_an_error_the_settings_dont_list_is_raised(cfg) -> None:
    llm, switches = make(Fake(cfg, "main", [], LLMError("bad model", "model")), Fake(cfg, "stand-in", ["x"]))
    with pytest.raises(LLMError, match="bad model"):
        asyncio.run(answer(llm))
    assert not llm.on_fallback and not switches


def test_a_half_said_answer_is_never_taken_over_but_the_next_round_is(cfg) -> None:
    primary = Fake(cfg, "main", ["Hal"], LLMError("limit", "limit"), after_words=True)
    standin = Fake(cfg, "stand-in", ["Whole"])
    llm, _ = make(primary, standin)
    with pytest.raises(LLMError):
        asyncio.run(answer(llm))
    assert standin.asked == 0 and not llm.on_fallback
    primary.after_words = False  # the next round fails before a word: the stand-in takes it
    assert asyncio.run(answer(llm)) == ["Whole"]


def test_the_main_brain_is_tried_again_after_the_delay_and_an_outage_is_said_once(cfg, monkeypatch) -> None:
    now = [1000.0]
    monkeypatch.setattr("orion.llm.fallback.time.monotonic", lambda: now[0])
    primary = Fake(cfg, "main", ["Main"], LLMError("limit", "limit"))
    standin = Fake(cfg, "stand-in", ["Stand-in"])
    llm, switches = make(primary, standin)
    assert asyncio.run(answer(llm)) == ["Stand-in"]
    now[0] += 60  # a minute later: still the stand-in, the main brain isn't asked
    assert asyncio.run(answer(llm)) == ["Stand-in"] and primary.asked == 1
    now[0] += 30 * 60  # past the delay, still limited: back to the stand-in, without a second announcement
    assert asyncio.run(answer(llm)) == ["Stand-in"] and primary.asked == 2
    assert [s.announce for s in switches] == [True, False]
    now[0] += 31 * 60
    primary.error = None  # the limit reset: the main brain answers and the outage is over
    assert asyncio.run(answer(llm)) == ["Main"] and not llm.on_fallback
    assert switches[-1].to == "primary"


def test_when_the_stand_in_cant_be_built_the_main_error_is_said_with_its_reason(cfg) -> None:
    primary = Fake(cfg, "main", [], LLMError("Usage limit reached.", "limit"))

    def broken() -> LLM:
        raise LLMError("No OpenRouter API key.", "auth")

    llm = FallbackLLM(cfg, primary, broken, labels=("Main", "Stand-in"))
    with pytest.raises(LLMError, match=r"Usage limit reached\. \(Stand-in couldn't stand in: No OpenRouter API key"):
        asyncio.run(answer(llm))
    assert not llm.on_fallback


def test_warm_up_readies_the_stand_in_when_the_main_brain_cant_start(cfg) -> None:
    primary = Fake(cfg, "main", [], LLMError("Claude Code isn't installed.", "unavailable"))
    standin = Fake(cfg, "stand-in", ["ok"])
    llm, switches = make(primary, standin)
    asyncio.run(llm.warm_up(PROMPT))
    assert standin.warmed == 1 and llm.on_fallback and switches[0].to == "fallback"


def test_ready_goes_to_the_brain_answering_now(cfg) -> None:
    class Waits(Fake):
        async def ready(self, prompt: Prompt) -> None:
            self.warmed += 10

    primary, standin = Waits(cfg, "main", []), Waits(cfg, "stand-in", [])
    llm, _ = make(primary, standin)
    asyncio.run(llm.ready(PROMPT))
    llm.fallback, llm.active = standin, standin  # an outage: the stand-in answers now
    asyncio.run(llm.ready(PROMPT))
    assert (primary.warmed, standin.warmed) == (10, 10)


def test_json_follows_the_brain_answering(cfg) -> None:
    llm, _ = make(Fake(cfg, "main", [], LLMError("limit", "limit")), Fake(cfg, "stand-in", []))
    assert asyncio.run(llm.json("x", {}, temperature=0)) == {"from": "stand-in"}


def test_make_llm_wraps_only_when_the_fallback_is_another_brain(meter) -> None:
    cfg = default_config()
    cfg.brain.provider, cfg.brain.fallback = "openrouter", "none"
    assert not isinstance(make_llm(cfg, meter=meter), FallbackLLM)
    cfg.brain.fallback = "openrouter"
    assert not isinstance(make_llm(cfg, meter=meter), FallbackLLM)
    cfg.brain.fallback = "ollama"
    llm = make_llm(cfg, meter=meter)
    assert isinstance(llm, FallbackLLM) and llm.labels == (cfg.openrouter.model, cfg.ollama.model)
    assert members(llm) == [llm.primary]
