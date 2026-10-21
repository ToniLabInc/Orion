"""What OpenRouter costs: each request's price recorded, a daily budget, and the key's own limits.

OpenRouter sends each answer's usage, its price in US dollars included, in the answer's last streamed event;
``OpenRouterLLM`` hands it here (``record``) with what the request was for: the conversation, a job, the memory
review or a benchmark. Before every request it asks ``check``: once today's spending reaches
``openrouter.daily_budget_usd`` (0: no budget), its requests fail with a "billing" error before they are sent, so the
fallback brain answers instead and the HUD says why. Free models (":free") cost nothing and always pass. The day is
this PC's: it starts at local midnight (OpenRouter's own daily figure for the key, shown beside it, counts UTC days).

Today's total lives in memory (summed from the database once, at start, and started again at midnight), so a check
costs nothing; each request's row is written on the store's thread. An answer cut off before its end (a time-out,
STOP) never sent its usage, so it isn't counted: OpenRouter's own figure for the key is exact (the HUD shows it
beside ours), and requests already under way when the budget runs out still finish, so a day can end a few cents
over.

``key_info`` reads the key's limit, what is left of it, today's usage and when its limit starts again (``GET /key``,
which costs nothing), at most once a minute.
"""

import bisect
import logging
import time
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Final

from orion.base import JSON, Expiring, LLMError, Publish, local_now, local_zone
from orion.llm import APP_HEADERS, CONNECT_TIMEOUT_S, KEY_CHECK_TIMEOUT_S, KeyInfo, Purpose, Usage, openrouter_key

if TYPE_CHECKING:
    import httpx

    from orion.config import Config
    from orion.memory.store import SpendRow, Store

log = logging.getLogger("orion.spend")

PROVIDER: Final = "openrouter"  # the one provider that charges per request
PURPOSES: Final[tuple[Purpose, ...]] = ("conversation", "job", "review", "bench")
FREE: Final = ":free"  # OpenRouter's free models end so: they cost nothing, budget or not
KEY_CACHE_S: Final = 60  # the key's limits are read again after this long
DECIMALS: Final = 6  # dollars are rounded to the micro-dollar when shown (no 0.30000000000000004)


def midnight(day: date) -> float:
    """When a day of this PC's begins.

    Args:
        day: the local date.

    Returns:
        Its local midnight, in epoch seconds (right across daylight-saving changes).
    """
    return datetime.combine(day, datetime.min.time(), tzinfo=local_zone()).timestamp()


class Spend:
    """What OpenRouter costs (see the module docstring): the ``Meter`` of its requests. Use it on the event loop."""

    def __init__(self, cfg: "Config", store: "Store", publish: Publish,
                 transport: "httpx.AsyncBaseTransport | None" = None) -> None:
        """Sum today's spending so far (one indexed read).

        Args:
            cfg: the live settings (``openrouter.daily_budget_usd``, ``openrouter.key``, ``openrouter.base_url``).
            store: the database.
            publish: ``EventBus.publish``: the HUD's ``spend`` event follows today's total.
            transport: an httpx transport for the key's limits (tests pass a mock).
        """
        self.cfg = cfg
        self.store = store
        self._publish = publish
        self._transport = transport
        self._http: httpx.AsyncClient | None = None
        self._key: Expiring[KeyInfo] = Expiring(KEY_CACHE_S)
        self._day = local_now().date()
        self._today = store.spend_total(midnight(self._day))

    def today(self) -> float:
        """What OpenRouter cost today (this PC's day), in US dollars."""
        day = local_now().date()
        if day != self._day:  # midnight passed: a new day
            self._day, self._today = day, 0.0
        return self._today

    def check(self, model: str) -> None:
        """Refuse a request before it is sent once today's budget is spent (a free model always passes).

        Args:
            model: the model asked.

        Raises:
            LLMError: kind "billing", saying how much was spent and when it starts again.
        """
        budget = self.cfg.openrouter.daily_budget_usd
        if budget <= 0 or model.endswith(FREE):
            return
        spent = self.today()
        if spent >= budget:
            raise LLMError(f"OpenRouter's daily budget is spent: ${spent:.2f} of ${budget:.2f} today. It starts again "
                           "at midnight, or raise it in Settings, Brain.", "billing")

    def record(self, *, model: str, purpose: Purpose, usage: Usage) -> None:
        """Count what one request cost: today's total at once, its row on the store's thread, and the HUD told.

        Args:
            model: the model asked.
            purpose: what the request was for.
            usage: its tokens and cost.
        """
        cost = max(0.0, usage.cost)
        self._today = self.today() + cost
        row: SpendRow = {"at": time.time(), "provider": PROVIDER, "model": model, "purpose": purpose,
                         "prompt_tokens": usage.input, "completion_tokens": usage.output, "cost": cost}
        self.store.submit(self.store.log_spend, row)
        self.publish()

    def summary(self) -> dict[str, JSON]:
        """Today's spending and the budget, as the HUD's ``init`` and ``spend`` event carry them.

        Returns:
            ``{today, budget}`` in US dollars (budget 0: none).
        """
        return {"today": round(self.today(), DECIMALS), "budget": self.cfg.openrouter.daily_budget_usd}

    def publish(self) -> None:
        """Tell the HUD today's spending and the budget (a ``spend`` event)."""
        self._publish("spend", **self.summary())

    def changed(self) -> None:
        """OpenRouter's settings changed: the key's limits are read afresh, and the HUD hears the budget."""
        self._key.invalidate()
        self.publish()

    async def report(self, days: int) -> dict[str, JSON]:
        """What OpenRouter cost over the last days.

        Args:
            days: how many days, today included (at least 1).

        Returns:
            ``{today, budget, currency: "USD", by_day: [{day, cost}], by_purpose: [{purpose, cost}]}``: every day,
            oldest first and "YYYY-MM-DD" in this PC's calendar; the purposes spent on in that time, in a fixed order.
        """
        today = local_now().date()
        firsts = [today - timedelta(days=back) for back in range(max(1, days) - 1, -1, -1)]
        starts = [midnight(day) for day in firsts]
        by_day = [0.0] * len(firsts)
        by_purpose: dict[str, float] = {}
        for at, purpose, cost in await self.store.call(self.store.spend_rows, starts[0]):  # sees the last request
            by_day[bisect.bisect_right(starts, at) - 1] += cost
            by_purpose[purpose] = by_purpose.get(purpose, 0.0) + cost
        return {**self.summary(), "currency": "USD",
                "by_day": [{"day": day.isoformat(), "cost": round(cost, DECIMALS)}
                           for day, cost in zip(firsts, by_day, strict=True)],
                "by_purpose": [{"purpose": purpose, "cost": round(by_purpose[purpose], DECIMALS)}
                               for purpose in PURPOSES if purpose in by_purpose]}

    async def key_info(self) -> KeyInfo | None:
        """The key's own limits, and today's usage as OpenRouter counts it (UTC), read at most once a minute.

        Returns:
            The limits; None when no key is set, or OpenRouter couldn't say (the reason is logged).
        """
        if not self.cfg.openrouter.key:
            return None
        try:
            return await self._key.get(self._read_key)
        except LLMError as e:
            log.info("the OpenRouter key's limits couldn't be read: %s", e)
            return None

    async def close(self) -> None:
        """Close the connection pool, if one was opened."""
        if self._http is not None:
            await self._http.aclose()

    async def _read_key(self) -> KeyInfo:
        """Ask OpenRouter for the key's limits (see ``key_info``)."""
        import httpx

        if self._http is None:
            self._http = httpx.AsyncClient(headers=APP_HEADERS, transport=self._transport,
                                           timeout=httpx.Timeout(KEY_CHECK_TIMEOUT_S, connect=CONNECT_TIMEOUT_S))
        openrouter = self.cfg.openrouter
        return await openrouter_key(self._http, f"{openrouter.base_url.rstrip('/')}/key", openrouter.key)
