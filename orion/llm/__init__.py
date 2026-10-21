"""The language models Orion thinks with, behind one small interface.

- ``LLM``: stream a ``Prompt`` (text pieces, and the tool calls that end a round), answer one JSON object (the memory
  review), warm up, prepare, get ready and wait (``ready``: the benchmark's clock starts after it), abort and close.
  ``make_llm`` builds the configured one by name: "claude" (``llm/claude.py``, your Claude plan), "ollama" and
  "openrouter" (both here, over httpx), and "script" (``llm/script.py``: no model, replies read from a file).
- ``Prompt``: the stable system text, the frozen ``[memory]`` snapshot, earlier turns, this turn's messages and the
  tools. Stateless providers send ``prompt.flat()``; Claude sends the parts separately.
- ``Meter``: OpenRouter charges per request, so each of its requests is checked against the day's budget before it is
  sent and its cost counted after (``orion.spend``), under what it was for (``Purpose``).
- ``guarded``: per-chunk timeouts around any stream (the first chunk may wait for a model to load); ``LLM.guard``
  applies the brain's.
- ``parse_json``: the JSON object in a model's reply.
- Pure adapters (tested): ``ollama_messages``, ``ollama_line``, ``openrouter_messages``, ``openrouter_error``,
  ``openrouter_usage``, ``ToolCallMerger``; ``check_key`` asks whether a key works and ``openrouter_key`` reads its
  own limits.

A round ends with a list of tool calls: a provider yields every call of the round as ONE list, then its stream ends.
The brain runs them and streams again with the results appended (Claude, whose session is waiting for those results,
picks the answer up where it paused).

httpx loads when a provider is built; importing this module costs only the standard library and ``orion.base``.
"""

import asyncio
import importlib
import json
import logging
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Final, Literal, Protocol

from orion import images
from orion.base import JSON, Expiring, Image, LLMError, LLMErrorKind, Message, ToolCall, ToolView, new_id, number
from orion.service import Ollama, OllamaError, error_detail

if TYPE_CHECKING:
    import httpx

    from orion.config import Config
    from orion.llm.fallback import Switch

log = logging.getLogger("orion.llm")

type Piece = str | list[ToolCall]  # a streamed text piece, or the tool calls that end a round
type Purpose = Literal["conversation", "job", "review", "bench"]  # what a model's requests are for, as they are counted

PROVIDERS: Final[Mapping[str, str]] = {  # provider -> "module:class", imported by name: llm and claude form no cycle
    "claude": "orion.llm.claude:ClaudeLLM",
    "ollama": "orion.llm:OllamaLLM",
    "openrouter": "orion.llm:OpenRouterLLM",
    "script": "orion.llm.script:ScriptLLM",
}
SCRIPT: Final = "script"  # the provider with no model (llm/script.py): the user's test tool, never the AI's choice
NO_KEY: Final = "No OpenRouter API key. Add one in Settings, Brain, or switch to another brain."
APP_HEADERS: Final[Mapping[str, str]] = {  # how OpenRouter credits the app
    "HTTP-Referer": "https://github.com/ToniLabInc/Orion", "X-Title": "ORION"}
MODELS_CACHE_S: Final = 600  # OpenRouter's model list is read again after this long
CONNECT_TIMEOUT_S: Final = 5  # a provider that doesn't accept the connection by then is down
KEY_CHECK_TIMEOUT_S: Final = 10  # the OpenRouter key check at warm-up
MODELS_TIMEOUT_S: Final = 15  # reading OpenRouter's model list
ANTHROPIC_PREFIX: Final = "anthropic/"  # OpenRouter models whose prompt prefix is cached only where marked
PICTURES_NOTE: Final = "The tool results above returned these pictures:"  # before them, where a tool can't hold them

_FENCE: Final = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")
_MODELS: Final[Expiring[list[dict[str, JSON]]]] = Expiring(MODELS_CACHE_S)


# ---------------------------------------------------------------------------------------------------- shapes
@dataclass(frozen=True, slots=True)
class Usage:
    """What one model call cost, for the per-turn log line (shows whether the prompt prefix was cached).

    Attributes:
        input: prompt tokens processed (with Claude, cached ones included).
        cached: of those, the ones a prefix cache served.
        output: tokens written.
        cost: what the provider charged, in US dollars (OpenRouter says; 0 for the others).
    """

    input: int = 0
    cached: int = 0
    output: int = 0
    cost: float = 0.0


@dataclass(frozen=True, slots=True)
class KeyInfo:
    """An OpenRouter key's own limits and usage, as ``GET /key`` says them (US dollars; OpenRouter's day is UTC's).

    Attributes:
        limit: its spending limit, None when it has none.
        limit_remaining: what is left of that limit, None when it has none.
        usage_daily: what it spent today, UTC.
        limit_reset: when its limit starts again ("daily", "weekly", "monthly"), None when it never does.
    """

    limit: float | None
    limit_remaining: float | None
    usage_daily: float
    limit_reset: str | None

    def as_dict(self) -> dict[str, JSON]:
        """The limits for the HUD.

        Returns:
            ``{limit, limit_remaining, usage_daily, limit_reset}``.
        """
        return asdict(self)


class Meter(Protocol):
    """Where a paid model's requests are checked and counted (``orion.spend.Spend``)."""

    def check(self, model: str) -> None:
        """Refuse a request before it is sent when the day's budget is spent.

        Args:
            model: the model asked.

        Raises:
            LLMError: kind "billing", saying why.
        """

    def record(self, *, model: str, purpose: Purpose, usage: Usage) -> None:
        """Count what one request cost.

        Args:
            model: the model asked.
            purpose: what the request was for.
            usage: its tokens and cost.
        """


@dataclass(frozen=True, slots=True)
class Prompt:
    """Everything a model reads in one round.

    Attributes:
        system: the persona, the memory rules and both reply styles; the same for every conversation.
        snapshot: the frozen "[memory] ... [/memory]" block of this context epoch.
        conv: the conversation.
        turn: the turn being answered.
        history: earlier turns, as they were sent (each message carries its ``turn``).
        messages: this turn so far: the user message (per-turn tail + words), then tool calls and results.
        tools: the tools offered, sent on every round so the prefix stays the same.
    """

    system: str
    snapshot: str
    conv: int
    turn: str
    history: tuple[Message, ...]
    messages: tuple[Message, ...]
    tools: ToolView

    def flat(self) -> list[Message]:
        """The prompt as one message list, for providers that are sent everything each time.

        Returns:
            A system message (system text, a blank line, the snapshot), then the history, then this turn.
        """
        system = "\n\n".join(part for part in (self.system, self.snapshot) if part)
        head: list[Message] = [{"role": "system", "content": system}] if system else []
        return [*head, *self.history, *self.messages]


class LLM(ABC):
    """One language model, whichever provider runs it.

    Attributes:
        cfg: the live settings (read at use time, so a changed temperature applies to the next call).
        usage: what the last call cost, None when unknown.
    """

    provider: ClassVar[str] = ""
    speaker: ClassVar[str] = "The model"  # who answers, as an error about the answer names it

    def __init__(self, cfg: "Config") -> None:
        """Keep the settings; nothing connects yet.

        Args:
            cfg: the live settings.
        """
        self.cfg = cfg
        self.usage: Usage | None = None
        self._sees = False  # set by a provider that knows its model reads pictures

    @property
    def all_tools(self) -> bool:
        """Whether the model is shown every tool (a big model picks well) instead of a routed short list."""
        return False

    def current(self) -> "LLM":
        """The model answering now: this one (a ``FallbackLLM`` says which of its two)."""
        return self

    def sees(self) -> bool:
        """Whether the model answering now can look at pictures (known once it has warmed up)."""
        return self._sees

    @property
    @abstractmethod
    def model(self) -> str:
        """The model's name, as its provider knows it."""

    @abstractmethod
    def stream(self, prompt: Prompt) -> AsyncIterator[Piece]:
        """Answer one round: text pieces as they arrive, then the round's tool calls as one list, if any.

        Args:
            prompt: what the model reads.

        Returns:
            An async iterator of pieces; wrap it in ``guard`` for timeouts.
        """

    @abstractmethod
    async def json(self, prompt: str, schema: Mapping[str, JSON], *, temperature: float) -> dict[str, JSON]:
        """Answer with one JSON object that follows a schema (the memory review).

        Args:
            prompt: the whole request.
            schema: the JSON schema of the answer.
            temperature: how freely to answer (ignored where the provider has no such setting).

        Returns:
            The object.

        Raises:
            LLMError: the model failed, timed out or wrote no JSON object.
        """

    def guard[T](self, items: AsyncIterator[T], *, whole: bool = False) -> AsyncIterator[T]:
        """A stream of this model with the brain's time limits (see ``guarded``): ``brain.first_chunk_timeout_s``
        for the first item, then ``brain.chunk_timeout_s`` between items.

        Args:
            items: the stream.
            whole: the answer comes in whole messages, not streamed: every wait gets the first item's limit.

        Returns:
            The guarded stream.
        """
        brain = self.cfg.brain
        first = brain.first_chunk_timeout_s
        return guarded(items, first_s=first, idle_s=first if whole else brain.chunk_timeout_s, what=self.speaker)

    async def warm_up(self, prompt: Prompt) -> None:  # an optional hook; doing nothing is the default
        """Get ready so the first real question is fast (default: nothing to do).

        Args:
            prompt: the system text, snapshot and tools the first question will carry.
        """

    async def prepare(self, prompt: Prompt) -> None:  # an optional hook
        """Get ready for a coming question without waiting (default: nothing; Claude opens a session).

        Args:
            prompt: the system text, snapshot and tools that question will carry.
        """

    async def ready(self, prompt: Prompt) -> None:
        """Get ready for this question and wait until the model can take it at once (``prepare``, then, for Claude,
        the session's start). A benchmark starts a question's clock after this, as a spoken question finds the
        session that waking him started.

        Args:
            prompt: the question's prompt.
        """
        await self.prepare(prompt)

    def abort(self) -> None:  # an optional hook
        """Stop the answer in progress, if any: the turn was cancelled between rounds (default: nothing to do)."""

    async def close(self) -> None:  # an optional hook
        """Close connections and sessions (default: nothing to close)."""


# ---------------------------------------------------------------------------------------------------- registry
def build_llm(cfg: "Config", provider: str, *, meter: Meter, purpose: Purpose) -> LLM:
    """Build one provider's model.

    Args:
        cfg: the live settings.
        provider: "claude", "ollama", "openrouter" or "script".
        meter: what a paid model's requests are checked against and counted in (``orion.spend``).
        purpose: what its requests are for, as they are counted.

    Returns:
        A new, unconnected LLM.
    """
    module, _, name = PROVIDERS[provider].partition(":")
    kind: type[LLM] = getattr(importlib.import_module(module), name)
    if issubclass(kind, OpenRouterLLM):  # the one provider that charges per request
        return kind(cfg, meter=meter, purpose=purpose)
    return kind(cfg)


def make_llm(cfg: "Config", *, meter: Meter, on_switch: "Callable[[Switch], None] | None" = None,
             before_fallback: "Callable[[], Awaitable[None]] | None" = None) -> LLM:
    """Build the model ``brain.provider`` names, with ``brain.fallback`` standing in for it when that names another.

    Args:
        cfg: the live settings.
        meter: what a paid model's requests are checked against and counted in.
        on_switch: told whenever the stand-in takes over or hands back (see ``FallbackLLM``).
        before_fallback: run before the stand-in first answers.

    Returns:
        A new, unconnected LLM; its requests count as the conversation's (its JSON answers as the memory review's).
    """
    primary = build_llm(cfg, cfg.brain.provider, meter=meter, purpose="conversation")
    standin = cfg.brain.fallback
    if standin in ("none", cfg.brain.provider):
        return primary
    from orion.llm.fallback import FallbackLLM

    return FallbackLLM(cfg, primary, lambda: build_llm(cfg, standin, meter=meter, purpose="conversation"),
                       labels=(label(cfg), label(cfg, standin)), on_switch=on_switch, before_fallback=before_fallback)


def label(cfg: "Config", provider: str = "") -> str:
    """A model's name as the HUD shows it: "Claude Haiku", an OpenRouter model id, an Ollama model, or the script.

    Args:
        cfg: the settings.
        provider: the provider ("" = ``brain.provider``).

    Returns:
        The label.
    """
    provider = provider or cfg.brain.provider
    if provider == "claude":
        return f"Claude {cfg.claude.model.capitalize()}"
    if provider == SCRIPT:
        return f"Script ({cfg.script_path.name})"
    return cfg.openrouter.model if provider == "openrouter" else cfg.ollama.model


def parse_json(text: str) -> dict[str, JSON]:
    """The JSON object in a model's reply, even inside ``` fences or a sentence.

    Args:
        text: the reply.

    Returns:
        The object.

    Raises:
        LLMError: the reply holds no JSON object.
    """
    text = _FENCE.sub("", text.strip())
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        try:
            value = json.loads(text[start:end + 1]) if 0 <= start < end else None
        except json.JSONDecodeError:
            value = None
    if not isinstance(value, dict):
        raise LLMError("The model didn't answer with a JSON object.")
    return value


async def guarded[T](items: AsyncIterator[T], *, first_s: float, idle_s: float, what: str) -> AsyncIterator[T]:
    """Pass a stream through, giving up when it goes quiet.

    The first item may take ``first_s`` (a model loading); each later one ``idle_s``. On expiry the source is closed
    and an LLMError says so. Tool calls run outside the stream, so they never count against it.

    Args:
        items: the source stream.
        first_s: the longest wait for the first item, in seconds.
        idle_s: the longest wait between items, in seconds.
        what: who answers, for the message ("Ollama", "Claude", ...).

    Yields:
        The source's items.

    Raises:
        LLMError: the stream went quiet for too long.
    """
    started = False
    try:
        while True:
            wait = idle_s if started else first_s
            try:
                async with asyncio.timeout(wait):
                    item = await anext(items)
            except StopAsyncIteration:
                return
            except TimeoutError:
                if started:
                    raise LLMError(f"{what} stopped answering ({wait:.0f} s without a word).", "timeout") from None
                raise LLMError(f"{what} didn't answer within {wait:.0f} s.", "timeout") from None
            started = True
            yield item
    finally:
        closer = getattr(items, "aclose", None)
        if closer is not None:
            await closer()


# ---------------------------------------------------------------------------------------------------- adapters
def ollama_messages(messages: Sequence[Message], *, vision: bool = False) -> list[dict[str, JSON]]:
    """Neutral messages in Ollama's chat format (arguments stay objects; private keys are never sent).

    With ``vision``, a user's pictures go in its message's ``images``, and the pictures tools returned in one
    following user message after their results (Ollama reads pictures on user messages).

    Args:
        messages: system, user, assistant and tool messages.
        vision: whether the model reads pictures.

    Returns:
        Ollama's messages.
    """
    out: list[dict[str, JSON]] = []
    seen: list[Image] = []
    for m in messages:
        if m["role"] != "tool" and seen:
            out.append(_ollama_pictures(seen))
            seen = []
        if m["role"] == "tool":
            out.append({"role": "tool", "content": m["content"], "tool_name": m.get("name", "")})
            if vision:
                seen += m.get("images") or ()
        elif m.get("tool_calls"):
            out.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": [
                {"function": {"name": call["name"], "arguments": call["arguments"]}} for call in m["tool_calls"]]})
        else:
            message: dict[str, JSON] = {"role": m["role"], "content": m["content"]}
            if vision and m["role"] == "user" and (pictures := m.get("images")):
                message["images"] = [picture["data"] for picture in pictures]
            out.append(message)
    if seen:
        out.append(_ollama_pictures(seen))
    return out


def _ollama_pictures(pictures: Sequence[Image]) -> dict[str, JSON]:
    """The user message that shows a model in Ollama the pictures tool results returned."""
    return {"role": "user", "content": f"{PICTURES_NOTE} {images.note(pictures)}",
            "images": [picture["data"] for picture in pictures]}


def ollama_line(line: str | bytes) -> tuple[str, list[ToolCall], Usage | None]:
    """One line of Ollama's streamed chat answer.

    Args:
        line: a JSON line.

    Returns:
        (text, tool calls, usage): the usage only on the last ("done") line, from ``prompt_eval_count``,
        ``eval_count`` and ``prompt_eval_duration``.

    Raises:
        LLMError: the line reports an error.
    """
    data = json.loads(line)
    if error := data.get("error"):
        raise LLMError(f"Ollama: {error}")
    message = data.get("message") or {}
    calls: list[ToolCall] = []
    for call in message.get("tool_calls") or ():
        function = call.get("function") or {}
        arguments = function.get("arguments") or {}
        if isinstance(arguments, str):
            arguments = _loads_object(arguments)
        calls.append(ToolCall(_call_id(), str(function.get("name") or ""), arguments if isinstance(arguments, dict)
                              else {}))
    usage = None
    if data.get("done"):
        usage = Usage(input=int(data.get("prompt_eval_count") or 0), output=int(data.get("eval_count") or 0))
    return str(message.get("content") or ""), calls, usage


def openrouter_messages(messages: Sequence[Message], *, breakpoints: Collection[int] = (),
                        vision: bool = False) -> list[dict[str, JSON]]:
    """Neutral messages in OpenRouter's (OpenAI's) chat format.

    Arguments become JSON strings; an assistant message that only calls tools has ``content`` None; private keys
    are never sent. Messages at ``breakpoints`` carry Anthropic's ``cache_control`` mark (OpenRouter passes it on),
    so the prompt up to them is cached. With ``vision``, a user's pictures join its message as ``image_url`` parts,
    and the pictures tools returned come in one user message after their results (a tool message holds text only).

    Args:
        messages: system, user, assistant and tool messages.
        breakpoints: indexes of messages to mark (system, user or assistant ones with text).
        vision: whether the model reads pictures.

    Returns:
        OpenRouter's messages.
    """
    out: list[dict[str, JSON]] = []
    seen: list[Image] = []
    for i, m in enumerate(messages):
        if m["role"] != "tool" and seen:
            out.append({"role": "user", "content": _openrouter_parts(f"{PICTURES_NOTE} {images.note(seen)}", seen)})
            seen = []
        pictures = (m.get("images") or []) if vision else []
        if m["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": m.get("tool_call_id", ""), "content": m["content"]})
            seen += pictures
        elif m.get("tool_calls"):
            out.append({"role": "assistant", "content": m.get("content") or None, "tool_calls": [
                {"id": call["id"], "type": "function", "function": {
                    "name": call["name"], "arguments": json.dumps(call["arguments"], ensure_ascii=False)}}
                for call in m["tool_calls"]]})
        elif i in breakpoints and m["content"]:
            parts = _openrouter_parts(m["content"], pictures)
            text_part = parts[0]
            if isinstance(text_part, dict):
                text_part["cache_control"] = {"type": "ephemeral"}
            out.append({"role": m["role"], "content": parts})
        elif pictures and m["role"] == "user":
            out.append({"role": "user", "content": _openrouter_parts(m["content"], pictures)})
        else:
            out.append({"role": m["role"], "content": m["content"]})
    if seen:
        out.append({"role": "user", "content": _openrouter_parts(f"{PICTURES_NOTE} {images.note(seen)}", seen)})
    return out


def _openrouter_parts(text: str, pictures: Sequence[Image]) -> list[JSON]:
    """A message's content as OpenAI-style parts: its text, then its pictures as data URLs."""
    return [{"type": "text", "text": text},
            *({"type": "image_url", "image_url": {"url": images.data_url(picture)}} for picture in pictures)]


def openrouter_error(status: int, text: str) -> LLMError:
    """OpenRouter's refusal, said plainly, with its own words after.

    Args:
        status: the HTTP status (or the error event's code).
        text: the response body.

    Returns:
        The error to raise.
    """
    hints = {
        401: "OpenRouter rejected the API key. Check it in Settings, Brain.",
        402: "The OpenRouter account has no credits left for this model. Pick a free (:free) model.",
        404: "OpenRouter doesn't know that model. Pick another one in Settings, Brain.",
        429: "OpenRouter's rate limit was reached (free models allow a few requests a minute). Try again shortly.",
    }
    kind: LLMErrorKind = {401: "auth", 402: "billing", 404: "model", 429: "limit"}.get(
        status, "unavailable" if status >= 500 or status == 408 else "other")
    return LLMError(f"{hints.get(status, 'OpenRouter error.')} ({error_detail(text, status)})", kind)


def openrouter_usage(usage: Mapping[str, Any]) -> Usage:
    """What one OpenRouter answer cost: the ``usage`` its last streamed event carries.

    Args:
        usage: ``{prompt_tokens, completion_tokens, prompt_tokens_details: {cached_tokens}, cost}``; ``cost`` is in US
            dollars (OpenRouter's credits), 0 for a free model.

    Returns:
        The usage; a missing or unreadable count is 0.
    """
    details = usage.get("prompt_tokens_details") or {}
    return Usage(input=int(usage.get("prompt_tokens") or 0), cached=int(details.get("cached_tokens") or 0),
                 output=int(usage.get("completion_tokens") or 0), cost=max(0.0, number(usage.get("cost")) or 0.0))


class ToolCallMerger:
    """Tool calls that arrive in fragments (OpenAI-style streaming), merged by their index."""

    def __init__(self) -> None:
        """Start with no calls."""
        self._calls: dict[int, dict[str, str]] = {}

    def add(self, deltas: Sequence[Mapping[str, Any]]) -> None:
        """Merge one event's tool-call fragments.

        Args:
            deltas: the ``tool_calls`` of one streamed delta: each has an ``index`` and parts of ``id``,
                ``function.name`` and ``function.arguments``.
        """
        for delta in deltas:
            index = delta.get("index")
            call = self._calls.setdefault(index if isinstance(index, int) else len(self._calls),
                                          {"id": "", "name": "", "arguments": ""})
            call["id"] = str(delta.get("id") or call["id"])
            function = delta.get("function") or {}
            call["name"] += str(function.get("name") or "")
            call["arguments"] += str(function.get("arguments") or "")

    def calls(self) -> list[ToolCall]:
        """The merged calls, in index order: unparseable arguments become {}, a missing id a fresh one.

        Returns:
            The calls.
        """
        return [ToolCall(call["id"] or _call_id(), call["name"], _loads_object(call["arguments"]))
                for _, call in sorted(self._calls.items())]


def _loads_object(text: str) -> dict[str, JSON]:
    """A JSON object from text; {} for blank, invalid or non-object text."""
    try:
        value = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _call_id() -> str:
    """A fresh id for a tool call its provider didn't name."""
    return f"call_{new_id()}"


# ---------------------------------------------------------------------------------------------------- Ollama
class OllamaLLM(LLM):
    """A model on this PC, through Ollama's HTTP API (streamed NDJSON)."""

    provider = "ollama"
    speaker = "Ollama"

    def __init__(self, cfg: "Config", transport: "httpx.AsyncBaseTransport | None" = None) -> None:
        """Prepare one connection pool; nothing connects yet.

        Args:
            cfg: the live settings (``ollama.*``, ``brain.*``).
            transport: an httpx transport (tests pass a mock).
        """
        import httpx

        super().__init__(cfg)
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=CONNECT_TIMEOUT_S), transport=transport)
        self._ollama = Ollama(cfg.ollama.host, timeout_s=cfg.ollama.api_timeout_s, client=self._http)

    @property
    def model(self) -> str:
        """``ollama.model``."""
        return self.cfg.ollama.model

    async def stream(self, prompt: Prompt) -> AsyncIterator[Piece]:
        """Answer one round through ``POST /api/chat`` (see ``LLM.stream``)."""
        cfg = self.cfg
        body = self._body(ollama_messages(prompt.flat(), vision=self._sees), prompt.tools, stream=True,
                          think=cfg.brain.think,
                          temperature=cfg.brain.temperature, top_p=cfg.ollama.top_p, top_k=cfg.ollama.top_k,
                          presence_penalty=cfg.ollama.presence_penalty)
        calls: list[ToolCall] = []
        self.usage = None
        async with self._chat(body) as lines:
            async for line in lines:
                text, found, usage = ollama_line(line)
                if text:
                    yield text
                calls += found
                self.usage = usage or self.usage
        if calls:
            yield calls

    async def json(self, prompt: str, schema: Mapping[str, JSON], *, temperature: float) -> dict[str, JSON]:
        """Answer in Ollama's JSON mode (``format`` = the schema, no hidden reasoning; see ``LLM.json``)."""
        body = self._body([{"role": "user", "content": prompt}], None, stream=True, think=False,
                          temperature=temperature)
        body["format"] = dict(schema)

        async def pieces() -> AsyncIterator[str]:
            """The answer's text as it streams."""
            async with self._chat(body) as lines:
                async for line in lines:
                    if text := ollama_line(line)[0]:
                        yield text

        return parse_json("".join([text async for text in self.guard(pieces())]))

    async def warm_up(self, prompt: Prompt) -> None:
        """Load the model having read the system text, the snapshot and the tools (the runtime has started Ollama):
        Ollama keeps that start of the prompt cached, and every question begins with it.

        Raises:
            LLMError: Ollama isn't running, or refused the model.
        """
        messages = prompt.flat() if prompt.messages else [*prompt.flat(), {"role": "user", "content": "hi"}]
        body = self._body(ollama_messages(messages), prompt.tools, stream=False, think=self.cfg.brain.think,
                          num_predict=1)
        async with self._chat(body) as lines:
            async for _ in lines:
                pass
        self._sees = "vision" in await self._ollama.capabilities(self.model)

    async def close(self) -> None:
        """Close the connection pool."""
        await self._http.aclose()

    def _body(self, messages: list[dict[str, JSON]], tools: ToolView | None, *, stream: bool, think: bool,
              **options: JSON) -> dict[str, JSON]:
        """A /api/chat request: the model, messages, tools (in the view's order), keep-alive and options."""
        body: dict[str, JSON] = {"model": self.model, "messages": messages, "stream": stream, "think": think,
                                 "keep_alive": self.cfg.ollama.keep_alive,
                                 "options": {"num_ctx": self.cfg.ollama.num_ctx, **options}}
        if tools is not None and tools.schemas:
            body["tools"] = list(tools.schemas)
        return body

    @asynccontextmanager
    async def _chat(self, body: Mapping[str, JSON]) -> AsyncIterator[AsyncIterator[str]]:
        """Send a /api/chat request and yield its non-blank lines.

        Raises:
            LLMError: Ollama isn't running, the request failed, or Ollama refused it (a model it lacks, said so).
        """
        try:
            async with self._ollama.lines("/api/chat", body) as lines:
                yield lines
        except OllamaError as e:
            if e.status == 404:
                raise LLMError(f"Ollama doesn't have {self.model}: download it in Settings, Brain. ({e})", "model") from e
            raise LLMError(f"Ollama: {e}" if e.status else str(e), "other" if e.status else "unavailable") from e


# ---------------------------------------------------------------------------------------------------- OpenRouter
class OpenRouterLLM(LLM):
    """Any model on openrouter.ai, through its chat completions API (server-sent events). Each request is checked
    against the day's budget before it is sent, and its cost (in the answer's last event) counted after."""

    provider = "openrouter"
    speaker = "OpenRouter"

    def __init__(self, cfg: "Config", *, meter: Meter, purpose: Purpose,
                 transport: "httpx.AsyncBaseTransport | None" = None) -> None:
        """Prepare one connection pool; nothing connects yet.

        Args:
            cfg: the live settings (``openrouter.*``, ``brain.*``); the base URL and timeout apply when the brain is
                rebuilt (they carry the LLM apply flag).
            meter: checks each request against the day's budget and counts its cost.
            purpose: what this model's answers are for (its JSON answers count as the memory review's).
            transport: an httpx transport (tests pass a mock).
        """
        import httpx

        super().__init__(cfg)
        self.meter = meter
        self.purpose: Purpose = purpose
        self._http = httpx.AsyncClient(base_url=cfg.openrouter.base_url, transport=transport, headers=APP_HEADERS,
                                       timeout=httpx.Timeout(cfg.openrouter.timeout_s, connect=CONNECT_TIMEOUT_S))
        self._always_reasons: set[str] = set()  # models that refuse to switch reasoning off

    @property
    def model(self) -> str:
        """``openrouter.model``."""
        return self.cfg.openrouter.model

    async def stream(self, prompt: Prompt) -> AsyncIterator[Piece]:
        """Answer one round through ``POST /chat/completions`` (see ``LLM.stream``).

        For Anthropic models the system message and the last earlier message are marked for caching.
        """
        flat = prompt.flat()
        marks: set[int] = set()
        if self.model.startswith(ANTHROPIC_PREFIX):
            system = 1 if flat and flat[0]["role"] == "system" else 0
            marks = {0} if system else set()
            if prompt.history:
                marks.add(system + len(prompt.history) - 1)
        body = self._body(messages=openrouter_messages(flat, breakpoints=marks, vision=self._sees), stream=True,
                          temperature=self.cfg.brain.temperature, top_p=self.cfg.openrouter.top_p)
        if prompt.tools.schemas:
            body["tools"] = list(prompt.tools.schemas)
        merger = ToolCallMerger()
        async for event in self._metered(body, self.purpose):
            for choice in event.get("choices") or ():
                delta = choice.get("delta") or {}
                if text := delta.get("content"):
                    yield str(text)
                if fragments := delta.get("tool_calls"):
                    merger.add(fragments)
        if calls := merger.calls():
            yield calls

    async def json(self, prompt: str, schema: Mapping[str, JSON], *, temperature: float) -> dict[str, JSON]:
        """Answer with structured output, or by instruction where the model can't (see ``LLM.json``); it counts as
        the memory review's."""
        text = f"{prompt}\n\n{self.cfg.prompts.json_instruction}\n{json.dumps(schema)}"
        body = self._body(messages=[{"role": "user", "content": text}], stream=True, temperature=temperature,
                          response_format={"type": "json_schema",
                                           "json_schema": {"name": "answer", "strict": False, "schema": dict(schema)}})
        try:
            return parse_json(await self._complete(body))
        except LLMError as e:
            if "response_format" not in str(e) and "json" not in str(e).lower():
                raise
        body.pop("response_format")  # the model can't do structured output: the prompt asks for JSON anyway
        return parse_json(await self._complete(body))

    async def warm_up(self, prompt: Prompt) -> None:
        """Check the API key now, so a problem shows in the HUD rather than at the first question (nothing is paid).

        Raises:
            LLMError: no key, or OpenRouter refused it.
        """
        await check_key(self._http, "/key", self.cfg.openrouter.key)
        try:
            listed = await openrouter_models(self.cfg.openrouter.models_url)
        except LLMError:
            log.info("OpenRouter's model list couldn't be read: %s is sent no pictures", self.model)
            return
        self._sees = any(m["id"] == self.model and m.get("vision") is True for m in listed)

    async def close(self) -> None:
        """Close the connection pool."""
        await self._http.aclose()

    def _headers(self) -> dict[str, str]:
        """The authorisation header.

        Raises:
            LLMError: no key is set.
        """
        key = self.cfg.openrouter.key
        if not key:
            raise LLMError(NO_KEY, "auth")
        return {"Authorization": f"Bearer {key}"}

    def _body(self, **body: JSON) -> dict[str, JSON]:
        """A request body: the model, and the reasoning switch the model accepts (OpenRouter always sends the
        usage)."""
        think = self.cfg.brain.think
        if think or self.model not in self._always_reasons:
            body["reasoning"] = {"enabled": think, "exclude": True}
        return {"model": self.model, **body}

    async def _complete(self, body: dict[str, JSON]) -> str:
        """Stream a request to its end and return the text, with the brain's chunk timeouts (the memory review's)."""

        async def pieces() -> AsyncIterator[str]:
            """The answer's text as it streams."""
            async for event in self._metered(body, "review"):
                for choice in event.get("choices") or ():
                    if text := (choice.get("delta") or {}).get("content"):
                        yield str(text)

        return "".join([text async for text in self.guard(pieces())])

    async def _metered(self, body: dict[str, JSON], purpose: Purpose) -> AsyncIterator[dict[str, Any]]:
        """A request's events, refused before it is sent once the day's budget is spent, its usage kept (OpenRouter
        sends it, its cost included, in the last event) and counted once the answer is complete. An answer cut off
        before its end (a time-out, STOP) never sent its usage, so it isn't counted.

        Args:
            body: the request.
            purpose: what it is for.

        Yields:
            Its events.

        Raises:
            LLMError: over the budget (kind "billing"), or as ``_events``.
        """
        self.meter.check(self.model)
        self.usage = None
        async for event in self._events(body):
            if usage := event.get("usage"):
                self.usage = openrouter_usage(usage)
            yield event
        if self.usage is not None:
            self.meter.record(model=self.model, purpose=purpose, usage=self.usage)

    async def _events(self, body: dict[str, JSON]) -> AsyncIterator[dict[str, Any]]:
        """Send a streamed request and yield its events.

        A model that refuses to switch reasoning off is remembered and asked again without the switch.

        Raises:
            LLMError: no key, a refusal, an error event, or a broken connection.
        """
        import httpx

        try:
            async with self._http.stream("POST", "/chat/completions", json=body, headers=self._headers()) as r:
                if r.status_code == 200:
                    async for line in r.aiter_lines():
                        if not line.startswith("data:"):
                            continue  # blank lines and ": OPENROUTER PROCESSING" keep-alives
                        data = line[5:].strip()
                        if data == "[DONE]":
                            return
                        event = json.loads(data)
                        if error := event.get("error"):
                            code = error.get("code") if isinstance(error, dict) else None
                            raise openrouter_error(code if isinstance(code, int) else 500, data)
                        yield event
                    return
                status, text = r.status_code, (await r.aread()).decode(errors="replace")
        except httpx.HTTPError as e:
            raise LLMError(f"OpenRouter: {e}", "unavailable") from e
        reasoning = body.get("reasoning")
        if status == 400 and "reason" in text.lower() and isinstance(reasoning, dict) and not reasoning.get("enabled"):
            log.info("%s always reasons; asking without the reasoning switch", self.model)
            self._always_reasons.add(self.model)
            async for event in self._events({key: value for key, value in body.items() if key != "reasoning"}):
                yield event
            return
        raise openrouter_error(status, text)


async def check_key(client: "httpx.AsyncClient", url: str, key: str) -> "httpx.Response":
    """Ask OpenRouter about a key (``GET /key``: nothing is paid); an answer means the key works.

    Args:
        client: the HTTP client.
        url: the endpoint: ``openrouter.base_url`` + "/key" (or "/key" for a client with that base URL).
        key: the API key.

    Returns:
        OpenRouter's answer.

    Raises:
        LLMError: no key, or OpenRouter refused it or didn't answer.
    """
    import httpx

    if not key:
        raise LLMError(NO_KEY, "auth")
    try:
        response = await client.get(url, headers={"Authorization": f"Bearer {key}"}, timeout=KEY_CHECK_TIMEOUT_S)
    except httpx.HTTPError as e:
        raise LLMError(f"OpenRouter didn't answer: {e}", "unavailable") from e
    if response.status_code != 200:
        raise openrouter_error(response.status_code, response.text)
    return response


async def openrouter_key(client: "httpx.AsyncClient", url: str, key: str) -> KeyInfo:
    """An OpenRouter key's own limits and usage (see ``check_key``).

    Args:
        client: the HTTP client.
        url: the endpoint.
        key: the API key.

    Returns:
        The key's limits.

    Raises:
        LLMError: as ``check_key``, or OpenRouter's answer can't be read.
    """
    response = await check_key(client, url, key)
    try:
        data = response.json()["data"]
        reset = data.get("limit_reset")
        return KeyInfo(limit=number(data.get("limit")), limit_remaining=number(data.get("limit_remaining")),
                       usage_daily=number(data.get("usage_daily")) or 0.0,
                       limit_reset=reset if isinstance(reset, str) and reset else None)
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise LLMError(f"OpenRouter's answer about the key couldn't be read: {e}") from e


async def openrouter_models(url: str) -> list[dict[str, JSON]]:
    """OpenRouter's models that can use tools, free ones first (cached ten minutes).

    Args:
        url: where the list is (openrouter.models_url).

    Returns:
        ``[{id, name, context, free, price}]``.

    Raises:
        LLMError: the list could not be read.
    """
    return await _MODELS.get(lambda: _fetch_models(url))


async def _fetch_models(url: str) -> list[dict[str, JSON]]:
    """Read and shape OpenRouter's model list (see ``openrouter_models``)."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=MODELS_TIMEOUT_S, headers=APP_HEADERS) as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()["data"]
    except (httpx.HTTPError, ValueError, KeyError) as e:
        raise LLMError(f"OpenRouter's model list couldn't be read: {e}") from e
    out: list[dict[str, JSON]] = []
    for m in data:
        if "tools" not in (m.get("supported_parameters") or ()):
            continue
        price = m.get("pricing") or {}
        prompt, completion = float(price.get("prompt") or 0), float(price.get("completion") or 0)
        if prompt < 0 or completion < 0:  # routers with variable prices
            continue
        modalities = (m.get("architecture") or {}).get("input_modalities") or ()
        out.append({"id": m["id"], "name": m.get("name") or m["id"], "context": m.get("context_length") or 0,
                    "free": prompt == 0 and completion == 0, "vision": "image" in modalities,
                    "price": f"${prompt * 1e6:.2f} / ${completion * 1e6:.2f} per million tokens"})
    return sorted(out, key=lambda m: (not m["free"], str(m["name"]).lower()))
