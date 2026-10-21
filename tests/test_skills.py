"""skills: SKILL.md files parsed, matched by name or trigger, written, rescanned and deleted (files in tmp_path)."""

import json
from pathlib import Path

import pytest

from orion.base import TRUNCATED
from orion.skills import ARGUMENTS_NOTE, SkillRoot, Skills, editable, normalize_name, parse_skill, skill_key


class Clock:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        """Start at zero."""
        self.now = 0.0

    def __call__(self) -> float:
        """The current time."""
        return self.now


def test_a_skill_file_parses_or_is_skipped(tmp_path) -> None:
    """Tools and triggers are read as words; no front matter, bad YAML, odd tools or Latin-1 give None."""
    path = tmp_path / "greet" / "SKILL.md"
    skill = parse_skill(path, "---\nname: greet\ndescription: Say  hello.\nallowed-tools: volume open\n"
                              "metadata:\n  triggers: [Good Morning]\n---\nSay hi.\n")
    assert skill is not None and (skill.name, skill.description, skill.body) == ("greet", "Say hello.", "Say hi.")
    assert (skill.tools, skill.triggers) == (("volume", "open"), ("good morning",))
    bad = ("no front matter", "---\nname: [unclosed\n---\nx", "---\nname: x\nallowed-tools: {a: 1}\n---\nx")
    assert all(parse_skill(path, text) is None for text in bad)
    latin = tmp_path / "cafe" / "SKILL.md"
    latin.parent.mkdir()
    latin.write_bytes("---\nname: café\n---\nx".encode("latin-1"))
    assert parse_skill(latin) is None
    assert (normalize_name("Movie Night!"), normalize_name("../../x")) == ("movie-night", "x")


def test_skills_are_matched_written_rescanned_and_deleted(tmp_path) -> None:
    """Names and triggers match whole words; a new folder shows after the rescan interval; bad edits change nothing."""
    clock = Clock()
    skills = Skills(tmp_path / "skills", clock=clock)
    movie = skills.write("Movie Night!", "Set up the living room for a film.", "1. volume set 40", ["volume"])
    assert movie.name == "movie-night" and skills.get("movie") is movie
    assert skills.match("Movie night!") is movie and skills.match("I watched a movie last night") is None
    skills.write("focus", "Quiet time.", "1. mute", ["volume"], ["focus mode", "time to focus"])
    assert skills.match("ok, time to focus please").tools == ("volume",)
    version = skills.version
    for folder, data in (("greet", b"---\nname: greet\ndescription: Hi.\n---\nSay hi.\n"),
                         ("cafe", "---\nname: café\n---\nx".encode("latin-1"))):
        (skills.root / folder).mkdir()
        (skills.root / folder / "SKILL.md").write_bytes(data)
    assert skills.get("greet") is None  # not looked for yet
    clock.now += 1.0
    assert skills.get("greet").description == "Hi." and skills.version == version + 1  # the unreadable one is skipped
    with pytest.raises(ValueError):
        skills.save("oops", "no front matter")
    assert not (skills.root / "oops").exists()
    assert skills.delete("movie-night") and skills.get("movie-night") is None and not skills.delete("nope")


# ---------------------------------------------------------------------------------------------------- Agent Skills
def skill_text(name: str, description: str = "Does it.", extra: str = "", body: str = "Steps.") -> str:
    return f"---\nname: {name}\ndescription: {description}\n{extra}---\n{body}\n"


def make(root: Path, name: str, **kw: str) -> Path:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(skill_text(name, **kw))
    return path


def test_names_follow_the_specification() -> None:
    """Lower-case letters, digits and single hyphens, at most 64, not starting or ending with one; a plugin's skill is
    plugin:skill with both parts the same."""
    assert [normalize_name(n) for n in ("Movie  Night!!", "a--b", "-lead-", "../../x", "x" * 80, "  ")] == [
        "movie-night", "a-b", "lead", "x", "x" * 64, ""]
    assert [skill_key(n) for n in ("Linear:Triage Bugs", "linear", "a:", ":b", "a:b:c")] == [
        "linear:triage-bugs", "linear", "", "", "a:b-c"]


def test_triggers_are_a_string_in_the_file_and_either_form_is_read(tmp_path) -> None:
    """metadata values are strings in the specification: Orion writes one comma-separated string, and still reads a
    list; his own are marked as his."""
    skills = Skills(tmp_path / "skills")
    written = skills.write("focus", "Quiet time.", "1. mute", ["volume"], ["focus mode", "time, to focus"])
    text = written.path.read_text()
    assert "triggers: focus mode, time to focus" in text and "author: orion" in text and "- focus" not in text
    assert written.triggers == ("focus mode", "time to focus")
    assert written.source == "learned" and editable(written)
    listed = parse_skill(tmp_path / "x" / "SKILL.md",
                         skill_text("x", extra="metadata:\n  triggers: [Alpha, beta gamma]\n"))
    assert listed is not None and listed.triggers == ("alpha", "beta gamma") and listed.source == "yours"
    assert parse_skill(tmp_path / "y" / "SKILL.md", skill_text("y", extra="metadata:\n  triggers: {a: 1}\n")) is None


def test_a_saved_skill_is_checked_against_the_specification(tmp_path) -> None:
    skills = Skills(tmp_path / "skills")
    ok = skills.save("tidy", skill_text("tidy"))
    assert ok.name == "tidy" and ok.source == "yours"
    for name, text, why in [("tidy", skill_text("other"), "its folder's"),
                            ("Tidy Up", skill_text("tidy-up", description=""), "description"),
                            ("big", skill_text("big", description="d" * 1025), "1024"),
                            ("a:b", skill_text("b"), "letters, digits"), ("", skill_text("z"), "letters, digits")]:
        with pytest.raises(ValueError, match=why):
            skills.save(name, text)
    with pytest.raises(ValueError, match="1024"):
        skills.write("long", "d" * 1025, "x")
    assert not (skills.root / "big").exists() and not (skills.root / "long").exists()


def test_where_each_skill_comes_from_and_what_may_be_changed(tmp_path) -> None:
    """The starters ship with Orion (switched off, never edited), his own and yours are editable, a plugin's comes with
    it; whoever else's text it is, following it makes the turn untrusted."""
    root = tmp_path / "skills"
    make(root, "morning-briefing")
    skills = Skills(root)
    skills.write("learned-one", "He learned it.", "1. x")
    mine = skills.save("mine", skill_text("mine"))
    plugin = tmp_path / "plugin" / "skills"
    make(plugin, "triage")
    skills = Skills(root, roots=lambda: [SkillRoot(plugin, "linear")])
    kinds = {name: (s.source, editable(s), s.trusted) for name, s in skills.all().items()}
    assert kinds == {"learned-one": ("learned", True, True), "mine": ("yours", True, True),
                     "morning-briefing": ("builtin", False, True), "linear:triage": ("plugin", False, False)}
    assert mine.plugin == "" and skills.all()["linear:triage"].plugin == "linear"
    for name, why in (("morning-briefing", "ships with Orion"), ("linear:triage", "comes with the linear plugin")):
        with pytest.raises(ValueError, match=why):
            skills.delete(name)
    with pytest.raises(ValueError, match="ships with Orion"):
        skills.write("morning-briefing", "Mine now.", "x")
    with pytest.raises(ValueError, match="ships with Orion"):
        skills.save("morning-briefing", skill_text("morning-briefing"))
    assert skills.delete("mine") and skills.delete("learned-one") and not skills.delete("nope")


def test_a_skill_switched_off_is_listed_but_never_used(tmp_path) -> None:
    root = tmp_path / "skills"
    Skills(root).write("movie-night", "Film.", "1. x", triggers=["movie night"])
    off: list[str] = []
    skills = Skills(root, disabled=lambda: off)
    first = skills.version
    assert skills.match("movie night") is not None and skills.enabled("movie-night")
    off.append("Movie-Night")  # however it was written
    skills.invalidate()
    assert skills.version == first + 1
    assert skills.match("movie night") is None and skills.get("movie") is None and not skills.enabled("movie-night")
    assert "movie-night" not in skills.all() and skills.everything()["movie-night"].name == "movie-night"
    listed = skills.entry("Movie Night")
    assert listed is not None and skills.view(listed)["enabled"] is False
    off.clear()
    skills.invalidate()
    assert skills.match("movie night") is not None and skills.version == first + 2


def test_a_plugins_commands_are_skills_too(tmp_path) -> None:
    commands = tmp_path / "plugin" / "commands"
    commands.mkdir(parents=True)
    (commands / "ship.md").write_text("---\ndescription: Ship it.\nargument-hint: <env>\n---\nShip $1 to $ARGUMENTS.")
    (commands / "plain.md").write_text("# Plain command\n\nJust do the thing.")
    (commands / "empty.md").write_text("---\ndescription: Nothing.\n---\n")
    (commands / "bad.md").write_text("---\n: [\n---\nx")
    (commands / ".hidden.md").write_text("x")
    skills = Skills(tmp_path / "skills", roots=lambda: [SkillRoot(commands, "tools", "commands")])
    assert sorted(skills.all()) == ["tools:plain", "tools:ship"]
    ship, plain = skills.all()["tools:ship"], skills.all()["tools:plain"]
    assert ship.description == "Ship it." and ship.source == "plugin" and ship.tools == ()
    assert ship.body.startswith("Ship $1 to $ARGUMENTS.") and ARGUMENTS_NOTE in ship.body
    assert plain.description == "Plain command" and ARGUMENTS_NOTE not in plain.body
    assert skills.get("tools:ship") is ship and skills.files("tools:ship") == ["ship.md"]
    assert skills.read("tools:ship", "", max_chars=1000, enabled_only=True).startswith("---\ndescription: Ship it.")
    with pytest.raises(LookupError, match="single file"):
        skills.read("tools:ship", "other.md", max_chars=100, enabled_only=True)


def test_a_skills_files_are_listed_and_read_inside_its_folder_only(tmp_path) -> None:
    root = tmp_path / "skills"
    folder = make(root, "docs").parent
    (folder / "references").mkdir()
    (folder / "references/api.md").write_text("# API\nline two")
    (folder / "scripts").mkdir()
    (folder / "scripts/run.sh").write_text("echo hi")
    (folder / ".hidden").write_text("x")
    (folder / "binary.bin").write_bytes(b"\xff\xfe\x00\x01")
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET")
    (folder / "link.txt").symlink_to(secret)
    (folder / "linked").symlink_to(tmp_path)
    skills = Skills(root)
    assert skills.files("docs") == ["SKILL.md", "binary.bin", "references/api.md", "scripts/run.sh"]
    assert skills.read("docs", "references/api.md", max_chars=1000, enabled_only=True) == "# API\nline two"
    assert skills.read("docs", "", max_chars=1000, enabled_only=True).startswith("---\nname: docs")
    assert skills.read("docs", "SKILL.md", max_chars=1000, enabled_only=True).startswith("---\nname: docs")
    assert skills.read("docs", "references/api.md", max_chars=5, enabled_only=True) == "# API\n"[:5] + TRUNCATED
    for path in ("../secret.txt", "/etc/passwd", "link.txt", "linked/secret.txt", "references/../../secret.txt",
                 "nope.md"):
        with pytest.raises(LookupError, match="no file"):
            skills.read("docs", path, max_chars=100, enabled_only=True)
    with pytest.raises(ValueError, match="isn't text"):
        skills.read("docs", "binary.bin", max_chars=100, enabled_only=True)
    with pytest.raises(LookupError, match="no skill called 'ghost'"):
        skills.files("ghost")
    skills = Skills(root, disabled=lambda: ["docs"])
    with pytest.raises(LookupError, match="switched on"):
        skills.read("docs", "SKILL.md", max_chars=100, enabled_only=True)  # what the model may read
    assert skills.read("docs", "SKILL.md", max_chars=100, enabled_only=False)  # what the HUD may


def test_a_skill_as_the_hud_lists_it(tmp_path) -> None:
    root = tmp_path / "skills"
    skills = Skills(root)
    skills.write("focus", "Quiet.", "1. x", ["volume", "open"], ["focus mode"])
    assert skills.view(skills.all()["focus"]) == {
        "name": "focus", "description": "Quiet.", "source": "learned", "enabled": True, "triggers": ["focus mode"],
        "tools": ["volume", "open"], "files": ["SKILL.md"], "editable": True}
    plugin = tmp_path / "p" / "skills"
    make(plugin, "triage")
    other = Skills(root, roots=lambda: [SkillRoot(plugin, "linear")])
    assert other.view(other.all()["linear:triage"])["plugin"] == "linear"


def test_imported_skills_keep_their_files_and_their_origin_across_restarts(tmp_path) -> None:
    source = tmp_path / "download" / "pdf"
    make(source.parent, "pdf")
    (source / "references").mkdir()
    (source / "references/forms.md").write_text("forms")
    (source / "LICENSE.txt").write_text("Apache")
    (source / "evil").symlink_to("/etc/passwd")
    skills = Skills(tmp_path / "skills")
    added = skills.adopt(source, origin="https://example.org/skills/pdf")
    assert (added.name, added.source, added.origin, added.trusted) == (
        "pdf", "yours", "https://example.org/skills/pdf", False)
    assert skills.files("pdf") == ["LICENSE.txt", "SKILL.md", "references/forms.md"]  # the link was not copied
    assert Skills(tmp_path / "skills").all()["pdf"].origin == "https://example.org/skills/pdf"  # remembered
    assert not [f for f in (tmp_path / "skills").iterdir() if f.name.startswith(".import")]
    assert skills.adopt(source, origin="https://example.org/v2").origin == "https://example.org/v2"  # replaced
    skills.write("mine", "Mine.", "x")
    clash = tmp_path / "download" / "mine"
    make(clash.parent, "mine")
    with pytest.raises(ValueError, match="already have a skill called mine"):
        skills.adopt(clash, origin="somewhere")
    assert skills.all()["mine"].origin == "" and skills.all()["mine"].source == "learned"
    nothing = tmp_path / "download" / "nothing"
    nothing.mkdir()
    with pytest.raises(ValueError, match="isn't a skill"):
        skills.adopt(nothing, origin="x")
    nodesc = tmp_path / "download" / "nodesc"
    nodesc.mkdir()
    (nodesc / "SKILL.md").write_text("---\nname: nodesc\n---\nx")
    with pytest.raises(ValueError, match="description of 1 to 1024"):
        skills.adopt(nodesc, origin="x")
    assert skills.delete("pdf") and "pdf" not in json.loads((tmp_path / "skills/.sources.json").read_text())["skills"]


def test_skills_opened_from_the_settings_follow_its_plugins_and_switches(tmp_path) -> None:
    from orion.config import default_config

    cfg = default_config()
    folder = tmp_path / "data" / "plugins" / "linear"
    make(folder / "skills", "triage")
    (folder / ".claude-plugin").mkdir()
    (folder / ".claude-plugin/plugin.json").write_text('{"name": "linear"}')
    record = {"source": "upload", "version": "", "sha": "a" * 64, "enabled": True, "installed_at": 1.0}
    cfg.plugins.installed = {"linear": record, "ghost": {**record}}
    skills = Skills.open(cfg)
    assert list(skills.all()) == ["linear:triage"]
    cfg.plugins.installed = {"linear": {**record, "enabled": False}}
    skills.invalidate()
    assert skills.all() == {}
    cfg.plugins.installed = {"linear": record}
    cfg.skills.disabled = ["linear:triage"]
    skills.invalidate()
    assert skills.all() == {} and "linear:triage" in skills.everything()
