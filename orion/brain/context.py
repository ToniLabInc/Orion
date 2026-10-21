"""What the model reads: the stable system text, the frozen memory snapshot, the small per-turn tail, the history
window, and which tools and skills it is shown.

The layout keeps prompt prefixes byte-stable, so every provider's prompt cache keeps working:
- SYSTEM: the persona and both reply styles (``prompts.persona``, ``voice_style``, ``text_style``); the same for every
  conversation until the user's name, honorific or those prompts change.
- SNAPSHOT: "[memory] ... [/memory]": the home city, USER.md and MEMORY.md with their ids, the skills (most recently
  used first) and today's journal. It holds no clock value and is frozen for a context epoch: a new conversation, a
  trimmed history or changed prompts start the next one. Memory written meanwhile reaches the model through the tail.
- TAIL, at the top of each user message: the time and whether the reply will be spoken, the 0-3 memories recalled
  for this request, the memory changes since the snapshot, a skill to follow now, skills added since.
- HISTORY: earlier turns exactly as they were sent, within ``brain.history_chars``; past the budget, the oldest whole
  turns go until half of it is left, so the prefix then stays the same for many turns instead of sliding every turn.

The router picks, for providers shown only part of a large toolbox, the core tools, the recently used ones and the
best matches for the request, from vectors computed once per toolbox version; it also spots an unmistakable skill and
answers ``find_tools``.
"""

import json
import re
import time
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final

import numpy as np

from orion import images
from orion.base import Message, Query, Skill, Tool, ToolSet, clip
from orion.memory.vault import FILES, short_ids

if TYPE_CHECKING:
    from numpy.typing import NDArray

    from orion.config import BrainConfig, Config, PromptsConfig
    from orion.inference import Embedder
    from orion.memory import MemorySystem
    from orion.memory.index import Hit
    from orion.memory.store import MessageRow, MetaChange
    from orion.skills import Skills

HISTORY_KEEP_RATIO: Final = 0.5  # a trimmed history keeps this share of brain.history_chars
DELTA_MAX_LINES: Final = 6  # memory changes listed in one tail
DELTA_MAX_CHARS: Final = 400  # ...and their characters
_WORD: Final = re.compile(r"[^\W_]{3,}")


# ---------------------------------------------------------------------------------------------------- pure parts
@dataclass(frozen=True, slots=True)
class Snapshot:
    """One context epoch: the system text and the frozen memory block.

    Attributes:
        conv: the conversation it was made for.
        built: when, in epoch seconds (memory changes after it appear in the tail).
        system: the system text.
        memory: the "[memory] ... [/memory]" block.
        skills: the skills it lists (others created later appear in the tail).
    """

    conv: int
    built: float
    system: str
    memory: str
    skills: frozenset[str]


def labels_of(prompts: "PromptsConfig") -> dict[str, str]:
    """The context labels of the prompts, by short name (for the pure renderers)."""
    return {"home_city": prompts.context_home_city, "user": prompts.context_user,
            "user_empty": prompts.context_user_empty, "notes": prompts.context_notes,
            "skills": prompts.context_skills, "journal": prompts.context_journal,
            "active_skill": prompts.context_active_skill, "recalled": prompts.context_recalled,
            "changes": prompts.context_changes, "reply": prompts.context_reply, "time": prompts.context_time}


def render_snapshot(*, city: str, user: Sequence[tuple[str, str]], orion: Sequence[tuple[str, str]],
                    skills: Sequence[tuple[str, str]], journal: str, labels: Mapping[str, str]) -> str:
    """The memory block, the same bytes for the same inputs (no clock value in it).

    Args:
        city: the user's home city ("" when unknown).
        user: USER.md's entries as (id, text).
        orion: MEMORY.md's entries as (id, text).
        skills: the skills listed, as (name, description).
        journal: today's journal lines ("" for none).
        labels: the context labels (``labels_of``).

    Returns:
        "[memory]\\n...\\n[/memory]".
    """
    lines = ["[memory]"]
    if city:
        lines.append(labels["home_city"].format(city=city))
    lines.append(labels["user"])
    lines += [f"- [#{i}] {text}" for i, text in user] or [labels["user_empty"]]
    if orion:
        lines.append(labels["notes"])
        lines += [f"- [#{i}] {text}" for i, text in orion]
    if skills:
        lines.append(labels["skills"])
        lines += [f"- {name}: {about}" for name, about in skills]
    if journal:
        lines.append(labels["journal"])
        lines += [f"- {line}" for line in journal.splitlines() if line.strip()]
    lines.append("[/memory]")
    return "\n".join(lines)


def render_tail(*, when: str, channel: str, recalled: Sequence[tuple[str, str]], changes: Sequence[str],
                skill: tuple[str, str] | None, new_skills: Sequence[tuple[str, str]], labels: Mapping[str, str],
                max_recall_chars: int) -> str:
    """The per-turn tail that heads the user's message.

    Args:
        when: the time line (``prompts.context_time`` filled in).
        channel: "spoken" or "shown on screen".
        recalled: memories recalled for this request, as (tag, text).
        changes: memory change lines ("+ [#a1b2] ...", "~ [#a1b2] ...", "- ...").
        skill: a skill to follow now, as (name, its instructions).
        new_skills: skills added since the snapshot, as (name, description).
        labels: the context labels (``labels_of``).
        max_recall_chars: the most characters of recalled text (``retrieval.recall_chars``).

    Returns:
        The tail: one line when nothing else applies.
    """
    lines = [f"{when} {labels['reply'].format(channel=channel)}"]
    if recalled:
        lines += ["[recalled]", labels["recalled"]]
        used = 0
        for tag, text in recalled:
            room = max_recall_chars - used
            if room <= 0:
                break
            shown = clip(text, room, flat=False)
            lines.append(f"- {tag}: {shown}")
            used += len(shown)
        lines.append("[/recalled]")
    if changes:
        lines.append(labels["changes"])
        lines += changes
    if skill is not None:
        lines += [labels["active_skill"].format(name=skill[0]), skill[1]]
    if new_skills:
        lines.append(labels["skills"])
        lines += [f"- {name}: {about}" for name, about in new_skills]
    return "\n".join(lines)


def change_lines(changes: Sequence["MetaChange"], ids: Mapping[str, str]) -> list[str]:
    """Memory changes as tail lines, the newest ``DELTA_MAX_LINES`` within ``DELTA_MAX_CHARS``.

    Args:
        changes: the changes, oldest first.
        ids: entry key -> id shown.

    Returns:
        "+ [#id] text" (learned), "~ [#id] text" (replaced), "- text" (removed), oldest first.
    """
    lines: list[str] = []
    used = 0
    for change in reversed(changes):
        ref = f" [#{ids[change.key]}]" if change.op != "-" and change.key in ids else ""
        line = f"{change.op}{ref} {change.text}"
        if len(lines) >= DELTA_MAX_LINES or used + len(line) > DELTA_MAX_CHARS:
            break
        lines.append(line)
        used += len(line)
    return lines[::-1]


def rank(qvec: "NDArray[np.float32] | None", query: str, names: Sequence[str], matrix: "NDArray[np.float32] | None",
         texts: Sequence[str]) -> list[tuple[str, float]]:
    """Rank items for a request: by cosine with their vectors, or (without vectors) by the words they share.

    Args:
        qvec: the request's unit vector, None without a model.
        query: the request.
        names: the items' names.
        matrix: their unit vectors (one row each), None without a model.
        texts: their texts (for the word fallback).

    Returns:
        (name, score), best first.
    """
    if qvec is not None and matrix is not None and len(names) and matrix.shape == (len(names), qvec.shape[0]):
        scores = matrix @ qvec
        order = np.argsort(-scores, kind="stable")
        return [(names[i], float(scores[i])) for i in order]
    words = set(_WORD.findall(query.lower()))
    scored = [(name, len(words & set(_WORD.findall(text.lower()))) / (len(words) or 1))
              for name, text in zip(names, texts, strict=True)]
    return sorted(scored, key=lambda item: -item[1])


# ---------------------------------------------------------------------------------------------------- history
class HistoryWindow:
    """Earlier turns of the conversation as they were sent, within a character budget, trimmed in blocks."""

    def __init__(self, brain: "BrainConfig") -> None:
        """Start empty.

        Args:
            brain: the live brain settings (``history_chars``, ``history_tool_result_chars``).
        """
        self.brain = brain
        self._turns: list[list[Message]] = []
        self._chars = 0
        self._view: tuple[Message, ...] = ()

    @property
    def last_ts(self) -> float:
        """When the last message was said (0 for an empty window)."""
        for turn in reversed(self._turns):
            for message in reversed(turn):
                if (ts := message.get("ts")) is not None:
                    return float(ts)
        return 0.0

    def messages(self) -> tuple[Message, ...]:
        """The history: the same tuple object until it changes."""
        return self._view

    def load(self, rows: Sequence["MessageRow"]) -> None:
        """Start from the logged messages of the current conversation (after a restart).

        Args:
            rows: user and assistant messages, oldest first.
        """
        self.clear()
        grouped: dict[str, list[Message]] = {}
        for row in rows:
            key = row["turn"] or f"m{row['id']}"
            message: Message = {"role": "user" if row["role"] == "user" else "assistant", "content": row["content"],
                                "turn": row["turn"] or key, "ts": row["ts"]}
            if row["role"] == "user":
                message["raw"] = row["content"]
            grouped.setdefault(key, []).append(message)
        self._turns = [turn for turn in grouped.values() if turn]
        self._recount()
        self._trim()
        self._view = tuple(m for turn in self._turns for m in turn)

    def append_turn(self, messages: Sequence[Message]) -> bool:
        """Add a finished turn (tool results compacted).

        Args:
            messages: the turn's messages: the user's (tail included), tool calls and results, the reply.

        Returns:
            True when the window was trimmed (the prompt prefix changes: a new context epoch should start).
        """
        if not messages:
            return False
        self._turns.append([self._compact(m) for m in messages])
        self._recount()
        trimmed = self._trim()
        self._view = tuple(m for turn in self._turns for m in turn)
        return trimmed

    def append_note(self, text: str, turn: str, ts: float) -> None:
        """Add an announcement (a timer, a reminder) as his own message, in order.

        Args:
            text: what he said.
            turn: its turn id.
            ts: when, in epoch seconds.
        """
        self._turns.append([{"role": "assistant", "content": text, "turn": turn, "ts": ts}])
        self._recount()
        self._trim()
        self._view = tuple(m for turn in self._turns for m in turn)

    def clear(self) -> None:
        """Forget every turn (a new conversation)."""
        self._turns = []
        self._chars = 0
        self._view = ()

    def _compact(self, message: Message) -> Message:
        """A tool result cut to ``brain.history_tool_result_chars`` (the reply already used the whole of it), and
        pictures replaced by a note naming them (each would cost every later turn its tokens again)."""
        if pictures := message.get("images"):
            message = {key: value for key, value in message.items() if key != "images"}  # type: ignore[assignment]
            message["content"] = "\n".join(part for part in (message["content"], images.note(pictures)) if part)
        limit = self.brain.history_tool_result_chars
        if message["role"] != "tool" or len(message["content"]) <= limit:
            return message
        return {**message, "content": clip(message["content"], limit, flat=False)}

    def _recount(self) -> None:
        """Count the characters held."""
        self._chars = sum(_size(m) for turn in self._turns for m in turn)

    def _trim(self) -> bool:
        """Past the budget, drop the oldest whole turns until ``HISTORY_KEEP_RATIO`` of it is left; True when it did."""
        budget = self.brain.history_chars
        if self._chars <= budget or len(self._turns) <= 1:
            return False
        target = budget * HISTORY_KEEP_RATIO
        while len(self._turns) > 1 and self._chars > target:
            self._chars -= sum(_size(m) for m in self._turns.pop(0))
        while self._turns and self._turns[0][0]["role"] != "user" and len(self._turns) > 1:
            self._chars -= sum(_size(m) for m in self._turns.pop(0))
        return True


def _size(message: Message) -> int:
    """A message's weight in the budget: its text and its tool calls."""
    calls = message.get("tool_calls")
    return len(message["content"]) + (len(json.dumps(calls, ensure_ascii=False, default=str)) if calls else 0)


# ---------------------------------------------------------------------------------------------------- routing
@dataclass(frozen=True, slots=True)
class RouterCalibration:
    """How the router scores labelled cases, and thresholds that would separate right from wrong.

    Attributes:
        right: the score of the expected item, per case.
        suggested_find_min: for ``tools.find_min_score``.
        suggested_auto_score: for ``skills.auto_score``.
        suggested_auto_margin: for ``skills.auto_margin``.
    """

    right: tuple[float, ...]
    suggested_find_min: float
    suggested_auto_score: float
    suggested_auto_margin: float


@dataclass(frozen=True, slots=True)
class _Indexed:
    """Items with their texts and vectors, for one version."""

    version: int
    names: tuple[str, ...]
    texts: tuple[str, ...]
    matrix: "NDArray[np.float32] | None"


def tool_text(tool: Tool) -> str:
    """What a tool is ranked by: its name, its first sentence and up to three example requests."""
    examples = "; ".join(tool.examples[:3])
    return f"{tool.name}: {tool.summary or tool.description}" + (f". e.g. {examples}" if examples else "")


def skill_text(skill: Skill) -> str:
    """What a skill is ranked by: its name, description and trigger phrases."""
    return f"skill {skill.name}: {skill.description}. {'; '.join(skill.triggers)}".rstrip(". ")


class Router:
    """Ranks tools and skills for a request (see the module docstring); vectors are computed once per version, once
    the embedder has loaded."""

    def __init__(self, embedder: "Embedder", cfg: "Config") -> None:
        """Start with nothing indexed.

        Args:
            embedder: the process's embedder.
            cfg: the live settings (``tools.*``, ``skills.*``).
        """
        self.embedder = embedder
        self.cfg = cfg
        self._tools = _Indexed(-1, (), (), None)
        self._skills = _Indexed(-1, (), (), None)
        self._skill_items: dict[str, Skill] = {}

    def index_tools(self, toolset: ToolSet) -> None:
        """Embed a new toolbox's tools (once per toolbox version, and again once the embedder has loaded).

        Args:
            toolset: the toolbox.
        """
        if toolset.version == self._tools.version and (self._tools.matrix is not None or not self.embedder.loaded):
            return
        names = toolset.names()
        texts = tuple(tool_text(tool) for name in names if (tool := toolset.get(name)) is not None)
        self._tools = _Indexed(toolset.version, names, texts, self._encode(texts))

    def index_skills(self, skills: Mapping[str, Skill], version: int) -> None:
        """Embed the skills (once per skill-store version).

        Args:
            skills: the skills by name.
            version: the store's version.
        """
        if version == self._skills.version and (self._skills.matrix is not None or not self.embedder.loaded):
            return
        names = tuple(skills)
        texts = tuple(skill_text(skills[name]) for name in names)
        self._skills = _Indexed(version, names, texts, self._encode(texts))
        self._skill_items = dict(skills)

    def select(self, q: Query, toolset: ToolSet, recent: AbstractSet[str]) -> list[str]:
        """A toolbox too big to show whole: the core tools first, then recently used ones, then the best matches.

        Args:
            q: the request.
            toolset: the toolbox (indexed with ``index_tools``).
            recent: tools used in the last turn.

        Returns:
            Tool names, at most ``tools.max_per_turn`` but never fewer than ``tools.min_relevant`` matches.
        """
        tools = self.cfg.tools
        known = set(toolset.names())
        chosen = [name for name in tools.always if name in known]
        chosen += [name for name in sorted(recent) if name in known and name not in chosen][:tools.recent_kept]
        slots = max(tools.min_relevant, tools.max_per_turn - len(chosen))
        taken = set(chosen)
        ranked = self._ranked(q, self._tools)
        chosen += [name for name, _ in ranked if name not in taken and name in known][:slots]
        return chosen

    def best_skill(self, q: Query) -> Skill | None:
        """A skill that matches the request unmistakably (the model can still pick one with use_skill).

        Args:
            q: the request.

        Returns:
            The skill when its score reaches ``skills.auto_score`` and leads the next by ``skills.auto_margin``.
        """
        ranked = self._ranked(q, self._skills)
        auto = self.cfg.skills
        if not ranked or ranked[0][1] < auto.auto_score:
            return None
        if len(ranked) > 1 and ranked[0][1] - ranked[1][1] < auto.auto_margin:
            return None
        return self._skill_items.get(ranked[0][0])

    def search(self, q: Query, toolset: ToolSet, exclude: AbstractSet[str]) -> list[str]:
        """``find_tools``: tools that may do what is asked, beyond those shown.

        Args:
            q: what the model needs to do.
            toolset: the toolbox.
            exclude: the tools already shown.

        Returns:
            Up to ``tools.find_results`` names scoring above ``tools.find_min_score`` (any score when there are no
            more candidates than that).
        """
        tools = self.cfg.tools
        known = set(toolset.names())
        candidates = [(name, score) for name, score in self._ranked(q, self._tools)
                      if name in known and name not in exclude]
        few = len(candidates) <= tools.find_results
        return [name for name, score in candidates[:tools.find_results] if few or score > tools.find_min_score]

    def calibrate(self, cases: Sequence[tuple[str, str]]) -> RouterCalibration:
        """Score labelled requests (phrase, expected tool or skill) and suggest thresholds.

        Args:
            cases: the labelled requests.

        Returns:
            The scores and the suggested thresholds.
        """
        right: list[float] = []
        wrong: list[float] = []
        for phrase, expected in cases:
            q = Query(phrase, self.embedder.encode_query(phrase))
            scored = dict(self._ranked(q, self._tools)) | dict(self._ranked(q, self._skills))
            if expected in scored:
                right.append(scored[expected])
                wrong.append(max((s for n, s in scored.items() if n != expected), default=0.0))
        if not right:
            return RouterCalibration((), 0.0, 0.0, 0.0)
        r, w = np.array(right), np.array(wrong)
        find_min = float(np.percentile(w, 90)) if len(w) else 0.0
        return RouterCalibration(tuple(right), round(find_min, 2),
                                 round(float(np.percentile(r, 25)), 2),
                                 round(max(0.0, float(np.median(r - w)) / 2), 2))

    def _ranked(self, q: Query, items: _Indexed) -> list[tuple[str, float]]:
        """Items ranked for a request."""
        return rank(q.vec, q.text, items.names, items.matrix, items.texts)

    def _encode(self, texts: Sequence[str]) -> "NDArray[np.float32] | None":
        """Unit vectors of texts; None with nothing to encode, or before the embedder has loaded (it is never loaded
        here, on the event loop: the words rank meanwhile)."""
        return self.embedder.encode(list(texts)) if texts and self.embedder.loaded else None


# ---------------------------------------------------------------------------------------------------- the context
class Context:
    """Builds the system text, the snapshots and the tails, and holds the router."""

    def __init__(self, cfg: "Config", memory: "MemorySystem", skills: "Skills", embedder: "Embedder") -> None:
        """Hold the parts.

        Args:
            cfg: the live settings.
            memory: Orion's memory.
            skills: the skill store.
            embedder: the process's embedder (the router's vectors).
        """
        self.cfg = cfg
        self.memory = memory
        self.skills = skills
        self.router = Router(embedder, cfg)
        self._system: tuple[tuple[str, ...], str] = ((), "")

    def system(self) -> str:
        """The system text: the persona, then both reply styles (cached while the inputs stay the same)."""
        prompts, user = self.cfg.prompts, self.cfg.user
        key = (user.name, user.honorific, prompts.persona, prompts.voice_style, prompts.text_style,
               prompts.unnamed_user, prompts.unnamed_possessive)
        if self._system[0] != key:
            name = user.name.strip()
            persona = prompts.persona.format(who=name or prompts.unnamed_user,
                                             who_pos=f"{name}'s" if name else prompts.unnamed_possessive,
                                             honorific=user.honorific)
            self._system = (key, "\n\n".join(part for part in (persona, prompts.voice_style, prompts.text_style)
                                             if part.strip()))
        return self._system[1]

    def snapshot(self, conv: int, now: datetime) -> Snapshot:
        """A new context epoch's snapshot.

        Args:
            conv: the conversation.
            now: the current local time (today's journal).

        Returns:
            The snapshot.
        """
        vault, store = self.memory.vault, self.memory.store
        ids = self._ids()
        user = [(ids[u.key], u.plain) for u in vault.entries("user")]
        orion = [(ids[u.key], u.plain) for u in vault.entries("orion")]
        listed = self._listed_skills(store.skill_last_used())
        journal = vault.journal_today(self.cfg.brain.snapshot_journal_chars, now)
        memory = render_snapshot(city=self.memory.home_city(), user=user, orion=orion,
                                 skills=[(s.name, s.description) for s in listed], journal=journal,
                                 labels=labels_of(self.cfg.prompts))
        return Snapshot(conv, time.time(), self.system(), memory, frozenset(s.name for s in listed))

    def tail(self, *, now: datetime, channel: str, snapshot: Snapshot, recalled: Sequence["Hit"],
             skill: Skill | None) -> str:
        """The tail for this turn's user message.

        Args:
            now: the current local time.
            channel: "voice" (spoken) or "text" (shown).
            snapshot: the epoch's snapshot (changes and skills newer than it are listed).
            recalled: the memories recalled for this request.
            skill: a skill to follow now.

        Returns:
            The tail.
        """
        prompts = self.cfg.prompts
        when = prompts.context_time.format(time=now.strftime(prompts.time_format), tz=now.tzname() or "")
        changes = change_lines(self.memory.changes_since(snapshot.built), self._ids())
        new = [(s.name, s.description) for name, s in self.skills.all().items() if name not in snapshot.skills]
        return render_tail(when=when, channel="spoken" if channel == "voice" else "shown on screen",
                           recalled=[(hit.tag(), " ".join(hit.text.split())) for hit in recalled], changes=changes,
                           skill=(skill.name, skill.body) if skill else None,
                           new_skills=new[:self.cfg.skills.context_max], labels=labels_of(prompts),
                           max_recall_chars=self.cfg.retrieval.recall_chars)

    def _ids(self) -> dict[str, str]:
        """Every entry's id, the same way the writer names them (unique across all entry files)."""
        keys = [unit.key for kind in FILES for unit in self.memory.vault.entries(kind)]
        return dict(zip(keys, short_ids(keys), strict=True))

    def _listed_skills(self, last_used: Mapping[str, float]) -> list[Skill]:
        """The skills the snapshot lists: the most recently used first, then by name, at most ``skills.context_max``."""
        skills = self.skills.all()
        order = sorted(skills.values(), key=lambda s: (-last_used.get(s.name, 0.0), s.name))
        return order[:self.cfg.skills.context_max]

