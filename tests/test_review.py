"""memory/review: windows at turn boundaries without web-drawn answers, lenient parsing, operations, the schema, and a
review run end to end on a memory in tmp_path with a stand-in model."""

import asyncio
import json
import time
from typing import Any

from orion import redact
from orion.base import EventBus, LLMError
from orion.config import default_config
from orion.memory import MemorySystem
from orion.memory.review import REVIEW_SCHEMA, Reviewer, ReviewOut, to_ops, windows
from orion.memory.store import MessageRow
from orion.redact import Redactor


def turn(n: int, chars: int, *, base: int = 0) -> list[MessageRow]:
    """One turn: a user message and a reply, together about ``chars`` long."""
    half = "w" * (chars // 2 - 30)
    return [MessageRow(id=base + 2 * n + 1, role="user", content=half, source="voice", ts=1.0, conv=1, turn=f"t{n}"),
            MessageRow(id=base + 2 * n + 2, role="assistant", content=half, source="voice", ts=2.0, conv=1,
                       turn=f"t{n}")]


def test_windows_keep_turns_whole_and_leave_out_web_answers() -> None:
    """Ten 1,000-character turns make windows of at most three turns; a 5,000-character turn stands alone."""
    rows = [row for n in range(10) for row in turn(n, 1000)]
    cut = windows(rows, set(), 3000)
    assert all(len(w.text) <= 3000 for w in cut) and len(cut) == 4
    assert [w.last_id for w in cut] == [6, 12, 18, 20]  # three turns (six messages) each, then the last turn
    long = windows(turn(0, 5000), set(), 3000)
    assert len(long) == 1 and len(long[0].text) <= 3000 and "w" * 2000 in long[0].text
    hidden = windows(turn(0, 200), {"t0"}, 3000)[0].text
    assert "Orion: (answer drawn from web or app content omitted)" in hidden and hidden.count("w" * 50) == 1


def test_lenient_answers_operations_and_schema() -> None:
    """What fits is kept, the rest dropped; ids lose their marks; the schema has no $ref and requires every field."""
    out = ReviewOut.model_validate({"ops": [{"op": "ADD", "target": "user", "text": "The user has a cat named Mochi."},
                                            {"op": "BOGUS"}, 5], "journal": 7, "extra": 1})
    assert [o.op for o in out.ops] == ["ADD"] and out.journal == ""
    ops = to_ops(ReviewOut.model_validate({"ops": [
        {"op": "UPDATE", "id": "#3fa2", "text": "New."}, {"op": "UPDATE", "text": "Plain add."},
        {"op": "DELETE"}, {"op": "NOOP"}, {"op": "ADD", "target": "orion", "text": "Note one."},
        {"op": "ADD", "target": "orion", "text": "Note two."}]}), limits=(6, 1, 4))
    assert [(o.op, o.id, o.text) for o in ops] == [("UPDATE", "3fa2", "New."), ("ADD", "", "Plain add."),
                                                   ("ADD", "", "Note one.")]
    assert "$ref" not in json.dumps(REVIEW_SCHEMA)

    def strict(node: Any) -> bool:
        """Every object level requires all its properties and allows no others."""
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node and (
                    set(node.get("required", [])) != set(node["properties"]) or node.get("additionalProperties")):
                return False
            return all(strict(value) for value in node.values())
        return all(strict(item) for item in node) if isinstance(node, list) else True

    assert strict(REVIEW_SCHEMA)


class Model:
    """A stand-in brain whose JSON answers are given, or which fails."""

    def __init__(self, answer: dict[str, Any] | None) -> None:
        """Answer reviews with ``answer``; None fails like a model over its limit."""
        self.answer = answer
        self.prompts: list[str] = []

    async def json(self, prompt: str, schema: dict[str, Any], *, temperature: float) -> dict[str, Any]:
        """The given answer, or an LLMError."""
        self.prompts.append(prompt)
        if self.answer is None:
            raise LLMError("usage limit reached")
        return self.answer


class NoModel:
    """An embedder without a model."""

    id = ""

    def load(self) -> bool:
        """Nothing to load."""
        return False

    def encode(self, texts: object) -> None:
        """No vectors."""


def test_a_review_saves_facts_and_a_journal_and_retries_a_failure() -> None:
    """A window becomes a saved fact and a journal entry; a failed window stays unreviewed and the HUD hears once."""
    cfg = default_config()
    memory = MemorySystem.open(cfg, NoModel(), role="main")
    store, vault = memory.store, memory.vault
    conv = store.current_conv().id
    now = time.time()
    store.log_message("user", "My sister Maria is visiting next week.", source="voice", conv=conv, turn="t1", ts=now)
    store.log_message("assistant", "Lovely, sir. I'll remember it.", source="voice", conv=conv, turn="t1", ts=now + 1)
    events: list[dict[str, Any]] = []
    bus = EventBus()
    bus.subscribe(events.append)
    model = Model({"ops": [{"op": "ADD", "target": "user", "text": "The user's sister is called Maria."}],
                   "journal": "The user said their sister Maria visits next week.", "concepts": ["Maria"]})
    reviewer = Reviewer(cfg, memory, bus, lambda: model, is_busy=lambda: False)

    async def review() -> None:
        """Review now and wait for it to end."""
        reviewer.review_now()
        assert reviewer._task is not None
        await reviewer._task

    async def run() -> None:
        """Review, then add a turn and fail to review it."""
        await review()
        store.log_message("user", "Remind me about the dentist.", source="voice", conv=conv, turn="t2", ts=now + 9)
        model.answer = None
        await review()
        await reviewer.stop()

    asyncio.run(run())
    assert [u.plain for u in vault.entries("user")] == ["The user's sister is called Maria."]
    assert "[[Maria]]" in next((vault.root / "journal").glob("*.md")).read_text()
    reviews = [e for e in events if e["type"] == "memory_review"]
    assert reviews[0]["changes"] == ["+ The user's sister is called Maria.", "journal entry"]
    assert reviews[1]["error"] == "usage limit reached" and reviews[1]["pending"] == 1
    assert store.get_setting("reviewed_message_id") == "2"
    memory.close()


def test_a_review_never_shows_a_secret_to_the_model_nor_writes_one_to_the_journal() -> None:
    """A secret the vault gained after the chat is cut out of the transcript; one the model writes anyway is cut from the
    journal, and the writer refuses it as an entry."""
    secret = "hunter2-Zx9Qm4Lp7Rk2Vb8Nc5Td1Wh6"
    memory = MemorySystem.open(default_config(), NoModel(), role="main")
    store = memory.store
    conv = store.current_conv().id
    store.log_message("user", f"My wifi password is {secret}, remember it.", source="voice", conv=conv, turn="t1",
                      ts=time.time())
    model = Model({"ops": [{"op": "ADD", "target": "user", "text": f"The wifi password is {secret}."}],
                   "journal": f"The user gave their wifi password, {secret}.", "concepts": []})
    reviewer = Reviewer(default_config(), memory, EventBus(), lambda: model, is_busy=lambda: False)

    async def review() -> None:
        reviewer.review_now()
        assert reviewer._task is not None
        await reviewer._task

    with redact.installed(Redactor((), min_chars=8)) as redactor:
        redactor.learn([secret])
        asyncio.run(review())
    assert len(model.prompts) == 1 and secret not in model.prompts[0] and "My wifi password is [secret], remember it." in (
        model.prompts[0])
    assert not memory.vault.entries("user")  # (the entry is refused: it holds a secret)
    journal = next((memory.vault.root / "journal").glob("*.md")).read_text()
    assert secret not in journal and "The user gave their wifi password, [secret]." in journal
    memory.close()
