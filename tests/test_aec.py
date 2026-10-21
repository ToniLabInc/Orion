"""voice/aec: WebRTC's canceller takes his own voice out of this PC's microphone. Proved offline (no microphone, no
sound): a synthetic voice through a synthetic room, driven as the speaker's callbacks and the listener's blocks drive
it, with the real library; and the frame sizes, order, hints and failures with a stand-in for it, because a frame of
the wrong size makes the real library abort the process."""

import sys
import threading
from types import ModuleType, SimpleNamespace
from typing import Any

import numpy as np
import pytest

from orion.voice import aec
from orion.voice.aec import EchoCanceller, EchoError

OUT_RATE, IN_RATE = 24_000, 16_000
FAR_FORMANTS = ((500, 90, 1.0), (1500, 140, 0.6), (2500, 200, 0.4))
NEAR_FORMANTS = ((700, 110, 1.0), (1200, 150, 0.7), (2900, 220, 0.4))


# ---------------------------------------------------------------------------------------------------- the synthetic world
def voice(seconds: float, f0: float, seed: int, formants: tuple[tuple[float, float, float], ...],
          rate: int) -> np.ndarray:
    """A speech-like signal: a gliding harmonic source through formants, in syllables with pauses, plus breath."""
    rng = np.random.default_rng(seed)
    n = int(seconds * rate)
    t = np.arange(n) / rate
    f = f0 * (1 + 0.10 * np.sin(2 * np.pi * 0.6 * t + rng.uniform(0, 6.28)) + 0.04 * np.sin(2 * np.pi * 4.3 * t))
    phase = 2 * np.pi * np.cumsum(f) / rate
    source = np.zeros(n)
    for k in range(1, 40):
        gain = sum(g * np.exp(-0.5 * ((k * f - c) / w) ** 2) for c, w, g in formants) + 0.02 / k
        source += gain * np.sin(k * phase + rng.uniform(0, 6.28))
    envelope = np.zeros(n)
    i = 0
    while i < n:
        length, gap = int(rng.uniform(0.12, 0.26) * rate), int(rng.uniform(0.03, 0.12) * rate)
        envelope[i:i + length] = np.sin(np.pi * np.arange(min(length, n - i)) / length) ** 0.7
        i += length + gap
    spoken = (source + rng.standard_normal(n) * 0.03) * envelope
    return (spoken / np.max(np.abs(spoken)) * 0.5).astype(np.float32)


def room(delay_ms: float, rt60: float = 0.35) -> np.ndarray:
    """Loudspeaker to microphone: a bulk delay, the direct path, an exponentially decaying tail (16 kHz)."""
    rng = np.random.default_rng(5)
    delay = int(IN_RATE * delay_ms / 1000)
    n = delay + int(IN_RATE * rt60)
    response = np.zeros(n)
    tail = rng.standard_normal(n - delay) * np.exp(-6.9 * np.arange(n - delay) / IN_RATE / rt60)
    response[delay:] = 0.15 * tail
    response[delay] += 1.0
    return (response / np.sqrt(np.sum(response ** 2))).astype(np.float32)


def to_16k(x: np.ndarray) -> np.ndarray:
    """24 kHz to 16 kHz by FFT."""
    m = len(x) * 2 // 3
    spectrum = np.fft.rfft(x)
    kept = np.zeros(m // 2 + 1, dtype=complex)
    size = min(len(spectrum), len(kept))
    kept[:size] = spectrum[:size]
    return (np.fft.irfft(kept, m) * (m / len(x))).astype(np.float32)


def simulate(far: np.ndarray | None, near: np.ndarray | None, *, seconds: float, delay_ms: float = 80,
             out_lat_ms: float = 100, in_lat_ms: float = 20, noise: bool = False,
             ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """He speaks `far` (24 kHz) through the room while `near` (16 kHz) is said at the microphone. The canceller gets
    the speaker's 40 ms blocks and the microphone's 80 ms blocks in the order real time would, the speaker's
    `out_lat_ms` ahead of the sound and the microphone's `in_lat_ms` behind it. Returns (the echo at the microphone,
    the microphone, the cleaned microphone)."""
    total = int(seconds * IN_RATE)
    played = np.zeros(int(seconds * OUT_RATE), np.float32)
    if far is not None:
        played[:len(far)] = far
    echo = np.convolve(to_16k(played)[:total], room(delay_ms))[:total]
    mic = echo.copy()
    if near is not None:
        mic[:len(near)] += near
    mic += np.random.default_rng(6).standard_normal(total).astype(np.float32) * 10 ** (-60 / 20)
    canceller = EchoCanceller()
    canceller.configure(echo=True, noise=noise)
    canceller.latency(output_s=out_lat_ms / 1000, input_s=in_lat_ms / 1000)
    events = [(b * 40 - out_lat_ms, 0, b) for b in range(int(seconds / 0.04))]
    events += [(b * 80 + in_lat_ms, 1, b) for b in range(1, int(seconds / 0.08) + 1)]
    cleaned = np.zeros(total, np.float32)
    for _, kind, b in sorted(events):
        if kind == 0:
            canceller.render(played[b * 960:(b + 1) * 960], OUT_RATE)
        elif (start := (b - 1) * 1280) + 1280 <= total:
            cleaned[start:start + 1280] = canceller.capture(mic[start:start + 1280])
    return echo, mic, cleaned


def power_db(x: np.ndarray) -> float:
    """Mean power in dB."""
    return 10 * np.log10(float(np.mean(np.square(x, dtype=np.float64))) + 1e-12)


def erle_db(echo: np.ndarray, mic: np.ndarray, cleaned: np.ndarray, first: float, last: float) -> float:
    """How much quieter the echo got, in dB (echo return loss enhancement): the microphone's power over the cleaned
    output's, between two moments (seconds), counting the 10 ms frames where the echo is audible."""
    span = slice(int(first * IN_RATE) // 160 * 160, int(last * IN_RATE) // 160 * 160)
    frames = lambda x: np.square(x[span], dtype=np.float64).reshape(-1, 160).sum(1)
    audible = frames(echo) > 160 * 1e-5
    return float(10 * np.log10(frames(mic)[audible].sum() / frames(cleaned)[audible].sum()))


# ---------------------------------------------------------------------------------------------------- the real library
@pytest.fixture(scope="module")
def reply() -> np.ndarray:
    """What he says: 8.5 s of voice, 3.5 s of quiet, then 4 s more (24 kHz)."""
    spoken = voice(16, 118, 3, FAR_FORMANTS, OUT_RATE)
    spoken[int(8.5 * OUT_RATE):int(12 * OUT_RATE)] = 0
    return spoken


@pytest.mark.parametrize(("delay_ms", "out_lat_ms", "in_lat_ms"), [(30, 100, 20), (80, 200, 40)])
@pytest.mark.parametrize("noise", [False, True])
def test_his_echo_is_cancelled_by_at_least_20_db(reply: np.ndarray, delay_ms: float, out_lat_ms: float,
                                                 in_lat_ms: float, noise: bool) -> None:
    """Between 6 and 8.5 s into his reply, with only his echo at the microphone (a room that delays it 30 or 80 ms
    and rings for a third of a second, the device buffers adding up to 120 or 240 ms), it is at least 20 dB quieter."""
    echo, mic, cleaned = simulate(reply, None, seconds=16, delay_ms=delay_ms, out_lat_ms=out_lat_ms,
                                  in_lat_ms=in_lat_ms, noise=noise)
    assert power_db(echo[6 * IN_RATE:int(8.5 * IN_RATE)]) > -40  # it was audible
    assert erle_db(echo, mic, cleaned, 6, 8.5) >= 20


def test_the_user_is_heard_over_him(reply: np.ndarray) -> None:
    """Said alone (he is quiet) the user's voice comes through unchanged; said over him, the cleaned microphone
    follows the user's voice (a raw one follows the echo: it is louder than the user), a few dB quieter."""
    near = np.zeros(16 * IN_RATE, np.float32)
    user = voice(2.5, 190, 9, NEAR_FORMANTS, IN_RATE)
    near[int(9 * IN_RATE):int(11.5 * IN_RATE)] = user
    near[int(13 * IN_RATE):int(15.5 * IN_RATE)] = user
    echo, mic, cleaned = simulate(reply, near, seconds=16)
    _, _, reference = simulate(None, near, seconds=16)  # the user's voice through the same canceller, nobody else
    for label, first, last, similar in (("alone", 9, 11.5, 0.99), ("over him", 13, 15.5, 0.7)):
        span = slice(int(first * IN_RATE), int(last * IN_RATE))
        got, want, heard = cleaned[span], reference[span], mic[span]
        assert np.corrcoef(got, want)[0, 1] >= similar, label
        assert abs(np.corrcoef(heard, want)[0, 1]) < 0.2 or label == "alone", label  # the raw one doesn't follow it
        assert power_db(want) - power_db(got) <= 5, label  # at most a few dB quieter
    assert power_db(mic[int(13 * IN_RATE):int(15.5 * IN_RATE)]) > power_db(near[int(13 * IN_RATE):int(15.5 * IN_RATE)])
    assert erle_db(echo, mic, cleaned, 6, 8.5) >= 20


def test_two_threads_can_use_it_at_once() -> None:
    """The speaker's callback renders while the listener's thread captures: nothing fails, and every block is
    handed on (the canceller is not left holding the speaker's blocks)."""
    canceller = EchoCanceller()
    canceller.configure(echo=True, noise=False)
    block, errors = np.full(960, 0.1, np.float32), []

    def speaker() -> None:
        """The audio callback's part."""
        try:
            for _ in range(250):
                canceller.render(block, OUT_RATE)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def listener() -> None:
        """The listener thread's part."""
        try:
            for _ in range(125):
                assert len(canceller.capture(np.zeros(1280, np.float32))) == 1280
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=speaker), threading.Thread(target=listener)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert errors == [] and not any(thread.is_alive() for thread in threads)
    canceller.capture(np.zeros(1280, np.float32))  # a capture picks up whatever the speaker left waiting
    assert not canceller._pending


# ---------------------------------------------------------------------------------------------------- a stand-in library
class Calls:
    """What the stand-in library was asked, in order."""

    def __init__(self) -> None:
        """Nothing yet."""
        self.log: list[tuple[Any, ...]] = []
        self.made: list[dict[str, bool]] = []


@pytest.fixture
def library(monkeypatch: pytest.MonkeyPatch) -> Calls:
    """A stand-in for livekit.rtc that notes every call (and has a mind of its own about nothing)."""
    calls = Calls()

    class AudioFrame:
        """A frame as the library has it."""

        def __init__(self, data: bytearray, sample_rate: int, num_channels: int, samples_per_channel: int) -> None:
            """Keep what the canceller wrote."""
            self._data, self.sample_rate, self.samples = data, sample_rate, samples_per_channel

    class AudioProcessingModule:
        """The canceller: it checks the frame as the native library does, by dying (here: raising)."""

        def __init__(self, **flags: bool) -> None:
            """Note how it was made."""
            calls.made.append(flags)

        def process_stream(self, frame: AudioFrame) -> None:
            """A microphone frame: 160 samples of 16 kHz, made louder by one so the test sees it came back."""
            assert (frame.sample_rate, frame.samples, len(frame._data)) == (16000, 160, 320)
            samples = np.frombuffer(frame._data, dtype="<i2")
            calls.log.append(("capture", int(samples[0])))
            samples += 1

        def process_reverse_stream(self, frame: AudioFrame) -> None:
            """A speaker frame: exactly 10 ms."""
            assert frame.samples == frame.sample_rate // 100 and len(frame._data) == frame.samples * 2
            calls.log.append(("render", frame.sample_rate, int(np.frombuffer(frame._data, dtype="<i2")[0])))

        def set_stream_delay_ms(self, delay_ms: int) -> None:
            """Note the hint."""
            calls.log.append(("delay", delay_ms))

    package = ModuleType("livekit")
    package.rtc = SimpleNamespace(AudioProcessingModule=AudioProcessingModule, AudioFrame=AudioFrame)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "livekit", package)
    return calls


def ramp(start: int, count: int) -> np.ndarray:
    """`count` float samples whose 16-bit values are start, start + 1, ... (so a test can tell them apart)."""
    return (np.arange(start, start + count) / 32767.0).astype(np.float32)


def test_blocks_are_cut_into_whole_10_ms_frames_in_order(library: Calls) -> None:
    """Speaker blocks of any size give 240-sample frames at 24 kHz and keep their remainder for the next; microphone
    blocks give 160-sample frames; a frame is never any other size (the native library would abort the process)."""
    canceller = EchoCanceller()
    canceller.configure(echo=True, noise=False)
    library.log.clear()
    canceller.render(ramp(0, 500), OUT_RATE)  # two frames and 20 samples over
    canceller.render(ramp(500, 700), OUT_RATE)  # the 20 and 700 more: 720 = three frames
    renders = [entry for entry in library.log if entry[0] == "render"]
    assert [r[2] for r in renders] == [0, 240, 480, 720, 960]  # five frames, nothing lost or repeated
    cleaned = canceller.capture(ramp(0, 320))
    assert [entry[0] for entry in library.log[5:]] == ["capture", "capture"]
    assert cleaned[0] == pytest.approx(1 / 32768 + 0 / 32767, abs=1e-4) and len(cleaned) == 320
    with pytest.raises(ValueError, match="whole"):
        canceller.capture(np.zeros(1000, np.float32))
    with pytest.raises(ValueError, match="whole 10 ms"):
        canceller.render(np.zeros(100, np.float32), 22_050)


def test_what_the_speaker_played_comes_before_the_microphone_frame_even_when_it_had_to_wait(library: Calls) -> None:
    """The speaker's callback never waits for the canceller: with the listener inside it, the block is queued, and the
    listener hands it on before its next frame, in order."""
    canceller = EchoCanceller()
    canceller.configure(echo=True, noise=False)
    library.log.clear()
    with canceller._lock:  # the listener thread is in the middle of a frame
        canceller.render(ramp(0, 480), OUT_RATE)
        canceller.render(ramp(480, 480), OUT_RATE)
    assert library.log == []  # nothing was handed on, and nobody waited
    canceller.capture(ramp(0, 160))
    assert [entry[0] for entry in library.log] == ["render", "render", "render", "render", "capture"]
    canceller.render(ramp(960, 240), OUT_RATE)  # the lock is free: handed on at once
    assert library.log[-1][0] == "render" and library.log[-1][2] == 960


def test_the_delay_hint_is_the_streams_latencies_and_half_a_block(library: Calls) -> None:
    """Speakers' latency + microphone's + 40 ms, given again whenever either is reported."""
    canceller = EchoCanceller()
    canceller.latency(output_s=0.1)
    canceller.configure(echo=True, noise=False)
    assert ("delay", 140) in library.log  # 100 ms + 0 + half of an 80 ms block
    canceller.latency(input_s=0.02)
    assert library.log[-1] == ("delay", 160)
    canceller.latency(output_s=0.2)
    assert library.log[-1] == ("delay", 260)


def test_it_is_off_until_started_and_off_again_when_stopped(library: Calls) -> None:
    """Off, the microphone passes through as it is (the same array) and the speaker's blocks are ignored; a changed
    noise setting makes a new canceller, an unchanged one doesn't."""
    canceller = EchoCanceller()
    block = np.ones(1280, np.float32)
    assert not canceller.on and canceller.capture(block) is block
    canceller.render(ramp(0, 960), OUT_RATE)
    assert library.log == []
    canceller.configure(echo=True, noise=False)
    canceller.configure(echo=True, noise=False)
    canceller.configure(echo=True, noise=True)
    assert canceller.on and library.made == [
        {"echo_cancellation": True, "noise_suppression": False, "high_pass_filter": True, "auto_gain_control": False},
        {"echo_cancellation": True, "noise_suppression": True, "high_pass_filter": True, "auto_gain_control": False}]
    canceller.configure(echo=False, noise=False)
    assert not canceller.on and canceller.capture(block) is block


def test_a_machine_without_the_library_is_told_in_words(monkeypatch: pytest.MonkeyPatch) -> None:
    """No LiveKit (or its native library): an error saying so, the canceller stays off, and `orion check` has the
    same words."""

    class Missing(ModuleType):
        """A package whose native library can't be loaded."""

        def __getattr__(self, name: str) -> Any:
            """Importing anything from it fails."""
            raise ImportError("failed to load liblivekit_ffi.so")

    monkeypatch.setitem(sys.modules, "livekit", Missing("livekit"))
    canceller = EchoCanceller()
    with pytest.raises(EchoError, match="livekit package.*liblivekit_ffi.so"):
        canceller.configure(echo=True, noise=False)
    assert not canceller.on
    assert "can't start here" in aec.unavailable()


def test_a_machine_with_the_library_has_nothing_to_report() -> None:
    """The real library starts here."""
    assert aec.unavailable() == ""


# ---------------------------------------------------------------------------------------------------- failing while running
def test_a_canceller_that_fails_while_running_is_off_and_says_why_once(library: Calls) -> None:
    """The library refusing a frame switches the canceller off (the microphone then passes through, as it is), and the
    one who started it is told why, in words, once."""
    told: list[str] = []
    canceller = EchoCanceller(on_failure=told.append)
    canceller.configure(echo=True, noise=False)
    block = np.zeros(1280, np.float32)

    def refuse(frame: Any) -> None:
        """The library failing."""
        raise RuntimeError("the frame was refused")

    canceller._apm.process_stream = refuse  # the stand-in's own method
    assert canceller.capture(block) is block and not canceller.on
    canceller.capture(block)
    canceller.render(ramp(0, 960), OUT_RATE)
    assert told == ["WebRTC's audio processing stopped: the frame was refused"]
    canceller.configure(echo=True, noise=False)  # started again, it works again
    assert canceller.on and canceller.capture(block) is not block


def test_stopping_it_lets_go_of_the_native_canceller(library: Calls) -> None:
    """Stopped, it holds nothing of the library (its object goes while the library is still there)."""
    canceller = EchoCanceller()
    canceller.configure(echo=True, noise=False)
    canceller.render(ramp(0, 960), OUT_RATE)
    canceller.configure(echo=False, noise=False)
    assert canceller._apm is None and canceller._capture is None and not canceller._reverse and not canceller._pending


# ---------------------------------------------------------------------------------------------------- the speaker and the listener
class Stethoscope:
    """A canceller that only notes what it is given (the speaker's blocks, the latencies) and halves the microphone."""

    def __init__(self) -> None:
        """Nothing yet."""
        self.rendered: list[tuple[np.ndarray, int]] = []
        self.latencies: list[dict[str, float]] = []

    def render(self, block: np.ndarray, rate: int) -> None:
        """A block the speaker wrote."""
        self.rendered.append((block.copy(), rate))

    def latency(self, **latencies: float) -> None:
        """A stream's latency."""
        self.latencies.append(latencies)

    def capture(self, block: np.ndarray) -> np.ndarray:
        """The microphone, quieter."""
        return block * 0.5


class Devices(ModuleType):
    """A stand-in for sounddevice whose streams report a latency."""

    class OutputStream:
        """The speaker's stream."""

        latency = 0.12

        def __init__(self, **options: Any) -> None:
            """Note how it was opened."""
            self.options = options

        def start(self) -> None:
            """Started."""

    class InputStream(OutputStream):
        """The microphone's."""

        latency = 0.03


def test_the_speaker_gives_the_canceller_every_block_it_writes_and_its_latency(monkeypatch: pytest.MonkeyPatch) -> None:
    """The block the callback fills (silence too) goes to the canceller at its rate, with the first-frame time to the
    reply; a browser's speakers (the same callback) are not this machine's, so nothing of them is given; the latency
    the output stream reports is passed on."""
    import asyncio

    from orion.voice.tts import TTS_RATE, Speaker, _Chunk

    monkeypatch.setitem(sys.modules, "sounddevice", Devices("sounddevice"))
    echo = Stethoscope()
    started: list[float] = []

    async def go() -> None:
        """Play one chunk and a gap."""
        events = SimpleNamespace(on_playing=lambda playing: None, on_caption=lambda text: None,
                                 on_voice_level=lambda value, bands: None)
        speaker = Speaker(SimpleNamespace(), None, events, asyncio.get_running_loop(), lambda: False,  # type: ignore[arg-type]
                          echo)  # type: ignore[arg-type]
        audio = ramp(1000, 1500) * 0.5
        speaker._chunks.append(_Chunk(audio, "Hello.", first=started.append))
        speaker._pending = 1
        out = np.zeros((960, 1), np.float32)
        speaker._fill(out, 960, None, None)  # the first 960 samples of the chunk
        speaker._fill(out, 960, None, None)  # the last 540 and 420 of silence
        speaker._fill(out, 960, None, None)  # nothing queued
        await asyncio.sleep(0)
        speaker.output = SimpleNamespace()  # type: ignore[assignment]  # a browser's speakers: not this machine's
        speaker._fill(out, 960, None, None)
        speaker.output = None
        speaker._open()
        speaker.close()

    asyncio.run(go())
    assert [rate for _, rate in echo.rendered] == [TTS_RATE] * 3
    blocks = [block for block, _ in echo.rendered]
    assert np.allclose(blocks[0], ramp(1000, 960) * 0.5) and np.allclose(blocks[1][:540], ramp(1960, 540) * 0.5)
    assert not blocks[1][540:].any() and not blocks[2].any()
    assert len(started) == 1 and isinstance(started[0], float)
    assert echo.latencies == [{"output_s": 0.12}]


def listener_with(folder: Any, echo: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, list[np.ndarray]]:
    """A listener that records what it is handed to process, and that list."""
    from orion.config import default_config
    from orion.voice import listener as listener_module
    from orion.voice.listener import Listener

    cfg = default_config()
    heard = Listener(cfg.voice, cfg.wake, folder, SimpleNamespace(), levels_wanted=lambda: False,  # type: ignore[arg-type]
                     is_speaking=lambda: False, echo=echo)
    seen: list[np.ndarray] = []
    monkeypatch.setattr(heard, "_process", lambda frame, notify: seen.append(frame))
    monkeypatch.setattr(listener_module, "_run", lambda notify: None)
    return heard, seen


def test_the_listener_hears_this_machines_microphone_through_the_canceller_only(tmp_path: Any,
                                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """With a canceller, this machine's microphone is cleaned before anything else sees it; a browser's microphone,
    which has been cleaned in the browser, and a listener with no canceller are heard as they are; the stream's
    latency goes to the canceller."""
    from orion.voice.listener import PushedInput

    frame = np.full(1280, 0.4, np.float32)
    for remote, echo, cleaned in ((None, Stethoscope(), True), (PushedInput(), Stethoscope(), False),
                                  (None, None, False)):
        heard, seen = listener_with(tmp_path, echo, monkeypatch)
        heard.remote = remote
        heard._queue.put(frame)
        heard._queue.put(None)
        heard._loop()
        assert len(seen) == 1 and np.allclose(seen[0], frame * 0.5 if cleaned else frame)
    echo = Stethoscope()
    heard, _ = listener_with(tmp_path, echo, monkeypatch)
    monkeypatch.setitem(sys.modules, "sounddevice", Devices("sounddevice"))
    heard._open()
    assert echo.latencies == [{"input_s": 0.03}]


def test_the_session_starts_the_canceller_when_the_settings_want_it_and_says_when_it_cannot(
        tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Wanted with a microphone: started with the noise setting. Not wanted (off, voice input off, no microphone
    here): stopped. A machine that can't run it gets a toast and a log line, and he listens without it; so does a
    canceller that stops while running."""
    import asyncio

    from orion.base import EventBus
    from orion.config import default_config
    from orion.voice.session import VoiceSession

    async def go() -> tuple[list[tuple[bool, bool]], list[dict[str, Any]]]:
        """Settings changes through a session whose canceller is a recorder."""
        cfg = default_config()
        events: list[dict[str, Any]] = []
        bus = EventBus()
        bus.subscribe(events.append)
        bus.bind(asyncio.get_running_loop())
        session = VoiceSession(cfg, bus, tmp_path, microphone=True, levels_wanted=lambda: False)
        configured: list[tuple[bool, bool]] = []

        def configure(echo: bool, noise: bool) -> None:
            """Note it; fail when noise suppression is on."""
            configured.append((echo, noise))
            if cfg.voice.noise_suppression:
                raise EchoError("no native library here")

        monkeypatch.setattr(session.echo, "configure", configure)
        await session.reload_echo()
        cfg.voice.echo_cancellation = False
        await session.reload_echo()
        cfg.voice.echo_cancellation = True
        cfg.voice.enabled = False
        await session.reload_echo()
        cfg.voice.enabled = True
        session.microphone = False
        await session.reload_echo()
        session.microphone = True
        cfg.voice.noise_suppression = True
        await session.reload_echo()
        session.echo._on_failure("it stopped")  # type: ignore[misc]  # from another thread, as it would be
        await asyncio.sleep(0)
        return configured, events

    configured, events = asyncio.run(go())
    assert configured == [(True, False), (False, False), (False, False), (False, False), (True, True)]
    toasts = [e["text"] for e in events if e["type"] == "toast"]
    assert toasts == ["No echo cancellation: no native library here", "No echo cancellation: it stopped"]
