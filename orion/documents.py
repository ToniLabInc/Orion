"""Your documents: folders (an Obsidian vault, a pile of PDFs) he can search and read.

What it does. ``documents.folders`` are scanned at start and watched (``watchfiles``). A file is read again only when
its inode, size or time changed. Markdown and text are read as they are; PDF, Word, PowerPoint, Excel, HTML, CSV, JSON
and EPUB are turned into Markdown by markitdown in worker processes (``orion.docworker``; a PDF's text alone, unless
``documents.pdf_tables``: markitdown's table detection takes fourteen times as long), a few documents per process
because starting one costs more than reading a document. The text is cut into pieces at headings and paragraphs (each
piece knows the headings above it, and where it is in the document), and the pieces are searched by keywords (SQLite
FTS5, BM25) and by meaning (the memory's embedder), fused by reciprocal rank like the memory search.

Security. A document is untrusted twice over. Its words may be an instruction aimed at the model: every tool that
returns them taints the turn (``tools/documents.py``), so a risky action after reading one needs the user's yes. And
the bytes are input to parsers written in C and Python: they are converted in a process that has no business with
anything of Orion's (``launch`` is the one place that confines it: a clean environment, an address-space limit, no files
written, the lowest priority, a time limit, killed as a group; a sandbox without network or file system replaces its
body when ``orion.sandbox`` exists), that opens each file without following a link and checks that it is the one the
scan saw. Nothing is ever read through a link: a link inside a folder is not followed, and ``read`` takes its text from
the index, so no path the model names can reach a file the scan did not. Orion's own data folder and the places keys
live (``secret_places``) are never read, whatever the folders say.

The index is its own file, ``data/documents.db``, because it is large, written to in bursts, and made again from your
files: it stays out of the memory's database (a long write never queues behind a message being logged), out of the
backups, and it is simply made afresh when it is damaged or Orion's way of reading changes.

Search at 200 000 pieces, in numbers. Keywords: FTS5 with positions, the query's words that sit in more than a tenth of
the pieces left out (their score is near nothing and their lists are most of the cost). Meaning: each piece's vector is
kept as 512 int8 values in the file and as 512 sign bits in memory (64 bytes: 13 MB for 200 000 pieces); a query is
compared with every sign code by Hamming distance (one pass over 13 MB), the nearest 2 % are re-ranked by the query's
own values against their sign bits, the best 400 by exact cosine over their int8 values. Against exact cosine over all
the vectors that finds 97-98 % of the best ten, in a twentieth of the time and a thirtieth of the memory.
"""

import asyncio
import codecs
import contextlib
import dataclasses
import fnmatch
import itertools
import json
import logging
import os
import re
import sqlite3
import stat
import sys
import threading
import time
from collections.abc import Callable, Collection, Iterable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final

import numpy as np

from orion.base import JSON, OrionError, Publish, lower_priority
from orion.host import profile
from orion.memory.index import content_terms, rrf
from orion.sandbox import SandboxError, jail_for, wrap

if TYPE_CHECKING:
    from numpy.typing import NDArray

    from orion.config import Config, DocumentsConfig
    from orion.inference import Embedder

log = logging.getLogger("orion.documents")

type Record = Callable[..., None]  # ``Activity.record``

SCHEMA_VERSION: Final = 1  # the index's layout (PRAGMA user_version); another one is made again, not upgraded
DB_NAME: Final = "documents.db"
DIRECT: Final = frozenset({"md", "markdown", "txt", "text"})  # read as they are; everything else goes to markitdown
BATCH_FILES: Final = 16  # documents one worker process converts before it is replaced...
BATCH_BYTES: Final = 64 << 20  # ...or this many bytes of them
QUEUE_DOCS: Final = 32  # converted documents waiting to be indexed (the converters wait when it is full)
WRITE_CHARS: Final = 2 << 20  # text indexed in one go at most (many small documents together, a big one alone)
EMBED_BATCH: Final = 256  # pieces embedded per call
INDEX_NICE: Final = 10
WATCH_DEBOUNCE_MS: Final = 1500  # changes this close together are one change
EVENT_EVERY_S: Final = 0.5  # the HUD hears of progress at most this often
STARTUP_S: Final = 60  # what a worker may take to start, on top of its documents
SILENCE_GRACE_S: Final = 15  # a worker silent for a document's time limit plus this is hung
KILL_WAIT_S: Final = 5  # how long a worker that was ended is given to close its pipes
POOL_SHARE: Final = 0.02  # meaning search: the nearest share of the sign codes re-ranked...
POOL_MIN: Final = 1024
POOL_MAX: Final = 20000
REFINE: Final = 400  # ...of which this many get an exact cosine
BLOCK: Final = 1 << 16  # sign codes compared per step (it stays in the processor's cache)
PRUNE_SHARE: Final = 0.1  # keyword search: query words in more than this share of the pieces are left out
MAX_TERMS: Final = 8  # keyword search: at most this many query words
PER_FILE: Final = 2  # results of one file in a search
WORKER: Final = (sys.executable, "-I", "-B", "-m", "orion.docworker")  # isolated: no user site, no PYTHONPATH, no .pyc
WORKER_ENV: Final = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/nonexistent", "PYTHONIOENCODING": "utf-8",
                     "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
                     "NUMEXPR_NUM_THREADS": "1", "MALLOC_ARENA_MAX": "2", "TOKENIZERS_PARALLELISM": "false"}

_CONTROL: Final = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TRAILING: Final = re.compile(r"[ \t]+$", re.MULTILINE)
_BLANKS: Final = re.compile(r"\n{3,}")
_HEADING: Final = re.compile(r"(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
_FENCE: Final = re.compile(r"(`{3,}|~{3,})")
_SENTENCE_END: Final = re.compile(r"[.!?…][\"')\]]*\s")


class DocumentsError(OrionError):
    """Something about the documents that the user can act on; the message says what, in words."""


# ---------------------------------------------------------------------------------------------------- shapes
@dataclass(frozen=True, slots=True)
class Chunk:
    """A piece of a document.

    Attributes:
        start: where it begins in the document's text.
        stop: where it ends (exclusive); the piece is exactly ``text[start:stop]``.
        heading: where it is: the document's title, then the headings above it ("Plans › Drone › Tuning").
        text: its text.
    """

    start: int
    stop: int
    heading: str
    text: str


@dataclass(frozen=True, slots=True)
class Hit:
    """One piece found.

    Attributes:
        path: its document's whole path.
        folder: the folder it belongs to.
        heading: where in the document it is.
        text: its text.
        start: where it begins in the document's text (``Documents.read`` starts there).
        stop: where it ends.
        score: its fused rank score (higher is better).
    """

    path: str
    folder: str
    heading: str
    text: str
    start: int
    stop: int
    score: float


@dataclass(frozen=True, slots=True)
class Page:
    """A stretch of a document's text.

    Attributes:
        path: the document's whole path.
        text: the text.
        start: where it begins.
        stop: where it ends.
        total: how long the document's text is.
    """

    path: str
    text: str
    start: int
    stop: int
    total: int


@dataclass(frozen=True, slots=True)
class _Job:
    """A file to read: what the scan saw of it."""

    folder: int
    rel: str
    path: Path
    ext: str
    size: int
    mtime_ns: int
    ino: int


@dataclass(slots=True)
class _Doc:
    """A document read: its text, or why not."""

    job: _Job
    text: str = ""
    error: str = ""
    retry: int = 0  # 0 never again until it changes; 1 at the next start; 2 when the settings change


@dataclass(slots=True)
class _Folder:
    """A folder of the settings: where it really is, how the settings write it, and why it can't be used (empty when
    it can)."""

    path: Path
    shown: str
    error: str = ""
    id: int = 0


@dataclass(slots=True)
class _Plan:
    """What a scan found to do."""

    jobs: list[_Job] = field(default_factory=list)
    broken: list[_Doc] = field(default_factory=list)  # files that can't be read at all (too large...)
    gone: list[tuple[int, str, bool]] = field(default_factory=list)  # (folder, path, whether it is a folder's prefix)


# ---------------------------------------------------------------------------------------------------- text
def decode_text(data: bytes) -> str:
    """A text file's characters: UTF-8 (with or without its byte order mark), else whatever its bytes look like (a
    Greek or Western Windows code page, UTF-16)."""
    data = data.removeprefix(codecs.BOM_UTF8)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        from charset_normalizer import from_bytes

        best = from_bytes(data).best()
        return str(best) if best is not None else data.decode("utf-8", "replace")


def normalize_text(text: str) -> str:
    """A document's text as it is indexed: no carriage returns or control characters, no trailing spaces, at most one
    blank line in a row, nothing before the first or after the last line."""
    text = _CONTROL.sub("", text.replace("\r\n", "\n").replace("\r", "\n").replace("\x0c", "\n\n"))
    return _BLANKS.sub("\n\n", _TRAILING.sub("", text)).strip("\n")


@dataclass(slots=True)
class _Block:
    """A stretch of a document that is never cut inside (a paragraph, a code block, a heading)."""

    start: int
    stop: int
    path: tuple[str, ...]  # the headings above it
    level: int = 0  # a heading's level, 0 for anything else


def _blocks(text: str) -> list[_Block]:
    """Cut a document into blocks that together are all of it: a heading is a block, a fenced code block is one block
    whatever blank lines it holds, and the rest are runs of lines between blank ones. Each block runs on to the next
    one's start, so that joining the blocks gives the text back."""
    blocks: list[_Block] = []
    stack: list[tuple[int, str]] = []
    path: tuple[str, ...] = ()
    fence = ""
    start = -1  # where the block in progress began (-1: none)
    pos = 0
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\n")
        end = pos + len(line)
        if fence:
            if body.lstrip().startswith(fence) and not body.lstrip().strip(fence[0]):
                fence = ""
            pos = end
            continue
        if not body.strip():
            if start >= 0:
                blocks.append(_Block(start, pos, path))
                start = -1
            pos = end
            continue
        heading = _HEADING.fullmatch(body)
        opening = _FENCE.match(body.lstrip())
        if heading or opening:
            if start >= 0:
                blocks.append(_Block(start, pos, path))
            start = -1
        if heading:
            level = len(heading.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, heading.group(2).strip()[:120]))
            path = tuple(title for _, title in stack)
            blocks.append(_Block(pos, end, path, level))
        elif opening:
            fence = opening.group(1)
            start = pos
        elif start < 0:
            start = pos
        pos = end
    if start >= 0:
        blocks.append(_Block(start, pos, path))
    for before, after in itertools.pairwise(blocks):
        before.stop = after.start
    if blocks:
        blocks[0].start = 0
        blocks[-1].stop = len(text)
    return blocks


def _cut(text: str, start: int, stop: int, size: int) -> list[tuple[int, int]]:
    """Cut a stretch of text longer than ``size`` into stretches of at most ``size``, at line ends, then sentence ends,
    then spaces, and in the middle of a word only when it has no spaces at all."""
    pieces: list[tuple[int, int]] = []
    content = start + len(text[start:stop].rstrip())  # the blank lines that end the stretch ride on its last piece
    while content - start > size:
        limit, floor = start + size, start + size // 2
        cut = text.rfind("\n", floor, limit) + 1
        if not cut:
            ends = [m.end() for m in _SENTENCE_END.finditer(text, floor, limit)]
            cut = ends[-1] if ends else text.rfind(" ", floor, limit) + 1
        cut = cut or limit
        pieces.append((start, cut))
        start = cut
    pieces.append((start, stop))
    return pieces


def chunk_markdown(text: str, title: str, size: int, overlap: int) -> list[Chunk]:
    """Cut a document into pieces for searching.

    The pieces are exactly slices of the text and together cover all of it, in order: ``read`` gives the text back from
    them. A piece is at most ``size`` characters, not counting the blank lines that may end it. It ends at a block boundary (a paragraph, a heading, a code block) when
    it can, never inside a code block that fits, starts a new section at a heading of level one or two once it is a
    quarter full, and begins with the last blocks of the piece before as far as ``overlap`` characters allow.

    Args:
        text: the document's text (``normalize_text``).
        title: the document's name, which heads every piece's heading path.
        size: the most characters of a piece (``documents.chunk_chars``).
        overlap: how much of a piece's end the next one repeats (``documents.chunk_overlap``).

    Returns:
        The pieces; none for a document with no text.
    """
    if not text.strip():
        return []
    blocks: list[_Block] = []
    for block in _blocks(text):
        if block.stop - block.start <= size:
            blocks.append(block)
            continue
        for first, last in _cut(text, block.start, block.stop, size):
            blocks.append(_Block(first, last, block.path, block.level))
    chunks: list[Chunk] = []
    a = fresh = 0  # a piece begins at block a; the blocks from `fresh` on are not in the piece before
    while a < len(blocks):
        b = a
        while b + 1 < len(blocks) and blocks[b + 1].stop - blocks[a].start <= size:
            major = 0 < blocks[b + 1].level <= 2
            if major and b >= fresh and blocks[b].stop - blocks[fresh].start >= size // 4:
                break  # a new section, and this piece has a quarter of its own already
            b += 1
        first, last = blocks[a].start, blocks[b].stop
        chunks.append(Chunk(first, last, _heading(title, blocks[a].path), text[first:last]))
        if b + 1 >= len(blocks):
            break
        j = fresh = b + 1
        while j - 1 > a and blocks[b].stop - blocks[j - 1].start <= overlap:
            j -= 1
        while j < b + 1 and blocks[b + 1].stop - blocks[j].start > size:  # room for the next block, or it adds nothing
            j += 1
        a = j
    return chunks


def _heading(title: str, path: tuple[str, ...]) -> str:
    """"Title › Section › Part": the headings above a piece after the document's title, a heading that repeats the line
    before it left out."""
    names = [title]
    for name in path:
        if name.casefold() != names[-1].casefold():
            names.append(name)
    return " › ".join(names)[:300]


def excerpt(text: str, terms: Sequence[str], limit: int) -> str:
    """At most ``limit`` characters of a piece: all of it when it fits, else the stretch around the first of the
    query's words that it holds (the start when it holds none), cut at spaces, with … where it was cut."""
    if len(text) <= limit:
        return text
    lowered = text.lower()
    hits = [i for term in terms if (i := lowered.find(term)) >= 0]
    start = max(0, min(hits) - limit // 4) if hits else 0
    if start:
        start = text.find(" ", start, start + limit // 4) + 1 or start
    stop = min(len(text), start + limit)
    if stop < len(text):
        stop = text.rfind(" ", start + limit // 2, stop) + 1 or stop
    return ("…" if start else "") + text[start:stop].strip() + ("…" if stop < len(text) else "")


# ---------------------------------------------------------------------------------------------------- vectors
def quantize(vectors: "NDArray[np.float32]") -> list[bytes]:
    """Vectors as stored: a float32 scale and the values as int8 (the largest becomes ±127)."""
    scale = np.abs(vectors).max(axis=1, keepdims=True) / 127.0
    scale[scale == 0] = 1.0
    values = np.round(vectors / scale).astype(np.int8)
    return [s.astype(np.float32).tobytes() + row.tobytes() for s, row in zip(scale, values, strict=True)]


def dequantize(blobs: Sequence[bytes]) -> "NDArray[np.float32]":
    """The vectors ``quantize`` made, as float32 rows."""
    raw = np.frombuffer(b"".join(blobs), dtype=np.uint8).reshape(len(blobs), -1)
    return raw[:, 4:].view(np.int8).astype(np.float32) * raw[:, :4].copy().view(np.float32)


def sign_codes(vectors: "NDArray[np.float32]") -> "NDArray[np.uint64]":
    """The sign of each value as a bit, a vector to 64-bit words (the last word padded with zeros)."""
    bits = vectors > 0
    if pad := -bits.shape[1] % 64:
        bits = np.pad(bits, ((0, 0), (0, pad)))
    return np.packbits(bits, axis=1).view(np.uint64)


_PAIRS: Final = np.uint64(0x00FF00FF00FF00FF)  # the even bytes of a word, each in a lane of 16 bits
_LANES: Final = np.uint64(0x0001000100010001)  # multiplying by it adds the four lanes up into the top one


def hamming(codes: "NDArray[np.uint64]", code: "NDArray[np.uint64]") -> "NDArray[np.uint16]":
    """In how many bits each row of ``codes`` differs from ``code``.

    Block by block, so that a block stays in the processor's cache. For the usual 512-bit codes (eight words) the eight
    bit counts of a row, a byte each, are packed in one word and added in it, which takes half the time of summing the
    rows with numpy.
    """
    n, words = codes.shape
    distance = np.empty(n, dtype=np.uint16)
    work = np.empty((min(n, BLOCK), words), dtype=np.uint64)
    for low in range(0, n, BLOCK):
        high = min(n, low + BLOCK)
        xor = work[:high - low]
        np.bitwise_xor(codes[low:high], code, out=xor)
        counts = np.bitwise_count(xor)
        if words == 8:
            packed = counts.view(np.uint64).reshape(-1)
            pairs = (packed & _PAIRS) + ((packed >> np.uint64(8)) & _PAIRS)
            distance[low:high] = (pairs * _LANES) >> np.uint64(48)
        else:
            distance[low:high] = counts.sum(axis=1, dtype=np.uint16)
    return distance


def nearest_rows(distance: "NDArray[np.uint16]", count: int, farthest: int) -> "NDArray[np.intp]":
    """The rows of the ``count`` smallest distances (ties at the last distance: the earliest rows), without sorting: the
    distances are small whole numbers, so counting them finds the distance to stop at.

    Args:
        distance: one per row; rows to leave out hold more than any real distance.
        count: how many rows (at most as many as there are real distances).
        farthest: the largest distance a row can hold, those left out included.
    """
    stop = int(np.searchsorted(np.cumsum(np.bincount(distance, minlength=farthest + 1)), count))
    closer = np.flatnonzero(distance < stop)
    return np.concatenate([closer, np.flatnonzero(distance == stop)[:count - len(closer)]])


def _tables(query: "NDArray[np.float32]", nbytes: int) -> "NDArray[np.float32]":
    """For each byte of a sign code and each of its 256 values, the sum of the query's values at the bits that are set."""
    padded = np.zeros(nbytes * 8, dtype=np.float32)
    padded[:query.shape[0]] = query
    pattern = ((np.arange(256)[:, None] >> np.arange(7, -1, -1)[None, :]) & 1).astype(np.float32)
    return padded.reshape(nbytes, 8) @ pattern.T


class _Codes:
    """Every piece's sign code in memory (ids ascending: they are never reused), with deleted rows masked."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.size = 0
        self.dead = 0
        self.ids = np.zeros(0, dtype=np.int64)
        self.folder = np.zeros(0, dtype=np.int32)
        self.codes = np.zeros((0, 0), dtype=np.uint64)
        self.alive = np.zeros(0, dtype=bool)

    @property
    def count(self) -> int:
        """How many pieces are searchable."""
        return self.size - self.dead

    def reset(self, capacity: int, words: int) -> None:
        """Forget everything and make room for ``capacity`` pieces of ``words`` 64-bit words."""
        with self.lock:
            self._allocate(capacity, words)

    def _allocate(self, capacity: int, words: int) -> None:
        """Empty arrays for ``capacity`` pieces (the lock is held)."""
        self.size = self.dead = 0
        self.ids = np.zeros(capacity, dtype=np.int64)
        self.folder = np.zeros(capacity, dtype=np.int32)
        self.codes = np.zeros((capacity, words), dtype=np.uint64)
        self.alive = np.zeros(capacity, dtype=bool)

    def add(self, ids: "NDArray[np.int64]", folders: "NDArray[np.int32]", codes: "NDArray[np.uint64]") -> None:
        """Append pieces (ids above every id so far)."""
        with self.lock:
            if self.codes.shape[1] != codes.shape[1]:
                self._allocate(max(len(ids), 1024), codes.shape[1])  # another vector width: nothing before it fits
            need = self.size + len(ids)
            if need > len(self.ids):
                room = max(need, len(self.ids) * 2, 1024)
                for name in ("ids", "folder", "alive"):
                    old = getattr(self, name)
                    grown = np.zeros(room, dtype=old.dtype)
                    grown[:self.size] = old[:self.size]
                    setattr(self, name, grown)
                grown_codes = np.zeros((room, codes.shape[1]), dtype=np.uint64)
                grown_codes[:self.size] = self.codes[:self.size]
                self.codes = grown_codes
            low = self.size
            self.ids[low:need], self.folder[low:need], self.codes[low:need] = ids, folders, codes
            self.alive[low:need] = True
            self.size = need

    def remove(self, ids: Sequence[int]) -> None:
        """Mask pieces; the rows are dropped once a fifth of them is dead."""
        if not len(ids):
            return
        with self.lock:
            if not self.size:
                return
            wanted = np.asarray(ids, dtype=np.int64)
            rows = np.searchsorted(self.ids[:self.size], wanted)
            found = rows < self.size
            found[found] = self.ids[rows[found]] == wanted[found]
            rows = rows[found]
            rows = rows[self.alive[rows]]
            self.alive[rows] = False
            self.dead += len(rows)
            if self.dead > self.size // 5:
                keep = np.flatnonzero(self.alive[:self.size])
                self.ids[:len(keep)], self.folder[:len(keep)] = self.ids[keep], self.folder[keep]
                self.codes[:len(keep)] = self.codes[keep]
                self.alive[:len(keep)], self.alive[len(keep):self.size] = True, False
                self.size, self.dead = len(keep), 0

    def nearest(self, query: "NDArray[np.float32]", folder: int | None) -> "NDArray[np.int64]":
        """The ids of the pieces whose vectors are probably nearest to the query's, best first: by Hamming distance
        between sign codes, then by the query's own values against the sign bits (see the module docstring); the exact
        cosine of these is the caller's to take."""
        with self.lock:
            n = self.size
            if not self.count or self.codes.shape[1] * 64 < query.shape[0]:
                return np.zeros(0, dtype=np.int64)
            distance = hamming(self.codes[:n], sign_codes(query[None, :])[0])
            usable = self.alive[:n] if folder is None else self.alive[:n] & (self.folder[:n] == folder)
            farthest = self.codes.shape[1] * 64 + 1  # more than any real distance: the rows left out
            distance[~usable] = farthest
            candidates = int(usable.sum())
            if not candidates:
                return np.zeros(0, dtype=np.int64)
            pool = min(candidates, min(POOL_MAX, max(POOL_MIN, int(self.count * POOL_SHARE))))
            top = nearest_rows(distance, pool, farthest) if pool < candidates else np.flatnonzero(usable)
            if pool > REFINE:
                nbytes = self.codes.shape[1] * 8
                bytes_ = np.ascontiguousarray(self.codes[top]).view(np.uint8)
                score = _tables(query, nbytes)[np.arange(nbytes)[None, :], bytes_].sum(axis=1)
                top = top[np.argpartition(-score, REFINE - 1)[:REFINE]]
            return self.ids[top].copy()


# ---------------------------------------------------------------------------------------------------- the file
SCHEMA: Final = (
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID",
    "CREATE TABLE doc_folders (id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE)",
    """CREATE TABLE doc_files (
        id INTEGER PRIMARY KEY, folder INTEGER NOT NULL REFERENCES doc_folders(id) ON DELETE CASCADE,
        path TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, ino INTEGER NOT NULL,
        chars INTEGER NOT NULL DEFAULT 0, chunks INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
        retry INTEGER NOT NULL DEFAULT 0, at REAL NOT NULL, UNIQUE (folder, path))""",
    """CREATE TABLE doc_chunks (
        id INTEGER PRIMARY KEY AUTOINCREMENT, file INTEGER NOT NULL REFERENCES doc_files(id) ON DELETE CASCADE,
        ord INTEGER NOT NULL, start INTEGER NOT NULL, stop INTEGER NOT NULL, folder INTEGER NOT NULL,
        heading TEXT NOT NULL, text TEXT NOT NULL)""",
    "CREATE INDEX doc_chunks_file ON doc_chunks(file, ord)",
    "CREATE TABLE doc_vecs (id INTEGER PRIMARY KEY REFERENCES doc_chunks(id) ON DELETE CASCADE, vec BLOB NOT NULL)",
    """CREATE TABLE doc_codes (id INTEGER PRIMARY KEY REFERENCES doc_chunks(id) ON DELETE CASCADE,
        folder INTEGER NOT NULL, code BLOB NOT NULL)""",
    """CREATE VIRTUAL TABLE doc_fts USING fts5(text, heading, folder, content='doc_chunks', content_rowid='id',
        tokenize='porter unicode61 remove_diacritics 2', detail=full)""",
    """CREATE TRIGGER doc_chunks_ai AFTER INSERT ON doc_chunks BEGIN
        INSERT INTO doc_fts(rowid, text, heading, folder) VALUES (new.id, new.text, new.heading, new.folder); END""",
    """CREATE TRIGGER doc_chunks_ad AFTER DELETE ON doc_chunks BEGIN
        INSERT INTO doc_fts(doc_fts, rowid, text, heading, folder)
        VALUES ('delete', old.id, old.text, old.heading, old.folder); END""",
)


class _Db:
    """The index's SQLite file: one write connection (used under a lock, by the indexing threads) and one read-only
    connection per reading thread. A file that is damaged or of another layout is made afresh: it holds nothing that
    your files do not."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.remade = ""  # why the file was made afresh, "" when it was not
        self.generation = 0  # changes when the file is made afresh: readers then connect again
        self._lock = threading.RLock()
        self._local = threading.local()
        self._readers: list[sqlite3.Connection] = []
        self._readers_lock = threading.Lock()
        self._db = self._open()

    def _open(self) -> sqlite3.Connection:
        """Open the file; make it afresh when it is damaged or of another layout."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            db = None
            try:
                db = self._connect(query_only=False)
                version = int(db.execute("PRAGMA user_version").fetchone()[0])
                if version == SCHEMA_VERSION:
                    db.execute("PRAGMA journal_mode = WAL")
                    db.execute("SELECT COUNT(*) FROM doc_files").fetchone()  # a damaged file fails here
                    return db
                self.remade = "it was made by another version of Orion" if version else ""
            except sqlite3.DatabaseError as e:
                self.remade = f"it is damaged ({e})"
            if db is not None:
                with contextlib.suppress(sqlite3.Error):
                    db.close()
            if self.remade:
                log.warning("the documents index is made afresh: %s", self.remade)
        return self._create()

    def _create(self) -> sqlite3.Connection:
        """A new, empty index in place of whatever file there is."""
        for suffix in ("", "-wal", "-shm"):
            Path(f"{self.path}{suffix}").unlink(missing_ok=True)
        db = self._connect(query_only=False)
        db.execute("PRAGMA journal_mode = WAL")
        for statement in SCHEMA:
            db.execute(statement)
        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        return db

    def remake(self) -> None:
        """Throw the index away and start an empty one (readers connect to it at their next read)."""
        with self._lock:
            with contextlib.suppress(sqlite3.Error):
                self._db.close()
            self._db = self._create()
            self.generation += 1

    def _connect(self, *, query_only: bool) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous = NORMAL")
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA temp_store = MEMORY")
        db.execute(f"PRAGMA cache_size = {-32768 if query_only else -65536}")
        if query_only:
            db.execute("PRAGMA query_only = 1")
            db.execute("PRAGMA mmap_size = 536870912")
        return db

    def write[T](self, work: Callable[[sqlite3.Connection], T]) -> T:
        """Run ``work`` in one write transaction (rolled back if it raises)."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                result = work(self._db)
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")
            return result

    @contextlib.contextmanager
    def reading(self) -> Iterator[sqlite3.Connection]:
        """A connection to read with, for the calling thread."""
        db: sqlite3.Connection | None = getattr(self._local, "db", None)
        if db is None or self._local.generation != self.generation:
            if db is not None:
                with self._readers_lock:
                    self._readers.remove(db)
                with contextlib.suppress(sqlite3.Error):
                    db.close()
            db = self._connect(query_only=True)
            self._local.db, self._local.generation = db, self.generation
            with self._readers_lock:
                self._readers.append(db)
        yield db

    def checkpoint(self) -> None:
        """Fold the write-ahead log back into the file."""
        with self._lock:
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()

    def close(self) -> None:
        """Close every connection."""
        with self._lock:
            with contextlib.suppress(sqlite3.Error):
                self._db.execute("PRAGMA optimize")
            self._db.close()
        with self._readers_lock:
            for db in self._readers:
                with contextlib.suppress(sqlite3.Error):
                    db.close()
            self._readers.clear()


# ---------------------------------------------------------------------------------------------------- the worker
@dataclass(frozen=True, slots=True)
class Launched:
    """How a worker process ended.

    Attributes:
        out: what it printed.
        err: what it said on its error output (the tail).
        code: its exit code (negative: the signal that ended it).
        why: "" when it ended by itself; otherwise why it was ended ("it hung", "it printed too much", "stopped").
    """

    out: bytes
    err: bytes
    code: int
    why: str = ""


def confine(cfg: "Config", argv: Sequence[str]) -> list[str]:
    """A worker's command line, kept in the sandbox (``orion.sandbox``, G3): no network and nothing written, the PC
    read-only to it with what is secret hidden, and a clean environment (``WORKER_ENV``). Its documents' folders
    are shown to it again, read-only, wherever they are (``/tmp`` is empty in the sandbox). Inside a container the
    container is the boundary: the command line is returned as it is.

    Args:
        cfg: the settings.
        argv: the worker's command line.

    Returns:
        The command line that runs it.

    Raises:
        SandboxError: this machine can't make a sandbox (a worker is never run bare).
    """
    from orion.config import CONFIG_PATH

    if profile().sandbox == "container":
        return list(argv)
    folders = tuple(Path(os.path.realpath(Path(text).expanduser())) for text in cfg.documents.folders)
    jail = dataclasses.replace(jail_for(cfg, CONFIG_PATH, network=False, write=(), memory_mb=0), read=folders)
    return wrap(argv, jail, env=WORKER_ENV, cwd=Path("/"))


async def launch(argv: Sequence[str], *, silence_s: float, cap: int, procs: set["asyncio.subprocess.Process"]) -> Launched:
    """Run a conversion worker and collect what it prints: its own process group (the whole group is ended), no
    input, a nowhere working folder, and a watchdog (ended when it prints nothing for ``silence_s``, or more than
    ``cap`` bytes). The command line is already confined (``confine``), and the worker limits itself further before
    it reads anything (``orion.docworker``).

    Args:
        argv: the command line.
        silence_s: the longest it may print nothing.
        cap: the most bytes it may print.
        procs: where the process is kept while it runs, so that stopping Orion can end it.

    Returns:
        How it ended.
    """
    process = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                                   stderr=asyncio.subprocess.PIPE, env=dict(WORKER_ENV), cwd="/",
                                                   start_new_session=True)
    procs.add(process)
    out, err = bytearray(), bytearray()
    why = ""

    async def pump(stream: asyncio.StreamReader, into: bytearray, limit: int) -> None:
        # Both pipes are read to their end whatever happens: a process counts as over only when its pipes are closed, and a
        # pipe nobody reads is never closed.
        nonlocal why
        ignoring = False
        while chunk := await stream.read(1 << 16):
            if ignoring:
                continue
            into += chunk
            if len(into) > limit:
                if into is not out:
                    del into[:len(into) - limit]  # the tail of the error output is what matters
                else:
                    why, ignoring = why or "it printed too much", True
                    _kill(process)

    async def watch() -> None:
        nonlocal why
        exited = asyncio.ensure_future(process.wait())
        try:
            seen = len(out)
            while not (await asyncio.wait([exited], timeout=silence_s))[0]:
                if len(out) == seen:
                    why = why or "it hung"
                    _kill(process)
                    return
                seen = len(out)
        finally:
            exited.cancel()

    assert process.stdout is not None and process.stderr is not None
    tasks = [asyncio.ensure_future(pump(process.stdout, out, cap)), asyncio.ensure_future(pump(process.stderr, err, 1 << 16)),
             asyncio.ensure_future(watch())]
    try:
        await asyncio.wait(tasks)
        await process.wait()
    except BaseException:  # ended from outside (cancelled): the process goes, and its pipes close
        _kill(process)
        await asyncio.wait(tasks, timeout=KILL_WAIT_S)
        raise
    finally:
        for task in tasks:
            task.cancel()
        procs.discard(process)
    return Launched(bytes(out), bytes(err), process.returncode if process.returncode is not None else -9, why)


def _kill(process: "asyncio.subprocess.Process") -> None:
    """End a worker and everything it started."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, 9)


def _batches(jobs: Sequence[_Job]) -> Iterator[list[_Job]]:
    """Group documents for one worker each: not too many, not too many bytes."""
    batch: list[_Job] = []
    size = 0
    for job in jobs:
        if batch and (len(batch) >= BATCH_FILES or size + job.size > BATCH_BYTES):
            yield batch
            batch, size = [], 0
        batch.append(job)
        size += job.size
    if batch:
        yield batch


def read_frames(out: bytes) -> tuple[dict[int, dict[str, object]], int]:
    """What a worker printed: each document's result by its number, and the number of the one it had started and not
    finished (-1 when none)."""
    results: dict[int, dict[str, object]] = {}
    started = -1
    for line in out.decode("utf-8", "replace").splitlines():
        try:
            frame = json.loads(line)
        except ValueError:
            continue  # a library printed something: not a frame
        if not isinstance(frame, dict):
            continue
        if isinstance(frame.get("start"), int):
            started = frame["start"]
        elif isinstance(frame.get("i"), int):
            results[frame["i"]] = frame
            started = -1
    return results, started


# ---------------------------------------------------------------------------------------------------- the folders
def secret_places(cfg: "Config") -> tuple[Path, ...]:
    """What a scan never enters: the places the sandbox hides from his commands (``safety.sandbox_hidden``: keys,
    sign-ins, browser profiles), his own data folder and his vault. One list of what is secret, for both.

    Args:
        cfg: the settings.

    Returns:
        The places, as the real paths they are (links resolved).
    """
    named = [Path(path).expanduser() for path in cfg.safety.sandbox_hidden]
    return tuple(Path(os.path.realpath(path)) for path in (*named, cfg.data_path, cfg.secrets_path))


def check_folders(configured: Sequence[str], secret: Collection[Path]) -> list[_Folder]:
    """The folders of the settings, each with the reason it can't be used when it can't.

    Args:
        configured: ``documents.folders``.
        secret: the places never read (``secret_places``).

    Returns:
        One entry per folder, in order. A folder that overlaps an earlier one is the one refused.
    """
    folders: list[_Folder] = []
    for text in configured:
        raw = Path(text).expanduser()
        if not raw.is_absolute():
            folders.append(_Folder(raw, text, "give the folder's whole path (it starts with / or ~)"))
            continue
        path = Path(os.path.realpath(raw))
        error = ""
        if not path.exists():
            error = "there is no such folder"
        elif not path.is_dir():
            error = "it is not a folder"
        elif not os.access(path, os.R_OK | os.X_OK):
            error = "you are not allowed to read it"
        elif any(path == p or path.is_relative_to(p) for p in secret):
            error = "keys, logins or his own data live there: it is never read"
        else:
            for other in folders:
                if not other.error and (path == other.path or path.is_relative_to(other.path)
                                        or other.path.is_relative_to(path)):
                    error = f"it overlaps {other.shown}, which is indexed already"
                    break
        folders.append(_Folder(path, text, error))
    return folders


def exclusion(patterns: Iterable[str]) -> "re.Pattern[str] | None":
    """One pattern matching a name that any of the shell-style patterns (* ? [..]) matches; None for no patterns."""
    parts = [fnmatch.translate(pattern) for pattern in patterns if pattern]
    return re.compile("|".join(parts)) if parts else None


@dataclass(frozen=True, slots=True)
class _Rules:
    """What a scan reads: the file types, the names it skips, the largest file, the folders it never enters."""

    extensions: frozenset[str]
    excluded: "re.Pattern[str] | None"
    max_bytes: int
    skip: frozenset[Path]

    @classmethod
    def of(cls, cfg: "Config") -> "_Rules":
        """The rules of the live settings."""
        d: DocumentsConfig = cfg.documents
        return cls(frozenset(d.extensions), exclusion(d.excludes), d.max_file_mb << 20, frozenset(secret_places(cfg)))

    def kept(self, name: str) -> bool:
        """Whether a file or folder of this name is read at all."""
        return self.excluded is None or self.excluded.match(name) is None


def _extension(name: str) -> str:
    """A file name's extension in lower case, without the dot ("" for none; a name like .bashrc has none)."""
    return os.path.splitext(name)[1][1:].lower()


def _walk(root: Path, prefix: str, rules: _Rules, unreadable: list[str]) -> Iterator[tuple[str, int, int, int]]:
    """The files of a folder that the rules read, as (path below the folder, size, mtime_ns, inode). Links, hidden
    and excluded names and the folders the rules skip are not entered; a folder that can't be listed is noted."""
    stack = [(root, prefix)]
    while stack:
        here, shown = stack.pop()
        try:
            with os.scandir(here) as scan:
                entries = sorted(scan, key=lambda entry: entry.name)
        except OSError:
            unreadable.append(str(here))
            continue
        for entry in reversed(entries):  # popped in name order
            if not rules.kept(entry.name) or entry.is_symlink():
                continue
            rel = f"{shown}/{entry.name}" if shown else entry.name
            if entry.is_dir(follow_symlinks=False):
                if Path(entry.path) not in rules.skip:
                    stack.append((Path(entry.path), rel))
            elif entry.is_file(follow_symlinks=False) and _extension(entry.name) in rules.extensions:
                try:
                    status = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                yield rel, status.st_size, status.st_mtime_ns, status.st_ino


# ---------------------------------------------------------------------------------------------------- the service
class _Until:
    """A stop event for ``watchfiles``, set when a test says so (its thread asks ``is_set`` every 50 ms) or when the
    watching is cancelled (it calls ``set``)."""

    def __init__(self, test: Callable[[], bool]) -> None:
        self._test = test
        self._set = False

    def is_set(self) -> bool:
        """Whether the watching should end."""
        return self._set or self._test()

    def set(self) -> None:
        """End the watching."""
        self._set = True


class Documents:
    """The documents: scanning, watching, converting, indexing, and searching them (see the module docstring).

    Args:
        cfg: the live settings (``documents.*``).
        embedder: the memory's embedder (meaning search; without its model, keywords alone).
        publish: ``EventBus.publish``: the HUD hears how the folders are doing (``documents`` events).
        record: ``Activity.record``: the documents that could not be read are written to the activity log.
    """

    def __init__(self, cfg: "Config", embedder: "Embedder", *, publish: Publish, record: Record) -> None:
        self.cfg = cfg
        self._embedder = embedder
        self._publish = publish
        self._record = record
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="documents", initializer=lower_priority,
                                        initargs=(INDEX_NICE,))
        self._db: _Db | None = None
        self._codes = _Codes()
        self._loaded = False  # whether the sign codes are in memory
        self._folders: list[_Folder] = []
        self._folder_version = 0  # changes when the set of folders to watch does
        self._folders_changed = asyncio.Event()
        self._wake = asyncio.Event()
        self._wake.set()  # the first thing to do is to look at everything
        self._round = asyncio.Lock()
        self._full, self._again = True, 1
        self._dirty: set[Path] = set()
        self._procs: set[asyncio.subprocess.Process] = set()
        self._stopping = False
        self._pending: dict[int, int] = {}  # folder -> documents still to read
        self._chunks = 0  # pieces searchable
        self._words: dict[str, int] = {}  # query word -> in how many pieces (until the index changes)
        self._announced = 0.0
        self._poke_task: asyncio.Task[None] | None = None
        self._failed: list[tuple[str, str]] = []  # (path, why) of this round's documents that could not be read
        self._unreadable: list[str] = []
        self._error = ""  # what is wrong with the service as a whole (the watcher, the index)

    # ------------------------------------------------------------------------------------------------ running
    async def run(self) -> None:
        """The indexing loop: wake when something may have changed, then bring the index up to date."""
        while not self._stopping:
            await self._wake.wait()
            self._wake.clear()
            if self._stopping:
                return
            try:
                await self.sync()
            except Exception:
                self._wake.set()  # whoever restarts this loop finds the work still to do
                raise

    async def sync(self) -> None:
        """Bring the index up to date now: look at what changed (everything after a start or a settings change),
        read those documents and index them; returns when there is nothing left to do."""
        async with self._round:
            full, again, dirty = self._full, self._again, self._dirty
            self._full, self._again, self._dirty = False, 1, set()
            started = time.monotonic()
            try:
                if not await self._prepare(full):
                    return
                plan = await self._in_pool(self._plan, full, again, dirty)
                await self._execute(plan)
            except BaseException:
                self._full, self._again, self._dirty = self._full or full, max(self._again, again), self._dirty | dirty
                raise
            finally:
                self._poke()
            if plan.jobs or plan.gone or plan.broken:
                log.info("documents: %d to read, %d gone, %d unreadable in %.1f s", len(plan.jobs), len(plan.gone),
                         len(plan.broken), time.monotonic() - started)

    def changed(self, paths: Iterable[Path]) -> None:
        """These paths changed (what the watcher reports): look at them soon."""
        self._dirty.update(paths)
        self._wake.set()

    def reconfigure(self) -> None:
        """The documents' settings changed (``Apply.DOCUMENTS``): look at everything again, and read again the files
        that failed for want of time or memory."""
        self._full, self._again = True, 2
        self._wake.set()

    async def watch(self) -> None:
        """Follow the folders: what changes in them is looked at again (changes close together count as one)."""
        import watchfiles

        while not self._stopping:
            version = self._folder_version
            folders = [str(folder.path) for folder in self._folders if not folder.error]
            if not folders:
                await self._folders_changed.wait()
                self._folders_changed.clear()
                continue
            over = _Until(lambda version=version: self._stopping or self._folder_version != version)
            try:
                async for changes in watchfiles.awatch(*folders, debounce=WATCH_DEBOUNCE_MS, rust_timeout=1000,
                                                       watch_filter=self._watched,
                                                       stop_event=over):  # type: ignore[arg-type]  # is_set is all it asks
                    self.changed(Path(path) for _, path in changes)
            except Exception as e:
                self._error = f"The folders can't be watched ({e}). Changes are noticed when he starts."
                self._poke()
                raise
            if self._error.startswith("The folders can't be watched"):
                self._error = ""

    def _watched(self, _change: object, path: str) -> bool:
        """Whether a change under the folders is worth a look: not in a name the rules skip, below the folder."""
        rules = _Rules.of(self.cfg)
        for folder in self._folders:
            if not folder.error and path.startswith(f"{folder.path}/"):
                return all(rules.kept(part) for part in Path(path[len(str(folder.path)) + 1:]).parts)
        return True

    async def stop(self) -> None:
        """Stop reading: workers are ended, the index is closed."""
        self._stopping = True
        self._wake.set()
        self._folders_changed.set()
        for process in list(self._procs):
            _kill(process)
        await asyncio.to_thread(self._shutdown)

    def _shutdown(self) -> None:
        """Wait for the thread at work, then close the index."""
        self._pool.shutdown(wait=True, cancel_futures=True)
        if self._db is not None:
            db, self._db = self._db, None
            db.close()

    def _in_pool[T](self, work: Callable[..., T], *args: object) -> "asyncio.Future[T]":
        """Run work on a thread of the documents' own (low priority) and await it."""
        return asyncio.get_running_loop().run_in_executor(self._pool, work, *args)

    # ------------------------------------------------------------------------------------------------ preparing
    async def _prepare(self, full: bool) -> bool:
        """Open the index and check the settings against it (on a full look). Returns whether there is anything to do
        (no folders, and no index of folders that were removed, is nothing)."""
        if self._db is None:
            if not self.cfg.documents.folders and not (self.cfg.data_path / DB_NAME).exists():
                self._folders = []
                return False
            self._db = await self._in_pool(_Db, self.cfg.data_path / DB_NAME)
            if self._db.remade:
                self._error = f"The index of the documents is being made afresh: {self._db.remade}."
        if full:
            await self._in_pool(self._configure)
            await self._in_pool(self._reembed)
            self._folders_changed.set()
        return True

    def _fingerprint(self) -> str:
        """What the pieces depend on: another one makes the whole index again."""
        from importlib.metadata import version

        d = self.cfg.documents
        return json.dumps([SCHEMA_VERSION, d.chunk_chars, d.chunk_overlap, d.max_chars, d.pdf_tables,
                           version("markitdown")])

    def _configure(self) -> None:
        """(a pool thread) Match the index to the settings: remake it when the way of reading changed, add the folders
        that are new, drop those that are gone."""
        assert self._db is not None
        db = self._db
        with db.reading() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = 'fingerprint'").fetchone()
        if row is not None and row[0] != self._fingerprint():
            log.info("the documents are read in another way now: the index is made afresh")
            self._codes.reset(0, 0)
            self._loaded = False
            db.remake()
            self._words.clear()
        folders = check_folders(self.cfg.documents.folders, secret_places(self.cfg))

        def apply(conn: sqlite3.Connection) -> None:
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('fingerprint', ?)", (self._fingerprint(),))
            known = {str(folder.path) for folder in folders}  # a folder that fails for now keeps its index
            for path, in conn.execute("SELECT path FROM doc_folders").fetchall():
                if path not in known:
                    gone = [r[0] for r in conn.execute("SELECT c.id FROM doc_chunks c JOIN doc_folders d ON "
                                                       "d.id = c.folder WHERE d.path = ?", (path,))]
                    conn.execute("DELETE FROM doc_folders WHERE path = ?", (path,))
                    self._codes.remove(gone)
            for folder in folders:
                if not folder.error:
                    conn.execute("INSERT OR IGNORE INTO doc_folders (path) VALUES (?)", (str(folder.path),))
                    folder.id = int(conn.execute("SELECT id FROM doc_folders WHERE path = ?",
                                                 (str(folder.path),)).fetchone()[0])

        db.write(apply)
        before = {folder.path for folder in self._folders if not folder.error}
        self._folders = folders
        if before != {folder.path for folder in folders if not folder.error}:
            self._folder_version += 1
        with db.reading() as conn:
            self._chunks = int(conn.execute("SELECT COALESCE(SUM(chunks), 0) FROM doc_files").fetchone()[0])
        self._unreadable = []

    def _reembed(self) -> None:
        """(a pool thread) Give every piece a vector from today's embedder: all of them when the embedder is another
        one, those that have none when it came late. Then load the sign codes into memory."""
        assert self._db is not None
        db, embedder = self._db, self._embedder
        model = embedder.id
        with db.reading() as conn:
            known = conn.execute("SELECT value FROM meta WHERE key = 'embedder'").fetchone()
            missing = conn.execute("SELECT 1 FROM doc_chunks c LEFT JOIN doc_vecs v ON v.id = c.id WHERE v.id IS NULL "
                                   "LIMIT 1").fetchone() is not None
        changed = False
        if model and ((known[0] if known else None) != model or missing):
            changed = True
            if known and known[0] != model:
                db.write(lambda conn: (conn.execute("DELETE FROM doc_vecs"), conn.execute("DELETE FROM doc_codes")))
            log.info("the documents get vectors from %s", model)
            while not self._stopping:
                with db.reading() as conn:
                    rows = conn.execute("SELECT c.id, c.folder, c.heading, c.text FROM doc_chunks c LEFT JOIN doc_vecs v "
                                        "ON v.id = c.id WHERE v.id IS NULL ORDER BY c.id LIMIT 2048").fetchall()
                if not rows:
                    break
                vectors = embedder.encode([f"{r['heading']}\n{r['text']}" for r in rows])
                if vectors is None:
                    break
                blobs, codes = quantize(vectors), sign_codes(vectors)
                db.write(lambda conn, rows=rows, blobs=blobs, codes=codes: (
                    conn.executemany("INSERT INTO doc_vecs (id, vec) VALUES (?, ?)",
                                     [(r["id"], b) for r, b in zip(rows, blobs, strict=True)]),
                    conn.executemany("INSERT INTO doc_codes (id, folder, code) VALUES (?, ?, ?)",
                                     [(r["id"], r["folder"], c.tobytes()) for r, c in zip(rows, codes, strict=True)])))
            if not self._stopping:
                db.write(lambda conn: conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('embedder', ?)",
                                                   (model,)))
        if changed or not self._loaded:
            self._load_codes()

    def _load_codes(self) -> None:
        """(a pool thread) Read every sign code into memory."""
        assert self._db is not None
        with self._db.reading() as conn:
            total = int(conn.execute("SELECT COUNT(*) FROM doc_codes").fetchone()[0])
            first = conn.execute("SELECT length(code) FROM doc_codes LIMIT 1").fetchone()
            self._codes.reset(total + 1024, first[0] // 8 if first else 0)
            cursor = conn.execute("SELECT id, folder, code FROM doc_codes ORDER BY id")
            while rows := cursor.fetchmany(50000):
                self._codes.add(np.array([r[0] for r in rows], dtype=np.int64),
                                np.array([r[1] for r in rows], dtype=np.int32),
                                np.frombuffer(b"".join(r[2] for r in rows), dtype=np.uint64).reshape(len(rows), -1))
        self._loaded = True

    # ------------------------------------------------------------------------------------------------ planning
    def _plan(self, full: bool, again: int, dirty: Collection[Path]) -> _Plan:
        """(a pool thread) Work out what to read and what to forget: a look at every folder (full), else at the paths
        that changed. A file is read when it is new, its inode, size or time changed, or it failed and may work now."""
        assert self._db is not None
        rules = _Rules.of(self.cfg)
        plan = _Plan()
        valid = [folder for folder in self._folders if not folder.error]
        with self._db.reading() as conn:
            if full:
                for folder in valid:
                    self._look(conn, folder, "", rules, again, plan)
            else:
                for path in sorted(dirty):
                    for folder in valid:
                        if path == folder.path:
                            self._look(conn, folder, "", rules, again, plan)
                        elif path.is_relative_to(folder.path):
                            self._touched(conn, folder, path, rules, again, plan)
                        else:
                            continue
                        break
        return plan

    def _look(self, conn: sqlite3.Connection, folder: _Folder, rel: str, rules: _Rules, again: int,
              plan: _Plan) -> None:
        """Compare a folder (or a part of it, ``rel``) with the index."""
        base = folder.path / rel if rel else folder.path
        known = {row["path"]: row for row in conn.execute(
            "SELECT path, size, mtime_ns, ino, error, retry FROM doc_files WHERE folder = ?" +
            (" AND path >= ? AND path < ?" if rel else ""), (folder.id, f"{rel}/", f"{rel}0") if rel else (folder.id,))}
        seen: set[str] = set()
        for found, size, mtime_ns, ino in _walk(base, rel, rules, self._unreadable):
            seen.add(found)
            self._compare(folder, found, size, mtime_ns, ino, known.get(found), rules, again, plan)
        plan.gone += [(folder.id, path, False) for path in known if path not in seen]

    def _touched(self, conn: sqlite3.Connection, folder: _Folder, path: Path, rules: _Rules, again: int,
                 plan: _Plan) -> None:
        """Compare one changed path (a file, a folder, or one that is gone) with the index."""
        rel = path.relative_to(folder.path).as_posix()
        if not all(rules.kept(part) for part in Path(rel).parts):
            return
        try:
            status = os.lstat(path)
        except OSError:
            plan.gone.append((folder.id, rel, True))  # a file, or a folder and everything in it
            return
        if stat.S_ISDIR(status.st_mode):
            if path not in rules.skip:
                self._look(conn, folder, rel, rules, again, plan)
        elif stat.S_ISREG(status.st_mode) and _extension(path.name) in rules.extensions:
            row = conn.execute("SELECT path, size, mtime_ns, ino, error, retry FROM doc_files WHERE folder = ? AND "
                               "path = ?", (folder.id, rel)).fetchone()
            self._compare(folder, rel, status.st_size, status.st_mtime_ns, status.st_ino, row, rules, again, plan)
        else:
            plan.gone.append((folder.id, rel, False))  # it became a link or something else

    @staticmethod
    def _compare(folder: _Folder, rel: str, size: int, mtime_ns: int, ino: int, row: sqlite3.Row | None,
                 rules: _Rules, again: int, plan: _Plan) -> None:
        """Decide what to do with one file."""
        job = _Job(folder.id, rel, folder.path / rel, _extension(rel), size, mtime_ns, ino)
        same = row is not None and (row["size"], row["mtime_ns"], row["ino"]) == (size, mtime_ns, ino)
        if size > rules.max_bytes:
            error = f"it is larger than {rules.max_bytes >> 20} MB (documents.max_file_mb)"
            if not (same and row is not None and row["error"] == error):
                plan.broken.append(_Doc(job, error=error, retry=2))  # looked at again when the settings change
        elif not same or (row is not None and row["error"] and 0 < row["retry"] <= again):
            plan.jobs.append(job)

    # ------------------------------------------------------------------------------------------------ doing it
    async def _execute(self, plan: _Plan) -> None:
        """Forget what is gone, note what can't be read, read and index the rest."""
        self._failed = []
        if plan.gone:
            await self._in_pool(self._forget, plan.gone)
        if plan.broken:
            await self._in_pool(self._index_docs, plan.broken)
            self._failed += [(str(doc.job.path), doc.error) for doc in plan.broken]
        jobs = sorted(plan.jobs, key=lambda job: (job.ext not in DIRECT, job.rel))
        self._pending = {}
        for job in jobs:
            self._pending[job.folder] = self._pending.get(job.folder, 0) + 1
        if jobs:
            self._poke()
            await self._process(jobs)
        self._pending = {}
        if self._failed:
            shown = "; ".join(f"{path}: {why}" for path, why in self._failed[:3])
            self._record("system", source="system", outcome="failed",
                         title=f"{len(self._failed)} document(s) could not be read",
                         detail=shown + (f"; and {len(self._failed) - 3} more" if len(self._failed) > 3 else ""))
        if jobs and self._db is not None and not self._stopping:
            await self._in_pool(self._db.checkpoint)

    async def _process(self, jobs: Sequence[_Job]) -> None:
        """Read the documents (text files here, the others in workers) and index them as they come."""
        d = self.cfg.documents
        ready: asyncio.Queue[_Doc | None] = asyncio.Queue(maxsize=QUEUE_DOCS)
        batches: asyncio.Queue[list[_Job]] = asyncio.Queue()
        for batch in _batches([job for job in jobs if job.ext not in DIRECT]):
            batches.put_nowait(batch)

        async def texts() -> None:
            for job in jobs:
                if job.ext in DIRECT and not self._stopping:
                    await ready.put(await self._in_pool(self._read_text, job))

        async def converter() -> None:
            while not batches.empty() and not self._stopping:
                batch = batches.get_nowait()
                while batch and not self._stopping:
                    docs, batch = await self._convert(batch)
                    for doc in docs:
                        await ready.put(doc)

        async def produce() -> None:
            async with asyncio.TaskGroup() as readers:
                readers.create_task(texts())
                for _ in range(d.workers):
                    readers.create_task(converter())
            await ready.put(None)

        async with asyncio.TaskGroup() as group:  # one failing part ends the others: a full queue never waits for nobody
            group.create_task(self._index_loop(ready))
            group.create_task(produce())

    async def _index_loop(self, ready: "asyncio.Queue[_Doc | None]") -> None:
        """Index documents as they arrive, several at once when they are small."""
        finished = False
        while not finished:
            doc = await ready.get()
            if doc is None:
                return
            docs, chars = [doc], len(doc.text)
            while chars < WRITE_CHARS and not ready.empty():
                nxt = ready.get_nowait()
                if nxt is None:
                    finished = True
                    break
                docs.append(nxt)
                chars += len(nxt.text)
            if not self._stopping:
                await self._in_pool(self._index_docs, docs)
            for done in docs:
                self._pending[done.job.folder] = max(0, self._pending.get(done.job.folder, 1) - 1)
                if done.error:
                    self._failed.append((str(done.job.path), done.error))
            self._poke()

    def _read_text(self, job: _Job) -> _Doc:
        """(a pool thread) A text or Markdown file, as it is: read without following a link, and only if it is still the
        file the scan saw."""
        d = self.cfg.documents
        try:
            fd = os.open(job.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError as e:
            return _Doc(job, error=f"it can't be opened ({e.strerror})", retry=1)
        try:
            with os.fdopen(fd, "rb") as f:
                before = os.fstat(fd)
                if not stat.S_ISREG(before.st_mode) or (before.st_ino, before.st_size, before.st_mtime_ns) != (
                        job.ino, job.size, job.mtime_ns):
                    return _Doc(job, error="it changed after it was found", retry=1)
                data = f.read(d.max_chars * 4 + 4)
                after = os.fstat(fd)
        except OSError as e:
            return _Doc(job, error=f"it can't be read ({e.strerror})", retry=1)
        if (after.st_ino, after.st_size, after.st_mtime_ns) != (job.ino, job.size, job.mtime_ns):
            return _Doc(job, error="it changed while it was being read", retry=1)
        if len(data) > d.max_chars * 4:
            data = data[:data.rfind(b"\n")] if b"\n" in data else data
        return _Doc(job, text=normalize_text(decode_text(data))[:d.max_chars])

    async def _convert(self, batch: list[_Job]) -> tuple[list[_Doc], list[_Job]]:
        """Have a worker turn a batch of documents into Markdown.

        Returns:
            The documents that are done (or failed), and the rest of the batch, which the worker did not get to
            because it was ended: they go to another worker.
        """
        d = self.cfg.documents
        specs = [json.dumps([job.ext, job.ino, job.size, job.mtime_ns, str(job.path)]) for job in batch]
        argv = [*WORKER, str(d.worker_memory_mb), str(int(d.convert_timeout_s)), str(d.max_chars), str(int(d.pdf_tables)),
                *specs]
        cap = len(batch) * (d.max_chars * 4 + 512) + (1 << 20)
        try:
            argv = confine(self.cfg, argv)
        except SandboxError as e:  # this machine can't make a sandbox: nothing is read, and the settings say why
            return [_Doc(job, error=str(e), retry=2) for job in batch], []
        done = await launch(argv, silence_s=d.convert_timeout_s + SILENCE_GRACE_S + STARTUP_S, cap=cap,
                            procs=self._procs)
        if self._stopping:
            return [], []
        results, started = read_frames(done.out)
        never = started < 0 and not results  # the worker did not get as far as its first document
        docs: list[_Doc] = []
        rest: list[_Job] = []
        died = self._death(done)
        for i, job in enumerate(batch):
            frame = results.get(i)
            if frame is not None and frame.get("ok"):
                docs.append(_Doc(job, text=normalize_text(str(frame.get("text") or ""))))
            elif frame is not None:
                why = str(frame.get("error") or "it can't be read")
                docs.append(_Doc(job, error=why, retry=2 if "documents." in why else 0))
            elif i == started or never:
                docs.append(_Doc(job, error=died, retry=2 if done.why == "it hung" else 1))
            else:
                rest.append(job)  # the worker was ended before it got to this one: another one will
        return docs, rest

    @staticmethod
    def _death(done: Launched) -> str:
        """Why a worker's document has no result, in words."""
        tail = done.err.decode("utf-8", "replace").strip().splitlines()
        if done.why == "it hung":
            return "the reader hung on it and was ended (documents.convert_timeout_s)"
        if done.why:
            return f"the reader was ended: {done.why}"
        if done.code < 0:
            return f"the reader was ended by the system (signal {-done.code}), probably for want of memory"
        return f"the reader stopped (exit {done.code})" + (f": {tail[-1][:160]}" if tail else "")

    # ------------------------------------------------------------------------------------------------ indexing
    def _embed(self, texts: Sequence[str]) -> "NDArray[np.float32] | None":
        """Vectors for pieces, a batch at a time; None without the embedding model."""
        if not texts or not self._embedder.id:
            return None
        parts = []
        for low in range(0, len(texts), EMBED_BATCH):
            part = self._embedder.encode(texts[low:low + EMBED_BATCH])
            if part is None:
                return None
            parts.append(part)
        return np.vstack(parts)

    def _index_docs(self, docs: Sequence[_Doc]) -> None:
        """(a pool thread) Cut documents into pieces, embed them, and write documents, pieces, vectors and codes in
        one transaction. A document that failed is written as a file with its error and no pieces."""
        assert self._db is not None
        d = self.cfg.documents
        prepared: list[tuple[_Doc, list[Chunk]]] = []
        for doc in docs:
            chunks: list[Chunk] = []
            if not doc.error:
                chunks = chunk_markdown(doc.text, Path(doc.job.rel).stem, d.chunk_chars, d.chunk_overlap)
                if not chunks:
                    doc.error, doc.retry = "it has no text to read (a scan, or only pictures?)", 0
            prepared.append((doc, chunks))
        vectors = self._embed([f"{c.heading}\n{c.text}" for _, chunks in prepared for c in chunks])
        blobs = quantize(vectors) if vectors is not None else None
        codes = sign_codes(vectors) if vectors is not None else None
        gone, added = self._db.write(lambda conn: self._store(conn, prepared, blobs, codes))
        self._codes.remove(gone)
        if len(added[0]):
            self._codes.add(*added)
        self._chunks += sum(len(chunks) for _, chunks in prepared) - len(gone)
        self._words.clear()

    @staticmethod
    def _store(conn: sqlite3.Connection, prepared: Sequence[tuple[_Doc, list[Chunk]]], blobs: list[bytes] | None,
               codes: "NDArray[np.uint64] | None",
               ) -> "tuple[list[int], tuple[NDArray[np.int64], NDArray[np.int32], NDArray[np.uint64]]]":
        """(in a write transaction) Replace each document's rows.

        Returns:
            The ids of the pieces that went, and the ids, folders and sign codes of those that came.
        """
        sequence = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM sqlite_sequence WHERE name = 'doc_chunks'").fetchone()
        next_id = int(sequence[0]) + 1
        gone: list[int] = []
        new_ids: list[int] = []
        new_folders: list[int] = []
        taken = 0
        for doc, chunks in prepared:
            job = doc.job
            old = conn.execute("SELECT id FROM doc_files WHERE folder = ? AND path = ?", (job.folder, job.rel)).fetchone()
            if old is not None:
                gone += [r[0] for r in conn.execute("SELECT id FROM doc_chunks WHERE file = ?", (old[0],))]
                conn.execute("DELETE FROM doc_files WHERE id = ?", (old[0],))
            file_id = conn.execute(
                "INSERT INTO doc_files (folder, path, size, mtime_ns, ino, chars, chunks, error, retry, at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (job.folder, job.rel, job.size, job.mtime_ns, job.ino, len(doc.text), len(chunks), doc.error,
                 doc.retry, time.time())).lastrowid
            ids = range(next_id, next_id + len(chunks))
            next_id += len(chunks)
            conn.executemany("INSERT INTO doc_chunks (id, file, ord, start, stop, folder, heading, text) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                             [(i, file_id, n, c.start, c.stop, job.folder, c.heading, c.text)
                              for n, (i, c) in enumerate(zip(ids, chunks, strict=True))])
            if blobs is not None and codes is not None:
                conn.executemany("INSERT INTO doc_vecs (id, vec) VALUES (?, ?)",
                                 [(i, blobs[taken + n]) for n, i in enumerate(ids)])
                conn.executemany("INSERT INTO doc_codes (id, folder, code) VALUES (?, ?, ?)",
                                 [(i, job.folder, codes[taken + n].tobytes()) for n, i in enumerate(ids)])
                new_ids += ids
                new_folders += [job.folder] * len(chunks)
            taken += len(chunks)
        words = codes.shape[1] if codes is not None else 0
        picked = codes if codes is not None else np.zeros((0, words), dtype=np.uint64)
        return gone, (np.array(new_ids, dtype=np.int64), np.array(new_folders, dtype=np.int32), picked)

    def _forget(self, gone: Sequence[tuple[int, str, bool]]) -> None:
        """(a pool thread) Drop documents from the index: (folder, path, whether everything below the path too)."""
        assert self._db is not None
        removed: list[int] = []

        def work(conn: sqlite3.Connection) -> None:
            for folder, path, below in gone:
                where = ("f.folder = ? AND (f.path = ? OR (f.path >= ? AND f.path < ?))" if below
                         else "f.folder = ? AND f.path = ?")
                args = (folder, path, f"{path}/", f"{path}0") if below else (folder, path)
                removed.extend(r[0] for r in conn.execute(
                    f"SELECT c.id FROM doc_chunks c JOIN doc_files f ON f.id = c.file WHERE {where}", args))
                conn.execute(f"DELETE FROM doc_files WHERE id IN (SELECT f.id FROM doc_files f WHERE {where})", args)

        self._db.write(work)
        self._codes.remove(removed)
        self._chunks -= len(removed)
        self._words.clear()

    # ------------------------------------------------------------------------------------------------ searching
    def search(self, query: str, *, qvec: "NDArray[np.float32] | None" = None, folder: str = "",
               limit: int = 0) -> tuple[list[Hit], str]:
        """Search the documents by keywords and by meaning (fused by reciprocal rank).

        Args:
            query: what to look for.
            qvec: the query's vector (None: embedded here, when there is an embedding model).
            folder: only this folder (its path, or its name).
            limit: how many results at most (0: ``documents.search_results``).

        Returns:
            The results, best first, at most two from one document; and a note ("" none) saying the index is not
            complete.

        Raises:
            DocumentsError: no folder is set up, or the index isn't open yet, or the folder isn't one of the settings'.
        """
        db = self._open()
        d = self.cfg.documents
        wanted = self._folder_id(folder) if folder else None
        limit = limit or d.search_results
        terms = content_terms(query)
        if qvec is None and self._embedder.id:
            qvec = self._embedder.encode_query(query)
        with db.reading() as conn:
            keyword = self._keyword_ids(conn, terms, wanted, d.search_candidates)
            meaning = self._meaning_ids(conn, qvec, wanted, d.search_candidates)
            fused = rrf([keyword, meaning])
            order = sorted(fused, key=fused.__getitem__, reverse=True)[:limit * 6]
            rows = {r["id"]: r for r in conn.execute(
                "SELECT c.id, c.heading, c.text, c.start, c.stop, f.path, d.path AS folder FROM doc_chunks c JOIN "
                "doc_files f ON f.id = c.file JOIN doc_folders d ON d.id = c.folder WHERE c.id IN "
                "(SELECT value FROM json_each(?))", (json.dumps(order),))}
        hits: list[Hit] = []
        per_file: dict[str, int] = {}
        for chunk_id in order:
            row = rows.get(chunk_id)
            if row is None:
                continue  # replaced since the lists were made
            path = f"{row['folder']}/{row['path']}"
            if per_file.get(path, 0) >= PER_FILE:
                continue
            per_file[path] = per_file.get(path, 0) + 1
            hits.append(Hit(path, row["folder"], row["heading"], excerpt(row["text"], terms, d.result_chars),
                            row["start"], row["stop"], fused[chunk_id]))
            if len(hits) >= limit:
                break
        return hits, self._note()

    def _open(self) -> _Db:
        """The index, for reading.

        Raises:
            DocumentsError: no folder is set up, or the index isn't open yet.
        """
        if not self.cfg.documents.folders:
            raise DocumentsError("No folders are set up yet: the user chooses them in Settings, under Customize, "
                                 "Documents.")
        if self._db is None:
            raise DocumentsError("The documents are being read for the first time: ask again in a moment.")
        return self._db

    def _note(self) -> str:
        """A line saying the index is not complete yet, "" when it is."""
        left = sum(self._pending.values())
        return f"(he is still reading {left} document(s): there may be more)" if left else ""

    def _folder_id(self, name: str) -> int:
        """The folder a name means: its path, or the last part of it, in any case.

        Raises:
            DocumentsError: it is none of the folders.
        """
        wanted = os.path.normpath(os.path.expanduser(name)).lower()
        for folder in self._folders:
            if not folder.error and wanted in (str(folder.path).lower(), folder.path.name.lower()):
                return folder.id
        names = ", ".join(folder.path.name for folder in self._folders if not folder.error)
        raise DocumentsError(f"There is no folder called {name!r} among the documents' folders ({names or 'none'}).")

    def _keyword_ids(self, conn: sqlite3.Connection, terms: Sequence[str], folder: int | None, k: int) -> list[int]:
        """The pieces that best match the query's words by BM25 (headings count half as much as text). Words in more
        than ``PRUNE_SHARE`` of the pieces are left out, but for the rarest one: their lists are the cost of the search
        and their score is near nothing."""
        counts: list[tuple[int, str]] = []
        for term in terms[:MAX_TERMS * 2]:
            if (count := self._words.get(term)) is None:
                count = int(conn.execute("SELECT COUNT(*) FROM doc_fts WHERE doc_fts MATCH ?",
                                         (f'"{term}"',)).fetchone()[0])
                self._words[term] = count
            if count:
                counts.append((count, term))
        counts.sort()
        limit = PRUNE_SHARE * max(1, self._chunks)
        words = [term for count, term in counts if count <= limit][:MAX_TERMS] or [term for _, term in counts[:1]]
        if not words:
            return []
        match = " OR ".join(f'"{word}"' for word in words)
        if folder is not None:
            match = f'({match}) AND folder : "{folder}"'
        return [int(r[0]) for r in conn.execute("SELECT rowid FROM doc_fts WHERE doc_fts MATCH ? ORDER BY "
                                                 "bm25(doc_fts, 1.0, 0.5, 0.0) LIMIT ?", (match, k))]

    def _meaning_ids(self, conn: sqlite3.Connection, qvec: "NDArray[np.float32] | None", folder: int | None,
                     k: int) -> list[int]:
        """The pieces nearest in meaning to the query: sign codes narrow them down, exact cosines choose."""
        if qvec is None or not self._codes.count:
            return []
        ids = self._codes.nearest(qvec, folder)
        if not len(ids):
            return []
        rows = conn.execute("SELECT id, vec FROM doc_vecs WHERE id IN (SELECT value FROM json_each(?))",
                            (json.dumps(ids.tolist()),)).fetchall()
        if not rows:
            return []
        cosines = dequantize([r[1] for r in rows]) @ qvec
        floor = self.cfg.documents.min_similarity
        return [int(rows[i][0]) for i in np.argsort(-cosines)[:k] if cosines[i] >= floor]

    # ------------------------------------------------------------------------------------------------ reading
    def read(self, path: str, start: int = 0, chars: int = 0) -> Page:
        """A stretch of a document's text, taken from the index: a path the scan did not index, whatever it names, has no
        text here, so no link or ``..`` can lead outside the folders.

        Args:
            path: the document's whole path (as a search gave it), or its path below one of the folders.
            start: where to begin.
            chars: how many characters (0: ``documents.read_chars``).

        Returns:
            The stretch, with where it is and how long the document is.

        Raises:
            DocumentsError: no such document in the folders.
        """
        db = self._open()
        chars = chars or self.cfg.documents.read_chars
        file_id, shown, total = self._locate(db, path)
        start = max(0, min(start, total))
        stop = min(total, start + chars)
        with db.reading() as conn:
            rows = conn.execute("SELECT start, stop, text FROM doc_chunks WHERE file = ? AND stop > ? AND start < ? "
                                "ORDER BY ord", (file_id, start, stop)).fetchall()
        pos, parts = start, []
        for first, last, text in rows:
            if last <= pos or first > pos:
                continue
            parts.append(text[pos - first:])
            pos = last
        return Page(shown, "".join(parts)[:stop - start], start, stop, total)

    def _locate(self, db: _Db, path: str) -> tuple[int, str, int]:
        """The indexed document a path means: (its id, its whole path, its length).

        Raises:
            DocumentsError: there is none.
        """
        text = os.path.normpath(os.path.expanduser(path))
        candidates: list[tuple[int, str]] = []
        for folder in self._folders:
            if folder.error:
                continue
            base = f"{folder.path}/"
            if text.startswith(base):
                candidates = [(folder.id, text[len(base):])]
                break
            if not os.path.isabs(text):
                candidates.append((folder.id, text))
        with db.reading() as conn:
            for folder_id, rel in candidates:
                row = conn.execute("SELECT f.id, f.chars, f.error, d.path AS folder FROM doc_files f JOIN doc_folders d "
                                   "ON d.id = f.folder WHERE f.folder = ? AND f.path = ?", (folder_id, rel)).fetchone()
                if row is not None:
                    if row["error"]:
                        raise DocumentsError(f"{path} could not be read: {row['error']}.")
                    return int(row["id"]), f"{row['folder']}/{rel}", int(row["chars"])
        raise DocumentsError(f"{path} is not among the indexed documents: give the whole path a search or a list "
                             "showed (a file the folders do not hold, or not read yet, is not there).")

    def files(self, folder: str = "", pattern: str = "", limit: int = 100) -> tuple[list[str], int]:
        """The indexed documents, optionally of one folder and matching a pattern (* ? [..], in any case, on the path
        below the folder).

        Returns:
            Their whole paths (at most ``limit``), and how many there are in all.

        Raises:
            DocumentsError: as ``search``.
        """
        db = self._open()
        wanted = self._folder_id(folder) if folder else None
        match = re.compile(fnmatch.translate(pattern), re.IGNORECASE) if pattern else None
        with db.reading() as conn:
            rows = conn.execute("SELECT d.path AS folder, f.path FROM doc_files f JOIN doc_folders d ON d.id = f.folder "
                                "WHERE f.error = ''" + (" AND f.folder = ?" if wanted is not None else "") +
                                " ORDER BY d.path, f.path", (wanted,) if wanted is not None else ()).fetchall()
        found = [f"{row['folder']}/{row['path']}" for row in rows if match is None or match.match(row["path"])]
        return found[:limit], len(found)

    # ------------------------------------------------------------------------------------------------ saying how it is
    def snapshot(self) -> dict[str, JSON]:
        """How the folders are doing, for the HUD (the ``documents`` event has the same shape)."""
        counts: dict[int, tuple[int, int, int]] = {}
        if self._db is not None:
            with self._db.reading() as conn:
                counts = {int(r[0]): (int(r[1] or 0), int(r[2] or 0), int(r[3] or 0)) for r in conn.execute(
                    "SELECT folder, SUM(error = ''), SUM(chunks), SUM(error != '') FROM doc_files GROUP BY folder")}
        rows: list[JSON] = []
        for folder in self._folders:
            files, chunks, errors = (0, 0, 0) if folder.error else counts.get(folder.id, (0, 0, 0))
            rows.append({"path": folder.shown, "files": files, "chunks": chunks, "errors": errors,
                         "indexing": self._pending.get(folder.id, 0) > 0, "error": folder.error})
        return {"folders": rows, "last_error": self._error or next((f"{f.shown}: {f.error}" for f in self._folders
                                                                     if f.error), "")}

    def status(self) -> str:
        """How the documents are, in words for the model (no file names: they are the user's documents' words)."""
        snapshot = self.snapshot()
        folders = snapshot["folders"]
        if not isinstance(folders, list) or not folders:
            return "No folders are set up. The user chooses them in Settings, under Customize, Documents."
        lines = []
        for folder in folders:
            if isinstance(folder, dict):
                lines.append(f"- {folder['path']}: {folder['files']} documents ({folder['chunks']} pieces)"
                             + (f", {folder['errors']} could not be read" if folder["errors"] else "")
                             + (", still reading" if folder["indexing"] else "")
                             + (f" — {folder['error']}" if folder["error"] else ""))
        if self._error:
            lines.append(f"Problem: {self._error}")
        return "\n".join(lines)

    def _poke(self) -> None:
        """Tell the HUD how the folders are doing, soon and not more than twice a second (several calls make one)."""
        if self._poke_task is None or self._poke_task.done():
            self._poke_task = asyncio.get_running_loop().create_task(self._announce())

    async def _announce(self) -> None:
        """Publish ``documents``."""
        wait = EVENT_EVERY_S - (time.monotonic() - self._announced)
        if wait > 0:
            await asyncio.sleep(wait)
        self._announced = time.monotonic()
        if not self._stopping:
            self._publish("documents", **await self._in_pool(self.snapshot))
