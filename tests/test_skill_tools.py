"""The skills tools (use_skill, skill_file, skill_manage) against skills in a temporary data folder: what the model is
shown, which text is someone else's (taint), what stays inside a skill's folder."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from orion.config import default_config
from orion.skills import Skills
from orion.tools import CallInfo, _call, _events, ctx
from orion.tools.memory import SKILL_REFUSAL, skill_file, skill_manage, use_skill


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Iterator[Skills]:
    """The tools' context over fresh settings: its skills are data/skills of the temporary folder."""
    cfg = default_config()
    monkeypatch.setattr(ctx, "refresh", lambda: None, raising=False)
    monkeypatch.setattr(type(ctx), "config", property(lambda self: cfg))
    skills = Skills.open(cfg)
    monkeypatch.setattr(ctx, "_skills", skills, raising=False)
    yield skills


@pytest.fixture
def events() -> Iterator[list[dict]]:
    """The HUD events the tools publish during a call by the brain."""
    published: list[dict] = []
    events_token, call_token = _events.set(published), _call.set(CallInfo("brain", None))
    yield published
    _events.reset(events_token)
    _call.reset(call_token)


def add(root: Path, name: str, body: str = "Steps.", files: dict[str, bytes] | None = None) -> None:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Does {name}.\n---\n{body}\n")
    for path, data in (files or {}).items():
        (folder / path).parent.mkdir(parents=True, exist_ok=True)
        (folder / path).write_bytes(data)


def test_use_skill_gives_the_steps_and_lists_the_files_that_come_with_it(store: Skills, events: list[dict]) -> None:
    """Orion's own skills are trusted: no taint; the extra files are named so the model can read them."""
    add(store.root, "pdf", "Read the form.", {"references/forms.md": b"# Forms", "scripts/fill.py": b"print(1)"})
    said = use_skill("pdf")
    assert said.startswith("# Skill: pdf\nRead the form.")
    assert "It comes with: references/forms.md, scripts/fill.py." in said and said.endswith("Follow these steps now.")
    assert events == [{"type": "skill_used", "name": "pdf"}]
    assert use_skill("nope").startswith("No skill called 'nope'. Available: ")


def test_an_untrusted_skill_taints_the_turn(store: Skills, events: list[dict], tmp_path: Path) -> None:
    """A skill imported from the internet is someone else's text: running it or reading its files taints the turn
    (so what it says can't start a change of the user's connectors or memory without the user)."""
    add(tmp_path / "downloaded", "imported", "Do as it says.", {"notes.md": b"hello"})
    store.adopt(tmp_path / "downloaded" / "imported", origin="https://example.test/skills.zip")
    assert "Do as it says." in use_skill("imported")
    assert events == [{"type": "skill_used", "name": "imported"},
                      {"type": "taint", "source": "The imported skill"}]
    events.clear()
    assert skill_file("imported", "notes.md") == "hello"
    assert events == [{"type": "taint", "source": "A file of the imported skill"}]


def test_skill_file_reads_text_inside_the_folder_only(store: Skills, events: list[dict]) -> None:
    """A path outside the folder, a missing file and a binary one are errors the model can read, not crashes."""
    add(store.root, "pdf", files={"references/forms.md": b"# Forms\nfields", "assets/logo.png": b"\x89PNG\xff\xfe"})
    (store.root / "secret.txt").write_text("outside")
    assert skill_file("pdf") == "SKILL.md\nassets/logo.png\nreferences/forms.md"
    assert skill_file("pdf", "references/forms.md") == "# Forms\nfields"
    for bad in ("../secret.txt", "references/missing.md", "/etc/passwd", "assets/logo.png"):
        with pytest.raises(ToolError):
            skill_file("pdf", bad)
    with pytest.raises(ToolError, match="No skill called 'nope'"):
        skill_file("nope")
    assert events == []  # Orion's own skill: nothing to mark


def test_skill_file_does_not_read_a_skill_that_is_switched_off(store: Skills) -> None:
    """The model sees only the skills the user left on."""
    add(store.root, "pdf", files={"a.md": b"a"})
    ctx.config.skills.disabled = ["pdf"]
    with pytest.raises(ToolError):
        skill_file("pdf", "a.md")


def test_skill_manage_says_why_it_refused(store: Skills, events: list[dict]) -> None:
    """A bad name or a starter's name is told in words (not an exception); from tainted turns nothing changes."""
    said = skill_manage("create", "movie night", "Film setup.", "1. volume set 40", "volume", "movie night")
    assert said.startswith("Skill 'movie-night' saved but NOT run.") and store.get("movie-night") is not None
    assert {"type": "skill_changed", "name": "movie-night", "action": "create"} in events
    assert skill_manage("create", "movie-night", "", "x") == "A skill needs both a description and instructions."
    assert skill_manage("delete", "nope").startswith("No skill called 'nope'.")
    assert skill_manage("delete", "movie-night") == "Deleted skill movie-night."
    token = _call.set(CallInfo("tainted", None))
    try:
        assert skill_manage("create", "x", "y", "z") == SKILL_REFUSAL
    finally:
        _call.reset(token)
