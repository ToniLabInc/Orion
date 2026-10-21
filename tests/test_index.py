"""memory/index: time phrases, ranking and gates (pure), then a real index over files and a log in tmp_path with a
word-hash embedder: sync, search, recall, vocabulary and hand edits."""

import re
import time
import zlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from orion.base import Query, local_now
from orion.config import default_config
from orion.memory.index import (
    Candidate,
    Hit,
    Index,
    Span,
    fts_query,
    gate,
    pack,
    parse_span,
    recency_prior,
    rrf,
    vocabulary_scores,
)
from orion.memory.store import Store
from orion.memory.vault import Vault, entry_key

ATHENS = ZoneInfo("Europe/Athens")
NOW = datetime(2026, 10, 3, 22, 10, tzinfo=ATHENS)  # a Saturday


def day(month: int, d: int, hour: int = 0) -> float:
    """Epoch seconds of a local time in 2026, in Athens."""
    return datetime(2026, month, d, hour, tzinfo=ATHENS).timestamp()


@pytest.mark.parametrize(("text", "start", "end", "hard"), [
    ("what did we talk about yesterday", day(10, 2), day(10, 3), True),
    ("this morning", day(10, 3, 5), day(10, 3, 12), True),
    ("last night", day(10, 2, 18), day(10, 3, 5), True),
    ("on Monday", day(9, 28), day(9, 29), True),
    ("last week", day(9, 21), day(9, 28), True),
    ("this week", day(9, 28), NOW.timestamp(), True),
    ("three days ago", day(9, 30), day(10, 1), True),
    ("the day before yesterday", day(10, 1), day(10, 2), True),
    ("in September", day(9, 1), day(10, 1), True),
    ("last month", day(9, 1), day(10, 1), True),
    ("on 2 October", day(10, 2), day(10, 3), True),
    ("October 2nd", day(10, 2), day(10, 3), True),
    ("2026-10-02", day(10, 2), day(10, 3), True),
    ("recently", NOW.timestamp() - 7 * 86400, NOW.timestamp(), False),
])
def test_time_phrases(text: str, start: float, end: float, hard: bool) -> None:
    """Each phrase names the right stretch of local time."""
    span = parse_span(text, NOW)
    assert span is not None and (span.start, span.end, span.hard) == (start, end, hard)


def test_no_time_and_the_ranking_rules() -> None:
    """Non-times stay None; FTS queries are safe; fusion, gates, packing and boosts follow their formulas."""
    assert parse_span("set a timer for 5 minutes", NOW) is None and parse_span("my Monday.com account", NOW) is None
    assert fts_query('What did we talk about "routers" AND NEAR(x)?') == '"routers" OR "near"' and fts_query("") == ""
    scores = rrf([[1, 2, 3], [3, 1]], 60)
    assert scores[1] == pytest.approx(1 / 61 + 1 / 62) and scores[3] == pytest.approx(1 / 63 + 1 / 61)
    assert sorted(scores, key=scores.get, reverse=True) == [1, 3, 2]

    def c(kind: str = "journal", bm25: int | None = None, cos: float | None = None, cos_rank: int | None = None,
          concept: bool = False, in_span: bool = False) -> Candidate:
        """A candidate with these features."""
        return Candidate(1, kind, 0.0, "review", bm25, cos, cos_rank, concept, in_span)

    limits = {"window": 10, "min_cosine": 0.25, "solo_cosine": 0.55}
    assert gate(c(bm25=3, cos=0.3, cos_rank=7), span_hard=False, **limits) == "agree"
    assert gate(c(bm25=15, concept=True), span_hard=False, **limits) == "concept"
    assert gate(c(in_span=True), span_hard=True, **limits) == "span"
    assert gate(c(cos=0.7, cos_rank=40), span_hard=False, **limits) == "strong"
    assert gate(c(bm25=12, cos=0.5, cos_rank=30), span_hard=False, **limits) is None
    assert gate(c("command", bm25=1, cos=0.9, cos_rank=1), span_hard=False, **limits) is None  # never recalled

    def hit(chars: int) -> Hit:
        """A unit of this many characters."""
        return Hit(chars, "k", "journal", "journal/x.md", 0.0, "word " * (chars // 5), "review", 1.0)

    assert [len(h.text) for h in pack([hit(150), hit(150), hit(150)], budget=400, max_items=3)] == [150, 150]
    cut = pack([hit(500)], budget=400, max_items=3)
    assert len(cut) == 1 and len(cut[0].text) <= 400 and cut[0].text.endswith("…")
    assert recency_prior(0, 30, 0.3) == 0.3 and recency_prior(30, 30, 0.3) == pytest.approx(0.15)
    now = time.time()
    ranked = vocabulary_scores([("Athens", 20, now - 200 * 86400), ("Tailscale", 5, now - 86400)], now, 30)
    assert [name for name, _ in ranked] == ["Tailscale", "Athens"]


class Words:
    """A stand-in embedder: each word of three or more letters points its own way, so texts sharing words are close."""

    id = "words-v1"

    def encode(self, texts: list[str]) -> np.ndarray:
        """Unit rows of word counts hashed into 64 dimensions."""
        out = np.zeros((len(texts), 64), dtype=np.float32)
        for row, text in zip(out, texts, strict=True):
            for word in re.findall(r"[a-z]{3,}", text.lower()):
                row[zlib.crc32(word.encode()) % 64] += 1
        return out / np.maximum(np.linalg.norm(out, axis=1, keepdims=True), 1e-9)

    def encode_query(self, text: str) -> np.ndarray:
        """One query's row."""
        return self.encode([text])[0]


def test_an_index_over_files_and_turns(tmp_path) -> None:
    """Files and turns are indexed; search finds them; recall gates by time and trust and skips this conversation."""
    cfg = default_config()
    vault = Vault(tmp_path / "memory", cfg.retrieval)
    store = Store(tmp_path / "orion.db", cfg.memory, role="tools")
    store.ensure_schema()
    words = Words()
    index = Index(store, vault, words, cfg)
    now = local_now()
    vault.write_entries("user", ["The user's sister is called [[Maria]].", "The user lives in Athens."])
    vault.write_entries("unverified", ["The user owns a yacht in Monaco."])
    vault.append_journal("The user asked how to set up [[Tailscale]] on the Raspberry Pi.", ["Tailscale"],
                         (now - timedelta(days=1)).replace(hour=14, minute=0))
    old = store.new_conv("start", time.time() - 3 * 86400).id
    store.log_message("user", "How do I flash ArduPilot on the drone?", source="hud", conv=old, turn="t1",
                      ts=time.time() - 3 * 86400)
    store.log_message("assistant", "You flash ArduPilot on the drone with Mission Planner over USB, sir.",
                      source="hud", conv=old, turn="t1", ts=time.time() - 3 * 86400 + 5)
    report = index.sync_files()
    assert index.sync_messages() == 1 and (report.files, report.units_added) == (4, 4)  # MEMORY.md is empty

    def ask(text: str) -> Query:
        """A request with its vector."""
        return Query(text, words.encode_query(text))

    found = index.search(ask("sister Maria"), k=5, include_core=True)
    assert found and found[0].kind == "user" and "Maria" in found[0].text and found[0].tag() == "about the user"
    assert all(h.kind != "user" for h in index.search(ask("sister Maria"), k=5, include_core=False))
    assert index.search(ask("yacht Monaco"), k=5, include_core=False)[0].tag().startswith("unverified")
    yesterday = index.recall(ask("what did we talk about yesterday"), conv=99, exclude=(), now=now)
    assert [h.kind for h in yesterday.items] == ["journal"] and "Tailscale" in yesterday.items[0].text
    assert not index.recall(ask("does the user own a yacht in Monaco"), conv=99, exclude=(), now=now).items
    drone = index.recall(ask("flash ArduPilot on the drone"), conv=99, exclude=(), now=now)
    assert [h.kind for h in drone.items] == ["turn"] and drone.items[0].tag().endswith("chat")
    assert not index.recall(ask("flash ArduPilot on the drone"), conv=old, exclude=(), now=now).items
    assert store.meta_rows([entry_key("The user lives in Athens.")])[entry_key("The user lives in Athens.")].source \
        == "hand"
    assert set(index.vocabulary(10)) == {"Maria", "Tailscale"}  # untrusted names never reach the recogniser
    vault.write_entries("user", ["The user lives in Athens."])  # edited by hand
    assert index.sync_files().units_removed == 1
    gone = store.meta_rows([entry_key("The user's sister is called Maria.")])
    assert next(iter(gone.values())).why == "edited by hand"
    assert not any("Maria" in h.text for h in index.search(ask("sister Maria"), k=5, include_core=True))
    store.close()


def test_spans_are_hard_or_soft() -> None:
    """A vague phrase only adds a list; a time question filters."""
    assert parse_span("what did I say recently", NOW) == Span(NOW.timestamp() - 7 * 86400, NOW.timestamp(), False,
                                                               "recently")
