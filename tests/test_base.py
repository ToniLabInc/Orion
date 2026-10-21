"""base: the event bus hops to the loop thread, Expiring fetches once, TTLCache expires, views are memoised, and the
one transcript format hides untrusted turns."""

import asyncio
import threading
import time

from orion.base import EventBus, Expiring, Message, Tool, ToolSet, TTLCache, clip, format_transcript


def test_event_bus_hops_to_the_loop_thread_and_fans_out() -> None:
    """An event published from a worker thread reaches every sink on the loop's thread, as one shared dict."""
    bus, got = EventBus(), []
    for _ in range(2):
        bus.subscribe(lambda event: got.append((event, threading.get_ident())))

    async def main() -> int:
        """Bind the bus here, publish from a worker thread, and return the loop's thread."""
        bus.bind(asyncio.get_running_loop())
        await asyncio.to_thread(bus.publish, "state", state="idle", type="spoofed")
        return threading.get_ident()

    loop_thread = asyncio.run(main())
    (first, t1), (second, t2) = got
    assert first is second and first["type"] == "state" and first["state"] == "idle" and t1 == t2 == loop_thread


def test_expiring_shares_one_fetch_and_ttl_cache_expires() -> None:
    """Three callers at once share a single fetch; a cache entry is evicted when least used, and expires on time."""
    calls = 0

    async def fetch() -> int:
        """Count the fetches."""
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return calls

    async def main() -> list[int]:
        """Ask for the value three times at once."""
        value = Expiring[int](60)
        return await asyncio.gather(*(value.get(fetch) for _ in range(3)))

    assert asyncio.run(main()) == [1, 1, 1] and calls == 1
    now = [0.0]
    cache = TTLCache[str, int](2, clock=lambda: now[0])
    cache.put("a", 1, 10)
    cache.put("b", 2, 10)
    cache.get("a")  # "a" is now the most recently used...
    cache.put("c", 3, 20)  # ...so going over the limit drops "b"
    assert (cache.get("a"), cache.get("b"), cache.get("c")) == (1, None, 3)
    now[0] = 10.0
    assert (cache.get("a"), cache.get("c")) == (None, 3)  # each entry lasts the time it was stored for


def test_tool_views_are_memoised_and_digested() -> None:
    """A view keeps known names once, in order; the same selection is the same object; another order, another digest."""
    tools = ToolSet({n: Tool(n, f"{n} things", {"type": "object"}, "system", n) for n in ("open", "volume")}, version=1)
    view = tools.view(["volume", "nope", "volume", "open"])
    assert [tool.name for tool in view.tools] == ["volume", "open"]
    assert tools.view(["volume", "nope", "volume", "open"]) is view
    assert tools.view(["open", "volume"]).digest != view.digest


def test_transcript_format() -> None:
    """User words come from raw, tool calls show, system lines go, untrusted turns are hidden."""
    chat: list[Message] = [
        {"role": "user", "content": "hello", "ts": time.mktime((2026, 10, 4, 22, 10, 0, 0, 0, -1))},
        {"role": "user", "content": "[tail] set a timer", "raw": "set a timer"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "set_timer",
                                                              "arguments": {"duration": "5 min"}}]},
        {"role": "tool", "name": "set_timer", "content": "ok"},
        {"role": "assistant", "content": "Done."},
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "news?", "turn": "t2"},
        {"role": "assistant", "content": "The web says hi.", "turn": "t2"},
    ]
    assert format_transcript([]) == "" and format_transcript(chat[:1], times=True) == "[22:10] User: hello"
    assert format_transcript(chat, tools=False).splitlines() == [
        "User: hello", "User: set a timer", "You: Done.", "User: news?", "You: The web says hi."]
    assert format_transcript(chat, omit_turns={"t2"}).splitlines() == [
        "User: hello", "User: set a timer", '(you called set_timer {"duration": "5 min"})', "(set_timer answered: ok)",
        "You: Done.", "User: news?", "You: (answer drawn from web or app content omitted)"]


def test_clip_cuts_after_a_whole_word() -> None:
    assert clip("hello  world", 20) == "hello world"  # short enough: only put on one line
    assert clip("hello wonderful world", 12) == "hello…"  # "wonde" would be half a word
    assert clip("hello world again", 12) == "hello world…"  # the cut fell on a space: "world" is whole
    assert clip("https://example.com/a/long/path", 10) == "https://e…"  # one long word is cut, not dropped
    assert clip("line one\nline two", 12, flat=False) == "line one…"
    assert len(clip("x" * 50, 7)) == 7
