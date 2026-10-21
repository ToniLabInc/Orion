"""Each turn's timings: how long hearing, thinking, the tools and speaking took, shown in the turn's trace and kept
for each brain's speeds in Settings.

A ``TurnClock`` gathers one turn's marks on the event loop, on the monotonic clock:
- said: the end of the request: the block of audio in which the voice detector last heard speech, or a typed
  message's arrival;
- heard: its words known (speech recognition done, or the end of the recording when a transcript taken earlier
  covers it; the arrival again for a typed request);
- out: the reply's first word (the model's first text, an instant command's reply, an apology);
- ready: the reply's first piece handed to the voice (a spoken reply);
- sound: that piece's first audio frame handed to this PC's speakers or sent to a browser;
- tools: each round of tool calls, from its first call's start to its last call's end (parallel calls once).

Once the turn is over, and its first sound has come or never will, its stages are worked out (``TurnClock.timing``).
The reply's first output is ``ready`` for a spoken reply, ``out`` for one that is only shown. Then
heard = heard - said; tools = the rounds that ended before the first output (the wait the first sound did not
overlap); think = first output - heard - tools; speak = sound - ready; total = said -> the first sound (-> the first
word for a reply that isn't spoken). So heard + think + tools + speak = total. Tools run while he is already
speaking (after "Let me check.") are in the trace's steps, not here. An interrupted or failed turn has no timings.

``stats`` gives each brain's median and 95th percentile of every stage over the latest turns, a stage over the turns
it took place in (hearing: spoken requests; tools: turns that waited for one; speaking: spoken replies).
"""

import math
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Final, TypedDict

STATS_TURNS: Final = 200  # each brain's speeds are worked out over this many latest turns
INSTANT: Final = "instant"  # the brain of a turn an instant command answered


class Timing(TypedDict):
    """One turn's timings, as the HUD and the database keep them (milliseconds).

    Keys:
        turn: the turn.
        at: when its request was said or typed (epoch seconds).
        source: where it came from ("voice", "hud", "discord").
        brain: the model that answered, as the HUD names it, or "instant".
        instant: whether an instant command answered it.
        heard_ms: from the end of speech to its words (0 for a typed request).
        think_ms: from its words to the reply's first output, the tools it waited for left out.
        tools_ms: the tools the reply's first output waited for.
        speak_ms: from the first piece handed to the voice to its first sound (0 when not spoken).
        total_ms: from the end of the request to the first sound, or to the first word when not spoken.
    """

    turn: str
    at: float
    source: str
    brain: str
    instant: bool
    heard_ms: int
    think_ms: int
    tools_ms: int
    speak_ms: int
    total_ms: int


class Stages(TypedDict):
    """A statistic of each stage (milliseconds)."""

    heard_ms: int
    think_ms: int
    tools_ms: int
    speak_ms: int
    total_ms: int


class BrainStats(TypedDict):
    """One brain's speeds: how many turns, and the median and 95th percentile of each stage."""

    brain: str
    turns: int
    p50: Stages
    p95: Stages


class Report(TypedDict):
    """What ``GET /api/timings`` answers: the latest turns, and each brain's speeds."""

    items: list[Timing]
    stats: list[BrainStats]


@dataclass(frozen=True, slots=True)
class Heard:
    """When a spoken request ended and when its words were known (monotonic seconds).

    Attributes:
        said: the end of the block of audio in which the voice detector last heard speech.
        heard: its words known.
    """

    said: float
    heard: float


class TurnClock:
    """One turn's marks (see the module docstring). Use it on the event loop; a mark after the first of its kind is
    ignored, and nothing counts once the timings were handed on."""

    __slots__ = ("_brain", "_done", "_end", "_finished", "_heard", "_instant", "_out", "_over", "_ready", "_said",
                 "_silent", "_sound", "_tools", "at", "source", "turn")

    def __init__(self, turn: str, source: str, *, heard: Heard | None, done: Callable[[Timing], None]) -> None:
        """Start a turn's clock.

        Args:
            turn: the turn.
            source: where its request came from.
            heard: when a spoken request ended and was heard; None for a typed one, which arrives now.
            done: receives the timings once they are known.
        """
        now = time.monotonic()
        self.turn = turn
        self.source = source
        self._said, self._heard = (heard.said, heard.heard) if heard is not None else (now, now)
        self.at = time.time() - (now - self._said)
        self._out = self._ready = self._sound = self._end = 0.0
        self._tools: list[tuple[float, float]] = []  # (when a round of calls ended, how long it took in seconds)
        self._brain = ""
        self._instant = self._over = self._silent = self._finished = False
        self._done = done

    def output(self) -> None:
        """The reply's first word is out."""
        self._out = self._out or time.monotonic()

    def ready(self) -> None:
        """The reply's first piece went to the voice."""
        self._ready = self._ready or time.monotonic()

    def sound(self, at: float) -> None:
        """The reply's first piece started playing.

        Args:
            at: when its first frame went to the device or the browser (monotonic seconds).
        """
        self._sound = self._sound or at
        self._settle()

    def tools(self, seconds: float) -> None:
        """A round of tool calls ended.

        Args:
            seconds: how long it took, from its first call's start to its last call's end.
        """
        self._tools.append((time.monotonic(), seconds))

    def end(self, brain: str, *, instant: bool = False) -> None:
        """The turn is over (its reply is all handed to the voice).

        Args:
            brain: the model that answered, as the HUD names it ("instant" for an instant command).
            instant: whether an instant command answered it.
        """
        self._brain, self._instant, self._over, self._end = brain, instant, True, time.monotonic()
        self._settle()

    def silence(self) -> None:
        """The reply's speech is over, or never started: no first sound is coming any more."""
        self._silent = True
        self._settle()

    def cancel(self) -> None:
        """The turn was interrupted or failed: it has no timings."""
        self._finished = True

    def timing(self) -> Timing:
        """The stages from the marks so far (see the module docstring)."""
        first = self._ready or self._out or self._end
        reached = self._sound or self._out or self._end
        waited = sum(seconds for ended, seconds in self._tools if not first or ended <= first)
        think = first - self._heard - waited if first else 0.0
        speak = self._sound - self._ready if self._sound and self._ready else 0.0
        total = reached - self._said if reached else 0.0
        return Timing(turn=self.turn, at=round(self.at, 3), source=self.source, brain=self._brain,
                      instant=self._instant, heard_ms=_ms(self._heard - self._said), think_ms=_ms(think),
                      tools_ms=_ms(waited), speak_ms=_ms(speak), total_ms=_ms(total))

    def _settle(self) -> None:
        """Hand the timings on once the turn is over and its first sound has come or never will."""
        if self._finished or not self._over or (self._ready and not self._sound and not self._silent):
            return
        self._finished = True
        self._done(self.timing())


def _ms(seconds: float) -> int:
    """Seconds as whole milliseconds (never negative)."""
    return max(0, round(seconds * 1000))


def percentile(values: Sequence[float], q: float) -> int:
    """A percentile of sorted values, interpolated between the two nearest (as numpy's default does).

    Args:
        values: the values, sorted.
        q: the percentile, 0 to 100.

    Returns:
        It, rounded to a whole number; 0 for no values.
    """
    if not values:
        return 0
    k = (len(values) - 1) * q / 100
    low = math.floor(k)
    high = min(low + 1, len(values) - 1)
    return round(values[low] + (values[high] - values[low]) * (k - low))


def stats(rows: Sequence[Timing]) -> list[BrainStats]:
    """Each brain's speeds over these turns (see the module docstring), the brain with the most turns first.

    Args:
        rows: the turns' timings (``STATS_TURNS`` of the latest).

    Returns:
        One entry per brain.
    """
    brains: dict[str, list[Timing]] = {}
    for row in rows:
        brains.setdefault(row["brain"], []).append(row)
    return [BrainStats(brain=brain, turns=len(turns), p50=_stages(turns, 50), p95=_stages(turns, 95))
            for brain, turns in sorted(brains.items(), key=lambda item: -len(item[1]))]


def _stages(turns: Sequence[Timing], q: float) -> Stages:
    """A percentile of each stage over the turns it took place in (hearing, tools and speaking are left out of the
    turns without any)."""

    def of(values: Iterable[int]) -> int:
        """The percentile of some values."""
        return percentile(sorted(values), q)

    return Stages(heard_ms=of(t["heard_ms"] for t in turns if t["heard_ms"] > 0),
                  think_ms=of(t["think_ms"] for t in turns),
                  tools_ms=of(t["tools_ms"] for t in turns if t["tools_ms"] > 0),
                  speak_ms=of(t["speak_ms"] for t in turns if t["speak_ms"] > 0),
                  total_ms=of(t["total_ms"] for t in turns))


def report(rows: Sequence[Timing], limit: int) -> Report:
    """The latest turns and each brain's speeds, for the HUD.

    Args:
        rows: the latest ``STATS_TURNS`` turns' timings, newest first.
        limit: how many of them to list (at least one); the speeds are over all of ``rows``.

    Returns:
        The report.
    """
    return Report(items=list(rows[:max(1, limit)]), stats=stats(rows))


def seconds(ms: int) -> str:
    """A time as said: "450 ms", "1.9 s"."""
    return f"{ms} ms" if ms < 1000 else f"{ms / 1000:.1f} s"


def in_words(brains: Sequence[BrainStats]) -> str:
    """Each brain's speeds in words (what the speed_report tool answers).

    Args:
        brains: ``stats`` of the latest turns.

    Returns:
        One line per brain, naming the median wait for the first sound and what it was spent on; or a sentence saying
        no turn has been timed.
    """
    if not brains:
        return "No turn has been timed yet."
    lines = []
    for brain in brains:
        p50, p95 = brain["p50"], brain["p95"]
        spent = ", ".join(f"{name} {seconds(ms)}" for name, ms in (
            ("hearing", p50["heard_ms"]), ("thinking", p50["think_ms"]), ("tools", p50["tools_ms"]),
            ("speaking", p50["speak_ms"])) if ms)
        lines.append(f"{brain['brain']}, {brain['turns']} turn{'s' * (brain['turns'] != 1)}: the first sound comes "
                     f"{seconds(p50['total_ms'])} after the request ends (half of them sooner), "
                     f"{seconds(p95['total_ms'])} or less for 19 in 20" + (f"; at the median, {spent}." if spent else "."))
    return "\n".join(lines)
