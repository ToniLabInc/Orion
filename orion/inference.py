"""Frugal in-process models: Orion's models share the PC with you, your browser and everything else.

- Thread caps (`cpu_threads`): a model's burst of work gets half the cores, at most 4.
- ONNX Runtime sessions whose threads sleep between runs and keep no memory arena (`onnx_session`). On a 6-core
  laptop Kokoro took 11.6 CPU-seconds per sentence, plus 3 a second while idle, with ONNX's defaults; 4.3 and
  nothing in between this way. The arena kept 400 MB of its biggest buffers for good.
- Freed heap handed back to the system (`release_memory`, `release_memory_async` off the event loop): loading Whisper
  onto the GPU leaves its 800 MB copy in RAM, freed but kept by glibc.
- The text embedder shared by memory, tool routing and the hub (`Embedder`: model2vec's StaticModel, int8), tens of
  microseconds a sentence on one CPU thread, with no GPU and no server.
- The files the local models need and their atomic downloads (`ModelFile`, `registry`, `ensure`), and the Whisper
  models' folders (`whisper_path`, `whisper_ready`).

asyncio (55 ms to import), model2vec, onnxruntime, faster_whisper and httpx load inside the functions that use them.
Settings come in as arguments: performance.cpu_threads as a number, the models.* values with the config.
"""

from __future__ import annotations

import ctypes
import functools
import logging
import os
import shutil
import threading
import warnings
from collections import OrderedDict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

import numpy as np

if TYPE_CHECKING:
    import httpx
    import onnxruntime
    from numpy.typing import NDArray

    from orion.config import Config

log = logging.getLogger("orion.inference")

type Quantize = Literal["int8", "float16", "float32"]
type ModelGroup = Literal["voice", "wake", "embeddings"]
type Progress = Callable[[str, int, int], None]  # (file name, bytes done, bytes in all or 0 when unknown)
type Loader = Callable[[Path, Quantize], StaticEncoder | None]  # (model folder, precision) -> the model, or None

MAX_THREADS: Final = 4  # a model's burst of work never takes more cores than this
QUERY_CACHE: Final = 256  # encode_query remembers this many recent queries
NORM_FLOOR: Final = 1e-12  # rows shorter than this (empty texts) stay zero instead of dividing by zero
EMBED_FILES: Final = ("config.json", "model.safetensors", "tokenizer.json")  # what model2vec loads from a folder
WAKE_FILES: Final = ("melspectrogram.onnx", "embedding_model.onnx")  # what an openWakeWord model runs on
HF_URL: Final = "https://huggingface.co"
DOWNLOAD_TIMEOUT_S: Final = 30.0  # the longest silence from a server while downloading
DOWNLOAD_CONNECT_S: Final = 10.0
CHUNK: Final = 1 << 20  # bytes written per step of a download


# ---------------------------------------------------------------------------------------------------- threads, memory
@functools.cache
def _auto_threads() -> int:
    """Half the cores this process may run on, at least 1 and at most MAX_THREADS."""
    try:
        cores = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):  # not Linux
        cores = os.cpu_count() or 2
    return max(1, min(MAX_THREADS, cores // 2))


def cpu_threads(setting: int) -> int:
    """Threads for a model's burst of work; call it when the model is made, with the setting as it is then.

    Args:
        setting: performance.cpu_threads (0 = automatic).

    Returns:
        The setting when positive, otherwise half the cores, at least 1 and at most 4.
    """
    return setting if setting > 0 else _auto_threads()


def onnx_session(path: Path, threads: int) -> onnxruntime.InferenceSession:
    """An ONNX Runtime CPU session whose threads sleep between runs and that keeps no memory arena.

    Args:
        path: The .onnx model.
        threads: Threads for one run (`cpu_threads(...)`).

    Returns:
        The session.

    Raises:
        Exception: ONNX Runtime's errors for a missing or broken model file.
    """
    import onnxruntime

    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = max(1, threads)
    options.inter_op_num_threads = 1
    options.enable_cpu_mem_arena = False
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    options.add_session_config_entry("session.inter_op.allow_spinning", "0")
    return onnxruntime.InferenceSession(str(path), sess_options=options, providers=["CPUExecutionProvider"])


@functools.cache
def _malloc_trim() -> Callable[[int], int] | None:
    """glibc's malloc_trim, looked up once; None on a C library without it."""
    try:
        return ctypes.CDLL("libc.so.6").malloc_trim
    except (OSError, AttributeError):  # not glibc
        return None


def release_memory() -> None:
    """Hand memory freed by Python and the models back to the system (glibc keeps it otherwise; elsewhere a no-op).

    It walks the whole heap: async code calls `release_memory_async` instead.
    """
    if (trim := _malloc_trim()) is not None:
        trim(0)


async def release_memory_async() -> None:
    """`release_memory` in a worker thread, so the event loop never waits for the heap walk."""
    import asyncio  # already loaded wherever a coroutine runs; importing it at the top would cost every importer

    await asyncio.to_thread(release_memory)


# ---------------------------------------------------------------------------------------------------- the embedder
class StaticEncoder(Protocol):
    """The part of model2vec's StaticModel the embedder uses (tests pass a fake)."""

    @property
    def dim(self) -> int:
        """The width of each vector."""
        ...

    def encode(self, sentences: list[str], *, use_multiprocessing: bool) -> NDArray[Any]:
        """One row per sentence (the mean of its token vectors; zeros for an empty one).

        Args:
            sentences: The texts.
            use_multiprocessing: Whether model2vec may fork workers for a huge batch.

        Returns:
            A (len(sentences), dim) array.
        """
        ...


def embed_folder(models_dir: Path, repo: str) -> Path:
    """Where an embedding model lives: <models_dir>/embed/<name>, for the Hugging Face repo id `repo`."""
    return models_dir / "embed" / repo.rsplit("/", 1)[-1]


def _has_files(folder: Path, names: Iterable[str]) -> bool:
    """True if each of the named files is in the folder."""
    return all((folder / name).is_file() for name in names)


def _load_model2vec(folder: Path, quantize: Quantize) -> StaticEncoder | None:
    """model2vec's StaticModel from its folder, never from the internet; None while its files are missing.

    Args:
        folder: The model's folder.
        quantize: The precision its vectors are kept in.

    Returns:
        The model, or None until `orion setup` has downloaded it.

    Raises:
        Exception: model2vec's errors for broken files.
    """
    if not _has_files(folder, EMBED_FILES):  # model2vec fetches a folder it can't find from the Hub
        return None
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")  # batches here are small: no idle thread per core
    from model2vec import StaticModel

    return StaticModel.from_pretrained(folder, quantize_to=quantize, force_download=False)


class Embedder:
    """The one text embedder per process: loaded on first use, thread-safe, remembering recent queries.

    Vectors are float32 rows of unit length (zeros for an empty text), so a dot product is a cosine. Without the
    model every method answers None and memory search goes by keywords.
    """

    def __init__(
        self, models_dir: Path, repo: str, quantize: Quantize = "int8", *, loader: Loader | None = None
    ) -> None:
        """Prepare the embedder; nothing loads until it is first used (or `load()` is called).

        Args:
            models_dir: The models folder (data/models); the model lives in its embed/ folder.
            repo: models.embedding_repo, such as "minishlab/potion-retrieval-32M".
            quantize: The precision of the model's vectors (int8: one global scale, so cosines are unchanged).
            loader: Loads the model from its folder, or returns None while it is missing (model2vec by default;
                tests pass a fake).
        """
        self.repo = repo
        self.quantize = quantize
        self.folder = embed_folder(models_dir, repo)
        self._id = f"{repo.rsplit('/', 1)[-1]}-{quantize}-v2"
        self._loader: Loader = loader or _load_model2vec
        self._model: StaticEncoder | None = None
        self._broken = False
        self._told_missing = False
        self._load_lock = threading.Lock()
        self._queries: OrderedDict[str, NDArray[np.float32]] = OrderedDict()
        self._queries_lock = threading.Lock()

    @classmethod
    def from_config(cls, cfg: Config) -> Embedder:
        """The embedder of these settings: models.embedding_repo, in the models folder."""
        return cls(cfg.models_dir, cfg.models.embedding_repo)

    @property
    def id(self) -> str:
        """Which vectors this makes ("potion-retrieval-32M-int8-v2"), "" without a model; a change re-embeds memory."""
        if self._broken:
            return ""
        return self._id if self._model is not None or _has_files(self.folder, EMBED_FILES) else ""

    @property
    def loaded(self) -> bool:
        """True once the model is in memory (before that, a call may load it: up to a second)."""
        return self._model is not None

    @property
    def dim(self) -> int:
        """The width of each vector (512 for the potion models), loading the model if needed; 0 without one."""
        return 0 if (model := self._get()) is None else int(model.dim)

    def load(self) -> bool:
        """Load the model now unless it is loaded (the runtime does this in a worker thread); True when ready."""
        return self._get() is not None

    def _get(self) -> StaticEncoder | None:
        """The model, loaded once by the first thread to ask while the others wait; None without one.

        A missing model is looked for again on the next call (a download may have finished since); one that failed
        to load is not tried again.
        """
        if (model := self._model) is not None:
            return model
        with self._load_lock:
            if self._model is None and not self._broken:
                try:
                    self._model = self._loader(self.folder, self.quantize)
                except Exception:  # model2vec, safetensors and tokenizers raise many kinds
                    log.exception("could not load the embedding model in %s: memory search goes by keywords",
                                  self.folder)
                    self._broken = True
                if self._model is None and not self._broken and not self._told_missing:
                    log.info("no embedding model in %s: memory search goes by keywords (orion setup)", self.folder)
                    self._told_missing = True
            return self._model

    def encode(self, texts: Sequence[str]) -> NDArray[np.float32] | None:
        """Embed texts: the synchronous bulk path.

        Args:
            texts: The texts.

        Returns:
            A (len(texts), dim) float32 array of unit rows (zeros for empty texts), or None without a model.
        """
        if (model := self._get()) is None:
            return None
        if not texts:
            return np.zeros((0, int(model.dim)), dtype=np.float32)
        rows = np.array(model.encode(list(texts), use_multiprocessing=False), dtype=np.float32, ndmin=2)
        norms = np.linalg.norm(rows, axis=1, keepdims=True)
        np.maximum(norms, NORM_FLOOR, out=norms)
        rows /= norms
        return rows

    def encode_query(self, text: str) -> NDArray[np.float32] | None:
        """Embed one query, remembering the last QUERY_CACHE of them.

        A remembered query costs a dictionary lookup, a new one tens of microseconds once the model is loaded. On
        the event loop, call it once `loaded` is true (or accept the one-time load).

        Args:
            text: The query.

        Returns:
            A read-only (dim,) float32 unit vector (zeros for an empty text), or None without a model.
        """
        with self._queries_lock:
            if (hit := self._queries.get(text)) is not None:
                self._queries.move_to_end(text)
                return hit
        if (rows := self.encode((text,))) is None:
            return None
        vector: NDArray[np.float32] = rows[0]
        vector.flags.writeable = False  # every caller of the same query shares it
        with self._queries_lock:
            self._queries[text] = vector
            if len(self._queries) > QUERY_CACHE:
                self._queries.popitem(last=False)
        return vector


# ---------------------------------------------------------------------------------------------------- model files
@dataclass(frozen=True, slots=True)
class ModelFile:
    """One downloadable file: where it comes from (`url`) and where it is kept (`dest`)."""

    url: str
    dest: Path

    @property
    def present(self) -> bool:
        """True when the file is on disk and not empty."""
        try:
            return self.dest.stat().st_size > 0
        except OSError:
            return False


def registry(cfg: Config) -> dict[ModelGroup, list[ModelFile]]:
    """Every file the local models need, by what it is for (Whisper's models are folders: `whisper_path`).

    Args:
        cfg: The settings (models.*, voice.wake_model and the models folder).

    Returns:
        "voice": Kokoro's model and voices; "wake": openWakeWord's two support models, only with a wake model of
        the user's own; "embeddings": the embedder's files from the Hugging Face Hub.
    """
    d, m = cfg.models_dir, cfg.models
    kokoro, wake = m.kokoro_url.rstrip("/"), m.openwakeword_url.rstrip("/")
    embed, hub = embed_folder(d, m.embedding_repo), f"{HF_URL}/{m.embedding_repo}/resolve/main"
    return {
        "voice": [ModelFile(f"{kokoro}/{name}", d / name) for name in (m.kokoro_model, m.kokoro_voices)],
        "wake": [ModelFile(f"{wake}/{name}", d / "openwakeword" / name) for name in WAKE_FILES if cfg.voice.wake_model],
        "embeddings": [ModelFile(f"{hub}/{name}", embed / name) for name in EMBED_FILES],
    }


def ensure(
    files: Iterable[ModelFile],
    progress: Progress | None = None,
    *,
    client: httpx.Client | None = None,
    timeout_s: float = DOWNLOAD_TIMEOUT_S,
) -> list[Path]:
    """Download the missing files one after the other (blocking: async code runs it in a worker thread).

    Each download goes to <name>.part beside the file and is renamed into place once complete and on disk, so no
    file is ever half there.

    Args:
        files: The files that should exist.
        progress: Told (file name, bytes done, bytes in all) as each download goes.
        client: The HTTP client (tests pass one on httpx.MockTransport); one is made otherwise.
        timeout_s: The longest silence from the server.

    Returns:
        The files downloaded; empty when all were there.

    Raises:
        httpx.HTTPError: A download failed (nothing is left half-written).
        OSError: A file could not be written, or a server sent less than it announced.
    """
    if not (todo := [f for f in files if not f.present]):
        return []
    import httpx

    http = client or httpx.Client(timeout=httpx.Timeout(timeout_s, connect=DOWNLOAD_CONNECT_S))
    try:
        for item in todo:
            _download(http, item, progress)
    finally:
        if client is None:
            http.close()
    return [item.dest for item in todo]


def _download(http: httpx.Client, item: ModelFile, progress: Progress | None) -> None:
    """Download one file to a .part file, flush it to disk, then rename it into place; the .part goes on failure.

    Raises:
        httpx.HTTPError: The download failed.
        OSError: The file could not be written, or the server sent less than it announced.
    """
    item.dest.parent.mkdir(parents=True, exist_ok=True)
    part = item.dest.with_name(item.dest.name + ".part")
    label = item.dest.name
    try:
        with (  # identity encoding: the announced length is then the file's, so a cut-short download shows
            http.stream("GET", item.url, follow_redirects=True, headers={"Accept-Encoding": "identity"}) as response,
            part.open("wb") as out,
        ):
            response.raise_for_status()
            total, done = int(response.headers.get("Content-Length") or 0), 0
            if progress is not None:
                progress(label, 0, total)
            for chunk in response.iter_bytes(CHUNK):
                out.write(chunk)
                done += len(chunk)
                if progress is not None:
                    progress(label, done, total)
            out.flush()
            os.fsync(out.fileno())
        if total and done != total:
            raise OSError(f"{label}: the download stopped at {done} of {total} bytes")
        part.replace(item.dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise


def whisper_ready(models_dir: Path, name: str) -> bool:
    """Whether a Whisper model is downloaded: its folder (see `whisper_path`) holds its model.bin."""
    return (_whisper_folder(models_dir, name) / "model.bin").is_file()


def whisper_path(models_dir: Path, name: str) -> Path:
    """The folder of a Whisper model, downloaded by faster-whisper the first time it is needed.

    A download goes to <folder>.part and is renamed into place once complete.

    Args:
        models_dir: The models folder (data/models).
        name: A model faster-whisper knows ("small.en", "large-v3-turbo"…) or the repo id of a CTranslate2 conversion.

    Returns:
        The model's folder, <models_dir>/whisper/<name> ("/" in a repo id written "--").

    Raises:
        ValueError: The name is neither a known model nor a repo id.
        Exception: huggingface_hub's errors when the download fails (no internet, say).
    """
    folder = _whisper_folder(models_dir, name)
    if whisper_ready(models_dir, name):
        return folder
    from faster_whisper.utils import download_model

    # faster-whisper passes huggingface_hub an argument it no longer uses, which only warns.
    warnings.filterwarnings("ignore", "The `local_dir_use_symlinks` argument is deprecated")
    part = folder.with_name(folder.name + ".part")
    shutil.rmtree(part, ignore_errors=True)
    log.info("downloading the speech recognition model %s", name)
    try:
        download_model(name, output_dir=str(part))
        shutil.rmtree(folder, ignore_errors=True)
        part.rename(folder)
    except BaseException:
        shutil.rmtree(part, ignore_errors=True)
        raise
    return folder


def _whisper_folder(models_dir: Path, name: str) -> Path:
    """Where a Whisper model lives: <models_dir>/whisper/<name>, a repo id's "/" written "--"."""
    return models_dir / "whisper" / name.replace("/", "--")
