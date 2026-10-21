"""Speech recognition with faster-whisper: the main model, the stage-1 name spotter, and the scheduler between them.

- ``STT``: the main Whisper (by default large-v3-turbo on an NVIDIA GPU, small.en on the CPU), loaded at start in a
  worker thread and reloadable when the settings change; if a new model fails to load, the previous one comes back.
- ``Candidate``: a tiny Whisper on a couple of CPU threads that listens for his name first (stage 1), so the main model
  only confirms likely hits and the GPU stays idle while a TV talks.
- ``SttScheduler``: one worker thread runs the main model's passes in order; a request first drops the name checks
  still waiting (they are moot once a recording runs), so it never waits behind one. The candidate has its own thread.

Decoding is tuned per job. A command gets the vocabulary prompt ("Orion, " and the names he knows: it halves the
errors on them) and at most ``stt.fallback_temperatures`` passes. A confirmation (stage 2) gets the vocabulary without
his name, because a prompt holding "Orion" made Whisper write "Orion" over background noise, and at most
``CONFIRM_MAX_TOKENS`` tokens. A candidate check gets only his name as its prompt (see ``Candidate``).

faster_whisper and ctranslate2 load inside the functions that need them. faster_whisper imports PyAV only to decode
files, and Orion always passes numpy audio, so a stand-in module spares loading FFmpeg (17 MB of memory, measured).
"""

import asyncio
import ctypes
import functools
import logging
import sys
import threading
import time
import types
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, TypedDict

import numpy as np

from orion.base import logged
from orion.inference import release_memory, whisper_path
from orion.voice import MAX_COMPRESSION_RATIO, MIC_RATE

if TYPE_CHECKING:
    from numpy.typing import NDArray

    from orion.config import STTConfig

log = logging.getLogger("orion.stt")

type Mode = Literal["command", "confirm"]

NAME_PROMPT: Final = "Orion"  # what the command prompt starts with: his name, spelled the way he spells it
CONFIRM_MAX_TOKENS: Final = 64  # a confirmation reads a few words: enough for "Orion, what time is it?"
CANDIDATE_MAX_TOKENS: Final = 24  # stage 1 only has to find his name
CANDIDATE_PROMPT: Final = "Orion."  # stage 1 is told his name, so a rare word isn't heard as a common one
CANDIDATE_THREADS: Final = 2  # CPU threads of the candidate model: quick on short windows, light on the PC
_AV_STUB_DOC: Final = "stand-in for PyAV: Orion passes numpy audio to faster-whisper and never decodes files"


@dataclass(frozen=True, slots=True)
class Transcript:
    """What one pass of a model heard.

    Attributes:
        text: the text of the segments that are speech, joined ("" when none is).
        hit_token_cap: the pass stopped at its token limit, so the end of what was said may be missing.
        ms: how long the pass took.
    """

    text: str
    hit_token_cap: bool
    ms: float


class SttInfo(TypedDict):
    """The main model as it runs now."""

    model: str
    device: str
    compute_type: str
    loaded: bool


@functools.cache
def cuda_available() -> bool:
    """Whether Whisper can run on an NVIDIA GPU here, loading cuBLAS from its Python package for CTranslate2.

    CTranslate2's Whisper needs only cuBLAS besides the driver (libcublasLt comes with it), never cuDNN. The answer
    is worked out once.
    """
    try:
        import ctranslate2
        import nvidia

        if ctranslate2.get_cuda_device_count() <= 0:
            return False
        ctypes.CDLL(f"{nvidia.__path__[0]}/cublas/lib/libcublas.so.12", mode=ctypes.RTLD_GLOBAL)
    except (ImportError, OSError, AttributeError, IndexError) as e:
        log.info("no GPU speech recognition (%s)", e)
        return False
    return True


def resolve(cfg: "STTConfig") -> tuple[str, str, str]:
    """The (model, device, compute type) these settings run, without loading anything.

    Args:
        cfg: the stt settings.

    Returns:
        "auto" device means the GPU when there is one; "auto" model means stt.auto_gpu_model there and
        stt.auto_cpu_model on the CPU; "auto" compute means int8_float16 on the GPU and int8 on the CPU.
    """
    device = cfg.device
    if device in ("auto", "cuda"):
        device = "cuda" if cuda_available() else "cpu"
    model = cfg.model
    if model == "auto":
        model = cfg.auto_gpu_model if device == "cuda" else cfg.auto_cpu_model
    compute = cfg.compute_type
    if compute == "auto":
        compute = "int8_float16" if device == "cuda" else "int8"
    return model, device, compute


def _whisper_model_class() -> Any:
    """faster_whisper.WhisperModel, imported with a stand-in for PyAV when PyAV isn't loaded already."""
    if "faster_whisper" in sys.modules or "av" in sys.modules:
        from faster_whisper import WhisperModel

        return WhisperModel
    sys.modules["av"] = types.ModuleType("av", _AV_STUB_DOC)
    try:
        from faster_whisper import WhisperModel
    except (ImportError, AttributeError):  # a faster-whisper that needs PyAV at import: load the real one
        del sys.modules["av"]
        for name in [m for m in sys.modules if m.startswith("faster_whisper")]:
            del sys.modules[name]
        from faster_whisper import WhisperModel
    return WhisperModel


def _load_whisper(models_dir: Path, name: str, device: str, compute: str, threads: int) -> Any:
    """A faster_whisper WhisperModel from its folder, its files fetched the first time (blocking)."""
    whisper = _whisper_model_class()  # first: the PyAV stand-in must be in place before faster_whisper loads
    return whisper(str(whisper_path(models_dir, name)), device=device, compute_type=compute, cpu_threads=threads)


def _decode(model: Any, audio: "NDArray[np.float32]", *, prompt: str | None, temperature: float | tuple[float, ...],
            max_tokens: int | None, beam_size: int, no_speech_threshold: float,
            logprob_threshold: float) -> Transcript:
    """Run one transcription and summarise it.

    Segments that the model rates as no speech AND is unsure about are dropped as noise.

    Args:
        model: a faster_whisper WhisperModel.
        audio: float32 mono at 16 kHz.
        prompt: the initial prompt, None for none.
        temperature: one temperature, or the fallback sequence.
        max_tokens: the token limit, None for the model's own.
        beam_size: the beam width.
        no_speech_threshold: a segment above this no-speech probability...
        logprob_threshold: ...and below this average log-probability is dropped.

    Returns:
        The transcript.
    """
    started = time.perf_counter()
    options: dict[str, Any] = {"language": "en", "beam_size": beam_size, "without_timestamps": True,
                               "condition_on_previous_text": False, "vad_filter": False, "initial_prompt": prompt,
                               "temperature": temperature, "compression_ratio_threshold": MAX_COMPRESSION_RATIO}
    if max_tokens is not None:
        options["max_new_tokens"] = max_tokens
    segments, _ = model.transcribe(audio, **options)
    kept: list[str] = []
    tokens = 0
    for segment in segments:
        tokens += len(segment.tokens)
        if segment.no_speech_prob > no_speech_threshold and segment.avg_logprob < logprob_threshold:
            continue
        kept.append(segment.text.strip())
    capped = max_tokens is not None and tokens >= max_tokens
    return Transcript(" ".join(t for t in kept if t).strip(), capped, (time.perf_counter() - started) * 1000)


class STT:
    """The main Whisper model: loaded on request, reloadable, its vocabulary given to Whisper as a prompt."""

    def __init__(self, cfg: "STTConfig", models_dir: Path, threads: int) -> None:
        """Prepare; nothing loads until `load()` (the runtime calls it in a worker thread).

        Args:
            cfg: the stt settings, read at use time.
            models_dir: the models folder; Whisper models live in its whisper/ folder.
            threads: CPU threads for a CPU model.
        """
        self.cfg = cfg
        self.models_dir = models_dir
        self.threads = threads
        self._model: Any = None
        self._info: SttInfo = {"model": "", "device": "", "compute_type": "", "loaded": False}
        self._words: tuple[str, ...] = ()
        self._lock = threading.Lock()  # one pass at a time; a reload waits for the pass under way

    @property
    def info(self) -> SttInfo:
        """The model that runs now (a copy)."""
        return SttInfo(**self._info)

    def load(self) -> None:
        """(Re)load the model for the current settings, downloading it the first time, then warm it up (blocking).

        The old model is freed first: two large models don't fit a 6 GB GPU. If the new one fails, the previous one
        is loaded again, so he keeps hearing you, and the error is raised for the HUD to show.

        Raises:
            Exception: the model could not be loaded (no internet for a first download, say).
        """
        previous = self._info if self._info["loaded"] else None
        with self._lock:
            self._model = None
            try:
                self._open(*resolve(self.cfg))
            except Exception:
                if previous is None:
                    raise
                self._open(previous["model"], previous["device"], previous["compute_type"])
                raise
        info = self._info
        log.info("speech recognition: %s on the %s (%s)", info["model"], "GPU" if info["device"] == "cuda" else "CPU",
                 info["compute_type"])
        self.transcribe(np.zeros(MIC_RATE, dtype=np.float32), "command")  # the first pass pays the CUDA set-up

    def _open(self, model: str, device: str, compute: str) -> None:
        """Open a model on a device, falling back from the GPU to the CPU auto model if CUDA fails."""
        try:
            self._model = _load_whisper(self.models_dir, model, device, compute, self.threads)
        except Exception as e:
            if device != "cuda":
                raise
            log.warning("speech recognition on the GPU failed (%s): using the CPU", e)
            model, device, compute = self.cfg.auto_cpu_model, "cpu", "int8"
            self._model = _load_whisper(self.models_dir, model, device, compute, self.threads)
        release_memory()  # the model's copy in RAM, once it is on the GPU
        self._info = SttInfo(model=model, device=device, compute_type=compute, loaded=True)

    def unload(self) -> None:
        """Free the model (the microphone was switched off, say)."""
        with self._lock:
            self._model = None
            self._info = SttInfo(model="", device="", compute_type="", loaded=False)
        release_memory()

    def set_vocabulary(self, words: Iterable[str]) -> None:
        """The names Whisper should spell right: the user's name, places, people and devices from memory.

        stt.extra_vocabulary is added, and the list is cut at stt.vocabulary_max, first ones kept.

        Args:
            words: the names, most important first.
        """
        names = (w.strip() for w in [*words, *self.cfg.extra_vocabulary] if w and w.strip())
        self._words = tuple(dict.fromkeys(names))[: self.cfg.vocabulary_max]

    def prompt(self, mode: Mode) -> str | None:
        """The initial prompt of a pass.

        Args:
            mode: "command" starts with his name; "confirm" never holds it.

        Returns:
            The prompt, or None for a confirmation with no vocabulary.
        """
        names = ", ".join(self._words)
        if mode == "command":
            return f"{NAME_PROMPT}, {names}." if names else f"{NAME_PROMPT}."
        return f"{names}." if names else None

    def transcribe(self, audio: "NDArray[np.float32]", mode: Mode) -> Transcript:
        """Transcribe audio with the main model (blocking: the scheduler's thread calls it).

        Args:
            audio: float32 mono at 16 kHz.
            mode: "command" (a request: fallback temperatures) or "confirm" (a name check: one pass, few tokens).

        Returns:
            What was heard.

        Raises:
            RuntimeError: no model is loaded.
        """
        with self._lock:
            if self._model is None:
                raise RuntimeError("speech recognition is not loaded")
            cfg = self.cfg
            confirm = mode == "confirm"
            result = _decode(self._model, audio, prompt=self.prompt(mode),
                             temperature=0.0 if confirm else tuple(cfg.fallback_temperatures),
                             max_tokens=CONFIRM_MAX_TOKENS if confirm else None, beam_size=cfg.beam_size,
                             no_speech_threshold=cfg.no_speech_threshold, logprob_threshold=cfg.logprob_threshold)
        log.debug("stt %s %.0f ms (%s): %s", mode, result.ms, self._info["model"], logged(result.text))
        return result


class Candidate:
    """Stage-1 name spotting: a tiny Whisper on the CPU, prompted with his name so it favours hearing it (unprompted it
    heard the old name, "Ultron", as "I'll try" or "Outrun": 77% against 97%). Stage 2, the main model with no name in
    its prompt, still decides, so the prompt costs no false wakes: on test speech in six voices, clean, noisy, echoing,
    far and shouted, both stages caught 99% of calls to "Orion"."""

    def __init__(self, cfg: "STTConfig", model: str, models_dir: Path, threads: int = CANDIDATE_THREADS) -> None:
        """Prepare; nothing loads until `load()`.

        Args:
            cfg: the stt settings (the no-speech filter thresholds), read at use time.
            model: the candidate model (wake.candidate_model), e.g. "tiny.en".
            models_dir: the models folder.
            threads: CPU threads it may use.
        """
        self.cfg = cfg
        self.name = model
        self.models_dir = models_dir
        self.threads = threads
        self._model: Any = None

    def load(self) -> bool:
        """Load the model (blocking), fetching it the first time.

        Returns:
            True when ready; False when it could not be loaded: stage 2 then checks everything, as before.
        """
        try:
            self._model = _load_whisper(self.models_dir, self.name, "cpu", "int8", self.threads)
        except Exception as e:  # noqa: BLE001 (a download or ctranslate2 failure must only turn stage 1 off)
            log.warning("name spotter %s unavailable (%s): the main model checks every window", self.name, e)
            return False
        log.info("name spotter: %s on %d CPU threads", self.name, self.threads)
        return True

    def check(self, audio: "NDArray[np.float32]") -> Transcript:
        """Transcribe a standby window cheaply (blocking: the scheduler's candidate thread calls it).

        Raises:
            RuntimeError: the model is not loaded.
        """
        if self._model is None:
            raise RuntimeError("the name spotter is not loaded")
        return _decode(self._model, audio, prompt=CANDIDATE_PROMPT, temperature=0.0, max_tokens=CANDIDATE_MAX_TOKENS,
                       beam_size=1, no_speech_threshold=self.cfg.no_speech_threshold,
                       logprob_threshold=self.cfg.logprob_threshold)


class SttScheduler:
    """Runs the main model on one worker thread, a request never behind a name check; the candidate on another."""

    def __init__(self, stt: STT, candidate: Candidate | None, loop: asyncio.AbstractEventLoop) -> None:
        """Prepare the worker threads (each starts with its first pass).

        Args:
            stt: the main model.
            candidate: the stage-1 model, loaded; None when stage 1 is off.
            loop: the event loop the futures belong to.
        """
        self.stt = stt
        self.candidate = candidate
        self._loop = loop
        self._main = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt")
        self._spotter = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt-candidate")
        self._checks: set[asyncio.Future[Transcript]] = set()  # name checks queued or under way

    async def run(self, audio: "NDArray[np.float32]", mode: Mode) -> Transcript:
        """Transcribe with the main model, after the passes before it; a command first drops the name checks.

        Args:
            audio: float32 mono at 16 kHz.
            mode: "command" (a request) or "confirm" (a name check, which `drop_checks` can drop).

        Returns:
            What was heard.

        Raises:
            asyncio.CancelledError: the name check was dropped (`drop_checks`) or its caller gave up.
            RuntimeError: the scheduler is closed.
            Exception: the model failed.
        """
        if mode == "command":
            self.drop_checks()
        future = self._loop.run_in_executor(self._main, self.stt.transcribe, audio, mode)
        if mode == "confirm":
            self._checks.add(future)
            future.add_done_callback(self._checks.discard)
        return await future

    async def spot(self, audio: "NDArray[np.float32]") -> Transcript | None:
        """Stage 1: transcribe a standby window with the candidate model on its own thread.

        Returns:
            What it heard, or None when there is no candidate model (stage 2 decides alone).
        """
        if self.candidate is None:
            return None
        return await self._loop.run_in_executor(self._spotter, self.candidate.check, audio)

    def drop_checks(self) -> None:
        """Drop every name check (a recording started: they are moot); one under way finishes, unheard. On the loop."""
        for future in list(self._checks):
            future.cancel()

    def close(self) -> None:
        """Stop the worker threads after the passes under way; queued ones are cancelled."""
        self._main.shutdown(wait=False, cancel_futures=True)
        self._spotter.shutdown(wait=False, cancel_futures=True)
