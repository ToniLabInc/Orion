"""Reply speech, from streamed text to sound.

- ``SentenceSplitter`` cuts a streamed reply into speakable pieces: the first clause as early as it can (he starts
  talking at the first comma), then whole sentences, capped at a clause near ``MAX_CHARS``. ``boundary()`` is called
  when a tool round starts, so "Let me check." is spoken before the tool runs and never glued to what follows.
- ``clean_for_speech`` turns Markdown, links and symbols into words a voice can say.
- ``TTS`` is Kokoro (American and British English voices): warmed up at load, each voice's style cached.
- ``Speaker`` synthesises on its own thread and plays through a PortAudio callback stream: gap-free, stopped
  instantly, kept open while a conversation lasts, and with no polling (it signals when it has drained). A device
  thread of its own opens, restarts and closes the stream, so the event loop never waits for the sound server.
- ``SpeechStream`` is one reply's speech: deltas in, spoken pieces out, and what was said remembered (to recognise his
  own voice in the microphone).

sounddevice and kokoro_onnx load inside the functions that need them, so the splitter imports with numpy alone.
"""

import asyncio
import collections
import contextlib
import functools
import io
import logging
import queue
import re
import struct
import threading
import time
import wave
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

import numpy as np

from orion.base import logged
from orion.inference import onnx_session, release_memory
from orion.voice import ACCENTS, LEVELS_HZ, TTS_RATE, is_voice, levels

if TYPE_CHECKING:
    from numpy.typing import NDArray

    from orion.config import TTSConfig
    from orion.timing import TurnClock
    from orion.voice.aec import EchoCanceller

log = logging.getLogger("orion.tts")

ABBREVIATIONS: Final = frozenset({"mr", "mrs", "ms", "dr", "st", "vs", "etc", "e.g", "i.e", "approx", "fig", "inc",
                                  "ltd"})  # a full stop after these doesn't end a sentence ("no" does: "No.")
FIRST_MIN_WORDS: Final = 4  # a first clause this many words long is spoken on its own...
FIRST_MAX_CHARS: Final = 60  # ...and a first piece is cut at a word near this length if no clause ends sooner
MAX_CHARS: Final = 150  # a long sentence is cut at a clause near this length
OUTPUT_LATENCY_S: Final = 0.1  # the device buffer: a callback hiccup never clicks, and stop() discards it
BLOCK: Final = 960  # 40 ms per callback at 24 kHz
STREAM_IDLE_CLOSE_S: Final = 3.0  # the speakers' stream closes this long after the last sound, unless held
LEVEL_GAIN: Final = 4.0  # his voice's RMS is scaled for the visualiser
WARM_UP: Final = "Ready."  # synthesised once at load: the first reply pays no start-up
REMOTE_LEAD_BLOCKS: Final = 4  # a browser's speakers get 160 ms ahead at the start of each sound (its jitter buffer)
AUDIO_FRAME: Final = 2  # the first byte of a binary frame of his voice to a browser (then the generation, then PCM)
CHIME_NOTE_S: Final = 0.09

type Chime = Literal["wake", "done", "error", "standby"]

_SENTENCE_END: Final = re.compile(r"([.!?…]+[\"')\]”]?)(\s+)|(\n+)")
_CLAUSE_END: Final = re.compile(r"[,;:](?=\s)|\s\u2014\s?|\u2014(?=\s)")  # , ; : or an em dash
_FENCE: Final = "```"

# How Markdown, links and symbols are read out.
_SPEECH_RULES: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)"), r"\1"),  # [text](link) -> text
    (re.compile(r"https?://\S+"), "the link on screen"),
    (re.compile(r"`([^`]*)`"), r"\1"),
    (re.compile(r"(\*\*|\*|~~)(?=\S)(.+?)(?<=\S)\1"), r"\2"),
    (re.compile(r"(?<![\w/])__?(?=\S)(.+?)(?<=\S)__?(?![\w/])"), r"\1"),  # _italic_, but not snake_case
    (re.compile(r"~/"), "your home folder, "),
    (re.compile(r"(?<=\w)_(?=\w)"), " "),
    (re.compile(r"^\s*#{1,6}\s*", re.MULTILINE), ""),
    (re.compile(r"^\s*(?:[-*•>]|\d+[.)])\s+", re.MULTILINE), ""),
    (re.compile(r"°\s*C\b"), " degrees Celsius"),
    (re.compile(r"°\s*F\b"), " degrees Fahrenheit"),
    (re.compile(r"°"), " degrees"),
    (re.compile(r"(\d)\s*%"), r"\1 percent"),
    (re.compile(r"\bkm/h\b"), "kilometres per hour"),
    (re.compile(r"\s*[\u2014\u2013]\s*"), ", "),  # an em or en dash is a pause
    (re.compile(r"\s&\s"), " and "),
    (re.compile(r"[\U0001F000-\U0001FAFF☀-➿️]"), ""),
    (re.compile(r"[*#|]"), " "),
    (re.compile(r"\s{2,}"), " "),
)


def clean_for_speech(text: str) -> str:
    """A piece of a reply as words a voice can say: Markdown removed, links named, symbols read out.

    Args:
        text: the piece.

    Returns:
        The cleaned text, without leading or trailing spaces and commas.
    """
    for pattern, replacement in _SPEECH_RULES:
        text = pattern.sub(replacement, text)
    return text.strip(" ,")


# ---------------------------------------------------------------------------------------------------- the splitter
@dataclass(frozen=True, slots=True)
class ChunkParams:
    """How a reply is cut into spoken pieces.

    Attributes:
        first_min_chars: a first clause this long is spoken on its own (tts.first_chunk_min_chars).
        min_chars: after the first piece, shorter sentences wait for the next one (tts.min_sentence_chars).
        code_note: said instead of reading a code block aloud (prompts.code_note).
        first_min_words: a first clause with this many words is spoken on its own too.
        first_max_chars: failing a clause, the first piece is cut at a word near this length.
        max_chars: a long sentence is cut at a clause near this length.
    """

    first_min_chars: int
    min_chars: int
    code_note: str
    first_min_words: int = FIRST_MIN_WORDS
    first_max_chars: int = FIRST_MAX_CHARS
    max_chars: int = MAX_CHARS


class SentenceSplitter:
    """Streams reply text into speakable pieces: the first clause early, then sentences."""

    __slots__ = ("_buf", "_first", "_in_code", "_pending", "_said_code", "_space", "p")

    def __init__(self, p: ChunkParams) -> None:
        """Start a reply.

        Args:
            p: how to cut it.
        """
        self.p = p
        self._buf = ""  # text not yet spoken
        self._pending = ""  # raw text not yet checked for code fences (they can arrive split across deltas)
        self._first = True  # the next piece opens the reply, or what follows a tool round
        self._in_code = False
        self._said_code = False
        self._space = False  # a tool round ended this text: what follows needs a space before it

    def feed(self, delta: str) -> list[str]:
        """Add streamed text.

        Args:
            delta: the next piece of the reply as the model wrote it.

        Returns:
            The pieces now ready to speak, in order.
        """
        if self._space and delta:
            if not delta[0].isspace():
                delta = " " + delta
            self._space = False
        out: list[str] = []
        self._pending += delta
        while (index := self._pending.find(_FENCE)) != -1:
            before, self._pending = self._pending[:index], self._pending[index + len(_FENCE):]
            if not self._in_code:
                self._buf += before
                out += self._extract(final=True)
                if not self._said_code and self.p.code_note:
                    out.append(self.p.code_note)
                    self._said_code = True
            self._in_code = not self._in_code
        held = len(self._pending) - len(self._pending.rstrip("`"))  # maybe the start of a fence
        safe = self._pending[: len(self._pending) - held]
        self._pending = self._pending[len(safe):]
        if not self._in_code:
            self._buf += safe
        return out + self._extract(final=False)

    def boundary(self) -> list[str]:
        """A tool round starts: speak what is pending now, and space-separate whatever comes after.

        Returns:
            The pieces to speak before the tool runs.
        """
        out = self._drain()
        self._space = True
        self._first = True  # after a wait, the next words should come quickly again
        return out

    def flush(self) -> list[str]:
        """The reply is complete: everything left.

        Returns:
            The last pieces.
        """
        out = self._drain()
        self._first = True
        self._space = False
        return out

    def _drain(self) -> list[str]:
        """Everything pending, as pieces (code text never included)."""
        if not self._in_code:
            self._buf += self._pending
        self._pending = ""
        return self._extract(final=True)

    def _extract(self, *, final: bool) -> list[str]:
        """Cut ready pieces off the buffer; with `final`, the rest too."""
        out: list[str] = []
        while True:
            piece = self._next_piece()
            if piece is None:
                break
            if piece:
                out.append(piece)
        if final and (rest := self._buf.strip()):
            out.append(rest)
            self._first = False
        if final:
            self._buf = ""
        return out

    def _next_piece(self) -> str | None:
        """The next piece the buffer allows, removed from it; "" for whitespace skipped; None when none is ready."""
        buf = self._buf
        p = self.p
        for m in _SENTENCE_END.finditer(buf):
            end = m.end()
            candidate = buf[:end].strip()
            if m.group(1) and _abbreviation(buf[: m.start(1) + 1]):
                continue
            if not self._first and len(candidate) < p.min_chars and not m.group(3):
                continue
            return self._take(end)
        if self._first:
            for m in _CLAUSE_END.finditer(buf):
                head = buf[: m.end()].strip()
                if len(head) >= p.first_min_chars or len(head.split()) >= p.first_min_words:
                    return self._take(m.end())
            if len(buf) >= p.first_max_chars:
                return self._take(_word_cut(buf, p.first_max_chars))
            return None
        if len(buf) >= p.max_chars:
            clauses = [m.end() for m in _CLAUSE_END.finditer(buf, 0, p.max_chars) if m.end() >= p.min_chars]
            return self._take(clauses[-1] if clauses else _word_cut(buf, p.max_chars))
        return None

    def _take(self, end: int) -> str:
        """Remove buf[:end] and return it stripped; the next piece no longer opens the reply."""
        piece, self._buf = self._buf[:end].strip(), self._buf[end:]
        if piece:
            self._first = False
        return piece


def _abbreviation(text: str) -> bool:
    """Whether `text` (up to a full stop) ends in an abbreviation such as "Dr." or "e.g."."""
    words = re.findall(r"([\w.]+)[.]$", text)
    return bool(words) and words[0].lower().rstrip(".") in ABBREVIATIONS


def _word_cut(text: str, limit: int) -> int:
    """Where to cut `text` near `limit`: after the last space before it, or at it when there is none."""
    space = text.rfind(" ", 0, limit)
    return space + 1 if space > 0 else limit


# ---------------------------------------------------------------------------------------------------- Kokoro
class TTS:
    """Kokoro text-to-speech: one ONNX session, a cached style per voice, warmed up at load."""

    def __init__(self, cfg: "TTSConfig", models_dir: Path, model_file: str, voices_file: str, threads: int) -> None:
        """Prepare; nothing loads until `load()` (the runtime calls it in a worker thread).

        Args:
            cfg: the tts settings (voice, speed), read at use time.
            models_dir: the models folder.
            model_file: Kokoro's model file name (models.kokoro_model).
            voices_file: its voices file name (models.kokoro_voices).
            threads: ONNX threads for one sentence.
        """
        self.cfg = cfg
        self.model_path = models_dir / model_file
        self.voices_path = models_dir / voices_file
        self.threads = threads
        self._kokoro: Any = None
        self._styles: dict[str, NDArray[np.float32]] = {}
        self._lock = threading.Lock()  # one synthesis at a time (replies and the settings page's previews)

    def load(self) -> None:
        """Open Kokoro and synthesise one word, so espeak's set-up and the first ONNX run are paid now (blocking).

        Raises:
            Exception: the model files are missing or broken (`orion setup` fetches them).
        """
        from kokoro_onnx import Kokoro

        logging.getLogger("phonemizer").setLevel(logging.ERROR)  # importing kokoro_onnx resets it; harmless chatter
        kokoro = Kokoro.from_session(onnx_session(self.model_path, self.threads), str(self.voices_path))
        with self._lock:
            self._kokoro = kokoro
            self._styles.clear()
        release_memory()
        self.synth(WARM_UP)

    def voices(self) -> list[str]:
        """The installed voices Orion offers (American and British English), as Kokoro names them."""
        kokoro = self._require()
        return [v for v in kokoro.get_voices() if is_voice(v)]

    def synth(self, text: str, voice: str | None = None, speed: float | None = None) -> "NDArray[np.float32]":
        """Speech for a piece of text (blocking).

        Args:
            text: what to say.
            voice: a voice name (tts.voice by default).
            speed: the speed (tts.speed by default).

        Returns:
            float32 mono audio at 24 kHz.
        """
        kokoro = self._require()
        name = voice or self.cfg.voice
        with self._lock:
            style = self._styles.get(name)
            if style is None:
                style = self._styles[name] = kokoro.get_voice_style(name)
            audio, _ = kokoro.create(text, voice=style, speed=speed or self.cfg.speed, lang=ACCENTS[name[0]][0])
        return np.asarray(audio, dtype=np.float32)

    def synth_wav(self, text: str, voice: str | None = None, speed: float | None = None) -> bytes:
        """Speech for a piece of text as a 16-bit mono WAV file (the settings page's voice preview)."""
        pcm = (np.clip(self.synth(text, voice, speed), -1, 1) * 32767).astype("<i2")
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(TTS_RATE)
            wav.writeframes(pcm.tobytes())
        return buffer.getvalue()

    def _require(self) -> Any:
        """Kokoro, or an error saying it isn't loaded."""
        kokoro = self._kokoro
        if kokoro is None:
            raise RuntimeError("the voice is not loaded")
        return kokoro


@functools.cache
def chime(kind: Chime) -> "NDArray[np.float32]":
    """A soft two-tone chime, generated (no sound files) and made once per kind (read-only).

    Args:
        kind: "wake" (he listens), "done", "error", or "standby" (he stopped listening; softer).
    """
    notes, gain = {"wake": ((880, 1320), 1.0), "done": ((1320, 880), 1.0), "error": ((440, 330), 1.0),
                   "standby": ((660, 440), 0.7)}[kind]
    t = np.arange(int(TTS_RATE * CHIME_NOTE_S)) / TTS_RATE
    envelope = np.minimum(1, t / 0.005) * np.exp(-t * 28)
    parts = [gain * envelope * (0.16 * np.sin(2 * np.pi * f * t) + 0.05 * np.sin(4 * np.pi * f * t)) for f in notes]
    audio = np.concatenate(parts).astype(np.float32)
    audio.flags.writeable = False
    return audio


# ---------------------------------------------------------------------------------------------------- the speaker
class SpeakerEvents(Protocol):
    """What the speaker tells the rest of Orion; called on the event loop."""

    def on_playing(self, playing: bool) -> None:
        """He started (True) or stopped (False) making sound."""

    def on_caption(self, text: str) -> None:
        """A spoken piece starts playing."""

    def on_voice_level(self, value: float, bands: list[float]) -> None:
        """His voice's level for the visualiser (value 0 and no bands: quiet)."""


@dataclass(slots=True)
class _Chunk:
    """Audio waiting to be played, with its caption and precomputed levels."""

    audio: "NDArray[np.float32]"
    text: str
    levels: list[tuple[float, list[float]]] = field(default_factory=list)
    pos: int = 0
    started: bool = False
    first: Callable[[float], None] | None = None  # told (on the loop) when its first frame went to the device


class Speaker:
    """Synthesis thread -> chunk queue -> PortAudio callback; instant stop; held open while a conversation lasts.

    Only the device thread opens, restarts or closes the stream, one after the other, and never under the lock:
    PortAudio waits for the callback, which takes the lock.
    """

    def __init__(self, tts: TTS, device: str | int | None, events: SpeakerEvents, loop: asyncio.AbstractEventLoop,
                 levels_wanted: Callable[[], bool], echo: "EchoCanceller | None" = None) -> None:
        """Start the synthesis thread; the output stream opens with the first sound.

        Args:
            tts: the voice.
            device: the output device (voice.output_device; None = the system default).
            events: where to report (on the loop).
            loop: the event loop.
            levels_wanted: whether a HUD shows his voice's level (otherwise none is computed).
            echo: the echo canceller, given every block this machine's speakers play (not a browser's).
        """
        self.tts = tts
        self.device = device
        self.events = events
        self.loop = loop
        self.levels_wanted = levels_wanted
        self.echo = echo
        self._texts: queue.Queue[tuple[int, str, Callable[[float], None] | None] | None] = queue.Queue()
        self._chunks: collections.deque[_Chunk] = collections.deque()
        self._lock = threading.Lock()
        self._gen = 0  # bumped by stop(): anything of an older generation is dropped
        self._pending = 0  # texts queued or being synthesised, plus chunks queued or playing
        self._holds = 0
        self._stream: Any = None  # set and cleared on the device thread only
        self.output: RemoteOutput | None = None  # a browser's speakers in place of this machine's
        self.unavailable = False  # this machine's speakers couldn't be opened (no sound server): text only, until
        # a browser carries the voice or the device changes
        self._cut = False  # stop() asked the device thread to discard the device's buffer: play nothing until then
        self._device = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts-device")
        self._playing = False
        self._idle: asyncio.TimerHandle | None = None
        self._drained = asyncio.Event()
        self._drained.set()
        self._level_index = -1
        self._loop_thread = threading.get_ident()  # the runtime builds the speaker on the event loop's thread
        threading.Thread(target=self._synth_loop, daemon=True, name="tts-synth").start()

    # ------------------------------------------------------------------------------------------------ control
    @property
    def busy(self) -> bool:
        """Whether anything is queued, being synthesised or playing."""
        return self._pending > 0

    def say(self, text: str, *, first: Callable[[float], None] | None = None) -> None:
        """Queue a piece of text to speak (already cleaned for speech).

        Args:
            text: what to say.
            first: called on the event loop when the piece's first audio frame has gone to the device (or been sent
                to a browser), with the moment (monotonic seconds); never called for a piece that is dropped.
        """
        if not text.strip():
            return
        with self._lock:
            self._pending += 1
            gen = self._gen
        self._loop_call(self._drained.clear)
        self._texts.put((gen, text, first))

    def play(self, audio: "NDArray[np.float32]") -> None:
        """Queue ready audio (a chime) after whatever is queued."""
        with self._lock:
            self._pending += 1
            self._chunks.append(_Chunk(audio, ""))
        self._loop_call(self._drained.clear)
        self._on_device(self._open)

    def stop(self) -> None:
        """Stop speaking at once: queued text and audio are dropped and the device's buffer is discarded."""
        with self._lock:
            self._gen += 1
            self._chunks.clear()
            self._pending = 0
            self._cut = True
        self._on_device(self._restart)
        self._loop_call(self._settle)

    def hold(self) -> None:
        """Keep the output stream open (a conversation is under way: no reopening between replies)."""
        with self._lock:
            self._holds += 1
        self._loop_call(self._cancel_idle)
        self._on_device(self._open)

    def release(self) -> None:
        """Undo one `hold`; once nothing holds it and everything has played, the stream closes after a while."""
        with self._lock:
            self._holds = max(0, self._holds - 1)
        self._loop_call(self._schedule_idle)

    def set_device(self, device: str | int | None) -> None:
        """Play on another device from the next sound on."""
        self.device = device
        self.unavailable = False
        self._on_device(self._close)

    def set_output(self, output: "RemoteOutput | None") -> None:
        """Play through a browser's speakers (or this machine's again, with None) from the next sound on; what is
        playing now stops."""
        self.stop()
        self.output = output
        self.unavailable = False
        self._on_device(self._close)

    async def drained(self) -> None:
        """Wait until everything queued has been spoken (no polling: the player signals it)."""
        await self._drained.wait()

    def close(self) -> None:
        """Stop, close the stream and end the synthesis and device threads (blocking until the stream is closed)."""
        self.stop()
        self._texts.put(None)
        self._on_device(self._close)
        self._device.shutdown()

    # ------------------------------------------------------------------------------------------------ threads
    def _synth_loop(self) -> None:
        """The synthesis thread: text in, audio chunks (with their levels) out."""
        while True:
            item = self._texts.get()
            if item is None:
                return
            gen, text, first = item
            if gen != self._gen:
                continue
            try:
                audio = self.tts.synth(text)
            except Exception:
                log.exception("speech synthesis failed for %s", logged(text))
                self._finished_one(gen)
                continue
            chunk = _Chunk(audio, text, _level_frames(audio) if self.levels_wanted() else [], first=first)
            with self._lock:
                if gen != self._gen:
                    continue
                self._chunks.append(chunk)  # its pending count moves from the text to the chunk
            self._on_device(self._open)

    def _finished_one(self, gen: int) -> None:
        """One queued item is done (played or failed); signal when nothing is left."""
        with self._lock:
            if gen == self._gen:
                self._pending = max(0, self._pending - 1)
            left = self._pending
        if left == 0:
            self._loop_call(self._settle)

    def _on_device(self, fn: Callable[[], None]) -> None:
        """Run `fn` on the device thread, after what is already queued there (a closed speaker: dropped)."""
        with contextlib.suppress(RuntimeError):
            self._device.submit(fn)

    def _open(self) -> None:
        """Device thread: open and start the output stream unless it is open; if the speakers can't open, drop what
        was queued."""
        if self._stream is not None:
            return
        try:
            if self.output is not None:
                stream: Any = self.output.open(self._fill, busy=lambda: self._pending > 0)
            else:
                import sounddevice as sd

                stream = sd.OutputStream(samplerate=TTS_RATE, channels=1, dtype="float32", blocksize=BLOCK,
                                         latency=OUTPUT_LATENCY_S, device=self.device, callback=self._fill)
            stream.start()
            if self.echo is not None and self.output is None:
                self.echo.latency(output_s=float(stream.latency))
        except Exception as e:
            if self.output is None and not self.unavailable:
                log.warning("this machine's speakers couldn't be opened (%s): replies are shown, not spoken, until "
                            "a browser carries the voice", e, exc_info=log.isEnabledFor(logging.DEBUG))
            elif self.output is not None:
                log.exception("a browser's speakers couldn't be opened")
            self.unavailable = self.output is None
            with self._lock:
                self._chunks.clear()
                self._pending = 0
            self._loop_call(self._settle)
            return
        self.unavailable = False
        self._stream = stream
        self._loop_call(self._schedule_idle)  # held or busy: no close; released meanwhile: closes after a while

    def _restart(self) -> None:
        """Device thread: discard what the device still had buffered (an instant cut), then play on."""
        stream = self._stream
        try:
            if stream is not None:
                stream.abort()
            self._cut = False  # the callback has stopped: what was queued since stop() plays from its start
            if stream is not None:
                stream.start()
        except Exception as e:  # noqa: BLE001 (a device that went away: reopened with the next sound)
            log.debug("restarting the output stream after a stop failed: %s", e)
            self._cut = False
            self._close()

    def _close(self) -> None:
        """Device thread: close the output stream (it reopens with the next sound)."""
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.close()
            except Exception as e:  # noqa: BLE001 (closing a device that went away)
                log.debug("closing the output stream: %s", e)

    def _fill(self, outdata: Any, frames: int, _time: Any, _status: Any) -> None:
        """PortAudio's callback: copy queued audio into the device buffer, silence when there is none."""
        out = outdata[:, 0]
        filled = 0
        events: list[Callable[[], None]] = []
        with self._lock:
            gen = self._gen
            while filled < frames and self._chunks and not self._cut:
                chunk = self._chunks[0]
                if not chunk.started:
                    chunk.started = True
                    self._level_index = -1
                    if chunk.first is not None:
                        events.append(functools.partial(chunk.first, time.monotonic()))
                    if chunk.text:
                        events.append(lambda text=chunk.text: self.events.on_caption(text))
                    if not self._playing:
                        self._playing = True
                        events.append(lambda: self.events.on_playing(True))
                take = min(frames - filled, len(chunk.audio) - chunk.pos)
                out[filled:filled + take] = chunk.audio[chunk.pos:chunk.pos + take]
                chunk.pos += take
                filled += take
                if chunk.levels:
                    index = min(len(chunk.levels) - 1, int(chunk.pos / TTS_RATE * LEVELS_HZ))
                    if index != self._level_index:
                        self._level_index = index
                        value, bands = chunk.levels[index]
                        events.append(lambda v=value, b=bands: self.events.on_voice_level(v, b))
                if chunk.pos >= len(chunk.audio):
                    self._chunks.popleft()
                    events.append(lambda: self._finished_one(gen))
        if filled < frames:
            out[filled:] = 0.0
        if self.echo is not None and self.output is None:  # this machine's speakers, silence included
            self.echo.render(out, TTS_RATE)
        for event in events:
            self.loop.call_soon_threadsafe(event)

    # ------------------------------------------------------------------------------------------------ on the loop
    def _settle(self) -> None:
        """On the loop: when everything has played, say so and let an unheld stream close after a while."""
        if self._pending:
            return
        if self._playing:
            self._playing = False
            self.events.on_playing(False)
            self.events.on_voice_level(0.0, [])
        self._drained.set()
        self._schedule_idle()

    def _schedule_idle(self) -> None:
        """On the loop: close the stream STREAM_IDLE_CLOSE_S after the last sound, unless held or busy."""
        self._cancel_idle()
        if self._holds == 0 and not self._pending and self._stream is not None:
            self._idle = self.loop.call_later(STREAM_IDLE_CLOSE_S, self._close_if_idle)

    def _cancel_idle(self) -> None:
        """On the loop: forget a scheduled close."""
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None

    def _close_if_idle(self) -> None:
        """On the loop: close the stream if it is still unheld and silent."""
        self._idle = None
        if self._holds == 0 and not self._pending:
            self._on_device(self._close)

    def _loop_call(self, fn: Callable[[], None]) -> None:
        """Run `fn` on the loop: at once when called there, else handed over (a closed loop: Orion is stopping).

        Never call it while holding the speaker's lock: `fn` may run immediately and report to the event handlers.
        """
        if threading.get_ident() == self._loop_thread:
            fn()
            return
        with contextlib.suppress(RuntimeError):
            self.loop.call_soon_threadsafe(fn)


class RemoteOutput:
    """Speakers that aren't this machine's: a browser's, reached over the HUD's connection.

    ``open`` gives what the speaker opens in place of PortAudio's output stream: a clock thread pulls 40 ms blocks
    from the speaker's callback at the pace of real time, as a sound card would, and sends each one as 16-bit PCM in a
    binary frame (``AUDIO_FRAME``, the generation, the samples); the browser plays them through a small buffer. The
    first blocks of each sound go ``REMOTE_LEAD_BLOCKS`` ahead to fill that buffer, and silence between sounds isn't
    sent. An instant stop (``abort``) moves to a new generation and tells the browser, which drops what it holds.
    """

    def __init__(self, send: Callable[[bytes], None], flush: Callable[[int], None]) -> None:
        """Reach a browser.

        Args:
            send: sends one binary frame to it (thread-safe).
            flush: tells it to drop what it holds, before the frames of this generation (thread-safe).
        """
        self.send = send
        self.flush = flush
        self.gen = 0

    def open(self, fill: Callable[[Any, int, Any, Any], None], *, busy: Callable[[], bool]) -> "_RemoteStream":
        """A stream that plays what ``fill`` gives, as long as ``busy`` says there is something to play.

        Args:
            fill: the speaker's callback (PortAudio's signature).
            busy: whether anything is queued or playing.

        Returns:
            The stream, not started.
        """
        return _RemoteStream(self, fill, busy)

    def frame(self, block: "NDArray[np.float32]") -> bytes:
        """One block as a binary frame of this generation."""
        pcm = np.rint(np.clip(block, -1.0, 1.0) * 32767).astype("<i2").tobytes()
        return bytes((AUDIO_FRAME,)) + struct.pack("<I", self.gen) + pcm


class _RemoteStream:
    """PortAudio's output stream, stood in for by a clock thread (see ``RemoteOutput``)."""

    def __init__(self, output: RemoteOutput, fill: Callable[[Any, int, Any, Any], None],
                 busy: Callable[[], bool]) -> None:
        self.output = output
        self._fill = fill
        self._busy = busy
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.closed = False

    def start(self) -> None:
        """Start the clock."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self.closed = False
        self._thread = threading.Thread(target=self._clock, daemon=True, name="tts-remote")
        self._thread.start()

    def abort(self) -> None:
        """Stop at once: the browser drops what it holds (a new generation); ``start`` plays on."""
        self._halt()
        self.output.gen += 1
        self.output.flush(self.output.gen)

    def close(self) -> None:
        """Stop the clock."""
        self._halt()
        self.closed = True

    def _halt(self) -> None:
        """Stop the clock thread and wait for it (never from itself)."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._thread = None

    def _clock(self) -> None:
        """Pull a block every 40 ms (the first ones of a sound at once), and send those that carry a sound."""
        block = np.zeros((BLOCK, 1), dtype=np.float32)
        period = BLOCK / TTS_RATE
        due = time.monotonic()
        lead = 0
        while not self._stop.is_set():
            playing = self._busy()
            if not playing:
                lead = 0  # the next sound fills the browser's buffer again first
                if self._stop.wait(period):
                    return
                due = time.monotonic()
                continue
            block[:] = 0.0
            try:
                self._fill(block, BLOCK, None, None)
                self.output.send(self.output.frame(block[:, 0]))
            except Exception:  # a browser that went away: the speaker carries on, unheard
                log.debug("a block for the browser was lost", exc_info=True)
            if lead < REMOTE_LEAD_BLOCKS:
                lead += 1
                continue
            due += period
            if self._stop.wait(max(0.0, due - time.monotonic())):
                return


def _level_frames(audio: "NDArray[np.float32]") -> list[tuple[float, list[float]]]:
    """His voice's level LEVELS_HZ times a second through `audio`, worked out before playing (no FFT in callbacks)."""
    step = max(1, round(TTS_RATE / LEVELS_HZ))
    return [levels(audio[i:i + step], TTS_RATE, LEVEL_GAIN) for i in range(0, len(audio), step)]


class SpeechStream:
    """One reply's speech: deltas through the splitter into the speaker, and what was said remembered."""

    __slots__ = ("_clock", "_said", "_started", "speaker", "splitter")

    def __init__(self, speaker: Speaker | None, splitter: SentenceSplitter, clock: "TurnClock | None" = None) -> None:
        """Start a reply.

        Args:
            speaker: where to speak; None when replies are text only (what was "said" is still kept).
            splitter: how to cut the reply.
            clock: the turn's clock: told when the first piece goes to the speaker and when it first sounds.
        """
        self.speaker = speaker
        self.splitter = splitter
        self._clock = clock
        self._started = False
        self._said: list[str] = []

    @property
    def said(self) -> str:
        """Everything handed to the speaker so far, as one text."""
        return " ".join(self._said)

    def feed(self, delta: str) -> None:
        """Add streamed reply text; finished pieces are spoken."""
        self._speak(self.splitter.feed(delta))

    def boundary(self) -> None:
        """A tool round starts: speak what is pending before the tool runs."""
        self._speak(self.splitter.boundary())

    def close(self) -> None:
        """The reply is complete: speak the rest."""
        self._speak(self.splitter.flush())

    def _speak(self, pieces: list[str]) -> None:
        """Clean and speak pieces, remembering what was said."""
        for piece in pieces:
            text = clean_for_speech(piece)
            if not text:
                continue
            self._said.append(text)
            if self.speaker is not None:
                first = None
                if self._clock is not None and not self._started:
                    self._started = True
                    self._clock.ready()
                    first = self._clock.sound
                self.speaker.say(text, first=first)
