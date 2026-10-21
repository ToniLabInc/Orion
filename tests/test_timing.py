"""timing: a turn's marks become stages that add up to its total, once its first sound has come (or never will); each
brain's medians leave out the stages a turn doesn't have; the store keeps the latest turns."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from orion import timing
from orion.config import default_config
from orion.memory import store as store_module
from orion.memory.store import Store
from orion.timing import Heard, Timing, TurnClock, in_words, percentile, report, stats


class Clock:
    """A clock the test moves (the module reads time.monotonic and time.time)."""

    def __init__(self, start: float = 100.0) -> None:
        """Start at a moment of the monotonic clock; wall time runs 1000 s ahead."""
        self.now = start

    def at(self, seconds: float) -> None:
        """Move to this moment of the monotonic clock."""
        self.now = seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """The module's time, controlled."""
    fake = Clock()
    monkeypatch.setattr(timing, "time", SimpleNamespace(monotonic=lambda: fake.now, time=lambda: fake.now + 1000.0))
    return fake


def test_a_spoken_reply_after_a_tool_adds_up(clock: Clock) -> None:
    """Spoken at 100.0 (the last speech), heard at 100.7; the tool ended before the first piece; the sound came at
    101.6: hearing 700, tools 250, thinking 450, speaking 200 are the 1600 ms the user waited."""
    done: list[Timing] = []
    clock.at(100.75)
    turn = TurnClock("t2", "voice", heard=Heard(said=100.0, heard=100.7), done=done.append)
    clock.at(101.0)
    turn.tools(0.25)
    clock.at(101.3)
    turn.output()
    clock.at(101.4)
    turn.ready()
    clock.at(101.5)
    turn.end("Claude Sonnet")
    assert done == []  # it was handed to the voice, and its sound has not come yet
    turn.sound(101.6)
    [row] = done
    assert row == {"turn": "t2", "at": row["at"], "source": "voice", "brain": "Claude Sonnet", "instant": False,
                   "heard_ms": 700, "think_ms": 450, "tools_ms": 250, "speak_ms": 200, "total_ms": 1600}
    assert row["heard_ms"] + row["think_ms"] + row["tools_ms"] + row["speak_ms"] == row["total_ms"]
    assert row["at"] == 1100.0  # the request's own moment on the wall clock, not when the turn began


def test_a_typed_request_has_no_hearing_and_a_shown_reply_ends_at_its_first_word(clock: Clock) -> None:
    """Nothing is heard (the message arrives, 0 ms), and a reply only shown ends at its first word."""
    done: list[Timing] = []
    clock.at(200.0)
    turn = TurnClock("t3", "hud", heard=None, done=done.append)
    clock.at(200.4)
    turn.output()
    clock.at(201.0)
    turn.output()  # later words change nothing
    turn.end("llama")
    [row] = done
    assert (row["heard_ms"], row["think_ms"], row["tools_ms"], row["speak_ms"], row["total_ms"]) == (0, 400, 0, 0, 400)


def test_tools_run_while_he_already_speaks_are_not_a_wait(clock: Clock) -> None:
    """"Let me check." is ready at 100.5 and sounds at 100.7; the tool that runs after it (100.8 to 102.0) is in
    the trace's steps, not in the wait for the first sound."""
    done: list[Timing] = []
    clock.at(100.0)
    turn = TurnClock("t4", "hud", heard=None, done=done.append)
    clock.at(100.4)
    turn.output()
    clock.at(100.5)
    turn.ready()
    turn.sound(100.7)
    clock.at(102.0)
    turn.tools(1.2)
    clock.at(102.5)
    turn.end("Claude Haiku")
    [row] = done
    assert (row["think_ms"], row["tools_ms"], row["speak_ms"], row["total_ms"]) == (500, 0, 200, 700)


def test_an_instant_command_is_its_matching_its_calls_and_its_voice(clock: Clock) -> None:
    """Heard at 50.0, matched and planned by 50.003, the call took 12 ms, the reply is ready and sounds 180 ms later:
    thinking 3 ms, tools 12, speaking 180 (the first two overlap nothing)."""
    done: list[Timing] = []
    clock.at(50.0)
    turn = TurnClock("t5", "voice", heard=Heard(said=49.5, heard=50.0), done=done.append)
    clock.at(50.015)
    turn.tools(0.012)
    turn.output()
    turn.ready()
    turn.end(timing.INSTANT, instant=True)
    turn.sound(50.195)
    [row] = done
    assert row["instant"] is True and row["brain"] == "instant"
    assert (row["heard_ms"], row["think_ms"], row["tools_ms"], row["speak_ms"], row["total_ms"]) == (
        500, 3, 12, 180, 695)


def test_a_reply_that_never_sounds_ends_when_its_speech_is_over(clock: Clock) -> None:
    """A spoken reply the speaker dropped (stopped, no sound server) has timings once the speech is over: no speaking
    time, and the total runs to the first word."""
    done: list[Timing] = []
    clock.at(10.0)
    turn = TurnClock("t6", "hud", heard=None, done=done.append)
    clock.at(10.3)
    turn.output()
    turn.ready()
    clock.at(10.6)
    turn.end("llama")
    assert done == []
    turn.silence()
    turn.silence()
    [row] = done
    assert (row["speak_ms"], row["total_ms"]) == (0, 300)


def test_an_interrupted_turn_has_no_timings(clock: Clock) -> None:
    """Cancelled (STOP, a failure): nothing is handed on, whatever marks come after."""
    done: list[Timing] = []
    clock.at(10.0)
    turn = TurnClock("t7", "hud", heard=None, done=done.append)
    turn.output()
    turn.cancel()
    turn.end("llama")
    turn.sound(10.5)
    assert done == []


@pytest.mark.parametrize(("values", "q", "expected"), [
    ([], 50, 0), ([7], 95, 7), ([1, 2, 3, 4], 50, 2), ([10, 20, 30, 40, 50], 50, 30),
    ([0, 100], 95, 95), (list(range(1, 101)), 95, 95), (list(range(1, 101)), 50, 50),
])
def test_percentile_interpolates_between_the_two_nearest(values: list[int], q: float, expected: int) -> None:
    """The same answers as numpy's default."""
    assert percentile(values, q) == expected


def row(brain: str, **stages: int) -> Timing:
    """A turn's timings of this brain."""
    base = {"turn": "x", "at": 0.0, "source": "hud", "brain": brain, "instant": brain == "instant", "heard_ms": 0,
            "think_ms": 0, "tools_ms": 0, "speak_ms": 0, "total_ms": 0}
    return Timing(**(base | stages))  # type: ignore[typeddict-item]


def test_each_brains_speeds_leave_out_the_stages_a_turn_lacks() -> None:
    """Typed turns have no hearing, turns without a tool no tools time and unspoken ones no speaking: those zeros are
    not data, so they don't pull the median down; the brain with the most turns comes first."""
    rows = [row("Claude Sonnet", heard_ms=900, think_ms=1000, speak_ms=200, total_ms=2100),
            row("Claude Sonnet", heard_ms=700, think_ms=1400, tools_ms=300, speak_ms=250, total_ms=2650),
            row("Claude Sonnet", think_ms=800, total_ms=800),
            row("instant", heard_ms=500, think_ms=2, tools_ms=10, speak_ms=150, total_ms=662)]
    [sonnet, instant] = stats(rows)
    assert sonnet == {"brain": "Claude Sonnet", "turns": 3,
                      "p50": {"heard_ms": 800, "think_ms": 1000, "tools_ms": 300, "speak_ms": 225, "total_ms": 2100},
                      "p95": {"heard_ms": 890, "think_ms": 1360, "tools_ms": 300, "speak_ms": 248, "total_ms": 2595}}
    assert instant["turns"] == 1 and instant["p50"] == instant["p95"] == {
        "heard_ms": 500, "think_ms": 2, "tools_ms": 10, "speak_ms": 150, "total_ms": 662}
    assert stats([]) == []


def test_the_report_lists_a_few_turns_and_counts_them_all() -> None:
    """The list is cut to the limit (at least one turn); the speeds are over every turn given."""
    rows = [row("llama", total_ms=100 * (i + 1)) for i in range(4)]
    assert [r["total_ms"] for r in report(rows, 2)["items"]] == [100, 200]
    assert len(report(rows, 0)["items"]) == 1 and len(report(rows, 99)["items"]) == 4
    assert report(rows, 1)["stats"][0]["turns"] == 4 and report([], 50) == {"items": [], "stats": []}


def test_the_store_keeps_the_latest_turns_and_a_turn_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A turn's second write replaces its first; past the limit the oldest go; they read back newest first."""
    monkeypatch.setattr(store_module, "TIMINGS_KEPT", 5)
    db = Store(tmp_path / "orion.db", default_config().memory, role="tools")
    db.ensure_schema()
    for i in range(8):
        db.log_timing(row("llama", turn=f"t{i}", at=float(i), total_ms=i * 10))
    db.log_timing(row("llama", turn="t7", at=7.0, total_ms=999, instant=True))
    found = db.timings(50)
    assert [r["turn"] for r in found] == ["t7", "t6", "t5", "t4", "t3"]
    assert found[0]["total_ms"] == 999 and found[0]["instant"] is True and found[1]["instant"] is False
    assert [r["turn"] for r in db.timings(2)] == ["t7", "t6"]
    db.close()


def test_the_speeds_are_said_in_words() -> None:
    """The median wait and the slowest twentieth for each brain, and what the median went on; a stage a brain never
    spends time on is left out; nothing timed yet is said so."""
    rows = [row("Claude Sonnet", heard_ms=900, think_ms=1000, speak_ms=200, total_ms=2100),
            row("Claude Sonnet", heard_ms=700, think_ms=1400, tools_ms=300, speak_ms=250, total_ms=2650),
            row("instant", heard_ms=500, think_ms=2, tools_ms=10, speak_ms=150, total_ms=662)]
    assert in_words(stats(rows)) == (
        "Claude Sonnet, 2 turns: the first sound comes 2.4 s after the request ends (half of them sooner), 2.6 s or "
        "less for 19 in 20; at the median, hearing 800 ms, thinking 1.2 s, tools 300 ms, speaking 225 ms.\n"
        "instant, 1 turn: the first sound comes 662 ms after the request ends (half of them sooner), 662 ms or less "
        "for 19 in 20; at the median, hearing 500 ms, thinking 2 ms, tools 10 ms, speaking 150 ms.")
    assert in_words([]) == "No turn has been timed yet."


def test_he_can_say_how_fast_he_is(tmp_path: Path) -> None:
    """The speed_report tool answers from the latest turns in the database."""
    from mcp import Client

    from orion.tools.core import Controls, server

    db = Store(tmp_path / "orion.db", default_config().memory, role="tools")
    db.ensure_schema()
    rt = SimpleNamespace(cfg=default_config(), memory=SimpleNamespace(store=db))

    async def ask() -> str:
        """Ask twice: with nothing timed, and with a turn."""
        async with Client(server(Controls(rt))) as client:  # type: ignore[arg-type]
            first = await client.call_tool("speed_report", {})
            db.log_timing(row("Claude Haiku", turn="t1", total_ms=1500, think_ms=1000, speak_ms=200, heard_ms=300))
            second = await client.call_tool("speed_report", {})
        assert not first.is_error and not second.is_error
        return first.content[0].text + "|" + second.content[0].text  # type: ignore[union-attr]

    none, one = asyncio.run(ask()).split("|")
    assert none == "No turn has been timed yet."
    assert one.startswith("Claude Haiku, 1 turn: the first sound comes 1.5 s after") and "thinking 1.0 s" in one
    db.close()
