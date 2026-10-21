"""The brain: one turn's agent loop (``Brain``) and the turns themselves, one at a time (``TurnRunner``).

``Brain.respond`` answers one request as a stream of typed events:
1. The request is embedded once; skill matching, memory recall and tool routing share that vector.
2. A skill asked for by name or trigger phrase, or an unmistakable match, goes before the model (``SkillUsed``).
3. The 0-3 memories recalled for the request go into the turn's tail (``Recalled``).
4. Rounds: the model streams text (``Delta``) and may end a round with tool calls. ``RoundEnd`` comes before they
   run, so the speech says what is pending first. Read-only calls run concurrently, the others one after another in
   the model's order, and the results go back in the model's order. ``find_tools`` and unknown names are answered
   here; everything else goes through the hub, which traces each call and marks the turn tainted after web or app
   content.
5. ``Done``: the reply, what it cost, and how many rounds it took.

The brain also owns the conversation: its id, the history window, the context epoch and the routed toolbox.

No secret goes through it either way (``orion.redact``): the request is redacted when a turn starts, before it is shown,
logged or read by the model, and the model's text is redacted as it streams (``without_secrets``).

``TurnRunner`` runs turns one at a time and implements the voice session's ``TurnPort``. A request that only shows or
hides parts of the HUD ("show me the weather") is answered at once, without the model, while a HUD is open. A request
that is one of Home Assistant's sentences ("set a timer for 5 minutes") is done at once too (``orion.instant``): its
tools run as the user's own command, and its reply is spoken and shown like any other, marked ``instant``; one that
can't be done exactly as said goes to the model. It publishes each turn's events, logs its messages and steps on the
database's thread (never waited for), feeds the reply to the speech port as it streams, and handles STOP, NEW CHAT, a
brain switch and announcements. A turn's lock covers the brain's work only: the speech drains after the lock is
released. Each turn is timed (``orion.timing``): hearing, thinking, tools and speaking, published and kept.
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable, Coroutine, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol, TypedDict

from orion import images, redact
from orion.base import (
    CORE_SERVER,
    JSON,
    TRUNCATED,
    AssistantState,
    CallContext,
    Channel,
    Conv,
    EventBus,
    Image,
    LLMError,
    Message,
    Query,
    StepKind,
    StepRecord,
    Tool,
    ToolCall,
    ToolOutput,
    ToolRequest,
    ToolSet,
    ToolView,
    TurnResult,
    TurnSource,
    clip,
    local_now,
    new_id,
)
from orion.brain.context import Context, HistoryWindow, Snapshot
from orion.llm import LLM, Piece, Prompt, Usage, label
from orion.service import notify
from orion.timing import INSTANT, Heard, Timing, TurnClock
from orion.voice.wake import Summons, summon

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

    from orion.config import Config
    from orion.inference import Embedder
    from orion.instant import Instant
    from orion.mcp import OpTrace
    from orion.memory import MemorySystem
    from orion.memory.index import Hit
    from orion.memory.review import Reviewer
    from orion.skills import Skills

log = logging.getLogger("orion.brain")

HISTORY_ROWS: Final = 400  # logged messages read back into the history window after a restart (it trims by size)
PREPARE_DEBOUNCE_S: Final = 1.0  # toolbox changes this close together get the brain ready once
RECALL_TEXT_CHARS: Final = 80  # a recalled memory as the HUD's recall event shows it
FIND_SUMMARY_CHARS: Final = 160  # a tool's description in find_tools' answer
LIMIT_REACHED: Final = "Tool limit reached: answer now from what you have."
NO_TOOL: Final = "Error: there is no tool called {name}."
NO_TOOL_FIND: Final = "Error: there is no tool called {name}. Call find_tools to search for one."
FOUND: Final = "These tools are now available; call them directly:"
NOT_FOUND: Final = "No matching tools. Say what you can't do and suggest an alternative."
_CLOSERS: Final = frozenset(",.;:!?)]}'\"…%")  # a round may start with one of these straight after the last
_TRUE: Final = frozenset({"true", "yes", "1", "on"})

FIND_TOOLS: Final = Tool(
    name="find_tools",
    description=("Search your full toolbox (including connected MCP servers) for tools that can do something the "
                 "tools you see now can't, before saying you can't. The tools found become available at once."),
    parameters={"type": "object", "required": ["query"], "properties": {
        "query": {"type": "string", "description": "What you need to do, e.g. 'control smart lights'."}}},
    server=CORE_SERVER, remote="find_tools", title="Find tools", summary="Search the full toolbox for a tool.",
    hints={"read_only": True}, builtin=True)


# ---------------------------------------------------------------------------------------------------- events
@dataclass(frozen=True, slots=True)
class Delta:
    """Reply text as it streams (shown and spoken).

    Attributes:
        text: the piece.
    """

    text: str


@dataclass(frozen=True, slots=True)
class RoundEnd:
    """A round ended with tool calls, which run next: speak what is pending now.

    Attributes:
        round: the round's number, from 0.
    """

    round: int


@dataclass(frozen=True, slots=True)
class Recalled:
    """Memories recalled for the request went into its tail.

    Attributes:
        items: the memories (1 to 3).
        ms: how long recall took.
    """

    items: tuple["Hit", ...]
    ms: float


@dataclass(frozen=True, slots=True)
class SkillUsed:
    """A skill went before the model without it asking (by name, trigger phrase or an unmistakable match).

    Attributes:
        name: the skill.
    """

    name: str


@dataclass(frozen=True, slots=True)
class ToolsRan:
    """A round's tool calls are over.

    Attributes:
        seconds: how long they took, from the first call's start to the last call's end.
    """

    seconds: float


@dataclass(frozen=True, slots=True)
class Done:
    """The turn is over.

    Attributes:
        text: the whole reply.
        usage: what the last model call cost, None when unknown.
        rounds: how many rounds it took.
    """

    text: str
    usage: Usage | None
    rounds: int


type BrainEvent = Delta | RoundEnd | Recalled | SkillUsed | ToolsRan | Done


@dataclass(frozen=True, slots=True)
class CallPlan:
    """One round's tool calls, sorted by how they run.

    Attributes:
        unknown: names the toolbox doesn't have.
        find: ``find_tools`` calls (answered by the brain).
        parallel: read-only tools: run together.
        serial: everything else: one after another, in the model's order.
    """

    unknown: tuple[ToolCall, ...]
    find: tuple[ToolCall, ...]
    parallel: tuple[ToolCall, ...]
    serial: tuple[ToolCall, ...]


@dataclass(slots=True)
class _Progress:
    """A request's answer as its rounds go.

    Attributes:
        final: the reply so far.
        rounds: how many rounds ran.
        first: when the first word came (monotonic seconds; 0 before).
    """

    final: str = ""
    rounds: int = 0
    first: float = 0.0


class LiveTurn(TypedDict):
    """The running turn, as the HUD's ``init`` sends it."""

    id: str
    text: str
    steps: list[StepRecord]


# ---------------------------------------------------------------------------------------------------- ports
class ToolHub(Protocol):
    """What the brain needs of the hub (``mcp.MCPHub`` provides it).

    Attributes:
        ops: the operation trace (the brain's own steps go there too: ``find_tools``, an unknown name).
    """

    ops: "OpTrace"

    async def call_named_rich(self, req: ToolRequest, ctx: CallContext) -> ToolOutput:
        """Run a tool by its model-visible name: its text and pictures; never raises ("Error: ..." in words)."""

    def end_turn(self, turn: str) -> None:
        """Forget a finished turn's taint and operations."""


class ReplySpeech(Protocol):
    """One reply's speech (``voice.tts.SpeechStream``)."""

    def feed(self, delta: str) -> None:
        """Add streamed reply text; finished pieces are spoken."""

    def boundary(self) -> None:
        """A tool round starts: speak what is pending now."""

    def close(self) -> None:
        """The reply is complete: speak the rest."""


class SpeechPort(Protocol):
    """What the turn runner needs of the voice (``voice.session.VoiceSession`` implements it)."""

    def reply_stream(self, turn: str, *, spoken: bool, clock: TurnClock | None = None) -> ReplySpeech:
        """A reply's speech (text only when ``spoken`` is False); ``clock`` hears when it is ready and sounds."""

    async def reply_done(self, turn: str, source: TurnSource) -> None:
        """After a reply: wait until it has been said, then keep listening (a voice request) or rest."""

    def announce(self, text: str) -> None:
        """Speak an announcement after a chime."""

    def chime(self, kind: Literal["wake", "done", "error", "standby"]) -> None:
        """Play a chime."""

    def stop_speaking(self) -> None:
        """Stop speaking at once."""

    @property
    def speaking(self) -> bool:
        """Whether he is making sound (or about to)."""


class Spawner(Protocol):
    """Runs background work, kept referenced and logged if it fails (the runtime's supervisor)."""

    def __call__(self, job: Coroutine[Any, Any, Any], /, *, name: str) -> "asyncio.Task[Any]":
        """Start ``job`` as a task called ``name``."""


# ---------------------------------------------------------------------------------------------------- pure parts
async def without_thinking(pieces: AsyncIterator[Piece]) -> AsyncIterator[Piece]:
    """The model's text with ``<think>...</think>`` left out (some models leak it even with thinking off; it is never
    shown or said), also when a tag arrives split across pieces. Tool calls pass through.

    Args:
        pieces: the model's stream.

    Yields:
        The visible text pieces and the tool-call lists, in order.
    """
    hidden, held = False, ""
    async for piece in pieces:
        if isinstance(piece, list):
            yield piece
            continue
        text, visible = held + piece, ""
        while (tag := "</think>" if hidden else "<think>") in text:
            before, _, text = text.partition(tag)
            visible += "" if hidden else before
            hidden = not hidden
        cut = next((n for n in range(len(tag) - 1, 0, -1) if text.endswith(tag[:n])), 0)  # a tag's start waits
        held, text = text[len(text) - cut:], text[:len(text) - cut]
        if not hidden:
            visible += text
        if visible:
            yield visible
    if held and not hidden:
        yield held


async def without_secrets(pieces: AsyncIterator[Piece]) -> AsyncIterator[Piece]:
    """The model's text with every secret cut out (``orion.redact``). A word waits for the next piece in case a secret
    goes on in it, and what waits comes out before a tool call, when the text ends and when the model fails. Tool calls
    pass through.

    Args:
        pieces: the model's stream.

    Yields:
        The text that is safe to show or say, and the tool-call lists, in order.
    """
    shield = redact.stream()
    try:
        async for piece in pieces:
            if isinstance(piece, list):
                if rest := shield.flush():
                    yield rest
                yield piece
            elif safe := shield.feed(piece):
                yield safe
    except Exception:
        if rest := shield.flush():
            yield rest  # the words before a failure are still said
        raise
    if rest := shield.flush():
        yield rest


def coerce(tool: Tool, args: Mapping[str, Any]) -> dict[str, Any]:
    """Arguments an external tool can take: unknown ones dropped, obvious type slips fixed ("5" for an integer).

    Orion's own tools validate their arguments themselves, so the brain uses this for external tools only.

    Args:
        tool: the tool (its ``parameters`` JSON schema).
        args: the model's arguments.

    Returns:
        The arguments; a value that can't be fixed is passed on as it is (the tool says what is wrong).
    """
    properties = tool.parameters.get("properties")
    if not isinstance(properties, dict) or not properties:
        return dict(args)
    clean: dict[str, Any] = {}
    for key, value in args.items():
        spec = properties.get(key)
        if spec is None:
            continue
        kind = spec.get("type") if isinstance(spec, dict) else None
        if isinstance(kind, list):  # ["integer", "null"]
            kind = next((k for k in kind if k != "null"), None)
        clean[key] = _typed(value, kind)
    return clean


def _typed(value: Any, kind: object) -> Any:
    """One argument given the JSON type its schema asks for, when that is plainly what was meant."""
    if value is None:
        return None
    try:
        if kind == "integer" and not isinstance(value, int):
            return int(float(value))
        if kind == "number" and not isinstance(value, int | float):
            return float(value)
    except (TypeError, ValueError):
        return value
    if kind == "boolean" and isinstance(value, str):
        return value.strip().lower() in _TRUE
    if kind == "string" and not isinstance(value, str):
        return json.dumps(value, ensure_ascii=False) if isinstance(value, dict | list) else str(value)
    return value


def plan_calls(calls: Sequence[ToolCall], tools: ToolSet) -> CallPlan:
    """Sort one round's calls by how they run.

    Args:
        calls: the round's calls, in the model's order.
        tools: the toolbox.

    Returns:
        The plan; each group keeps the model's order.
    """
    unknown: list[ToolCall] = []
    find: list[ToolCall] = []
    parallel: list[ToolCall] = []
    serial: list[ToolCall] = []
    for call in calls:
        tool = tools.get(call.name)
        if call.name == FIND_TOOLS.name:
            find.append(call)
        elif tool is None:
            unknown.append(call)
        else:
            (parallel if tool.read_only else serial).append(call)
    return CallPlan(tuple(unknown), tuple(find), tuple(parallel), tuple(serial))


def needs_space(previous: str, piece: str) -> bool:
    """Whether a new round's first text needs a space after the reply so far ("I'll check." then "Done").

    Args:
        previous: the reply so far.
        piece: the new round's first text.

    Returns:
        True when neither side has a space at the join and the piece doesn't open with punctuation.
    """
    return (bool(previous) and bool(piece) and not previous[-1].isspace() and not piece[0].isspace()
            and piece[0] not in _CLOSERS)


def _step(turn: str, step_id: str, kind: StepKind, name: str, title: str, args: dict[str, JSON], result: str,
          ms: float) -> StepRecord:
    """One of the turn runner's own steps (a recall, a skill) as the HUD and the database keep it."""
    return StepRecord(id=step_id, turn=turn, kind=kind, name=name, title=title, server="", args=args, source="brain",
                      client="", at=time.time() - ms / 1000, ms=round(ms), ok=True, result=result, taints=False,
                      memory=None)


# ---------------------------------------------------------------------------------------------------- the brain
class Brain:
    """One turn's agent loop and the conversation it belongs to (see the module docstring). Use it on the loop."""

    def __init__(self, cfg: "Config", memory: "MemorySystem", skills: "Skills", embedder: "Embedder",
                 context: Context, hub: ToolHub, llm: LLM, spawn: Spawner) -> None:
        """Pick up the current conversation from the log; nothing connects yet.

        Args:
            cfg: the live settings.
            memory: Orion's memory (role "main").
            skills: the skill store.
            embedder: the process's embedder (used only once it has loaded).
            context: builds the system text, snapshots and tails, and routes tools.
            hub: runs the tools.
            llm: the language model.
            spawn: the runtime's way of running background work (getting ready, closing an old model).
        """
        self.cfg = cfg
        self.memory = memory
        self.skills = skills
        self.embedder = embedder
        self.context = context
        self.hub = hub
        self.llm = llm
        self.spawn = spawn
        self.toolset = ToolSet({FIND_TOOLS.name: FIND_TOOLS}, 0)
        self.history = HistoryWindow(cfg.brain)
        self._conv = memory.store.current_conv()
        self.history.load(memory.store.history(self._conv.id, HISTORY_ROWS))
        self._snapshot: Snapshot | None = None
        self._recalled: set[int] = set()  # units recalled in this context epoch (never twice)
        self._routed: list[str] = []  # a routed toolbox: what this conversation has been shown (it only grows)
        self._recent: frozenset[str] = frozenset()  # tools used in the last turn (offered again after NEW CHAT)
        self._notes: list[tuple[str, str, float]] = []  # announcements made during a turn, added after it
        self._running = False
        self._warm = False  # warm_up succeeded: the brain may get ready on its own from now on
        self._prepare_timer: asyncio.TimerHandle | None = None

    @property
    def conv(self) -> Conv:
        """The conversation in progress."""
        return self._conv

    # ------------------------------------------------------------------------------------------------ state
    def set_tools(self, tools: Mapping[str, Tool], version: int) -> None:
        """Take a new toolbox: the hub's tools plus find_tools (the router embeds them before the next turn). A brain
        shown every tool gets ready for the new list once the changes settle.

        Args:
            tools: the hub's tools by model-visible name, in its order.
            version: the hub's toolbox version.
        """
        self.toolset = ToolSet({**tools, FIND_TOOLS.name: FIND_TOOLS}, version)
        if self._warm and self.llm.all_tools and not self._running:
            self._prepare_soon()

    def set_llm(self, llm: LLM) -> None:
        """Think with another model from the next turn on (the old one closes in the background).

        Args:
            llm: the new model (call ``warm_up`` next).
        """
        old, self.llm = self.llm, llm
        self._warm = False
        if old is not llm:
            self.spawn(old.close(), name="close the old brain")

    def begin_turn(self, now: float) -> tuple[Conv, bool]:
        """Start a turn's bookkeeping: after ``brain.session_gap_min`` quiet minutes the conversation starts afresh.

        Args:
            now: the time, in epoch seconds.

        Returns:
            The turn's conversation, and whether it is a new one.
        """
        gap = self.cfg.brain.session_gap_min * 60
        if gap and self.history.messages() and now - self.history.last_ts > gap:
            return self.new_conversation("idle_gap", prepare=False), True
        return self._conv, False

    def new_conversation(self, reason: Literal["new_chat", "idle_gap"], *, prepare: bool = True) -> Conv:
        """Start a new conversation, for the model too (a turn under way still ends in the old one).

        Args:
            reason: "new_chat" (the HUD or a tool) or "idle_gap" (a long pause).
            prepare: get the model ready for it in the background.

        Returns:
            The new conversation.
        """
        self._conv = self.memory.store.new_conv(reason, time.time())
        self.history.clear()
        self._recalled.clear()
        self._routed = []  # chosen again on its first turn (the last turn's tools stay offered: _recent)
        self._snapshot = None
        if prepare and self._warm:
            self.spawn(self.prepare(), name="prepare the brain")
        return self._conv

    def invalidate_context(self) -> None:
        """Start a new context epoch with the next turn (a fresh memory snapshot)."""
        self._snapshot = None

    def note(self, text: str, turn: str, ts: float) -> None:
        """Add an announcement (a timer, a reminder) to the history: now, or after the turn under way.

        Args:
            text: what he said.
            turn: its turn id.
            ts: when, in epoch seconds.
        """
        if self._running:
            self._notes.append((text, turn, ts))
        else:
            self.history.append_note(text, turn, ts)

    def exchange(self, text: str, reply: str, turn: str, conv: int) -> None:
        """Add a request answered without the model (an instant command) and its reply to the history, so he knows of
        them at his next turn. Call it between turns (the turn runner holds the turn lock).

        Args:
            text: the request.
            reply: what he said.
            turn: the turn.
            conv: the conversation it was made in (nothing is added to a newer one).
        """
        if conv != self._conv.id:
            return
        now = time.time()
        messages: list[Message] = [{"role": "user", "content": text, "raw": text, "turn": turn, "ts": now},
                                   {"role": "assistant", "content": reply, "turn": turn, "ts": now}]
        if self.history.append_turn(messages):
            self._snapshot = None  # the prefix changed: the next turn starts a new epoch

    # ------------------------------------------------------------------------------------------------ readiness
    async def warm_up(self) -> None:
        """Get the model ready for the first question (Claude: check the sign-in, start a session; Ollama: load the
        model with the start of the prompt read).

        Raises:
            LLMError: the model can't be used, saying why.
        """
        await self.llm.warm_up(self._base_prompt())
        self._warm = True

    async def prepare(self) -> None:
        """Get the model ready for a coming question without making anyone wait (on wake, NEW CHAT, a new toolbox);
        nothing before ``warm_up`` succeeded. Never raises."""
        if not self._warm:
            return
        try:
            await self.llm.prepare(self._base_prompt())
        except Exception:
            log.warning("the brain could not get ready ahead of the question", exc_info=True)

    async def rewarm(self) -> None:
        """Ollama: read the conversation again after the memory review took its one cache slot. Never raises."""
        if self.llm.current().provider != "ollama" or not self._warm or self._running:
            return
        try:
            await self.llm.warm_up(self._base_prompt())
        except LLMError as e:
            log.info("could not read the conversation again: %s", e)

    async def close(self) -> None:
        """Close the model (the runtime has stopped the background work)."""
        if self._prepare_timer is not None:
            self._prepare_timer.cancel()
        await self.llm.close()

    # ------------------------------------------------------------------------------------------------ the turn
    async def respond(self, text: str, *, channel: Channel, turn: str, source: str = "", device: str = "",
                      pictures: Sequence[Image] = ()) -> AsyncIterator[BrainEvent]:
        """Answer one request (see the module docstring).

        Args:
            text: the request, as heard or typed.
            channel: "voice" (the reply will be spoken) or "text" (shown only).
            turn: the turn's id.
            source: where the request came from ("voice", "hud"...): a question before a risky call goes there too.
            device: the HUD it came from or that carries the voice ("" for this machine).
            pictures: pictures the user sent with it (an attachment), prepared for the models.

        Yields:
            ``SkillUsed`` and ``Recalled`` first (when they apply), then ``Delta``, ``RoundEnd`` and ``ToolsRan`` as
            the rounds go, then ``Done``.

        Raises:
            LLMError: the model failed or went quiet.
        """
        started = time.monotonic()
        conv = self._conv
        progress = _Progress()
        used, finished = set[str](), False
        messages: list[Message] = []
        self._running = True  # from here on, announcements wait for the end of the turn
        try:
            snapshot = self._epoch()
            q = Query(text, self.embed(text))
            self._index()
            # A skill runs unasked when named, by a trigger phrase, or by an unmistakable meaning (never by words
            # alone: without vectors a shared word or two would start one).
            skill = self.skills.match(text) or (self.context.router.best_skill(q) if q.vec is not None else None)
            if skill is not None:
                if not skill.trusted:
                    self.hub.taint(turn, f"The {skill.name} skill")  # its steps are someone else's text
                yield SkillUsed(skill.name)
            recalled: tuple[Hit, ...] = ()
            if self.cfg.memory.recall:
                found = self.memory.recall(q, conv=conv.id, exclude=self._recalled)
                if found.items:
                    recalled = found.items
                    self._recalled.update(hit.id for hit in recalled)
                    yield Recalled(recalled, found.ms)
            tail = self.context.tail(now=local_now(), channel=channel, snapshot=snapshot, recalled=recalled,
                                     skill=skill)
            request: Message = {"role": "user", "content": f"{tail}\n\n{text}", "raw": text, "turn": turn,
                                "ts": time.time()}
            if pictures:
                request["images"] = list(pictures)
            messages.append(request)
            active = self._active(q)
            if skill is not None:
                self._extend(active, skill.tools)
            ctx = CallContext("brain", turn, source=source, device=device)
            async for event in self._rounds(self.llm, snapshot=snapshot, conv=conv.id, turn=turn,
                                            history=self.history.messages(), messages=messages, active=active,
                                            request=text, ctx=ctx, used=used, progress=progress):
                yield event
            finished = True
        finally:
            self._running = False
            if not finished:
                self.llm.abort()  # cancelled or failed between rounds: release a model waiting for results
                self._merge_notes()  # the turn is not kept; announcements made meanwhile are
        if conv.id == self._conv.id:
            if self.history.append_turn(messages):
                self._snapshot = None  # the prefix changed: the next turn starts a new epoch
            self._recent = frozenset(used)
        self._merge_notes()
        usage = self.llm.usage
        first, rounds = progress.first, progress.rounds
        log.info("turn %s: first word %s, %.0f ms, %d round%s, tools: %s%s", turn,
                 f"{(first - started) * 1000:.0f} ms" if first else "-", (time.monotonic() - started) * 1000,
                 rounds, "s" * (rounds != 1), ", ".join(sorted(used)) or "none",
                 f", tokens {usage.input} in ({usage.cached} cached), {usage.output} out" if usage else "")
        yield Done(progress.final.strip(), usage, rounds)

    async def run_task(self, text: str, *, llm: LLM, turn: str, source: str, limit: int) -> tuple[str, Usage | None]:
        """Answer a request outside the conversation (a scheduled job): on its own model, with the same system text
        and memory snapshot but no history and no recall, its own list of tools; the conversation, its routed tools
        and its prompt cache are untouched. A skill named or triggered by the request runs as in a turn.

        Args:
            text: the request.
            llm: the model to answer with (its own instance: the conversation's sessions are never borrowed).
            turn: an id for its operations (the trace, the questions before risky calls).
            source: where it comes from ("job").
            limit: the most tool rounds it may chain.

        Returns:
            The reply, and what the last model call cost.

        Raises:
            LLMError: the model failed or went quiet.
        """
        text = redact.redacted(text)
        snapshot = self._epoch()
        q = Query(text, self.embed(text))
        self._index()
        skill = self.skills.match(text) or (self.context.router.best_skill(q) if q.vec is not None else None)
        if skill is not None and not skill.trusted:
            self.hub.taint(turn, f"The {skill.name} skill")
        tail = self.context.tail(now=local_now(), channel="text", snapshot=snapshot, recalled=(), skill=skill)
        messages: list[Message] = [{"role": "user", "content": f"{tail}\n\n{text}", "raw": text, "turn": turn,
                                    "ts": time.time()}]
        routed = not llm.all_tools and len(self.toolset.names()) - 1 > self.cfg.tools.max_per_turn
        active = self.context.router.select(q, self.toolset, frozenset()) if routed else self._every_tool()
        if skill is not None:
            self._extend(active, skill.tools)
        progress, finished = _Progress(), False
        try:
            async for _ in self._rounds(llm, snapshot=snapshot, conv=-1, turn=turn, history=(), messages=messages,
                                        active=active, request=text, ctx=CallContext("brain", turn, source=source),
                                        used=set(), progress=progress, limit=limit):
                pass
            finished = True
        finally:
            if not finished:
                llm.abort()
        return progress.final.strip(), llm.usage

    async def _rounds(self, llm: LLM, *, snapshot: Snapshot, conv: int, turn: str, history: tuple[Message, ...],
                      messages: list[Message], active: list[str], request: str, ctx: CallContext, used: set[str],
                      progress: "_Progress", limit: int | None = None) -> AsyncIterator[BrainEvent]:
        """The model's rounds for one request: stream its text, run the tools it calls, hand back their results, until
        it answers without calling one (past ``limit`` rounds it is told to answer, and then stopped).

        Args:
            llm: the model.
            snapshot: the system text and memory snapshot.
            conv: the conversation (-1 outside one).
            turn: the turn.
            history: earlier turns.
            messages: this turn so far (the request first); grows with every round.
            active: the tools shown (find_tools and use_skill add to it).
            request: the user's words (find_tools' query when the model gives none).
            ctx: who calls the tools.
            used: the tools used (added to).
            progress: the reply so far, the rounds and the first word's time (updated).
            limit: the most tool rounds (default ``brain.max_tool_rounds``).

        Yields:
            ``Delta``, ``RoundEnd`` and ``ToolsRan``.
        """
        limit = self.cfg.brain.max_tool_rounds if limit is None else limit
        for r in range(limit + 2):
            progress.rounds = r + 1
            prompt = Prompt(snapshot.system, snapshot.memory, conv, turn, history, tuple(messages),
                            self.toolset.view(active))
            content, calls = "", list[ToolCall]()
            async for piece in without_secrets(without_thinking(llm.guard(llm.stream(prompt)))):
                if isinstance(piece, list):
                    calls += piece
                    continue
                if not content and needs_space(progress.final, piece):
                    progress.final += " "
                    yield Delta(" ")
                progress.first = progress.first or time.monotonic()
                content += piece
                progress.final += piece
                yield Delta(piece)
            reply: Message = {"role": "assistant", "content": content, "turn": turn, "ts": time.time()}
            if calls:
                reply["tool_calls"] = [call.as_dict() for call in calls]
            messages.append(reply)
            if not calls:
                return
            yield RoundEnd(r)
            if r > limit:  # still calling tools after being told to answer: stop with the limit reply
                llm.abort()
                text_end = self.cfg.prompts.tool_limit_reply.format(honorific=self.cfg.user.honorific)
                piece = (" " if needs_space(progress.final, text_end) else "") + text_end
                progress.final += piece
                messages += [self._tool_message(call, ToolOutput(LIMIT_REACHED), turn) for call in calls]
                messages.append({"role": "assistant", "content": text_end, "turn": turn, "ts": time.time()})
                yield Delta(piece)
                return
            if r == limit:
                results = {call.id: ToolOutput(LIMIT_REACHED) for call in calls}
            else:
                ran = time.monotonic()
                results = await self._run_calls(calls, request=request, turn=turn, active=active, used=used, ctx=ctx)
                yield ToolsRan(time.monotonic() - ran)
            messages += [self._tool_message(call, results[call.id], turn) for call in calls]

    async def _run_calls(self, calls: Sequence[ToolCall], *, request: str, turn: str, active: list[str],
                         used: set[str], ctx: CallContext) -> dict[str, ToolOutput]:
        """Run one round's calls: unknown names and find_tools here, read-only tools together, the rest in order.

        Args:
            calls: the round's calls.
            request: the user's words (find_tools' query when the model gives none).
            turn: the turn.
            active: the tools shown (find_tools and use_skill add to it).
            used: the tools used this turn (added to).
            ctx: who asks: the brain, in this turn, for a request from this source and device.

        Returns:
            Each call's result (text and pictures), by call id.
        """
        plan = plan_calls(calls, self.toolset)
        results: dict[str, ToolOutput] = {}
        missing = NO_TOOL_FIND if self._routing() else NO_TOOL
        for call in plan.unknown:
            results[call.id] = ToolOutput(missing.format(name=call.name))
            self.hub.ops.record(server=CORE_SERVER, name=call.name, title=call.name, ctx=ctx, args=call.arguments,
                                ok=False, result=results[call.id].text)
        for call in plan.find:
            results[call.id] = ToolOutput(self._find(call, request, ctx, active))

        async def run(call: ToolCall) -> None:
            """Run one call through the hub and note what it changed."""
            tool = self.toolset.get(call.name)
            args = call.arguments if tool is None or tool.builtin else coerce(tool, call.arguments)
            output = results[call.id] = await self.hub.call_named_rich(ToolRequest(call.name, args), ctx)
            text = output.text
            used.add(call.name)
            if call.name == "use_skill" and not text.startswith("Error"):
                skill = self.skills.get(str(args.get("name") or ""))
                if skill is not None:
                    self._extend(active, skill.tools)

        async def chain() -> None:
            """Run the calls that change something, one after another."""
            for call in plan.serial:
                await run(call)

        async with asyncio.TaskGroup() as group:
            for call in plan.parallel:
                group.create_task(run(call))
            if plan.serial:
                group.create_task(chain())
        return results

    def _find(self, call: ToolCall, request: str, ctx: CallContext, active: list[str]) -> str:
        """``find_tools``: show the best matches beyond the tools shown, from now on in this conversation."""
        started = time.monotonic()
        query = str(call.arguments.get("query") or request)
        found = self.context.router.search(Query(query, self.embed(query)), self.toolset,
                                           exclude={*active, FIND_TOOLS.name})
        self._extend(active, found)
        lines = [f"- {name}: {clip(tool.summary or tool.description, FIND_SUMMARY_CHARS)}"
                 for name in found if (tool := self.toolset.get(name)) is not None]
        text = "\n".join([FOUND, *lines]) if lines else NOT_FOUND
        self.hub.ops.record(server=CORE_SERVER, name=FIND_TOOLS.name, title=FIND_TOOLS.title, ctx=ctx,
                            args={"query": query}, ok=bool(lines), result=text,
                            ms=round((time.monotonic() - started) * 1000))
        return text

    def _tool_message(self, call: ToolCall, result: ToolOutput, turn: str) -> Message:
        """A tool result as the model reads it, cut to ``brain.tool_result_chars``, with its pictures."""
        limit = self.cfg.brain.tool_result_chars
        text = result.text
        content = text if len(text) <= limit else text[:limit] + TRUNCATED
        message: Message = {"role": "tool", "tool_call_id": call.id, "name": call.name, "content": content,
                            "turn": turn}
        if result.images:
            message["images"] = list(result.images)
        return message

    # ------------------------------------------------------------------------------------------------ internals
    def _merge_notes(self) -> None:
        """Add the announcements made during the turn to the history, after it."""
        for note in self._notes:
            self.history.append_note(*note)
        self._notes.clear()

    def _epoch(self) -> Snapshot:
        """The context epoch's snapshot, made again for a new conversation, a trimmed history or changed prompts."""
        snapshot = self._snapshot
        if snapshot is None or snapshot.conv != self._conv.id or snapshot.system != self.context.system():
            snapshot = self._snapshot = self.context.snapshot(self._conv.id, local_now())
            self._recalled.clear()
        return snapshot

    def embed(self, text: str) -> "NDArray[np.float32] | None":
        """A request's vector once the embedder has loaded (never loaded here, on the loop); None before. The hub's
        query vectors come from here too.

        Args:
            text: the request.

        Returns:
            Its unit vector, or None.
        """
        return self.embedder.encode_query(text) if self.embedder.loaded else None

    def _index(self) -> None:
        """Keep the router's vectors current (a no-op unless the toolbox, the skills or the embedder changed)."""
        router = self.context.router
        router.index_tools(self.toolset)
        router.index_skills(self.skills.all(), self.skills.version)

    def _routing(self) -> bool:
        """Whether the model is shown a routed short list instead of every tool."""
        return not self.llm.all_tools and len(self.toolset.names()) - 1 > self.cfg.tools.max_per_turn

    def _every_tool(self) -> list[str]:
        """Every tool but find_tools, in the hub's order."""
        return [name for name in self.toolset.names() if name != FIND_TOOLS.name]

    def _active(self, q: Query) -> list[str]:
        """The tools shown this turn: every one in the hub's order, or this conversation's routed list (chosen on its
        first turn, then only added to, so the prompt prefix stays the same)."""
        if not self._routing():
            return self._every_tool()
        if not self._routed:
            self._routed = self.context.router.select(q, self.toolset, self._recent)
        return self._routed

    def _extend(self, active: list[str], names: Iterable[str]) -> None:
        """Show more tools from now on (at the end of the list, so the prefix before them stays the same)."""
        active += [name for name in dict.fromkeys(names) if name not in active and self.toolset.get(name) is not None]

    def _base_view(self) -> ToolView:
        """The tools a coming question will most likely be shown (for warm-up and getting ready)."""
        return self.toolset.view(self._every_tool() if not self._routing() else self._routed or self.cfg.tools.always)

    def _base_prompt(self) -> Prompt:
        """The prompt without a question: system text, snapshot, history and the likely tools."""
        snapshot = self._epoch()
        return Prompt(snapshot.system, snapshot.memory, self._conv.id, "", self.history.messages(), (),
                      self._base_view())

    def _prepare_soon(self) -> None:
        """Get ready once toolbox changes settle (servers connect one by one at start)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._prepare_timer is not None:
            self._prepare_timer.cancel()
        self._prepare_timer = loop.call_later(PREPARE_DEBOUNCE_S, self._prepare_now)

    def _prepare_now(self) -> None:
        """The toolbox settled: get ready, unless a turn started meanwhile (it gets the model ready itself)."""
        self._prepare_timer = None
        if not self._running:
            self.spawn(self.prepare(), name="prepare the brain")


# ---------------------------------------------------------------------------------------------------- the turns
@dataclass(slots=True)
class _Live:
    """The running turn's state, for the HUD's ``init`` and the database.

    Attributes:
        id: the turn id.
        conv: its conversation.
        parts: the reply's pieces so far.
        steps: the turn runner's own steps (a recall, a skill); the hub keeps the tools'.
    """

    id: str
    conv: int
    parts: list[str]
    steps: list[StepRecord]


class TurnRunner:
    """Turns, one at a time (see the module docstring); implements the voice session's ``TurnPort``.

    Attributes:
        quiet: VOICE OFF: replies are shown, not spoken.
    """

    def __init__(self, cfg: "Config", bus: EventBus, memory: "MemorySystem", brain: Brain, hub: ToolHub,
                 speech: SpeechPort, reviewer: "Reviewer | None", spawn: Spawner, *,
                 instant: "Instant | None" = None) -> None:
        """Prepare; nothing runs until a request arrives.

        Args:
            cfg: the live settings.
            bus: the HUD's event bus.
            memory: Orion's memory (role "main").
            brain: the agent loop.
            hub: the tool hub (each turn's steps, and forgetting a turn).
            speech: the voice.
            reviewer: the memory review (paused while he talks), None when there is none.
            spawn: the runtime's way of running background work.
            instant: instant commands, tried before the model; None when there are none.
        """
        self.cfg = cfg
        self.bus = bus
        self.memory = memory
        self.brain = brain
        self.hub = hub
        self.speech = speech
        self.reviewer = reviewer
        self.spawn = spawn
        self.instant = instant
        self.quiet = False
        self.hud_open: Callable[[], bool] = lambda: False  # the runtime says whether a HUD is connected
        self._state: AssistantState = "idle"
        self._loading = ""
        self._loaded = asyncio.Event()
        self._loaded.set()
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[Any] | None = None  # the running turn
        self._stopping: asyncio.Task[Any] | None = None  # the turn interrupt() cancelled
        self._live: _Live | None = None
        self._reply: asyncio.Task[Any] | None = None  # the last reply_done
        self._tasks: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------------------------------------ state
    @property
    def state(self) -> AssistantState:
        """What he is doing, as the HUD shows it."""
        return self._state

    def set_state(self, state: AssistantState, **extra: JSON) -> None:
        """Show what he is doing (a change, or extra fields, publishes a ``state`` event).

        Args:
            state: "idle", "loading", "listening", "thinking" or "speaking".
            **extra: more fields (``model`` while loading).
        """
        if state == self._state and not extra:
            return
        self._state = state
        self.bus.publish("state", state=state, **extra)

    @property
    def busy(self) -> bool:
        """Whether a turn is running."""
        return self._lock.locked()

    @property
    def loading(self) -> str:
        """The brain being switched to ("" when none is loading)."""
        return self._loading

    def set_loading(self, model: str) -> None:
        """While another brain gets ready, show it and hold new requests until it is.

        Args:
            model: the brain's label, "" once it is ready.
        """
        self._loading = model
        if model:
            self._loaded.clear()
            self.set_state("loading", model=model)
            return
        self._loaded.set()
        if self._state == "loading":
            self.set_state("idle")

    def live(self) -> LiveTurn | None:
        """The running turn for a HUD that connects in the middle of it, None when none runs."""
        live = self._live
        if live is None:
            return None
        steps = sorted([*live.steps, *self.hub.ops.turn_steps(live.id)], key=lambda step: step["at"])
        return LiveTurn(id=live.id, text="".join(live.parts), steps=steps)

    # ------------------------------------------------------------------------------------------------ requests
    def submit(self, text: str, source: TurnSource, *, device: str = "", pictures: Sequence[Image] = (),
               heard: Heard | None = None) -> "asyncio.Task[TurnResult]":
        """Start a turn for a request without waiting for it.

        Args:
            text: the request.
            source: "voice", "hud", "discord" or "job".
            device: the HUD it came from or that carries the voice ("" for this machine).
            pictures: pictures sent with it.
            heard: when a spoken request ended and was heard (for the turn's timings); None for a typed one.

        Returns:
            The turn's task.
        """
        task = asyncio.get_running_loop().create_task(
            self.handle(text, source, device=device, pictures=pictures, heard=heard), name="turn")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def handle(self, text: str, source: TurnSource, *, device: str = "", pictures: Sequence[Image] = (),
                     heard: Heard | None = None) -> TurnResult:
        """Run a turn: wait for the one under way (and a brain switch), then answer.

        Args:
            text: the request (cut to ``hud.max_message_chars``).
            source: "voice", "hud", "discord" or "job".
            device: the HUD it came from or that carries the voice ("" for this machine).
            pictures: pictures sent with it.
            heard: when a spoken request ended and was heard (for the turn's timings); None for a typed one.

        Returns:
            How it ended. Its speech may still be playing: the reply's ``reply_done`` runs on in the background.
        """
        text = redact.redacted(text.strip())[:self.cfg.hud.max_message_chars]  # a secret said aloud or typed goes no further
        if not text:
            return TurnResult("", interrupted=False)
        if self.hud_open() and (asked := summon(text, Summons.from_config(self.cfg))) is not None:
            self.bus.publish("summon", action=asked.action, panels=list(asked.panels), source=source)
            log.info("summoned: %s %s", asked.action, ", ".join(asked.panels))
            turn = new_id()
            self._reply = self.spawn(self.speech.reply_done(turn, source), name=f"reply {turn}")  # keeps listening
            return TurnResult(turn, interrupted=False)
        if self.reviewer is not None:
            self.reviewer.interrupt()
        await self._loaded.wait()
        async with self._lock:
            self._task = asyncio.current_task()
            self.speech.stop_speaking()  # a new request silences what is left of the previous reply
            try:
                result, clock = await self._run(text, source, device, pictures, heard)
            finally:
                self._task = self._live = None
        if result.interrupted:
            if self._state == "thinking":
                self.set_state("loading" if self._loading else "idle")
        else:
            self._reply = self.spawn(self._spoken(result.turn, source, clock), name=f"reply {result.turn}")
        if self.reviewer is not None:
            self.reviewer.arm()
        return result

    def interrupt(self) -> None:
        """STOP: silence him and cancel the turn under way."""
        self.speech.stop_speaking()
        task = self._task
        if task is not None and not task.done():
            self._stopping = task
            task.cancel()

    async def _spoken(self, turn: str, source: TurnSource, clock: TurnClock) -> None:
        """The voice's part after a reply (see ``SpeechPort.reply_done``); then the turn's clock hears that his speech
        is over, so a reply that never sounded gets its timings too."""
        try:
            await self.speech.reply_done(turn, source)
        finally:
            clock.silence()

    async def idle(self) -> None:
        """Return once no turn runs and the last reply has been said (``tools.core`` waits for this)."""
        while True:
            async with self._lock:
                pass
            reply = self._reply
            if reply is not None and not reply.done():
                await asyncio.wait({reply})
            if not self._lock.locked():
                return

    def new_conversation(self) -> None:
        """NEW CHAT: a fresh conversation (a turn under way ends in the old one); the last one is reviewed."""
        conv = self.brain.new_conversation("new_chat")
        self.bus.publish("conversation_reset", conv=conv.id, reason="new_chat")
        if self.reviewer is not None:
            self.reviewer.review_now()

    def announce(self, text: str, title: str = "ORION", *, spoken: bool = True) -> None:
        """Say something unasked (a timer, a reminder, a job's result): shown and logged now, spoken after a turn under
        way.

        Args:
            text: what to say.
            title: the HUD's and the desktop notification's title.
            spoken: whether to say it aloud too (only its chime otherwise).
        """
        text = redact.redacted(text)
        turn, now = new_id(), time.time()
        store = self.memory.store
        store.submit(store.log_message, "assistant", text, source="announcement", conv=self.brain.conv.id, turn=turn,
                     ts=now)
        self.brain.note(text, turn, now)
        self.bus.publish("announcement", id=turn, title=title, text=text)
        self.spawn(asyncio.to_thread(notify, title, text), name="notify")
        if self.busy:
            self.spawn(self._announce_after_turn(text, spoken), name="announce")
        else:
            self._say_announcement(text, spoken)

    def on_wake(self) -> None:
        """He was woken: pause the memory review and get the brain ready for the question."""
        if self.reviewer is not None:
            self.reviewer.interrupt()
        self.spawn(self.brain.prepare(), name="prepare the brain")

    async def close(self) -> None:
        """Cancel the turn under way and the turns waiting."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------------------------------------------ one turn
    async def _run(self, text: str, source: TurnSource, device: str, pictures: Sequence[Image],
                   heard: Heard | None) -> tuple[TurnResult, TurnClock]:
        """One turn under the lock: events, speech, logs; errors become an apology.

        Returns:
            How it ended, and the turn's clock (it hears when his speech is over).
        """
        started = time.monotonic()
        now = time.time()
        turn = new_id()
        clock = TurnClock(turn, source, heard=heard, done=self._timed)
        conv, fresh = self.brain.begin_turn(now)
        if fresh:
            self.bus.publish("conversation_reset", conv=conv.id, reason="idle_gap")
        spoken = not self.quiet and source in ("voice", "hud")  # a request from Discord is answered there, silently
        channel: Channel = "voice" if spoken else "text"
        store = self.memory.store
        store.submit(store.log_message, "user", text, source=source, conv=conv.id, turn=turn, ts=now)
        live = self._live = _Live(turn, conv.id, [], [])
        self.bus.publish("user_message", text=text, source=source, turn=turn, device=device,
                         images=[images.data_url(picture) for picture in pictures])
        self.set_state("thinking")
        stream = self.speech.reply_stream(turn, spoken=spoken, clock=clock)
        final, rounds, quick = "", 0, False
        try:
            if (answer := await self._instantly(text, source, device, pictures, turn, clock)) is not None:
                quick, final = True, answer
                self.bus.publish("assistant_start", id=turn, conv=conv.id, channel=channel, instant=True)
                live.parts.append(answer)
                clock.output()
                self.bus.publish("assistant_delta", id=turn, text=answer, instant=True)
                stream.feed(answer)
                self.brain.exchange(text, answer, turn, conv.id)
            else:
                self.bus.publish("assistant_start", id=turn, conv=conv.id, channel=channel)
                async for event in self.brain.respond(text, channel=channel, turn=turn, source=source, device=device,
                                                      pictures=pictures):
                    match event:
                        case Delta(text=piece):
                            live.parts.append(piece)
                            clock.output()
                            self.bus.publish("assistant_delta", id=turn, text=piece)
                            stream.feed(piece)
                        case RoundEnd():
                            stream.boundary()
                        case ToolsRan(seconds=seconds):
                            clock.tools(seconds)
                        case Recalled(items=items, ms=ms):
                            self._recalled(live, items, ms)
                        case SkillUsed(name=name):
                            self._skill_used(live, name)
                        case Done(text=reply, rounds=count):
                            final, rounds = reply, count
        except asyncio.CancelledError:
            clock.cancel()
            task = asyncio.current_task()
            ours = task is not None and self._stopping is task
            self._stopping = None
            self.bus.publish("assistant_interrupted", id=turn)
            self._finish(live, "".join(live.parts).strip(), source)
            if not ours or task is None or task.uncancel() > 0:
                raise
            return TurnResult(turn, interrupted=True, text="".join(live.parts).strip()), clock
        except LLMError as e:
            log.warning("turn failed: %s", e)
            clock.cancel()
            return self._failed(str(e), live, source, stream, started), clock
        except Exception as e:
            log.exception("turn failed")
            clock.cancel()
            return self._failed(f"an internal error ({type(e).__name__})", live, source, stream, started), clock
        stream.close()
        self.bus.publish("assistant_done", id=turn, text=final, ms=round((time.monotonic() - started) * 1000),
                         rounds=rounds, **({"instant": True} if quick else {}))
        clock.end(INSTANT if quick else self._answered_by(), instant=quick)
        self._finish(live, final, source)
        return TurnResult(turn, interrupted=False, text=final), clock

    async def _instantly(self, text: str, source: TurnSource, device: str, pictures: Sequence[Image], turn: str,
                         clock: TurnClock) -> str | None:
        """Answer a request without the model when it is an instant command (``orion.instant``), its tools run as the
        user's own command. Never raises: a bug there must not cost the user their request.

        Args:
            text: the request.
            source: where it came from.
            device: the HUD it came from or that carries the voice.
            pictures: pictures sent with it (a request about a picture is the model's).
            turn: the turn.
            clock: the turn's clock (told how long the tools took).

        Returns:
            What he says; None when it isn't an instant command, can't be done exactly as said, or failed (the model
            answers then).
        """
        instant = self.instant
        if instant is None or pictures:
            return None
        try:
            match = await instant.match(text)
            plan = await instant.plan(match, text) if match is not None else None
            if plan is None:
                return None
            outcome = await instant.execute(plan, CallContext("user", turn, source=source, device=device))
        except Exception:
            log.warning("an instant command failed: the model takes the request", exc_info=True)
            return None
        if plan.calls:
            clock.tools(outcome.ms / 1000)
        return outcome.reply or None

    def _answered_by(self) -> str:
        """The model answering now, as the HUD names it (the stand-in's during an outage)."""
        return label(self.cfg, self.brain.llm.current().provider)

    def _timed(self, row: Timing) -> None:
        """A turn's timings are known: keep them (queued, never waited for) and tell the HUD."""
        store = self.memory.store
        store.submit(store.log_timing, row)
        self.bus.publish("timing", **row)

    def _failed(self, why: str, live: _Live, source: TurnSource, stream: ReplySpeech, started: float) -> TurnResult:
        """A turn failed: apologise (shown, and spoken after an error chime), and log it.

        Args:
            why: the reason, in words.
            live: the turn.
            source: where its request came from.
            stream: its speech so far.
            started: when it started (monotonic seconds).

        Returns:
            How it ended.
        """
        apology = self.cfg.prompts.error_reply.format(honorific=self.cfg.user.honorific, error=redact.redacted(why))
        stream.close()
        spoken = not self.quiet and source in ("voice", "hud")
        if spoken:
            self.speech.chime("error")
        said = self.speech.reply_stream(live.id, spoken=spoken)
        said.feed(apology)
        said.close()
        text = "\n\n".join(part for part in ("".join(live.parts).strip(), apology) if part)
        self.bus.publish("assistant_done", id=live.id, text=text, error=True,
                         ms=round((time.monotonic() - started) * 1000), rounds=0)
        self._finish(live, text, source)
        return TurnResult(live.id, interrupted=False, text=text)

    def _finish(self, live: _Live, reply: str, source: TurnSource) -> None:
        """Log the reply and the turn runner's steps (queued, never waited for), index the turn, forget it."""
        store = self.memory.store
        if reply:
            store.submit(store.log_message, "assistant", reply, source=source, conv=live.conv, turn=live.id,
                         ts=time.time())
        for step in live.steps:
            store.submit(store.log_step, step, live.conv)
        self.memory.after_turn()
        self.hub.end_turn(live.id)

    def _recalled(self, live: _Live, items: Sequence["Hit"], ms: float) -> None:
        """Memories went into the turn's tail: tell the HUD and keep the step."""
        texts = [clip(hit.text, RECALL_TEXT_CHARS) for hit in items]
        self.bus.publish("recall", turn=live.id, items=[{"id": hit.key, "text": shown}
                                                        for hit, shown in zip(items, texts, strict=True)],
                         ms=round(ms, 1))
        live.steps.append(_step(live.id, f"r-{live.id}", "recall", "recall", "Recalled",
                                {"memories": " · ".join(texts)}, "\n".join(f"• {t}" for t in texts), ms))

    def _skill_used(self, live: _Live, name: str) -> None:
        """A skill went before the model: tell the HUD and keep the step."""
        self.bus.publish("skill_used", name=name, turn=live.id, call="")
        live.steps.append(_step(live.id, f"s-{live.id}-{name}", "skill", name, "Skill", {"name": name}, "", 0.0))

    async def _announce_after_turn(self, text: str, spoken: bool) -> None:
        """Speak an announcement once the turn under way is over."""
        async with self._lock:
            pass
        self._say_announcement(text, spoken)

    def _say_announcement(self, text: str, spoken: bool = True) -> None:
        """Speak an announcement (only its chime while replies are not spoken, or when it is only shown)."""
        if self.quiet or not spoken:
            self.speech.chime("done")
        else:
            self.speech.announce(text)
