"""The always-on microphone: capture, speech detection, standby windows for name checks, and recordings.

How Orion hears you:
- **sleep** (standby): speech is followed as it is said; at every breath, and regularly while the talk goes on, its
  latest seconds go out as a ``Window`` for a name check (after long unbroken talk, such as a TV, only at pauses).
  Once his name is found, the speech it was in becomes the start of the request (``adopt``).
- **record**: a request or a follow-up is recorded until you pause. The pause that ends it adapts: short when the
  sentence sounds finished, longer after a trailing "and" or an "um" (``Recorder.hint``). At the first pause the
  session may transcribe what it has so far; if nobody spoke after that, ``finish`` ends the recording at once.
- **busy**: he is thinking or speaking; only an optional wake-word model and, if barge-in is on, name checks run.

The speech detector is Silero VAD on one ONNX Runtime thread, about 70 µs a chunk. The microphone is captured as
float32, what Silero and Whisper take, and its frames are kept as they come until a window or a recording joins them.
``Segmenter`` and ``Recorder`` are pure (probabilities in, decisions out) and unit-tested; ``Listener`` wires them to
PortAudio (imported when the microphone opens) and calls ``ListenerEvents`` outside its lock.
"""

import collections
import contextlib
import importlib.util
import logging
import queue
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

import numpy as np

from orion.inference import onnx_session
from orion.voice import LEVELS_HZ, MIC_FRAME, MIC_RATE, VAD_CHUNK, LevelGate, levels
from orion.voice.wake import Purpose

if TYPE_CHECKING:
    from numpy.typing import NDArray

    from orion.config import VoiceConfig, WakeConfig
    from orion.voice.aec import EchoCanceller

log = logging.getLogger("orion.listener")

type Mode = Literal["sleep", "record", "busy"]
type SegmentAction = Literal["start", "check", "end"]
type RecordAction = Literal["pause", "end", "timeout"]

CHUNK_MS: Final = VAD_CHUNK / MIC_RATE * 1000  # 32 ms: what one speech-probability covers
MIC_QUEUE_FRAMES: Final = 200  # 16 s of audio waiting for the listener thread before blocks are dropped
PREROLL_FRAMES: Final = 5  # 400 ms kept before speech starts, so the first word is never cut
WAKE_PREROLL_FRAMES: Final = 3  # 240 ms before a wake word fires, kept in its recording
SEGMENT_START_CHUNKS: Final = 2  # consecutive speech chunks that start a stretch of standby speech
WAKE_COOLDOWN_S: Final = 1.5  # a wake-word model doesn't fire twice within this
LEVEL_GAIN: Final = 6.0  # the microphone's RMS is scaled for the visualiser
_VAD_CONTEXT: Final = 64  # samples of the previous chunk Silero sees with each chunk


# ---------------------------------------------------------------------------------------------------- pure parts
@dataclass(frozen=True, slots=True)
class SegmentParams:
    """How standby speech is followed (wake.* and voice.* settings).

    Attributes:
        start_chunks: consecutive speech chunks that start a stretch.
        pause_ms: a pause this long after new speech triggers a check.
        every_s: during unbroken talk, check this often...
        min_speech_s: ...once this much new speech came in.
        end_ms: a pause this long ends the stretch.
        long_talk_s: after this much talk with no pause, checks happen only at pauses.
        speech: a chunk with this probability or more is speech.
        silence: a chunk below this probability is silence (in between counts as neither).
    """

    start_chunks: int
    pause_ms: float
    every_s: float
    min_speech_s: float
    end_ms: float
    long_talk_s: float
    speech: float
    silence: float

    @classmethod
    def of(cls, voice: "VoiceConfig", wake: "WakeConfig") -> "SegmentParams":
        """The parameters from the settings as they are now."""
        return cls(SEGMENT_START_CHUNKS, wake.check_pause_ms, wake.check_every_s, wake.check_min_speech_s,
                   wake.segment_end_ms, wake.long_talk_s, voice.vad_threshold, voice.vad_silence_threshold)


class Segmenter:
    """Follows standby speech chunk by chunk and says when a stretch starts, deserves a check, and ends."""

    __slots__ = ("_new_ms", "_since_ms", "_talk_ms", "active", "p", "run", "silence_ms", "speech_chunks")

    def __init__(self, p: SegmentParams) -> None:
        """Start with no stretch of speech under way.

        Args:
            p: the parameters (replace `p` to apply changed settings).
        """
        self.p = p
        self.active = False
        self.run = 0  # consecutive speech chunks
        self.silence_ms = 0.0  # silence since the last speech
        self.speech_chunks = 0  # speech chunks in the stretch under way
        self._new_ms = 0.0  # speech since the last check
        self._since_ms = 0.0  # time since the stretch started or was last checked
        self._talk_ms = 0.0  # speech since the last pause (unbroken talk)

    @property
    def long_talk(self) -> bool:
        """Whether the talk has gone on unbroken for long_talk_s (a TV, a conversation in the room)."""
        return self._talk_ms >= self.p.long_talk_s * 1000

    def reset(self) -> None:
        """Forget the stretch under way and every counter."""
        self.active = False
        self.run = self.speech_chunks = 0
        self.silence_ms = self._new_ms = self._since_ms = self._talk_ms = 0.0

    def feed(self, probs: Sequence[float]) -> list[SegmentAction]:
        """Follow some chunks.

        Args:
            probs: the speech probability of each 32 ms chunk, in order.

        Returns:
            What happened, in order: "start" (a stretch began), "check" (check its latest words for his name),
            "end" (it ended).
        """
        p = self.p
        actions: list[SegmentAction] = []
        for prob in probs:
            if prob >= p.speech:
                self.run += 1
                self.silence_ms = 0.0
                self._talk_ms += CHUNK_MS
                if self.active:
                    self.speech_chunks += 1
                    self._new_ms += CHUNK_MS
            elif prob < p.silence:
                self.run = 0
                self.silence_ms += CHUNK_MS
                if self.silence_ms >= p.pause_ms:
                    self._talk_ms = 0.0
            if not self.active:
                if self.run >= p.start_chunks:
                    self.active = True
                    self.speech_chunks = self.run
                    self._new_ms = self.run * CHUNK_MS
                    self._since_ms = 0.0
                    actions.append("start")
                continue
            self._since_ms += CHUNK_MS
            enough = self._new_ms >= p.min_speech_s * 1000
            paused = self.silence_ms >= p.pause_ms
            periodic = self._since_ms >= p.every_s * 1000 and not self.long_talk
            if enough and (paused or periodic):
                actions.append("check")
                self._new_ms = self._since_ms = 0.0
            if self.silence_ms >= p.end_ms:
                actions.append("end")
                self.reset()
        return actions


@dataclass(frozen=True, slots=True)
class RecordParams:
    """How a recording ends (voice.* settings).

    Attributes:
        min_ms: the pause that ends a sentence which sounds finished (voice.end_silence_short_ms).
        neutral_ms: the pause when there is no telling (voice.end_silence_ms).
        max_ms: the pause after a trailing word or a filler (voice.end_silence_long_ms).
        max_utterance_s: the longest recording.
        wait_s: how long to wait for speech to begin.
        speech: a chunk with this probability or more is speech.
        silence: a chunk below this probability is silence.
        min_speech_ms: this much speech means a recording heard you (voice.min_speech_ms); less is noise.
    """

    min_ms: float
    neutral_ms: float
    max_ms: float
    max_utterance_s: float
    wait_s: float
    speech: float
    silence: float
    min_speech_ms: float

    @classmethod
    def of(cls, voice: "VoiceConfig", wait_s: float) -> "RecordParams":
        """The parameters from the settings as they are now, waiting `wait_s` for speech."""
        return cls(voice.end_silence_short_ms, voice.end_silence_ms, voice.end_silence_long_ms, voice.max_utterance_s,
                   wait_s, voice.vad_threshold, voice.vad_silence_threshold, voice.min_speech_ms)


class Recorder:
    """Decides when a recording pauses, ends or times out, chunk by chunk."""

    __slots__ = ("_complete", "_paused", "_total_ms", "_waited_ms", "p", "silence_ms", "speech_chunks")

    def __init__(self, p: RecordParams, *, speech_chunks: int = 0, silence_ms: float = 0.0) -> None:
        """Start a recording.

        Args:
            p: the parameters (replace `p` to apply changed settings).
            speech_chunks: speech already heard (a stretch of standby speech it continues).
            silence_ms: silence already since that speech.
        """
        self.p = p
        self.speech_chunks = speech_chunks
        self.silence_ms = silence_ms
        self._complete: bool | None = None
        self._paused = silence_ms >= p.min_ms
        self._total_ms = 0.0
        self._waited_ms = 0.0

    @property
    def heard(self) -> bool:
        """Whether voice.min_speech_ms of speech has been heard."""
        return self.speech_chunks * CHUNK_MS >= self.p.min_speech_ms

    @property
    def endpoint_ms(self) -> float:
        """The pause that ends the recording now, given the last completeness hint."""
        if self._complete is None:
            return self.p.neutral_ms
        return self.p.min_ms if self._complete else self.p.max_ms

    def hint(self, complete: bool | None) -> None:
        """Say whether what was said so far sounds finished (from a transcript taken at the pause).

        Args:
            complete: True (finished: end at the short pause), False (trailing off: wait longer), None (no telling).
        """
        self._complete = complete

    def feed(self, probs: Sequence[float]) -> list[RecordAction]:
        """Follow some chunks of the recording.

        Args:
            probs: the speech probability of each 32 ms chunk, in order.

        Returns:
            "pause" once per pause when it reaches the short pause (a moment to transcribe so far), then "end" when
            it reaches the endpoint or the recording its longest; or "timeout" when no speech came within wait_s.
            Nothing comes after "end" or "timeout".
        """
        p = self.p
        actions: list[RecordAction] = []
        for prob in probs:
            self._total_ms += CHUNK_MS
            self._waited_ms += CHUNK_MS
            if prob >= p.speech:
                self.speech_chunks += 1
                self.silence_ms = 0.0
                self._paused = False
                self._complete = None  # new speech: an earlier hint no longer applies
            elif prob < p.silence:
                self.silence_ms += CHUNK_MS
            if not self.heard:
                if self._waited_ms > p.wait_s * 1000:
                    actions.append("timeout")
                    return actions
                continue
            if self._total_ms >= p.max_utterance_s * 1000:
                actions.append("end")
                return actions
            if not self._paused and self.silence_ms >= p.min_ms:
                self._paused = True
                actions.append("pause")
            if self.silence_ms >= self.endpoint_ms:
                actions.append("end")
                return actions
        return actions


# ---------------------------------------------------------------------------------------------------- the microphone
@dataclass(frozen=True, slots=True)
class Window:
    """Standby speech handed out for a name check.

    Attributes:
        seg_id: the stretch of speech it comes from.
        audio: float32 mono at 16 kHz, at most wake.check_window_s long.
        end_frame: the listener's frame count at its end.
        truncated: the stretch began before the window, so its head is cut off.
        busy: heard while he was thinking or speaking (a barge-in check; his own voice may be in it).
    """

    seg_id: int
    audio: "NDArray[np.float32]"
    end_frame: int
    truncated: bool
    busy: bool


@dataclass(frozen=True, slots=True)
class Recording:
    """A finished recording.

    Attributes:
        rec_id: which recording.
        audio: float32 mono at 16 kHz.
        purpose: why it was recorded.
        seg_id: the stretch of standby speech it continued, None if none.
        last_speech_frame: the listener's frame count at its last speech.
        last_speech_at: when the block of audio with its last speech was processed (monotonic seconds): the end of
            the request, as the turn's timings count it.
    """

    rec_id: int
    audio: "NDArray[np.float32]"
    purpose: Purpose
    seg_id: int | None
    last_speech_frame: int
    last_speech_at: float


class ListenerEvents(Protocol):
    """What the listener tells the voice session; called on the listener thread, outside its lock."""

    def on_window(self, w: Window) -> None:
        """Standby (or busy) speech to check for his name."""

    def on_pause(self, rec_id: int, audio: "NDArray[np.float32]", upto_frame: int) -> None:
        """A recording reached its first short pause: what was said so far, up to `upto_frame`."""

    def on_utterance(self, rec: Recording) -> None:
        """A recording ended; the listener is busy until told otherwise."""

    def on_timeout(self, purpose: Purpose) -> None:
        """Nobody spoke within the wait; the listener is back on standby."""

    def on_hearing(self, active: bool) -> None:
        """Standby speech started (True) or ended (False): the HUD's "Hearing you…"."""

    def on_level(self, value: float, bands: list[float]) -> None:
        """The microphone's level for the visualiser (value 0 and no bands: quiet)."""

    def on_wake_word(self) -> None:
        """The optional wake-word model fired; a recording has started."""


class SileroVAD:
    """Silero VAD v6, the copy faster-whisper ships, on one ONNX Runtime thread: how likely each chunk is speech.

    It carries state from one chunk to the next, so chunks must come in order (``reset`` after a gap).
    """

    def __init__(self) -> None:
        """Open the model from faster-whisper's assets without importing faster-whisper."""
        spec = importlib.util.find_spec("faster_whisper")
        if spec is None or not spec.submodule_search_locations:
            raise RuntimeError("faster-whisper is not installed, and its speech detector comes with it")
        model = Path(next(iter(spec.submodule_search_locations)), "assets", "silero_vad_v6.onnx")
        self.session = onnx_session(model, 1)
        self._input = np.zeros((1, _VAD_CONTEXT + VAD_CHUNK), dtype=np.float32)
        self.reset()

    def reset(self) -> None:
        """Forget what it heard (a new recording)."""
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._input[0, :_VAD_CONTEXT] = 0.0

    def __call__(self, chunk: "NDArray[np.float32]") -> float:
        """The probability that a chunk of VAD_CHUNK float32 samples (-1...1) is speech."""
        self._input[0, _VAD_CONTEXT:] = chunk
        prob, self._h, self._c = self.session.run(None, {"input": self._input, "h": self._h, "c": self._c})
        self._input[0, :_VAD_CONTEXT] = chunk[-_VAD_CONTEXT:]
        return float(prob[0])


class PushedInput:
    """A microphone that isn't this machine's: a browser's, whose samples arrive over the HUD's connection.

    It stands where PortAudio's input stream stands (``start``, ``close``, ``closed``): samples pushed in (16 kHz
    float32 mono, any length) are cut into the listener's frames and queued for its thread, as PortAudio's callback
    does, while it is started.
    """

    def __init__(self) -> None:
        """Closed until the listener starts it."""
        self.closed = True
        self._queue: queue.Queue[NDArray[np.float32] | None] | None = None
        self._carry = np.zeros(0, dtype=np.float32)
        self._lock = threading.Lock()

    def start(self, frames: "queue.Queue[NDArray[np.float32] | None]") -> None:
        """Deliver frames to the listener's queue from now on."""
        with self._lock:
            self._queue = frames
            self._carry = np.zeros(0, dtype=np.float32)
            self.closed = False

    def close(self) -> None:
        """Stop delivering; samples pushed meanwhile are dropped."""
        with self._lock:
            self.closed = True
            self._queue = None

    def push(self, samples: "NDArray[np.float32]") -> None:
        """Samples from the browser, in order; whole frames go to the listener (dropped if it is 16 s behind).

        Args:
            samples: 16 kHz mono float32 samples, -1 to 1.
        """
        with self._lock:
            target = self._queue
            if target is None or self.closed:
                return
            data = np.concatenate((self._carry, samples.astype(np.float32, copy=False)))
            whole = len(data) // MIC_FRAME * MIC_FRAME
            self._carry = data[whole:].copy()
        for start in range(0, whole, MIC_FRAME):
            with contextlib.suppress(queue.Full):
                target.put_nowait(data[start:start + MIC_FRAME].copy())


class Listener:
    """The microphone thread: standby name checks, recordings, and the optional wake-word model."""

    def __init__(self, voice: "VoiceConfig", wake: "WakeConfig", models_dir: Path, events: ListenerEvents, *,
                 levels_wanted: Callable[[], bool], is_speaking: Callable[[], bool],
                 echo: "EchoCanceller | None" = None) -> None:
        """Prepare; the microphone opens in `start`.

        Args:
            voice: the voice settings, read at use time.
            wake: the name-spotting settings, read at use time.
            models_dir: the models folder (an optional wake-word model lives in its openwakeword/ folder).
            events: where to report.
            levels_wanted: whether a HUD shows the microphone's level (otherwise it isn't computed).
            is_speaking: whether he is speaking (his own voice isn't checked for his name on standby).
            echo: the echo canceller: this machine's microphone is heard through it (a browser's is not).
        """
        self.voice = voice
        self.wake = wake
        self.models_dir = models_dir
        self.events = events
        self.levels_wanted = levels_wanted
        self.is_speaking = is_speaking
        self.echo = echo
        self.mode: Mode = "sleep"
        self.muted = False
        self._lock = threading.RLock()
        self._queue: queue.Queue[NDArray[np.float32] | None] = queue.Queue(maxsize=MIC_QUEUE_FRAMES)
        self._stream: Any = None
        self.remote: PushedInput | None = None  # a browser's microphone in place of this machine's
        self._thread: threading.Thread | None = None
        self._vad: SileroVAD | None = None
        self._wake_model: Any = None
        self._wake_key = ""
        self._last_wake = 0.0
        self._frame = 0  # frames processed since start
        self._preroll: collections.deque[NDArray[np.float32]] = collections.deque(maxlen=PREROLL_FRAMES)
        self._pending = np.zeros(VAD_CHUNK + MIC_FRAME, dtype=np.float32)  # samples waiting for a whole chunk
        self._pending_n = 0
        self._level_gate = LevelGate(LEVELS_HZ)
        self._segmenter = Segmenter(SegmentParams.of(voice, wake))
        self._window: collections.deque[NDArray[np.float32]] = collections.deque()  # the stretch's latest frames
        self._window_cut = False  # the stretch began before the window: its head is cut off
        self._seg_id = 0
        self._seg_busy = False  # the stretch under way was heard while he was busy
        self._ended: tuple[int, list[NDArray[np.float32]], int, bool] | None = None  # (id, frames, speech, busy)
        self._rec_id = 0
        self._rec: list[NDArray[np.float32]] = []
        self._rec_seg: int | None = None
        self._rec_purpose: Purpose = "command"
        self._rec_last_speech = 0
        self._speech_at = 0.0  # when a block with speech in it was last processed (monotonic seconds)
        self._recorder: Recorder | None = None

    # ------------------------------------------------------------------------------------------------ control
    def start(self) -> None:
        """Open the microphone and start the listener thread.

        Raises:
            Exception: PortAudio could not open the device (busy, unplugged).
        """
        self._vad = SileroVAD()
        self._load_wake_model()
        self._open()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="listener")
        self._thread.start()
        model = " (and the wake-word model)" if self._wake_model else ""
        log.info("microphone open, waiting for “Orion”%s", model)

    def close(self) -> None:
        """Close the microphone and stop the listener thread."""
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.close()
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(None)

    def listen(self, wait_s: float, purpose: Purpose, *, seed: bool = False, preroll: bool = False) -> int:
        """Start recording without his name: a follow-up, or the request after "Orion" alone.

        Args:
            wait_s: how long to wait for speech to begin.
            purpose: why.
            seed: continue the stretch of standby speech under way, so words said right after his name stay.
            preroll: keep the last 400 ms, said while the previous words were being transcribed.

        Returns:
            The recording's id.
        """
        with self._lock:
            return self._begin(wait_s, purpose, seed=seed, preroll=PREROLL_FRAMES if preroll else 0)

    def adopt(self, seg_id: int, wait_s: float, *, awaiting: bool) -> int | None:
        """His name was heard in this stretch of standby speech: it becomes the start of the request.

        Args:
            seg_id: the stretch (still being said, or just ended).
            wait_s: with `awaiting`, how long to wait for the request itself.
            awaiting: only his name so far: wait for the request instead of ending at the next pause.

        Returns:
            The recording's id, or None when the stretch is gone or the listener isn't on standby.
        """
        notify: list[Callable[[], None]] = []
        with self._lock:
            if self.mode != "sleep":
                return None
            segmenter = self._segmenter
            if segmenter.active and self._seg_id == seg_id:
                frames, speech, silence = list(self._window), segmenter.speech_chunks, segmenter.silence_ms
            elif not segmenter.active and self._ended is not None and self._ended[0] == seg_id:
                frames, speech, silence = self._ended[1], self._ended[2], float(self.wake.segment_end_ms)
            else:
                return None
            self._end_segment(notify)
            self._ended = None
            self._rec_id += 1
            self._rec = frames
            self._rec_seg = seg_id
            self._rec_purpose = "command"
            self._rec_last_speech = self._frame
            self._recorder = Recorder(RecordParams.of(self.voice, wait_s), speech_chunks=0 if awaiting else speech,
                                      silence_ms=0.0 if awaiting else silence)
            self.mode = "record"
            rec_id = self._rec_id
        _run(notify)
        return rec_id

    def finish(self, rec_id: int, upto_frame: int) -> bool:
        """End a recording now if nobody spoke after `upto_frame` (a transcript taken at the pause covers it).

        Returns:
            True when the recording ended (``on_utterance`` follows); False otherwise.
        """
        notify: list[Callable[[], None]] = []
        with self._lock:
            if self.mode != "record" or rec_id != self._rec_id or self._rec_last_speech > upto_frame:
                return False
            self._end_record(notify)
        _run(notify)
        return True

    def hint(self, rec_id: int, complete: bool | None) -> None:
        """Pass on whether the recording sounds finished so far (see ``Recorder.hint``)."""
        with self._lock:
            if self.mode == "record" and rec_id == self._rec_id and self._recorder is not None:
                self._recorder.hint(complete)

    def busy(self) -> None:
        """He is thinking or speaking: stop following standby speech."""
        notify: list[Callable[[], None]] = []
        with self._lock:
            self.mode = "busy"
            self._end_segment(notify)
            self._ended = None
        _run(notify)

    def sleep(self) -> None:
        """Back to standby. Speech heard while he was busy is his own voice, so it is never adopted."""
        notify: list[Callable[[], None]] = []
        with self._lock:
            self.mode = "sleep"
            if self._seg_busy:
                self._end_segment(notify)
                self._ended = None
        _run(notify)

    def use_input(self, remote: "PushedInput | None") -> None:
        """Listen through another microphone from now on: a browser's (its samples pushed in), or this machine's
        again (None). What was being followed or recorded is dropped; the mode stays.

        Args:
            remote: the browser's microphone, or None for this machine's.

        Raises:
            Exception: this machine's microphone could not be opened (it stays without one).
        """
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.close()
        self.remote = remote
        with self._lock:
            self._end_segment([])
            self._pending_n = 0
        if not self.muted:
            self._open()

    def close_input(self) -> None:
        """Listen to nothing until another input is given (a browser that carried the voice went away, and this
        machine has no microphone to go back to)."""
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.close()
        self.remote = None
        self.sleep()

    def set_muted(self, muted: bool) -> None:
        """MIC MUTED closes the microphone (the desktop shows it unused); unmuting opens it again."""
        if muted:
            self.muted = True
            self.sleep()
            if self._stream is not None:
                self._stream.close()
            return
        if self._stream is not None and self._stream.closed:
            try:
                self._open()
            except Exception as e:  # noqa: BLE001 (unplugged meanwhile, say: it stays muted, and says why)
                log.warning("could not open the microphone again: %s", e)
                return
        self.muted = False

    # ------------------------------------------------------------------------------------------------ PortAudio
    def _open(self) -> None:
        """Open and start the input stream; a stream that fails to start is closed, never kept."""
        if self.remote is not None:
            self.remote.start(self._queue)
            self._stream = self.remote
            return
        import sounddevice as sd

        stream = sd.InputStream(samplerate=MIC_RATE, channels=1, dtype="float32", blocksize=MIC_FRAME,
                                device=self.voice.input_device, callback=self._callback)
        try:
            stream.start()
        except Exception:
            stream.close()
            raise
        if self.echo is not None:
            self.echo.latency(input_s=float(stream.latency))
        self._stream = stream

    def _callback(self, indata: Any, _frames: int, _time: Any, _status: Any) -> None:
        """PortAudio's thread: hand the block to the listener thread (dropped if it is 16 s behind)."""
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(indata[:, 0].copy())

    def _load_wake_model(self) -> None:
        """Load the optional openWakeWord model (voice.wake_model); a missing one only costs the instant wake."""
        name = self.voice.wake_model
        if not name:
            return
        folder = self.models_dir / "openwakeword"
        path = Path(name) if name.endswith(".onnx") else folder / f"{name}.onnx"
        try:
            from openwakeword.model import Model

            if not path.is_file():
                raise FileNotFoundError(path)
            model = Model(wakeword_models=[str(path)], inference_framework="onnx",
                          melspec_model_path=str(folder / "melspectrogram.onnx"),
                          embedding_model_path=str(folder / "embedding_model.onnx"))
        except Exception as e:  # noqa: BLE001 (the optional extra: any failure means "wake on his name only")
            log.warning("wake-word model %r not used (%s): he wakes on his name", name, e)
            return
        self._wake_model, self._wake_key = model, next(iter(model.models))

    # ------------------------------------------------------------------------------------------------ the thread
    def _loop(self) -> None:
        """The listener thread: one frame at a time, decisions under the lock, callbacks outside it."""
        while True:
            frame = self._queue.get()
            if frame is None:
                return
            if self.muted:
                continue
            if self.echo is not None and self.remote is None:  # this machine's microphone: without his own voice
                frame = self.echo.capture(frame)
            notify: list[Callable[[], None]] = []
            try:
                with self._lock:
                    self._frame += 1
                    self._process(frame, notify)
            except Exception:
                log.exception("listener error")
            _run(notify)

    def _probs(self, frame: "NDArray[np.float32]") -> list[float]:
        """Speech probabilities of every whole 32 ms chunk available after this frame (the new samples)."""
        vad = self._vad
        assert vad is not None  # start() made it
        n = len(frame)
        self._pending[self._pending_n:self._pending_n + n] = frame
        self._pending_n += n
        probs: list[float] = []
        start = 0
        while self._pending_n - start >= VAD_CHUNK:
            probs.append(vad(self._pending[start:start + VAD_CHUNK]))
            start += VAD_CHUNK
        left = self._pending_n - start
        if start:
            self._pending[:left] = self._pending[start:self._pending_n]
        self._pending_n = left
        return probs

    def _process(self, frame: "NDArray[np.float32]", notify: list[Callable[[], None]]) -> None:
        """Handle one frame in the current mode."""
        if self.mode == "record":
            self._record(frame, notify)
            return
        self._preroll.append(frame)
        if self._wake_model is not None and self._wake_word(frame, notify):
            return
        voice = self.voice
        if self.mode == "sleep":
            if voice.name_wake and not self.is_speaking():
                self._segment(frame, busy=False, notify=notify)
            else:
                self._end_segment(notify)
        elif voice.barge_in and voice.name_wake:
            self._segment(frame, busy=True, notify=notify)

    def _wake_word(self, frame: "NDArray[np.float32]", notify: list[Callable[[], None]]) -> bool:
        """Run the wake-word model on a frame; True when it fired (a recording has started)."""
        pcm = np.clip(frame * 32768, -32768, 32767).astype(np.int16)  # openWakeWord takes 16-bit samples only
        score = float(self._wake_model.predict(pcm)[self._wake_key])
        now = time.monotonic()
        if score < self.voice.wake_threshold or now - self._last_wake < WAKE_COOLDOWN_S:
            return False
        self._last_wake = now
        self._wake_model.reset()
        log.info("wake word (%.2f)", score)
        self._begin(self.voice.command_wait_s, "command", preroll=WAKE_PREROLL_FRAMES)
        notify.append(self.events.on_wake_word)
        return True

    def _segment(self, frame: "NDArray[np.float32]", *, busy: bool, notify: list[Callable[[], None]]) -> None:
        """Follow standby (or busy) speech and hand out windows for name checks."""
        segmenter = self._segmenter
        segmenter.p = SegmentParams.of(self.voice, self.wake)
        was_active = segmenter.active
        probs = self._probs(frame)
        if any(p >= segmenter.p.speech for p in probs):
            self._speech_at = time.monotonic()
        actions = segmenter.feed(probs)
        if "start" in actions:
            self._seg_id += 1
            self._seg_busy = busy
            self._ended = None
            frames = round(self.wake.check_window_s * MIC_RATE / MIC_FRAME)  # the window, to the nearest frame
            self._window = collections.deque(self._preroll, maxlen=frames)  # the preroll already holds this frame
            self._window_cut = False
            if not busy:
                notify.append(lambda: self.events.on_hearing(True))
        elif was_active:
            self._window_cut |= len(self._window) == self._window.maxlen
            self._window.append(frame)
        if segmenter.active and not busy:
            self._level(frame, notify)
        for action in actions:
            if action == "check":
                window = Window(self._seg_id, _join(self._window), self._frame, self._window_cut, busy)
                notify.append(lambda w=window: self.events.on_window(w))
            elif action == "end":
                self._ended = (self._seg_id, list(self._window), segmenter.speech_chunks, busy)
                self._finish_segment(busy, notify)

    def _end_segment(self, notify: list[Callable[[], None]]) -> None:
        """Drop the stretch of standby speech under way (it is not kept for adoption)."""
        if self._segmenter.active:
            busy = self._seg_busy
            self._segmenter.reset()
            self._finish_segment(busy, notify)

    def _finish_segment(self, busy: bool, notify: list[Callable[[], None]]) -> None:
        """Tell the HUD standby speech stopped (unless it was heard while he was busy)."""
        if not busy:
            notify.append(lambda: self.events.on_hearing(False))
            notify.append(lambda: self.events.on_level(0.0, []))

    def _begin(self, wait_s: float, purpose: Purpose, *, seed: bool = False, preroll: int = 0) -> int:
        """Start a recording (the lock is held)."""
        speech, silence = 0, 0.0
        if seed and self._segmenter.active:
            self._rec = list(self._window)
            speech, silence = self._segmenter.speech_chunks, self._segmenter.silence_ms
        else:
            self._rec = list(self._preroll)[-preroll:] if preroll else []
            if self._vad is not None:
                self._vad.reset()
            self._pending_n = 0
        self._segmenter.reset()
        self._ended = None
        self._rec_id += 1
        self._rec_seg = None
        self._rec_purpose = purpose
        self._rec_last_speech = self._frame
        self._recorder = Recorder(RecordParams.of(self.voice, wait_s), speech_chunks=speech, silence_ms=silence)
        self.mode = "record"
        return self._rec_id

    def _record(self, frame: "NDArray[np.float32]", notify: list[Callable[[], None]]) -> None:
        """Add a frame to the recording and act on its pauses, its end or a timeout."""
        recorder = self._recorder
        assert recorder is not None  # set whenever the mode is "record"
        self._rec.append(frame)
        self._level(frame, notify)
        recorder.p = RecordParams.of(self.voice, recorder.p.wait_s)
        probs = self._probs(frame)
        if any(p >= recorder.p.speech for p in probs):
            self._rec_last_speech = self._frame
            self._speech_at = time.monotonic()
        for action in recorder.feed(probs):
            if action == "pause":
                audio = _join(self._rec)
                notify.append(lambda r=self._rec_id, a=audio, u=self._frame: self.events.on_pause(r, a, u))
            elif action == "timeout":
                purpose: Purpose = self._rec_purpose
                self.mode = "sleep"
                self._recorder = None
                if self._wake_model is not None:
                    self._wake_model.reset()
                notify.append(lambda: self.events.on_level(0.0, []))
                notify.append(lambda why=purpose: self.events.on_timeout(why))
            else:
                self._end_record(notify)

    def _end_record(self, notify: list[Callable[[], None]]) -> None:
        """Close the recording and hand it out; the listener is busy until told otherwise (the lock is held)."""
        recording = Recording(self._rec_id, _join(self._rec), self._rec_purpose, self._rec_seg, self._rec_last_speech,
                              self._speech_at)
        self.mode = "busy"
        self._recorder = None
        self._rec = []
        notify.append(lambda: self.events.on_level(0.0, []))
        notify.append(lambda: self.events.on_utterance(recording))

    def _level(self, frame: "NDArray[np.float32]", notify: list[Callable[[], None]]) -> None:
        """The microphone's level for the HUD's visualiser, at most LEVELS_HZ times a second, only if it is shown."""
        if self.levels_wanted() and self._level_gate.ready():
            value, bands = levels(frame, MIC_RATE, LEVEL_GAIN)
            notify.append(lambda: self.events.on_level(value, bands))


def _join(frames: Sequence["NDArray[np.float32]"]) -> "NDArray[np.float32]":
    """Frames as one array (empty for none)."""
    return np.concatenate(frames) if frames else np.zeros(0, dtype=np.float32)


def _run(notify: list[Callable[[], None]]) -> None:
    """Call the queued event callbacks (outside the lock); one that fails is logged and the rest still run."""
    for call in notify:
        try:
            call()
        except Exception:
            log.exception("a listener event handler failed")
