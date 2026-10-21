"""Plugins, and where plugins and skills come from.

A plugin is a Claude plugin: a folder with ``.claude-plugin/plugin.json`` that bundles skills, commands, MCP servers,
agents, hooks... Orion uses two of its parts:
- its skills (``skills/<name>/SKILL.md``) and commands (``commands/*.md``), as skills named "plugin:name"
  (``orion.skills``), as Claude's chat does;
- its MCP servers (``.mcp.json``, either ``{name: server}`` or ``{"mcpServers": {name: server}}``, or the manifest's
  ``mcpServers``), as connectors: each becomes an entry of ``mcp_servers`` marked ``plugin: <name>``.
It never runs anything else a plugin brings: hooks, agents, output styles, LSP servers and mods are listed on its page
as not used by Orion. Installing runs nothing at all: the plugin is unpacked into data/plugins/<name> and recorded in
``plugins.installed``; its connectors then start like any other MCP server (a local one inside the sandbox).

A server's ``${VAR}`` placeholders never read Orion's environment (a plugin could otherwise ask for any of his keys):
each one is a field of its connector, secret when its name says KEY, TOKEN, SECRET or PASSWORD (kept in the vault as
``connector.<id>.<var>``), else saved in the server's ``values``; ``${CLAUDE_PLUGIN_ROOT}`` is the plugin's folder
and ``${CLAUDE_PLUGIN_DATA}`` the connector's own data folder. ``fill`` puts them in when the server connects.

Where they come from:
- marketplaces (``plugins.marketplaces``): git repositories holding ``.claude-plugin/marketplace.json``; Anthropic's
  directory by default. A plugin's source is a folder of the marketplace (``./path``) or another repository
  (``{source: url | git-subdir | github, url | repo, path, ref, sha}``), pinned to a commit;
- skill sources (``skills.sources``): repositories of skill folders; Anthropic's by default;
- uploads: a .zip or .plugin with exactly one ``.claude-plugin/plugin.json``, or a .zip of skills.
A GitHub repository is fetched as a codeload tarball at its commit (no git needed, and not GitHub's API, which allows
60 requests an hour); its pax header names the commit. Any other git host needs ``git``, run with no credentials, over
HTTPS only. A marketplace or a skill source is kept in data/plugins/cache, fetched again once ``plugins.refresh_h``
has passed (with its ETag, so an unchanged one costs a few bytes) or when the HUD asks. Its archive is kept too, at the
commit read: installing one of its plugins, or importing one of its skills, then needs no network.

Everything fetched or uploaded is unpacked defensively: regular files and folders only (never a link or a device),
nothing outside its folder (no absolute path, no ``..``), at most ``plugins.max_files`` files and
``plugins.max_unpacked_mb``, from at most ``plugins.max_download_mb``.
"""

import asyncio
import contextlib
import dataclasses
import hashlib
import io
import json
import logging
import os
import re
import secrets
import shutil
import stat
import tarfile
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, Final, Literal, Protocol, TypedDict
from urllib.parse import quote, unquote

from orion.base import (
    GITHUB_REPO,
    HTTPS_GIT,
    JSON,
    MARKET_NAME,
    PLUGIN_NAME,
    NeedsUser,
    Skill,
    repo_url,
    write_atomic,
)
from orion.skills import (
    SKILL_FILE,
    SkillRoot,
    command_files,
    copy_tree,
    normalize_name,
    parse_command,
    parse_skill,
    replace_dir,
    skill_files,
    skill_key,
)

if TYPE_CHECKING:
    from orion.activity import Activity
    from orion.base import EventBus
    from orion.config import Config, InstalledPlugin
    from orion.secrets import Vault
    from orion.skills import Skills

log = logging.getLogger("orion.plugins")

type Trust = Literal["reviewed", "unreviewed"]

MANIFEST: Final = ".claude-plugin/plugin.json"
MARKETPLACE: Final = ".claude-plugin/marketplace.json"
MCP_FILE: Final = ".mcp.json"
LSP_FILE: Final = ".lsp.json"
HOOKS_FILE: Final = "hooks/hooks.json"
CACHE: Final = "cache"  # data/plugins/cache: what was fetched (no plugin may take this name)
UPLOAD: Final = "upload"  # an uploaded plugin's source (no marketplace may take this name)
REVIEWED: Final = frozenset({"anthropics/claude-plugins-official"})  # GitHub marketplaces whose plugins are reviewed
PLUGIN: Final = re.compile(PLUGIN_NAME)
MARKET: Final = re.compile(MARKET_NAME)
SERVER_ID: Final = re.compile(r"[a-z0-9][a-z0-9_-]{0,40}")  # what tools.core takes as an MCP server's name
PLACEHOLDER: Final = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")  # ${VAR} or ${VAR:-default}
ROOT_VARS: Final = frozenset({"CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA"})  # filled by Orion, never fields
SECRET_WORDS: Final = ("KEY", "TOKEN", "SECRET", "PASSWORD")  # a placeholder whose name says one of these is secret
COMPONENT_KEYS: Final = ("commands", "agents", "skills", "hooks", "mcpServers", "outputStyles", "lspServers", "mods")
KEPT_SETTINGS: Final = ("enabled", "disabled_tools", "sandbox", "gestures", "values", "oauth", "timeout", "intents",
                        "lend")  # what the user set on a plugin's connector, kept when the plugin updates
CONNECTION_KEYS: Final = ("url", "headers", "command", "args", "env", "cwd", "oauth")  # where a server's placeholders are
KNOWN_FIELDS: Final = {  # placeholders Orion words itself: label, and what to put there ({redirect}: sign-in address)
    "OAUTH_CLIENT_ID": ("OAuth client ID", ("From an OAuth app of your own at the maker's console, with {redirect} "
                                            "added as its redirect address")),
    "OAUTH_CLIENT_SECRET": ("OAuth client secret", "From the same OAuth app"),
}
WHO: Final = "you, in the HUD"  # the activity log's actor for what the user does in the HUD
SHA: Final = re.compile(r"[0-9a-f]{40}")
CHUNK: Final = 1 << 16
_GITHUB_TREE: Final = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/tree/([^/]+)(?:/(.+?))?/?")


class FetchError(Exception):
    """A download or a git fetch failed; the message says what, in words."""


class SetupNeeded(NeedsUser):
    """A connector has a field that isn't set yet (a plugin's ``${VAR}``, a secret deleted from the vault)."""


@dataclass(frozen=True, slots=True)
class Limits:
    """What one fetch or upload may cost.

    Attributes:
        download: the largest archive, in bytes.
        unpacked: the most it may unpack to, in bytes.
        files: the most files it may hold.
        timeout_s: the longest one download or git command may take.
    """

    download: int
    unpacked: int
    files: int
    timeout_s: float

    @classmethod
    def of(cls, cfg: "Config") -> "Limits":
        """The limits the settings set (``plugins.*``)."""
        p = cfg.plugins
        return cls(p.max_download_mb << 20, p.max_unpacked_mb << 20, p.max_files, p.timeout_s)


# ---------------------------------------------------------------------------------------------------- sources
@dataclass(frozen=True, slots=True)
class GitRef:
    """A git repository at a commit or a ref, maybe one folder of it.

    Attributes:
        url: its HTTPS URL.
        ref: a branch or tag ("" for the default branch).
        sha: the commit it is pinned to ("" for whatever ``ref`` points at).
        path: a folder in it ("" for all of it).
    """

    url: str
    ref: str = ""
    sha: str = ""
    path: str = ""

    @property
    def github(self) -> tuple[str, str] | None:
        """(owner, repository) when it is on GitHub, else None."""
        match = GITHUB_REPO.fullmatch(self.url)
        return (match.group(1), match.group(2)) if match else None

    @property
    def web(self) -> str:
        """Where people can look at it: on GitHub its folder at its commit, else its URL."""
        if (repo := self.github) is None:
            return self.url
        where = self.sha or self.ref or "HEAD"
        return f"https://github.com/{repo[0]}/{repo[1]}/tree/{where}" + (f"/{self.path}" if self.path else "")


def plugin_source(raw: object, *, market: GitRef, plugin_root: str = "") -> GitRef | str:
    """Where a marketplace's plugin comes from.

    Args:
        raw: the entry's ``source``: "./folder" (in the marketplace's repository), or ``{source: "url" | "git-subdir"
            | "github", url | repo, path, ref, sha}``.
        market: the marketplace's repository, at the commit it was read at.
        plugin_root: the marketplace's ``metadata.pluginRoot``, for a bare folder name.

    Returns:
        The repository (at its pinned commit, maybe a folder of it), or why Orion can't install it.
    """
    if isinstance(raw, str):
        path = raw if raw.startswith("./") or not plugin_root else f"{plugin_root.rstrip('/')}/{raw}"
        parts = _safe_parts(path)
        if parts is None:
            return f"its folder {raw!r} is outside the marketplace"
        return dataclasses.replace(market, path="/".join(parts))
    if not isinstance(raw, dict):
        return "it says no source"
    kind = raw.get("source")
    ref, sha, sub = (str(raw.get(key) or "") for key in ("ref", "sha", "path"))
    if sha and not SHA.fullmatch(sha):
        return f"its commit {sha!r} isn't one"
    parts = _safe_parts(sub) if sub else ()
    if parts is None:
        return f"its folder {sub!r} is outside its repository"
    match kind:
        case "github":
            url = f"https://github.com/{raw.get('repo')}"
        case "url" | "git-subdir":
            url = str(raw.get("url") or "")
        case "npm":
            return "it comes from npm, which Orion doesn't install from"
        case _:
            return f"its source {kind!r} is one Orion doesn't know"
    try:
        url = repo_url(url)
    except ValueError as e:
        return str(e)
    return GitRef(url, ref, sha, "/".join(parts))


def _safe_parts(path: str) -> tuple[str, ...] | None:
    """A relative path as its parts ("./a/b" -> ("a", "b")), None when it would leave its folder (absolute, a drive,
    "..")."""
    path = path.replace("\\", "/")
    if path.startswith("/") or re.match(r"[A-Za-z]:", path):
        return None
    parts = tuple(part for part in path.split("/") if part not in ("", "."))
    return None if ".." in parts else parts


# ---------------------------------------------------------------------------------------------------- unpacking
@dataclass(slots=True)
class _Budget:
    """What an unpacking has taken so far."""

    limits: Limits
    files: int = 0
    size: int = 0

    def take(self, size: int) -> None:
        """Count one more file.

        Raises:
            ValueError: past the limits.
        """
        self.files += 1
        self.size += max(0, size)
        if self.files > self.limits.files:
            raise ValueError(f"it holds more than {self.limits.files} files")
        if self.size > self.limits.unpacked:
            raise ValueError(f"it unpacks to more than {self.limits.unpacked >> 20} MB")


def _write(source: IO[bytes], target: Path, size: int) -> None:
    """Copy one archive member's bytes to a new file, never more than it declared.

    Raises:
        ValueError: it holds more than it said.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with target.open("xb") as out:
        while chunk := source.read(CHUNK):
            written += len(chunk)
            if written > size:
                raise ValueError(f"{target.name} holds more than it says")
            out.write(chunk)


def unpack_tar(archive: Path, dest: Path, limits: Limits, *, strip: int = 0, within: str = "") -> str:
    """Unpack a tarball's regular files and folders into a folder.

    Args:
        archive: the .tar.gz (or any tar).
        dest: where they go (created).
        limits: what it may cost.
        strip: leading path parts dropped from every member (1 for a codeload tarball's "<repo>-<ref>/").
        within: only this folder of it (after ``strip``), its own path dropped too.

    Returns:
        The commit its pax header names (codeload's tarballs say it), "" when it names none.

    Raises:
        ValueError: a member would land outside ``dest``, it is past the limits, or ``within`` isn't in it.
    """
    wanted = _safe_parts(within) if within else ()
    if wanted is None:
        raise ValueError(f"{within!r} is outside the archive")
    dest.mkdir(parents=True, exist_ok=True)
    budget = _Budget(limits)
    found = not wanted
    try:
        with tarfile.open(archive, "r:*") as tar:
            for member in tar:
                parts = _safe_parts(member.name)
                if parts is None:
                    raise ValueError(f"{member.name} would land outside its folder")
                parts = parts[strip:]
                if parts[:len(wanted)] != wanted:
                    continue
                parts = parts[len(wanted):]
                found = True
                if not parts:
                    continue
                if member.isdir():
                    budget.take(0)  # a folder counts as a file: a million empty ones would fill the disk too
                    dest.joinpath(*parts).mkdir(parents=True, exist_ok=True)
                elif member.isfile():  # never a link, a device or a pipe
                    budget.take(member.size)
                    source = tar.extractfile(member)
                    if source is not None:
                        target = dest.joinpath(*parts)
                        _write(source, target, member.size)
                        target.chmod(0o755 if member.mode & 0o111 else 0o644)
            commit = str(tar.pax_headers.get("comment") or "")
    except (FileExistsError, NotADirectoryError, IsADirectoryError) as e:
        raise ValueError(f"it holds {Path(str(e.filename)).name} twice, or as a file and a folder") from None
    except (tarfile.TarError, EOFError) as e:
        raise ValueError(f"it isn't a readable archive ({e})") from None
    if not found:
        raise ValueError(f"it has no folder {within}")
    return commit if SHA.fullmatch(commit) else ""


def unpack_zip(data: bytes, dest: Path, limits: Limits) -> None:
    """Unpack a zip's regular files and folders into a folder (a link inside is left out).

    Args:
        data: the zip.
        dest: where they go (created).
        limits: what it may cost.

    Raises:
        ValueError: it isn't a zip, a member would land outside ``dest``, one is encrypted, or it is past the limits.
    """
    dest.mkdir(parents=True, exist_ok=True)
    budget = _Budget(limits)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for info in archive.infolist():
                parts = _safe_parts(info.filename)
                if parts is None:
                    raise ValueError(f"{info.filename} would land outside its folder")
                if not parts:
                    continue
                if info.is_dir():
                    budget.take(0)
                    dest.joinpath(*parts).mkdir(parents=True, exist_ok=True)
                    continue
                kind = stat.S_IFMT(info.external_attr >> 16)
                if kind not in (0, stat.S_IFREG):  # a link (or anything but a file): never
                    continue
                budget.take(info.file_size)
                target = dest.joinpath(*parts)
                with archive.open(info) as source:
                    _write(source, target, info.file_size)
                target.chmod(0o755 if (info.external_attr >> 16) & 0o111 else 0o644)
    except (FileExistsError, NotADirectoryError, IsADirectoryError) as e:
        raise ValueError(f"it holds {Path(str(e.filename)).name} twice, or as a file and a folder") from None
    except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, NotImplementedError, EOFError) as e:
        raise ValueError(f"it isn't a zip Orion can read ({e})") from None


def digest(folder: Path) -> str:
    """A folder's content as one hash: its files' paths and bytes, in order (what an update compares).

    Args:
        folder: the folder.

    Returns:
        64 hex characters (sha256).
    """
    hasher = hashlib.sha256()
    for path in sorted(_files(folder)):
        hasher.update(path.relative_to(folder).as_posix().encode() + b"\0")
        with path.open("rb") as f:
            while chunk := f.read(CHUNK):
                hasher.update(chunk)
        hasher.update(b"\0")
    return hasher.hexdigest()


def _files(folder: Path) -> Iterator[Path]:
    """Every regular file under a folder (links neither listed nor followed)."""
    for here, folders, names in os.walk(folder, followlinks=False):
        base = Path(here)
        folders[:] = [name for name in folders if not (base / name).is_symlink()]
        for name in names:
            path = base / name
            if not path.is_symlink() and path.is_file():
                yield path


# ---------------------------------------------------------------------------------------------------- fetching
def _download(url: str, target: Path, limits: Limits, etag: str) -> tuple[int, str]:
    """GET a URL into a file (a worker thread's job); (status, its ETag): 304 leaves the file alone.

    Raises:
        FetchError: it failed, answered something else, or was too big or too slow.
    """
    import httpx

    from orion import __version__

    deadline = time.monotonic() + limits.timeout_s
    headers = {"user-agent": f"ORION/{__version__}"}
    if etag:
        headers["if-none-match"] = etag
    try:
        with httpx.Client(timeout=min(30.0, limits.timeout_s), follow_redirects=True) as client, \
                client.stream("GET", url, headers=headers) as response:
            if response.status_code == 304:
                return 304, etag
            if response.status_code != 200:
                raise FetchError(f"{url} answered {response.status_code}")
            if int(response.headers.get("content-length") or 0) > limits.download:
                raise FetchError(f"{url} is bigger than {limits.download >> 20} MB")
            size = 0
            with target.open("wb") as out:
                for chunk in response.iter_bytes(CHUNK):
                    size += len(chunk)
                    if size > limits.download:
                        raise FetchError(f"{url} is bigger than {limits.download >> 20} MB")
                    if time.monotonic() > deadline:
                        raise FetchError(f"{url} took longer than {limits.timeout_s:g} s")
                    out.write(chunk)
            return 200, str(response.headers.get("etag") or "")
    except httpx.HTTPError as e:
        raise FetchError(f"{url} couldn't be reached ({e})") from None


async def _git(*args: str, cwd: Path, timeout_s: float) -> str:
    """Run git with no credentials and over HTTPS only; its output.

    Raises:
        FetchError: git is missing, failed or took too long.
    """
    program = shutil.which("git")
    if program is None:
        raise FetchError("this needs git, which isn't installed (only GitHub's repositories are fetched without it)")
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(cwd), "GIT_TERMINAL_PROMPT": "0",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "LC_ALL": "C"}
    safety = ("-c", "protocol.allow=never", "-c", "protocol.https.allow=always", "-c", "credential.helper=",
              "-c", "core.hooksPath=/dev/null")
    process = await asyncio.create_subprocess_exec(program, *safety, *args, cwd=cwd, env=env,
                                                   stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        async with asyncio.timeout(timeout_s):
            out, err = await process.communicate()
    except TimeoutError:
        process.kill()
        await process.wait()
        raise FetchError(f"git took longer than {timeout_s:g} s") from None
    if process.returncode:
        raise FetchError(f"git {args[0]} failed: {err.decode(errors='replace').strip()[-300:]}")
    return out.decode(errors="replace")


def archive_path(cache: Path, repo: tuple[str, str], commit: str) -> Path:
    """Where the archive of a GitHub repository at a commit is kept."""
    return cache / f"{repo[0]}__{repo[1]}__{commit}.tar.gz"


def keep_archive(cache: Path, repo: tuple[str, str], commit: str, archive: Path) -> None:
    """Keep a repository's archive for installs and imports at its commit; the older ones of it go.

    Args:
        cache: the folder archives are kept in.
        repo: (owner, name).
        commit: the commit it holds.
        archive: the downloaded file (left where it is).
    """
    if not SHA.fullmatch(commit):
        return
    cache.mkdir(parents=True, exist_ok=True)
    target = archive_path(cache, repo, commit)
    part = target.with_suffix(".part")
    shutil.copyfile(archive, part)
    part.replace(target)
    for old in cache.glob(f"{repo[0]}__{repo[1]}__*.tar.gz"):
        if old != target:
            old.unlink(missing_ok=True)


def drop_archives(cache: Path, repos: Iterable[tuple[str, str]]) -> None:
    """Delete every kept archive but those of these repositories (a marketplace or skill source that was removed).

    Args:
        cache: the folder archives are kept in.
        repos: (owner, name) of those to keep.
    """
    wanted = {f"{owner}__{name}__" for owner, name in repos}
    for found in cache.glob("*.tar.gz") if cache.is_dir() else ():
        if not any(found.name.startswith(prefix) for prefix in wanted):
            found.unlink(missing_ok=True)


def _from_archive(ref: GitRef, archive: Path, dest: Path, limits: Limits) -> str:
    """Unpack a repository's archive (a worker thread's job) and check it is the commit asked for.

    Returns:
        The commit.

    Raises:
        FetchError: it isn't the commit ``ref`` is pinned to, or names none.
        ValueError: it is unsafe, past the limits or unreadable.
    """
    commit = unpack_tar(archive, dest, limits, strip=1, within=ref.path) or ref.sha
    if not commit or (ref.sha and commit != ref.sha):
        shutil.rmtree(dest, ignore_errors=True)
        raise FetchError(f"{ref.url} sent commit {commit or 'unknown'}, not {ref.sha or 'a named one'}")
    return commit


async def fetch_tree(ref: GitRef, dest: Path, limits: Limits, *, cache: Path | None = None) -> str:
    """Fetch a repository (or one of its folders) at its commit into a new folder.

    Args:
        ref: what to fetch.
        dest: where it goes (must not exist yet; created).
        limits: what it may cost.
        cache: where archives are kept: one of this commit that is there is used instead of the network.

    Returns:
        The commit.

    Raises:
        FetchError: it couldn't be fetched (or wasn't the commit it is pinned to).
        ValueError: what came is unsafe or past the limits.
    """
    got = await fetch_changed(ref, dest, limits, etag="", cache=cache)
    if got is None:  # pragma: no cover (no ETag was sent, so the answer can't be "unchanged")
        raise FetchError(f"{ref.url} answered 304 to a request that named no version")
    return got[0]


async def fetch_changed(ref: GitRef, dest: Path, limits: Limits, *, etag: str, cache: Path | None = None,
                        keep: bool = False) -> tuple[str, str] | None:
    """``fetch_tree``, unless GitHub says the repository is unchanged since ``etag``.

    Args:
        ref: what to fetch.
        dest: where it goes (must not exist yet; created).
        limits: what it may cost.
        etag: the ETag of the last fetch of it on GitHub ("" for none).
        cache: where archives are kept: a pinned commit that is there is unpacked without the network (a damaged one is
            fetched again).
        keep: keep what is downloaded in ``cache`` (what a marketplace or skill source is: its plugins and skills
            are installed from it).

    Returns:
        (the commit, the new ETag); None when it is unchanged.

    Raises:
        FetchError: it couldn't be fetched (or wasn't the commit it is pinned to).
        ValueError: what came is unsafe or past the limits.
    """
    if (repo := ref.github) is not None:
        owner, name = repo
        url = f"https://codeload.github.com/{owner}/{name}/tar.gz/{quote(ref.sha or ref.ref or 'HEAD', safe='/')}"
        if cache is not None and ref.sha and SHA.fullmatch(ref.sha):
            kept = archive_path(cache, (owner, name), ref.sha)
            if kept.is_file():
                try:
                    return await asyncio.to_thread(_from_archive, ref, kept, dest, limits), ""
                except (FetchError, ValueError, OSError) as e:  # not what it was: fetched, and kept, again below
                    log.warning("the kept archive of %s is no good (%s): fetching it again", ref.url, e)
                    kept.unlink(missing_ok=True)
                    shutil.rmtree(dest, ignore_errors=True)
                    keep = True
        with tempfile.TemporaryDirectory(prefix="orion-fetch-") as scratch:
            archive = Path(scratch) / "repo.tar.gz"
            status, tag = await asyncio.to_thread(_download, url, archive, limits, etag)
            if status == 304:
                return None
            commit = await asyncio.to_thread(_from_archive, ref, archive, dest, limits)
            if keep and cache is not None:
                await asyncio.to_thread(keep_archive, cache, (owner, name), commit, archive)
        return commit, tag
    if not HTTPS_GIT.fullmatch(ref.url):
        raise FetchError(f"{ref.url} isn't an https:// git URL")
    with tempfile.TemporaryDirectory(prefix="orion-git-") as scratch:
        work = Path(scratch) / "repo"
        work.mkdir()
        await _git("init", "-q", cwd=work, timeout_s=limits.timeout_s)
        await _git("fetch", "-q", "--depth", "1", "--no-tags", ref.url, ref.sha or ref.ref or "HEAD", cwd=work,
                   timeout_s=limits.timeout_s)
        await _git("-c", "advice.detachedHead=false", "checkout", "-q", "FETCH_HEAD", cwd=work,
                   timeout_s=limits.timeout_s)
        commit = (await _git("rev-parse", "HEAD", cwd=work, timeout_s=limits.timeout_s)).strip()
        shutil.rmtree(work / ".git", ignore_errors=True)
        source = work.joinpath(*(_safe_parts(ref.path) or ())) if ref.path else work
        if not source.is_dir():
            raise ValueError(f"it has no folder {ref.path}")
        await asyncio.to_thread(_copy_limited, source, dest, limits)
    if ref.sha and commit != ref.sha:
        shutil.rmtree(dest, ignore_errors=True)
        raise FetchError(f"{ref.url} sent commit {commit}, not {ref.sha}")
    return commit, ""


def _copy_limited(source: Path, dest: Path, limits: Limits) -> None:
    """``copy_tree`` within the limits.

    Raises:
        ValueError: past them.
    """
    budget = _Budget(limits)
    for path in _files(source):
        budget.take(path.stat().st_size)
    copy_tree(source, dest)


# ---------------------------------------------------------------------------------------------------- manifests
class ServerSpec(TypedDict):
    """One MCP server a plugin brings, as Orion would run it.

    Keys:
        name: its name in the plugin.
        settings: its ``mcp_servers`` settings (url, transport, headers; or command, args, env, cwd; ``oauth`` when
            it signs in with an OAuth app of its maker's own), placeholders still in.
    """

    name: str
    settings: dict[str, JSON]


@dataclass(frozen=True, slots=True)
class Parts:
    """What a plugin brings.

    Attributes:
        skills: its skills' names (without the plugin's).
        commands: its commands' names.
        servers: its MCP servers.
        unused: what Orion never uses, in words ("Agent: reviewer", "Hooks").
        problems: what of it Orion can't use, and why.
    """

    skills: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()
    servers: tuple[ServerSpec, ...] = ()
    unused: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, JSON]:
        """For the index on disk."""
        return {"skills": list(self.skills), "commands": list(self.commands),
                "servers": [dict(server) for server in self.servers], "unused": list(self.unused),
                "problems": list(self.problems)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Parts":
        """From the index on disk."""
        return cls(tuple(data.get("skills") or ()), tuple(data.get("commands") or ()),
                   tuple(data.get("servers") or ()), tuple(data.get("unused") or ()),
                   tuple(data.get("problems") or ()))


def read_json(path: Path) -> Any:
    """A JSON file's data; None when it is missing or isn't JSON."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def manifest_of(folder: Path, entry: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """A plugin's manifest: its plugin.json, with what its marketplace entry adds (a ``strict: false`` entry defines
    the plugin's parts itself).

    Args:
        folder: the plugin's folder.
        entry: its marketplace entry, when installing from one.

    Returns:
        The manifest ({} when there is none).
    """
    own = read_json(folder / MANIFEST)
    own = own if isinstance(own, dict) else {}
    if entry is None:
        return own
    listed = {key: value for key, value in entry.items() if key not in ("source", "strict")}
    if entry.get("strict") is False:
        return {**{key: value for key, value in own.items() if key not in COMPONENT_KEYS}, **listed}
    return {**listed, **own}


def _paths(folder: Path, manifest: Mapping[str, Any], key: str, default: str) -> list[Path]:
    """A part's places: its default one and those the manifest names ("./x" or a list), inside the folder only."""
    found = [folder / default]
    named = manifest.get(key)
    for raw in [named] if isinstance(named, str) else named if isinstance(named, list) else []:
        parts = _safe_parts(str(raw))
        if parts:
            path = folder.joinpath(*parts)
            if path not in found:
                found.append(path)
    return [path for path in found if path.exists() and path.resolve().is_relative_to(folder.resolve())]


def servers_of(data: object) -> dict[str, dict[str, Any]]:
    """The servers of a .mcp.json (or a manifest's inline ``mcpServers``), in either shape: ``{name: server}`` or
    ``{"mcpServers": {name: server}}``.

    Args:
        data: the parsed JSON.

    Returns:
        name -> server, those that are mappings.
    """
    if isinstance(data, dict) and isinstance(data.get("mcpServers"), dict):
        data = data["mcpServers"]
    if not isinstance(data, dict):
        return {}
    return {str(name): spec for name, spec in data.items() if isinstance(spec, dict)}


def server_settings(spec: Mapping[str, Any]) -> dict[str, JSON] | str:
    """A plugin's server as Orion's ``mcp_servers`` settings.

    Args:
        spec: ``{"type": "http" | "sse", "url", "headers"}`` or ``{"command", "args", "env", "cwd"}``. A remote one
            that names an OAuth client of its maker's (``oauth.clientId``: Orion can't use another app's) is signed
            in to with an OAuth app of the user's own: its ID and secret become two placeholders.

    Returns:
        The settings (placeholders still in), or why Orion can't run it.
    """
    kind = str(spec.get("type") or ("stdio" if "command" in spec else "http" if "url" in spec else ""))
    if not kind:
        return "it has neither a command to run nor a URL"
    out: dict[str, JSON] = {}
    if kind in ("http", "streamable-http", "sse"):
        url = spec.get("url")
        if not isinstance(url, str) or not re.match(r"https?://", url):
            return "it has no http(s) URL"
        out["url"] = url
        if kind == "sse":
            out["transport"] = "sse"
        headers = spec.get("headers")
        if isinstance(headers, dict) and headers:
            out["headers"] = {str(key): str(value) for key, value in headers.items()}
        if isinstance(spec.get("oauth"), dict) and spec["oauth"].get("clientId"):
            out["oauth"] = {"client_id": "${OAUTH_CLIENT_ID}", "client_secret": "${OAUTH_CLIENT_SECRET}"}
        return out
    if kind != "stdio":
        return f"its kind {kind!r} is one Orion can't connect to"
    command, args, env = spec.get("command"), spec.get("args") or [], spec.get("env") or {}
    if not isinstance(command, str) or not command.strip() or not isinstance(args, list) or not isinstance(env, dict):
        return "its command, args or env aren't what a server's are"
    out["command"] = command
    if args:
        out["args"] = [str(arg) for arg in args]
    if env:
        out["env"] = {str(key): str(value) for key, value in env.items()}
    if isinstance(spec.get("cwd"), str):
        out["cwd"] = spec["cwd"]
    return out


def secret_name(var: str) -> bool:
    """Whether a placeholder holds a secret: its name says KEY, TOKEN, SECRET or PASSWORD ("GITHUB_TOKEN",
    "APIKEY")."""
    return any(part.endswith(SECRET_WORDS) for part in var.upper().split("_"))


def placeholders(value: object) -> dict[str, str | None]:
    """The ``${VAR}`` placeholders in a server's settings (``CLAUDE_PLUGIN_ROOT`` and ``_DATA`` aside), in order.

    Args:
        value: the settings.

    Returns:
        name -> its default (``${VAR:-default}``), None when it has none.
    """
    found: dict[str, str | None] = {}
    for text in strings(value):
        for match in PLACEHOLDER.finditer(text):
            if match.group(1) not in ROOT_VARS:
                found.setdefault(match.group(1), match.group(2))
    return found


def map_strings(value: Any, change: Callable[[str], str]) -> Any:
    """A JSON value with every string (not the keys) changed by a function.

    Args:
        value: the value.
        change: what to do with each string.

    Returns:
        A new value of the same shape.
    """
    if isinstance(value, str):
        return change(value)
    if isinstance(value, dict):
        return {key: map_strings(item, change) for key, item in value.items()}
    if isinstance(value, list):
        return [map_strings(item, change) for item in value]
    return value


def strings(value: object) -> Iterator[str]:
    """Every string in a JSON value (keys included)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def placeholder_fields(found: Mapping[str, str | None]) -> list[dict[str, JSON]]:
    """A server's placeholders as the fields the HUD asks for (a secret one when its name says so).

    Args:
        found: ``placeholders`` of its settings.

    Returns:
        Fields: key (the placeholder), label, kind, help, placeholder, options, required, secret.
    """
    fields: list[dict[str, JSON]] = []
    for var, default in found.items():
        secret = secret_name(var)
        label, about = KNOWN_FIELDS.get(var, (var.replace("_", " ").capitalize(), f"The plugin's ${{{var}}}"))
        fields.append({"key": var, "label": label, "kind": "secret" if secret else "text",
                       "help": about + (f" (empty: {default})" if default else "") + ".",
                       "placeholder": "" if secret else default or "", "options": [], "required": default is None,
                       "secret": secret})
    return fields


def parts_of(folder: Path, manifest: Mapping[str, Any], plugin: str, *, quiet: bool = False) -> Parts:
    """What a plugin's folder brings, by its manifest and the default places.

    Args:
        folder: the plugin's folder.
        manifest: ``manifest_of`` it.
        plugin: its name (its skills are named "plugin:skill").
        quiet: a file of it that can't be used is only a debug line (a plugin of a marketplace, looked at before anyone
            installs it: what is wrong with it isn't his to hear).

    Returns:
        Its parts.
    """
    roots = roots_of(folder, manifest, plugin)
    found = [parse_skill(path, plugin=plugin, quiet=quiet) for root in roots if root.kind == "skills"
             for path in skill_files(root.path, single=True)]
    skills = [skill.name.split(":", 1)[1] for skill in found if skill is not None]
    commands = [skill.name.split(":", 1)[1] for root in roots if root.kind == "commands"
                for skill in (parse_command(path, plugin=plugin, quiet=quiet) for path in command_files(root.path))
                if skill is not None]
    servers: list[ServerSpec] = []
    problems: list[str] = []
    for name, spec in _server_specs(folder, manifest).items():
        settings = server_settings(spec)
        if isinstance(settings, str):
            problems.append(f"MCP server {name}: {settings}")
            continue
        servers.append(ServerSpec(name=name, settings=settings))
    return Parts(tuple(dict.fromkeys(skills)), tuple(dict.fromkeys(commands)), tuple(servers),
                 tuple(_unused(folder, manifest)), tuple(problems))


def roots_of(folder: Path, manifest: Mapping[str, Any], plugin: str) -> list[SkillRoot]:
    """Where a plugin keeps skills and commands: skills/ and commands/, and the places its manifest names.

    Args:
        folder: the plugin's folder.
        manifest: its manifest.
        plugin: its name.

    Returns:
        The places that exist.
    """
    return [*(SkillRoot(path, plugin, "skills") for path in _paths(folder, manifest, "skills", "skills")),
            *(SkillRoot(path, plugin, "commands") for path in _paths(folder, manifest, "commands", "commands"))]


def _server_specs(folder: Path, manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """A plugin's MCP servers: .mcp.json, and the manifest's ``mcpServers`` (inline, or the files it names)."""
    found = servers_of(read_json(folder / MCP_FILE))
    named = manifest.get("mcpServers")
    if isinstance(named, dict):
        found |= servers_of(named)
    else:
        for path in _paths(folder, manifest, "mcpServers", MCP_FILE):
            if path != folder / MCP_FILE:
                found |= servers_of(read_json(path))
    return found


def _unused(folder: Path, manifest: Mapping[str, Any]) -> Iterator[str]:
    """What a plugin brings that Orion never runs, in words."""
    for path in _paths(folder, manifest, "agents", "agents"):
        names = [path.stem] if path.is_file() else sorted(p.stem for p in path.glob("*.md"))
        yield from (f"Agent: {name}" for name in names)
    if _paths(folder, manifest, "hooks", HOOKS_FILE) or isinstance(manifest.get("hooks"), dict):
        yield "Hooks"
    for path in _paths(folder, manifest, "outputStyles", "output-styles"):
        names = [path.stem] if path.is_file() else sorted(p.stem for p in path.glob("*.md"))
        yield from (f"Output style: {name}" for name in names)
    lsp: dict[str, Any] = {}
    for path in _paths(folder, manifest, "lspServers", LSP_FILE):
        data = read_json(path)
        lsp |= data if isinstance(data, dict) else {}
    if isinstance(manifest.get("lspServers"), dict):
        lsp |= manifest["lspServers"]
    yield from (f"LSP server: {name}" for name in lsp)
    for path in _paths(folder, manifest, "mods", "mods"):
        names = [path.stem] if path.is_file() else sorted(p.name for p in path.iterdir() if not p.name.startswith("."))
        yield from (f"Mod: {name}" for name in names)


def author_of(manifest: Mapping[str, Any]) -> str:
    """A manifest's author, in words ("" when unsaid)."""
    author = manifest.get("author")
    if isinstance(author, dict):
        return str(author.get("name") or "")
    return str(author) if isinstance(author, str) else ""


# ---------------------------------------------------------------------------------------------------- connectors
def connector_id(server: str, plugin: str, taken: Callable[[str], bool]) -> str:
    """The connector id a plugin's server gets: its own name made an MCP server name, else "plugin-server" when that
    is taken.

    Args:
        server: its name in the plugin.
        plugin: the plugin.
        taken: whether an id is used by something else already.

    Returns:
        The id.

    Raises:
        ValueError: both are taken.
    """
    base = re.sub(r"[^a-z0-9_-]+", "-", server.lower()).strip("-_")[:41] or "server"
    for candidate in (base, f"{plugin}-{base}"[:41].strip("-_")):
        if SERVER_ID.fullmatch(candidate) and not taken(candidate):
            return candidate
    raise ValueError(f"the {plugin} plugin's {server} server can't be added: there are connectors called {base} and "
                     f"{plugin}-{base} already")


def vault_name(connector: str, field: str) -> str:
    """The vault's name for one of a connector's secrets: ``connector.<id>.<field>`` (lower case)."""
    return f"connector.{connector}.{re.sub(r'[^a-z0-9._-]+', '_', field.lower())}"


def fill(settings: Mapping[str, Any], *, connector: str, root: Path, data: Path,
         secret: Callable[[str], str | None]) -> dict[str, Any]:
    """A plugin's server settings with its placeholders put in, as it connects (never from Orion's environment).

    Args:
        settings: its ``mcp_servers`` entry (``values`` holds its fields that aren't secret).
        connector: its connector id.
        root: the plugin's folder (``${CLAUDE_PLUGIN_ROOT}``).
        data: the connector's data folder (``${CLAUDE_PLUGIN_DATA}``).
        secret: reads a vault entry (None when it isn't set).

    Returns:
        The settings to connect with.

    Raises:
        SetupNeeded: a field is not set, naming it.
    """
    values = settings.get("values") if isinstance(settings.get("values"), dict) else {}

    def put(match: re.Match[str]) -> str:
        """One placeholder's value."""
        var, default = match.group(1), match.group(2)
        if var == "CLAUDE_PLUGIN_ROOT":
            return str(root)
        if var == "CLAUDE_PLUGIN_DATA":
            return str(data)
        if secret_name(var):
            value = secret(vault_name(connector, var))
        else:
            given = values.get(var)
            value = str(given) if given not in (None, "") else None
        if value is None and default is not None:
            return default
        if value is None:
            raise SetupNeeded(f"{var.replace('_', ' ').capitalize()} isn't set (Customize, Connectors, {connector}).")
        return value

    return {key: map_strings(value, lambda text: PLACEHOLDER.sub(put, text)) if key in CONNECTION_KEYS else value
            for key, value in settings.items()}


# ---------------------------------------------------------------------------------------------------- skill roots
_MANIFEST_LOCK: Final = threading.Lock()
_MANIFESTS: dict[Path, tuple[object, dict[str, Any]]] = {}  # folder -> (plugin.json's stamp, its manifest)


def cached_manifest(folder: Path) -> dict[str, Any]:
    """``manifest_of`` a plugin's folder, read again only when its plugin.json changed (the HUD asks often).

    Args:
        folder: the plugin's folder.

    Returns:
        The manifest ({} when there is none); treat it as read-only.
    """
    try:
        stamp: object = (folder / MANIFEST).stat().st_mtime_ns
    except OSError:
        stamp = None
    with _MANIFEST_LOCK:
        known = _MANIFESTS.get(folder)
        if known is None or known[0] != stamp:
            known = _MANIFESTS[folder] = (stamp, manifest_of(folder))
        return known[1]


def skill_roots(cfg: "Config") -> tuple[SkillRoot, ...]:
    """Where the enabled plugins keep skills and commands (what ``Skills`` reads besides data/skills).

    Args:
        cfg: the live settings (``plugins.installed``, the data folder).

    Returns:
        The places, plugin by plugin in the settings' order (each plugin's manifest is read again only when its
        plugin.json changed).
    """
    found: list[SkillRoot] = []
    for name, record in cfg.plugins.installed.items():
        folder = cfg.plugins_path / name
        if record["enabled"] and folder.is_dir():
            found += roots_of(folder, cached_manifest(folder), name)
    return tuple(found)


def trust_of(source: str, markets: Mapping[str, str]) -> Trust:
    """A plugin's trust: "reviewed" when it comes from Anthropic's directory, else "unreviewed".

    Args:
        source: the marketplace it came from (``UPLOAD`` for an upload).
        markets: ``plugins.marketplaces``.
    """
    url = markets.get(source, "")
    repo = GitRef(url).github if url else None
    return "reviewed" if repo is not None and f"{repo[0]}/{repo[1]}".lower() in REVIEWED else "unreviewed"


# ---------------------------------------------------------------------------------------------------- the library
@dataclass(slots=True)
class Market:
    """A marketplace as fetched.

    Attributes:
        name: its name (its marketplace.json's).
        url: its repository.
        sha: the commit it was read at.
        etag: what GitHub said of that tarball (an unchanged one isn't fetched again).
        updated_at: when it was last fetched or found unchanged (epoch seconds; 0 never).
        error: why the last fetch failed ("" when it didn't).
        plugins: its entries, as listed.
        parts: what each of its plugins in the marketplace's own folders brings, by name (others are unknown until
            installed).
        digests: those plugins' content hashes (what an update compares).
        plugin_root: its ``metadata.pluginRoot``.
    """

    name: str
    url: str
    sha: str = ""
    etag: str = ""
    updated_at: float = 0.0
    error: str = ""
    plugins: list[dict[str, Any]] = field(default_factory=list)
    parts: dict[str, Parts] = field(default_factory=dict)
    digests: dict[str, str] = field(default_factory=dict)
    plugin_root: str = ""

    def entry(self, name: str) -> dict[str, Any] | None:
        """A plugin's entry by name."""
        return next((p for p in self.plugins if p.get("name") == name), None)

    def as_dict(self) -> dict[str, Any]:
        """For the cache on disk."""
        return {"name": self.name, "url": self.url, "sha": self.sha, "etag": self.etag, "updated_at": self.updated_at,
                "error": self.error, "plugins": self.plugins, "plugin_root": self.plugin_root,
                "parts": {name: parts.as_dict() for name, parts in self.parts.items()}, "digests": self.digests}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Market":
        """From the cache on disk."""
        return cls(str(data["name"]), str(data["url"]), str(data.get("sha") or ""), str(data.get("etag") or ""),
                   float(data.get("updated_at") or 0), str(data.get("error") or ""), list(data.get("plugins") or []),
                   {name: Parts.from_dict(parts) for name, parts in (data.get("parts") or {}).items()},
                   dict(data.get("digests") or {}), str(data.get("plugin_root") or ""))


def read_market(folder: Path, url: str, sha: str, etag: str) -> Market:
    """A fetched marketplace: its plugins, and what those in its own folders bring (a worker thread's job).

    Args:
        folder: the repository as fetched.
        url: its URL.
        sha: the commit it was fetched at.
        etag: GitHub's ETag of that fetch.

    Returns:
        The marketplace (its entries without a usable name left out).

    Raises:
        ValueError: it has no marketplace.json listing plugins, or no usable name.
    """
    data = read_json(folder / MARKETPLACE)
    if isinstance(data, dict) and isinstance(data.get("plugins"), list):
        return _market(data, folder, url, sha, etag)
    raise ValueError(f"{url} has no {MARKETPLACE} listing plugins")


def _market(data: Mapping[str, Any], folder: Path, url: str, sha: str, etag: str) -> Market:
    """``read_market`` once its marketplace.json is known to list plugins."""
    name = str(data.get("name") or "")
    if not MARKET.fullmatch(name) or name == UPLOAD:
        raise ValueError(f"{url}'s marketplace has no usable name ({name!r})")
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    market = Market(name, url, sha, etag, time.time(), plugin_root=str(metadata.get("pluginRoot") or ""))
    here = GitRef(url, sha=sha)
    for entry in data["plugins"]:
        if not isinstance(entry, dict) or not PLUGIN.fullmatch(str(entry.get("name") or "")) \
                or entry.get("name") == CACHE:
            continue
        market.plugins.append(entry)
        source = plugin_source(entry.get("source"), market=here, plugin_root=market.plugin_root)
        if isinstance(source, GitRef) and source.url == url:  # in the marketplace itself: known now
            path = folder.joinpath(*(_safe_parts(source.path) or ()))
            if path.is_dir() and path.resolve().is_relative_to(folder.resolve()):
                plugin = str(entry["name"])
                market.parts[plugin] = parts_of(path, manifest_of(path, entry), plugin, quiet=True)
                market.digests[plugin] = digest(path)
    return market


@dataclass(slots=True)
class SkillSource:
    """A repository of skills as fetched (``skills.sources``).

    Attributes:
        url: the repository.
        sha: the commit it was read at.
        etag: GitHub's ETag of that tarball.
        updated_at: when it was last fetched or found unchanged.
        error: why the last fetch failed.
        skills: its skills: {name, description, path} (path: the folder in the repository).
    """

    url: str
    sha: str = ""
    etag: str = ""
    updated_at: float = 0.0
    error: str = ""
    skills: list[dict[str, str]] = field(default_factory=list)


class LibraryHost(Protocol):
    """What the library needs of the running Orion (``runtime.Runtime`` provides it)."""

    cfg: "Config"
    skills: "Skills"
    bus: "EventBus"

    @property
    def vault(self) -> "Vault":
        """The vault (a removed plugin's connectors' secrets go)."""

    @property
    def activity(self) -> "Activity":
        """The activity log."""

    def reserved(self, name: str) -> bool:
        """Whether a connector id is one of Orion's own groups' or the directory's (a plugin's server can't take
        it)."""

    async def apply_own(self, patch: Mapping[str, Any]) -> dict[str, JSON]:
        """Save settings the user changed on a page of their own (validated, then applied)."""


class Library:
    """Marketplaces, skill sources and the installed plugins (see the module docstring).

    Changes run one at a time (``_lock``); the lists the HUD reads are swapped whole, so reads never wait.
    """

    def __init__(self, host: LibraryHost) -> None:
        """Read what was fetched before (nothing is fetched until ``refresh``).

        Args:
            host: the running Orion.
        """
        self.host = host
        self._lock = asyncio.Lock()
        self._markets: dict[str, Market] = {}
        self._sources: dict[str, SkillSource] = {}
        self._loaded = False

    # ------------------------------------------------------------------------------------------------ places
    @property
    def cfg(self) -> "Config":
        """The live settings."""
        return self.host.cfg

    @property
    def root(self) -> Path:
        """data/plugins."""
        return self.cfg.plugins_path

    @property
    def cache(self) -> Path:
        """data/plugins/cache."""
        return self.root / CACHE

    @property
    def archives(self) -> Path:
        """data/plugins/cache/archives: the marketplaces' and skill sources' archives, for installs and imports."""
        return self.cache / "archives"

    def _tidy_archives(self) -> None:
        """Delete the archives of marketplaces and skill sources that are no longer there (a worker thread's job)."""
        urls = [*self.cfg.plugins.marketplaces.values(), *self.cfg.skills.sources]
        drop_archives(self.archives, [repo for url in urls if (repo := GitRef(url).github) is not None])

    def _market_file(self, name: str) -> Path:
        return self.cache / "marketplaces" / f"{name}.json"

    def _source_slug(self, url: str) -> str:
        return hashlib.blake2b(url.encode(), digest_size=6).hexdigest()

    def load(self) -> None:
        """Read the cached marketplaces and skill sources (once; a worker thread's job)."""
        if self._loaded:
            return
        self._loaded = True
        for name in self.cfg.plugins.marketplaces:
            data = read_json(self._market_file(name))
            if isinstance(data, dict):
                with contextlib.suppress(KeyError, TypeError, ValueError):
                    self._markets[name] = Market.from_dict(data)
        for url in self.cfg.skills.sources:
            data = read_json(self.cache / "skills" / f"{self._source_slug(url)}.json")
            if isinstance(data, dict):
                with contextlib.suppress(KeyError, TypeError, ValueError):
                    self._sources[url] = SkillSource(url, str(data.get("sha") or ""), str(data.get("etag") or ""),
                                                     float(data.get("updated_at") or 0), str(data.get("error") or ""),
                                                     list(data.get("skills") or []))

    # ------------------------------------------------------------------------------------------------ refreshing
    async def refresh(self, *, force: bool = False) -> None:
        """Fetch the marketplaces and skill sources that are older than ``plugins.refresh_h`` (all, with ``force``);
        a failure is kept as that one's error, and the others still refresh.

        Args:
            force: fetch every one now.
        """
        await asyncio.to_thread(self.load)
        stale = time.time() - self.cfg.plugins.refresh_h * 3600
        async with self._lock:
            for name, url in list(self.cfg.plugins.marketplaces.items()):
                market = self._markets.get(name)
                if force or market is None or market.updated_at < stale or market.url != url:
                    await self._refresh_market(name, url)
            for url in list(self.cfg.skills.sources):
                source = self._sources.get(url)
                if force or source is None or source.updated_at < stale:
                    await self._refresh_source(url)
            await asyncio.to_thread(self._tidy_archives)
        self.host.bus.publish("plugins", items=self.plugins())

    async def _refresh_market(self, name: str, url: str) -> Market:
        """Fetch one marketplace again (unchanged: only its time moves on); a failure is kept as its error."""
        old = self._markets.get(name)
        market = old if old is not None and old.url == url else Market(name, url)
        try:
            fresh = await self._fetch_market(url, etag=market.etag)
        except (FetchError, ValueError, OSError) as e:
            log.warning("marketplace %s couldn't be fetched: %s", name, e)
            market.error = str(e)
        else:
            if fresh is not None:
                fresh.name = name  # the name it was added under (its marketplace.json says the same)
                market = fresh
            market.error = ""
            market.updated_at = time.time()
        self._markets[name] = market
        await asyncio.to_thread(write_atomic, self._market_file(name), json.dumps(market.as_dict()))
        return market

    async def _fetch_market(self, url: str, *, etag: str = "") -> Market | None:
        """Fetch a marketplace's repository and read it: its plugins, and what those in its own folders bring.

        Returns:
            The marketplace; None when GitHub says it hasn't changed since ``etag``.

        Raises:
            FetchError, ValueError: it couldn't be fetched, or isn't a marketplace.
        """
        limits = Limits.of(self.cfg)
        folder = self.cache / "marketplaces" / f".fetch-{secrets.token_hex(4)}"
        try:
            got = await fetch_changed(GitRef(url), folder, limits, etag=etag, cache=self.archives, keep=True)
            if got is None:
                return None
            sha, tag = got
            return await asyncio.to_thread(read_market, folder, url, sha, tag)
        finally:
            await asyncio.to_thread(shutil.rmtree, folder, True)

    async def _refresh_source(self, url: str) -> SkillSource:
        """Fetch one skill source again; a failure is kept as its error."""
        source = self._sources.get(url) or SkillSource(url)
        folder = self.cache / "skills" / f".fetch-{secrets.token_hex(4)}"
        try:
            got = await fetch_changed(GitRef(url), folder, Limits.of(self.cfg), etag=source.etag,
                                      cache=self.archives, keep=True)
            if got is not None:
                source.sha, source.etag = got
                source.skills = await asyncio.to_thread(_skills_in, folder)
            source.error = ""
            source.updated_at = time.time()
        except (FetchError, ValueError, OSError) as e:
            log.warning("skill source %s couldn't be fetched: %s", url, e)
            source.error = str(e)
        finally:
            await asyncio.to_thread(shutil.rmtree, folder, True)
        self._sources[url] = source
        record = {"url": url, "sha": source.sha, "etag": source.etag, "updated_at": source.updated_at,
                  "error": source.error, "skills": source.skills}
        await asyncio.to_thread(write_atomic, self.cache / "skills" / f"{self._source_slug(url)}.json",
                                json.dumps(record))
        return source

    # ------------------------------------------------------------------------------------------------ marketplaces
    def marketplaces(self) -> list[dict[str, JSON]]:
        """The marketplaces: ``{name, url, plugins, updated_at, error}``."""
        self.load()
        out: list[dict[str, JSON]] = []
        for name, url in self.cfg.plugins.marketplaces.items():
            market = self._markets.get(name)
            out.append({"name": name, "url": url, "plugins": len(market.plugins) if market else 0,
                        "updated_at": market.updated_at if market else 0.0, "error": market.error if market else ""})
        return out

    async def add_marketplace(self, url: str) -> dict[str, JSON]:
        """Add a marketplace: fetched and read first, then kept under the name it gives itself.

        Args:
            url: its repository ("owner/repo" on GitHub, or an https:// git URL).

        Returns:
            It, as ``marketplaces`` lists it.

        Raises:
            ValueError: it isn't a marketplace, or one by that name exists with another URL.
            FetchError: it couldn't be fetched.
        """
        url = repo_url(url)
        async with self._lock:
            fetched = await self._fetch_market(url)
            if fetched is None:  # pragma: no cover (no ETag was sent)
                raise FetchError(f"{url} answered as if it had been fetched before")
            markets = self.cfg.plugins.marketplaces
            if markets.get(fetched.name, url) != url:
                raise ValueError(f"There's a marketplace called {fetched.name} already ({markets[fetched.name]}).")
            self._markets[fetched.name] = fetched
            await asyncio.to_thread(write_atomic, self._market_file(fetched.name), json.dumps(fetched.as_dict()))
            await self.host.apply_own({"plugins.marketplaces": {**markets, fetched.name: url}})
        self.host.activity.record("plugin", source="hud", title=f"Added the {fetched.name} marketplace",
                                  outcome="ok", detail={"url": url, "plugins": len(fetched.plugins)})
        return next(item for item in self.marketplaces() if item["name"] == fetched.name)

    async def remove_marketplace(self, name: str) -> None:
        """Forget a marketplace (the plugins installed from it stay).

        Raises:
            LookupError: there is none by that name.
        """
        markets = self.cfg.plugins.marketplaces
        if name not in markets:
            raise LookupError(f"There's no marketplace called {name}.")
        async with self._lock:
            await self.host.apply_own({"plugins.marketplaces": {k: v for k, v in markets.items() if k != name}})
            self._markets.pop(name, None)
            await asyncio.to_thread(lambda: self._market_file(name).unlink(missing_ok=True))
            await asyncio.to_thread(self._tidy_archives)
        self.host.activity.record("plugin", source="hud", title=f"Removed the {name} marketplace", outcome="ok")

    # ------------------------------------------------------------------------------------------------ discover
    def discover(self, *, query: str = "", category: str = "", marketplace: str = "") -> dict[str, JSON]:
        """The marketplaces' plugins, for the HUD's Discover tab.

        Args:
            query: words that must all appear in a plugin's name, description, category or author.
            category: only this category ("" all).
            marketplace: only this marketplace ("" all).

        Returns:
            ``{items: [{name, description, category, marketplace, author, homepage, installed, components_known}],
            categories}``: a name match first, then by name.
        """
        self.load()
        words = query.lower().split()
        installed = self.cfg.plugins.installed
        items: list[tuple[tuple[int, str], dict[str, JSON]]] = []
        categories: set[str] = set()
        for name, market in self._markets.items():
            if name not in self.cfg.plugins.marketplaces or (marketplace and name != marketplace):
                continue
            for entry in market.plugins:
                plugin = str(entry["name"])
                kind = str(entry.get("category") or "")
                if kind:
                    categories.add(kind)
                author = author_of(entry)
                text = f"{plugin} {entry.get('description') or ''} {kind} {author}".lower()
                if (category and kind != category) or not all(word in text for word in words):
                    continue
                rank = 0 if words and all(word in plugin for word in words) else 1
                record = installed.get(plugin)
                items.append(((rank, plugin), {
                    "name": plugin, "description": str(entry.get("description") or ""), "category": kind,
                    "marketplace": name, "author": author, "homepage": str(entry.get("homepage") or ""),
                    "installed": record is not None and record.get("source") == name,
                    "components_known": plugin in market.parts}))
        return {"items": [item for _, item in sorted(items, key=lambda pair: pair[0])],
                "categories": sorted(categories)}

    def listing(self, marketplace: str, name: str) -> tuple[Market, dict[str, Any]]:
        """A marketplace's plugin.

        Raises:
            LookupError: no such marketplace (fetched) or plugin.
        """
        self.load()
        market = self._markets.get(marketplace)
        if market is None or marketplace not in self.cfg.plugins.marketplaces:
            raise LookupError(f"There's no marketplace called {marketplace} (or it hasn't been fetched yet).")
        entry = market.entry(name)
        if entry is None:
            raise LookupError(f"The {marketplace} marketplace has no plugin called {name}.")
        return market, entry

    def connector_listings(self) -> list[tuple[str, Market, dict[str, Any], Parts]]:
        """The marketplaces' plugins known to bring MCP servers (their own folders' plugins): (marketplace, it,
        entry, parts)."""
        self.load()
        found: list[tuple[str, Market, dict[str, Any], Parts]] = []
        for name, market in self._markets.items():
            if name not in self.cfg.plugins.marketplaces:
                continue
            for plugin, parts in market.parts.items():
                entry = market.entry(plugin)
                if parts.servers and entry is not None:
                    found.append((name, market, entry, parts))
        return found

    # ------------------------------------------------------------------------------------------------ installed
    def folder(self, name: str) -> Path:
        """An installed plugin's folder."""
        return self.root / name

    def plugins(self) -> list[dict[str, JSON]]:
        """Every installed plugin, as the HUD shows it (``view``)."""
        return [self.view(name) for name in self.cfg.plugins.installed]

    def view(self, name: str) -> dict[str, JSON]:
        """One installed plugin as the HUD shows it.

        Returns:
            ``{name, version, description, author, homepage, source: {marketplace?, url?, sha}, enabled,
            installed_at, trust, components: {skills, commands, connectors, unused}, update?: {version, sha}}``.

        Raises:
            LookupError: it isn't installed.
        """
        self.load()
        record = self.cfg.plugins.installed.get(name)
        if record is None:
            raise LookupError(f"There's no plugin called {name}.")
        manifest = cached_manifest(self.folder(name))
        market = self._markets.get(record["source"])
        entry = market.entry(name) if market is not None else None
        source: dict[str, JSON] = {"sha": record["sha"]}
        if record["source"] != UPLOAD:
            source["marketplace"] = record["source"]
            if market is not None and entry is not None:
                ref = plugin_source(entry.get("source"), market=GitRef(market.url, sha=market.sha),
                                    plugin_root=market.plugin_root)
                if isinstance(ref, GitRef):
                    source["url"] = ref.web
        homepage = next((value for value in (manifest.get("homepage"), manifest.get("repository"))
                         if isinstance(value, str) and value.startswith("https://")), "")
        components: dict[str, JSON] = dict(self._components(name))
        out: dict[str, JSON] = {
            "name": name, "version": record["version"], "description": str(manifest.get("description") or ""),
            "author": author_of(manifest), "homepage": homepage, "source": source, "enabled": record["enabled"],
            "installed_at": record["installed_at"], "trust": trust_of(record["source"], self.cfg.plugins.marketplaces),
            "components": components}
        if (update := self._update(name, record, market, entry)) is not None:
            out["update"] = update
        return out

    def _components(self, name: str) -> dict[str, list[JSON]]:
        """What an installed plugin brings, by kind: skills and commands by their names ("plugin:skill"), connectors
        by id, and what Orion doesn't use (with what of it he can't), in words."""
        folder = self.folder(name)
        parts = parts_of(folder, cached_manifest(folder), name) if folder.is_dir() else \
            Parts(problems=("its folder is missing: remove it and install it again",))
        prefix = normalize_name(name)
        return {"skills": [f"{prefix}:{skill}" for skill in parts.skills],
                "commands": [f"{prefix}:{command}" for command in parts.commands],
                "connectors": [server for server, settings in self.cfg.mcp_servers.items()
                               if isinstance(settings, dict) and settings.get("plugin") == name],
                "unused": [*parts.unused, *parts.problems]}

    def _update(self, name: str, record: "InstalledPlugin", market: Market | None,
                entry: Mapping[str, Any] | None) -> dict[str, JSON] | None:
        """What an installed plugin would update to: its marketplace's newer commit, or (for a plugin in the
        marketplace's own folders) its changed content; None when it is current."""
        if market is None or entry is None:
            return None
        version = str(entry.get("version") or "")
        if name in market.digests:
            current = market.digests[name]
            return None if current == record["sha"] else {"version": version, "sha": current}
        source = plugin_source(entry.get("source"), market=GitRef(market.url, sha=market.sha),
                               plugin_root=market.plugin_root)
        if isinstance(source, GitRef) and source.sha and source.sha != record["sha"]:
            return {"version": version, "sha": source.sha}
        return None

    async def install(self, marketplace: str, name: str, *, source: str = "hud", actor: str = WHO,
                      connectors_on: bool = True) -> dict[str, JSON]:
        """Install a marketplace's plugin: fetched at its pinned commit, unpacked, its servers added as connectors.
        Nothing of it runs.

        Args:
            marketplace: the marketplace.
            name: the plugin.
            source: where the request came from (the activity log's source).
            actor: who acted (the activity log's).
            connectors_on: whether its connectors start switched on (the AI's installs start them off: only the user
                switches a connector on).

        Returns:
            The plugin (``view``).

        Raises:
            LookupError: no such marketplace or plugin.
            ValueError: it is installed already, can't be installed, or what came is unsafe.
            FetchError: it couldn't be fetched.
        """
        await asyncio.to_thread(self.load)
        async with self._lock:
            market, entry = self.listing(marketplace, name)
            if name in self.cfg.plugins.installed:
                raise ValueError(f"{name} is installed already: update it instead.")
            await self._place(name, marketplace, market=market, entry=entry, fresh_on=connectors_on)
        self._done("Installed", name, source, actor)
        return self.view(name)

    async def update(self, name: str, *, source: str = "hud", actor: str = WHO,
                     connectors_on: bool = True) -> dict[str, JSON]:
        """Update an installed plugin to its marketplace's current version (its marketplace fetched again first);
        its switches, its connectors' fields and secrets stay.

        Args:
            name: the plugin.
            source: where the request came from.
            actor: who acted.
            connectors_on: whether a connector the update adds starts switched on.

        Returns:
            The plugin (``view``).

        Raises:
            LookupError: it isn't installed, or its marketplace no longer lists it.
            ValueError: it was uploaded (upload it again), or the update can't be installed.
            FetchError: it couldn't be fetched.
        """
        await asyncio.to_thread(self.load)
        async with self._lock:
            record = self.cfg.plugins.installed.get(name)
            if record is None:
                raise LookupError(f"There's no plugin called {name}.")
            if record["source"] == UPLOAD:
                raise ValueError(f"{name} was uploaded: upload its new version to update it.")
            url = self.cfg.plugins.marketplaces.get(record["source"])
            if url is None:
                raise LookupError(f"{name}'s marketplace ({record['source']}) is no longer added.")
            await self._refresh_market(record["source"], url)
            market, entry = self.listing(record["source"], name)
            before = self._components(name)
            await self._place(name, record["source"], market=market, entry=entry, enabled=record["enabled"],
                              fresh_on=connectors_on)
            after = self._components(name)
        changes = {kind: ", ".join([*(f"+{item}" for item in after[kind] if item not in before[kind]),
                                    *(f"-{item}" for item in before[kind] if item not in after[kind])]) or "same"
                   for kind in ("skills", "commands", "connectors")}
        self._done("Updated", name, source, actor, detail=changes)
        return self.view(name)

    async def upload(self, data: bytes, filename: str, *, source: str = "hud", actor: str = WHO) -> dict[str, JSON]:
        """Install an uploaded .zip or .plugin: it must hold exactly one ``.claude-plugin/plugin.json``, naming it.

        Args:
            data: the file.
            filename: its name (for the log).
            source: where the request came from.
            actor: who acted.

        Returns:
            The plugin (``view``).

        Raises:
            ValueError: too big, not a zip, unsafe, not exactly one plugin, a bad name, or a plugin of that name from
                a marketplace is installed.
        """
        limits = Limits.of(self.cfg)
        if len(data) > limits.download:
            raise ValueError(f"{filename} is bigger than {limits.download >> 20} MB.")
        async with self._lock:
            scratch = self.root / f".upload-{secrets.token_hex(4)}"
            try:
                await asyncio.to_thread(unpack_zip, data, scratch, limits)
                folder, name = await asyncio.to_thread(_plugin_in, scratch, filename)
                record = self.cfg.plugins.installed.get(name)
                if record is not None and record["source"] != UPLOAD:
                    raise ValueError(f"{name} is installed from {record['source']}: remove it first.")
                await self._place(name, UPLOAD, folder=folder, enabled=record is None or record["enabled"])
            finally:
                await asyncio.to_thread(shutil.rmtree, scratch, True)
        self._done("Uploaded", name, source, actor, detail={"file": filename})
        return self.view(name)

    async def set_enabled(self, name: str, enabled: bool, *, source: str = "hud",
                          actor: str = WHO) -> dict[str, JSON]:
        """Switch a plugin on or off: its skills and commands, and its connectors (switching it on switches them all
        on).

        Raises:
            LookupError: it isn't installed.
        """
        async with self._lock:
            record = self.cfg.plugins.installed.get(name)
            if record is None:
                raise LookupError(f"There's no plugin called {name}.")
            servers = {server: _switched(settings, enabled) if isinstance(settings, dict)
                       and settings.get("plugin") == name else settings
                       for server, settings in self.cfg.mcp_servers.items()}
            installed = {**self.cfg.plugins.installed, name: {**record, "enabled": enabled}}
            await self.host.apply_own({"plugins.installed": installed, "mcp_servers": servers})
        self._changed(name, "enabled" if enabled else "disabled")
        self.host.activity.record("plugin", source=source, actor=actor, outcome="ok",
                                  title=f"Switched the {name} plugin {'on' if enabled else 'off'}")
        return self.view(name)

    async def remove(self, name: str, *, source: str = "hud", actor: str = WHO) -> None:
        """Remove a plugin: its folder, its connectors and their secrets, and its skills' switches.

        Raises:
            LookupError: it isn't installed.
        """
        async with self._lock:
            if name not in self.cfg.plugins.installed:
                raise LookupError(f"There's no plugin called {name}.")
            gone = [server for server, settings in self.cfg.mcp_servers.items()
                    if isinstance(settings, dict) and settings.get("plugin") == name]
            prefix = f"{normalize_name(name)}:"
            await self.host.apply_own({
                "plugins.installed": {k: v for k, v in self.cfg.plugins.installed.items() if k != name},
                "mcp_servers": {k: v for k, v in self.cfg.mcp_servers.items() if k not in gone},
                "skills.disabled": [n for n in self.cfg.skills.disabled if not skill_key(n).startswith(prefix)]})
            await asyncio.to_thread(shutil.rmtree, self.folder(name), True)
            await asyncio.to_thread(self._forget_secrets, gone)
        self._changed(name, "removed")
        self.host.activity.record("plugin", source=source, actor=actor, title=f"Removed the {name} plugin",
                                  outcome="ok", detail={"connectors": ", ".join(gone) or "none"})

    def _forget_secrets(self, connectors: Iterable[str]) -> None:
        """Delete the vault's entries of these connectors (a worker thread's job)."""
        vault = self.host.vault
        prefixes = tuple(f"connector.{connector}." for connector in connectors)
        for secret in vault.names():
            if prefixes and secret.startswith(prefixes):
                vault.delete(secret)

    async def _place(self, name: str, source: str, *, market: Market | None = None,
                     entry: Mapping[str, Any] | None = None, folder: Path | None = None, enabled: bool = True,
                     fresh_on: bool = True) -> None:
        """Fetch (or take) a plugin's files, put them in its folder, and record it with its connectors; on any
        failure nothing changes. Called with the lock held.

        Args:
            name: the plugin.
            source: its marketplace, or ``UPLOAD``.
            market: the marketplace, with ``entry``, the plugin's entry there: it is fetched from where that says.
            entry: see ``market``.
            folder: else, an upload's unpacked plugin.
            enabled: whether it is switched on.
            fresh_on: whether a connector that is new to it starts switched on.

        Raises:
            ValueError, FetchError: see ``install``.
        """
        if not PLUGIN.fullmatch(name) or name == CACHE:
            raise ValueError(f"{name!r} isn't a plugin's name: lower-case letters, digits and hyphens.")
        limits = Limits.of(self.cfg)
        staged = self.root / f".install-{name}-{secrets.token_hex(4)}"
        try:
            if market is not None and entry is not None:
                ref = plugin_source(entry.get("source"), market=GitRef(market.url, sha=market.sha),
                                    plugin_root=market.plugin_root)
                if isinstance(ref, str):
                    raise ValueError(f"{name} can't be installed: {ref}.")
                sha = await fetch_tree(ref, staged, limits, cache=self.archives)
                if ref.url == market.url:  # a folder of the marketplace itself: pinned by what is in it
                    sha = await asyncio.to_thread(digest, staged)
            elif folder is not None:
                await asyncio.to_thread(_copy_limited, folder, staged, limits)
                sha = await asyncio.to_thread(digest, staged)
            else:  # pragma: no cover (every caller gives one or the other)
                raise ValueError(f"{name} has nowhere to come from")
            manifest = manifest_of(staged, entry)  # the files as they came; the manifest below is written after
            manifest["name"] = name
            await asyncio.to_thread(write_atomic, staged / MANIFEST, json.dumps(manifest, indent=1) + "\n")
            parts = await asyncio.to_thread(parts_of, staged, manifest, name)
            servers = self._servers(name, parts, enabled, fresh_on)
            record: dict[str, Any] = {"source": source, "version": str(manifest.get("version") or "")[:100],
                                      "sha": sha, "enabled": enabled, "installed_at": time.time()}
            previous = self.cfg.plugins.installed.get(name)
            if previous is not None:
                record["installed_at"] = previous["installed_at"]
            await asyncio.to_thread(replace_dir, staged, self.folder(name))
            gone = [server for server, settings in self.cfg.mcp_servers.items()
                    if isinstance(settings, dict) and settings.get("plugin") == name and server not in servers]
            kept = {k: v for k, v in self.cfg.mcp_servers.items() if k not in gone}
            try:
                await self.host.apply_own({"plugins.installed": {**self.cfg.plugins.installed, name: record},
                                           "mcp_servers": {**kept, **servers}})
            except Exception:
                if previous is None:
                    await asyncio.to_thread(shutil.rmtree, self.folder(name), True)
                raise
            await asyncio.to_thread(self._forget_secrets, gone)
        finally:
            await asyncio.to_thread(shutil.rmtree, staged, True)

    def _servers(self, name: str, parts: Parts, enabled: bool, fresh_on: bool) -> dict[str, dict[str, Any]]:
        """A plugin's servers as ``mcp_servers`` entries: new ones with their placeholders, existing ones (an update)
        keeping the user's own settings (switches, fields, sandbox, gestures).

        Raises:
            ValueError: a server's id is taken twice over.
        """
        servers = self.cfg.mcp_servers
        mine = {settings.get("source_server"): server for server, settings in servers.items()
                if isinstance(settings, dict) and settings.get("plugin") == name}
        out: dict[str, dict[str, Any]] = {}

        def taken(candidate: str) -> bool:
            """Whether an id is someone else's: Orion's own, the directory's, another server's."""
            return candidate in out or self.host.reserved(candidate) or (
                candidate in servers and servers[candidate].get("plugin") != name)

        for spec in parts.servers:
            existing = mine.get(spec["name"])
            sid = existing or connector_id(spec["name"], name, taken)
            settings: dict[str, Any] = {**spec["settings"], "plugin": name, "source_server": spec["name"],
                                        "description": f"from the {name} plugin"}
            if existing is not None:  # an update: what the user set stays
                old = servers[existing]
                settings |= {key: old[key] for key in KEPT_SETTINGS if key in old}
            if not enabled or (existing is None and not fresh_on):
                settings["enabled"] = False
            out[sid] = settings
        return out

    def _done(self, verb: str, name: str, source: str, actor: str, *,
              detail: Mapping[str, Any] | None = None) -> None:
        """Say a plugin was installed, updated or uploaded: the HUD, the skills, the activity log."""
        self._changed(name, verb.lower())
        parts = self._components(name)
        record = self.cfg.plugins.installed[name]
        self.host.activity.record(
            "plugin", source=source, actor=actor, title=f"{verb} the {name} plugin", outcome="ok",
            detail={"version": record["version"] or record["sha"][:12], "skills": len(parts["skills"]),
                    "commands": len(parts["commands"]), "connectors": ", ".join(map(str, parts["connectors"])) or "none",
                    "not used": ", ".join(map(str, parts["unused"])) or "nothing", **(detail or {})})

    def _changed(self, name: str, action: str) -> None:
        """Tell the skills store and the HUD a plugin changed."""
        self.host.skills.invalidate()
        self.host.bus.publish("plugins", items=self.plugins())
        self.host.bus.publish("skill_changed", name=name, action=f"plugin {action}")

    # ------------------------------------------------------------------------------------------------ skills
    def discover_skills(self, query: str = "") -> list[dict[str, JSON]]:
        """The skill sources' skills, for the HUD's Discover tab.

        Args:
            query: words that must all appear in a skill's name or description.

        Returns:
            ``[{name, description, source, url, installed}]``, a name match first, then by name.
        """
        self.load()
        words = query.lower().split()
        have = self.host.skills.everything()
        found: list[tuple[tuple[int, str], dict[str, JSON]]] = []
        for url in self.cfg.skills.sources:
            source = self._sources.get(url)
            if source is None:
                continue
            ref = GitRef(url, sha=source.sha)
            short = "/".join(ref.github) if ref.github else url
            for skill in source.skills:
                text = f"{skill['name']} {skill['description']}".lower()
                if not all(word in text for word in words):
                    continue
                rank = 0 if words and all(word in skill["name"] for word in words) else 1
                found.append(((rank, skill["name"]), {
                    "name": skill["name"], "description": skill["description"], "source": short,
                    "url": dataclasses.replace(ref, path=skill["path"]).web, "installed": skill["name"] in have}))
        return [item for _, item in sorted(found, key=lambda pair: pair[0])]

    async def import_skills(self, *, url: str = "", data: bytes = b"", filename: str = "",
                            source: str = "hud") -> list[Skill]:
        """Import skills: from a repository or a folder of one (a GitHub "tree" URL), or from a .zip; every folder
        with a SKILL.md in it becomes a skill of yours (its other files, its LICENSE among them, with it).

        Args:
            url: the repository or folder.
            data: a .zip (when there is no URL).
            filename: the zip's name.
            source: who asked.

        Returns:
            The skills added.

        Raises:
            ValueError: no skill in it, an unsafe or too big archive, or a skill that clashes with one of yours.
            FetchError: it couldn't be fetched.
        """
        limits = Limits.of(self.cfg)
        scratch = self.cache / f".import-{secrets.token_hex(4)}"
        try:
            if url:
                ref = _skill_ref(url)
                await fetch_tree(ref, scratch, limits, cache=self.archives)
                origin = ref.web
            else:
                if len(data) > limits.download:
                    raise ValueError(f"{filename or 'The file'} is bigger than {limits.download >> 20} MB.")
                await asyncio.to_thread(unpack_zip, data, scratch, limits)
                origin = filename or "an uploaded zip"
            found = await asyncio.to_thread(_skill_folders, scratch)
            for skill in found.values():  # every clash said before anything is added
                current = self.host.skills.entry(skill.name)
                if current is not None and not current.origin:
                    raise ValueError(f"You already have a skill called {skill.name}: rename or delete it first.")
            added = []
            for folder in found:
                where = folder.relative_to(scratch).as_posix()
                skill_origin = origin if len(found) == 1 or where == "." else f"{origin}/{where}"
                added.append(await asyncio.to_thread(self.host.skills.adopt, folder, origin=skill_origin))
        finally:
            await asyncio.to_thread(shutil.rmtree, scratch, True)
        for skill in added:
            self.host.bus.publish("skill_changed", name=skill.name, action="imported")
            self.host.activity.record("skill", source=source, title=f"Imported the {skill.name} skill", outcome="ok",
                                      detail={"from": skill.origin})
        return added


def _switched(settings: Mapping[str, Any], enabled: bool) -> dict[str, Any]:
    """A server's settings switched on or off."""
    out = {key: value for key, value in settings.items() if key != "enabled"}
    if not enabled:
        out["enabled"] = False
    return out


def _skill_ref(url: str) -> GitRef:
    """What a skill import's URL names: a GitHub folder ("…/tree/<ref>/<path>"), or a repository.

    Raises:
        ValueError: it is neither.
    """
    url = url.strip()
    if (tree := _GITHUB_TREE.fullmatch(url)) is not None:
        owner, repo, at, path = (unquote(part or "") for part in tree.groups())  # %2F.. is .. all the same
        parts = _safe_parts(path)
        if parts is None:
            raise ValueError(f"{url} names a folder outside its repository.")
        return GitRef(f"https://github.com/{owner}/{repo}", sha=at if SHA.fullmatch(at) else "",
                      ref="" if SHA.fullmatch(at) else at, path="/".join(parts))
    return GitRef(repo_url(url))


def _skills_in(folder: Path) -> list[dict[str, str]]:
    """The skills of a fetched repository: every SKILL.md that parses, with its folder (a worker thread's job)."""
    return [{"name": skill.name, "description": skill.description,
             "path": "" if (where := path.relative_to(folder).as_posix()) == "." else where}
            for path, skill in _skill_folders(folder).items()]


def _skill_folders(folder: Path) -> dict[Path, Skill]:
    """The skill folders under a folder: each SKILL.md that parses with a description, the first of each name.

    Raises:
        ValueError: there is none (a worker thread's job).
    """
    found: dict[Path, Skill] = {}
    names: set[str] = set()
    for path in sorted(folder.rglob(SKILL_FILE)):
        skill = parse_skill(path) if path.is_file() and not path.is_symlink() else None
        if skill is not None and skill.description and skill.name not in names:
            names.add(skill.name)
            found[path.parent] = skill
    if not found:
        raise ValueError("There's no skill in it: a skill is a folder with a SKILL.md (a name and a description).")
    return found


def _plugin_in(folder: Path, filename: str) -> tuple[Path, str]:
    """An upload's plugin: the one folder holding ``.claude-plugin/plugin.json``, and the name it gives.

    Raises:
        ValueError: none, more than one, or a manifest without a name.
    """
    manifests = [path for path in folder.rglob("plugin.json") if path.parent.name == ".claude-plugin"]
    if len(manifests) != 1:
        raise ValueError(f"{filename} must hold exactly one .claude-plugin/plugin.json (it holds {len(manifests)}).")
    root = manifests[0].parent.parent
    name = str(manifest_of(root).get("name") or "")
    if not name:
        raise ValueError(f"{filename}'s plugin.json gives no name.")
    return root, name
