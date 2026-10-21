"""memory/vault: Markdown files parsed into units, entry rewrites, links, the lock and the map (files in tmp_path)."""

import threading
import time
from datetime import datetime
from types import SimpleNamespace

import pytest

from orion.memory.vault import (
    Vault,
    VaultBusy,
    concept_set,
    entry_key,
    is_concept,
    link_text,
    links_in,
    parse_units,
    slugify,
    strip_links,
)

NOT_CONCEPTS = ["the user", "timer"]


def local(*when: int) -> float:
    """Epoch seconds of a local wall-clock time (year, month, day, hour, minute)."""
    return time.mktime((*when, 0, 0, 0, -1))


def unit_file(vault: Vault, when: datetime) -> str:
    """The text of the journal file of that day."""
    return (vault.root / "journal" / f"{when:%Y-%m-%d}.md").read_text()


@pytest.fixture
def vault(tmp_path) -> Vault:
    """A fresh memory folder."""
    return Vault(tmp_path / "memory", SimpleNamespace(not_concepts=NOT_CONCEPTS))


def test_entries_keep_the_hand_written_head(vault) -> None:
    """The title and intro before the first bullet survive a rewrite; "* " bullets are entries too."""
    vault.file("user").write_text("# Me\n\nWritten by hand.\n\n* Likes tea.\n- Lives in Athens.\n")
    assert [u.text for u in vault.entries("user")] == ["Likes tea.", "Lives in Athens."]
    vault.write_entries("user", ["Likes tea.", "Has a cat\nnamed Mochi."])
    assert vault.file("user").read_text() == "# Me\n\nWritten by hand.\n\n- Likes tea.\n- Has a cat named Mochi.\n"
    vault.file("orion").write_text("")
    vault.write_entries("orion", ["Uses Firefox."])
    assert vault.file("orion").read_text().startswith("# Orion's notes\n") and vault.entries("orion")
    vault.write_entries("facts", [])
    assert not vault.file("facts").exists()  # not created just to hold nothing


def test_journal_notes_and_archive_parse_into_dated_units() -> None:
    """Journal sections carry their time and topics; note sections their title; archive lines their date."""
    day = ("# Thursday 02 October 2026\n\n## 09:12\n\nThe user asked about [[Tailscale]].\n\n"
           "Topics: [[Raspberry Pi]]\n\n## 21:40\n\nShort.\n")
    first, second = parse_units("journal/2026-10-02.md", day, 0.0)
    assert (first.ts, first.anchor, second.anchor) == (local(2026, 10, 2, 9, 12), "09:12", "21:40")
    assert first.concepts == ("Tailscale", "Raspberry Pi") and first.plain == "The user asked about Tailscale."
    edited = parse_units("journal/2026-10-02.md", day.replace("asked about", "set up"), 0.0)
    assert edited[0].key == first.key
    note = "---\ncreated: 2026-09-28 10:00\n---\n\n# Drone PID\n\n## Roll\n\nP 0.8\n\n## Pitch\n\nP 0.9\n"
    roll, pitch = parse_units("notes/drone-pid.md", note, 0.0)
    assert roll.ts == pitch.ts == local(2026, 9, 28, 10, 0) and roll.text == "Drone PID\nRoll\nP 0.8"
    archive = "- 2026-10-02 14:03 · USER.md · replaced: The user lives in Athens.\n- garbled\n"
    athens, garbled = parse_units("archive.md", archive, 7.0)
    assert (athens.ts, athens.text) == (local(2026, 10, 2, 14, 3), "The user lives in Athens.")
    assert (garbled.ts, garbled.text) == (7.0, "garbled")


def test_links_concepts_and_slugs() -> None:
    """First mentions only, the longest name wins, other casing becomes an alias; linking is idempotent."""
    concepts = concept_set(("Raspberry Pi", "Pi", "ArduPilot"))
    text = "The user's raspberry pi runs ArduPilot. The Raspberry Pi is loud; a Pi 5 too."
    linked = link_text(text, concepts)
    assert linked == ("The user's [[Raspberry Pi|raspberry pi]] runs [[ArduPilot]]. The Raspberry Pi is loud; "
                      "a [[Pi]] 5 too.")
    assert link_text(linked, concepts) == linked and strip_links(linked) == text
    assert links_in("[[A|alias]] [[B#part]] [[ ]]") == ["A", "B"]
    assert entry_key("The user likes tea.") == entry_key("the user likes [[tea]]")
    assert not any(is_concept(name, {"the user", "timer"}) for name in ("the user", "Timer", "42", "x|y", "a"))
    assert (slugify("../../etc/passwd"), slugify(""), len(slugify("word " * 40))) == ("etcpasswd", "note", 59)


def test_writes_link_and_the_journal_is_kept_in_mind(vault) -> None:
    """A journal entry links known concepts and lists the rest as topics; today's journal fits its budget."""
    vault.write_entries("user", ["Has a [[Raspberry Pi]]."])
    when = datetime(2026, 10, 2, 9, 12).astimezone()
    unit = vault.append_journal("Set up the raspberry pi with him.", ["Tailscale", "the user"], when)
    assert unit.text == "Set up the [[Raspberry Pi|raspberry pi]] with him."  # "the user" is not a concept
    assert unit.concepts == ("Raspberry Pi", "Tailscale") and "Topics: [[Tailscale]]" in unit_file(vault, when)
    vault.append_journal("A much longer second entry about the weather and other things.", [], when)
    assert vault.journal_today(70, when) == "[09:12] A much longer second entry about the weather and other things."
    assert vault.journal_today(400, when).startswith("[09:12] Set up the raspberry pi with him.\n[09:12] A much")
    path = vault.save_note("Buy [[Raspberry Pi]] case", "", when)
    assert path.name == "buy-raspberry-pi-case.md" and vault.save_note("x", "Buy Raspberry Pi case", when) != path


def test_the_lock_is_reentrant_and_never_waits_forever(vault) -> None:
    """A holder can call any write; another writer gives up after its timeout."""
    with vault.locked(), vault.locked():
        vault.write_entries("orion", ["Nested writes work."])
    held, done = threading.Event(), threading.Event()

    def hold() -> None:
        """Hold the lock until the test is done."""
        with vault.locked():
            held.set()
            done.wait()

    holder = threading.Thread(target=hold)
    holder.start()
    held.wait()
    try:
        with pytest.raises(VaultBusy), vault.locked(timeout=0):
            pass
    finally:
        done.set()
        holder.join()


def test_map_ids_stay_put_and_nodes_have_text(vault) -> None:
    """Removing the first entry leaves the second one's node id unchanged; text comes from node(id)."""
    vault.write_entries("user", ["Lives in [[Athens]].", "Likes tea."])
    graph = vault.graph([("focus", "Quiet time.")], "Ada")
    ids = [n["id"] for n in graph["nodes"] if n["kind"] == "fact"]
    assert graph["counts"]["concept"] == 1 and {"source": ids[0], "target": "concept:athens"} in graph["links"]
    vault.write_entries("user", ["Likes tea."])
    assert [n["id"] for n in vault.graph([], "Ada")["nodes"] if n["kind"] == "fact"] == ids[1:]
    assert vault.node(ids[1]) == {"id": ids[1], "kind": "fact", "label": "Likes tea.", "text": "Likes tea.",
                                  "path": "USER.md"}
    assert vault.node("note:../../etc/passwd") is None and vault.node(ids[0]) is None
