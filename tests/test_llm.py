"""llm: the providers' formats and errors, their streams over a mock transport, Claude's questions and tool rounds.
No model, Claude Code process or network."""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import get_args

import httpx
import pytest
from claude_agent_sdk import ResultMessage, StreamEvent

from orion.base import LLMError, Tool, ToolSet
from orion.config import ClaudeConfig, default_config
from orion.llm import (
    LLM,
    OllamaLLM,
    OpenRouterLLM,
    Prompt,
    ToolCallMerger,
    Usage,
    guarded,
    ollama_line,
    ollama_messages,
    openrouter_error,
    openrouter_messages,
    parse_json,
)
from orion.llm.claude import (
    CLAUDE_MODELS,
    ClaudeLLM,
    ClaudeStatus,
    _Session,
    _Turn,
    build_question,
    error_text,
    parse_auth_status,
)

HISTORY = (
    {"role": "user", "content": "[turn] 09:00\n\ntimer 5 min", "raw": "timer 5 min", "turn": "t1", "ts": 1.0},
    {"role": "assistant", "content": "", "turn": "t1",
     "tool_calls": [{"id": "c1", "name": "set_timer", "arguments": {"duration": "5 minutes"}}]},
    {"role": "tool", "tool_call_id": "c1", "name": "set_timer", "content": "Timer #1 started.", "turn": "t1"},
    {"role": "assistant", "content": "Done, sir.", "turn": "t1"},
)
TOOLS = ToolSet({"set_timer": Tool("set_timer", "Start a timer.", {"type": "object", "properties": {}},
                                   "orion-tools", "set_timer")}, 1).view(["set_timer"])
REQUEST = "[turn] 09:05\n\nAnd tomorrow?"


def prompt(**changes: object) -> Prompt:
    """A second turn after HISTORY, with one tool offered."""
    base = {"system": "Be brief.", "snapshot": "[memory]\n- likes tea\n[/memory]", "conv": 7,
            "turn": "t2", "history": HISTORY, "tools": TOOLS,
            "messages": ({"role": "user", "content": REQUEST, "raw": "And tomorrow?", "turn": "t2"},)}
    return Prompt(**(base | changes))


def collect(llm: OllamaLLM | OpenRouterLLM) -> list[object]:
    """Every piece of one round, the client closed after."""

    async def run() -> list[object]:
        """Stream and close."""
        try:
            return [piece async for piece in llm.stream(prompt())]
        finally:
            await llm.close()

    return asyncio.run(run())


def test_json_replies_and_message_formats() -> None:
    """Fenced or embedded JSON is found; each provider gets its own message shape; private keys never leave."""
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1} and parse_json('Here: {"a": 2} ok') == {"a": 2}
    with pytest.raises(LLMError):
        parse_json("nope")
    ollama = ollama_messages(HISTORY)
    assert ollama[1] == {"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": "set_timer", "arguments": {"duration": "5 minutes"}}}]}
    assert ollama[2] == {"role": "tool", "content": "Timer #1 started.", "tool_name": "set_timer"}
    cloud = openrouter_messages(prompt().flat(), breakpoints={0, 4})
    assert cloud[2] == {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "set_timer", "arguments": '{"duration": "5 minutes"}'}}]}
    assert cloud[3] == {"role": "tool", "tool_call_id": "c1", "content": "Timer #1 started."}
    assert [i for i, m in enumerate(cloud) if isinstance(m["content"], list)] == [0, 4]  # the cache marks
    assert not any(key in m for m in [*cloud, *ollama] for key in ("raw", "turn", "ts"))


def test_stream_lines_errors_and_call_fragments() -> None:
    """Ollama's lines carry text, calls and usage; OpenRouter's errors read plainly; call fragments merge."""
    text, calls, usage = ollama_line(b'{"message":{"content":"hi","tool_calls":[{"function":{"name":"x",'
                                     b'"arguments":{"a":1}}}]},"done":false}')
    assert (text, [(c.name, c.arguments) for c in calls], usage) == ("hi", [("x", {"a": 1})], None)
    done = ollama_line('{"message":{},"done":true,"prompt_eval_count":412,"eval_count":9,'
                       '"prompt_eval_duration":180000000}')
    assert done[2] == Usage(input=412, output=9)
    for status, words in ((401, "API key"), (402, "credits"), (404, "model"), (429, "rate limit"),
                          (500, "OpenRouter error.")):
        error = str(openrouter_error(status, json.dumps({"error": {"message": "details"}})))
        assert words in error and error.endswith("(details)")
    assert str(openrouter_error(502, "Bad Gateway")).endswith("(Bad Gateway)")
    merger = ToolCallMerger()
    merger.add([{"index": 0, "id": "call_a", "function": {"name": "set_", "arguments": '{"duration": "5 min'}}])
    merger.add([{"index": 0, "function": {"name": "timer", "arguments": 'utes", "label": "pasta"}'}},
                {"index": 1, "function": {"name": "get_weather", "arguments": "{oops"}}])
    timer, weather = merger.calls()
    assert (timer.id, timer.name) == ("call_a", "set_timer")
    assert timer.arguments == {"duration": "5 minutes", "label": "pasta"}
    assert (weather.name, weather.arguments) == ("get_weather", {}) and weather.id.startswith("call_")


def test_ollama_streams_text_then_one_round_of_calls() -> None:
    """The request carries the flat prompt and the tools; the calls come once, after the text; usage is kept."""
    sent: dict[str, object] = {}

    def answer(request: httpx.Request) -> httpx.Response:
        """Ollama's streamed answer."""
        sent.update(json.loads(request.content))
        lines = [{"message": {"content": "Let me "}}, {"message": {"content": "check."}},
                 {"message": {"tool_calls": [{"function": {"name": "set_timer", "arguments": {"duration": "5m"}}}]}},
                 {"message": {}, "done": True, "prompt_eval_count": 30}]
        return httpx.Response(200, text="\n".join(map(json.dumps, lines)))

    llm = OllamaLLM(default_config(), transport=httpx.MockTransport(answer))
    pieces = collect(llm)
    assert pieces[:2] == ["Let me ", "check."] and [c.name for c in pieces[2]] == ["set_timer"] and len(pieces) == 3
    assert sent["tools"] == list(TOOLS.schemas) and sent["messages"][0]["content"].startswith("Be brief.\n\n[memory]")
    assert llm.usage == Usage(input=30)


@pytest.mark.parametrize(("status", "words"), [(404, "Ollama doesn't have qwen"), (500, "Ollama: out of memory")])
def test_ollamas_refusals_read_plainly(status: int, words: str) -> None:
    """A model Ollama lacks says where to get it; any other refusal comes with Ollama's own words."""
    cfg = default_config()
    cfg.ollama = cfg.ollama.model_copy(update={"model": "qwen3.5:4b"})
    error = "model 'qwen3.5:4b' not found" if status == 404 else "out of memory"
    llm = OllamaLLM(cfg, transport=httpx.MockTransport(lambda _: httpx.Response(status, json={"error": error})))
    with pytest.raises(LLMError, match=words):
        collect(llm)


def test_openrouter_streams_events_and_retries_without_the_reasoning_switch(meter) -> None:
    """A model that can't switch reasoning off is asked again without it; SSE text, calls and usage (its price
    included) come back, and the request is checked against the budget before it is sent and counted after."""
    cfg = default_config()
    cfg.openrouter = cfg.openrouter.model_copy(update={"api_key": "sk-test", "model": "anthropic/claude-haiku"})
    bodies: list[dict[str, object]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        """A 400 while the reasoning switch is sent, then the streamed answer."""
        bodies.append(json.loads(request.content))
        if "reasoning" in bodies[-1]:
            return httpx.Response(400, json={"error": {"message": "Reasoning is mandatory for this endpoint"}})
        events = [{"choices": [{"delta": {"content": "Hi"}}]},
                  {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c9", "function": {
                      "name": "set_timer", "arguments": "{}"}}]}}]},
                  {"choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 3, "cost": 0.0021,
                                            "prompt_tokens_details": {"cached_tokens": 40}}}]
        lines = [f"data: {json.dumps(event)}" for event in events]
        return httpx.Response(200, text="\n\n".join([lines[0], ": OPENROUTER PROCESSING", *lines[1:], "data: [DONE]"]))

    llm = OpenRouterLLM(cfg, meter=meter, purpose="job", transport=httpx.MockTransport(answer))
    pieces = collect(llm)
    assert pieces[0] == "Hi" and [(c.id, c.name) for c in pieces[1]] == [("c9", "set_timer")]
    usage = Usage(input=50, cached=40, output=3, cost=0.0021)
    assert llm.usage == usage and len(bodies) == 2
    assert bodies[1]["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert "usage" not in bodies[0] and "usage" not in bodies[1]  # OpenRouter always sends it now: no flag to ask
    assert meter.checked == ["anthropic/claude-haiku"] and meter.counted == [("anthropic/claude-haiku", "job", usage)]


def test_a_quiet_stream_is_given_up() -> None:
    """After the first piece, a stream silent past its idle time ends with an error, not a hang."""

    async def stalls() -> AsyncIterator[str]:
        """One piece, then nothing ever again."""
        yield "a"
        await asyncio.Event().wait()

    async def run() -> list[str]:
        """Read it through guarded."""
        return [piece async for piece in guarded(stalls(), first_s=5, idle_s=0, what="Ollama")]

    with pytest.raises(LLMError, match="Ollama stopped answering"):
        asyncio.run(run())


def test_claude_questions_errors_and_status() -> None:
    """A fresh session gets memory and the conversation; a running one only what it hasn't heard."""
    fresh = build_question(prompt(), fresh=True, heard=set())
    assert fresh.startswith("[memory]") and "<conversation_so_far>\nUser: timer 5 min" in fresh
    assert fresh.endswith("\n\n" + REQUEST) and build_question(prompt(), fresh=False, heard={"t1"}) == REQUEST
    note = {"role": "assistant", "content": "Sir, your timer is done.", "turn": "a1"}
    assert build_question(prompt(history=(*HISTORY, note)), fresh=False, heard={"t1"}) == (
        f"<since_your_last_reply>\nYou: Sir, your timer is done.\n</since_your_last_reply>\n\n{REQUEST}")
    assert "usage limit" in error_text("rate_limit", "") and "billing" in error_text("billing_error", "")
    assert "isn't signed in" in error_text("authentication_failed", "")
    assert error_text("server_error", "API Error: Overloaded") == "Claude: API Error: Overloaded"
    signed_in = parse_auth_status('{"loggedIn":true,"email":"x@y","subscriptionType":"max","authMethod":"oauth"}')
    assert signed_in == ClaudeStatus(True, True, "max", "oauth") and not parse_auth_status("").logged_in
    assert set(CLAUDE_MODELS) == set(get_args(ClaudeConfig.model_fields["model"].annotation))


def test_claude_calls_close_together_are_one_round() -> None:
    """Two calls made at once reach the brain as one list; their results resume the answer."""

    class Client:
        """A Claude Code that says a word, calls two tools at once, then uses their results."""

        async def query(self, question: str) -> None:
            """Take the question."""

        async def receive_response(self) -> AsyncIterator[object]:
            """The answer's messages."""
            yield StreamEvent("u", "s", {"type": "content_block_delta", "delta": {"type": "text_delta",
                                                                                  "text": "Checking."}})
            weather, news = await asyncio.gather(session.turn.call("get_weather", {}),
                                                 session.turn.call("get_news", {}))
            yield StreamEvent("u", "s", {"type": "content_block_delta", "delta": {"type": "text_delta",
                                                                                  "text": f" {weather.text}, {news.text}."}})
            yield ResultMessage("success", 1, 1, False, 1, "s", usage={
                "input_tokens": 5, "cache_read_input_tokens": 100, "output_tokens": 7})

    session = _Session(key=("", "", False, 0, 0), model="haiku", client=Client())

    async def run() -> tuple[list[object], list[object], _Turn]:
        """Ask, answer the round, read the rest."""
        turn = session.turn = _Turn(session, "q")
        first = [piece async for piece in turn.pieces()]
        assert turn.answer(tuple({"role": "tool", "tool_call_id": c.id, "content": c.name.upper()} for c in first[-1]))
        return first, [piece async for piece in turn.pieces()], turn

    first, rest, turn = asyncio.run(run())
    assert first[0] == "Checking." and [c.name for c in first[1]] == ["get_weather", "get_news"]
    assert rest == [" GET_WEATHER, GET_NEWS."] and turn.finished
    assert turn.usage == Usage(input=105, cached=100, output=7)


@pytest.mark.parametrize("fails", [False, True])
def test_claude_is_ready_once_its_session_has_started_and_a_failed_start_waits_for_the_question(
        fails: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    """A benchmark starts a case's clock after ``ready``: it returns when the session that will take the question is
    running (a spoken question finds the one waking him started); a start that failed says so at the question."""
    llm = ClaudeLLM(default_config())
    progress: list[str] = []

    async def starting() -> None:
        await asyncio.sleep(0.05)
        progress.append("started")
        if fails:
            raise LLMError("Claude Code isn't installed.", "unavailable")

    def open_session(prompt: Prompt, key: tuple) -> _Session:
        session = _Session(key=key, model=llm.model)
        session.ready = asyncio.get_running_loop().create_task(starting())
        progress.append("opened")
        return session

    monkeypatch.setattr(llm, "_open", open_session)

    async def run() -> list[str]:
        await llm.ready(prompt())
        return list(progress)

    assert asyncio.run(run()) == ["opened", "started"]  # not before it had started, and no error either way


def test_by_default_a_model_is_ready_when_it_has_prepared() -> None:
    prepared: list[str] = []

    class Plain(LLM):
        provider = "plain"

        @property
        def model(self) -> str:
            return "plain"

        async def stream(self, prompt: Prompt) -> AsyncIterator[object]:
            yield "x"

        async def json(self, prompt: str, schema: object, *, temperature: float) -> dict:
            return {}

        async def prepare(self, prompt: Prompt) -> None:
            prepared.append("prepared")

    asyncio.run(Plain(default_config()).ready(prompt()))
    assert prepared == ["prepared"]  # the default: get ready, nothing to wait for
