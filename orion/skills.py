"""Skills: reusable know-how in the open Agent Skills format (https://agentskills.io/specification): a folder named
after the skill, holding a SKILL.md.

    ---
    name: morning-briefing
    description: One line saying what it does and when to use it.
    allowed-tools: get_weather list_reminders todo get_news     # optional: the tools it uses
    metadata:
      triggers: good morning, morning briefing                  # optional: phrases that start it directly
    ---
    Step-by-step instructions for Orion...

The same format Claude and other agents use, so skills can be shared: a name of lower-case letters, digits and single
hyphens that is also its folder's name, a description of at most 1024 characters, and ``metadata`` values that are
text (Orion writes his triggers as one comma-separated string, and reads a list as well). A skill may bring more files
(``references/``, ``assets/``, ``scripts/``): he reads their text with ``skill_file`` and runs its scripts with
``run_command``, in its sandbox.

Where skills come from (``Skill.source``):
- builtin: the starter skills that ship with Orion, in data/skills (git tracks them, so the HUD switches them off
  rather than editing or deleting them);
- learned: the ones he wrote from what you taught him (``skill_manage``; ``metadata.author: orion``);
- yours: the ones you wrote, by hand or in the HUD, or imported; data/skills/.sources.json remembers where each
  import came from;
- plugin: a plugin's skills and commands (``orion.plugins`` lists their folders), named ``plugin:skill`` so they
  never clash with any other; read-only.
An imported skill's or a plugin's instructions are someone else's text: following one makes the turn untrusted, so
his risky calls after it need your yes (``Skill.trusted``).

A skill switched off (``skills.disabled``) is still listed for the HUD (``everything``) and never used (``all``).

``Skills`` keeps them parsed. The folders are looked at again at most once a second (or right after ``invalidate``),
and only files that changed are parsed again. ``version`` changes whenever a skill does, or which ones are switched
on, so callers can cache what they derive from the skills (routing vectors, the snapshot's list).
"""

import codecs
import contextlib
import dataclasses
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, Literal

import yaml

from orion.base import FRONT_MATTER, JSON, TRUNCATED, Skill, file_stamp, write_atomic

if TYPE_CHECKING:
    from orion.config import Config

log = logging.getLogger("orion.skills")

type RootKind = Literal["skills", "commands"]
type Stamp = tuple[int, int, int]

SKILL_FILE: Final = "SKILL.md"
SOURCES_FILE: Final = ".sources.json"  # in data/skills: where each imported skill came from
RESCAN_S: Final = 1.0  # the folders are looked at again at most this often (invalidate() forces it)
NAME_MAX: Final = 64  # the longest skill name (the Agent Skills specification's limit)
DESCRIPTION_MAX: Final = 1024  # the longest description (the same)
FILES_MAX: Final = 500  # files listed for one skill at most
AUTHOR: Final = "orion"  # metadata.author of a skill he wrote from what he was taught
STARTERS: Final = frozenset({"deep-research", "morning-briefing", "pc-checkup", "play-music"})  # ship with Orion
ARGUMENTS_NOTE: Final = "($ARGUMENTS, $1, $2... above stand for what the user asked for.)"  # a command's arguments

_NON_WORD: Final = re.compile(r"[^a-z0-9]+")
_ARGUMENT: Final = re.compile(r"\$(?:ARGUMENTS\b|\d)")
_LOADER: Final = getattr(yaml, "CSafeLoader", yaml.SafeLoader)  # libyaml's parser when it is installed
_DUMPER: Final = getattr(yaml, "CSafeDumper", yaml.SafeDumper)


def normalize_name(name: str) -> str:
    """A skill's name as a folder name: "Movie Night!" -> "movie-night", "../../x" -> "x", "a--b" -> "a-b".

    Args:
        name: any name.

    Returns:
        Lower-case letters, digits and single hyphens, at most 64 characters, starting and ending with a letter or
        digit ("" when nothing is left).
    """
    return _NON_WORD.sub("-", name.lower()).strip("-")[:NAME_MAX].strip("-")


def skill_key(name: str) -> str:
    """A skill's name as Orion keys it: ``normalize_name``, or "plugin:skill" for a plugin's (both parts normalised).

    Args:
        name: the name asked for ("Movie Night", "linear:Triage").

    Returns:
        The key ("" when a part has nothing left).
    """
    plugin, colon, rest = name.partition(":")
    if not colon:
        return normalize_name(name)
    left, right = normalize_name(plugin), normalize_name(rest)
    return f"{left}:{right}" if left and right else ""


def parse_skill(path: Path, text: str | None = None, *, plugin: str = "", quiet: bool = False) -> Skill | None:
    """The skill in a SKILL.md, or None when the file isn't one.

    Args:
        path: the file (read unless ``text`` is given; its folder names a skill without a ``name``).
        text: the file's contents, when already at hand (the HUD's editor).
        plugin: the plugin it comes with ("" for none): its name goes in front, and its ``allowed-tools`` (Claude
            Code's tools, not Orion's) are left out.

    Returns:
        The skill, or None for a file that can't be read as UTF-8, has no front matter, has YAML that doesn't parse,
        or lists its tools or triggers as something other than words.
    """
    try:
        if text is None:
            text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:  # one bad file mustn't hide every other skill
        _skipped(quiet)("skipping %s: %s", path, e)
        return None
    match = FRONT_MATTER.match(text)
    if match is None:
        _skipped(quiet)("skipping %s: no YAML front matter", path)
        return None
    meta = _front(match.group(1), path, quiet)
    extra = meta.get("metadata", {}) if isinstance(meta, dict) else None
    if not isinstance(meta, dict) or not isinstance(extra, dict):
        return None
    name = normalize_name(str(meta.get("name") or path.parent.name))
    tools = () if plugin else _words(meta.get("allowed-tools", meta.get("tools", [])), split=r"[,\s]+")
    triggers = _words(extra.get("triggers", meta.get("triggers", [])), split=r",")
    if not name or tools is None or triggers is None:
        return None
    source = "plugin" if plugin else "learned" if str(extra.get("author") or "").strip().lower() == AUTHOR else "yours"
    return Skill(name=f"{normalize_name(plugin)}:{name}" if plugin else name,
                 description=" ".join(str(meta.get("description") or "").split()), body=text[match.end():].strip(),
                 tools=tools, triggers=tuple(t.lower() for t in triggers), path=path, source=source, plugin=plugin)


def parse_command(path: Path, *, plugin: str, quiet: bool = False) -> Skill | None:
    """A plugin's command (a Markdown file, front matter optional) as a skill, as Claude's chat uses commands.

    Args:
        path: the command's .md file.
        plugin: the plugin it comes with.
        quiet: a file that can't be used is a debug line, not a warning (a plugin that is only being looked at).

    Returns:
        The skill "plugin:command": its description from the front matter (else its first line), its body the
        instructions, with a note on what ``$ARGUMENTS`` stands for when they use it; None for a file that can't be
        read, has YAML that doesn't parse, or says nothing.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        _skipped(quiet)("skipping %s: %s", path, e)
        return None
    meta: Any = {}
    body = text
    if (match := FRONT_MATTER.match(text)) is not None:
        meta = _front(match.group(1), path, quiet)
        body = text[match.end():]
    body = body.strip()
    name = normalize_name(path.stem)
    if not isinstance(meta, dict) or not name or not body:
        return None
    about = " ".join(str(meta.get("description") or _first_line(body)).split())
    if _ARGUMENT.search(body):
        body = f"{body}\n\n{ARGUMENTS_NOTE}"
    return Skill(name=f"{normalize_name(plugin)}:{name}", description=about[:DESCRIPTION_MAX], body=body, tools=(),
                 triggers=(), path=path, source="plugin", plugin=plugin)


def _skipped(quiet: bool) -> Callable[..., None]:
    """How a file that can't be used is said: a warning, or (``quiet``: a plugin only looked at before it is installed,
    in a directory that isn't his) a debug line."""
    return log.debug if quiet else log.warning


def _front(text: str, path: Path, quiet: bool = False) -> Any:
    """A file's front matter as YAML data (a safe loader: plain data only); None when it doesn't parse."""
    try:
        return yaml.load(text, Loader=_LOADER) or {}
    except yaml.YAMLError as e:
        _skipped(quiet)("skipping %s: %s", path, e)
        return None


def _first_line(body: str) -> str:
    """A text's first line that says something, without a heading's #."""
    return next((line.strip().lstrip("#").strip() for line in body.splitlines() if line.strip("# \t")), "")


def _words(value: Any, *, split: str) -> tuple[str, ...] | None:
    """A list of words from a YAML value: a list, or one string split by ``split``; None for anything else."""
    if isinstance(value, str):
        value = re.split(split, value)
    if not isinstance(value, list):
        return None
    return tuple(word for word in (" ".join(str(item).split()) for item in value) if word)


def _phrase(text: str) -> str:
    """Text as the matcher compares it: lower-case words separated by single spaces."""
    return _NON_WORD.sub(" ", text.lower()).strip()


def expand_paths(skill: Skill, plugins_dir: Path) -> str:
    """A skill's instructions with the folders its text names written in: ``${CLAUDE_SKILL_DIR}`` (the skill's own
    folder) and, for a plugin's, ``${CLAUDE_PLUGIN_ROOT}`` (the plugin's), so the commands it gives can be run.

    Args:
        skill: the skill.
        plugins_dir: where the plugins are kept (``cfg.plugins_path``).

    Returns:
        The instructions.
    """
    body = skill.body.replace("${CLAUDE_SKILL_DIR}", str(skill.path.parent))
    if skill.plugin:
        body = body.replace("${CLAUDE_PLUGIN_ROOT}", str(plugins_dir / skill.plugin))
    return body


def editable(skill: Skill) -> bool:
    """Whether the HUD may change or delete a skill: yours and the ones he learned (a starter is tracked by git, a
    plugin's comes with it)."""
    return skill.source in ("learned", "yours")


def copy_tree(source: Path, target: Path) -> int:
    """Copy a folder's regular files and folders (never a link, nor what one points at).

    Args:
        source: the folder.
        target: where the copy goes (created; must not exist yet).

    Returns:
        How many files were copied.
    """
    count = 0
    target.mkdir(parents=True)
    for folder, folders, files in os.walk(source, followlinks=False):
        here = Path(folder)
        out = target / here.relative_to(source)
        out.mkdir(exist_ok=True)
        folders[:] = [name for name in folders if not (here / name).is_symlink()]
        for name in files:
            path = here / name
            if path.is_symlink() or not path.is_file():
                continue
            shutil.copyfile(path, out / name)
            count += 1
    return count


def replace_dir(staged: Path, target: Path) -> None:
    """Put a staged folder in a folder's place in two renames (the old one is deleted after).

    Args:
        staged: the new folder, beside the target.
        target: the folder it replaces (it may not exist).
    """
    old = target.with_name(f".old-{target.name}-{secrets.token_hex(4)}")
    with contextlib.suppress(FileNotFoundError):
        target.rename(old)
    staged.rename(target)
    shutil.rmtree(old, ignore_errors=True)


@dataclass(frozen=True, slots=True)
class SkillRoot:
    """Where a plugin keeps skills or commands (``orion.plugins.skill_roots`` lists them).

    Attributes:
        path: a folder of skill folders or one skill's folder (kind "skills"); a folder of .md files or one .md file
            (kind "commands").
        plugin: the plugin.
        kind: "skills" or "commands".
    """

    path: Path
    plugin: str
    kind: RootKind = "skills"


class Skills:
    """The skills, parsed and cached; safe to use from any thread."""

    def __init__(self, root: Path, *, roots: Callable[[], Sequence[SkillRoot]] = tuple,
                 disabled: Callable[[], Collection[str]] = tuple, clock: Callable[[], float] = time.monotonic) -> None:
        """Open the folder (created when missing); nothing is read until the skills are first asked for.

        Args:
            root: the skills folder (``cfg.skills_path``).
            roots: the plugins' skills and commands now (``orion.plugins.skill_roots``); none by default.
            disabled: the names switched off now (``skills.disabled``); none by default.
            clock: a monotonic clock (tests pass a fake one).
        """
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self._roots = roots
        self._disabled = disabled
        self._clock = clock
        self._lock = threading.RLock()
        self._files: dict[tuple[Path, str, RootKind], tuple[Stamp, Skill | None]] = {}
        self._origins: dict[str, str] = {}
        self._origins_stamp: Stamp | None = None
        self._signature: tuple[object, ...] = ()
        self._every: Mapping[str, Skill] = MappingProxyType({})
        self._skills: Mapping[str, Skill] = MappingProxyType({})
        self._phrases: tuple[tuple[str, Skill], ...] = ()
        self._checked = float("-inf")
        self._version = 0

    @classmethod
    def open(cls, cfg: "Config") -> "Skills":
        """The skills of these settings: data/skills, the enabled plugins' skills and commands, ``skills.disabled``.

        Args:
            cfg: the live settings.

        Returns:
            The store.
        """
        from orion.plugins import skill_roots

        return cls(cfg.skills_path, roots=lambda: skill_roots(cfg), disabled=lambda: cfg.skills.disabled)

    @property
    def version(self) -> int:
        """A number that changes whenever a skill is added, changed, removed, switched on or off."""
        self.all()
        return self._version

    def all(self) -> Mapping[str, Skill]:
        """Every skill switched on, by name: data/skills first, in folder order, then the plugins' (when two declare
        one name, the first wins).

        Returns:
            A read-only mapping; the same object until something changes.
        """
        with self._lock:
            if self._clock() - self._checked >= RESCAN_S:
                self._rescan()
            return self._skills

    def everything(self) -> Mapping[str, Skill]:
        """Every skill, switched on or off (for the HUD).

        Returns:
            A read-only mapping, in the order of ``all``.
        """
        with self._lock:
            self.all()
            return self._every

    def invalidate(self) -> None:
        """Look at the folders and the settings again on the next request (a file or a setting changed)."""
        with self._lock:
            self._checked = float("-inf")

    def entry(self, name: str) -> Skill | None:
        """A skill by its exact name, switched on or off.

        Args:
            name: its name (normalised here).

        Returns:
            The skill, or None.
        """
        return self.everything().get(skill_key(name))

    def enabled(self, name: str) -> bool:
        """Whether a skill is switched on.

        Args:
            name: its name.
        """
        return skill_key(name) in self.all()

    def get(self, name: str) -> Skill | None:
        """A skill switched on, by name: exactly, or the only one whose name contains the words given.

        Args:
            name: the name asked for ("movie night", "movie-night", "linear:triage").

        Returns:
            The skill, or None when there is none or the name is ambiguous.
        """
        skills = self.all()
        key = skill_key(name)
        if found := skills.get(key):
            return found
        matches = [skill for skill_name, skill in skills.items() if key and (key in skill_name or skill_name in key)]
        return matches[0] if len(matches) == 1 else None

    def match(self, text: str) -> Skill | None:
        """The skill switched on that the user asked for by name or by one of its trigger phrases, anywhere in what
        they said.

        Args:
            text: the request ("movie night!", "ok, time to focus please").

        Returns:
            The first skill whose name or trigger appears as whole words, else None.
        """
        self.all()
        words = f" {_phrase(text)} "
        return next((skill for phrase, skill in self._phrases if f" {phrase} " in words), None)

    def write(self, name: str, description: str, instructions: str, tools: Sequence[str] | None = None,
              triggers: Sequence[str] | None = None) -> Skill:
        """Create or replace a skill he learned, from its parts (``metadata.author: orion``).

        Args:
            name: its name (normalised).
            description: one line saying what it does and when.
            instructions: the steps.
            tools: the tools it uses.
            triggers: phrases that start it directly.

        Returns:
            The skill as saved.

        Raises:
            ValueError: the name has no letters or digits, the description is too long, or the name is a starter's or
                a plugin's.
        """
        key = normalize_name(name)
        about = " ".join(description.split())
        if not key:
            raise ValueError("A skill needs a name with letters or digits.")
        if len(about) > DESCRIPTION_MAX:
            raise ValueError(f"A skill's description is at most {DESCRIPTION_MAX} characters.")
        meta: dict[str, Any] = {"name": key, "description": about}
        if tools:
            meta["allowed-tools"] = " ".join(tools)
        extra = {"author": AUTHOR}
        if said := [t for t in (" ".join(str(t).replace(",", " ").split()) for t in triggers or ()) if t]:
            extra["triggers"] = ", ".join(said)
        meta["metadata"] = extra
        front = yaml.dump(meta, Dumper=_DUMPER, sort_keys=False, allow_unicode=True, width=1000)
        return self._store(key, f"---\n{front}---\n\n{instructions.strip()}\n")

    def save(self, name: str, content: str) -> Skill:
        """Save a SKILL.md as written in the HUD's editor; nothing changes when it isn't a valid one.

        Args:
            name: the skill's folder name (normalised).
            content: the whole file.

        Returns:
            The skill as saved.

        Raises:
            ValueError: the name is empty or a starter's or a plugin's, the text is not a SKILL.md, its name isn't the
                folder's, or its description is missing or too long.
        """
        key = normalize_name(name)
        if not key or ":" in name:
            raise ValueError("A skill needs a name of letters, digits and hyphens.")
        skill = parse_skill(self.root / key / SKILL_FILE, content)
        if skill is None:
            raise ValueError("A SKILL.md starts with --- front matter (name, description) --- then the steps.")
        if skill.name != key:
            raise ValueError(f"The name in the file is {skill.name}: a skill's name is its folder's ({key}).")
        if not skill.description or len(skill.description) > DESCRIPTION_MAX:
            raise ValueError(f"A skill needs a description of 1 to {DESCRIPTION_MAX} characters.")
        return self._store(key, content)

    def delete(self, name: str) -> bool:
        """Delete a skill's folder.

        Args:
            name: the skill.

        Returns:
            Whether it existed.

        Raises:
            ValueError: it is a starter or a plugin's: those are switched off instead.
        """
        with self._lock:
            skill = self.entry(name)
            if skill is None:
                return False
            _check_editable(skill)
            shutil.rmtree(skill.path.parent, ignore_errors=True)
            if skill.origin:
                self._save_origins({key: value for key, value in self._origins.items() if key != skill.name})
            self._checked = float("-inf")
            return True

    def adopt(self, folder: Path, *, origin: str) -> Skill:
        """Add an imported skill: a copy of its folder (files only, never links) under its name, and where it came
        from. A skill imported before from anywhere is replaced; one written here never is.

        Args:
            folder: the unpacked skill (it holds its SKILL.md).
            origin: where it came from (a URL, or the uploaded file's name).

        Returns:
            The skill as added.

        Raises:
            ValueError: it isn't a skill (no SKILL.md, or one that doesn't parse), its description is missing or too
                long, or a skill of that name that wasn't imported exists.
        """
        skill = parse_skill(folder / SKILL_FILE)
        if skill is None:
            raise ValueError(f"{folder.name} isn't a skill: it needs a SKILL.md that starts with --- front matter "
                             "(name, description) ---.")
        if not skill.description or len(skill.description) > DESCRIPTION_MAX:
            raise ValueError(f"{skill.name} needs a description of 1 to {DESCRIPTION_MAX} characters.")
        with self._lock:
            current = self.entry(skill.name)
            if current is not None and not (current.origin and current.path.is_relative_to(self.root)):
                raise ValueError(f"You already have a skill called {skill.name}.")
            staged = self.root / f".import-{skill.name}-{secrets.token_hex(4)}"
            try:
                copy_tree(folder, staged)
                replace_dir(staged, self.root / skill.name)
            finally:
                shutil.rmtree(staged, ignore_errors=True)
            self._save_origins({**self._origins, skill.name: origin})
            self._checked = float("-inf")
            added = self.entry(skill.name)
        if added is None:
            raise ValueError(f"{skill.name} could not be read back.")
        return added

    def files(self, name: str) -> list[str]:
        """A skill's files, as paths in its folder ("SKILL.md", "references/api.md"; a command: its file).

        Args:
            name: the skill.

        Returns:
            At most ``FILES_MAX`` paths, sorted.

        Raises:
            LookupError: there is no such skill.
        """
        skill = self._found(name)
        if skill.path.name != SKILL_FILE:
            return [skill.path.name]
        folder = skill.path.parent
        found = sorted(path.relative_to(folder).as_posix() for path in _regular_files(folder))
        return found[:FILES_MAX]

    def read(self, name: str, path: str, *, max_chars: int, enabled_only: bool) -> str:
        """The text of one of a skill's files.

        Args:
            name: the skill.
            path: the file, as ``files`` lists it ("" or "SKILL.md" for the skill itself).
            max_chars: the longest text returned (a longer one is cut, and says so).
            enabled_only: only a skill switched on (what the AI may read).

        Returns:
            The text.

        Raises:
            LookupError: no such skill (switched on, when asked), or no such file in its folder.
            ValueError: the file isn't text.
        """
        skill = self._found(name, enabled_only=enabled_only)
        folder = skill.path.parent
        target = skill.path if path.strip() in ("", SKILL_FILE, skill.path.name) else folder / path
        if skill.path.name != SKILL_FILE and target != skill.path:
            raise LookupError(f"{skill.name} is a single file: it has no {path}.")
        resolved = target.resolve()
        if not resolved.is_relative_to(folder.resolve()) or not resolved.is_file():
            raise LookupError(f"{skill.name} has no file {path}.")
        with resolved.open("rb") as f:
            head = f.read(max_chars * 4 + 1)
        try:
            text = codecs.getincrementaldecoder("utf-8")().decode(head)
        except UnicodeDecodeError:
            raise ValueError(f"{path} isn't text: it can't be read as words.") from None
        return text[:max_chars] + TRUNCATED if len(text) > max_chars or len(head) > max_chars * 4 else text

    def view(self, skill: Skill) -> dict[str, JSON]:
        """A skill as the HUD lists it (Customize → Skills).

        Args:
            skill: the skill.

        Returns:
            ``{name, description, source, plugin?, enabled, triggers, tools, files, editable}``.
        """
        out: dict[str, JSON] = {"name": skill.name, "description": skill.description, "source": skill.source,
                                "enabled": self.enabled(skill.name), "triggers": list(skill.triggers),
                                "tools": list(skill.tools), "files": list(self.files(skill.name)),
                                "editable": editable(skill)}
        if skill.plugin:
            out["plugin"] = skill.plugin
        return out

    # ------------------------------------------------------------------------------------------------ internals
    def _found(self, name: str, *, enabled_only: bool = False) -> Skill:
        """A skill by name.

        Raises:
            LookupError: there is none (switched on, when asked).
        """
        skill = self.all().get(skill_key(name)) if enabled_only else self.entry(name)
        if skill is None:
            raise LookupError(f"There is no skill called {skill_key(name) or name!r}"
                              + (" switched on." if enabled_only else "."))
        return skill

    def _store(self, key: str, content: str) -> Skill:
        """Write a skill's file in one step and return it as parsed now.

        Raises:
            ValueError: it would replace a starter or a plugin's skill, or the written file doesn't parse (it never
                happens for text that parsed before).
        """
        path = self.root / key / SKILL_FILE
        with self._lock:
            if (current := self.entry(key)) is not None:
                _check_editable(current)
            write_atomic(path, content)
            self._checked = float("-inf")
            skill = parse_skill(path)
            if skill is None:
                raise ValueError(f"{path} could not be read back.")
            return self.everything().get(skill.name, skill)

    def _save_origins(self, origins: Mapping[str, str]) -> None:
        """Write where each imported skill came from (``SOURCES_FILE``)."""
        path = self.root / SOURCES_FILE
        write_atomic(path, json.dumps({"skills": dict(sorted(origins.items()))}, indent=1) + "\n")
        self._origins, self._origins_stamp = dict(origins), file_stamp(path.stat())

    def _read_origins(self) -> None:
        """Read where each imported skill came from again when the file changed."""
        path = self.root / SOURCES_FILE
        try:
            stamp: Stamp | None = file_stamp(path.stat())
        except OSError:
            stamp = None
        if stamp == self._origins_stamp:
            return
        origins: dict[str, str] = {}
        if stamp is not None:
            try:
                raw = json.loads(path.read_text(encoding="utf-8")).get("skills", {})
            except (OSError, ValueError, AttributeError) as e:
                log.warning("%s can't be read: %s", path, e)
                raw = {}
            origins = {str(k): str(v) for k, v in raw.items() if isinstance(v, str)} if isinstance(raw, dict) else {}
        self._origins, self._origins_stamp = origins, stamp

    def _rescan(self) -> None:
        """Look at every skill file (one stat each) and parse only those that changed; then the settings."""
        roots = tuple(self._roots())
        disabled = frozenset(skill_key(name) for name in self._disabled())
        self._read_origins()
        sources: list[tuple[Path, str, RootKind]] = [(path, "", "skills") for path in skill_files(self.root)]
        for root in roots:
            paths = skill_files(root.path, single=True) if root.kind == "skills" else command_files(root.path)
            sources += [(path, root.plugin, root.kind) for path in paths]
        files: dict[tuple[Path, str, RootKind], tuple[Stamp, Skill | None]] = {}
        for key in sources:
            path, plugin, kind = key
            try:
                stamp = file_stamp(path.stat())
            except OSError:
                continue
            known = self._files.get(key)
            if known is not None and known[0] == stamp:
                files[key] = known
            else:
                files[key] = (stamp, parse_command(path, plugin=plugin) if kind == "commands"
                              else parse_skill(path, plugin=plugin))
        self._checked = self._clock()
        signature = (tuple((key, stamp) for key, (stamp, _) in files.items()), disabled,
                     tuple(sorted(self._origins.items())))
        if signature == self._signature:
            return
        every: dict[str, Skill] = {}
        for (path, plugin, _), (_, skill) in files.items():
            if skill is not None:
                every.setdefault(skill.name, self._placed(skill, path, plugin))
        self._files, self._signature = files, signature
        self._every = MappingProxyType(every)
        self._skills = MappingProxyType({name: skill for name, skill in every.items() if name not in disabled})
        self._phrases = tuple((phrase, skill) for skill in self._skills.values()
                              for phrase in dict.fromkeys([_phrase(skill.name), *map(_phrase, skill.triggers)])
                              if phrase)
        self._version += 1

    def _placed(self, skill: Skill, path: Path, plugin: str) -> Skill:
        """A skill of data/skills with where it came from: a starter, or an import."""
        if plugin:
            return skill
        folder = path.parent.name
        if folder in STARTERS and skill.name == folder:
            return dataclasses.replace(skill, source="builtin")
        if origin := self._origins.get(skill.name):
            return dataclasses.replace(skill, source="yours", origin=origin)
        return skill


def _check_editable(skill: Skill) -> None:
    """Refuse to change or delete a skill the HUD may only switch off.

    Raises:
        ValueError: it is a starter or a plugin's.
    """
    if skill.source == "builtin":
        raise ValueError(f"{skill.name} ships with Orion: switch it off, and write a skill of your own instead.")
    if skill.source == "plugin":
        raise ValueError(f"{skill.name} comes with the {skill.plugin} plugin: switch it off, or remove the plugin.")


def skill_files(folder: Path, *, single: bool = False) -> list[Path]:
    """The SKILL.md files of a folder's skill folders, in name order (dot-folders skipped); with ``single``, the
    folder's own SKILL.md when it is one skill."""
    if single and (folder / SKILL_FILE).is_file():
        return [folder / SKILL_FILE]
    with contextlib.suppress(FileNotFoundError, NotADirectoryError), os.scandir(folder) as entries:
        names = sorted(entry.name for entry in entries if entry.is_dir() and not entry.name.startswith("."))
        return [folder / name / SKILL_FILE for name in names]
    return []


def command_files(path: Path) -> list[Path]:
    """A plugin's command files: one .md file, or a folder's, in name order."""
    if path.is_file():
        return [path] if path.suffix == ".md" else []
    with contextlib.suppress(FileNotFoundError, NotADirectoryError), os.scandir(path) as entries:
        return sorted(path / entry.name for entry in entries
                      if entry.name.endswith(".md") and not entry.name.startswith(".") and entry.is_file())
    return []


def _regular_files(folder: Path) -> Iterator[Path]:
    """Every regular file under a folder (links neither listed nor followed; dot-files skipped)."""
    for here, folders, files in os.walk(folder, followlinks=False):
        base = Path(here)
        folders[:] = sorted(name for name in folders if not name.startswith("."))
        for name in files:
            path = base / name
            if not name.startswith(".") and not path.is_symlink() and path.is_file():
                yield path
