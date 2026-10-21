"""brain: hidden reasoning never shown, external tools' argument types, how a round's calls run, the space between
rounds, and whole turns against a stand-in model, hub and voice (the preamble is said before the tool runs, the turn
never waits for its speech, a failed model gets an apology), and that no secret goes through it either way."""

import asyncio
import json
import re
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orion import redact
from orion.base import CallContext, EventBus, LLMError, Tool, ToolCall, ToolOutput, ToolRequest, ToolSet
from orion.brain import Brain, TurnRunner, coerce, needs_space, plan_calls, without_secrets, without_thinking
from orion.brain.context import Context
from orion.config import default_config
from orion.llm import LLM, Piece, Prompt
from orion.memory import MemorySystem
from orion.redact import Redactor
from orion.skills import Skills

SECRET = "Zx9-Qm4_Lp7Rk2Vb8Nc5Td1Wh6Yg3Jf0Ae"


async def pieces(*parts: Piece) -> AsyncIterator[Piece]:
    """A stream of the given pieces."""
    for part in parts:
        yield part


@pytest.mark.parametrize("text", [
    "Sure. <think>the user wants a joke</think>Here goes.",
    "<think>hmm</think>Hi <think>again</think>there, <b>sir</b>.",
    "Three < four, and <thin ice: <th",  # brackets that aren't a tag stay, even last
])
def test_hidden_reasoning_split_anywhere(text: str) -> None:
    """However the text is cut, nothing inside <think> gets out and nothing after it is lost; tool calls pass."""

    async def visible(*parts: str) -> str:
        """The text that gets out of these pieces."""
        return "".join([str(p) async for p in without_thinking(pieces(*parts))])

    async def check() -> None:
        """Every cut, one character at a time, an unclosed tag, and a tool call in the middle of a tag."""
        expected = re.sub(r"<think>.*?</think>", "", text)
        for i in range(len(text) + 1):
            assert await visible(text[:i], text[i:]) == expected, i
        assert await visible(*text) == expected
        assert await visible("Hi <think>still thinking") == "Hi "
        calls = [ToolCall("c1", "set_timer", {})]
        assert [p async for p in without_thinking(pieces("Hi <th", calls, "ink>x</think>!"))] == ["Hi ", calls, "!"]

    asyncio.run(check())


def tool(name: str, *, read_only: bool = False, builtin: bool = True, properties: Mapping[str, Any] | None = None,
         ) -> Tool:
    """A tool of Orion's own (or an external one)."""
    return Tool(name, f"{name}.", {"type": "object", "properties": dict(properties or {})},
                "system" if builtin else "ext", name, hints={"read_only": read_only}, builtin=builtin)


def test_arguments_plans_and_spaces() -> None:
    """External arguments get their types; calls sort into unknown, find, parallel and serial; rounds join right."""
    search = tool("web_search", builtin=False, properties={
        "query": {"type": "string"}, "max_results": {"type": "integer"}, "safe": {"type": "boolean"},
        "ratio": {"type": ["number", "null"]}})
    assert coerce(search, {"query": "f1", "max_results": "3", "bogus": 1}) == {"query": "f1", "max_results": 3}
    assert coerce(search, {"safe": "yes", "ratio": "2.5", "max_results": "many"}) == {
        "safe": True, "ratio": 2.5, "max_results": "many"}
    assert coerce(tool("bare", builtin=False), {"x": "1"}) == {"x": "1"}
    tools = ToolSet({t.name: t for t in (tool("get_weather", read_only=True), tool("get_news", read_only=True),
                                         tool("set_timer"))}, 1)
    plan = plan_calls([ToolCall(n, n, {}) for n in ("get_weather", "get_news", "set_timer", "unknown_x",
                                                     "find_tools")], tools)
    assert [[c.name for c in group] for group in (plan.unknown, plan.find, plan.parallel, plan.serial)] == [
        ["unknown_x"], ["find_tools"], ["get_weather", "get_news"], ["set_timer"]]
    assert needs_space("I'll check.", "Done") and not needs_space("I'll check. ", "Done")
    assert not needs_space("Hmm", ", well") and not needs_space("", "Hi")


class Model(LLM):
    """A stand-in brain: each round streams its pieces; an exception among them is raised."""

    def __init__(self, rounds: list[list[Any]]) -> None:
        """Answer the rounds in order."""
        super().__init__(default_config())
        self.rounds = rounds
        self.prompts: list[Prompt] = []

    @property
    def model(self) -> str:
        """Its name."""
        return "stand-in"

    async def stream(self, prompt: Prompt) -> AsyncIterator[Piece]:
        """The next round's pieces."""
        self.prompts.append(prompt)
        for piece in self.rounds.pop(0):
            if isinstance(piece, Exception):
                raise piece
            yield piece

    async def json(self, prompt: str, schema: Mapping[str, Any], *, temperature: float) -> dict[str, Any]:
        """Not used."""
        return {}


class Hub:
    """A stand-in hub that writes what happens into a shared log (its trace records nothing)."""

    def __init__(self, log: list[str]) -> None:
        """Log into ``log``."""
        self.log = log
        self.ops = SimpleNamespace(record=lambda **step: None, turn_steps=lambda turn: [])

    async def call_named_rich(self, req: ToolRequest, ctx: CallContext) -> ToolOutput:
        """Every tool answers the weather."""
        self.log.append(f"call {req.name} for {ctx.origin}")
        return ToolOutput("Sunny, 22 C.")

    def end_turn(self, turn: str) -> None:
        """The turn is over."""
        self.log.append("end turn")

    def taint(self, turn: str, source: str = "") -> None:
        """The turn read someone else's text."""
        self.log.append(f"taint: {source}")


class Speech:
    """A stand-in voice (its own reply stream) that is still talking when the turn ends."""

    speaking = True

    def __init__(self, log: list[str]) -> None:
        """Log into ``log``."""
        self.log = log

    def reply_stream(self, turn: str, *, spoken: bool, clock: object = None) -> "Speech":
        """Itself."""
        return self

    def feed(self, delta: str) -> None:
        """Say a piece."""
        self.log.append(f"say {delta!r}")

    def boundary(self) -> None:
        """A round ends."""
        self.log.append("boundary")

    def close(self) -> None:
        """The reply is complete."""
        self.log.append("close")

    async def reply_done(self, turn: str, source: str) -> None:
        """After the reply."""
        self.log.append("reply done")

    def announce(self, text: str) -> None:
        """Not used."""

    def chime(self, kind: str) -> None:
        """A chime."""
        self.log.append(f"chime {kind}")

    def stop_speaking(self) -> None:
        """Silence."""
        self.log.append("stop")


class NoModel:
    """An embedder without a model."""

    id = ""
    loaded = False

    def load(self) -> bool:
        """Nothing to load."""
        return False

    def encode(self, texts: object) -> None:
        """No vectors."""

    def encode_query(self, text: str) -> None:
        """No vector."""


def run_turn(rounds: list[list[Any]], tmp_path: Path, *, text: str = "What's the weather?",
             setup: Callable[[Skills], None] | None = None, kept: list[str] | None = None,
             announce: str = "") -> tuple[list[str], list[dict[str, Any]], Any, Model]:
    """One HUD turn through a real Brain and TurnRunner over stand-ins; returns the log, the events, the result (and
    puts what the memory logged in ``kept``). ``announce``: something he says unasked afterwards."""
    cfg = default_config()
    memory = MemorySystem.open(cfg, NoModel(), role="main")  # type: ignore[arg-type]
    skills = Skills(tmp_path / "skills")
    if setup is not None:
        setup(skills)
    log: list[str] = []
    events: list[dict[str, Any]] = []
    bus = EventBus()
    bus.subscribe(events.append)
    hub, model = Hub(log), Model(rounds)

    def spawn(job: Any, *, name: str) -> Any:
        """Run background work."""
        return asyncio.get_running_loop().create_task(job, name=name)

    brain = Brain(cfg, memory, skills, NoModel(), Context(cfg, memory, skills, NoModel()), hub,  # type: ignore[arg-type]
                  model, spawn)
    brain.set_tools({"get_weather": tool("get_weather", read_only=True)}, 1)

    async def turn() -> Any:
        """Run the turn; the runner is free again as soon as it returns, though the voice still talks."""
        runner = TurnRunner(cfg, bus, memory, brain, hub, Speech(log), None, spawn)  # type: ignore[arg-type]
        result = await runner.handle(text, "hud")
        assert not runner.busy and "reply done" not in log
        await runner.idle()
        if announce:
            runner.announce(announce, spoken=False)
        return result

    result = asyncio.run(turn())
    if kept is not None:
        memory.store.submit(lambda: None).result()  # (what was queued for the database is written)
        kept += [row["content"] for row in memory.store.messages_after(0, 100)]
    memory.close()
    return log, events, result, model


def test_the_preamble_is_said_before_the_tool_runs(tmp_path: Path) -> None:
    """Round one's text is spoken at its boundary, then the tool runs, then the answer follows with a space."""
    log, events, result, model = run_turn([["Let me check.", [ToolCall("c1", "get_weather", {})]], ["It's sunny."]],
                                          tmp_path)
    assert log == ["stop", "say 'Let me check.'", "boundary", "call get_weather for brain", "say ' '",
                   "say \"It's sunny.\"", "close", "end turn", "reply done"]
    assert not result.interrupted
    assert model.prompts[1].messages[-1] == {"role": "tool", "tool_call_id": "c1", "name": "get_weather",
                                             "content": "Sunny, 22 C.", "turn": result.turn}
    done = next(e for e in events if e["type"] == "assistant_done")
    assert (done["text"], done["rounds"], done.get("error")) == ("Let me check. It's sunny.", 2, None)


def test_a_failed_model_gets_an_apology(tmp_path: Path) -> None:
    """An LLMError mid-answer ends the turn with the configured apology, an error chime and error=True."""
    log, events, result, _ = run_turn([["Let me", LLMError("usage limit reached")]], tmp_path)
    apology = "My apologies, sir, something went wrong: usage limit reached"
    done = [e for e in events if e["type"] == "assistant_done"]
    assert len(done) == 1 and done[0]["error"] is True and done[0]["text"] == f"Let me\n\n{apology}"
    assert not result.interrupted and "chime error" in log and f"say {apology!r}" in log


# ---------------------------------------------------------------------------------------------------- no secret goes through
@pytest.fixture
def knowing():
    """A process whose redactor knows one secret and the shipped shapes."""
    with redact.installed(Redactor(default_config().safety.secret_patterns, min_chars=8)) as redactor:
        redactor.learn([SECRET])
        yield redactor


async def failing(*parts: Piece) -> AsyncIterator[Piece]:
    """A stream of the given pieces that then fails."""
    for part in parts:
        yield part
    raise LLMError("the connection was lost")


def test_the_models_text_is_the_text_redacted_however_the_stream_is_cut(knowing: Redactor) -> None:
    """A secret split across pieces never shows; a tool call and the text before it keep their order; what waited when
    the model fails still comes out."""

    async def shown(*parts: Piece) -> list[Piece]:
        return [p async for p in without_secrets(pieces(*parts))]

    async def check() -> None:
        text = f"Your token is {SECRET}, sir. And again: {SECRET}!"
        for i in range(len(text) + 1):
            assert "".join(str(p) for p in await shown(text[:i], text[i:])) == knowing.redact(text), i
        assert "".join(str(p) for p in await shown(*text)) == knowing.redact(text)
        calls = [ToolCall("c1", "set_timer", {})]
        assert await shown("Let me check", calls, f" it {SECRET} now") == ["Let me ", "check", calls, " it [secret] ", "now"]
        got: list[Piece] = []
        with pytest.raises(LLMError, match="connection was lost"):
            async for piece in without_secrets(failing("Almost ", "the")):
                got.append(piece)
        assert "".join(str(p) for p in got) == "Almost the"

    asyncio.run(check())


def test_a_secret_the_model_says_is_not_shown_spoken_logged_nor_handed_back_to_it(tmp_path: Path, knowing: Redactor) -> None:
    kept: list[str] = []
    log, events, result, model = run_turn(
        [[f"Your key is {SECRET[:12]}", f"{SECRET[12:]}, sir. One moment.", [ToolCall("c1", "get_weather", {})]],
         [f"Once more: {SECRET}"]], tmp_path, kept=kept)
    everything = json.dumps([log, events, kept, [p.messages for p in model.prompts], result.text])
    assert SECRET not in everything and SECRET[:12] not in everything
    done = next(e for e in events if e["type"] == "assistant_done")
    assert done["text"] == "Your key is [secret], sir. One moment. Once more: [secret]"
    assert model.prompts[1].messages[-2]["content"] == "Your key is [secret], sir. One moment."  # what it said, as kept
    assert kept[-1] == done["text"]


def test_a_secret_the_user_says_goes_no_further_than_the_turns_start(tmp_path: Path, knowing: Redactor) -> None:
    kept: list[str] = []
    log, events, _, model = run_turn([["Noted."]], tmp_path, text=f"My token is {SECRET}, keep it safe", kept=kept)
    everything = json.dumps([log, events, kept, [p.messages for p in model.prompts]])
    assert SECRET not in everything
    assert next(e for e in events if e["type"] == "user_message")["text"] == "My token is [secret], keep it safe"
    assert model.prompts[0].messages[0]["raw"] == "My token is [secret], keep it safe" and kept[0] == model.prompts[0].messages[0]["raw"]


def test_an_error_that_names_a_secret_is_apologised_for_without_it(tmp_path: Path, knowing: Redactor) -> None:
    log, events, _, _ = run_turn([[LLMError(f"401 for the key {SECRET}")]], tmp_path)
    done = next(e for e in events if e["type"] == "assistant_done")
    assert done["error"] is True and done["text"].endswith("something went wrong: 401 for the key [secret]")
    assert SECRET not in json.dumps([log, events])


def test_something_he_says_unasked_is_redacted_too(tmp_path: Path, knowing: Redactor, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("orion.brain.notify", lambda *args, **kwargs: None)  # (never a real desktop notification)
    kept: list[str] = []
    log, events, _, _ = run_turn([["Hello."]], tmp_path, kept=kept, announce=f"The job found the key {SECRET}.")
    said = next(e for e in events if e["type"] == "announcement")
    assert said["text"] == "The job found the key [secret]." and kept[-1] == said["text"]
    assert SECRET not in json.dumps([log, events, kept])


def test_a_jobs_request_and_answer_are_redacted_like_a_turns(tmp_path: Path, knowing: Redactor) -> None:
    cfg = default_config()
    memory = MemorySystem.open(cfg, NoModel(), role="main")  # type: ignore[arg-type]
    skills = Skills(tmp_path / "skills")
    model = Model([[f"The key {SECRET[:10]}", f"{SECRET[10:]} is set."]])

    def spawn(job: Any, *, name: str) -> Any:
        return asyncio.get_running_loop().create_task(job, name=name)

    brain = Brain(cfg, memory, skills, NoModel(), Context(cfg, memory, skills, NoModel()), Hub([]),  # type: ignore[arg-type]
                  model, spawn)

    async def job() -> tuple[str, Any]:
        return await brain.run_task(f"Check that {SECRET} still works", llm=model, turn="job-1", source="job", limit=3)

    reply, _ = asyncio.run(job())
    memory.close()
    assert reply == "The key [secret] is set."
    assert SECRET not in json.dumps([p.messages for p in model.prompts]) and "Check that [secret] still works" in (
        model.prompts[0].messages[0]["raw"])


def test_a_skill_that_is_someone_elses_text_taints_the_turn_it_starts(tmp_path: Path) -> None:
    """A skill the user wrote runs as before; an imported one (here by name) marks the turn untrusted before the model
    sees it, so its steps can't change the user's connectors or memory without a yes."""
    def add(skills: Skills, name: str, origin: str) -> None:
        folder = tmp_path / "downloaded" / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Does {name}.\n---\nSteps.\n")
        if origin:
            skills.adopt(folder, origin=origin)
        else:
            skills.write(name, f"Does {name}.", "Steps.", None, None)

    log, *_ = run_turn([["Done."]], tmp_path, text="run mine please", setup=lambda s: add(s, "mine", ""))
    assert not any(line.startswith("taint") for line in log)
    log, *_ = run_turn([["Done."]], tmp_path, text="run weekend please", setup=lambda s: add(s, "weekend", "x.zip"))
    assert "taint: The weekend skill" in log
