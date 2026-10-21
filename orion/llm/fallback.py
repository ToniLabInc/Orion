"""The brain with a stand-in: when the main model can't answer (a usage limit, signed out, not running), the
fallback brain the settings name answers instead, and the main one is tried again later.

``FallbackLLM`` wraps two LLMs and is itself one, so the brain's loop doesn't change:
- A round that fails before its first word, with an error whose kind ``brain.fallback_on`` lists, is answered by the
  fallback at once: the user hears the answer, then (once) why another brain gave it.
- A round that fails after its first word can't be taken over (half an answer has been said): the turn fails as
  before, and the next one starts on the fallback.
- While on the fallback, the main brain is tried again ``brain.fallback_retry_min`` minutes after the switch; a
  success ends the outage, another failure goes back to the fallback without a second announcement.
- When the fallback can't answer either, the main brain's error is raised with the fallback's reason after it, and
  the main brain stays the one asked first.

The fallback is built the first time it is needed, so a working main brain never costs a second connection.
"""

import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from orion.base import JSON, LLMError
from orion.llm import LLM, Piece, Prompt

if TYPE_CHECKING:
    from orion.config import Config

log = logging.getLogger("orion.llm")


@dataclass(frozen=True, slots=True)
class Switch:
    """The brain answering changed.

    Attributes:
        to: "fallback" (the stand-in answers now) or "primary" (the main brain answers again).
        primary: the main brain's label.
        fallback: the stand-in's label.
        reason: why the main brain couldn't answer ("" when it is back).
        announce: whether to say so out loud (the first switch of an outage, and the end of one).
    """

    to: Literal["fallback", "primary"]
    primary: str
    fallback: str
    reason: str
    announce: bool


def members(llm: LLM) -> list[LLM]:
    """The models an LLM holds: itself, or a ``FallbackLLM``'s main brain and its stand-in (once built).

    Args:
        llm: the brain's model.

    Returns:
        The concrete models, the main one first.
    """
    if isinstance(llm, FallbackLLM):
        return [llm.primary, *(() if llm.fallback is None else (llm.fallback,))]
    return [llm]


class FallbackLLM(LLM):
    """The main brain, and the stand-in it hands over to (see the module docstring)."""

    def __init__(self, cfg: "Config", primary: LLM, make_fallback: Callable[[], LLM], *, labels: tuple[str, str],
                 on_switch: Callable[[Switch], None] | None = None,
                 before_fallback: Callable[[], Awaitable[None]] | None = None) -> None:
        """Hold the main brain; the stand-in is built when first needed.

        Args:
            cfg: the live settings (``brain.fallback_on``, ``brain.fallback_retry_min``).
            primary: the main brain.
            make_fallback: builds the stand-in.
            labels: the two brains' names for people (main, stand-in).
            on_switch: told whenever the brain answering changes (the runtime shows and says it).
            before_fallback: run before the stand-in first answers (the runtime starts a local Ollama for it).
        """
        super().__init__(cfg)
        self.primary = primary
        self.fallback: LLM | None = None
        self.active: LLM = primary
        self.labels = labels
        self.on_switch = on_switch
        self._make_fallback = make_fallback
        self._before_fallback = before_fallback
        self._since = 0.0  # monotonic time of the last switch to the fallback
        self._outage = False  # the main brain failed and hasn't answered since: switches are said once
        self.reason = ""

    # ------------------------------------------------------------------------------------------------ the LLM API
    @property
    def all_tools(self) -> bool:
        """Whether the brain answering now is shown every tool."""
        return self.active.all_tools

    @property
    def model(self) -> str:
        """The model answering now."""
        return self.active.model

    def current(self) -> LLM:
        """The brain answering now: the main one, or the stand-in during an outage."""
        return self.active

    def sees(self) -> bool:
        """Whether the brain answering now can look at pictures."""
        return self.active.sees()

    @property
    def on_fallback(self) -> bool:
        """Whether the stand-in answers now."""
        return self.active is not self.primary

    def guard[T](self, items: AsyncIterator[T], *, whole: bool = False) -> AsyncIterator[T]:
        """No outer time limits: each brain's own stream is guarded inside ``stream``, so the main brain's silence
        can be handed over to the stand-in instead of failing the turn."""
        return items

    async def stream(self, prompt: Prompt) -> AsyncIterator[Piece]:
        """Answer one round with the brain answering now, handing a round the main brain fails before its first
        word over to the stand-in (see the module docstring)."""
        self._retry_primary_if_due()
        llm = self.active
        said = False
        try:
            async for piece in llm.guard(llm.stream(prompt)):
                said = True
                yield piece
        except LLMError as e:
            if said or llm is not self.primary or not self._hands_over(e):
                raise
            stand_in = await self._take_over(e)
            async for piece in stand_in.guard(stand_in.stream(prompt)):
                yield piece
            self.usage = stand_in.usage
            return
        self.usage = llm.usage
        if llm is self.primary:
            self._recovered()

    async def json(self, prompt: str, schema: Mapping[str, JSON], *, temperature: float) -> dict[str, JSON]:
        """Answer with one JSON object (the memory review): the brain answering now, or the stand-in when the main
        one fails with an error ``brain.fallback_on`` lists."""
        self._retry_primary_if_due()
        llm = self.active
        try:
            answer = await llm.json(prompt, schema, temperature=temperature)
        except LLMError as e:
            if llm is not self.primary or not self._hands_over(e):
                raise
            stand_in = await self._take_over(e)
            return await stand_in.json(prompt, schema, temperature=temperature)
        if llm is self.primary:
            self._recovered()
        return answer

    async def warm_up(self, prompt: Prompt) -> None:
        """Get the main brain ready; when it can't be (not installed, signed out), get the stand-in ready instead,
        so Orion answers from the start."""
        try:
            await self.primary.warm_up(prompt)
        except LLMError as e:
            if not self._hands_over(e):
                raise
            stand_in = await self._take_over(e)
            await stand_in.warm_up(prompt)
        else:
            if self.on_fallback:
                self._back_to_primary(announce=False)

    async def prepare(self, prompt: Prompt) -> None:
        """Get the brain answering now ready for a coming question."""
        await self.active.prepare(prompt)

    async def ready(self, prompt: Prompt) -> None:
        """Get the brain answering now ready for this question, and wait until it is."""
        await self.active.ready(prompt)

    def abort(self) -> None:
        """Stop the answer in progress, whichever brain gives it."""
        self.primary.abort()
        if self.fallback is not None:
            self.fallback.abort()

    async def close(self) -> None:
        """Close both brains."""
        await self.primary.close()
        if self.fallback is not None:
            await self.fallback.close()

    # ------------------------------------------------------------------------------------------------ switching
    def _hands_over(self, error: LLMError) -> bool:
        """Whether an error of the main brain is one the settings hand over to the stand-in."""
        return error.kind in self.cfg.brain.fallback_on

    async def _take_over(self, error: LLMError) -> LLM:
        """Switch to the stand-in for a failure of the main brain and return it, ready to answer.

        Raises:
            LLMError: the stand-in can't be built or started: the main brain's error, with the stand-in's reason.
        """
        try:
            if self.fallback is None:
                self.fallback = self._make_fallback()
            if self._before_fallback is not None:
                await self._before_fallback()
        except Exception as e:  # noqa: BLE001 (the stand-in can't help: the user hears the real problem)
            raise LLMError(f"{error} ({self.labels[1]} couldn't stand in: {e})", error.kind) from error
        announce = not self._outage
        self._outage = True
        self.active = self.fallback
        self._since = time.monotonic()
        self.reason = str(error)
        log.warning("%s can't answer (%s): %s stands in", self.labels[0], error.kind, self.labels[1])
        self._tell(Switch("fallback", *self.labels, reason=str(error), announce=announce))
        return self.fallback

    def _retry_primary_if_due(self) -> None:
        """While on the stand-in, ask the main brain first again once ``brain.fallback_retry_min`` has passed."""
        if self.on_fallback and time.monotonic() - self._since >= self.cfg.brain.fallback_retry_min * 60:
            log.info("trying %s again", self.labels[0])
            self.active = self.primary

    def _recovered(self) -> None:
        """The main brain answered: an outage is over."""
        if self._outage:
            self._outage = False
            self.reason = ""
            log.info("%s answers again", self.labels[0])
            self._tell(Switch("primary", *self.labels, reason="", announce=False))

    def _back_to_primary(self, *, announce: bool) -> None:
        """Ask the main brain first from now on."""
        self.active = self.primary
        self._outage = False
        self.reason = ""
        self._tell(Switch("primary", *self.labels, reason="", announce=announce))

    def _tell(self, switch: Switch) -> None:
        """Tell the runtime the brain answering changed; a listener that fails is logged, never raised."""
        if self.on_switch is None:
            return
        try:
            self.on_switch(switch)
        except Exception:
            log.exception("the brain switch couldn't be shown")
