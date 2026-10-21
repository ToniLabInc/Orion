"""Your Claude plan as Orion's brain: Claude Code, driven by Anthropic's Claude Agent SDK.

Claude Code signs you in itself, so Orion never sees your login, and your plan's usage limits apply. Every one of
Claude Code's own tools (commands, files, the web) is switched off: the model uses only Orion's tools, through
callbacks that hand each call to the brain, which runs it (the HUD shows it) and hands the result back. The brain's
loop is therefore the same as with the other models.

Sessions. A Claude Code process (a session, about 220 MB) holds a conversation, so each question sends only what is
new. A session's system text and tools are fixed; the frozen memory snapshot goes once, in its first question, so a
session belongs to no conversation until it is first asked. That lets a spare session start in the background (on
wake, NEW CHAT or a changed toolbox) while the user is still speaking. At most one live and one spare session run;
quiet ones close after ``claude.session_idle_s``.

STOP is bounded: the pending tool calls are released and Claude is interrupted; when its answer hasn't ended within
``STOP_DRAIN_S``, the session drains and closes in the background and the next question takes the spare.

Tool calls that arrive within ``TOOL_BATCH_MS`` of each other form one round, which the brain runs in parallel where
that is safe. The SDK loads on first use.
"""

import asyncio
import contextlib
import hashlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Coroutine, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final

from orion.base import (
    JSON,
    Expiring,
    Image,
    LLMError,
    LLMErrorKind,
    Message,
    Tool,
    ToolCall,
    ToolOutput,
    format_transcript,
    new_id,
)
from orion.llm import LLM, Piece, Prompt, Usage, parse_json
from orion.service import which

if TYPE_CHECKING:
    from orion.config import Config

log = logging.getLogger("orion.claude")

CLAUDE_MODELS: Final[Mapping[str, str]] = MappingProxyType({  # claude.model -> what the HUD says of it
    "haiku": "fastest, and lightest on your plan's limits",
    "sonnet": "smart and quick: the best voice assistant",
    "opus": "the smartest, but slower and uses your limits fastest",
})
STOP_DRAIN_S: Final = 1.5  # how long a stopped answer may take to end before its session is left to drain alone
DRAIN_S: Final = 10  # how long a session left to drain may take before it is closed anyway
TOOL_BATCH_MS: Final = 10  # tool calls this close together are one round
STATUS_CACHE_S: Final = 60  # `claude auth status` is asked again after this long
STATUS_TIMEOUT_S: Final = 15  # the longest wait for `claude auth status`
SERVER: Final = "orion"  # the in-process MCP server Claude Code sees: tools are named mcp__orion__<name>
STOPPED: Final = "Error: stopped."  # what a tool call nobody waits for any more answers
NOT_INSTALLED: Final = "Claude Code isn't installed: see Settings, Brain for how to set it up."
SIGN_IN: Final = "Claude Code isn't signed in: run `claude` in a terminal and sign in with your Claude account."
ERRORS: Final[Mapping[str, str]] = MappingProxyType({  # Claude Code's typed errors, said plainly
    "authentication_failed": SIGN_IN,
    "billing_error": "Your Claude plan has a billing problem: check it on claude.ai, or use another brain meanwhile.",
    "rate_limit": "Your Claude plan's usage limit is reached for now. Switch to another brain meanwhile.",
})

ERROR_KINDS: Final[Mapping[str, LLMErrorKind]] = MappingProxyType({  # Claude Code's typed errors, by kind
    "authentication_failed": "auth",
    "billing_error": "billing",
    "rate_limit": "limit",
})

type _Item = str | ToolCall | LLMError | None  # what a question's queue carries; None ends the answer
type _Key = tuple[str, str, bool, int, int]  # system digest, tools digest, think, rounds, tool timeout


@dataclass(frozen=True, slots=True)
class ClaudeStatus:
    """Whether Claude Code can be Orion's brain, never who the account is.

    Attributes:
        installed: the ``claude`` program was found.
        logged_in: it is signed in.
        plan: the subscription ("pro", "max", ...; "" when unknown).
        method: how it signed in ("claude.ai", "oauth", ...; "" when unknown).
    """

    installed: bool
    logged_in: bool
    plan: str
    method: str

    def as_dict(self) -> dict[str, JSON]:
        """The status for the HUD.

        Returns:
            ``{installed, logged_in, plan, method}``.
        """
        return asdict(self)


_STATUS: Final[Expiring[ClaudeStatus]] = Expiring(STATUS_CACHE_S)


def parse_auth_status(stdout: str) -> ClaudeStatus:
    """Read ``claude auth status``'s JSON, keeping only what the HUD needs (never the e-mail address).

    Args:
        stdout: the command's output.

    Returns:
        The status of an installed Claude Code; not signed in when the output isn't the expected JSON.
    """
    try:
        data = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return ClaudeStatus(True, bool(data.get("loggedIn")), str(data.get("subscriptionType") or ""),
                        str(data.get("authMethod") or ""))


async def claude_status(*, fresh: bool = False) -> ClaudeStatus:
    """Is Claude Code installed and signed in? Asked at most once a minute; callers meanwhile share one answer.

    Args:
        fresh: ask again now.

    Returns:
        The status.
    """
    return await _STATUS.get(_ask_status, refresh=fresh)


async def _ask_status() -> ClaudeStatus:
    """Run ``claude auth status`` (see ``claude_status``)."""
    cli = which("claude")
    if cli is None:
        return ClaudeStatus(False, False, "", "")
    try:
        process = await asyncio.create_subprocess_exec(cli, "auth", "status", stdout=asyncio.subprocess.PIPE,
                                                       stderr=asyncio.subprocess.DEVNULL)
    except OSError:
        return ClaudeStatus(True, False, "", "")
    try:
        async with asyncio.timeout(STATUS_TIMEOUT_S):
            out, _ = await process.communicate()
    except TimeoutError:
        process.kill()
        await process.wait()
        out = b""
    return parse_auth_status(out.decode(errors="replace"))


def error_text(code: str | None, text: str) -> str:
    """An error Claude Code reported in an answer, said plainly.

    Args:
        code: its type ("rate_limit", "authentication_failed", "billing_error", ...).
        text: the words it came with.

    Returns:
        The sentence to show and say.
    """
    return ERRORS.get(code or "") or f"Claude: {text or code or 'unknown error'}"


def answer_error(code: str | None, text: str) -> LLMError:
    """An error Claude Code reported in an answer, as the error Orion raises (its kind from Claude's code).

    Args:
        code: its type ("rate_limit", "authentication_failed", "billing_error", ...).
        text: the words it came with.

    Returns:
        The error, its message said plainly.
    """
    return LLMError(error_text(code, text), ERROR_KINDS.get(code or "", "other"))


def build_question(prompt: Prompt, *, fresh: bool, heard: Collection[str]) -> str:
    """The text a session is asked.

    A fresh session gets the memory snapshot, the conversation so far and the request. A session already in this
    conversation gets only what it hasn't heard (an announcement made between turns) and the request; earlier turns
    it answered are not sent again.

    Args:
        prompt: this round's prompt (its first user message is the request, per-turn tail included).
        fresh: whether the session has never been asked anything.
        heard: the turns the session has seen.

    Returns:
        The question.
    """
    request = next((m["content"] for m in prompt.messages if m["role"] == "user"), "")
    parts: list[str] = []
    if fresh:
        if prompt.snapshot:
            parts.append(prompt.snapshot)
        if earlier := format_transcript(prompt.history):
            parts.append(f"<conversation_so_far>\n{earlier}\n</conversation_so_far>")
    elif said := format_transcript([m for m in prompt.history if m.get("turn") and m.get("turn") not in heard]):
        parts.append(f"<since_your_last_reply>\n{said}\n</since_your_last_reply>")
    return "\n\n".join([*parts, request])


async def user_message(text: str, pictures: Sequence[Image]) -> AsyncIterator[dict[str, Any]]:
    """A question with pictures, as the SDK's streaming input takes it (a plain question can't carry them).

    Args:
        text: the question.
        pictures: the user's pictures.

    Yields:
        The one user message: the text, then each picture as a base64 image block.
    """
    yield {"type": "user", "message": {"role": "user", "content": [
        {"type": "text", "text": text},
        *({"type": "image", "source": {"type": "base64", "media_type": p["mime"], "data": p["data"]}}
          for p in pictures)]}, "parent_tool_use_id": None}


def _text_of(message: Any) -> str:
    """The words of an SDK AssistantMessage."""
    return " ".join(getattr(block, "text", "") for block in getattr(message, "content", ())).strip()


def _usage_of(usage: Mapping[str, Any] | None) -> Usage | None:
    """A ResultMessage's token counts as Usage (cache reads and writes count as input)."""
    if not usage:
        return None
    cached = int(usage.get("cache_read_input_tokens") or 0)
    written = int(usage.get("cache_creation_input_tokens") or 0)
    return Usage(input=int(usage.get("input_tokens") or 0) + cached + written, cached=cached,
                 output=int(usage.get("output_tokens") or 0))


def _annotations(tool: Tool) -> Any:
    """MCP annotations for a tool, so Claude Code knows which calls are read-only."""
    from mcp.types import ToolAnnotations

    hints = tool.hints
    return ToolAnnotations(title=tool.title or None, read_only_hint=hints.get("read_only"),
                           destructive_hint=hints.get("destructive"), idempotent_hint=hints.get("idempotent"),
                           open_world_hint=hints.get("open_world"))


# ---------------------------------------------------------------------------------------------------- sessions
@dataclass(eq=False, slots=True)
class _Session:
    """One Claude Code process.

    Attributes:
        key: what it was started with: digests of the system text and the tools, think, rounds, tool timeout.
        model: the model it runs now (switched live when the setting changes).
        client: the SDK client.
        ready: its connect(), started in the background; awaited before the first question.
        conv: the conversation it holds; None until its first question.
        heard: the turns it has seen.
        turn: the question it is answering.
        broken: unusable (failed, retired or detached); it is being closed.
        retire_after_turn: close it once the current answer ends (it holds another conversation).
    """

    key: _Key
    model: str
    client: Any = None
    ready: "asyncio.Task[None] | None" = None
    conv: int | None = None
    heard: set[str] = field(default_factory=set)
    turn: "_Turn | None" = None
    broken: bool = False
    retire_after_turn: bool = False


class _Turn:
    """One question to a session. Claude's tool calls wait in ``call`` until the brain hands back their results."""

    def __init__(self, session: _Session, question: str, pictures: Sequence[Image] = ()) -> None:
        """Send the question in the background.

        Args:
            session: the session asked.
            question: the text (see ``build_question``).
            pictures: pictures the user sent with it (sent as image blocks after the text).
        """
        self.session = session
        self.queue: asyncio.Queue[_Item] = asyncio.Queue()
        self.pending: dict[str, asyncio.Future[ToolOutput]] = {}
        self.finished = False  # nothing more for the brain: the answer ended or failed, or nobody listens
        self.ended = False  # the client is done with it (answered, stopped or detached)
        self.usage: Usage | None = None
        self._held: list[_Item] = []  # an item read while collecting a batch of calls, kept for the next round
        self.task = asyncio.get_running_loop().create_task(self._run(question, list(pictures)))

    async def call(self, name: str, args: Mapping[str, Any]) -> ToolOutput:
        """Claude calls a tool: pass the call to the brain and wait for its result.

        Args:
            name: the tool's name.
            args: its arguments.

        Returns:
            The result's text and pictures (``STOPPED`` when nobody listens any more).
        """
        if self.finished:
            return ToolOutput(STOPPED)
        call = ToolCall(f"toolu_{new_id()}", name, dict(args or {}))
        future: asyncio.Future[ToolOutput] = asyncio.get_running_loop().create_future()
        self.pending[call.id] = future
        self.queue.put_nowait(call)
        try:
            return await future
        finally:
            self.pending.pop(call.id, None)

    def answer(self, messages: Sequence[Message]) -> bool:
        """Hand back the results of the calls this question waits for.

        Args:
            messages: this turn's messages; the tool messages at their end are the results.

        Returns:
            True when they answered pending calls; False when ``messages`` start a new question instead.
        """
        results: dict[str, ToolOutput] = {}
        for message in reversed(messages):
            if message["role"] != "tool":
                break
            results[str(message.get("tool_call_id", ""))] = ToolOutput(str(message["content"]),
                                                                       tuple(message.get("images") or ()))
        if self.finished or not results or not set(results) <= set(self.pending):
            return False
        for call_id, output in results.items():
            future = self.pending[call_id]
            if not future.done():
                future.set_result(output)
        return True

    def stop(self) -> None:
        """Nobody listens any more (STOP, a new question): release the calls still waiting."""
        self.finished = True
        for future in self.pending.values():
            if not future.done():
                future.set_result(ToolOutput(STOPPED))

    async def pieces(self) -> AsyncIterator[Piece]:
        """Text as it streams until Claude calls tools (one list, then this round ends) or the answer ends.

        Raises:
            LLMError: Claude Code reported an error.
        """
        loop = asyncio.get_running_loop()
        while True:
            item = self._held.pop() if self._held else await self.queue.get()
            if item is None:
                self.finished = True
                return
            if isinstance(item, LLMError):
                self.finished = True
                raise item
            if isinstance(item, str):
                yield item
                continue
            calls = [item]
            deadline = loop.time() + TOOL_BATCH_MS / 1000
            while (left := deadline - loop.time()) > 0:
                try:
                    async with asyncio.timeout(left):
                        following = await self.queue.get()
                except TimeoutError:
                    break
                if isinstance(following, ToolCall):
                    calls.append(following)
                else:
                    self._held.append(following)
                    break
            yield calls
            return

    async def _run(self, question: str, pictures: list[Image]) -> None:
        """Ask, and queue the answer's text, tool calls (through ``call``) and errors; None at the end."""
        from claude_agent_sdk import AssistantMessage, ResultMessage, StreamEvent

        client = self.session.client
        try:
            await client.query(user_message(question, pictures) if pictures else question)
            async for message in client.receive_response():  # this answer's messages, up to its ResultMessage
                if isinstance(message, StreamEvent):
                    event = message.event
                    delta = event.get("delta") or {}
                    if event.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                        self.queue.put_nowait(str(delta.get("text") or ""))
                elif isinstance(message, AssistantMessage) and message.error:
                    if message.error == "authentication_failed":  # signed in since? A new session will know
                        self.session.broken = True
                        _STATUS.invalidate()
                    self.queue.put_nowait(answer_error(message.error, _text_of(message)))
                elif isinstance(message, ResultMessage):
                    self.usage = _usage_of(message.usage)
                    if message.is_error and message.subtype != "error_max_turns" and not self.finished:
                        self.queue.put_nowait(LLMError(f"Claude: {message.result or message.subtype}"))
        except Exception:
            self.session.broken = True
            log.warning("Claude Code stopped answering", exc_info=True)
            self.queue.put_nowait(LLMError("Claude Code stopped answering; the next question starts it again.",
                                             "unavailable"))
        finally:
            self.queue.put_nowait(None)


class ClaudeLLM(LLM):
    """Claude on your Claude Pro or Max plan, through the Claude Code you signed in to."""

    provider = "claude"
    speaker = "Claude"

    def __init__(self, cfg: "Config") -> None:
        """Prepare; no session starts until ``prepare``, ``warm_up`` or a question.

        Args:
            cfg: the live settings (``claude.*``, ``brain.*``, ``prompts.json_system``).
        """
        super().__init__(cfg)
        self._sees = True  # every Claude model reads pictures
        self._live: _Session | None = None
        self._spare: _Session | None = None
        self._turn: _Turn | None = None
        self._lock = asyncio.Lock()  # guards session swaps, never a whole answer
        self._chores: set[asyncio.Task[Any]] = set()  # connects, drains and closes in the background
        self._idle: asyncio.TimerHandle | None = None

    @property
    def model(self) -> str:
        """``claude.model``."""
        return self.cfg.claude.model

    @property
    def all_tools(self) -> bool:
        """``claude.all_tools``: a big model picks well from every tool."""
        return self.cfg.claude.all_tools

    async def warm_up(self, prompt: Prompt) -> None:
        """Check that Claude Code is installed and signed in, then start a session for the first question (none of
        this uses your plan).

        Raises:
            LLMError: not installed, or not signed in.
        """
        status = await claude_status()
        if not status.installed:
            raise LLMError(NOT_INSTALLED, "unavailable")
        if not status.logged_in:
            raise LLMError(SIGN_IN, "auth")
        await self.prepare(prompt)

    async def prepare(self, prompt: Prompt) -> None:
        """Make sure a session that can take this prompt is running or starting, without waiting for it.

        The live session is kept when it fits. One holding another conversation is closed (after its answer, when
        one is running), and a spare starts in the background.
        """
        async with self._lock:
            key = self._key(prompt)
            live = self._live
            if live is not None and self._fits(live, key, prompt.conv):
                return
            if live is not None:
                if live.turn is not None and not live.turn.ended:
                    live.retire_after_turn = True
                else:
                    self._retire(live)
                    self._live = None
            spare = self._spare
            if spare is not None and (spare.broken or spare.key != key):
                self._retire(spare)
                spare = self._spare = None
            if spare is None:
                self._spare = self._open(prompt, key)
            self._rest()

    async def ready(self, prompt: Prompt) -> None:
        """Make sure a session that can take this prompt is running, and wait until it has started (see
        ``LLM.ready``; nothing is asked, none of your plan is used). A session that fails to start says why at the
        question."""
        await self.prepare(prompt)
        async with self._lock:
            key, live = self._key(prompt), self._live
            session = live if live is not None and self._fits(live, key, prompt.conv) else self._spare
        if session is not None and session.ready is not None:
            with contextlib.suppress(LLMError):
                await asyncio.shield(session.ready)

    async def stream(self, prompt: Prompt) -> AsyncIterator[Piece]:
        """Answer one round (see ``LLM.stream``): a new question, or the same one resumed with tool results."""
        turn = self._turn
        if turn is None or turn.finished or not turn.answer(prompt.messages):
            turn = await self._ask(prompt)
        try:
            async for piece in turn.pieces():
                yield piece
        except BaseException:
            if turn.finished:
                self._after_answer(turn)
            else:  # stopped, or nobody reads on: release Claude without making the caller wait
                self._chore(self._stop(turn))
            raise
        if turn.finished:
            self._after_answer(turn)

    def abort(self) -> None:
        """Stop the answer in progress (the turn was cancelled between rounds); returns at once."""
        turn = self._turn
        if turn is not None and not turn.ended:
            turn.stop()
            self._chore(self._stop(turn))

    async def json(self, prompt: str, schema: Mapping[str, JSON], *, temperature: float) -> dict[str, JSON]:
        """A one-off question to a short-lived Claude Code on ``claude.review_model``, with structured output
        (Claude Code has no temperature setting). See ``LLM.json``."""
        from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, CLINotFoundError, ResultMessage, query

        options = ClaudeAgentOptions(
            system_prompt=self.cfg.prompts.json_system, tools=[], mcp_servers={}, strict_mcp_config=True,
            setting_sources=[], model=self.cfg.claude.review_model, thinking={"type": "disabled"},
            max_turns=self.cfg.brain.max_tool_rounds + 2, verbatim_prompts=True, cwd=str(self._workdir()),
            cli_path=which("claude"), extra_args={"no-session-persistence": None},
            output_format={"type": "json_schema", "schema": dict(schema)})
        result: Any = None
        failure: LLMError | None = None
        try:
            # read to the end, so Claude Code exits cleanly
            async for message in self.guard(query(prompt=prompt, options=options), whole=True):
                if isinstance(message, AssistantMessage) and message.error:
                    failure = answer_error(message.error, _text_of(message))
                elif isinstance(message, ResultMessage):
                    result = message
        except CLINotFoundError:
            raise LLMError(NOT_INSTALLED, "unavailable") from None
        except Exception as e:
            if isinstance(e, LLMError):
                raise
            raise failure or LLMError(f"Claude: {e}") from e
        if failure is not None:
            raise failure
        if result is None or result.is_error:
            raise LLMError(f"Claude: {(result.result or result.subtype) if result is not None else 'no answer'}")
        if isinstance(result.structured_output, dict):
            return result.structured_output
        return parse_json(result.result or "")

    async def close(self) -> None:
        """Close every session and wait (briefly) for the background work to finish."""
        async with self._lock:
            if self._idle is not None:
                self._idle.cancel()
                self._idle = None
            turn, self._turn = self._turn, None
            if turn is not None:
                turn.stop()
            for session in (self._live, self._spare):
                if session is not None:
                    self._retire(session)
            self._live = self._spare = None
        if self._chores:
            _, pending = await asyncio.wait(set(self._chores), timeout=DRAIN_S)
            for task in pending:
                task.cancel()

    # ------------------------------------------------------------------------------------------------ internals
    def _key(self, prompt: Prompt) -> _Key:
        """What a session must have been started with to take this prompt."""
        system = hashlib.blake2b(prompt.system.encode(), digest_size=8).hexdigest()
        cfg = self.cfg
        return system, prompt.tools.digest, cfg.brain.think, cfg.brain.max_tool_rounds, cfg.claude.tool_timeout_s

    @staticmethod
    def _fits(session: _Session, key: _Key, conv: int) -> bool:
        """Whether a session can take a question of this conversation with these settings."""
        return not session.broken and session.key == key and session.conv in (None, conv)

    def _workdir(self) -> Path:
        """Claude Code's working folder: an empty one in Orion's data folder."""
        path = self.cfg.data_path / "runtime" / "claude"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _open(self, prompt: Prompt, key: _Key) -> _Session:
        """Start a session in the background (its ``ready`` task connects)."""
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, SdkMcpTool, create_sdk_mcp_server

        cfg = self.cfg
        session = _Session(key=key, model=self.model)
        tools = [SdkMcpTool(tool.name, tool.description, tool.parameters, self._handler(session, tool.name),
                            annotations=_annotations(tool)) for tool in prompt.tools.tools]
        session.client = ClaudeSDKClient(ClaudeAgentOptions(
            system_prompt=prompt.system,
            tools=[],  # none of Claude Code's own tools
            mcp_servers={SERVER: create_sdk_mcp_server(SERVER, tools=tools)} if tools else {},
            allowed_tools=[f"mcp__{SERVER}__{tool.name}" for tool in prompt.tools.tools],
            strict_mcp_config=True,  # none of your other MCP servers either
            setting_sources=[],  # nor your Claude Code settings, hooks or plugins
            model=session.model,
            thinking={"type": "adaptive"} if cfg.brain.think else {"type": "disabled"},
            max_turns=cfg.brain.max_tool_rounds + 2,  # per question
            include_partial_messages=True,  # the text as it is written, so he starts speaking at once
            verbatim_prompts=True,  # nothing said or quoted is taken for a /command or an @file
            cwd=str(self._workdir()),
            cli_path=which("claude"),
            env={"MCP_TOOL_TIMEOUT": str(cfg.claude.tool_timeout_s * 1000)},
            extra_args={"no-session-persistence": None},  # nothing saved in ~/.claude
        ))
        session.ready = self._chore(self._connect(session))
        return session

    def _handler(self, session: _Session, name: str) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
        """The callback Claude Code calls for one tool of one session: it waits for that session's brain round."""

        async def handle(args: dict[str, Any]) -> dict[str, Any]:
            """Run the call through the brain and return the result as MCP content, its pictures after its text."""
            turn = session.turn
            output = ToolOutput(STOPPED) if turn is None or turn.finished else await turn.call(name, args)
            return {"content": [{"type": "text", "text": output.text},
                                *({"type": "image", "data": p["data"], "mimeType": p["mime"]} for p in output.images)]}

        return handle

    async def _connect(self, session: _Session) -> None:
        """Start Claude Code (no question is sent, none of your plan is used).

        Raises:
            LLMError: it isn't installed or didn't start.
        """
        from claude_agent_sdk import CLINotFoundError

        try:
            await session.client.connect()
        except CLINotFoundError:
            session.broken = True
            raise LLMError(NOT_INSTALLED, "unavailable") from None
        except Exception as e:
            session.broken = True
            raise LLMError(f"Claude Code didn't start: {e}", "unavailable") from e

    async def _usable(self, session: _Session) -> bool:
        """Wait until the session is connected and on the chosen model; False when it can't be used."""
        if session.ready is not None:
            with contextlib.suppress(LLMError):
                await asyncio.shield(session.ready)
        if session.broken:
            return False
        if session.model != self.model:
            try:
                await session.client.set_model(self.model)
            except Exception:
                log.info("Claude Code couldn't switch to %s; a new session starts on it", self.model, exc_info=True)
                return False
            session.model = self.model
        return True

    async def _acquire(self, prompt: Prompt) -> tuple[_Session, bool]:
        """The session to ask (under the lock): the live one if it fits, else the spare, else a new one.

        Returns:
            (session, fresh): fresh when it has never been asked anything.

        Raises:
            LLMError: no session could be started.
        """
        key = self._key(prompt)
        live = self._live
        if live is not None:
            if self._fits(live, key, prompt.conv) and await self._usable(live):
                return live, live.conv is None
            self._retire(live)
            self._live = None
        spare, self._spare = self._spare, None
        if spare is not None:
            if self._fits(spare, key, prompt.conv) and spare.conv is None and await self._usable(spare):
                self._live = spare
                return spare, True
            self._retire(spare)
        session = self._live = self._open(prompt, key)
        if not await self._usable(session):
            self._live = None
            self._retire(session)
            if session.ready is not None:
                await session.ready  # raises the reason it didn't start
            raise LLMError("Claude Code didn't start.", "unavailable")
        return session, True

    async def _ask(self, prompt: Prompt) -> _Turn:
        """Start a new question, ending one left unfinished first (its tool calls never answered)."""
        async with self._lock:
            if self._turn is not None:
                previous, self._turn = self._turn, None
                await self._end(previous)
            session, fresh = await self._acquire(prompt)
            question = build_question(prompt, fresh=fresh, heard=session.heard)
            request = next((m for m in prompt.messages if m["role"] == "user"), None)
            pictures = list(request.get("images") or ()) if request is not None else []
            session.conv = prompt.conv
            session.heard.update(str(m.get("turn")) for m in prompt.history if m.get("turn"))
            session.heard.add(prompt.turn)
            turn = session.turn = self._turn = _Turn(session, question, pictures)
            self._rest()
        return turn

    def _after_answer(self, turn: _Turn) -> None:
        """An answer ended (or failed): keep its usage and close its session if it must go."""
        turn.ended = True
        if self._turn is turn:
            self._turn = None
        session = turn.session
        if session.turn is turn:
            session.turn = None
        self.usage = turn.usage or self.usage
        if session.broken or session.retire_after_turn:
            if self._live is session:
                self._live = None
            self._retire(session)
        self._rest()

    async def _stop(self, turn: _Turn) -> None:
        """End a question nobody listens to any more."""
        async with self._lock:
            if self._turn is turn:
                self._turn = None
            await self._end(turn)

    async def _end(self, turn: _Turn) -> None:
        """Release a question's calls, interrupt Claude and wait at most ``STOP_DRAIN_S`` for its answer to end;
        a session that takes longer is detached to drain and close in the background (under the lock)."""
        if turn.ended:
            return
        turn.ended = True
        turn.stop()
        session = turn.session
        if session.turn is turn:
            session.turn = None
        if not turn.task.done():
            try:
                async with asyncio.timeout(STOP_DRAIN_S):
                    await session.client.interrupt()
                    await asyncio.shield(turn.task)  # up to its ResultMessage: left unread, it'd start the next answer
            except TimeoutError:
                session.broken = True  # it keeps answering: let it drain alone
            except Exception:
                log.warning("Claude Code couldn't be interrupted; its session is closed", exc_info=True)
                session.broken = True
        if session.broken:
            if self._live is session:
                self._live = None
            if self._spare is session:
                self._spare = None
            self._chore(self._drain(session, turn))

    async def _drain(self, session: _Session, turn: _Turn) -> None:
        """Let a detached session finish its answer for a while, then close it."""
        if not turn.task.done():
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(DRAIN_S - STOP_DRAIN_S):
                    await asyncio.shield(turn.task)
        await self._disconnect(session)
        turn.task.cancel()

    def _retire(self, session: _Session) -> None:
        """Take a session out of use and close it in the background."""
        session.broken = True
        if session.turn is not None:
            session.turn.stop()
        self._chore(self._disconnect(session))

    @staticmethod
    async def _disconnect(session: _Session) -> None:
        """Close a session's Claude Code (a connect still under way is cancelled first)."""
        ready = session.ready
        if ready is not None:
            if not ready.done():
                ready.cancel()
            await asyncio.wait({ready})
        with contextlib.suppress(Exception):
            await session.client.disconnect()

    def _rest(self) -> None:
        """Restart the countdown that closes the sessions when Orion stays quiet."""
        if self._idle is not None:
            self._idle.cancel()
        self._idle = asyncio.get_running_loop().call_later(self.cfg.claude.session_idle_s,
                                                           lambda: self._chore(self._close_if_quiet()))

    async def _close_if_quiet(self) -> None:
        """Close the sessions unless an answer is running (then wait another idle period)."""
        async with self._lock:
            if self._turn is not None and not self._turn.task.done():
                self._rest()
                return
            for session in (self._live, self._spare):
                if session is not None:
                    self._retire(session)
            self._live = self._spare = None

    def _chore[T](self, job: Coroutine[Any, Any, T]) -> "asyncio.Task[T]":
        """Run background work, kept referenced until it ends (``close`` waits for it)."""
        task = asyncio.get_running_loop().create_task(job)
        self._chores.add(task)
        task.add_done_callback(self._chore_done)
        return task

    def _chore_done(self, task: "asyncio.Task[Any]") -> None:
        """Forget a finished chore; log a failure that isn't a reason already said elsewhere."""
        self._chores.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None and not isinstance(error, LLMError):
            log.warning("Claude Code background work failed: %s", error)
