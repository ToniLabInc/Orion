"""documents: the pieces a document is cut into (always exact slices that cover it), the vectors and sign codes, the
folders the service may read, an index over real files (incremental updates, search, reading, path safety), documents
converted by real worker processes (and workers that hang, crash or fail), and the tools' taint."""

import asyncio
import itertools
import json
import os
import random
import shutil
import sqlite3
import sys
import time
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from make_documents import make_docx, make_pdf, make_pptx, make_xlsx

from orion import documents, sandbox
from orion.config import Config, default_config
from orion.documents import (
    Chunk,
    Documents,
    DocumentsError,
    check_folders,
    chunk_markdown,
    decode_text,
    dequantize,
    excerpt,
    normalize_text,
    quantize,
    sign_codes,
)


# ---------------------------------------------------------------------------------------------------- the pieces
def covers(text: str, chunks: list[Chunk], size: int) -> None:
    """The pieces are exact slices, in order, that together cover all of the text, none longer than ``size``."""
    assert chunks and chunks[0].start == 0 and chunks[-1].stop == len(text)
    for before, after in itertools.pairwise(chunks):
        assert after.start <= before.stop and after.start > before.start and after.stop > before.stop
    assert all(c.text == text[c.start:c.stop] and len(c.text.rstrip()) <= size and c.text.strip() for c in chunks)


NOTE = """# Drone

The quad flies well. PID values are 48, 52 and 38.

## Tuning

Raise P until it oscillates, then back off a little.

```python
def tune(p, i, d):

    return p, i, d
```

## Batteries

Four cells, 1500 mAh. Charge them at one amp.
"""


def test_a_document_is_cut_at_its_headings_and_each_piece_knows_where_it_is() -> None:
    """With room for the whole, one piece; with less, pieces start at headings once they are a quarter full, a code
    block with a blank line inside stays whole, and each piece's heading path names the headings above it."""
    text = normalize_text(NOTE)
    whole = chunk_markdown(text, "Quad notes", 5000, 100)
    assert len(whole) == 1 and whole[0].heading == "Quad notes › Drone" and whole[0].text == text
    sections = chunk_markdown(text, "Quad notes", 160, 0)
    covers(text, sections, 160)
    assert [p.heading for p in sections] == ["Quad notes › Drone", "Quad notes › Drone › Tuning",
                                             "Quad notes › Drone › Batteries"]
    pieces = chunk_markdown(text, "Quad notes", 100, 0)
    covers(text, pieces, 100)
    assert [p.heading for p in pieces][:2] == ["Quad notes › Drone", "Quad notes › Drone › Tuning"]
    assert pieces[-1].heading == "Quad notes › Drone › Batteries"
    code = next(p for p in pieces if "def tune" in p.text)
    assert "return p, i, d" in code.text and code.text.count("```") == 2  # the blank line inside did not split it


def test_overlap_repeats_the_end_of_the_piece_before() -> None:
    paragraphs = [f"Paragraph {n} says something short about topic {n}." for n in range(40)]
    text = "\n\n".join(paragraphs)
    pieces = chunk_markdown(text, "T", 300, 100)
    covers(text, pieces, 300)
    for before, after in itertools.pairwise(pieces):
        shared = before.stop - after.start
        assert 0 < shared <= 100 and text[after.start:before.stop] in before.text
    assert chunk_markdown(text, "T", 300, 0)[1].start == chunk_markdown(text, "T", 300, 0)[0].stop


def test_long_things_are_cut_where_they_can_be() -> None:
    """A long paragraph at sentence ends, a long table at line ends, a long word-less line in the middle of a word; no
    piece is over the size; text that is only blanks has no pieces."""
    prose = " ".join(f"This is sentence number {n}, and it ends here." for n in range(60))
    pieces = chunk_markdown(prose, "T", 200, 0)
    covers(prose, pieces, 200)
    assert all(p.text.rstrip().endswith(".") for p in pieces[:-1])
    table = "| a | b |\n|---|---|\n" + "\n".join(f"| {n} | row {n} |" for n in range(80))
    rows = chunk_markdown(table, "T", 120, 0)
    covers(table, rows, 120)
    assert all(p.text.endswith("|\n") or p is rows[-1] for p in rows)
    word = "x" * 1000
    covers(word, chunk_markdown(word, "T", 300, 50), 300)
    assert chunk_markdown("  \n\n \n", "T", 300, 50) == [] and chunk_markdown("", "T", 300, 50) == []


def test_headings_do_not_repeat_the_title_and_unicode_is_fine() -> None:
    text = "# Quad notes\n\nΤο drone πετάει καλά.\n\n## Τρίτο μέρος\n\nΚείμενο."
    pieces = chunk_markdown(text, "quad notes", 30, 0)
    covers(text, pieces, 30)
    assert pieces[0].heading == "quad notes" and pieces[-1].heading == "quad notes › Τρίτο μέρος"


def test_cutting_always_ends_and_always_covers() -> None:
    """Random documents of lines, blanks, headings, fences and long runs: whatever the size and overlap."""
    rng = random.Random(3)
    atoms = ["word " * 3, "A sentence. Another one! A third?", "# H1", "## H2 title", "```", "~~~", "", "", "",
             "| a | b |", "- item", "x" * 150, "a b  c\t d", "### H3 ##"]
    for _ in range(300):
        text = normalize_text("\n".join(rng.choice(atoms) for _ in range(rng.randint(1, 60))))
        size, overlap = rng.choice((120, 200, 500, 1000)), rng.choice((0, 30, 100))
        pieces = chunk_markdown(text, "Title", size, min(overlap, size // 2))
        if text.strip():
            covers(text, pieces, size)
        else:
            assert pieces == []


def test_text_is_decoded_and_normalized() -> None:
    assert decode_text(b"\xef\xbb\xbfHello") == "Hello" and decode_text("Ωμέγα".encode()) == "Ωμέγα"
    assert decode_text("Καλημέρα κόσμε, τι κάνεις σήμερα;".encode("cp1253")) == "Καλημέρα κόσμε, τι κάνεις σήμερα;"
    assert decode_text("Hi!".encode("utf-16")) == "Hi!"
    assert normalize_text("a  \r\nb\r\n\r\n\r\n\r\nc\x00\x0cd\x07\n") == "a\nb\n\nc\n\nd"


def test_an_excerpt_shows_where_the_words_are() -> None:
    text = " ".join(f"filler{n}" for n in range(100)) + " the invoice from Acme " + " ".join(f"tail{n}" for n in range(100))
    short = excerpt(text, ["invoice"], 200)
    assert "invoice" in short and short.startswith("…") and short.endswith("…") and len(short) <= 204
    assert excerpt(text, ["nothing"], 100).startswith("filler0") and excerpt("small", ["x"], 100) == "small"


# ---------------------------------------------------------------------------------------------------- the vectors
def test_vectors_are_kept_as_int8_and_sign_bits() -> None:
    rng = np.random.default_rng(1)
    vectors = rng.standard_normal((50, 512)).astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    back = dequantize(quantize(vectors))
    assert (back * vectors).sum(axis=1).min() > 0.998  # the cosine of a vector with its int8 copy
    assert len(quantize(vectors)[0]) == 4 + 512
    codes = sign_codes(vectors)
    assert codes.shape == (50, 8) and codes.dtype == np.uint64
    odd = sign_codes(rng.standard_normal((3, 100)).astype(np.float32))  # not a multiple of 64: padded
    assert odd.shape == (3, 2)
    zero = quantize(np.zeros((1, 8), dtype=np.float32))
    assert dequantize(zero).tolist() == [[0.0] * 8]


def test_distances_between_codes_and_the_nearest_rows_are_what_plain_counting_gives() -> None:
    """The quick ways (bit counts added in one word; counting distances instead of sorting them) agree with the slow ones,
    for the usual eight words of a code and for others, across the blocks the scan is made in."""
    rng = np.random.default_rng(3)
    for words in (1, 3, 8, 16):
        for n in (1, 7, documents.BLOCK + 1000):
            codes = rng.integers(0, 2**64, (n, words), dtype=np.uint64)
            code = rng.integers(0, 2**64, words, dtype=np.uint64)
            plain = np.unpackbits((codes ^ code).view(np.uint8), axis=1).sum(axis=1)
            assert (documents.hamming(codes, code) == plain).all(), (words, n)
    distance = rng.integers(0, 30, 5000).astype(np.uint16)
    distance[rng.integers(0, 5000, 400)] = 31  # left out
    for count in (1, 50, 777, 4000):
        by_sorting = np.lexsort((np.arange(5000), distance))[:count]
        assert sorted(documents.nearest_rows(distance, count, 31).tolist()) == sorted(by_sorting.tolist()), count


def test_the_nearest_vectors_are_found_without_looking_at_all_of_them() -> None:
    """Sign codes, then the query's own values against them: on clustered data most of the true nearest are found, the
    rest of a folder is never returned, and deleted pieces never come back."""
    rng = np.random.default_rng(5)
    centres = rng.standard_normal((40, 128)).astype(np.float32)
    vectors = (centres[rng.integers(0, 40, 6000)] + 0.7 * rng.standard_normal((6000, 128))).astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    codes = documents._Codes()
    folders = (np.arange(6000) % 3).astype(np.int32)
    codes.reset(10, 2)  # too small: it grows
    codes.add(np.arange(1, 6001, dtype=np.int64), folders, sign_codes(vectors))
    hits = total = 0
    for q in vectors[rng.integers(0, 6000, 40)]:
        exact = set((np.argsort(-(vectors @ q))[:10] + 1).tolist())
        found = codes.nearest(q, None)
        assert len(found) == documents.REFINE
        hits += len(exact & set(found.tolist()))
        total += 10
    assert hits / total > 0.9
    mine = codes.nearest(vectors[0], 1)
    assert len(mine) and all(folders[i - 1] == 1 for i in mine)
    codes.remove(list(range(1, 3001)))
    assert codes.count == 3000 and codes.size < 6000  # more than a fifth went: compacted
    assert all(i > 3000 for i in codes.nearest(vectors[10], None))
    codes.remove([5, 10 ** 9])  # gone already, never there
    assert codes.count == 3000


# ---------------------------------------------------------------------------------------------------- the folders
def test_only_folders_he_may_read_are_used(tmp_path: Path) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    for path in (home / "vault", home / "vault" / "inner", home / ".ssh", data / "memory", home / "other"):
        path.mkdir(parents=True)
    (home / "file.txt").write_text("x")
    link = home / "linked"
    link.symlink_to(home / "other")
    found = check_folders([str(home / "vault"), "relative/path", str(home / "nope"), str(home / "file.txt"),
                           str(home / ".ssh"), str(home), str(data), str(data / "memory"), str(home / "vault" / "inner"),
                           str(link), "~/other"], [home / ".ssh", data])
    errors = {f.shown.replace(str(home), "~"): f.error for f in found}
    assert errors["~/vault"] == ""
    assert "whole path" in errors["relative/path"] and "no such folder" in errors["~/nope"]
    assert errors["~/file.txt"] == "it is not a folder" and "never read" in errors["~/.ssh"]
    assert "overlaps" in errors["~"] and "never read" in errors[str(data)] and "never read" in errors[str(data / "memory")]
    assert "overlaps" in errors["~/vault/inner"]
    assert errors["~/linked"] == "" and "overlaps" in errors["~/other"]  # a link is resolved: it is the same folder
    assert next(f for f in found if f.shown == str(link)).path == (home / "other").resolve()


# ---------------------------------------------------------------------------------------------------- the service
class Words:
    """A stand-in embedder: each word of three or more letters points its own way, so texts sharing words are close."""

    def __init__(self) -> None:
        self.id = "words-v1"

    def encode(self, texts: list[str]) -> np.ndarray:
        import re
        import zlib

        out = np.zeros((len(texts), 64), dtype=np.float32)
        for row, text in zip(out, texts, strict=True):
            for word in re.findall(r"[a-z]{3,}", text.lower()):
                row[zlib.crc32(word.encode()) % 64] += 1
        return out / np.maximum(np.linalg.norm(out, axis=1, keepdims=True), 1e-9)

    def encode_query(self, text: str) -> np.ndarray:
        return self.encode([text])[0]


FILES = {
    "notes/drone-pid.md": "# Drone\n\nThe quad flies well. PID values are 48, 52 and 38.\n\n## Tuning\n\nRaise P until it "
                          "oscillates, then back off a little.\n",
    "recipes/soup.md": "# Soup\n\nBoil water. Add onions, carrots and lentils. Simmer for forty minutes.\n",
    "todo.txt": "buy new propellers\ncall the plumber about the leak\n",
    "ignored.log": "not a document at all",
    ".hidden/secret.md": "# Secret\n\nthe hidden vault code is 9137\n",
    "node_modules/pkg/readme.md": "# pkg\n\nnode module readme\n",
}


@dataclass
class Rig:
    """A documents service over a real folder, with what it published and recorded."""

    docs: Documents
    cfg: Config
    folder: Path
    events: list[tuple[str, dict]] = field(default_factory=list)
    records: list[tuple[str, dict]] = field(default_factory=list)

    def put(self, rel: str, text: str | bytes) -> Path:
        path = self.folder / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text if isinstance(text, bytes) else text.encode())
        return path

    def paths(self, hits) -> list[str]:
        return [h.path.removeprefix(f"{self.folder}/") for h in hits]


async def build(tmp_path: Path, files: dict[str, str | bytes] | None = None, embedder: object | None = None,
                *, sync: bool = True, **settings: object) -> Rig:
    """A service over ~/docs with these files, read once."""
    cfg = default_config()
    folder = tmp_path / "home" / "docs"
    folder.mkdir(parents=True, exist_ok=True)
    cfg.documents.folders = [str(folder)]
    cfg.documents.workers = 1
    for key, value in settings.items():
        setattr(cfg.documents, key, value)
    events: list[tuple[str, dict]] = []
    records: list[tuple[str, dict]] = []
    docs = Documents(cfg, embedder or Words(), publish=lambda kind, **data: events.append((kind, data)),  # type: ignore[arg-type]
                     record=lambda kind, **data: records.append((kind, data)))
    rig = Rig(docs, cfg, folder, events, records)
    for rel, text in (FILES if files is None else files).items():
        rig.put(rel, text)
    if sync:
        await docs.sync()
    return rig


def scenario(tmp_path: Path, body: Callable[[Rig], Awaitable[None]], files: dict[str, str | bytes] | None = None,
             embedder: object | None = None, **settings: object) -> None:
    """Build a service, run the body, and stop the service."""
    async def go() -> None:
        rig = await build(tmp_path, files, embedder, **settings)
        try:
            await body(rig)
        finally:
            await rig.docs.stop()

    asyncio.run(go())


def test_documents_are_found_by_what_they_say_and_by_their_words(tmp_path: Path) -> None:
    """Markdown and text are indexed; hidden, excluded and other kinds of files are not. A question finds the document
    by keywords and by meaning, with where in it the piece is; a rare word alone finds it too."""
    async def body(r: Rig) -> None:
        hits, note = r.docs.search("what are the drone PID values")
        assert r.paths(hits)[0] == "notes/drone-pid.md" and note == ""
        assert hits[0].heading == "drone-pid › Drone" and "48, 52 and 38" in hits[0].text
        assert r.paths(r.docs.search("lentils onions soup")[0])[0] == "recipes/soup.md"
        assert r.paths(r.docs.search("plumber leak")[0])[0] == "todo.txt"
        assert "secret.md" not in " ".join(r.paths(r.docs.search("hidden vault code 9137", limit=20)[0]))
        everything, total = r.docs.files()
        assert sorted(p.removeprefix(f"{r.folder}/") for p in everything) == [
            "notes/drone-pid.md", "recipes/soup.md", "todo.txt"] and total == 3
        assert r.docs.files(pattern="*.MD")[1] == 2 and r.docs.files(pattern="recipes/*")[0] == [
            f"{r.folder}/recipes/soup.md"]
        tuned = r.docs.search("raise P until it oscillates")[0][0]
        assert tuned.heading == "drone-pid › Drone › Tuning" or "oscillates" in tuned.text

    scenario(tmp_path, body)


def test_a_document_is_read_back_exactly_from_the_index(tmp_path: Path) -> None:
    """``read`` gives the text in the pieces' coordinates: all of it, a stretch, past the end; by the whole path or the
    path below the folder."""
    big = "\n\n".join(f"Paragraph {n} talks about subject number {n} for a while." for n in range(200))

    async def body(r: Rig) -> None:
        for rel, text in (("notes/drone-pid.md", FILES["notes/drone-pid.md"]), ("big.md", big)):
            whole = r.docs.read(str(r.folder / rel), chars=10 ** 6)
            assert whole.text == normalize_text(str(text)) and whole.total == len(whole.text) and whole.start == 0
            assert r.docs.read(rel, 7, 25).text == whole.text[7:32]
        page = r.docs.read(str(r.folder / "big.md"), 4000, 1000)
        assert page.text == normalize_text(big)[4000:5000] and (page.start, page.stop) == (4000, 5000)
        tail = r.docs.read("big.md", 10 ** 9)
        assert tail.text == "" and tail.start == tail.stop == tail.total
        assert len(r.docs.read("big.md").text) == r.cfg.documents.read_chars
        hit = r.docs.search("subject number 120")[0][0]
        assert r.docs.read(hit.path, hit.start, hit.stop - hit.start).text == hit.text  # the excerpt is the whole piece

    scenario(tmp_path, body, {**FILES, "big.md": big}, chunk_chars=500, chunk_overlap=100)


def test_only_what_changed_is_read_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing changed: nothing is read. An edited file's old words go and its new ones come; a deleted file, a renamed
    one and a deleted folder disappear from search, the memory of the sign codes included; a new file appears."""
    async def body(r: Rig) -> None:
        indexed: list[int] = []
        real = r.docs._index_docs
        monkeypatch.setattr(r.docs, "_index_docs", lambda docs: (indexed.append(len(docs)), real(docs))[1])
        r.docs.reconfigure()
        await r.docs.sync()
        assert indexed == [] and len(r.docs.files()[0]) == 3
        path = r.folder / "notes/drone-pid.md"
        path.write_text("# Drone\n\nNow the gains are 60, 70 and 80.\n")
        r.docs.changed([path])
        await r.docs.sync()
        assert indexed == [1]
        texts = " ".join(h.text for h in r.docs.search("drone gains values")[0])
        assert "60, 70 and 80" in texts and "48, 52" not in texts
        before = r.docs._codes.count
        path.rename(r.folder / "notes/quad.md")
        r.docs.changed([path, r.folder / "notes/quad.md"])
        await r.docs.sync()
        assert r.paths(r.docs.search("gains 60 70 80")[0])[0] == "notes/quad.md" and r.docs._codes.count == before
        shutil.rmtree(r.folder / "recipes")
        r.docs.changed([r.folder / "recipes"])
        await r.docs.sync()
        assert "recipes/soup.md" not in r.paths(r.docs.search("lentils onions soup")[0]) and r.docs._codes.count < before
        (r.folder / "todo.txt").unlink()
        r.put("fresh/new.md", "# New\n\nA brand new note about parachutes.\n")
        r.docs.changed([r.folder / "todo.txt", r.folder / "fresh"])
        await r.docs.sync()
        assert r.paths(r.docs.search("parachutes")[0])[0] == "fresh/new.md" and r.docs.search("plumber leak")[0] == []
        assert sorted(p.removeprefix(f"{r.folder}/") for p in r.docs.files()[0]) == ["fresh/new.md", "notes/quad.md"]
        with r.docs._db.reading() as conn:  # nothing of the gone documents is left in any table
            assert conn.execute("SELECT COUNT(*) FROM doc_chunks").fetchone()[0] == conn.execute(
                "SELECT COUNT(*) FROM doc_vecs").fetchone()[0] == conn.execute("SELECT COUNT(*) FROM doc_codes").fetchone()[0]
            assert conn.execute("SELECT COUNT(*) FROM doc_fts WHERE doc_fts MATCH 'lentils'").fetchone()[0] == 0

    scenario(tmp_path, body)


def test_links_and_paths_outside_the_folders_are_never_read(tmp_path: Path) -> None:
    """A link in a folder is not followed, a path the index does not hold has no text however it is spelled, and a
    file swapped for a link (or changed) after the scan is refused when it is opened."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.md").write_text("# Private\n\nthe bank pin is 4455\n")

    async def body(r: Rig) -> None:
        (r.folder / "link.md").symlink_to(outside / "private.md")
        (r.folder / "linkdir").symlink_to(outside)
        r.docs.reconfigure()
        await r.docs.sync()
        assert r.docs.search("bank pin 4455")[0] == []
        for spelled in (str(outside / "private.md"), "../outside/private.md", str(r.folder / "link.md"),
                        str(r.folder / "linkdir/private.md"), str(r.folder / ".." / "outside" / "private.md"),
                        "/etc/passwd", "~/.ssh/id_rsa", str(r.folder / ".hidden/secret.md"), "", "notes", "notes/",
                        str(r.folder)):
            with pytest.raises(DocumentsError, match="not among the indexed documents"):
                r.docs.read(spelled)
        real = r.folder / "todo.txt"
        status = real.lstat()
        job = documents._Job(1, "todo.txt", real, "txt", status.st_size, status.st_mtime_ns, status.st_ino)
        assert r.docs._read_text(job).text.startswith("buy new propellers")
        real.write_text("changed after the scan, and longer")
        assert "changed after it was found" in r.docs._read_text(job).error
        real.unlink()
        real.symlink_to(outside / "private.md")
        assert "can't be opened" in r.docs._read_text(job).error  # O_NOFOLLOW: the link is not followed

    scenario(tmp_path, body)


def test_settings_decide_what_is_read(tmp_path: Path) -> None:
    """A changed folder list, file type list, size limit or piece size is looked at again: gone folders and types leave
    the index, a larger limit lets a file in, another piece size makes the pieces again."""
    other = tmp_path / "home" / "more"

    async def body(r: Rig) -> None:
        d = r.cfg.documents
        assert len(r.docs.files()[0]) == 3
        d.extensions = ["md"]
        r.docs.reconfigure()
        await r.docs.sync()
        assert [p.removeprefix(f"{r.folder}/") for p in r.docs.files()[0]] == ["notes/drone-pid.md", "recipes/soup.md"]
        r.put("huge.md", "# Huge\n\n" + "words about elephants " * 70_000)  # about 1.5 MB
        d.max_file_mb = 1
        r.docs.reconfigure()
        await r.docs.sync()
        snapshot = r.docs.snapshot()
        assert snapshot["folders"][0]["errors"] == 1 and "huge.md" not in " ".join(r.docs.files()[0])  # type: ignore[index]
        assert not any("elephants" in h.text for h in r.docs.search("elephants")[0])
        d.max_file_mb = 5
        r.docs.reconfigure()
        await r.docs.sync()
        assert r.docs.snapshot()["folders"][0]["errors"] == 0 and "elephants" in r.docs.search("elephants")[0][0].text  # type: ignore[index]
        d.chunk_chars, d.chunk_overlap = 200, 20
        r.docs.reconfigure()
        await r.docs.sync()
        with r.docs._db.reading() as conn:
            assert conn.execute("SELECT MAX(length(rtrim(text, char(10)))) FROM doc_chunks").fetchone()[0] <= 200
        assert r.paths(r.docs.search("drone PID values")[0])[0] == "notes/drone-pid.md"
        other.mkdir()
        (other / "elsewhere.md").write_text("# Elsewhere\n\nA note about glaciers.\n")
        d.folders = [str(other)]
        r.docs.reconfigure()
        await r.docs.sync()
        assert [p.removeprefix(f"{other}/") for p in r.docs.files()[0]] == ["elsewhere.md"]
        assert r.docs.search("drone PID values")[0] == [] or "elsewhere.md" in r.paths(r.docs.search("drone")[0])
        assert r.docs.search("glaciers")[0][0].folder == str(other)
        d.folders = []
        with pytest.raises(DocumentsError, match="No folders are set up"):
            r.docs.search("glaciers")

    scenario(tmp_path, body)


def test_a_search_can_be_limited_to_one_folder(tmp_path: Path) -> None:
    other = tmp_path / "home" / "work"
    other.mkdir(parents=True)
    (other / "plan.md").write_text("# Plan\n\nThe drone project needs new propellers and a battery.\n")

    async def body(r: Rig) -> None:
        r.cfg.documents.folders = [str(r.folder), str(other)]
        r.docs.reconfigure()
        await r.docs.sync()
        both = r.paths(r.docs.search("drone propellers", limit=10)[0])
        assert "notes/drone-pid.md" in both and any(p.endswith("plan.md") for p in both)
        only = r.docs.search("drone propellers", folder="work", limit=10)[0]
        assert [h.folder for h in only] == [str(other)] and r.docs.search("drone", folder=str(r.folder))[0]
        assert all(h.folder == str(r.folder) for h in r.docs.search("drone propellers", folder="DOCS", limit=10)[0])
        assert r.docs.files(folder="work")[1] == 1
        with pytest.raises(DocumentsError, match="no folder called 'nope'"):
            r.docs.search("drone", folder="nope")

    scenario(tmp_path, body)


def test_a_document_has_at_most_two_results(tmp_path: Path) -> None:
    text = "\n\n".join(f"# Part {n}\n\nThe falcon is a bird of prey, part {n}." for n in range(12))

    async def body(r: Rig) -> None:
        hits = r.docs.search("falcon bird of prey", limit=10)[0]
        assert r.paths(hits).count("birds.md") == 2 and len(hits) == 2

    scenario(tmp_path, body, {"birds.md": text}, chunk_chars=100, chunk_overlap=0)


def test_what_cannot_be_read_is_said_not_hidden(tmp_path: Path) -> None:
    """A blank file, a file that is too large and a folder that is not there each show up as an error the user can see,
    with a word on the activity log, and no other document suffers."""
    async def body(r: Rig) -> None:
        snapshot = r.docs.snapshot()
        folder = snapshot["folders"][0]  # type: ignore[index]
        assert (folder["files"], folder["errors"], folder["indexing"]) == (3, 1, False)
        assert r.docs.read("notes/drone-pid.md").text and r.paths(r.docs.search("lentils")[0])[0] == "recipes/soup.md"
        with pytest.raises(DocumentsError, match="could not be read: it has no text"):
            r.docs.read("blank.md")
        assert "1 document(s) could not be read" in r.records[-1][1]["title"] and "blank.md" in r.records[-1][1]["detail"]
        assert "could not be read" in r.docs.status() and "blank.md" not in r.docs.status()  # no file names in status
        r.cfg.documents.folders = [str(r.folder), str(tmp_path / "nowhere")]
        r.docs.reconfigure()
        await r.docs.sync()
        broken = r.docs.snapshot()
        assert broken["folders"][1]["error"] == "there is no such folder" and "nowhere" in broken["last_error"]  # type: ignore[index,operator]
        assert "no such folder" in r.docs.status()

    scenario(tmp_path, body, {**FILES, "blank.md": "  \n\n  \n"})


def test_a_huge_text_is_read_up_to_the_limit_and_an_endless_line_is_cut(tmp_path: Path) -> None:
    """Only the first ``documents.max_chars`` of a text are read (never the whole file into memory), a line that never ends
    is cut into pieces like any other text, and a file of random bytes is text of replacement characters at worst."""
    huge = "alpha beta gamma. " * 700 + "\n\n" + "omegaword filler " * 200_000
    noise = bytes(random.Random(1).randrange(256) for _ in range(300_000))

    async def body(r: Rig) -> None:
        cap = r.cfg.documents.max_chars
        for name in ("huge.txt", "line.txt", "noise.txt"):
            page = r.docs.read(name, 0, 100_000)
            assert page.total == len(page.text) <= cap, name
        assert r.docs.read("huge.txt", 0, 100_000).total == cap
        assert r.docs.read("line.txt", 0, 100_000).text == "x" * cap
        assert not any("omegaword" in h.text for h in r.docs.search("omegaword filler")[0])  # past the limit: not read
        assert r.paths(r.docs.search("alpha beta gamma")[0])[0] == "huge.txt"
        assert r.docs.snapshot()["folders"][0]["chunks"] >= 3 * cap // 1300  # type: ignore[index]

    scenario(tmp_path, body, {"huge.txt": huge, "line.txt": "x" * 2_000_000, "noise.txt": noise}, max_chars=10_000)


def test_a_damaged_index_is_made_again_from_the_files(tmp_path: Path) -> None:
    async def go() -> None:
        data = tmp_path / "data"
        data.mkdir()
        (data / "documents.db").write_bytes(b"this is not a database, whatever it was once")
        r = await build(tmp_path)
        try:
            assert "damaged" in r.docs.snapshot()["last_error"]  # type: ignore[operator]
            assert r.paths(r.docs.search("lentils")[0])[0] == "recipes/soup.md"
            await r.docs.stop()
            (data / "documents.db").unlink()
            with sqlite3.connect(data / "documents.db") as db:  # a file of another layout
                db.execute("CREATE TABLE doc_files (x)")
                db.execute("PRAGMA user_version = 99")
            again = await build(tmp_path)
            assert again.docs._db is not None and again.docs._db.remade.startswith("it was made by another version")
            assert again.paths(again.docs.search("lentils")[0])[0] == "recipes/soup.md"
            await again.docs.stop()
        finally:
            await r.docs.stop()

    asyncio.run(go())


def test_vectors_follow_the_embedding_model(tmp_path: Path) -> None:
    """With no model there are only keywords; when one comes, every piece gets its vector and the sign codes load."""
    embedder = Words()
    embedder.id = ""

    async def body(r: Rig) -> None:
        assert r.docs._codes.count == 0 and r.paths(r.docs.search("drone PID values")[0])[0] == "notes/drone-pid.md"
        embedder.id = "words-v1"
        r.docs.reconfigure()
        await r.docs.sync()
        with r.docs._db.reading() as conn:
            chunks = conn.execute("SELECT COUNT(*) FROM doc_chunks").fetchone()[0]
        assert chunks and r.docs._codes.count == chunks
        embedder.id = "words-v2"
        r.docs.reconfigure()
        await r.docs.sync()
        assert r.docs._codes.count == chunks and r.paths(r.docs.search("drone PID values")[0])[0] == "notes/drone-pid.md"

    scenario(tmp_path, body, embedder=embedder)


def test_the_hud_hears_how_the_folders_are_doing(tmp_path: Path) -> None:
    async def body(r: Rig) -> None:
        await asyncio.sleep(0.8)
        kind, data = r.events[-1]
        assert kind == "documents" and data["last_error"] == ""
        (folder,) = data["folders"]
        assert folder == {"path": str(r.folder), "files": 3, "chunks": folder["chunks"], "errors": 0, "indexing": False,
                          "error": ""} and folder["chunks"] >= 3
        assert len(r.events) <= 4  # several changes in a moment make one event

    scenario(tmp_path, body)


def test_searching_while_indexing_never_fails(tmp_path: Path) -> None:
    """Searches on other threads during a long indexing see a consistent index: results or an honest 'not yet'."""
    files = {f"bulk/{n:03d}.md": f"# Note {n}\n\nAlpha beta gamma delta number {n} about topic {n % 7}.\n" * 5
             for n in range(300)}

    async def go() -> None:
        r = await build(tmp_path, files, sync=False)
        stop = False
        errors: list[BaseException] = []

        async def searcher() -> None:
            while not stop:
                try:
                    await asyncio.to_thread(r.docs.search, "alpha gamma topic")
                except DocumentsError:
                    pass
                except Exception as e:  # noqa: BLE001 (any failure is what the test looks for)
                    errors.append(e)
                    return
                await asyncio.sleep(0.005)

        task = asyncio.create_task(searcher())
        await r.docs.sync()
        stop = True
        await task
        assert errors == [] and len(r.docs.files(limit=1000)[0]) == 300
        assert len(r.docs.search("alpha gamma topic", limit=20)[0]) == 20
        await r.docs.stop()

    asyncio.run(go())


# ---------------------------------------------------------------------------------------------------- the workers
# These run real workers, and a worker runs in a sandbox: without one that works here (no bubblewrap, or a runner that
# forbids user namespaces) Orion refuses to read a document, as it should (test_a_machine_that_cannot_make_a_sandbox...).
needs_sandbox = pytest.mark.skipif(not sandbox.status().works, reason="bubblewrap can't make a sandbox on this machine")


@needs_sandbox
def test_real_documents_are_turned_into_text_by_real_workers(tmp_path: Path) -> None:
    """Word, PowerPoint, Excel, PDF, HTML, CSV and JSON files are converted by the worker processes and found by what
    they say; files that are not what their names say are errors that cost the others nothing."""
    base = tmp_path / "src"
    base.mkdir()
    make_docx(base / "report.docx", [("Heading1", "Quarterly report"), ("Normal", "The supplier in Oinofyta is late."),
                                     ("Heading2", "Risks"), ("Normal", "Currency swings hurt the margin.")])
    make_xlsx(base / "budget.xlsx", {"spend": [["month", "item", "cost"], ["jan", "gearbox", 1200], ["feb", "bearings", 98]]})
    make_pptx(base / "deck.pptx", [("Drone", "PID values are 48, 52 and 38"), ("Next steps", "Order new propellers")])
    make_pdf(base / "manual.pdf", [["Chapter one", "The flight controller boots in two seconds."],
                                   ["Chapter two", "Calibrate the compass before the first flight."]])

    async def body(r: Rig) -> None:
        for question, expected in (("supplier Oinofyta late", "report.docx"), ("gearbox bearings cost", "budget.xlsx"),
                                   ("propellers next steps", "deck.pptx"), ("calibrate the compass", "manual.pdf"),
                                   ("tree frogs live in ponds", "page.html"), ("bolts nuts qty", "table.csv"),
                                   ("zebra crossing", "data.json")):
            found = r.paths(r.docs.search(question)[0])
            assert found and found[0] == expected, (question, found)
        assert "# Quarterly report" in r.docs.read("report.docx", chars=10_000).text
        assert "<script>" not in r.docs.read("page.html").text and "evil()" not in r.docs.read("page.html").text
        snapshot = r.docs.snapshot()["folders"][0]  # type: ignore[index]
        assert snapshot["errors"] == 2 and snapshot["files"] == 8
        for broken in ("broken.pdf", "broken.docx"):
            with pytest.raises(DocumentsError, match="could not be read"):
                r.docs.read(broken)
        assert r.paths(r.docs.search("lentils soup")[0])[0] == "recipes/soup.md"

    files: dict[str, str | bytes] = {rel: (base / rel).read_bytes() for rel in
                                     ("report.docx", "budget.xlsx", "deck.pptx", "manual.pdf")}
    files |= {"page.html": "<html><body><h1>Pond life</h1><p>Tree frogs live in ponds.</p><script>evil()</script></body></html>",
              "table.csv": "name,qty\nbolts,12\nnuts,30\n", "data.json": '{"road": "zebra crossing"}',
              "broken.pdf": b"%PDF-1.4\n" + bytes(range(256)) * 20, "broken.docx": b"PK\x03\x04" + bytes(range(256)) * 20,
              "recipes/soup.md": FILES["recipes/soup.md"]}
    scenario(tmp_path, body, files)


@needs_sandbox
def test_a_pdf_is_read_for_its_text_or_with_its_tables(tmp_path: Path) -> None:
    """Both ways give the words of a PDF of prose (the quick one is what markitdown does itself when no page has a table);
    the setting that tells them apart is part of how the index is made."""
    make_pdf(tmp_path / "manual.pdf", [["Chapter one", "The flight controller boots in two seconds."],
                                       ["Chapter two", "Calibrate the compass before the first flight."]])
    texts = {}

    for tables in (False, True):
        async def body(r: Rig, tables: bool = tables) -> None:
            assert r.paths(r.docs.search("calibrate the compass")[0])[0] == "manual.pdf"
            texts[tables] = r.docs.read("manual.pdf").text

        scenario(tmp_path / str(tables), body, {"manual.pdf": (tmp_path / "manual.pdf").read_bytes()}, pdf_tables=tables)
    assert texts[False].split() == texts[True].split() and "boots in two seconds" in texts[False]

    def fingerprint(tables: bool) -> str:
        config = default_config()
        config.documents.pdf_tables = tables
        return Documents(config, Words(), publish=lambda *a, **k: None,  # type: ignore[arg-type]
                         record=lambda *a, **k: None)._fingerprint()

    assert fingerprint(False) != fingerprint(True)


@needs_sandbox
def test_a_conversion_that_runs_out_of_memory_is_stopped(tmp_path: Path) -> None:
    """A Word file whose text expands to 400 MB cannot take the worker's 512 MB: that document is an error, the next
    one is read."""
    bomb = tmp_path / "bomb.docx"
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
                         'package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/></Types>')
        package.writestr("_rels/.rels", '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/'
                         'package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
                         'officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        with package.open("word/document.xml", "w", force_zip64=True) as part:
            part.write(b'<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/'
                       b'2006/main"><w:body>')
            row = b"<w:p><w:r><w:t>aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa</w:t></w:r></w:p>" * 1000
            for _ in range(5000):
                part.write(row)
            part.write(b"</w:body></w:document>")

    async def body(r: Rig) -> None:
        started = time.monotonic()
        await r.docs.sync()
        assert time.monotonic() - started < 120
        with pytest.raises(DocumentsError, match="could not be read: (it needs more memory|the reader)"):
            r.docs.read("bomb.docx")
        assert r.paths(r.docs.search("lentils soup")[0])[0] == "recipes/soup.md"

    scenario(tmp_path, body, {"bomb.docx": bomb.read_bytes(), "recipes/soup.md": FILES["recipes/soup.md"]},
             worker_memory_mb=512, convert_timeout_s=60)


FAKE_WORKER = """
import json, os, sys, time
memory, seconds, chars, tables, *specs = sys.argv[1:]
def say(**fields):
    print(json.dumps(fields), flush=True)
for i, raw in enumerate(specs):
    ext, ino, size, mtime, path = json.loads(raw)
    name = os.path.basename(path)
    if name.startswith("quit"):
        sys.exit(3)
    say(start=i)
    if name.startswith("hang"):
        time.sleep(60)
    if name.startswith("crash"):
        os.abort()
    if name.startswith("flood"):
        sys.stdout.write("x" * 5_000_000)
        sys.stdout.flush()
        time.sleep(60)
    if name.startswith("bad"):
        say(i=i, ok=False, error="it is damaged")
        continue
    if name.startswith("env"):
        say(i=i, ok=True, text=json.dumps({"env": dict(os.environ), "cwd": os.getcwd(), "pid": os.getpid()}), cut=False)
        continue
    say(i=i, ok=True, text=open(path).read(), cut=False)
"""


@pytest.fixture
def fake_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Replace the conversion worker by a script that hangs, crashes, floods or fails by the name of the file; the
    returned list gets each launch's file names."""
    script = tmp_path / "fake_worker.py"
    script.write_text(FAKE_WORKER)
    monkeypatch.setattr(documents, "WORKER", (sys.executable, "-B", str(script)))
    monkeypatch.setattr(documents, "SILENCE_GRACE_S", 0)
    monkeypatch.setattr(documents, "STARTUP_S", 0)
    monkeypatch.setattr(documents, "confine", lambda cfg, argv: list(argv))  # (these test the worker's ways, not the jail)
    launches: list[list[str]] = []
    real = documents.launch

    async def spy(argv, **kwargs):
        launches.append([Path(json.loads(a)[4]).name for a in argv[7:]])
        return await real(argv, **kwargs)

    monkeypatch.setattr(documents, "launch", spy)
    return launches


def test_a_worker_that_crashes_costs_one_document_and_the_rest_go_to_another(tmp_path: Path,
                                                                            fake_worker: list[list[str]]) -> None:
    files = {"a.pdf": "alpha text one", "bad1.pdf": "x", "crash1.pdf": "y", "m.pdf": "mike text", "z.pdf": "zulu text"}

    async def body(r: Rig) -> None:
        assert fake_worker == [["a.pdf", "bad1.pdf", "crash1.pdf", "m.pdf", "z.pdf"], ["m.pdf", "z.pdf"]]
        assert sorted(p.removeprefix(f"{r.folder}/") for p in r.docs.files()[0]) == ["a.pdf", "m.pdf", "z.pdf"]
        with pytest.raises(DocumentsError, match="signal 6"):
            r.docs.read("crash1.pdf")
        with pytest.raises(DocumentsError, match="it is damaged"):
            r.docs.read("bad1.pdf")
        assert r.paths(r.docs.search("zulu")[0])[0] == "z.pdf"
        retry = r.docs._plan(True, 1, ())  # a crash may not happen again: tried at the next start, a damaged file not
        assert [j.rel for j in retry.jobs] == ["crash1.pdf"]

    scenario(tmp_path, body, files)


def test_a_worker_that_hangs_is_ended_and_the_documents_after_it_are_still_read(tmp_path: Path,
                                                                               fake_worker: list[list[str]]) -> None:
    async def body(r: Rig) -> None:
        with pytest.raises(DocumentsError, match="the reader hung on it"):
            r.docs.read("hang1.pdf")
        assert r.paths(r.docs.search("yankee")[0])[0] == "y.pdf" and fake_worker[-1] == ["y.pdf"]
        assert not r.docs._procs

    started = time.monotonic()
    scenario(tmp_path, body, {"a.pdf": "alpha", "hang1.pdf": "x", "y.pdf": "yankee"}, convert_timeout_s=0.5)
    assert time.monotonic() - started < 20


def test_a_worker_that_floods_or_will_not_start_is_an_error_not_a_problem(tmp_path: Path,
                                                                         fake_worker: list[list[str]]) -> None:
    async def body(r: Rig) -> None:
        with pytest.raises(DocumentsError, match="it printed too much"):
            r.docs.read("flood1.pdf")
        with pytest.raises(DocumentsError, match=r"the reader stopped \(exit 3\)"):
            r.docs.read("quit1.pdf")
        assert r.docs.search("anything")[0] == []

    scenario(tmp_path, body, {"flood1.pdf": "x", "quit1.pdf": "y"}, max_chars=10_000, convert_timeout_s=30)


def test_the_worker_knows_nothing_of_orion(tmp_path: Path, fake_worker: list[list[str]],
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """What the worker process is given: a clean environment (no key, no home, no Orion setting), nowhere to work, and
    nothing of the file but its path."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-secret")
    monkeypatch.setenv("ORION_CONFIG", "/somewhere/config.yaml")
    monkeypatch.setenv("DISCORD_TOKEN", "discord-secret")

    async def body(r: Rig) -> None:
        seen = json.loads(r.docs.read("env1.pdf").text)
        assert seen["env"] == documents.WORKER_ENV and seen["cwd"] == "/" and seen["pid"] != os.getpid()
        assert "secret" not in json.dumps(seen)

    scenario(tmp_path, body, {"env1.pdf": "x"})


def test_stopping_ends_a_worker_that_is_running(tmp_path: Path, fake_worker: list[list[str]]) -> None:
    async def go() -> None:
        r = await build(tmp_path, {"hang1.pdf": "x"}, sync=False, convert_timeout_s=60)
        task = asyncio.create_task(r.docs.sync())
        while not r.docs._procs:
            await asyncio.sleep(0.05)
        (process,) = r.docs._procs
        started = time.monotonic()
        await r.docs.stop()
        await asyncio.wait_for(task, 10)
        assert time.monotonic() - started < 5 and process.returncode is not None and not r.docs._procs

    asyncio.run(go())


# ---------------------------------------------------------------------------------------------------- the tools
LONG = "# Long\n\n" + "\n\n".join(f"Paragraph {n} says something about the harbour and its {n} boats." for n in range(60))


def hub_for(rig: Rig, words: Words):
    """A real hub with Orion's own tools, bound to this documents service."""
    from types import SimpleNamespace

    from orion.mcp import MCPHub, OpTrace
    from orion.tools import core
    from orion.tools.documents import DOCUMENTS

    controls = SimpleNamespace(rt=SimpleNamespace(cfg=rig.cfg, documents=rig.docs))
    return MCPHub(rig.cfg, {}, ops=OpTrace(lambda kind, **data: None, rig.cfg.hud, recent=10),
                  core=core.server(controls), core_groups={core.CORE.name: core.CORE, DOCUMENTS.name: DOCUMENTS},
                  embed=words.encode_query)  # type: ignore[arg-type]


def test_the_tools_search_read_and_list_and_what_they_read_taints_the_turn(tmp_path: Path) -> None:
    """Through a real hub, as the model meets them: a search, a read and a list each make their turn untrusted (a
    document may hold orders aimed at the model); the status, which names no document, does not."""
    from orion.base import CallContext

    words = Words()

    async def go() -> None:
        rig = await build(tmp_path, {**FILES, "long.md": LONG}, words)
        hub = hub_for(rig, words)
        await hub.configure(wait_s=5)

        async def call(name: str, turn: str, **args: object) -> str:
            return await hub.call("documents", name, args, CallContext("brain", turn))

        status = await call("documents_status", "t0")
        assert f"{rig.folder}: 4 documents" in status and "drone-pid" not in status  # no document is named
        assert not hub.is_tainted("t0")

        found = await call("documents_search", "t1", query="what are the drone PID values")
        assert found.startswith(f"[1] {rig.folder}/notes/drone-pid.md › drone-pid › Drone (characters 0-")
        assert "48, 52 and 38" in found and hub.is_tainted("t1")

        nothing = await call("documents_search", "t2", query="zzzzqqqq")
        assert nothing == "Nothing found in the documents." and hub.is_tainted("t2")  # it read the documents all the same

        first = await call("documents_read", "t3", path=f"{rig.folder}/long.md", chars=300)
        assert first.startswith(f"{rig.folder}/long.md (characters 0-300 of {len(LONG)}). Continue with start=300.\n# Long")
        second = await call("documents_read", "t3", path="long.md", start=300, chars=300)
        assert second.startswith(f"{rig.folder}/long.md (characters 300-600 of {len(LONG)}). Continue with start=600.")
        whole = await call("documents_read", "t4", path="long.md", start=0, chars=len(LONG) + 50)
        assert "Continue" not in whole.splitlines()[0] and whole.endswith("and its 59 boats.") and hub.is_tainted("t4")

        listed = await call("documents_list", "t5", pattern="*.md")
        assert listed.splitlines() == [f"{rig.folder}/{rel}" for rel in ("long.md", "notes/drone-pid.md", "recipes/soup.md")]
        assert hub.is_tainted("t5")
        assert "more: narrow it" in await call("documents_list", "t6", limit=1)
        assert await call("documents_list", "t6", pattern="*.xyz") == "No documents match."

        # What the model may not do: read what the folders do not hold, name a folder that is not one, ask for 0 results.
        for path in ("/etc/passwd", "../x.md", "~/.ssh/id_rsa", f"{rig.folder}/.hidden/secret.md"):
            refused = await call("documents_read", "t7", path=path)
            assert "not among the indexed documents" in refused, (path, refused)
        assert "no folder called '/etc'" in await call("documents_search", "t7", query="soup", folder="/etc")
        assert "limit" in await call("documents_search", "t7", query="soup", limit=0)

        rig.cfg.documents.folders = []
        assert "No folders are set up yet" in await call("documents_search", "t8", query="soup")
        await hub.stop()
        await rig.docs.stop()

    asyncio.run(go())


def test_orion_check_says_which_folders_cannot_be_read(tmp_path: Path) -> None:
    from orion.__main__ import _check_documents

    cfg = default_config()
    cfg.data_dir = str(tmp_path / "data")
    (tmp_path / "docs").mkdir()
    (tmp_path / "data").mkdir()
    said: list[tuple[bool, str, str]] = []

    def report(ok: bool, what: str, fix: str = "") -> None:
        said.append((ok, what, fix))

    _check_documents(cfg, report)
    assert said == []  # no folders chosen: nothing to say
    cfg.documents.folders = [str(tmp_path / "docs"), str(tmp_path / "nowhere"), str(tmp_path / "data")]
    _check_documents(cfg, report)
    assert [ok for ok, _, _ in said] == [True, False, False]
    assert said[1][1].endswith("nowhere (there is no such folder)") and "Customize, Documents" in said[1][2]
    assert "never read" in said[2][1]


def test_the_hud_asks_how_the_documents_are_doing(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from orion.pairing import Pairing
    from orion.server import COALESCE, create_app

    async def make() -> Rig:
        return await build(tmp_path, {**FILES, "bad1.pdf": b"%PDF-1.4\n" + bytes(range(256)) * 20})

    rig = asyncio.run(make())
    rt = SimpleNamespace(cfg=rig.cfg, controls=None, hub=None, documents=rig.docs, activity=None)
    app = create_app(rt, Pairing(tmp_path / "pair", rig.cfg.server))  # type: ignore[arg-type]  # a stub of the runtime
    with TestClient(app) as client:
        shown = client.get("/api/documents").json()
    assert shown == rig.docs.snapshot() and set(shown) == {"folders", "last_error"}
    (folder,) = shown["folders"]
    assert set(folder) == {"path", "files", "chunks", "errors", "indexing", "error"}
    assert folder["files"] == 3 and folder["errors"] == 1 and folder["indexing"] is False and folder["chunks"] >= 3
    assert COALESCE["documents"] == ("latest", "")  # a burst of progress is one message to the HUD
    asyncio.run(rig.docs.stop())


def test_the_runtime_holds_the_documents_and_a_changed_folder_list_reaches_them(tmp_path: Path) -> None:
    """The wiring in the running Orion: the service is built with the settings it reads live, his own tools include the
    documents', and a change to the folders (here a hand edit's dispatch) makes the service look again."""
    from orion.config import Settings
    from orion.runtime import Runtime
    from orion.tools.documents import DOCUMENTS

    async def go() -> None:
        settings = Settings.open(role="main")
        rt = Runtime(settings, microphone=False)
        rt.build()
        assert rt.documents.cfg is rt.cfg and DOCUMENTS.name in rt.hub._own
        rt.documents._full = False
        await rt._dispatch(["documents.folders"], "test")
        assert rt.documents._full and rt.documents._wake.is_set()
        rt.documents._full = False
        await rt._dispatch(["documents.search_results"], "test")
        assert not rt.documents._full  # a setting that is read when a tool asks needs no new look
        await rt.documents.stop()
        await asyncio.to_thread(rt.memory.close)

    asyncio.run(go())


def test_only_the_settings_that_change_what_is_indexed_make_it_again() -> None:
    from orion.config import Apply, meta

    for key in ("folders", "extensions", "excludes", "max_file_mb", "max_chars", "chunk_chars", "chunk_overlap"):
        assert Apply.DOCUMENTS in meta(f"documents.{key}").apply, key
    for key in ("search_results", "search_candidates", "result_chars", "read_chars"):
        assert meta(f"documents.{key}").apply == Apply.LIVE, key  # read the moment a tool asks


def test_changes_in_the_folders_are_noticed_while_he_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A new file, an edited one and a deleted one are followed by the real watcher, with nothing asking for it."""
    monkeypatch.setattr(documents, "WATCH_DEBOUNCE_MS", 100)

    async def until(check: Callable[[], bool], what: str) -> None:
        for _ in range(200):
            if check():
                return
            await asyncio.sleep(0.05)
        raise AssertionError(f"not noticed in 10 s: {what}")

    async def go() -> None:
        rig = await build(tmp_path, FILES, sync=False)
        loops = [asyncio.create_task(rig.docs.run()), asyncio.create_task(rig.docs.watch())]
        await until(lambda: rig.docs._db is not None and rig.paths(rig.docs.search("lentils soup")[0])[:1] == ["recipes/soup.md"],
                    "the first reading")
        await asyncio.sleep(0.5)  # the watcher is up
        rig.put("notes/new.md", "# Harbour\n\nthe zeppelin docks at pier nine\n")
        await until(lambda: rig.paths(rig.docs.search("zeppelin pier")[0])[:1] == ["notes/new.md"], "a new file")
        rig.put("notes/new.md", "# Harbour\n\nthe submarine docks at pier twelve\n")
        await until(lambda: "submarine" in rig.docs.read("notes/new.md").text, "an edit")
        assert "zeppelin" not in rig.docs.read("notes/new.md").text
        (rig.folder / "recipes/soup.md").unlink()
        await until(lambda: "recipes/soup.md" not in rig.paths(rig.docs.search("lentils soup")[0]), "a deletion")
        rig.put(".hidden/new.md", "# Hidden\n\nthe skipped words\n")  # a name the rules skip: no work at all
        await asyncio.sleep(0.6)
        assert not any("hidden" in path for path in rig.paths(rig.docs.search("skipped words")[0]))
        await rig.docs.stop()
        await asyncio.wait_for(asyncio.gather(*loops), 10)  # both end when he stops

    asyncio.run(go())


# ---------------------------------------------------------------------------------------------------- the sandbox
def test_the_worker_runs_in_a_jail_with_no_network_that_shows_it_only_its_folders(tmp_path: Path,
                                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker's command line is bubblewrap's: a clean environment, nothing shared (no network), the PC read-only,
    his documents' folders shown again read-only (``/tmp`` is empty in the jail), and his secrets hidden."""
    from orion import sandbox

    folder = tmp_path / "docs"
    folder.mkdir()
    cfg = default_config()
    cfg.documents.folders = [str(folder)]
    cfg.data_path.mkdir(parents=True, exist_ok=True)  # (only places that exist are hidden)
    monkeypatch.setattr(documents, "profile", lambda: SimpleNamespace(sandbox="bwrap"))
    monkeypatch.setattr(sandbox, "require", lambda jail: "/usr/bin/bwrap")
    line = documents.confine(cfg, ["python", "-m", "worker", "1"])
    assert line[0] == "/usr/bin/bwrap" and line[-4:] == ["python", "-m", "worker", "1"]
    assert "--unshare-all" in line and "--share-net" not in line and "--clearenv" in line
    shown = [line[i + 1] for i, part in enumerate(line) if part == "--ro-bind" and line[i + 1] == line[i + 2] == str(folder)]
    assert shown == [str(folder)]
    assert "PATH" in line and "HOME" in line  # the worker's own environment is the one that is set
    assert str(cfg.data_path.resolve()) in line  # his data folder is hidden from it


def test_in_a_container_the_container_is_the_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(documents, "profile", lambda: SimpleNamespace(sandbox="container"))
    assert documents.confine(default_config(), ["python", "-m", "worker"]) == ["python", "-m", "worker"]


def test_a_machine_that_cannot_make_a_sandbox_reads_nothing_and_says_why(monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker is never run bare: the error is the documents' error, and it is looked at again when the settings
    change."""
    from orion import sandbox

    def refuse(jail):
        raise sandbox.SandboxError("this machine can't make one: no bubblewrap")

    monkeypatch.setattr(documents, "profile", lambda: SimpleNamespace(sandbox=""))
    monkeypatch.setattr(sandbox, "require", refuse)
    with pytest.raises(sandbox.SandboxError, match="no bubblewrap"):
        documents.confine(default_config(), ["python"])
