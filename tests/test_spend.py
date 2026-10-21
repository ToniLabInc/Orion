"""spend: what OpenRouter costs. Each answer's price counted under what it was for, a daily budget that refuses
requests before they are sent (free models always pass), a day that is this PC's, the report by day and purpose, the
key's own limits, and the tables that hold it all. A fake OpenRouter over a mock transport: nothing is ever paid."""

import asyncio
import json
import logging
from collections.abc import Iterator
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest

from orion.base import LLMError, ToolSet
from orion.config import default_config
from orion.llm import NO_KEY, KeyInfo, OpenRouterLLM, Prompt, Usage, build_llm, openrouter_key, openrouter_usage
from orion.memory.store import CURRENT_VERSION, SCHEMA, V5_SCHEMA, SpendRow, Store
from orion.spend import FREE, Spend, midnight

MEMORY = SimpleNamespace(db_timeout_s=5)
ATHENS = ZoneInfo("Europe/Athens")
PROMPT = Prompt("Be brief.", "", 1, "t1", (), ({"role": "user", "content": "hi", "turn": "t1"},),
                ToolSet({}, 0).view(()))


@pytest.fixture
def store(tmp_path) -> Iterator[Store]:
    db = Store(tmp_path / "spend.db", MEMORY, role="main")
    db.ensure_schema()
    yield db
    db.close()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[datetime]:
    """This PC's clock, in Athens, set by the test (a list of one moment)."""
    now = [datetime(2026, 10, 11, 12, 0, tzinfo=ATHENS)]
    monkeypatch.setattr("orion.spend.local_zone", lambda: ATHENS)
    monkeypatch.setattr("orion.spend.local_now", lambda: now[0])
    return now


def flush(store: Store) -> None:
    """Until the store's thread has done everything queued so far."""
    store.submit(lambda: None).result(timeout=5)


def spend_in(store: Store, budget: float = 0.0, *, key: str = "", transport: httpx.MockTransport | None = None,
             ) -> tuple[Spend, list[tuple[str, dict]]]:
    """A Spend over this store with this budget (and key), and the events it publishes."""
    cfg = default_config()
    cfg.openrouter.daily_budget_usd = budget
    cfg.openrouter.api_key = key
    events: list[tuple[str, dict]] = []
    return Spend(cfg, store, lambda kind, **data: events.append((kind, data)), transport=transport), events


def row(at: float, purpose: str, cost: float, model: str = "m") -> SpendRow:
    return {"at": at, "provider": "openrouter", "model": model, "purpose": purpose, "prompt_tokens": 10,
            "completion_tokens": 5, "cost": cost}


def sse(*events: dict, done: bool = True) -> httpx.Response:
    """OpenRouter's streamed answer."""
    lines = [f"data: {json.dumps(event)}" for event in events]
    return httpx.Response(200, text="\n\n".join([*lines, "data: [DONE]"] if done else lines))


ANSWER = [{"choices": [{"delta": {"content": "Hi"}}]},
          {"choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 3, "cost": 0.0042,
                                    "prompt_tokens_details": {"cached_tokens": 40}}}]


def test_openrouter_reports_its_usage_and_its_price_in_dollars() -> None:
    full = {"prompt_tokens": 194, "completion_tokens": 2, "total_tokens": 196, "cost": 0.95,
            "cost_details": {"upstream_inference_cost": 19}, "completion_tokens_details": {"reasoning_tokens": 0},
            "prompt_tokens_details": {"cached_tokens": 10, "cache_write_tokens": 100, "audio_tokens": 0}}
    assert openrouter_usage(full) == Usage(input=194, cached=10, output=2, cost=0.95)
    assert openrouter_usage({}) == Usage()
    assert openrouter_usage({"prompt_tokens": 5, "prompt_tokens_details": None}) == Usage(input=5)
    for odd in (None, -1, "0.01", float("nan"), True):  # a free model, a bring-your-own key, nonsense: nothing owed
        assert openrouter_usage({"cost": odd}).cost == 0.0


def test_a_budget_refuses_before_the_request_and_free_models_always_pass(store: Store) -> None:
    spend, events = spend_in(store, budget=1.0)
    spend.check("anthropic/claude-haiku")
    spend.record(model="anthropic/claude-haiku", purpose="conversation", usage=Usage(cost=0.6))
    spend.check("anthropic/claude-haiku")  # 60 cents of a dollar
    assert spend.summary() == {"today": 0.6, "budget": 1.0}
    spend.record(model="anthropic/claude-haiku", purpose="job", usage=Usage(cost=0.4))
    with pytest.raises(LLMError, match=r"daily budget is spent: \$1\.00 of \$1\.00 today.*midnight.*Settings") as e:
        spend.check("anthropic/claude-haiku")
    assert e.value.kind == "billing"
    spend.check(f"google/gemma-4-31b-it{FREE}")  # free models cost nothing, budget or not
    assert [data for kind, data in events if kind == "spend"][-1] == {"today": 1.0, "budget": 1.0}
    assert len(events) == 2  # one event per request counted
    spend.cfg.openrouter.daily_budget_usd = 0  # no budget: never refused
    spend.check("anthropic/claude-haiku")
    spend.cfg.openrouter.daily_budget_usd = 5
    spend.check("anthropic/claude-haiku")  # a raised budget applies at once


def test_a_price_is_never_negative_and_floats_stay_tidy(store: Store) -> None:
    spend, _ = spend_in(store)
    for cost in (0.1, 0.2, -3.0):
        spend.record(model="m", purpose="bench", usage=Usage(cost=cost))
    assert spend.summary()["today"] == 0.3  # not 0.30000000000000004, and the refund-looking -3 counts as 0


def test_every_request_is_a_row_with_its_purpose_and_the_day_survives_a_restart(store: Store) -> None:
    spend, _ = spend_in(store)
    spend.record(model="anthropic/claude-haiku", purpose="job", usage=Usage(input=10, output=5, cost=0.5))
    spend.record(model="anthropic/claude-haiku", purpose="review", usage=Usage(cost=0.25))
    flush(store)
    with store.reading() as db:
        found = [tuple(r) for r in db.execute("SELECT provider, model, purpose, prompt_tokens, completion_tokens, "
                                              "cost FROM spend ORDER BY id")]
    assert found == [("openrouter", "anthropic/claude-haiku", "job", 10, 5, 0.5),
                     ("openrouter", "anthropic/claude-haiku", "review", 0, 0, 0.25)]
    again, _ = spend_in(store)  # Orion restarted: today's total is summed from the rows
    assert again.today() == 0.75


def test_a_new_day_starts_the_total_again_at_local_midnight(store: Store, clock: list[datetime]) -> None:
    clock[0] = datetime(2030, 10, 11, 23, 59, 30, tzinfo=ATHENS)  # a day in the future: the rows ``record`` writes
    store.log_spend(row(clock[0].timestamp() - 10, "conversation", 0.9))  # under the real clock come before it
    store.log_spend(row(clock[0].timestamp() - 86400, "conversation", 7.0))  # yesterday's
    spend, _ = spend_in(store, budget=1.0)
    assert spend.today() == 0.9
    spend.record(model="m", purpose="conversation", usage=Usage(cost=0.2))
    with pytest.raises(LLMError, match="budget is spent"):
        spend.check("m")
    clock[0] = datetime(2030, 10, 12, 0, 0, 1, tzinfo=ATHENS)  # two seconds later, a new day
    assert spend.today() == 0.0
    spend.check("m")
    spend.record(model="m", purpose="conversation", usage=Usage(cost=0.3))
    assert spend.today() == 0.3
    flush(store)
    restarted, _ = spend_in(store, budget=1.0)
    assert restarted.today() == 0.0  # the rows were written under the real clock; none after this fake midnight


def test_days_are_this_pcs_even_when_the_clocks_change(clock: list[datetime]) -> None:
    assert midnight(date(2026, 10, 12)) - midnight(date(2026, 10, 11)) == 24 * 3600
    assert midnight(date(2026, 3, 30)) - midnight(date(2026, 3, 29)) == 23 * 3600  # spring forward in Athens
    assert midnight(date(2026, 10, 26)) - midnight(date(2026, 10, 25)) == 25 * 3600  # and back


def test_the_report_sums_days_and_purposes_in_this_pcs_calendar(store: Store, clock: list[datetime]) -> None:
    def at(day: int, hour: int, minute: int = 0, second: int = 0) -> float:
        return datetime(2026, 10, day, hour, minute, second, tzinfo=ATHENS).timestamp()

    for found in (row(at(8, 12), "conversation", 5.0),  # before the three days asked about
                  row(at(9, 10), "conversation", 0.25), row(at(10, 23, 59, 59), "review", 0.01),
                  row(at(11, 0), "job", 0.5), row(at(11, 11), "bench", 1.0), row(at(11, 11, 30), "job", 0.25)):
        store.log_spend(found)
    spend, _ = spend_in(store, budget=2.0)
    report = asyncio.run(spend.report(3))
    assert report == {"today": 1.75, "budget": 2.0, "currency": "USD",
                      "by_day": [{"day": "2026-10-09", "cost": 0.25}, {"day": "2026-10-10", "cost": 0.01},
                                 {"day": "2026-10-11", "cost": 1.75}],
                      "by_purpose": [{"purpose": "conversation", "cost": 0.25}, {"purpose": "job", "cost": 0.75},
                                     {"purpose": "review", "cost": 0.01}, {"purpose": "bench", "cost": 1.0}]}
    quiet = asyncio.run(spend.report(1))  # one day: today alone
    assert [d["day"] for d in quiet["by_day"]] == ["2026-10-11"] and len(asyncio.run(spend.report(0))["by_day"]) == 1
    assert asyncio.run(spend_in(store)[0].report(400))["by_day"][0]["day"] == "2025-09-07"  # a year and a bit back


def key_response(request: httpx.Request) -> httpx.Response:
    assert (request.method, request.url.path, request.headers["authorization"]) == ("GET", "/api/v1/key", "Bearer sk-t")
    return httpx.Response(200, json={"data": {"label": "sk-or-v1-abc...xyz", "limit": 5, "limit_remaining": 3.25,
                                              "limit_reset": "monthly", "usage": 1.75, "usage_daily": 0.5,
                                              "usage_weekly": 1.0, "usage_monthly": 1.75, "is_free_tier": False}})


def test_the_keys_own_limits_are_read_once_a_minute_and_never_raise(store: Store,
                                                                    caplog: pytest.LogCaptureFixture) -> None:
    requests: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return key_response(request)

    spend, _ = spend_in(store, key="sk-t", transport=httpx.MockTransport(answer))

    async def run() -> list[KeyInfo | None]:
        first, second = await spend.key_info(), await spend.key_info()
        spend.changed()  # the key or the budget changed: read afresh
        third = await spend.key_info()
        await spend.close()
        return [first, second, third]

    first, second, third = asyncio.run(run())
    assert first == KeyInfo(limit=5.0, limit_remaining=3.25, usage_daily=0.5, limit_reset="monthly") == second
    assert first.as_dict() == {"limit": 5.0, "limit_remaining": 3.25, "usage_daily": 0.5, "limit_reset": "monthly"}
    assert third == first and len(requests) == 2
    # a key with no limit says so with nulls; no key asks nothing; a refusal or garbage is None and logged
    free = spend_in(store, key="sk-t", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": {
        "limit": None, "limit_remaining": None, "limit_reset": None, "usage_daily": 0.0}})))[0]
    assert asyncio.run(free.key_info()) == KeyInfo(None, None, 0.0, None)
    nothing = spend_in(store, transport=httpx.MockTransport(lambda r: pytest.fail("no key, no request")))[0]
    assert asyncio.run(nothing.key_info()) is None
    with caplog.at_level(logging.INFO, logger="orion.spend"):
        for response in (httpx.Response(401, json={"error": {"message": "No auth credentials found"}}),
                         httpx.Response(200, text="<html>maintenance</html>"), httpx.Response(200, json={"oops": 1})):
            broken = spend_in(store, key="sk-t", transport=httpx.MockTransport(lambda r, found=response: found))[0]
            assert asyncio.run(broken.key_info()) is None
    assert "limits couldn't be read" in caplog.text and "rejected the API key" in caplog.text


def test_the_key_check_says_what_is_wrong() -> None:
    def check(handler: object, key: str = "sk-t") -> KeyInfo:
        async def run() -> KeyInfo:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:  # type: ignore[arg-type]
                return await openrouter_key(client, "https://openrouter.ai/api/v1/key", key)

        return asyncio.run(run())

    with pytest.raises(LLMError) as e:
        check(key_response, key="")
    assert (str(e.value), e.value.kind) == (NO_KEY, "auth")
    with pytest.raises(LLMError, match="rejected the API key") as e:
        check(lambda r: httpx.Response(401, json={"error": {"message": "Invalid key"}}))
    assert e.value.kind == "auth"

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    with pytest.raises(LLMError, match="didn't answer: no route") as e:
        check(down)
    assert e.value.kind == "unavailable"
    with pytest.raises(LLMError, match="couldn't be read"):
        check(lambda r: httpx.Response(200, text="not json"))


def test_openrouter_requests_are_checked_counted_and_labelled(store: Store) -> None:
    """Each answer's price is counted under what it was for; the memory review's JSON answers count as the review's;
    an answer cut off before its end, or one that never sent its usage, is not counted."""
    spend, events = spend_in(store, key="sk-t")
    cfg = spend.cfg
    plain = [{"choices": [{"delta": {"content": "Hi"}}]}]  # no usage event
    review = [{"choices": [{"delta": {"content": '{"ops": [], "journal": "", "concepts": []}'}}]}, ANSWER[1]]
    answers = iter([sse(*ANSWER), sse(*ANSWER), sse(*plain), sse(*ANSWER), sse(*review)])
    sent: list[dict] = []

    def reply(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return next(answers)

    async def run() -> list[object]:
        llm = OpenRouterLLM(cfg, meter=spend, purpose="job", transport=httpx.MockTransport(reply))
        bench = OpenRouterLLM(cfg, meter=spend, purpose="bench", transport=httpx.MockTransport(reply))
        out: list[object] = [[piece async for piece in llm.stream(PROMPT)], llm.usage]
        out.append([piece async for piece in bench.stream(PROMPT)])
        out.append([piece async for piece in llm.stream(PROMPT)])  # no usage in it
        out.append(llm.usage)
        agen = llm.stream(PROMPT)
        out.append(await anext(agen))
        await agen.aclose()  # cut off after its first word: STOP, or a time-out
        out.append(await llm.json("Review this.", {"type": "object"}, temperature=0))
        await llm.close(), await bench.close()
        return out

    out = asyncio.run(run())
    assert out[0] == ["Hi"] and out[1] == Usage(input=50, cached=40, output=3, cost=0.0042) and out[4] is None
    assert out[5] == "Hi" and out[6] == {"ops": [], "journal": "", "concepts": []}
    flush(store)
    with store.reading() as db:
        counted = [tuple(r) for r in db.execute("SELECT purpose, model, prompt_tokens, completion_tokens, cost "
                                                "FROM spend ORDER BY id")]
    model = cfg.openrouter.model
    assert counted == [("job", model, 50, 3, 0.0042), ("bench", model, 50, 3, 0.0042),
                       ("review", model, 50, 3, 0.0042)]
    assert spend.today() == pytest.approx(0.0126) and [d["today"] for k, d in events] == [0.0042, 0.0084, 0.0126]
    assert len(sent) == 5 and all("usage" not in body for body in sent)


def test_a_request_over_the_budget_is_never_sent(store: Store) -> None:
    spend, _ = spend_in(store, budget=0.01, key="sk-t")
    spend.cfg.openrouter.model = "anthropic/claude-haiku"  # the default model is a free one, which always passes
    sent: list[httpx.Request] = []

    def reply(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return sse(*ANSWER)

    async def run() -> LLMError:
        llm = OpenRouterLLM(spend.cfg, meter=spend, purpose="conversation", transport=httpx.MockTransport(reply))
        for _ in range(3):  # 0.0042 each: 0.0126 passes the cent the budget allows
            assert [piece async for piece in llm.stream(PROMPT)] == ["Hi"]
        with pytest.raises(LLMError, match="budget is spent") as e:
            [piece async for piece in llm.stream(PROMPT)]
        with pytest.raises(LLMError, match="budget is spent"):  # the memory review is refused too
            await llm.json("Review this.", {"type": "object"}, temperature=0)
        await llm.close()
        return e.value

    error = asyncio.run(run())
    assert error.kind == "billing" and len(sent) == 3


def test_warm_up_checks_the_key_without_paying(monkeypatch: pytest.MonkeyPatch, store: Store) -> None:
    spend, _ = spend_in(store, key="sk-t")
    seen: list[tuple[str, str]] = []

    def reply(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return key_response(request)

    async def models(url: str) -> list[dict]:
        return [{"id": spend.cfg.openrouter.model, "vision": True}]

    monkeypatch.setattr("orion.llm.openrouter_models", models)

    async def run() -> bool:
        llm = OpenRouterLLM(spend.cfg, meter=spend, purpose="conversation", transport=httpx.MockTransport(reply))
        await llm.warm_up(PROMPT)
        await llm.close()
        return llm.sees()

    assert asyncio.run(run()) and seen == [("GET", "/api/v1/key")] and spend.today() == 0.0

    async def odd() -> None:  # a key that works is a key that works, whatever shape its limits come in
        llm = OpenRouterLLM(spend.cfg, meter=spend, purpose="conversation",
                            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"unexpected": True})))
        await llm.warm_up(PROMPT)
        await llm.close()

    asyncio.run(odd())
    spend.cfg.openrouter.api_key = ""
    keyless = OpenRouterLLM(spend.cfg, meter=spend, purpose="conversation")
    with pytest.raises(LLMError, match="No OpenRouter API key") as e:
        asyncio.run(keyless.warm_up(PROMPT))
    assert e.value.kind == "auth"


def test_the_provider_that_charges_gets_the_meter_and_the_others_dont(meter) -> None:
    cfg = default_config()
    paid = build_llm(cfg, "openrouter", meter=meter, purpose="bench")
    assert isinstance(paid, OpenRouterLLM) and paid.purpose == "bench" and paid.meter is meter
    assert not hasattr(build_llm(cfg, "ollama", meter=meter, purpose="bench"), "meter")


def test_the_tables_exist_in_a_new_database_and_in_one_upgraded_from_version_4(tmp_path) -> None:
    import sqlite3

    fresh = Store(tmp_path / "new.db", MEMORY, role="main")
    fresh.ensure_schema()
    path = tmp_path / "v4.db"
    with sqlite3.connect(path) as db:
        for statement in SCHEMA:
            if statement not in V5_SCHEMA:
                db.execute(statement)
        db.execute("PRAGMA user_version = 4")
    old = Store(path, MEMORY, role="main")
    old.ensure_schema()
    for db_store in (fresh, old):
        assert db_store.log_spend(row(1.0, "job", 0.5)) == 1 and db_store.spend_total(0.0) == 0.5
        assert db_store.add_bench_run(at=1.0, suite="builtin", brains=["claude"], results={}) == 1
        with db_store.reading() as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == CURRENT_VERSION
        db_store.close()
