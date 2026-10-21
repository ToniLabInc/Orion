"""Light voice facts, usable without audio or model libraries.

- The audio formats: microphone and Kokoro rates, frames and the Silero chunk.
- `levels`, the one sound-level helper (overall loudness and 16 frequency bands), and `LevelGate`, its rate limit.
- The Whisper catalogue (`STT_MODELS`, `stt_options`) and the Kokoro voice rules (`ACCENTS`, `is_voice`,
  `voice_cards`).
- The audio devices, listed once and cached (`audio_devices`).

numpy is imported inside the functions that need it, and sounddevice only when the devices are listed. Importing
`orion.voice`, or `orion.voice.wake` through it, therefore costs no more than the standard library.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterable, Mapping
from functools import lru_cache
from typing import TYPE_CHECKING, Final, TypedDict

if TYPE_CHECKING:
    from pathlib import Path

    import numpy as np
    from numpy.typing import NDArray

    from orion.config import STTConfig

log = logging.getLogger("orion.voice")

MIC_RATE: Final = 16_000  # microphone samples per second: what Whisper and Silero VAD take
MAX_COMPRESSION_RATIO: Final = 2.4  # Whisper's own: text that compresses better than this is a loop
MIC_FRAME: Final = 1_280  # 80 ms, one microphone block (openWakeWord's frame)
VAD_CHUNK: Final = 512  # 32 ms, what Silero VAD looks at
TTS_RATE: Final = 24_000  # Kokoro's output rate
BANDS: Final = 16  # frequency bands in a level event
LEVELS_HZ: Final = 15  # level events per second and source, at most

_LOW_HZ: Final = 80.0  # the bands are log-spaced from here...
_HIGH_HZ: Final = 8_000.0  # ...to here (or the Nyquist frequency, if lower)
_FLOOR_DB: Final = -60.0  # a band this quiet shows as 0...
_SPAN_DB: Final = 50.0  # ...and one this much louder (-10 dB) as 1
_MIN_SAMPLES: Final = 256  # shorter audio is too short for the bands


def levels(samples: NDArray[np.float32] | NDArray[np.float64], rate: int, gain: float) -> tuple[float, list[float]]:
    """How loud a short piece of audio is, overall and per frequency band (what the HUD's visualiser shows).

    Args:
        samples: mono float audio in -1...1, of any shape (it is flattened).
        rate: its sample rate in Hz, above zero.
        gain: what the RMS is multiplied by (the microphone and his voice are scaled differently).

    Returns:
        The RMS times `gain`, rounded to 3 decimals, and `BANDS` values in 0...1 rounded to 2 decimals: the peak of
        each band, log-spaced from 80 Hz to 8 kHz, mapped from -60...-10 dB. Audio shorter than 256 samples has
        silent bands; empty audio gives 0.0 and silent bands.
    """
    import numpy as np

    x = np.asarray(samples, dtype=np.float32).ravel()
    if not x.size:
        return 0.0, [0.0] * BANDS
    value = round(float(np.sqrt(np.dot(x, x) / x.size)) * gain, 3)
    if x.size < _MIN_SAMPLES:
        return value, [0.0] * BANDS
    window, starts, filled, stop = _band_plan(x.size, rate)
    magnitude = np.abs(np.fft.rfft(x * window)) * (4 / x.size)
    peaks = np.zeros(BANDS)
    if starts.size:
        peaks[filled] = np.maximum.reduceat(magnitude[:stop], starts)
    shown = (20 * np.log10(peaks + 1e-9) - _FLOOR_DB) / _SPAN_DB
    return value, np.round(np.clip(shown, 0.0, 1.0), 2).tolist()


@lru_cache(maxsize=32)
def _band_plan(size: int, rate: int) -> tuple[NDArray[np.float32], NDArray[np.intp], NDArray[np.bool_], int]:
    """The analysis window and the FFT bins of each band, for one block size and sample rate (cached).

    A band holds the bins from its lower edge (included) to its upper edge (excluded). Bands with bins follow one
    another without gaps, so one `np.maximum.reduceat` over their first bins finds every band's peak.

    Args:
        size: samples in the block.
        rate: the sample rate in Hz.

    Returns:
        (the Hann window, the first bin of each band that has bins, which of the bands have bins, the bin after the
        last band). The arrays are read-only: every call with this size and rate shares them.
    """
    import numpy as np

    freqs = np.fft.rfftfreq(size, 1 / rate)
    bounds = np.searchsorted(freqs, np.geomspace(_LOW_HZ, min(_HIGH_HZ, rate / 2), BANDS + 1))
    filled = bounds[1:] > bounds[:-1]
    starts = bounds[:-1][filled]
    window = np.hanning(size).astype(np.float32)
    for array in (window, starts, filled):
        array.flags.writeable = False
    return window, starts, filled, int(bounds[-1])


class LevelGate:
    """Lets at most `hz` level events a second through, for one source and one thread (the listener's microphone;
    the speaker works out his voice's levels per chunk before playing it).
    """

    __slots__ = ("_interval", "_last")

    def __init__(self, hz: float) -> None:
        """Set the rate.

        Args:
            hz: events per second, at most; 0 or less lets none through.
        """
        self._interval = 1.0 / hz if hz > 0 else None
        self._last: float | None = None

    def ready(self) -> bool:
        """Whether an event may go now; when it may, the next one has to wait a full interval.

        Returns:
            True at most `hz` times a second.
        """
        if self._interval is None:
            return False
        now = time.monotonic()
        if self._last is not None and now - self._last < self._interval:
            return False
        self._last = now
        return True


class SttModelInfo(TypedDict):
    """One Whisper model the settings offer."""

    size_mb: int  # download size (0: nothing to download of its own)
    label: str  # what it is good for, as the settings page says it ("" for auto: see stt_label)


class SttOption(TypedDict):
    """A Whisper model as the models page lists it (`GET /api/models`)."""

    name: str
    size: str  # "75 MB", "1.5 GB"; "" for auto
    note: str
    downloaded: bool


class VoiceCard(TypedDict):
    """A Kokoro voice as the settings page lists it."""

    id: str  # "bm_george"
    name: str  # "George"
    accent: str  # "British"
    gender: str  # "female" or "male"


class AudioDevices(TypedDict):
    """The names the microphone and speaker choices offer."""

    input: list[str]
    output: list[str]


STT_MODELS: Final[Mapping[str, SttModelInfo]] = {
    "auto": {"size_mb": 0, "label": ""},
    "tiny.en": {"size_mb": 75, "label": "fastest, least accurate"},
    "base.en": {"size_mb": 145, "label": "fast"},
    "small.en": {"size_mb": 485, "label": "good on the CPU"},
    "medium.en": {"size_mb": 1500, "label": "accurate but slow on the CPU"},
    "large-v3-turbo": {"size_mb": 1600, "label": "best accuracy for its speed — needs the GPU"},
    "distil-large-v3.5": {"size_mb": 1500, "label": "accurate — needs the GPU"},
    "large-v3": {"size_mb": 3100, "label": "most accurate, slowest — needs a big GPU"},
}

# The first letter of a Kokoro voice's name: its language code and accent.
ACCENTS: Final[Mapping[str, tuple[str, str]]] = {"a": ("en-us", "American"), "b": ("en-gb", "British")}


def stt_label(name: str, stt: STTConfig) -> str:
    """What a catalogue model is good for, as the settings say it; auto's comes from what it picks.

    Args:
        name: the model.
        stt: the stt settings.

    Returns:
        The words.
    """
    if name == "auto":
        return f"{stt.auto_gpu_model} on the GPU, {stt.auto_cpu_model} on the CPU"
    return STT_MODELS[name]["label"]


def stt_options(models_dir: Path, stt: STTConfig) -> list[SttOption]:
    """The Whisper catalogue with what is already on disk, for the models page.

    Args:
        models_dir: the models folder.
        stt: the stt settings (for auto's description).

    Returns:
        One option per catalogue model, in catalogue order. `auto` counts as downloaded: it picks one of the others.
    """
    from orion.inference import whisper_ready  # it imports numpy

    return [{"name": name, "size": _size_text(info["size_mb"]), "note": stt_label(name, stt),
             "downloaded": name == "auto" or whisper_ready(models_dir, name)}
            for name, info in STT_MODELS.items()]


def _size_text(size_mb: int) -> str:
    """`size_mb` as the models page shows it: "75 MB" below a gigabyte, "1.5 GB" from there, "" for nothing."""
    if size_mb <= 0:
        return ""
    return f"{size_mb} MB" if size_mb < 1000 else f"{size_mb / 1000:.1f} GB"


def is_voice(name: str) -> bool:
    """Whether this is a Kokoro voice Orion offers: accent letter, f or m, an underscore and a name ("bm_george").

    Args:
        name: a voice name from the Kokoro voices file.

    Returns:
        True for an American or British English voice.
    """
    return len(name) > 3 and name[0] in ACCENTS and name[1] in "fm" and name[2] == "_"


def voice_cards(names: Iterable[str]) -> list[VoiceCard]:
    """Voice cards for the settings page.

    Args:
        names: Kokoro voice names; any that `is_voice` rejects are left out.

    Returns:
        One card per voice, in the given order.
    """
    return [{"id": name, "name": name[3:].replace("_", " ").title(), "accent": ACCENTS[name[0]][1],
             "gender": "female" if name[1] == "f" else "male"}
            for name in names if is_voice(name)]


_devices: AudioDevices | None = None
_devices_lock = threading.Lock()


def audio_devices(*, refresh: bool = False) -> AudioDevices:
    """The microphones and speakers PortAudio knows, listed once and then served from a cache.

    The list is not refreshed by itself: with PipeWire the default device follows hot-plug anyway, and PortAudio is
    never restarted here, because the microphone stream may be open.

    Args:
        refresh: ask PortAudio again instead of using the cache.

    Returns:
        A fresh copy of the device names, each listed once. When PortAudio cannot say (no library, no sound
        server), the last list it gave, or empty lists if it never gave one.
    """
    global _devices
    with _devices_lock:
        if refresh or _devices is None:
            found = _query_devices()
            if found is not None:
                _devices = found
        if _devices is None:
            return {"input": [], "output": []}
        return {"input": list(_devices["input"]), "output": list(_devices["output"])}


def _query_devices() -> AudioDevices | None:
    """Ask PortAudio, through sounddevice, for the devices.

    Returns:
        The input and output device names, or None when sounddevice or PortAudio is unavailable.
    """
    try:
        import sounddevice as sd
    except (ImportError, OSError) as e:  # OSError: the PortAudio library itself is missing
        log.warning("cannot list the audio devices: %s", e)
        return None
    try:
        devices = sd.query_devices()
    except sd.PortAudioError as e:
        log.warning("cannot list the audio devices: %s", e)
        return None

    def names(kind: str) -> list[str]:
        """The distinct names of the devices with channels of one kind ("input" or "output")."""
        return list(dict.fromkeys(str(d["name"]) for d in devices if d[f"max_{kind}_channels"] > 0))

    return {"input": names("input"), "output": names("output")}
