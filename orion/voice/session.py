"""The voice conversation: hearing his name, taking the request, speaking the reply, keeping the conversation open.

``VoiceSession`` owns the voice parts (the listener, the speech models and their scheduler, the speaker) and only
sequences decisions that live, tested, in ``wake.py`` and ``listener.py``:

1. Standby speech is checked for his name in two stages: the small candidate model on the CPU first (most room talk
   stops there), then the main model confirms and ``wake.address`` decides whether he was spoken to. Only the newest
   window waits; older ones are dropped.
2. Once addressed, the stretch of speech becomes the recording of the request (``Listener.adopt``). At its first pause
   a speculative transcript may end it early, when the words already sound finished.
3. At the end of the request the cheapest transcript wins: the name check's (when it covers everything said), the
   speculative one, or a new pass. ``wake.classify_utterance`` then says what to do: a command for the turn runner, a
   filler (keep listening), a dismissal or a stop (stand by), his name again (listen again).
4. Replies stream through ``SpeechStream`` objects. After a voice reply has been spoken, the follow-up window keeps
   listening without his name for ``voice.follow_up_seconds``; every reply opens a fresh window.
5. Barge-in (opt-in): speech heard while he thinks or speaks goes through the same two stages, then
   ``wake.barge_action``.

The listener and the speaker call in from their own threads; every callback hops to the event loop, so the session's
own state needs no locks. It implements the turn runner's ``SpeechPort``.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol, TypedDict

from orion.base import JSON, AssistantState, EventBus, TurnResult, TurnSource, logged
from orion.inference import cpu_threads, release_memory_async
from orion.timing import Heard
from orion.voice import MIC_RATE
from orion.voice.aec import EchoCanceller, EchoError
from orion.voice.listener import Listener, PushedInput, Recording, Window
from orion.voice.stt import STT, Candidate, SttInfo, SttScheduler, Transcript
from orion.voice.tts import (
    TTS,
    Chime,
    ChunkParams,
    RemoteOutput,
    SentenceSplitter,
    Speaker,
    SpeechStream,
    chime,
    clean_for_speech,
)
from orion.voice.wake import (
    CheckInfo,
    OwnWords,
    Phrases,
    Purpose,
    RecordInfo,
    address,
    answer_of,
    barge_action,
    can_reuse,
    candidate_hit,
    classify_utterance,
    clean_transcript,
    is_repetitive,
    utterance_complete,
)

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

    from orion.config import Config
    from orion.timing import TurnClock

log = logging.getLogger("orion.voice")

ECHO_SETTLE_S: Final = 0.25  # after his reply, the room's echo dies down this long before he listens again
FOLLOW_UP_MIN_S: Final = 1.0  # a follow-up window with less left than this closes instead
ECHO_MIN_WORDS: Final = 4  # this many of his own words coming back through the speakers count as an echo


class TurnPort(Protocol):
    """What the voice session needs from the turn runner (``brain.TurnRunner`` implements it)."""

    def submit(self, text: str, source: TurnSource, *, device: str = "",
               heard: Heard | None = None) -> "asyncio.Task[TurnResult]":
        """Start a turn for a request (``device``: the browser carrying the voice, "" for this machine; ``heard``:
        when it ended and was heard, for the turn's timings)."""

    def interrupt(self) -> None:
        """Stop the speech and cancel the turn under way."""

    def set_state(self, state: AssistantState, **extra: JSON) -> None:
        """Show what he is doing."""

    def on_wake(self) -> None:
        """He was woken: pause the memory review, get the brain ready."""

    @property
    def busy(self) -> bool:
        """Whether a turn is running."""

    @property
    def loading(self) -> str:
        """The brain being switched to ("" when none is loading)."""


class VoiceInfo(TypedDict):
    """The voice as the HUD shows it."""

    stt: SttInfo | None
    candidate: str  # the stage-1 model ("" when off)
    engine: Literal["name", "openwakeword"]


class VoiceSession:
    """The voice conversation (see the module docstring). Build it, and call it, on the event loop's thread."""

    def __init__(self, cfg: "Config", bus: EventBus, models_dir: Path, *, microphone: bool,
                 levels_wanted: Callable[[], bool], on_idle: Callable[[], None] | None = None) -> None:
        """Prepare; nothing loads until ``load_tts`` and ``load_ears``.

        Args:
            cfg: the live settings.
            bus: the HUD's event bus.
            models_dir: the models folder.
            microphone: whether to use the microphone at all (``orion run --no-voice`` says no).
            levels_wanted: whether a HUD shows the levels (otherwise none are computed).
            on_idle: called when a conversation closes (the runtime frees memory, the review may start).
        """
        self.cfg = cfg
        self.bus = bus
        self.models_dir = models_dir
        self.microphone = microphone
        self.levels_wanted = levels_wanted
        self.on_idle = on_idle or (lambda: None)
        self.loop = asyncio.get_running_loop()
        self.turns: TurnPort | None = None
        self.tts: TTS | None = None
        self.speaker: Speaker | None = None
        self.stt: STT | None = None
        self.candidate: Candidate | None = None
        self.scheduler: SttScheduler | None = None
        self.listener: Listener | None = None
        self.muted = False
        self._tasks: set[asyncio.Task[Any]] = set()
        self._next_window: Window | None = None
        self._checking: asyncio.Task[None] | None = None
        self._check: CheckInfo | None = None  # the name check that woke him (its transcript may be reused)
        self._speculative: tuple[int, int, Transcript] | None = None  # (recording, up to frame, transcript)
        self._follow_until = 0.0
        self._held = False  # the speaker is held open for a conversation
        self._stream: SpeechStream | None = None  # the reply being spoken
        self._last_reply = ""
        self._listening = False  # the microphone is open
        self._answer: asyncio.Future[bool | None] | None = None  # a yes/no question waiting for a spoken answer
        self.holder = ""  # the device carrying the voice ("" = this machine's microphone and speakers)
        self._remote_mic: PushedInput | None = None
        self.echo = EchoCanceller(on_failure=self._echo_stopped)

    # ------------------------------------------------------------------------------------------------ lifecycle
    def attach(self, turns: TurnPort) -> None:
        """Connect the turn runner (it exists after the session, which it speaks through)."""
        self.turns = turns

    async def load_tts(self) -> None:
        """Load the voice (Kokoro) in a worker thread and open the speaker.

        Raises:
            Exception: the voice files are missing or broken (``orion setup`` fetches them).
        """
        cfg = self.cfg
        tts = TTS(cfg.tts, self.models_dir, cfg.models.kokoro_model, cfg.models.kokoro_voices,
                  cpu_threads(cfg.performance.cpu_threads))
        await asyncio.to_thread(tts.load)
        self.tts = tts
        await self.reload_echo()
        self.speaker = Speaker(tts, cfg.voice.output_device, self, self.loop, self.levels_wanted, self.echo)

    async def load_ears(self) -> None:
        """Load speech recognition (and the stage-1 model) in worker threads; nothing without a microphone or with
        ``voice.enabled`` off (about 1 GB of graphics memory saved).

        Raises:
            Exception: the main model could not load.
        """
        await self._load_ears(force=False)

    async def _load_ears(self, *, force: bool) -> None:
        """``load_ears``; ``force`` loads them for a browser's microphone even when this machine's isn't used."""
        if (not self.microphone and not force) or not self.cfg.voice.enabled or self.stt is not None:
            return
        cfg = self.cfg
        threads = cpu_threads(cfg.performance.cpu_threads)
        stt = STT(cfg.stt, self.models_dir, threads)
        await asyncio.to_thread(stt.load)
        candidate = None
        if cfg.wake.candidate_model:
            candidate = Candidate(cfg.stt, cfg.wake.candidate_model, self.models_dir)
            if not await asyncio.to_thread(candidate.load):
                log.info("no stage-1 model (%s): the main model checks every window", cfg.wake.candidate_model)
                candidate = None
        self.stt, self.candidate = stt, candidate
        self.scheduler = SttScheduler(stt, candidate, self.loop)
        self.listener = Listener(cfg.voice, cfg.wake, self.models_dir, self, levels_wanted=self.levels_wanted,
                                 is_speaking=lambda: self.speaker is not None and self.speaker.busy, echo=self.echo)

    # ------------------------------------------------------------------------------------------------ devices
    async def take_voice(self, device: str, *, send: "Callable[[dict[str, JSON]], None]",
                         send_audio: "Callable[[bytes], None]", name: str) -> None:
        """A browser carries the voice from now on: its microphone and speakers in place of this machine's (another
        browser that carried it lets go). Speech recognition loads first when it isn't loaded (on a machine without
        a microphone of its own).

        Args:
            device: the browser's HUD (its surface id).
            send: sends an event to it alone (the speaker's stops).
            send_audio: sends a binary frame of his voice to it alone.
            name: the device's name for people.

        Raises:
            RuntimeError: voice input is off in the settings, or speech recognition couldn't load.
        """
        if not self.cfg.voice.enabled:
            raise RuntimeError("voice input is off (Settings, Voice)")
        await self._load_ears(force=True)
        listener = self.listener
        if listener is None:
            raise RuntimeError("speech recognition isn't loaded")
        if self.holder and self.holder != device:
            self.bus.publish("toast", title="VOICE", text=f"{name} has the voice now.")
        mic = PushedInput()
        self.holder, self._remote_mic = device, mic
        if self._listening:
            listener.use_input(mic)
        else:
            listener.remote = mic
            self.start_listening()
        if self.speaker is not None:
            self.speaker.set_output(RemoteOutput(
                send=send_audio, flush=lambda gen: self.loop.call_soon_threadsafe(
                    send, {"type": "speaker", "gen": gen, "ts": time.time()})))
        log.info("the voice goes through %s", name)
        self.bus.publish("voice_holder", device=device, name=name)

    def release_voice(self, device: str) -> None:
        """A browser lets go of the voice (or went away): this machine's microphone and speakers again, when it
        has them and may use them.

        Args:
            device: the browser's HUD; nothing happens unless it carries the voice.
        """
        if not device or device != self.holder:
            return
        self.holder, self._remote_mic = "", None
        if self.conversation_open:
            self._stand_by()
        listener = self.listener
        if listener is not None:
            try:
                listener.use_input(None) if self.microphone else listener.close_input()
            except Exception as e:  # noqa: BLE001 (no microphone here: he listens to nobody until a browser asks)
                log.info("no microphone of this machine to go back to: %s", e)
                listener.close_input()
        if self.speaker is not None:
            self.speaker.set_output(None)
        log.info("the voice is back on this machine")
        self.bus.publish("voice_holder", device="", name="")

    def feed(self, device: str, samples: "NDArray[np.float32]") -> None:
        """Samples from a browser's microphone (dropped unless it carries the voice).

        Args:
            device: the browser's HUD.
            samples: 16 kHz mono float32 samples.
        """
        if device == self.holder and self._remote_mic is not None:
            self._remote_mic.push(samples)

    async def transcribe(self, recording: bytes) -> str:
        """A recording in words (a voice message from Discord: Ogg Opus, or anything PyAV reads), with the main
        speech model; it loads first when it isn't loaded.

        Args:
            recording: the file's bytes.

        Returns:
            The words ("" for silence or noise).

        Raises:
            RuntimeError: voice input is off, or speech recognition couldn't load.
            Exception: the recording couldn't be decoded.
        """
        import io

        from faster_whisper.audio import decode_audio

        if not self.cfg.voice.enabled:
            raise RuntimeError("voice input is off (Settings, Voice)")
        samples = await asyncio.to_thread(decode_audio, io.BytesIO(recording), sampling_rate=MIC_RATE)
        await self._load_ears(force=True)
        if self.scheduler is None:
            raise RuntimeError("speech recognition isn't loaded")
        heard = await self.scheduler.run(samples.astype("float32", copy=False), "command")
        text = clean_transcript(heard.text, self._phrases())
        return "" if is_repetitive(text) else text

    def talk(self, device: str) -> None:
        """The browser carrying the voice asks to talk now, without his name (its talk button): stop what he says
        and record the request."""
        turns, listener = self.turns, self.listener
        if device != self.holder or turns is None or listener is None or self.muted:
            return
        if turns.busy or self.speaking:
            turns.interrupt()
            self.stop_speaking()
        self._woken()
        listener.listen(self.cfg.voice.command_wait_s, "command", preroll=True)

    async def reload_echo(self) -> None:
        """Start, change or stop the echo canceller as the settings now say (it needs this machine's microphone). If
        it can't start, he says so and listens without it."""
        voice = self.cfg.voice
        try:
            await asyncio.to_thread(self.echo.configure, voice.echo_cancellation and voice.enabled and self.microphone,
                                    voice.noise_suppression)
        except EchoError as e:
            self._echo_failed(str(e))

    def _echo_stopped(self, why: str) -> None:
        """The echo canceller failed while running, on the audio thread or the listener's: say so on the loop."""
        self.loop.call_soon_threadsafe(self._echo_failed, why)

    def _echo_failed(self, why: str) -> None:
        """The echo canceller can't run: say so (he listens without it, and can be interrupted only with headphones)."""
        log.warning("listening without echo cancellation: %s", why)
        self.bus.publish("toast", title="EARS", text=f"No echo cancellation: {why}")

    async def reload_ears(self) -> None:
        """Load the speech model the settings now name (``Apply.STT``), keeping the listener."""
        if self.stt is not None:
            await asyncio.to_thread(self.stt.load)

    def start_listening(self) -> bool:
        """Open the microphone.

        Returns:
            True when listening; False when there is no microphone to open (speech recognition is unloaded again).
        """
        if self.listener is None:
            return False
        try:
            self.listener.start()
        except Exception as e:
            log.warning("the microphone could not be opened: %s; replies stay text and speech", e,
                        exc_info=log.isEnabledFor(logging.DEBUG))
            self._unload_ears()
            return False
        self._listening = True
        if self.muted:
            self.listener.set_muted(True)
        return True

    async def close(self) -> None:
        """Close the microphone, the speaker and the models' threads, and cancel background work."""
        for task in [*self._tasks, *([self._checking] if self._checking else [])]:
            task.cancel()
        if self.listener is not None:
            self.listener.close()
        if self.scheduler is not None:
            self.scheduler.close()
        if self.speaker is not None:
            self.speaker.close()
        self.echo.configure(echo=False, noise=False)

    # ------------------------------------------------------------------------------------------------ SpeechPort
    def reply_stream(self, turn: str, *, spoken: bool, clock: "TurnClock | None" = None) -> SpeechStream:
        """A reply's speech: deltas in, pieces spoken as they are ready (text only when ``spoken`` is False).

        Args:
            turn: the turn (for the log).
            spoken: whether to speak it (VOICE OFF, or no voice loaded: not).
            clock: the turn's clock, told when the first piece is ready and when it first sounds.

        Returns:
            The stream; the session reads what it said for its echo filter.
        """
        cfg = self.cfg
        splitter = SentenceSplitter(ChunkParams(first_min_chars=cfg.tts.first_chunk_min_chars,
                                                min_chars=cfg.tts.min_sentence_chars, code_note=cfg.prompts.code_note))
        speaker = self.speaker if spoken and self.speaker is not None and not self.speaker.unavailable else None
        stream = SpeechStream(speaker, splitter, clock)
        self._stream = stream
        log.debug("reply %s %s", turn, "spoken" if spoken and self.speaker else "shown")
        return stream

    async def reply_done(self, turn: str, source: TurnSource) -> None:
        """After a reply: wait until it has been spoken, then keep listening (a voice request) or rest.

        Args:
            turn: the turn.
            source: where its request came from.
        """
        stream, self._stream = self._stream, None
        if stream is not None and stream.said:
            self._last_reply = stream.said
        if self.speaker is not None:
            await self.speaker.drained()
        turns = self.turns
        if turns is None or turns.busy:
            return  # a new request started meanwhile
        if source == "voice" and self.listener is not None and self.cfg.voice.follow_up_seconds > 0:
            await asyncio.sleep(ECHO_SETTLE_S)
            if not turns.busy:
                self._open_window(float(self.cfg.voice.follow_up_seconds))
            return
        if self.listener is not None and self.listener.mode == "busy":
            self.listener.sleep()
        turns.set_state("idle")
        self._release()
        self.on_idle()

    def announce(self, text: str) -> None:
        """Speak an announcement (a timer, a reminder) after a short chime.

        Args:
            text: what to say.
        """
        self._last_reply = text
        if self.speaker is None or self.speaker.unavailable:
            return
        self.speaker.play(chime("done"))
        self.speaker.say(clean_for_speech(text))

    async def ask_aloud(self, question: str, timeout_s: float) -> bool | None:
        """Ask a yes/no question aloud and listen for the answer (``phrases.affirm`` / ``phrases.deny``), in the middle of a
        turn: the question is said after what he is saying now, and the turn goes on after the answer.

        Args:
            question: what to ask.
            timeout_s: how long to wait for speech to begin once the question has been said.

        Returns:
            True for yes, False for no, None when there was no clear answer by voice (no microphone, muted, silence,
            or something else was said). Cancelling it (the HUD answered first) stops listening for the answer.
        """
        listener, speaker, turns = self.listener, self.speaker, self.turns
        if listener is None or speaker is None or turns is None or self.muted or not self._listening:
            return None
        answer: asyncio.Future[bool | None] = self.loop.create_future()
        self._answer = answer
        try:
            self._last_reply = question
            speaker.say(clean_for_speech(question))
            await speaker.drained()
            turns.set_state("listening")
            self.bus.publish("listen", purpose="answer", seconds=round(timeout_s, 1))
            listener.listen(timeout_s, "answer")
            return await answer
        finally:
            self._answer = None
            if listener.mode == "record":
                listener.busy()  # answered elsewhere, or cancelled: stop recording the answer
            if turns.busy:
                turns.set_state("thinking")

    def chime(self, kind: Chime) -> None:
        """Play a chime (the wake and standby ones only with ``voice.chime``).

        Args:
            kind: "wake", "done", "error" or "standby".
        """
        if self.speaker is not None and not self.speaker.unavailable and (self.cfg.voice.chime or
                                                                           kind in ("done", "error")):
            self.speaker.play(chime(kind))

    def stop_speaking(self) -> None:
        """Stop speaking at once."""
        if self.speaker is not None:
            self.speaker.stop()

    @property
    def speaking(self) -> bool:
        """Whether he is making sound (or about to)."""
        return self.speaker is not None and self.speaker.busy

    # ------------------------------------------------------------------------------------------------ controls
    def stand_by(self) -> None:
        """The HUD's STOP: end a voice conversation and wait for his name again (the turn runner was interrupted;
        nothing happens while he is already waiting for his name)."""
        if self.listener is not None and self.listener.mode != "sleep":
            self._stand_by()

    def set_muted(self, muted: bool) -> None:
        """MIC MUTED: the microphone closes (the desktop shows it unused); unmuting opens it again (a microphone that
        can't be opened again, unplugged meanwhile, stays muted)."""
        if self.listener is not None:
            self.listener.set_muted(muted)
        self.muted = self.listener.muted if self.listener is not None else muted
        if muted and self.turns is not None and not self.turns.busy:
            self.turns.set_state("idle")

    def set_output_device(self, device: str | int | None) -> None:
        """Speak through another device from the next sound on."""
        if self.speaker is not None:
            self.speaker.set_device(device)

    def set_vocabulary(self, words: Iterable[str]) -> None:
        """Names Whisper should expect (the user's name, home city, trusted concepts)."""
        if self.stt is not None:
            self.stt.set_vocabulary(words)

    async def preview(self, text: str, voice: str, speed: float | None) -> bytes:
        """A voice sample for the settings page, as WAV bytes.

        Raises:
            RuntimeError: the voice isn't loaded yet.
        """
        if self.tts is None:
            raise RuntimeError("the voice is still loading")
        return await asyncio.to_thread(self.tts.synth_wav, text, voice, speed)

    @property
    def info(self) -> VoiceInfo:
        """The voice as the HUD shows it."""
        return VoiceInfo(stt=self.stt.info if self.stt is not None else None,
                         candidate=self.cfg.wake.candidate_model if self.candidate is not None else "",
                         engine="openwakeword" if self.cfg.voice.wake_model else "name")

    @property
    def conversation_open(self) -> bool:
        """Whether a voice conversation is under way (from his name until he stands by again)."""
        return self._held

    # ------------------------------------------------------------------------------------------------ ListenerEvents
    def on_window(self, w: Window) -> None:
        """Standby (or busy) speech to check for his name (listener thread)."""
        self.loop.call_soon_threadsafe(self._queue_window, w)

    def on_pause(self, rec_id: int, audio: "NDArray[np.float32]", upto_frame: int) -> None:
        """A recording paused for the first time (listener thread)."""
        self.loop.call_soon_threadsafe(self._spawn, self._speculate(rec_id, audio, upto_frame))

    def on_utterance(self, rec: Recording) -> None:
        """A recording ended (listener thread, or the caller of ``finish``)."""
        self.loop.call_soon_threadsafe(self._spawn, self._utterance(rec))

    def on_timeout(self, purpose: Purpose) -> None:
        """Nobody spoke within the wait (listener thread)."""
        self.loop.call_soon_threadsafe(self._timed_out, purpose)

    def on_hearing(self, active: bool) -> None:
        """Standby speech started or ended: the HUD's "Hearing you..." (thread-safe publish)."""
        self.bus.publish("hearing", active=active)

    def on_level(self, value: float, bands: list[float]) -> None:
        """The microphone's level for the visualiser (thread-safe publish)."""
        self.bus.publish("level", source="mic", value=value, bands=bands)

    def on_wake_word(self) -> None:
        """The optional wake-word model fired; the listener is already recording (listener thread)."""
        self.loop.call_soon_threadsafe(self._woken_by_model)

    # ------------------------------------------------------------------------------------------------ SpeakerEvents
    def on_playing(self, playing: bool) -> None:
        """He started or stopped making sound (on the loop)."""
        turns = self.turns
        if turns is None:
            return
        if playing:
            turns.set_state("speaking")
        elif turns.busy:
            turns.set_state("thinking")
        else:
            turns.set_state("listening" if self.conversation_open else "idle")

    def on_caption(self, text: str) -> None:
        """A spoken piece starts playing (on the loop)."""
        self.bus.publish("caption", text=text)

    def on_voice_level(self, value: float, bands: list[float]) -> None:
        """His voice's level for the visualiser (on the loop)."""
        self.bus.publish("level", source="voice", value=value, bands=bands)

    # ------------------------------------------------------------------------------------------------ the pipelines
    def _queue_window(self, w: Window) -> None:
        """Keep only the newest window waiting; start the checker when it isn't running."""
        self._next_window = w
        if self._checking is None:
            self._checking = self.loop.create_task(self._check_windows())

    async def _check_windows(self) -> None:
        """Check waiting windows one at a time, newest first."""
        try:
            while (w := self._next_window) is not None:
                self._next_window = None
                await (self._check_barge(w) if w.busy else self._check_standby(w))
        finally:
            self._checking = None

    async def _heard(self, w: Window) -> Transcript | None:
        """Both stages of a name check: the candidate (when loaded) must hear something like his name, then the main
        model's transcript, None when either says no (or the check was dropped)."""
        scheduler = self.scheduler
        if scheduler is None:
            return None
        try:
            spotted = await scheduler.spot(w.audio)
            if spotted is not None and not candidate_hit(spotted.text):
                log.debug("name check, stage 1 heard no name: %s", logged(spotted.text))
                return None
            heard = await scheduler.run(w.audio, "confirm")
        except asyncio.CancelledError:
            if (task := asyncio.current_task()) is not None and task.cancelling():
                raise
            return None  # dropped: a recording started
        except Exception:
            log.warning("a name check failed", exc_info=True)
            return None
        if is_repetitive(heard.text):
            return None
        return heard

    async def _check_standby(self, w: Window) -> None:
        """Stage 1 and 2 on standby speech; when he was addressed, take the request."""
        if not self._standby() or not self.cfg.voice.name_wake:
            return
        heard = await self._heard(w)
        ph = self._phrases()
        text = clean_transcript(heard.text, ph) if heard is not None else ""
        if heard is None or not text or not self._standby():
            return
        wake = self.cfg.wake
        verdict = address(text, ph, wake.lead_words, max_edits=wake.name_max_edits)
        listener = self.listener
        if not verdict.addressed or listener is None:
            log.debug("name check, not said to him: %s", logged(text))
            return
        log.info("heard his name: %s", logged(text))
        self._check = CheckInfo(w.seg_id, w.end_frame, text, w.truncated, heard.hit_token_cap)
        self._speculative = None
        rec_id = listener.adopt(w.seg_id, self.cfg.voice.command_wait_s, awaiting=verdict.name_only)
        self._woken()
        if rec_id is not None:
            return  # what is being said is recorded until the pause, then answered
        # The stretch ended and someone talked on before the check finished: take the request from what was heard.
        if verdict.name_only:
            listener.listen(self.cfg.voice.command_wait_s, "command", seed=True)
        else:
            self._command(verdict.request)

    async def _check_barge(self, w: Window) -> None:
        """Speech heard while he thinks or speaks: his name stops him (``voice.barge_in``)."""
        turns, listener = self.turns, self.listener
        if not self.cfg.voice.barge_in or turns is None or listener is None or listener.mode != "busy":
            return
        heard = await self._heard(w)
        if heard is None or listener.mode != "busy":
            return
        ph, wake = self._phrases(), self.cfg.wake
        action = barge_action(clean_transcript(heard.text, ph), self._own(), loading=bool(turns.loading), ph=ph,
                              lead_words=wake.lead_words, max_edits=wake.name_max_edits)
        if action == "ignore":
            return
        log.info("interrupted by name: %s (%s)", logged(heard.text), action)
        turns.interrupt()
        self.stop_speaking()
        if action == "stop":
            self._stand_by()
            return
        self._woken()
        listener.listen(self.cfg.voice.command_wait_s, "command", preroll=True)

    async def _speculate(self, rec_id: int, audio: "NDArray[np.float32]", upto_frame: int) -> None:
        """At a recording's first pause, transcribe what was said so far; end it early when it sounds finished."""
        scheduler, listener = self.scheduler, self.listener
        if scheduler is None or listener is None or not self._speculation_on():
            return
        try:
            heard = await scheduler.run(audio, "command")
        except asyncio.CancelledError:
            if (task := asyncio.current_task()) is not None and task.cancelling():
                raise
            return
        except Exception:
            log.debug("the speculative transcript failed", exc_info=True)
            return
        self._speculative = (rec_id, upto_frame, heard)
        complete = utterance_complete(clean_transcript(heard.text, self._phrases()), self._phrases())
        if complete is True and listener.finish(rec_id, upto_frame):
            return  # on_utterance follows, and this transcript covers it
        listener.hint(rec_id, complete)

    async def _utterance(self, rec: Recording) -> None:
        """A request ended: pick the cheapest transcript, then do what it asks."""
        scheduler, turns, listener = self.scheduler, self.turns, self.listener
        if scheduler is None or turns is None or listener is None:
            return
        scheduler.drop_checks()
        turns.set_state("thinking")
        if rec.purpose == "answer":
            await self._answered(rec)
            return
        text = self._reused(rec)
        if text is None:
            try:
                text = (await scheduler.run(rec.audio, "command")).text
            except Exception as e:
                log.exception("speech recognition failed")
                self.bus.publish("toast", title="EARS", text=f"Speech recognition failed: {e}")
                self._stand_by()
                return
        heard = Heard(said=rec.last_speech_at or time.monotonic(), heard=time.monotonic())
        ph, wake = self._phrases(), self.cfg.wake
        cleaned = clean_transcript(text, ph)
        if is_repetitive(cleaned):
            cleaned = ""
        action, said = classify_utterance(cleaned, rec.purpose, self._own(), ph, ECHO_MIN_WORDS,
                                          lead_words=wake.lead_words, max_edits=wake.name_max_edits)
        log.info("heard (%s): %s -> %s", rec.purpose, logged(cleaned), action)
        wait = self.cfg.voice.command_wait_s
        if action == "command":
            self._command(said, heard)
        elif action == "filler":
            self.bus.publish("transcript", text=said, purpose="filler")
            turns.set_state("listening")
            listener.listen(wait, rec.purpose, preroll=True)
        elif action == "wake_again":
            self._woken()
            listener.listen(wait, "command", preroll=True)
        elif action == "keep_listening":
            if not self._keep_listening():
                self._stand_by()
        elif action == "stop":
            turns.interrupt()
            self.stop_speaking()
            self.bus.publish("transcript", text=said, purpose="stop")
            self._stand_by()
        else:  # "end": a dismissal, or nothing asked after his name
            if said:
                self.bus.publish("transcript", text=said, purpose="stop")
            self._stand_by()

    def _reused(self, rec: Recording) -> str | None:
        """A transcript already taken that covers the whole request: the name check's, or the speculative one."""
        check, self._check = self._check, None
        if check is not None and can_reuse(check, RecordInfo(rec.seg_id, rec.last_speech_frame)):
            return check.text
        speculative, self._speculative = self._speculative, None
        if speculative is not None:
            rec_id, upto, heard = speculative
            if rec_id == rec.rec_id and upto >= rec.last_speech_frame and not heard.hit_token_cap:
                return heard.text
        return None

    def _command(self, text: str, heard: Heard | None = None) -> None:
        """Hand a request to the turn runner (the listener stays busy until the reply is done).

        Args:
            text: the request.
            heard: when it ended and was heard; None when the name check took it straight from a stretch of speech
                (its turn then counts from now).
        """
        if self.turns is None or not text:
            self._stand_by()
            return
        if self.listener is not None:
            self.listener.busy()
        task = self.turns.submit(text, "voice", device=self.holder,  # its reply_done opens the follow-up window
                                 heard=heard)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _answered(self, rec: Recording) -> None:
        """A spoken answer to a yes/no question ended: say what it was to whoever asked (the listener stays busy, as
        the turn goes on)."""
        answer, scheduler = self._answer, self.scheduler
        if answer is None or answer.done() or scheduler is None:
            return
        try:
            text = clean_transcript((await scheduler.run(rec.audio, "command")).text, self._phrases())
        except Exception:
            log.warning("the answer couldn't be transcribed", exc_info=True)
            text = ""
        said = answer_of(text, self._phrases(), max_edits=self.cfg.wake.name_max_edits)
        log.info("answered (%s): %s", said or "unclear", logged(text))
        self.bus.publish("transcript", text=text, purpose="answer")
        if not answer.done():
            answer.set_result(None if said is None else said == "yes")

    def _timed_out(self, purpose: Purpose) -> None:
        """Nobody spoke within the wait: stand by (no answer to a question is no answer, and the turn goes on)."""
        if purpose == "answer":
            if self.listener is not None:
                self.listener.busy()
            if self._answer is not None and not self._answer.done():
                self._answer.set_result(None)
            return
        log.debug("nobody spoke (%s)", purpose)
        if self.turns is not None and not self.turns.busy:
            self._stand_by()

    def _woken_by_model(self) -> None:
        """The wake-word model fired: interrupt whatever he does and listen (the listener is recording)."""
        turns = self.turns
        if turns is None or turns.loading:
            return
        if turns.busy or self.speaking:
            turns.interrupt()
            self.stop_speaking()
        self._woken()

    # ------------------------------------------------------------------------------------------------ helpers
    def _woken(self) -> None:
        """He is listening for a request: hold the speaker, chime, show it, and tell the turn runner."""
        if self.speaker is not None and not self._held:
            self.speaker.hold()
            self._held = True
        self.chime("wake")
        if self.turns is not None:
            self.turns.set_state("listening")
            self.turns.on_wake()
        self.bus.publish("wake")

    def _open_window(self, seconds: float) -> None:
        """Keep listening without his name for ``seconds`` of silence (every reply opens a fresh window)."""
        if self.listener is None or self.turns is None:
            return
        self._follow_until = self.loop.time() + seconds
        self.turns.set_state("listening")
        self.bus.publish("listen", purpose="follow_up", seconds=round(seconds, 1))
        self.listener.listen(seconds, "follow_up")

    def _keep_listening(self) -> bool:
        """Listen on for what is left of the follow-up window; False once it has run out."""
        left = self._follow_until - self.loop.time()
        if left < FOLLOW_UP_MIN_S:
            return False
        self._open_window(left)
        return True

    def _stand_by(self) -> None:
        """Stop listening, wait for his name again (with the soft standby chime), and let the speaker rest."""
        if self.listener is not None:
            self.listener.sleep()
        if self.turns is not None:
            self.turns.set_state("idle")
        self.chime("standby")
        self._release()
        self.on_idle()

    def _release(self) -> None:
        """Let the speaker close once quiet (undoing the hold of a conversation)."""
        if self.speaker is not None and self._held:
            self.speaker.release()
        self._held = False

    def _standby(self) -> bool:
        """Whether he is waiting for his name (not listening, thinking or speaking)."""
        return (self.listener is not None and self.listener.mode == "sleep" and self.turns is not None
                and not self.turns.busy and not self.turns.loading)

    def _speculation_on(self) -> bool:
        """Whether to transcribe at a pause: ``voice.speculative`` on, or auto with Whisper on an NVIDIA GPU."""
        mode = self.cfg.voice.speculative
        return mode == "on" or (mode == "auto" and self.stt is not None and self.stt.info["device"] == "cuda")

    def _own(self) -> OwnWords:
        """His own words: the last reply and what he is saying now (the echo filter)."""
        return OwnWords(self._last_reply, self._stream.said if self._stream is not None else "")

    def _phrases(self) -> Phrases:
        """The phrase sets of the live settings (cached while they don't change)."""
        return Phrases.from_config(self.cfg)

    def _spawn(self, job: Coroutine[Any, Any, Any]) -> None:
        """Run background work, kept referenced until it ends."""
        task = self.loop.create_task(job)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _unload_ears(self) -> None:
        """No microphone after all: free the speech models."""
        if self.scheduler is not None:
            self.scheduler.close()
        if self.stt is not None:
            self.stt.unload()
        self.scheduler = self.stt = self.candidate = self.listener = None
        self._spawn(release_memory_async())

