"""Echo cancellation on this PC's own microphone, so he can be interrupted while he speaks through the PC's speakers.

WebRTC's audio processing (LiveKit's ``AudioProcessingModule``; the canceller browsers use) hears what the speakers play
(the reverse stream) and takes it out of what the microphone picks up (the capture stream). It works in exact 10 ms
frames, 160 samples of the microphone's 16 kHz and 240 of the speakers' 24 kHz, and a frame of any other size makes its
native library abort the whole process, so the cutting into frames is done here and nowhere else.

- ``render`` is called by the speaker's audio callback with each block it writes to the device, at the device's rate,
  silence included (both streams then keep step). It never waits: the block is queued, and handed to the canceller
  at once unless the listener thread is using it at that moment, in which case that thread hands it on, in order.
- ``capture`` is called by the listener thread with each block of the microphone and returns the block without the
  echo (the same length). Everything he hears from then on, the speech detector, the name checks and the recordings,
  is the cleaned audio.
- The delay between the two (the speakers' latency, the microphone's, and half a microphone block) is given as a hint
  from the streams' own reported latencies; the canceller finds the rest itself.

Only this machine's microphone and speakers go through it; a browser carrying the voice has its own canceller
(``web/src/audio`` asks for ``echoCancellation``). The word-based echo filter of the voice session stays as a second
defence. Measured offline against a synthetic room (his voice through a decaying echo path), see ``tests/test_aec.py``.
"""

import collections
import logging
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Final

import numpy as np

from orion.base import OrionError
from orion.voice import MIC_FRAME, MIC_RATE

if TYPE_CHECKING:
    from numpy.typing import NDArray

log = logging.getLogger("orion.aec")

FRAME_MS: Final = 10  # the canceller's frames
CAPTURE_FRAME: Final = MIC_RATE * FRAME_MS // 1000  # 160 samples of the microphone
BLOCK_MS: Final = MIC_FRAME * 1000 // MIC_RATE  # the microphone's blocks: 80 ms, eight frames
PENDING_BLOCKS: Final = 100  # speaker blocks waiting for the lock at most (4 s of 40 ms): older ones are dropped
_PCM_MAX: Final = 32767.0

assert MIC_FRAME % CAPTURE_FRAME == 0  # a microphone block is whole frames


class EchoError(OrionError):
    """Echo cancellation can't run here; the message says why, in words."""


def _pcm(block: "NDArray[np.float32]") -> "NDArray[np.int16]":
    """Float samples in -1...1 as 16-bit ones, clipped."""
    return np.clip(np.rint(block * _PCM_MAX), -32768, 32767).astype(np.int16)


class EchoCanceller:
    """The canceller for this PC's microphone and speakers (see the module docstring). ``configure`` starts it; the
    speaker's thread calls ``render`` and the listener's ``capture``."""

    def __init__(self, on_failure: Callable[[str], None] | None = None) -> None:
        """Start off: ``render`` ignores what it is given and ``capture`` passes the microphone through.

        Args:
            on_failure: told why (in words, from the audio thread or the listener's) when the canceller fails while
                running; it is off from then on, until ``configure`` starts it again.
        """
        self._on_failure = on_failure
        self._lock = threading.Lock()  # one call into the canceller at a time
        self._rtc: Any = None  # livekit.rtc, once it is loaded
        self._apm: Any = None
        self._noise = False
        self._pending: collections.deque[tuple[NDArray[np.int16], int]] = collections.deque(maxlen=PENDING_BLOCKS)
        self._tail: NDArray[np.int16] = np.zeros(0, dtype=np.int16)  # what the last block left of a frame
        self._reverse: dict[int, tuple[Any, NDArray[np.int16]]] = {}  # rate -> (the frame, its samples), reused
        self._capture: tuple[Any, NDArray[np.int16]] | None = None
        self._output_s = 0.0
        self._input_s = 0.0

    @property
    def on(self) -> bool:
        """Whether it is running."""
        return self._apm is not None

    def configure(self, echo: bool, noise: bool) -> None:
        """Start, change or stop it (blocking: the first start loads LiveKit's native library, about a third of a
        second; call it from a worker thread, never from an audio callback).

        Args:
            echo: whether to cancel echo; False stops it.
            noise: whether to quiet steady noise too.

        Raises:
            EchoError: the library can't be loaded or started here. It is off then.
        """
        if not echo:
            with self._lock:  # the native object goes now, while its library is still there (not at exit)
                self._apm = self._capture = None
                self._reverse.clear()
                self._pending.clear()
            return
        if self._apm is not None and noise == self._noise:
            return
        try:
            from livekit import rtc

            apm = rtc.AudioProcessingModule(echo_cancellation=True, noise_suppression=noise, high_pass_filter=True,
                                            auto_gain_control=False)
            with self._lock:
                self._rtc = rtc
                self._hint(apm)
                self._apm, self._noise, self._capture = apm, noise, self._frame(CAPTURE_FRAME, MIC_RATE)
                self._reverse.clear()
                self._pending.clear()
                self._tail = np.zeros(0, dtype=np.int16)
        except (ImportError, OSError, RuntimeError) as e:
            with self._lock:
                self._apm = None
            raise EchoError(f"WebRTC's audio processing (the livekit package) can't start here: {e}") from e
        log.info("echo cancellation is on%s", " (and noise suppression)" if noise else "")

    def latency(self, *, output_s: float | None = None, input_s: float | None = None) -> None:
        """Say how long the speakers' or the microphone's stream buffers audio (what PortAudio reports of an open
        stream), so the canceller knows about how late the echo comes.

        Args:
            output_s: the speakers' stream latency in seconds.
            input_s: the microphone's.
        """
        with self._lock:
            self._output_s = self._output_s if output_s is None else max(0.0, output_s)
            self._input_s = self._input_s if input_s is None else max(0.0, input_s)
            if self._apm is not None:
                try:
                    self._hint(self._apm)
                except RuntimeError as e:
                    log.warning("the echo canceller refused its delay hint: %s", e)

    def render(self, block: "NDArray[np.float32]", rate: int) -> None:
        """What the speakers are playing: the block just written to the device (call it from the audio callback; it
        copies the block, so the buffer may be reused at once, and it does not wait).

        Args:
            block: float samples in -1...1, mono.
            rate: its sample rate in Hz, a multiple of 100 (24000 for his voice).

        Raises:
            ValueError: the rate can't be cut into 10 ms frames of whole samples.
        """
        if self._apm is None:
            return
        if rate % 100:
            raise ValueError(f"{rate} Hz can't be cut into whole 10 ms frames")
        self._pending.append((_pcm(block), rate))
        if self._lock.acquire(blocking=False):  # the listener holds it for a frame at a time: nobody waits for it
            try:
                if (apm := self._apm) is not None:
                    self._drain(apm)
            except RuntimeError as e:
                self._fail(e)
            finally:
                self._lock.release()

    def capture(self, block: "NDArray[np.float32]") -> "NDArray[np.float32]":
        """A block of the microphone without the echo of the speakers.

        Args:
            block: float samples in -1...1 at 16 kHz, whole 10 ms frames (a listener block is 80 ms).

        Returns:
            The cleaned samples, as many as were given; the block itself when the canceller is off.

        Raises:
            ValueError: the block isn't whole frames.
        """
        if (apm := self._apm) is None:
            return block
        if len(block) % CAPTURE_FRAME:
            raise ValueError(f"{len(block)} samples aren't whole {FRAME_MS} ms frames of {MIC_RATE} Hz")
        samples = _pcm(block)
        cleaned = np.empty_like(samples)
        for start in range(0, len(samples), CAPTURE_FRAME):
            with self._lock:
                if self._capture is None:
                    return block
                frame, view = self._capture
                view[:] = samples[start:start + CAPTURE_FRAME]
                try:
                    self._drain(apm)  # what the speakers played meanwhile comes first
                    apm.process_stream(frame)
                except RuntimeError as e:
                    self._fail(e)
                    return block
                cleaned[start:start + CAPTURE_FRAME] = view
        return cleaned.astype(np.float32) / 32768.0

    def _fail(self, error: Exception) -> None:
        """The canceller failed while running: switch it off, say why once. The lock is held."""
        self._apm = None
        self._pending.clear()
        why = f"WebRTC's audio processing stopped: {error}"
        log.error("echo cancellation is off: %s", why)
        if self._on_failure is not None:
            self._on_failure(why)

    def _frame(self, size: int, rate: int) -> "tuple[Any, NDArray[np.int16]]":
        """A mono frame of `size` samples to reuse, and a view of its bytes to write the samples through. The lock
        is held."""
        data = bytearray(size * 2)
        return self._rtc.AudioFrame(data, rate, 1, size), np.frombuffer(data, dtype="<i2")

    def _hint(self, apm: Any) -> None:
        """Tell the canceller the delay of an echo: the speakers' latency, the microphone's, and half a block (a frame
        waits for its block to be whole). The lock is held."""
        apm.set_stream_delay_ms(round((self._output_s + self._input_s) * 1000 + BLOCK_MS / 2))

    def _drain(self, apm: Any) -> None:
        """Hand every waiting block of the speakers' output to the canceller, in whole 10 ms frames, the rest of a
        block waiting for the next. The lock is held."""
        while self._pending:
            samples, rate = self._pending.popleft()
            size = rate // 100
            if (reverse := self._reverse.get(rate)) is None:
                reverse = self._reverse[rate] = self._frame(size, rate)
                self._tail = np.zeros(0, dtype=np.int16)  # another rate: what was left over was meant for another
            frame, view = reverse
            data = np.concatenate((self._tail, samples)) if self._tail.size else samples
            whole = len(data) // size * size
            for start in range(0, whole, size):
                view[:] = data[start:start + size]
                apm.process_reverse_stream(frame)
            self._tail = data[whole:]


def unavailable() -> str:
    """Why echo cancellation can't run on this machine, in words ("" when it can): what ``orion check`` says.

    Returns:
        The reason, or "".
    """
    try:
        EchoCanceller().configure(True, False)
    except EchoError as e:
        return str(e)
    return ""
