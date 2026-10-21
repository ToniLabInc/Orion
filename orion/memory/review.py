"""The memory review: after a conversation goes quiet, the brain re-reads it and keeps what matters.

Facts the model forgot to save in the moment still reach USER.md or MEMORY.md, corrected facts are updated by id, a
nearly full file gets room (entries demoted to facts.md), and a short entry goes into today's journal with the
conversation's concepts. It never runs on the reply path:
- event-driven: ``arm`` (after every turn) starts the idle timer; ``interrupt`` (a turn, the wake word) stops it and
  any review under way; no polling;
- windowed: unreviewed messages are cut at turn boundaries into windows of ``memory.review_window_chars``, oldest first,
  and the review mark advances window by window, so a long chat is reviewed whole;
- careful with provenance: the answers of turns that drew on web pages or other apps are left out of the transcript, so
  external text never reaches the review as if the user had said it;
- careful with secrets: the transcript is redacted before the model reads it (a secret kept in the vault since the chat is
  cut out), and so is the journal entry; the writer refuses an entry that holds one;
- never skipped silently: a failed window stays unreviewed and is retried with a growing delay (5 minutes to an hour);
  the first failure of a streak is shown in the HUD.

The LLM comes in as a callable (whichever brain runs now); with Ollama, ``on_reviewed`` lets the runtime prefill the
conversation again afterwards.
"""

import asyncio
import contextlib
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, OnErrorOmit, ValidationError, WrapValidator
from pydantic_core import PydanticUseDefault

from orion import redact
from orion.base import EventBus, LLMError, Message, conversations_logged, format_transcript, local_now
from orion.memory.store import MessageRow
from orion.memory.vault import FILES, Unit, short_ids
from orion.memory.writer import Change, Op, resolve, used_chars

if TYPE_CHECKING:
    from orion.config import Config
    from orion.llm import LLM
    from orion.memory import MemorySystem

log = logging.getLogger("orion.review")

RETRY_S: Final = 300  # the first retry after a failed review
RETRY_MAX_S: Final = 3600  # the longest wait between retries
BUSY_REARM_S: Final = 30  # a review due while he is busy waits this long
FACTS_IN_PROMPT: Final = 30  # facts.md entries shown to the review
MESSAGES_PER_RUN: Final = 2000  # unreviewed messages read at once (a run reviews them window by window)
FULL_AT: Final = 0.9  # a curated file this full is pointed out, so the review can demote entries


def _or_default(value: Any, handler: Any) -> Any:
    """A model may not follow the schema exactly: a field it got wrong is left at its default, the rest counts."""
    try:
        return handler(value)
    except ValidationError:
        raise PydanticUseDefault from None


_Lenient = WrapValidator(_or_default)
_Words = Annotated[list[OnErrorOmit[str]], _Lenient]  # an item that isn't text is dropped
_STRICT = ConfigDict(json_schema_serialization_defaults_required=True,
                     json_schema_extra={"additionalProperties": False})  # what structured output needs


class OpIn(BaseModel):
    """One operation as the model returns it."""

    model_config = _STRICT

    op: Literal["ADD", "UPDATE", "DELETE", "NOOP"]
    target: Annotated[Literal["user", "orion"], _Lenient] = "user"
    id: Annotated[str, _Lenient] = ""
    text: Annotated[str, _Lenient] = ""
    demote: Annotated[bool, _Lenient] = False


class ReviewOut(BaseModel):
    """The review's answer: operations, a journal entry and the conversation's concepts."""

    model_config = _STRICT

    ops: Annotated[list[OnErrorOmit[OpIn]], _Lenient] = []
    journal: Annotated[str, _Lenient] = ""
    concepts: _Words = []


def _inline(schema: dict[str, Any]) -> dict[str, Any]:
    """A JSON schema with its $defs put in place (Ollama's format= and some structured outputs skip $ref)."""
    defs = schema.pop("$defs", {})

    def walk(node: Any) -> Any:
        """Replace every $ref with its definition."""
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(defs[node["$ref"].rsplit("/", 1)[-1]])
            return {key: walk(value) for key, value in node.items()}
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    return walk(schema)


REVIEW_SCHEMA: Final[dict[str, Any]] = _inline(ReviewOut.model_json_schema(mode="serialization"))


@dataclass(frozen=True, slots=True)
class Window:
    """A stretch of the conversation reviewed in one call.

    Attributes:
        last_id: its last message's id (the review mark moves here once it is done).
        text: the transcript: "[HH:MM] User: ..." and "[HH:MM] Orion: ..." lines.
    """

    last_id: int
    text: str


# ---------------------------------------------------------------------------------------------------- pure parts
def _message(row: MessageRow) -> Message:
    """A logged message as a transcript message."""
    return {"role": "user" if row["role"] == "user" else "assistant", "content": row["content"], "turn": row["turn"],
            "ts": row["ts"]}


def _render(rows: Sequence[MessageRow], tainted: Collection[str]) -> str:
    """A transcript of messages; answers of tainted turns left out."""
    return format_transcript([_message(r) for r in rows], user="User", me="Orion", tools=False, times=True,
                             omit_turns=tainted)


def windows(rows: Sequence[MessageRow], tainted: Collection[str], max_chars: int) -> list[Window]:
    """Cut unreviewed messages into windows at turn boundaries.

    A window holds whole turns up to ``max_chars``. A turn longer than that gets a window of its own, its user lines
    whole and its replies shortened to fit.

    Args:
        rows: the messages, oldest first.
        tainted: turns whose answers drew on web or app content (left out).
        max_chars: the most characters of one window's transcript.

    Returns:
        The windows, oldest first.
    """
    groups: list[list[MessageRow]] = []
    for row in rows:
        if groups and row["turn"] and groups[-1][0]["turn"] == row["turn"]:
            groups[-1].append(row)
        else:
            groups.append([row])
    out: list[Window] = []
    current: list[MessageRow] = []
    size = 0
    for group in groups:
        text = _render(group, tainted)
        if len(text) > max_chars:
            if current:
                out.append(Window(current[-1]["id"], _render(current, tainted)))
                current, size = [], 0
            out.append(Window(group[-1]["id"], _shorten(group, tainted, max_chars)))
            continue
        if current and size + 1 + len(text) > max_chars:
            out.append(Window(current[-1]["id"], _render(current, tainted)))
            current, size = [], 0
        current += group
        size += len(text) + (1 if size else 0)
    if current:
        out.append(Window(current[-1]["id"], _render(current, tainted)))
    return out


def _shorten(group: Sequence[MessageRow], tainted: Collection[str], max_chars: int) -> str:
    """One over-long turn: the user's words whole, the replies cut evenly until the transcript fits (only user words
    longer than the window itself can leave it over)."""
    users = [r for r in group if r["role"] == "user"]
    replies = [r for r in group if r["role"] != "user"]
    order = {r["id"]: i for i, r in enumerate(group)}
    each = max(100, (max_chars - len(_render(users, tainted))) // max(1, len(replies)))
    text = _render(group, tainted)
    for _ in range(4):
        cut = [MessageRow({**r, "content": r["content"] if len(r["content"]) <= each
                           else r["content"][:each].rsplit(" ", 1)[0] + " ..."}) for r in replies]
        text = _render(sorted([*users, *cut], key=lambda r: order[r["id"]]), tainted)
        if len(text) <= max_chars or each <= 100 or not replies:
            break
        each = max(100, each - math.ceil((len(text) - max_chars) / len(replies)))
    return text


def _lines(units: Sequence[Unit], ids: Mapping[str, str]) -> str:
    """Entries as the review reads them: "- [#3fa2] text" lines ("(empty)" when there are none)."""
    return "\n".join(f"- [#{ids[unit.key]}] {unit.plain}" for unit in units) or "(empty)"


def render_review(template: str, *, user: Sequence[Unit], orion: Sequence[Unit], facts: Sequence[Unit],
                  window: Window, fill: Mapping[str, float], max_concepts: int) -> str:
    """The review's prompt.

    Args:
        template: ``prompts.memory_review``.
        user: USER.md's entries.
        orion: MEMORY.md's entries.
        facts: facts.md's entries (the newest ``FACTS_IN_PROMPT``).
        window: the stretch of conversation.
        fill: how full each curated file is (file name -> share of its cap).
        max_concepts: ``memory.review_max_concepts``.

    Returns:
        The prompt.
    """
    entries = [*user, *orion, *facts]
    ids = dict(zip((u.key for u in entries), short_ids([u.key for u in entries]), strict=True))
    shown_fill = "\n".join(f"- {name} is {share:.0%} full" + (": demote the least important entries (DELETE with "
                                                             "demote true) to make room" if share >= FULL_AT else "")
                           for name, share in fill.items())
    return template.format(user=_lines(user, ids), orion=_lines(orion, ids), facts=_lines(facts, ids),
                           fill=shown_fill, transcript=window.text, max_concepts=max_concepts)


def to_ops(out: ReviewOut, *, limits: tuple[int, int, int] | None = None) -> list[Op]:
    """The review's operations as the writer takes them.

    "#3fa2" becomes "3fa2"; an UPDATE without an id but with text becomes an ADD; an operation missing what it needs
    is dropped. With ``limits`` (new facts about the user, new notes, updates and removals), the excess is dropped.

    Args:
        out: the review's answer.
        limits: (``review_max_user_facts``, ``review_max_notes``, ``review_max_updates``), or None for no caps.

    Returns:
        The operations, in order.
    """
    ops: list[Op] = []
    counts = {"user": 0, "orion": 0, "change": 0}
    for item in out.ops:
        ref = item.id.strip().strip("[]").lstrip("#").strip()
        text = " ".join(item.text.split())
        kind = item.op
        if kind == "UPDATE" and not ref and text:
            kind = "ADD"
        if kind == "NOOP" or (kind == "ADD" and not text) or (kind == "UPDATE" and not text) or (
                kind in ("UPDATE", "DELETE") and not ref):
            continue
        bucket = item.target if kind == "ADD" else "change"
        if limits is not None:
            cap = {"user": limits[0], "orion": limits[1], "change": limits[2]}[bucket]
            if counts[bucket] >= cap:
                continue
        counts[bucket] += 1
        ops.append(Op(kind, item.target, id=ref if kind != "ADD" else "", text=text, demote=item.demote))
    return ops


def _counted(listed: Sequence[str]) -> str:
    """The review's changes as counts, for a log that keeps no memories: "2 saved, 1 updated, journal entry"."""
    words = {"+": "saved", "~": "updated", "-": "removed", ">": "moved to facts.md"}
    counts: dict[str, int] = {}
    for item in listed:
        what = words.get(item[:1], item)
        counts[what] = counts.get(what, 0) + 1
    return ", ".join(what if what == "journal entry" else f"{n} {what}" for what, n in counts.items())


def _summary(changes: Sequence[Change]) -> list[str]:
    """The changes as the HUD's memory_review event lists them."""
    marks = {"saved": "+", "overflow": "+", "updated": "~", "promoted": "~", "removed": "-", "demoted": ">"}
    return [f"{marks[c.status]} {c.text}" for c in changes if c.status in marks]


# ---------------------------------------------------------------------------------------------------- the reviewer
class Reviewer:
    """Runs the review after quiet spells (see the module docstring). Use it on the event loop."""

    def __init__(self, cfg: "Config", memory: "MemorySystem", bus: EventBus, llm: Callable[[], "LLM"],
                 is_busy: Callable[[], bool], on_reviewed: Callable[[], Awaitable[None]] | None = None) -> None:
        """Prepare; nothing runs until ``start`` or ``arm``.

        Args:
            cfg: the live settings (``memory.*``, ``prompts.memory_review``).
            memory: Orion's memory (role "main").
            bus: the HUD's event bus.
            llm: returns the brain in use now.
            is_busy: whether a turn is running (a review then waits).
            on_reviewed: awaited after a review changed anything (Ollama: prefill the conversation again).
        """
        self.cfg = cfg
        self.memory = memory
        self.bus = bus
        self.llm = llm
        self.is_busy = is_busy
        self.on_reviewed = on_reviewed
        self._timer: asyncio.TimerHandle | None = None
        self._task: asyncio.Task[None] | None = None
        self._failures = 0
        self._retry_at = 0.0

    def start(self) -> None:
        """Arm the timer, so messages left unreviewed (after a restart, say) are reviewed once he is idle."""
        self.arm()

    def arm(self, delay_s: float | None = None) -> None:
        """Start (or restart) the idle timer.

        Args:
            delay_s: the wait, ``memory.review_after_idle_s`` by default.
        """
        if self._timer is not None:
            self._timer.cancel()
        wait = self.cfg.memory.review_after_idle_s if delay_s is None else delay_s
        self._timer = asyncio.get_running_loop().call_later(wait, self._fire, False)

    def interrupt(self) -> None:
        """The user is talking: stop the timer and any review under way (the next turn arms it again)."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._task is not None and not self._task.done():
            self._task.cancel()

    def review_now(self) -> None:
        """Review now (NEW CHAT, the HUD's button, a tool), unless he is busy."""
        if self._timer is not None:
            self._timer.cancel()
        self._fire(True)

    async def stop(self) -> None:
        """Stop the timer and wait for a review under way to end (it is cancelled)."""
        self.interrupt()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    # ------------------------------------------------------------------------------------------------ internals
    def _mark(self) -> int:
        """The id of the last message reviewed (a database read: call it in a worker thread)."""
        value = self.memory.store.get_setting("reviewed_message_id", "0")
        return int(value) if value.isdigit() else 0

    def _unreviewed(self) -> tuple[list[MessageRow], set[str]]:
        """The next messages to review and their tainted turns (in a worker thread)."""
        store = self.memory.store
        rows = store.messages_after(self._mark(), MESSAGES_PER_RUN)
        return rows, store.tainted_turns({r["turn"] for r in rows if r["turn"]})

    def _fire(self, forced: bool) -> None:
        """The timer went off: review, unless reviews are off, he is busy, or a retry is not due yet."""
        self._timer = None
        if not self.cfg.memory.review or (self._task is not None and not self._task.done()):
            return
        if self.is_busy():
            self.arm(BUSY_REARM_S)
            return
        if not forced and (left := self._retry_at - time.time()) > 0:
            self.arm(left)
            return
        self._task = asyncio.get_running_loop().create_task(self._run(), name="memory-review")

    async def _run(self) -> None:
        """Review the unreviewed windows; back off after a failure."""
        try:
            changed = await self._review()
        except asyncio.CancelledError:
            if (task := asyncio.current_task()) is not None and task.cancelling():
                raise
            log.info("memory review postponed (you started talking)")
            return
        except Exception as e:
            log.debug("memory review error", exc_info=True)
            await self._failed(e)
            return
        self._failures = 0
        self._retry_at = 0.0
        if changed and self.on_reviewed is not None:
            await self.on_reviewed()

    async def _review(self) -> bool:
        """Review every unreviewed window in order (returns whether memory changed)."""
        store, mem = self.memory.store, self.cfg.memory
        changed = False
        while True:
            rows, tainted = await asyncio.to_thread(self._unreviewed)
            if not rows:
                return changed
            if any(r["role"] == "user" for r in rows):
                for window in windows(rows, tainted, mem.review_window_chars):
                    changed |= await self._review_window(window)
                    await store.call(store.set_setting, "reviewed_message_id", str(window.last_id))
            else:  # announcements only: nothing to learn
                await store.call(store.set_setting, "reviewed_message_id", str(rows[-1]["id"]))
            if len(rows) < MESSAGES_PER_RUN:
                return changed

    async def _review_window(self, window: Window) -> bool:
        """Review one window: ask, apply the operations, write the journal; publish what changed."""
        vault, writer, mem = self.memory.vault, self.memory.writer, self.cfg.memory
        user, orion, facts = await asyncio.to_thread(lambda: [vault.entries(kind) for kind in ("user", "orion", "facts")])
        fill = {FILES["user"]: _share(user, mem.user_chars), FILES["orion"]: _share(orion, mem.self_chars)}
        prompt = render_review(self.cfg.prompts.memory_review, user=user, orion=orion,
                               facts=facts[-FACTS_IN_PROMPT:], window=Window(window.last_id, redact.redacted(window.text)),
                               fill=fill, max_concepts=mem.review_max_concepts)
        data = await self.llm().json(prompt, REVIEW_SCHEMA, temperature=mem.review_temperature)
        out = ReviewOut.model_validate(data)
        ops = self._fallback(to_ops(out, limits=(mem.review_max_user_facts, mem.review_max_notes,
                                                 mem.review_max_updates)), [*user, *orion, *facts])
        changes = await asyncio.to_thread(writer.apply, ops, source="review") if ops else []
        listed = _summary(changes)
        concepts = out.concepts[:mem.review_max_concepts]
        if journal := redact.redacted(out.journal).strip():
            await asyncio.to_thread(vault.append_journal, journal, concepts, local_now())
            listed.append("journal entry")
        if listed:
            log.info("memory review: %s", "; ".join(listed) if conversations_logged() else _counted(listed))
            self.bus.publish("memory_review", changes=listed)
        return bool(listed)

    @staticmethod
    def _fallback(ops: Sequence[Op], entries: Sequence[Unit]) -> list[Op]:
        """An UPDATE whose id matches no entry becomes an ADD of its text (a DELETE of nothing is dropped)."""
        out: list[Op] = []
        for op in ops:
            if op.op in ("UPDATE", "DELETE") and resolve(op.id, entries) is None:
                if op.op == "UPDATE":
                    out.append(Op("ADD", op.target, text=op.text))
                continue
            out.append(op)
        return out

    async def _failed(self, error: Exception) -> None:
        """A review failed: keep the window, retry later (5 minutes, doubling up to an hour), tell the HUD once."""
        self._failures += 1
        delay = min(RETRY_S * 2 ** (self._failures - 1), RETRY_MAX_S)
        self._retry_at = time.time() + delay
        reason = str(error) if isinstance(error, (LLMError, json.JSONDecodeError)) else type(error).__name__
        if isinstance(error, LLMError):
            log.warning("memory review failed: %s (retrying in %d min)", error, delay // 60)
        else:
            log.warning("memory review failed (retrying in %d min)", delay // 60, exc_info=error)
        self.arm(delay)
        if self._failures == 1:
            store = self.memory.store
            pending = await asyncio.to_thread(lambda: store.count_messages_after(self._mark()))
            self.bus.publish("memory_review", changes=[], error=reason, pending=pending)


def _share(entries: Sequence[Unit], cap: int) -> float:
    """How full a curated file is: its entries' characters over its cap."""
    return used_chars(entries) / cap if cap else 0.0
