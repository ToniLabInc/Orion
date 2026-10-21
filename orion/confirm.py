"""Asking the user before a risky action: one question, every way it can be answered, the first answer wins.

The hub asks (``MCPHub.call``) when ``tools.policy.confirm_reason`` says a call of the model needs the user's yes:
after the request has read web or app content, before anything that changes or sends something; before the tools the
user listed in ``safety.always_confirm`` and those a channel always asks about (``safety.channels``); and every time
before running a command outside the sandbox or adding, changing or removing a server. ``Confirmer.ask``:

- publishes a ``confirm`` event: every open HUD shows the question with its details, and its answer comes back as a
  ``confirm`` message (``answer``);
- asks aloud when the request was spoken (``VoiceSession.ask_aloud``), and through any other channel that registered
  an asker for the request's source (Discord asks with buttons);
- waits at most ``safety.confirm_timeout_s``: no answer means no. The others are told the question is settled
  (``confirm_done``), and a channel still asking is cancelled.

The model only ever learns the outcome ("the user said no"): it can't answer for the user, because every answer
comes from the user's side (the HUD, the microphone, Discord), never from a tool call.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Literal

from orion.base import JSON, EventBus, clip, new_id

if TYPE_CHECKING:
    from orion.config import Config

log = logging.getLogger("orion.confirm")

type Answer = Literal["yes", "no", "timeout"]
type Asker = Callable[["Question", float], Awaitable[bool | None]]  # asks one way; None: no answer that way

DETAIL_CHARS = 600  # the arguments as the HUD shows them
ACTION_ARG_CHARS = 140  # the main argument as the question says it


@dataclass(frozen=True, slots=True)
class Question:
    """One question to the user.

    Attributes:
        id: its id (the HUD answers with it).
        turn: the turn whose call waits ("" for none).
        tool: the tool's model-visible name.
        title: the tool's title for people ("Run a shell command").
        server: the server that runs it.
        action: what would be done, in words ("run a shell command (rm -rf ~/tmp)").
        detail: the call's arguments, as the HUD shows them.
        reason: "content" (the request read web or app content), "always" (``safety.always_confirm``, or a change of his
            reach), "channel" (the place the request came from always asks about it) or "outside_sandbox".
        source: what the request read ("Read a web page"), "" for the other reasons.
        text: the question as it is said and shown.
        source_kind: where the request came from ("voice", "hud", "discord", "job"...).
        expires: when no answer becomes no, in epoch seconds.
        device: the request's device: a HUD's id, or the chat server it came through ("discord"); "" for none.
    """

    id: str
    turn: str
    tool: str
    title: str
    server: str
    action: str
    detail: str
    reason: str
    source: str
    text: str
    source_kind: str
    expires: float
    device: str = ""

    def as_event(self) -> dict[str, JSON]:
        """The question as the HUD's ``confirm`` event carries it."""
        return {key: value for key, value in asdict(self).items()}


def describe_action(title: str, args: Mapping[str, JSON]) -> str:
    """What a call would do, in words for a question: its title in lower case, then its main argument.

    The main argument is the first of command, path, url, text, message, query, name or the only argument there
    is; long ones are cut.

    Args:
        title: the tool's title ("Run a shell command").
        args: its arguments.

    Returns:
        E.g. "run a shell command (rm -rf ~/tmp)".
    """
    words = (title[:1].lower() + title[1:]) if title else "do that"
    main = next((args[key] for key in ("command", "path", "url", "text", "message", "query", "name") if key in args),
                next(iter(args.values())) if len(args) == 1 else None)
    if main is None or main == "" or isinstance(main, dict | list):
        return words
    return f"{words} ({clip(str(main), ACTION_ARG_CHARS)})"


def detail_of(args: Mapping[str, JSON]) -> str:
    """A call's arguments as the HUD shows them under the question: one "name: value" line each."""
    return clip("\n".join(f"{key}: {value}" for key, value in args.items()), DETAIL_CHARS, flat=False)


class Confirmer:
    """Asks the user before risky actions (see the module docstring). Use it on the event loop."""

    def __init__(self, cfg: "Config", bus: EventBus) -> None:
        """Ask nobody yet; askers are registered by the parts that can ask (the voice, Discord).

        Args:
            cfg: the live settings (``safety.confirm_timeout_s``, the question's prompts).
            bus: the HUD's event bus.
        """
        self.cfg = cfg
        self.bus = bus
        self.askers: dict[str, Asker] = {}  # by the request source they serve: "voice", "discord"
        self._pending: dict[str, tuple[Question, asyncio.Future[bool]]] = {}
        self._by: dict[str, str] = {}  # question -> who answered it ("the HUD", "voice", "discord")

    def question(self, *, turn: str, tool: str, title: str, server: str, args: Mapping[str, JSON], reason: str,
                 source: str, source_kind: str, device: str = "") -> Question:
        """The question for a call, worded with ``prompts.confirm_question`` (or ``confirm_always``).

        Args:
            turn: the turn whose call waits.
            tool: the tool's model-visible name.
            title: its title.
            server: its server.
            args: its arguments.
            reason: "content", "always", "channel" or "outside_sandbox".
            source: what the request read ("" unless the reason is "content").
            source_kind: where the request came from.
            device: the request's device (a HUD, or the chat server it came through).

        Returns:
            The question, expiring ``safety.confirm_timeout_s`` from now.
        """
        user, prompts = self.cfg.user, self.cfg.prompts
        action = describe_action(title, args)
        names = {"honorific": user.honorific, "Honorific": user.honorific[:1].upper() + user.honorific[1:],
                 "action": action, "source": source}
        asked = {"content": prompts.confirm_question, "outside_sandbox": prompts.confirm_outside}
        text = asked.get(reason, prompts.confirm_always).format(**names)
        return Question(id=new_id(), turn=turn, tool=tool, title=title or tool, server=server, action=action,
                        detail=detail_of(args), reason=reason, source=source, text=text, source_kind=source_kind,
                        expires=time.time() + self.cfg.safety.confirm_timeout_s, device=device)

    async def ask(self, q: Question) -> Answer:
        """Ask the user and wait for the first answer (see the module docstring).

        Args:
            q: the question.

        Returns:
            "yes", "no", or "timeout" (treated as no).
        """
        loop = asyncio.get_running_loop()
        answered: asyncio.Future[bool] = loop.create_future()
        self._pending[q.id] = (q, answered)
        self.bus.publish("confirm", **q.as_event())
        log.info("asking before %s (%s): %s", q.tool, q.reason, q.action)
        channels = [loop.create_task(self._through(name, asker, q, answered), name=f"confirm {name}")
                    for name, asker in self.askers.items() if name == q.source_kind]
        answer: Answer
        try:
            async with asyncio.timeout(max(0.0, q.expires - time.time())):
                answer = "yes" if await asyncio.shield(answered) else "no"
        except TimeoutError:
            answer = "timeout"
        finally:
            self._pending.pop(q.id, None)
            for task in channels:
                task.cancel()
            for task in channels:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self.bus.publish("confirm_done", id=q.id, answer=answer)
        log.info("answer about %s: %s", q.tool, answer)
        return answer

    def answer(self, qid: str, yes: bool, *, by: str = "hud") -> bool:
        """The user answered a question (the HUD's ``confirm`` message, or a channel).

        Args:
            qid: the question's id.
            yes: their answer.
            by: who answered, for the log.

        Returns:
            False when no such question waits (answered already, or expired).
        """
        pending = self._pending.get(qid)
        if pending is None or pending[1].done():
            return False
        log.info("%s answered %s", by, "yes" if yes else "no")
        self._by[qid] = by
        pending[1].set_result(yes)
        return True

    def answered_by(self, qid: str) -> str:
        """Who answered a question that is settled ("the HUD", "voice", "discord"; "" when nobody did: it expired). Said
        once: the hub records it in the activity log."""
        return self._by.pop(qid, "")

    def pending(self) -> list[dict[str, JSON]]:
        """The questions waiting now, for a HUD that connects in the middle of one."""
        return [q.as_event() for q, _ in self._pending.values()]

    async def _through(self, name: str, asker: Asker, q: Question, answered: asyncio.Future[bool]) -> None:
        """Ask one other way; its clear answer settles the question (unless another came first)."""
        try:
            said = await asker(q, max(1.0, q.expires - time.time()))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("asking through %s failed", name, exc_info=True)
            return
        if said is not None and not answered.done():
            log.info("%s answered %s", name, "yes" if said else "no")
            self._by[q.id] = name
            answered.set_result(said)
