"""brain/context: the history window, byte-stable snapshots, the tail, ids and change lines, and tool and skill routing
with a word-hash embedder."""

import re
import zlib
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from orion.base import Query, Skill, Tool, ToolSet
from orion.brain.context import (
    Context,
    HistoryWindow,
    Router,
    change_lines,
    labels_of,
    render_snapshot,
    render_tail,
)
from orion.config import default_config
from orion.memory import MemorySystem
from orion.memory.store import MetaChange
from orion.memory.vault import short_ids
from orion.skills import Skills


class Words:
    """A stand-in embedder: each word of three or more letters points its own way."""

    id = "words-v1"
    loaded = True

    def load(self) -> bool:
        """Always ready."""
        return True

    def encode(self, texts: list[str]) -> np.ndarray:
        """Unit rows of word counts hashed into 512 dimensions."""
        out = np.zeros((len(texts), 512), dtype=np.float32)
        for row, text in zip(out, texts, strict=True):
            for word in re.findall(r"[^\W_]{3,}", text.lower()):
                row[zlib.crc32(word.encode()) % 512] += 1
        return out / np.maximum(np.linalg.norm(out, axis=1, keepdims=True), 1e-9)

    def encode_query(self, text: str) -> np.ndarray:
        """One query's row."""
        return self.encode([text])[0]


def test_the_history_is_trimmed_in_blocks_and_tool_results_compacted() -> None:
    """Four 30-character turns over a 100 budget: one trim, to at most half, starting with the user."""
    window = HistoryWindow(SimpleNamespace(history_chars=100, history_tool_result_chars=20))
    trims = [window.append_turn([{"role": "user", "content": "u" * 15, "turn": f"t{n}", "ts": float(n)},
                                 {"role": "assistant", "content": "a" * 15, "turn": f"t{n}", "ts": n + 0.5}])
             for n in range(4)]
    assert trims == [False, False, False, True] and sum(len(m["content"]) for m in window.messages()) <= 50
    assert window.messages()[0]["role"] == "user" and window.last_ts == 3.5 and window.messages() is window.messages()
    window.append_turn([{"role": "tool", "content": "x" * 2000, "name": "read_file", "tool_call_id": "c1",
                         "turn": "t9"}])
    assert window.messages()[-1]["content"] == "x" * 19 + "…"  # history_tool_result_chars, the "…" included


def test_snapshots_are_byte_stable_and_tails_small() -> None:
    """The same inputs give the same bytes; the tail is one line until something needs saying."""
    labels = labels_of(default_config().prompts)
    snapshot = {"city": "Athens", "user": [("3fa2", "Has a cat.")], "orion": [],
                "skills": [("focus", "Quiet time.")], "journal": "[09:12] Talked about Tailscale.", "labels": labels}
    assert render_snapshot(**snapshot) == render_snapshot(**snapshot)
    assert render_snapshot(**snapshot).splitlines()[:3] == ["[memory]", "User's home city: Athens.",
                                                            "USER (what you know about the user):"]
    tail = {"when": "Current time: 22:10.", "channel": "spoken", "changes": [], "skill": None, "new_skills": [],
            "labels": labels, "max_recall_chars": 60}
    assert render_tail(recalled=[], **tail) == "Current time: 22:10. This reply will be spoken."
    recalled = render_tail(recalled=[("2 Oct · journal", "w" * 50), ("note: x", "v" * 50)], **tail)
    items = [line for line in recalled.splitlines() if line.startswith("- ")]
    assert sum(len(line.rsplit(": ", 1)[1]) for line in items) <= 60 and "[recalled]" in recalled
    assert short_ids(["3fa2b1", "3fa2c9", "9c01aa"]) == ["3fa2b", "3fa2c", "9c01"]
    changes = [MetaChange("+", "user", "k1", "Likes tea.", 1.0, "brain"), MetaChange("-", "user", "k2", "Gone.", 2.0,
                                                                                     "brain")]
    assert change_lines(changes, {"k1": "3fa2"}) == ["+ [#3fa2] Likes tea.", "- Gone."]


def test_a_context_snapshot_holds_still_through_the_day(tmp_path: Path) -> None:
    """A snapshot made at 09:00 and one made at 21:00 the same day, files unchanged, have the same memory text."""
    cfg = default_config()
    memory = MemorySystem.open(cfg, Words(), role="tools")
    memory.vault.write_entries("user", ["Has a cat named Mochi."])
    skills = Skills(tmp_path / "skills")
    skills.write("focus", "Quiet time.", "1. mute")
    context = Context(cfg, memory, skills, Words())
    morning = context.snapshot(1, datetime(2026, 10, 3, 9, 0).astimezone())
    evening = context.snapshot(1, datetime(2026, 10, 3, 21, 0).astimezone())
    assert morning.memory == evening.memory and "focus" in morning.skills
    assert "- [#" in morning.memory and "Mochi" in morning.memory and morning.system.startswith("You are ORION")
    skills.write("movie-night", "Film setup.", "1. dim")
    tail = context.tail(now=datetime.now().astimezone(), channel="text", snapshot=morning, recalled=[], skill=None)
    assert "shown on screen" in tail and "- movie-night: Film setup." in tail
    memory.close()


def test_routing_core_recent_then_relevant() -> None:
    """Core tools first, a recent one, then the best matches; words alone rank without vectors; skills need a clear
    lead; find_tools never returns what is shown."""
    tools = {name: Tool(name, about, {"type": "object", "properties": {}}, "system", name, summary=about)
             for name, about in (("open", "open apps"), ("get_weather", "weather forecast"),
                                 ("volume", "speaker volume"),
                                 ("hass__turn_on", "turn on a light or lamp"), ("git__commit", "git commit changes"),
                                 ("spotify__play", "play a song or music"), ("find_files", "find a file"),
                                 ("set_timer", "start a timer"))}
    toolset = ToolSet(tools, 1)
    cfg = default_config()
    cfg.tools = cfg.tools.model_copy(update={"max_per_turn": 4, "always": ["open", "get_weather"], "recent_kept": 1,
                                             "min_relevant": 5})
    words = Words()
    router = Router(words, cfg)
    router.index_tools(toolset)
    lamp = Query("turn on the lamp in the living room", words.encode_query("turn on the lamp in the living room"))
    chosen = router.select(lamp, toolset, recent={"volume", "set_timer"})
    assert chosen[:3] == ["open", "get_weather", "set_timer"] and chosen[3] == "hass__turn_on" and len(chosen) == 8
    assert router.select(Query("play a song", None), toolset, recent=set())[2] == "spotify__play"
    found = router.search(Query("play a song", words.encode_query("play a song")), toolset,
                          exclude={"open", "get_weather", "volume"})
    assert found[0] == "spotify__play" and "open" not in found
    skills = {"movie-night": Skill("movie-night", "Dim the lights and start a film.", "", (), ("movie night",),
                                   Path("x")),
              "focus": Skill("focus", "Quiet time for work.", "", (), ("focus mode",), Path("y"))}
    router.index_skills(skills, 1)
    film = "dim the lights and start a film movie night"
    assert router.best_skill(Query(film, words.encode_query(film))) is skills["movie-night"]
    assert router.best_skill(Query("what time is it", words.encode_query("what time is it"))) is None
