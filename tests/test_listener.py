"""voice/listener: the pure decisions about standby speech and recordings (no audio libraries), and when a recording's
last speech was."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from orion.config import default_config
from orion.voice import MIC_FRAME
from orion.voice import listener as listener_module
from orion.voice.listener import CHUNK_MS, Listener, Recorder, Recording, RecordParams, Segmenter, SegmentParams

SEG = SegmentParams(start_chunks=2, pause_ms=250, every_s=3.0, min_speech_s=0.25, end_ms=700, long_talk_s=10,
                    speech=0.5, silence=0.35)
REC = RecordParams(min_ms=450, neutral_ms=700, max_ms=1000, max_utterance_s=20, wait_s=6, speech=0.5, silence=0.35,
                   min_speech_ms=96)


def chunks(ms: float) -> int:
    """How many 32 ms speech-probability chunks cover `ms`."""
    return round(ms / CHUNK_MS)


def test_standby_speech_is_checked_at_a_breath_then_ends() -> None:
    """Half a second of speech and a breath give one check; the pause then ends the stretch, with no second check."""
    seg = Segmenter(SEG)
    assert seg.feed([1.0] * chunks(500)) + seg.feed([0.0] * chunks(260)) == ["start", "check"]
    assert seg.feed([0.0] * chunks(450)) == ["end"] and not seg.active


def test_long_talk_is_checked_only_at_pauses() -> None:
    """Unbroken talk is checked every 3 s until it has gone on for 10 s (a TV); then only a pause brings a check."""
    seg = Segmenter(SEG)
    assert seg.feed([1.0] * chunks(14_000)).count("check") == 3 and seg.long_talk
    assert seg.feed([0.0] * chunks(260)) == ["check"]


def test_a_recording_ends_sooner_when_it_sounds_finished() -> None:
    """The pause that ends a request is short after a finished sentence, longer when unsure, longest after "and"."""

    def silence_until_end(complete: bool | None) -> float:
        """Speak for 600 ms, then stay silent; give the hint at the first pause; return the silence at the end."""
        rec = Recorder(REC)
        assert rec.feed([1.0] * chunks(600)) == []
        for n in range(1, 100):
            actions = rec.feed([0.0])
            if "pause" in actions:
                rec.hint(complete)
            if "end" in actions:
                return n * CHUNK_MS
        raise AssertionError("the recording never ended")

    assert silence_until_end(True) < 550 < silence_until_end(None) < 750 < silence_until_end(False) < 1100


def test_a_recording_times_out_or_stops_at_its_longest() -> None:
    """No speech within the wait is a timeout; speech that never stops ends at the longest request."""
    rec = Recorder(REC)
    assert rec.feed([0.0] * chunks(5_900)) == [] and rec.feed([0.0] * chunks(300)) == ["timeout"]
    assert Recorder(REC).feed([1.0] * chunks(21_000))[-1:] == ["end"]



class Events:
    """The listener's events, kept: only the finished recordings matter here."""

    def __init__(self) -> None:
        """Nothing heard yet."""
        self.recordings: list[Recording] = []

    def on_utterance(self, rec: Recording) -> None:
        """A recording ended."""
        self.recordings.append(rec)

    def __getattr__(self, name: str) -> Any:
        """Every other event is ignored."""
        return lambda *args, **kwargs: None


class LoudIsSpeech:
    """A speech detector for the test: a loud chunk is speech."""

    def reset(self) -> None:
        """Nothing to forget."""

    def __call__(self, chunk: "np.ndarray[Any, Any]") -> float:
        """Speech for a loud chunk."""
        return 1.0 if float(np.abs(chunk).max()) > 0.1 else 0.0


def test_a_recording_knows_when_the_last_speech_was(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Frames of 80 ms, four loud and then silence until the pause ends the request: the recording says its last
    speech was in the fourth frame, not when the pause was over (that is what the turn's hearing time counts from)."""
    now = [500.0]
    monkeypatch.setattr(listener_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    cfg = default_config()
    events = Events()
    listener = Listener(cfg.voice, cfg.wake, tmp_path, events, levels_wanted=lambda: False, is_speaking=lambda: False)
    listener._vad = LoudIsSpeech()  # type: ignore[assignment]
    listener.listen(6.0, "command")
    loud, quiet = np.full(MIC_FRAME, 0.5, np.float32), np.zeros(MIC_FRAME, np.float32)
    for index in range(20):
        now[0] += 0.08
        listener._frame += 1
        notify: list[Any] = []
        listener._process(loud if index < 4 else quiet, notify)
        listener_module._run(notify)
    [rec] = events.recordings
    assert rec.last_speech_at == pytest.approx(500.0 + 4 * 0.08) and rec.last_speech_frame == 4
    assert listener.mode == "busy"
