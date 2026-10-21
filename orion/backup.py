"""Backups: his memory and settings in an archive you can restore, and optionally an encrypted copy off this machine.

What a backup holds is what cannot be made again: the database (conversations, reminders, to-dos, jobs, what he
learned and when), the memory folder, the skills, the installed plugins and config.yaml. The database is copied while
he runs, with SQLite's online backup, so it is one consistent moment. A backup never holds the vault of secrets (a key
inside a backup is a key everywhere the backup goes: after a restore you enter them again; config.yaml holds only
references to them), the devices paired with the HUD, the models, the caches, the logs, or the index of your documents
(it is made again from your files).

An archive is ``orion-backup-YYYYmmdd-HHMMSS.tar.gz`` in ``backup.folder`` (readable by you alone). Its first member
is ``manifest.json``: the layout version, Orion's version, when, the database's schema and every file with its size
and sha256, so a reader can refuse an archive from a newer Orion before it reads anything else, and can check every
byte it extracts. A backup is made in two passes over the files: the first measures them, the second writes them and
checks each against the first; a file that changed in between (a skill saved at that moment) makes the whole backup
start again, so what is archived is what the manifest says. The new archive is then read back and verified before it
gets its name, and only then are the oldest archives beyond ``backup.keep`` deleted: a backup that failed its own
check never costs an older one. Only files named like an archive are ever deleted.

With ``backup.restic_repository`` set, the same tree is also kept in a restic repository (the password is the secret
``backup.restic.password``, given to restic through a pipe, never in a command line, an environment or a file), which
restic encrypts and deduplicates; ``backup.restic_keep_*`` say which snapshots it keeps. A failed restic copy does
not undo the local archive, and the result says so.

``restore`` puts an archive back while Orion is stopped: the archive is verified as it is unpacked into a folder next
to the data (nothing in it is trusted: only plain files with safe names that the manifest lists are written), the
database is checked, then the current files move to ``data/before-restore-<time>/`` and the restored ones take their
place; any failure on the way puts everything back as it was.
"""

import asyncio
import contextlib
import dataclasses
import gzip
import hashlib
import io
import json
import logging
import os
import re
import shutil
import socket
import sqlite3
import stat
import subprocess
import tarfile
import threading
import time
import zlib
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, BinaryIO, Final, Literal

from filelock import FileLock, Timeout

from orion.base import JSON, OrionError, Publish, local_zone, lower_priority
from orion.config import BASE_NAME
from orion.host import profile

if TYPE_CHECKING:
    from orion.config import Config

log = logging.getLogger("orion.backup")

type Where = Literal["local", "restic"]
type Record = Callable[..., None]  # ``Activity.record``

FORMAT: Final = 1  # the layout of an archive; a reader refuses a newer one
PREFIX: Final = "orion-backup-"
SUFFIX: Final = ".tar.gz"
STAMP: Final = "%Y%m%d-%H%M%S"
NAME: Final = re.compile(r"^orion-backup-(\d{8}-\d{6})\.tar\.gz$")
MANIFEST: Final = "manifest.json"
DATABASE: Final = "orion.db"
SETTINGS: Final = "config.yaml"
FOLDERS: Final = ("memory", "skills", "plugins")  # inside the data folder, as inside an archive
TOP: Final = frozenset({DATABASE, SETTINGS, BASE_NAME, *FOLDERS})  # the only names an archive may hold at its top
EXCLUDED: Final = ("the vault of secrets", "the devices paired with the HUD", "the models", "the caches", "the logs",
                   "the index of your documents (it is made again from your files)")
CHUNK: Final = 1 << 20  # bytes read or written at a time
MAX_MANIFEST_BYTES: Final = 64 << 20
ATTEMPTS: Final = 3  # how often a backup starts again because a file changed while it was read
NICE: Final = 15  # how far a scheduled backup yields the processor
MARGIN_BYTES: Final = 16 << 20  # free space wanted beyond the files' own size
LOCK_FILE: Final = ".backup.lock"
WORK: Final = ".work"  # inside the backup folder: the database copy, while a backup is made
STAGE: Final = ".stage"  # inside the backup folder: the tree restic is given
RESTIC_HOST: Final = "orion"  # restic snapshots carry this host name, so a new machine continues the same line
RESTIC_TAG: Final = "orion"
RESTIC_LISTING_S: Final = 60  # the restic snapshots are asked for at most this often (the repository may be far away)
STOP_POLL_S: Final = 0.5  # how often a restic run looks whether Orion is stopping
STOP_WAIT_S: Final = 15  # how long stopping Orion waits for a backup to give up
MIN_GAP_S: Final = 300  # a backup the model asks for is refused while the last one is younger than this
_NO_REPOSITORY: Final = "Is there a repository at the following location?"
_LOGIN_IN_URL: Final = re.compile(r"(://[^/@\s:]+):[^/@\s]+@")
_FRACTION: Final = re.compile(r"(\.\d{6})\d+")  # restic times have nanoseconds; fromisoformat reads six digits


class BackupError(OrionError):
    """A backup, a restore or a restic run that did not work; the message says why, in words for the user."""


class _Changed(Exception):
    """A file changed while it was being backed up."""

    def __init__(self, arc: str) -> None:
        super().__init__(arc)
        self.arc = arc


class _NoRepository(BackupError):
    """restic found no repository at the address (yet)."""


# ---------------------------------------------------------------------------------------------------- shapes
@dataclass(frozen=True, slots=True)
class Item:
    """A backup you can restore.

    Attributes:
        name: an archive's file name, or a restic snapshot's short id.
        at: when it was made (epoch seconds).
        size: its size in bytes (0 when restic doesn't say).
        where: "local" (an archive in the backup folder) or "restic".
    """

    name: str
    at: float
    size: int
    where: Where

    def as_dict(self) -> dict[str, JSON]:
        """The backup as the HUD receives it."""
        return {"name": self.name, "at": self.at, "size": self.size, "where": self.where}


@dataclass(frozen=True, slots=True)
class Made:
    """What one backup made.

    Attributes:
        name: the archive's file name.
        size: its size in bytes.
        files: how many files it holds.
        seconds: how long it took.
        restic: "" when no restic repository is set, "ok", or why the restic copy failed.
    """

    name: str
    size: int
    files: int
    seconds: float
    restic: str


@dataclass(frozen=True, slots=True)
class Manifest:
    """An archive's list of what it holds (``manifest.json``).

    Attributes:
        format: the archive layout.
        orion: the version of Orion that made it.
        created: when (epoch seconds).
        host: the machine's name.
        data_dir: the data folder it came from.
        schema: the database's schema version (None: the archive holds no database).
        files: every file's path in the archive -> (size, sha256).
        skipped: what was left out and why ("memory/x.md (a link)").
    """

    format: int
    orion: str
    created: float
    host: str
    data_dir: str
    schema: int | None
    files: dict[str, tuple[int, str]]
    skipped: tuple[str, ...]

    @property
    def total(self) -> int:
        """The files' bytes together."""
        return sum(size for size, _ in self.files.values())


@dataclass(frozen=True, slots=True)
class Restored:
    """What a restore did.

    Attributes:
        manifest: what the backup held.
        kept: where the files it replaced went (None when nothing was replaced).
        warnings: things to know afterwards, in words.
    """

    manifest: Manifest
    kept: Path | None
    warnings: tuple[str, ...]


@dataclass(slots=True)
class _Entry:
    """A file to back up: its place in the archive, where it is now, and what the first pass saw of it."""

    arc: str
    path: Path
    size: int
    mtime: float
    executable: bool
    sha256: str = ""


# ---------------------------------------------------------------------------------------------------- the folder
def backup_folder(cfg: "Config") -> Path:
    """Where the archives go: ``backup.folder``, a relative one counted from the data folder."""
    folder = Path(cfg.backup.folder).expanduser()
    return folder if folder.is_absolute() else cfg.data_path / folder


def archive_name(moment: float) -> str:
    """The file name of an archive made at a moment (local time)."""
    return f"{PREFIX}{time.strftime(STAMP, time.localtime(moment))}{SUFFIX}"


def archives(folder: Path) -> list[Item]:
    """The archives in a folder, newest first (only files named like one: nothing else is ever listed or deleted).

    Args:
        folder: the backup folder.

    Returns:
        Their names, times and sizes; empty when the folder is missing.

    Raises:
        BackupError: the folder can't be read.
    """
    try:
        entries = list(os.scandir(folder))
    except FileNotFoundError:
        return []
    except OSError as e:
        raise BackupError(f"The backup folder {folder} can't be read ({e.strerror}).") from None
    found: list[Item] = []
    for entry in entries:
        match = NAME.match(entry.name)
        if match is None or not entry.is_file(follow_symlinks=False):
            continue
        moment = datetime.strptime(match.group(1), STAMP).replace(tzinfo=local_zone()).timestamp()
        found.append(Item(entry.name, moment, entry.stat(follow_symlinks=False).st_size, "local"))
    return sorted(found, key=lambda item: item.name, reverse=True)


def prune(folder: Path, keep: int) -> list[str]:
    """Delete the oldest archives beyond the newest ``keep``.

    Args:
        folder: the backup folder.
        keep: how many to keep.

    Returns:
        The names deleted.
    """
    gone = [item.name for item in archives(folder)[keep:]]
    for name in gone:
        (folder / name).unlink(missing_ok=True)
    return gone


def latest(cfg: "Config") -> Path:
    """The newest archive in the backup folder.

    Raises:
        BackupError: there is none.
    """
    folder = backup_folder(cfg)
    found = archives(folder)
    if not found:
        raise BackupError(f"There are no backups in {folder}.")
    return folder / found[0].name


# ---------------------------------------------------------------------------------------------------- making one
def create(cfg: "Config", config_path: Path, *, name: str, restic: "Restic | None" = None,
           background: bool = False, stop: threading.Event | None = None) -> Made:
    """Make a backup now (blocking: run it in a thread). Only one runs at a time, in any process.

    Args:
        cfg: the settings (where the data is, ``backup.*``).
        config_path: the settings file, which may live outside the data folder.
        name: the archive's file name (``archive_name``); a second in which one was made already moves it on.
        restic: the restic repository to keep a copy in as well (None: only the archive).
        background: lower this thread's priority, so a scheduled backup never slows the voice (call it on a thread of
            its own).
        stop: set when Orion is stopping: the backup gives up at its next step.

    Returns:
        What was made.

    Raises:
        BackupError: it could not be made; nothing is left behind and no older archive was deleted.
    """
    if background:
        lower_priority(NICE)
    started = time.monotonic()
    folder = backup_folder(cfg)
    for top in (cfg.vault_path, cfg.skills_path, cfg.data_path / "plugins"):
        if folder.resolve().is_relative_to(top.resolve()):
            raise BackupError(f"The backup folder {folder} is inside {top.name}, which is itself backed up: choose "
                              "another folder in Settings.")
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        guard = FileLock(folder / LOCK_FILE, mode=0o600).acquire(timeout=0)
    except Timeout:
        raise BackupError("Another backup is running; wait for it to finish.") from None
    with guard:
        last = ""
        try:
            for attempt in range(1, ATTEMPTS + 1):
                _clear(folder)
                try:
                    final, files, note = _make(cfg, config_path, folder, name, restic, stop)
                except _Changed as e:
                    last = e.arc
                    log.info("%s changed while it was backed up (attempt %d of %d)", e.arc, attempt, ATTEMPTS)
                    continue
                prune(folder, cfg.backup.keep)
                return Made(final, (folder / final).stat().st_size, files, time.monotonic() - started, note)
            raise BackupError(f"{last} kept changing while it was backed up; try again in a moment.")
        finally:
            _clear(folder)


def _clear(folder: Path) -> None:
    """Remove what an earlier backup left (only while holding the folder's lock: nobody else's work is in it)."""
    for leftover in (*folder.glob("*.part"), folder / WORK, folder / STAGE):
        if leftover.is_dir():
            shutil.rmtree(leftover, ignore_errors=True)
        else:
            leftover.unlink(missing_ok=True)


def _halt(stop: threading.Event | None) -> None:
    """Give up when Orion is stopping."""
    if stop is not None and stop.is_set():
        raise BackupError("The backup was given up because Orion is stopping.")


def _make(cfg: "Config", config_path: Path, folder: Path, name: str, restic: "Restic | None",
          stop: threading.Event | None) -> tuple[str, int, str]:
    """One try at a backup: the database copy, the lists, the archive (verified), and the restic copy.

    Returns:
        The archive's final name, how many files it holds, and the restic note ("" none, "ok", or why not).
    """
    work = folder / WORK
    work.mkdir(mode=0o700)
    entries, skipped = _collect(cfg, config_path)
    schema: int | None = None
    database = cfg.data_path / DATABASE
    if database.is_file():
        schema = _copy_database(database, work / DATABASE)
        status = (work / DATABASE).stat()
        entries.append(_Entry(DATABASE, work / DATABASE, status.st_size, status.st_mtime, False))
    _need_space(folder, sum(entry.size for entry in entries) * (2 if restic is not None else 1) + MARGIN_BYTES)
    for entry in entries:
        _halt(stop)
        entry.sha256 = _hash(entry)
    created = time.time()
    manifest = _manifest(cfg, entries, skipped, schema, created)
    final = _free_name(folder, name)
    part = folder / (final + ".part")
    _write(part, manifest, entries, created, cfg.backup.compress_level, stop)
    _halt(stop)
    verify(part)
    os.rename(part, folder / final)
    _sync_folder(folder)
    note = _copy_to_restic(cfg, restic, folder / STAGE, manifest, entries, stop) if restic is not None else ""
    return final, len(entries), note


def _collect(cfg: "Config", config_path: Path) -> tuple[list[_Entry], list[str]]:
    """The files to back up, in a fixed order, and what was left out (links and other things that are not files)."""
    entries: list[_Entry] = []
    skipped: list[str] = []
    for arc, path in ((SETTINGS, config_path), (BASE_NAME, config_path.with_name(BASE_NAME))):
        if path.is_file() and not path.is_symlink():
            _add(entries, arc, path)
    trees = {"memory": cfg.vault_path, "skills": cfg.skills_path, "plugins": cfg.data_path / "plugins"}
    for arc, top in trees.items():
        if top.is_dir():
            _walk(arc, top, entries, skipped)
    return entries, skipped


def _add(entries: list[_Entry], arc: str, path: Path) -> None:
    """List one regular file."""
    try:
        status = path.lstat()
    except FileNotFoundError:
        return  # gone since it was listed
    entries.append(_Entry(arc, path, status.st_size, status.st_mtime, bool(status.st_mode & stat.S_IXUSR)))


def _walk(arc: str, top: Path, entries: list[_Entry], skipped: list[str]) -> None:
    """List the regular files under a folder; links and other things are left out and said so. A lock file and the
    temporary file an atomic write is in the middle of are not part of the data."""
    def fail(error: OSError) -> None:
        raise BackupError(f"{error.filename} can't be read ({error.strerror}).")

    for root, dirs, files in os.walk(top, followlinks=False, onerror=fail):
        here = Path(root)
        prefix = PurePosixPath(arc) / here.relative_to(top).as_posix()
        for folder in [d for d in dirs if (here / d).is_symlink()]:
            skipped.append(f"{_shown(prefix / folder)} (a link)")
            dirs.remove(folder)
        dirs.sort()
        for file in sorted(files):
            if file == ".lock" or (file.startswith(".") and file.endswith(".tmp")):
                continue
            path = here / file
            try:
                mode = path.lstat().st_mode
            except FileNotFoundError:
                continue
            if _shown(prefix / file) != str(prefix / file):
                skipped.append(f"{_shown(prefix / file)} (a name that isn't text)")
            elif not stat.S_ISREG(mode):
                skipped.append(f"{prefix / file} ({'a link' if stat.S_ISLNK(mode) else 'not a plain file'})")
            else:
                _add(entries, str(prefix / file), path)


def _shown(path: PurePosixPath) -> str:
    """A path as text that can be written down: a byte in a name that is not text shows as its escape."""
    return str(path).encode("utf-8", "backslashreplace").decode("utf-8")


def _copy_database(source: Path, target: Path) -> int:
    """Copy the live database with SQLite's online backup (one consistent moment while he writes), make the copy a
    single self-contained file that claims no owner of the memory index, and check it.

    Returns:
        The copy's schema version.

    Raises:
        BackupError: the database could not be copied, or it is damaged.
    """
    from orion.memory import LEASE

    try:
        with contextlib.closing(sqlite3.connect(source, timeout=30)) as live, \
                contextlib.closing(sqlite3.connect(target)) as copy:
            live.execute("PRAGMA query_only = 1")
            live.backup(copy)
            copy.execute("PRAGMA journal_mode = DELETE")
            copy.execute("UPDATE settings SET value = '' WHERE key = ?", (LEASE,))  # a restored file has no owner yet
            copy.commit()
            verdict = copy.execute("PRAGMA quick_check").fetchone()[0]
            version = int(copy.execute("PRAGMA user_version").fetchone()[0])
    except sqlite3.Error as e:
        raise BackupError(f"The database couldn't be copied: {e}.") from None
    if verdict != "ok":
        raise BackupError(f"The database is damaged ({verdict}); nothing was backed up. Restore an earlier backup, "
                          "or move orion.db away and let him start afresh.")
    return version


def _need_space(folder: Path, need: int) -> None:
    """Refuse early when the folder's disk is too full."""
    free = shutil.disk_usage(folder).free
    if free < need:
        raise BackupError(f"There isn't enough room in {folder}: about {need >> 20} MB are needed and {free >> 20} MB "
                          "are free.")


def _hash(entry: _Entry) -> str:
    """The sha256 of a file's first ``entry.size`` bytes (what the archive will hold)."""
    digest = hashlib.sha256()
    left = entry.size
    try:
        with open(entry.path, "rb") as f:
            while left > 0 and (chunk := f.read(min(CHUNK, left))):
                digest.update(chunk)
                left -= len(chunk)
    except FileNotFoundError:
        raise _Changed(entry.arc) from None
    except OSError as e:
        raise BackupError(f"{entry.path} can't be read ({e.strerror}).") from None
    if left:
        raise _Changed(entry.arc)  # shorter than it was listed
    return digest.hexdigest()


def _manifest(cfg: "Config", entries: Sequence[_Entry], skipped: Sequence[str], schema: int | None,
              created: float) -> bytes:
    """The text of ``manifest.json``."""
    from orion import __version__

    document: dict[str, JSON] = {
        "format": FORMAT, "orion": __version__, "created": created,
        "created_text": datetime.fromtimestamp(created, local_zone()).isoformat(timespec="seconds"),
        "host": socket.gethostname(), "data_dir": str(cfg.data_path), "schema": schema,
        "files": [{"path": e.arc, "size": e.size, "sha256": e.sha256} for e in entries],
        "skipped": list(skipped), "excluded": list(EXCLUDED),
    }
    return (json.dumps(document, indent=1, ensure_ascii=False) + "\n").encode()


class _Reader:
    """A file as it is read for the archive or a copy: hashed on the way, and compared with the first pass when its
    last byte is read (``_Changed`` when it differs, or ends early)."""

    def __init__(self, entry: _Entry, file: BinaryIO) -> None:
        self._entry = entry
        self._file = file
        self._digest = hashlib.sha256()
        self._seen = 0

    def read(self, size: int) -> bytes:
        """The next ``size`` bytes (fewer at the end of the file as it was listed)."""
        want = min(size, self._entry.size - self._seen)
        data = self._file.read(want)
        if len(data) < want:
            raise _Changed(self._entry.arc)
        self._digest.update(data)
        self._seen += len(data)
        if data and self._seen == self._entry.size and self._digest.hexdigest() != self._entry.sha256:
            raise _Changed(self._entry.arc)
        return data


def _write(part: Path, manifest: bytes, entries: Sequence[_Entry], created: float, level: int,
           stop: threading.Event | None) -> None:
    """Write the archive: the manifest first, then every file, each checked against the first pass."""
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with open(fd, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=level, mtime=0) as packed, \
                tarfile.open(fileobj=packed, mode="w", format=tarfile.PAX_FORMAT, copybufsize=CHUNK) as tar:
            head = tarfile.TarInfo(MANIFEST)
            head.size, head.mtime, head.mode = len(manifest), created, 0o600
            tar.addfile(head, io.BytesIO(manifest))
            for entry in entries:
                _halt(stop)
                info = tarfile.TarInfo(entry.arc)
                info.size, info.mtime, info.mode = entry.size, entry.mtime, 0o700 if entry.executable else 0o600
                try:
                    with open(entry.path, "rb") as f:
                        tar.addfile(info, _Reader(entry, f))  # type: ignore[arg-type]  # it only calls read(size)
                except FileNotFoundError:
                    raise _Changed(entry.arc) from None
        raw.flush()
        os.fsync(raw.fileno())


def _free_name(folder: Path, name: str) -> str:
    """The archive name, moved on by a second at a time while a file of that name exists."""
    match = NAME.match(name)
    if match is None:
        raise ValueError(f"{name!r} isn't an archive name")
    moment = datetime.strptime(match.group(1), STAMP)
    while (folder / name).exists():
        moment = datetime.fromtimestamp(moment.timestamp() + 1)
        name = f"{PREFIX}{moment.strftime(STAMP)}{SUFFIX}"
    return name


def _sync_folder(folder: Path) -> None:
    """Make a new file's name durable (a file system that can't is no reason to fail a backup that is done)."""
    with contextlib.suppress(OSError):
        fd = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


# ---------------------------------------------------------------------------------------------------- reading one
def verify(archive: Path, *, extract_to: Path | None = None) -> Manifest:
    """Read an archive from beginning to end and check it: the manifest first, then every file's name, size and
    sha256 against it. Nothing in the archive is trusted: only plain files with safe names that the manifest lists are
    accepted, and with ``extract_to`` exactly those are written there.

    Args:
        archive: the archive.
        extract_to: an empty folder to unpack into while checking (None: only check).

    Returns:
        Its manifest.

    Raises:
        BackupError: it is not a complete, undamaged backup of Orion's, or it is from a newer Orion.
    """
    try:
        with tarfile.open(archive, "r|gz") as tar:
            return _check(tar, extract_to)
    except FileNotFoundError:
        raise BackupError(f"There is no backup at {archive}.") from None
    except PermissionError:
        raise BackupError(f"{archive} can't be read (not allowed).") from None
    except (tarfile.TarError, EOFError, zlib.error, OSError) as e:
        raise BackupError(f"{archive.name} is damaged, or isn't a backup of Orion's ({e}).") from None


def _check(tar: tarfile.TarFile, extract_to: Path | None) -> Manifest:
    """``verify`` over an open archive stream."""
    first = tar.next()
    if first is None or first.name != MANIFEST or not first.isreg():
        raise BackupError("That archive has no list of its files, so it isn't a backup of Orion's.")
    if first.size > MAX_MANIFEST_BYTES:
        raise BackupError("That archive's list of files is too large to be one of Orion's.")
    reader = tar.extractfile(first)
    manifest = parse_manifest(reader.read() if reader is not None else b"")
    if extract_to is not None:
        _need_space(extract_to, manifest.total + MARGIN_BYTES)
    seen: set[str] = set()
    while (member := tar.next()) is not None:
        arc = _arc(member.name)
        if not member.isreg():
            raise BackupError(f"{arc} is not a plain file: an archive of Orion's holds nothing else.")
        if arc in seen or arc not in manifest.files:
            raise BackupError(f"{arc} is {'in that archive twice' if arc in seen else 'not in its list of files'}.")
        size, digest = manifest.files[arc]
        _unpack(tar, member, arc, size, digest, extract_to)
        seen.add(arc)
    if missing := sorted(manifest.files.keys() - seen):
        raise BackupError(f"That archive is incomplete: {missing[0]} is missing"
                          + (f" and {len(missing) - 1} more." if len(missing) > 1 else "."))
    return manifest


def _unpack(tar: tarfile.TarFile, member: tarfile.TarInfo, arc: str, size: int, digest: str,
            extract_to: Path | None) -> None:
    """Read one file of an archive, check it against the manifest, and write it under ``extract_to``."""
    if member.size != size:
        raise BackupError(f"{arc} has {member.size} bytes in the archive but {size} in its list: the archive is "
                          "damaged.")
    source = tar.extractfile(member)
    if source is None:
        raise BackupError(f"{arc} can't be read from the archive.")
    hasher = hashlib.sha256()
    destination = extract_to / arc if extract_to is not None else None
    left = size
    with contextlib.ExitStack() as stack:
        target: BinaryIO | None = None
        if destination is not None:
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            target = stack.enter_context(open(os.open(destination, flags, 0o600), "wb"))
        while left > 0 and (chunk := source.read(min(CHUNK, left))):
            hasher.update(chunk)
            left -= len(chunk)
            if target is not None:
                target.write(chunk)
    if left or hasher.hexdigest() != digest:
        raise BackupError(f"{arc} doesn't match its checksum: the archive is damaged.")
    if destination is not None:
        os.utime(destination, (member.mtime, member.mtime))
        if member.mode & stat.S_IXUSR:
            destination.chmod(0o700)


def _arc(name: str) -> str:
    """A path inside an archive, checked: relative, plain, below one of the places a backup has files.

    Raises:
        BackupError: it could point outside the folder it is unpacked to, or is not a place a backup has files.
    """
    path = PurePosixPath(name)
    plain = (str(path) == name and not path.is_absolute() and bool(path.parts) and "\0" not in name
             and all(part not in ("", ".", "..") for part in path.parts))
    if not plain or path.parts[0] not in TOP or (path.parts[0] not in FOLDERS and len(path.parts) != 1):
        raise BackupError(f"{name!r} is not a place where a backup of Orion's keeps files: the archive is not one "
                          "of his, or was tampered with.")
    return name


def parse_manifest(raw: bytes) -> Manifest:
    """Read ``manifest.json``.

    Raises:
        BackupError: it isn't one, or is from a newer Orion.
    """
    try:
        document = json.loads(raw)
        layout = document["format"]
        if not isinstance(layout, int) or isinstance(layout, bool):
            raise TypeError
    except (ValueError, KeyError, TypeError):
        raise BackupError("That archive's list of files can't be read, so it isn't a backup of Orion's.") from None
    if layout > FORMAT:
        raise BackupError(f"That backup was made by a newer Orion (layout {layout}; this one reads {FORMAT}): update "
                          "Orion first.")
    try:
        files: dict[str, tuple[int, str]] = {}
        for item in document["files"]:
            arc, size, digest = _arc(item["path"]), item["size"], item["sha256"]
            if (not isinstance(size, int) or isinstance(size, bool) or size < 0 or arc in files
                    or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
                raise TypeError
            files[arc] = (size, digest)
        schema = document.get("schema")
        return Manifest(layout, str(document["orion"]), float(document["created"]), str(document["host"]),
                        str(document["data_dir"]), schema if isinstance(schema, int) else None, files,
                        tuple(str(s) for s in document.get("skipped", ())))
    except (ValueError, KeyError, TypeError):
        raise BackupError("That archive's list of files is malformed, so it isn't a backup of Orion's.") from None


# ---------------------------------------------------------------------------------------------------- restic
class Restic:
    """The restic program, for one repository. Every run gets the password through a pipe (a path ``/dev/fd/N``):
    never in the command line, the environment or a file. restic gets Orion's environment otherwise, so the logins of
    its backends (``AWS_ACCESS_KEY_ID``, ``B2_ACCOUNT_KEY``, the ssh agent) work as they do in your shell.

    Args:
        binary: the program.
        repository: the repository's address.
        password: its password.
        timeout_s: the longest one run may take.
        stop: set when Orion is stopping: a run under way is ended.
    """

    def __init__(self, binary: str, repository: str, password: str, *, timeout_s: int,
                 stop: threading.Event | None = None) -> None:
        self._binary = binary
        self._repository = repository
        self._password = password
        self._timeout_s = timeout_s
        self._stop = stop

    def run(self, *args: str, cwd: Path | None = None) -> str:
        """Run restic with these arguments.

        Returns:
            What it printed.

        Raises:
            BackupError: it didn't start, took too long, was ended because Orion is stopping, or failed;
                ``_NoRepository`` for a repository that isn't there yet.
        """
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("RESTIC_PASSWORD", "RESTIC_REPOSITORY", "RESTIC_FROM"))}
        read, write = os.pipe()
        environment |= {"RESTIC_REPOSITORY": self._repository, "RESTIC_PASSWORD_FILE": f"/dev/fd/{read}"}
        try:
            process = subprocess.Popen([self._binary, *args], cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=(read,),
                                       start_new_session=True, encoding="utf-8", errors="replace")
        except OSError as e:
            os.close(write)
            raise BackupError(f"restic couldn't be started ({e.strerror}).") from None
        finally:
            os.close(read)
        with os.fdopen(write, "w", encoding="utf-8") as pipe, contextlib.suppress(BrokenPipeError):
            pipe.write(self._password)  # a pipe holds this much: restic reads it when it needs it
        deadline = time.monotonic() + self._timeout_s
        while True:
            try:
                out, err = process.communicate(timeout=min(STOP_POLL_S, max(0.0, deadline - time.monotonic())))
                break
            except subprocess.TimeoutExpired:
                late = time.monotonic() >= deadline
                if not late and not (self._stop is not None and self._stop.is_set()):
                    continue
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, 9)
                process.communicate()
                raise BackupError(f"restic didn't finish within {self._timeout_s} seconds." if late else
                                  "restic was ended because Orion is stopping.") from None
        if process.returncode:
            raise self._failure(process.returncode, err)
        return out

    @staticmethod
    def _failure(code: int, stderr: str) -> BackupError:
        """A restic failure in words (its exit codes name the common ones; a login in the address never shows)."""
        if code == 10 or _NO_REPOSITORY in stderr:
            return _NoRepository("There is no restic repository at the address in Settings yet.")
        if code == 12:
            return BackupError("restic says the password is wrong: set the secret backup.restic.password again.")
        if code == 11:
            return BackupError("The restic repository is locked by another run; try again when it has finished.")
        text = _LOGIN_IN_URL.sub(r"\1:***@", stderr.strip())
        return BackupError("restic failed: " + (" / ".join(text.splitlines()[-3:]) or f"exit code {code}") + ".")

    def ready(self) -> None:
        """Make sure the repository exists (a new one is created) and the password opens it."""
        try:
            self.run("cat", "config")
        except _NoRepository:
            self.run("init", "--quiet")

    def backup(self, tree: Path) -> None:
        """Keep a folder's contents as a new snapshot (unchanged files are recognised by their size and time)."""
        self.run("backup", "--quiet", "--host", RESTIC_HOST, "--tag", RESTIC_TAG, "--ignore-inode", "--ignore-ctime",
                 ".", cwd=tree)

    def forget(self, daily: int, weekly: int, monthly: int) -> None:
        """Drop the snapshots beyond a policy (all three 0 keeps every snapshot), and free their space."""
        policy = [arg for flag, count in (("--keep-daily", daily), ("--keep-weekly", weekly),
                                          ("--keep-monthly", monthly)) if count for arg in (flag, str(count))]
        if policy:
            self.run("forget", "--quiet", "--host", RESTIC_HOST, "--tag", RESTIC_TAG, *policy, "--prune")

    def snapshots(self) -> list[Item]:
        """Orion's snapshots, newest first."""
        try:
            listed = json.loads(self.run("snapshots", "--json", "--host", RESTIC_HOST, "--tag", RESTIC_TAG) or "[]")
            return sorted((Item(str(s["short_id"]), datetime.fromisoformat(_FRACTION.sub(r"\1", s["time"])).timestamp(),
                                int((s.get("summary") or {}).get("total_bytes_processed") or 0), "restic")
                           for s in listed), key=lambda item: item.at, reverse=True)
        except (ValueError, KeyError, TypeError):
            raise BackupError("restic's list of snapshots can't be read.") from None

    def restore(self, snapshot: str, target: Path) -> None:
        """Unpack a snapshot ("latest" or an id) into a folder."""
        self.run("restore", snapshot, "--quiet", "--host", RESTIC_HOST, "--tag", RESTIC_TAG, "--target", str(target))


def open_restic(cfg: "Config", password: str | None, stop: threading.Event | None = None) -> Restic:
    """The restic of the settings.

    Args:
        cfg: the settings (``backup.restic_*``).
        password: the repository's password from the vault (None or "" when it isn't set).
        stop: set when Orion is stopping.

    Raises:
        BackupError: no repository is set, restic isn't installed, or there is no password.
    """
    b = cfg.backup
    if not b.restic_repository:
        raise BackupError("No restic repository is set: give its address in Settings, Memory, Backups.")
    if not (binary := profile().restic):
        raise BackupError("restic isn't installed on this machine (sudo apt install restic).")
    if not password:
        raise BackupError("restic needs its password: set the secret backup.restic.password (Settings, or "
                          "orion secret set backup.restic.password).")
    return Restic(binary, b.restic_repository, password, timeout_s=b.restic_timeout_s, stop=stop)


def _copy_to_restic(cfg: "Config", restic: Restic, stage: Path, manifest: bytes, entries: Sequence[_Entry],
                    stop: threading.Event | None) -> str:
    """Keep the backup's tree in restic. Returns "ok", or why it failed (the local archive stands either way)."""
    try:
        stage.mkdir(mode=0o700)
        (stage / MANIFEST).write_bytes(manifest)
        for entry in entries:
            _halt(stop)
            destination = stage / entry.arc
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with open(entry.path, "rb") as source, open(destination, "wb") as target:
                reader = _Reader(entry, source)
                target.writelines(reader.read(CHUNK) for _ in range(0, entry.size, CHUNK))
            os.utime(destination, (entry.mtime, entry.mtime))
            destination.chmod(0o700 if entry.executable else 0o600)
        restic.ready()
        restic.backup(stage)
        b = cfg.backup
        restic.forget(b.restic_keep_daily, b.restic_keep_weekly, b.restic_keep_monthly)
    except _Changed as e:
        return f"{e.arc} changed while it was copied; the next backup will copy it"
    except BackupError as e:
        return str(e)
    except OSError as e:
        return f"{e.filename or 'a file'} can't be copied to restic ({e.strerror})"
    return "ok"


# ---------------------------------------------------------------------------------------------------- restoring
def holders(path: Path) -> list[tuple[int, str]]:
    """The processes of this user that have a file open (Orion, his tools, another app lent his tools, a database
    browser): found by reading what each process has open.

    Args:
        path: the file.

    Returns:
        (process id, its command) for each.
    """
    target = os.path.realpath(path)
    found: list[tuple[int, str]] = []
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        try:
            for fd in os.scandir(f"/proc/{entry.name}/fd"):
                if os.readlink(fd.path).removesuffix(" (deleted)") == target:
                    command = Path(f"/proc/{entry.name}/cmdline").read_bytes().replace(b"\0", b" ")
                    found.append((int(entry.name), command.decode(errors="replace").strip()[:100]))
                    break
        except OSError:  # gone since it was listed, or another user's
            continue
    return found


def restore(cfg: "Config", config_path: Path, archive: Path, *, now: float | None = None) -> Restored:
    """Put an archive's files back (see the module docstring). Orion must be stopped.

    Args:
        cfg: the settings (where the data lives).
        config_path: the settings file.
        archive: the archive.
        now: the time, for the name of the folder the replaced files go to.

    Returns:
        What was restored.

    Raises:
        BackupError: Orion is running, the archive is not a good backup, or the files could not be put back (then
            everything is as it was).
    """
    _stopped(cfg)
    now = now or time.time()
    stage = _scratch(cfg.data_path, ".restore-", now)
    try:
        manifest = verify(archive, extract_to=stage)
        return _install(cfg, config_path, stage, manifest, now)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def restore_from_restic(cfg: "Config", config_path: Path, restic: Restic, snapshot: str, *,
                        now: float | None = None) -> Restored:
    """``restore``, from a restic snapshot ("latest" or an id).

    Raises:
        BackupError: as ``restore``, or restic could not unpack the snapshot.
    """
    _stopped(cfg)
    now = now or time.time()
    stage = _scratch(cfg.data_path, ".restore-", now)
    try:
        restic.restore(snapshot, stage / "restic")
        roots = [path.parent for path in (stage / "restic").rglob(MANIFEST) if path.is_file()]
        if len(roots) != 1:
            raise BackupError("That restic snapshot isn't a backup of Orion's.")
        manifest = _check_tree(roots[0])
        return _install(cfg, config_path, roots[0], manifest, now)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _stopped(cfg: "Config") -> None:
    """Refuse while any process has the database open."""
    if running := holders(cfg.data_path / DATABASE):
        who = ", ".join(f"process {pid} ({command})" for pid, command in running[:3])
        raise BackupError(f"Orion is running ({who}): stop him first (orion stop), then restore.")


def _scratch(parent: Path, prefix: str, moment: float) -> Path:
    """A new empty folder in ``parent`` named for a moment (the same disk as the data, so files move into place at
    once)."""
    parent.mkdir(parents=True, exist_ok=True)
    base = prefix + time.strftime(STAMP, time.localtime(moment))
    for n in range(100):
        path = parent / (base if n == 0 else f"{base}-{n}")
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            continue
        return path
    raise BackupError(f"{parent} has too many leftover {prefix}* folders: delete them.")


def _check_tree(root: Path) -> Manifest:
    """``verify`` for a folder that holds the files of a backup (what restic unpacked)."""
    try:
        manifest = parse_manifest((root / MANIFEST).read_bytes())
    except OSError as e:
        raise BackupError(f"The backup's list of files can't be read ({e.strerror}).") from None
    found: set[str] = set()
    for here, dirs, files in os.walk(root, followlinks=False):
        for entry in (*dirs, *files):
            path = Path(here) / entry
            if path.is_symlink():
                raise BackupError(f"{path.relative_to(root)} is a link: the snapshot is not one of Orion's.")
        found |= {(Path(here) / file).relative_to(root).as_posix() for file in files}
    found.discard(MANIFEST)
    if found != manifest.files.keys():
        odd = min(found ^ manifest.files.keys())
        raise BackupError(f"{odd} is not as that backup's list says: the snapshot is damaged.")
    for arc, (size, digest) in manifest.files.items():
        path = root / arc
        if path.stat().st_size != size or _hash(_Entry(arc, path, size, 0.0, False)) != digest:
            raise BackupError(f"{arc} doesn't match its checksum: the snapshot is damaged.")
    return manifest


def _install(cfg: "Config", config_path: Path, root: Path, manifest: Manifest, now: float) -> Restored:
    """Check the database of an unpacked backup, then swap its files for the current ones."""
    if DATABASE in manifest.files:
        _check_schema(root / DATABASE)
    data = cfg.data_path
    places = {DATABASE: data / DATABASE, "memory": cfg.vault_path, "skills": cfg.skills_path,
              "plugins": data / "plugins", SETTINGS: config_path, BASE_NAME: config_path.with_name(BASE_NAME)}
    plan = [(root / top, destination) for top, destination in places.items() if (root / top).exists()]
    for _, destination in plan:
        if destination.is_symlink():
            raise BackupError(f"{destination} is a link: restore into the folder it points to by hand.")
    # a database's write-ahead log belongs to the file it was written beside: it goes away with it
    logs = [data / f"{DATABASE}-{suffix}" for suffix in ("wal", "shm")] if DATABASE in manifest.files else []
    kept = _swap(plan, [path for path in logs if path.exists()], data, now)
    warnings: list[str] = []
    if manifest.data_dir and Path(manifest.data_dir) != data:
        warnings.append(f"This backup came from {manifest.data_dir}; your data folder is {data}. If the restored "
                        "config.yaml now says data_dir somewhere else, set it back.")
    return Restored(manifest, kept, tuple(warnings))


def _check_schema(database: Path) -> None:
    """Refuse a database this Orion can't open: damaged, from a newer Orion, or too old to upgrade."""
    from orion.memory.store import CURRENT_VERSION, UPGRADES

    try:
        with contextlib.closing(sqlite3.connect(database)) as db:
            verdict = db.execute("PRAGMA quick_check").fetchone()[0]
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
    except sqlite3.Error as e:
        raise BackupError(f"The backup's database can't be opened ({e}).") from None
    if verdict != "ok":
        raise BackupError(f"The backup's database is damaged ({verdict}).")
    if version > CURRENT_VERSION:
        raise BackupError(f"The backup's database (schema {version}) was written by a newer Orion: update Orion first.")
    if version != CURRENT_VERSION and version not in UPGRADES:
        raise BackupError(f"The backup's database (schema {version}) is too old for this Orion to upgrade.")


def _swap(plan: Sequence[tuple[Path, Path]], extra: Sequence[Path], data: Path, now: float) -> Path | None:
    """Move the current files (and the database's write-ahead log) into a folder of their own and the new ones into
    their place; any failure puts everything back.

    Returns:
        The folder the current files went to (None when there was nothing to replace).
    """
    done: list[tuple[Path, Path]] = []  # (from, to), in the order they happened
    kept: Path | None = None

    def move(source: Path, destination: Path) -> None:
        shutil.move(source, destination)
        done.append((source, destination))

    try:
        for current in [*extra, *(destination for _, destination in plan)]:
            if current.exists() or current.is_symlink():
                if kept is None:
                    kept = _scratch(data, "before-restore-", now)
                move(current, kept / current.name)
        for new, destination in plan:
            destination.parent.mkdir(parents=True, exist_ok=True)
            move(new, destination)
    except BaseException:
        for source, destination in reversed(done):
            with contextlib.suppress(OSError):
                shutil.move(destination, source)
        if kept is not None:
            with contextlib.suppress(OSError):
                kept.rmdir()
        raise
    return kept


# ---------------------------------------------------------------------------------------------------- the service
class Backups:
    """The backups of the running Orion: the nightly one, the ones asked for, and the lists. Use it on the event loop.

    Args:
        cfg: the live settings (``backup.*``).
        config_path: the settings file.
        publish: ``EventBus.publish``: the HUD hears of each backup (``backup`` events: start, done, failed).
        record: ``Activity.record``: each backup is written to the activity log.
        password: the restic repository's password, from the vault (None or "" when it isn't set); called off the
            event loop.
    """

    def __init__(self, cfg: "Config", config_path: Path, *, publish: Publish, record: Record,
                 password: Callable[[], str | None]) -> None:
        self.cfg = cfg
        self._config_path = config_path
        self._publish = publish
        self._record = record
        self._password = password
        self._task: asyncio.Task[Made] | None = None
        self._name = ""
        self._stopping = threading.Event()
        self._attempted = 0.0  # when the last backup was tried in this run: a failed one waits for its next time
        self._snapshots: tuple[float, list[Item], str] | None = None  # when listed, the snapshots, why not

    @property
    def running(self) -> bool:
        """Whether a backup is being made."""
        return self._task is not None and not self._task.done()

    @property
    def name(self) -> str:
        """The archive being made now ("" when no backup is under way)."""
        return self._name if self.running else ""

    def last(self) -> float | None:
        """When the newest archive was made."""
        found = archives(backup_folder(self.cfg))
        return found[0].at if found else None

    def due(self, now: float) -> bool:
        """Whether the schedule calls for a backup: the time after the newest archive (or the last try) that the
        schedule names has passed. A backup missed while Orion was off is therefore made when he starts."""
        return (when := self.next_run()) is not None and when <= now

    def next_run(self) -> float | None:
        """When the schedule next calls for a backup: None when there is no schedule or a backup is under way, and 0
        when none was ever made."""
        from orion.jobs import next_run

        schedule = self.cfg.backup.schedule
        if not schedule or self.running:
            return None
        anchor = max(self.last() or 0.0, self._attempted)
        return next_run(schedule, anchor) if anchor else 0.0

    async def items(self) -> tuple[list[Item], str]:
        """Every backup, newest first (the archives, and restic's snapshots when restic is set up), and why the
        snapshots couldn't be listed ("" when they could)."""
        found = await asyncio.to_thread(archives, backup_folder(self.cfg))
        error = ""
        if self.cfg.backup.restic_repository and profile().has("restic"):
            snapshots, error = await self._snapshots_cached()
            found = sorted([*found, *snapshots], key=lambda item: item.at, reverse=True)
        return found, error

    async def listing(self) -> dict[str, JSON]:
        """What the HUD shows: the folder, the schedule, the policy, restic, and every backup, newest first."""
        b = self.cfg.backup
        items, error = await self.items()
        return {"folder": str(backup_folder(self.cfg)), "schedule": b.schedule, "keep": b.keep,
                "restic": {"configured": bool(b.restic_repository), "available": profile().has("restic"),
                           "error": error},
                "items": [item.as_dict() for item in items]}

    async def _snapshots_cached(self) -> tuple[list[Item], str]:
        """The restic snapshots, and why they couldn't be listed ("" when they could)."""
        cached = self._snapshots
        if cached is not None and time.monotonic() - cached[0] < RESTIC_LISTING_S:
            return cached[1], cached[2]

        def ask() -> list[Item]:
            return open_restic(self.cfg, self._password()).snapshots()

        try:
            found, error = await asyncio.to_thread(ask), ""
        except OrionError as e:
            found, error = [], str(e)
        self._snapshots = (time.monotonic(), found, error)
        return found, error

    def start(self, source: str) -> "asyncio.Task[Made]":
        """Begin a backup, or return the one under way. Never waits; the HUD and the activity log are told how it went.

        Args:
            source: who asked, as the activity log names it ("system" for the schedule, "hud", "voice", "user"...).

        Returns:
            The task; it raises ``BackupError`` when the backup fails (already announced).
        """
        if self._task is None or self._task.done():
            self._name = archive_name(time.time())
            self._task = asyncio.get_running_loop().create_task(self._run(source, self._name), name="backup")
            self._task.add_done_callback(lambda done: done.cancelled() or done.exception())  # retrieved: told already
        return self._task

    async def run(self, source: str) -> Made:
        """Make a backup now (``start``) and wait for it.

        Raises:
            BackupError: it failed.
        """
        return await asyncio.shield(self.start(source))

    async def stop(self) -> None:
        """Orion is stopping: a backup under way gives up at its next step (waited for a little)."""
        self._stopping.set()
        if self._task is not None and not self._task.done():
            await asyncio.wait({self._task}, timeout=STOP_WAIT_S)

    async def _run(self, source: str, name: str) -> Made:
        """The backup itself, with its announcements."""
        self._attempted = time.time()
        self._publish("backup", phase="start", name=name)

        def work() -> Made:
            restic, problem = None, ""
            if self.cfg.backup.restic_repository:
                try:
                    restic = open_restic(self.cfg, self._password(), self._stopping)
                except OrionError as e:  # a vault that won't answer costs the off-site copy, never the archive
                    problem = str(e)
            made = create(self.cfg, self._config_path, name=name, restic=restic, background=True,
                          stop=self._stopping)
            return dataclasses.replace(made, restic=problem) if problem else made

        try:
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="backup") as thread:  # its own: lower_priority
                made = await asyncio.get_running_loop().run_in_executor(thread, work)
        except Exception as e:
            why = str(e) if isinstance(e, OrionError) else f"{type(e).__name__}: {e}"
            if isinstance(e, OrionError):
                log.warning("backup failed: %s", why)
            else:
                log.exception("backup failed")
            self._publish("backup", phase="failed", name=name, error=why)
            self._record("backup", source=source, title="A backup failed", outcome="failed", detail=why)
            raise
        self._snapshots = None
        failed = made.restic not in ("", "ok")
        news: dict[str, JSON] = {"phase": "done", "name": made.name, "size": made.size}
        if failed:
            news["error"] = made.restic
        self._publish("backup", **news)
        detail = f"{made.name}: {made.files} files, {made.size >> 10} KB" + (f"; {made.restic}" if failed else "")
        self._record("backup", source=source, outcome="failed" if failed else "ok", ms=round(made.seconds * 1000),
                     title="Backed up, but the restic copy failed" if failed else "Backed up his memory and settings",
                     detail=detail)
        return made
