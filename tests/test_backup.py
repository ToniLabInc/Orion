"""backup: an archive of a live data folder, its manifest and verification, retention, hostile and damaged archives,
restoring (refused while he runs, undone on failure), the schedule, restic (a fake one for the exact commands, the real
one when it is installed) and the service's announcements."""

import asyncio
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from filelock import FileLock

from orion import backup
from orion.backup import BackupError, Backups, Restic
from orion.config import BASE_NAME, Config, Settings, render
from orion.memory.store import CURRENT_VERSION, Store

SECRET = "hunter2-the-restic-password"


@dataclass
class World:
    """A data folder as Orion leaves it: a live database, memory, skills, plugins and settings."""

    cfg: Config
    config_path: Path
    store: Store

    @property
    def data(self) -> Path:
        return self.cfg.data_path

    def tree(self) -> dict[str, bytes]:
        """Every file that a backup holds, by its place in the archive."""
        out = {"config.yaml": self.config_path.read_bytes(), BASE_NAME: (self.data / BASE_NAME).read_bytes()}
        for top in ("memory", "skills", "plugins"):
            for path in sorted((self.data / top).rglob("*")):
                transient = path.name == ".lock" or (path.name.startswith(".") and path.name.endswith(".tmp"))
                if path.is_file() and not path.is_symlink() and not transient:
                    out[path.relative_to(self.data).as_posix()] = path.read_bytes()
        return out


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    settings = Settings.open(tmp_path / "data" / "config.yaml", role="main")
    settings.sync_file()
    cfg = settings.cfg
    data = cfg.data_path
    store = Store(data / "orion.db", cfg.memory, role="main")
    store.ensure_schema()
    conv = store.current_conv().id
    store.log_message("user", "Remember the red door.", source="hud", conv=conv, turn="t1", ts=1.0)
    store.log_message("assistant", "The red door, noted, sir.", source="hud", conv=conv, turn="t1", ts=2.0)
    for name, text in (("memory/USER.md", "# About the user\n\n- The user lives in Athens.\n"),
                       ("memory/MEMORY.md", "# Orion's notes\n\n- Likes tea.\n"),
                       ("memory/journal/2026-10-04.md", "## 21:00\nThe user asked about the door.\n"),
                       ("memory/notes/drone-pid.md", "PID values are 48, 52, 38\n"),
                       ("memory/.lock", ""), ("memory/.USER.md.1.2.tmp", "half written"),
                       ("skills/morning-briefing/SKILL.md", "---\nname: morning-briefing\n---\nSay good morning.\n"),
                       ("plugins/demo/.claude-plugin/plugin.json", '{"name": "demo"}\n'),
                       ("plugins/demo/bin/run.sh", "#!/bin/sh\necho hi\n")):
        path = data / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (data / "plugins/demo/bin/run.sh").chmod(0o755)
    for private in ("vault/openrouter.api_key", "models/embed/model.safetensors", "logs/orion.log", "cache.db",
                    "devices.json", "documents.db"):
        (data / private).parent.mkdir(parents=True, exist_ok=True)
        (data / private).write_text("never in a backup")
    yield World(cfg, settings.path, store)
    store.close()


def made(world: World, name: str = "orion-backup-20261004-210000.tar.gz", **kwargs) -> Path:
    """Back up now and return the archive's path."""
    result = backup.create(world.cfg, world.config_path, name=name, **kwargs)
    return backup.backup_folder(world.cfg) / result.name


def forge(path: Path, files: dict[str, bytes], *, listed: dict[str, bytes] | None = None,
          manifest: dict | bytes | None = None, extra: tuple[tarfile.TarInfo, ...] = (), first: bool = True,
          again: tuple[str, ...] = ()) -> Path:
    """An archive written by hand: ``files`` are its members, ``listed`` what its manifest says (default: the same),
    ``manifest`` replaces the manifest (bytes as they are, None for a valid one), ``extra`` adds odd members, ``again``
    adds a second member of each of these names."""
    listed = files if listed is None else listed
    document = {"format": 1, "orion": "test", "created": 1.0, "host": "h", "data_dir": "/elsewhere", "schema": None,
                "files": [{"path": n, "size": len(b), "sha256": hashlib.sha256(b).hexdigest()}
                          for n, b in listed.items()], "skipped": []}
    raw = manifest if isinstance(manifest, bytes) else json.dumps({**document, **(manifest or {})}).encode()

    def add(tar: tarfile.TarFile, name: str, data: bytes) -> None:
        info = tarfile.TarInfo(name)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    with tarfile.open(path, "w:gz") as tar:
        if first:
            add(tar, backup.MANIFEST, raw)
        for name, data in files.items():
            add(tar, name, data)
        for name in again:
            add(tar, name, files[name])
        for info in extra:
            tar.addfile(info)
        if not first:
            add(tar, backup.MANIFEST, raw)
    return path


# ---------------------------------------------------------------------------------------------------- making one
def test_a_backup_holds_what_cannot_be_made_again_and_nothing_else(world: World) -> None:
    """The archive is owner-only and verified; it has the database (a copy that claims no memory-index owner and is one
    self-contained file), memory, skills, plugins (the executable bit kept) and the settings; never the vault, models,
    logs, caches, paired devices, the documents index, a lock file or a half-written temporary file."""
    world.store.set_setting("index_owner", "4242")
    path = made(world)
    folder = path.parent
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700 and stat.S_IMODE(path.stat().st_mode) == 0o600
    extracted = world.data.parent / "extracted"
    extracted.mkdir()
    manifest = backup.verify(path, extract_to=extracted)
    assert set(manifest.files) == {
        "orion.db", "config.yaml", BASE_NAME, "memory/USER.md", "memory/MEMORY.md", "memory/journal/2026-10-04.md",
        "memory/notes/drone-pid.md", "skills/morning-briefing/SKILL.md", "plugins/demo/.claude-plugin/plugin.json",
        "plugins/demo/bin/run.sh"}
    assert manifest.schema == CURRENT_VERSION and manifest.format == backup.FORMAT and manifest.skipped == ()
    assert (extracted / "plugins/demo/bin/run.sh").stat().st_mode & stat.S_IXUSR
    assert not (extracted / "memory/plugins").exists() and not list(extracted.glob("**/*.tmp"))
    with sqlite3.connect(extracted / "orion.db") as db:
        assert db.execute("SELECT content FROM messages ORDER BY id").fetchall() == [
            ("Remember the red door.",), ("The red door, noted, sir.",)]
        assert db.execute("SELECT value FROM settings WHERE key = 'index_owner'").fetchone() == ("",)
        assert db.execute("PRAGMA journal_mode").fetchone() == ("delete",)
    assert world.store.get_setting("index_owner") == "4242"  # the live database was not touched
    with tarfile.open(path) as tar:
        assert tar.getnames()[0] == backup.MANIFEST  # the manifest comes first
        document = json.loads(tar.extractfile(backup.MANIFEST).read())  # type: ignore[union-attr]
    assert document["excluded"] == list(backup.EXCLUDED) and document["files"][0]["path"] == "config.yaml"
    assert not any(p.name.startswith(".work") or p.suffix == ".part" for p in folder.iterdir() if p != path)


def test_a_round_trip_brings_everything_back(world: World, tmp_path: Path) -> None:
    """Back up, lose and change things, restore: the files are what they were, the database opens and answers, the
    replaced files are kept, and a restored memory index owner is empty."""
    path = made(world)
    before = world.tree()
    world.store.close()
    shutil.rmtree(world.data / "memory")
    (world.data / "skills/morning-briefing/SKILL.md").write_text("changed")
    (world.data / "orion.db").write_bytes(b"not a database any more")
    (world.data / "orion.db-wal").write_bytes(b"stale log")
    result = backup.restore(world.cfg, world.config_path, path, now=time.mktime((2026, 10, 5, 8, 0, 0, 0, 0, -1)))
    assert world.tree() == before and result.manifest.schema == CURRENT_VERSION
    assert result.kept is not None and result.kept.name == "before-restore-20261005-080000"
    assert (result.kept / "orion.db").read_bytes() == b"not a database any more"
    assert (result.kept / "orion.db-wal").read_bytes() == b"stale log" and not (world.data / "orion.db-wal").exists()
    assert (result.kept / "skills").is_dir() and not list(world.data.glob(".restore-*"))
    reopened = Store(world.data / "orion.db", world.cfg.memory, role="main")
    reopened.ensure_schema()
    assert [m["content"] for m in reopened.history(reopened.current_conv().id, 10)] == [
        "Remember the red door.", "The red door, noted, sir."]
    reopened.close()


def test_restoring_onto_a_machine_with_nothing_replaces_nothing(world: World) -> None:
    path = made(world)
    world.store.close()
    shutil.rmtree(world.data / "memory")
    shutil.rmtree(world.data / "skills")
    shutil.rmtree(world.data / "plugins")
    (world.data / "orion.db").unlink()
    world.config_path.unlink()
    (world.data / BASE_NAME).unlink()
    result = backup.restore(world.cfg, world.config_path, path)
    assert result.kept is None and (world.data / "memory/USER.md").is_file() and world.config_path.is_file()


def test_the_database_is_copied_while_it_is_written(world: World, tmp_path: Path) -> None:
    """A writer commits pairs of rows as fast as it can; no archive ever holds half a pair, and each passes SQLite's
    own check: the copy is one consistent moment."""
    stop = threading.Event()
    pairs = []

    def write() -> None:
        db = sqlite3.connect(world.data / "orion.db", isolation_level=None, timeout=30)
        n = 0
        while not stop.is_set():
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO todos (text, ts) VALUES (?, 1)", (f"pair-{n}",))
            db.execute("INSERT INTO todos (text, ts) VALUES (?, 2)", (f"pair-{n}",))
            db.execute("COMMIT")
            n += 1
        pairs.append(n)
        db.close()

    writer = threading.Thread(target=write)
    writer.start()
    try:
        for i in range(12):
            out = tmp_path / f"out{i}"
            out.mkdir()
            backup.verify(made(world, f"orion-backup-20261004-2100{i:02d}.tar.gz"), extract_to=out)
            with sqlite3.connect(out / "orion.db") as copy:
                counts = copy.execute("SELECT COUNT(*) FROM todos GROUP BY text").fetchall()
                assert all(n == 2 for (n,) in counts) and copy.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        stop.set()
        writer.join()
    assert pairs[0] > 12  # it really was busy


def test_a_file_that_changes_between_the_two_passes_starts_the_backup_again(world: World,
                                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """A skill saved after the first pass has measured it: the archive must say what it holds, so the backup starts
    over, and the new archive holds the new text. A file that never stops changing fails the backup cleanly."""
    skill = world.data / "skills/morning-briefing/SKILL.md"
    real = backup._write
    calls = []

    def changing(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            skill.write_text("saved in the middle of a backup")
        return real(*args, **kwargs)

    monkeypatch.setattr(backup, "_write", changing)
    path = made(world)
    assert len(calls) == 2
    extracted = world.data.parent / "x"
    extracted.mkdir()
    backup.verify(path, extract_to=extracted)
    assert (extracted / "skills/morning-briefing/SKILL.md").read_text() == "saved in the middle of a backup"

    def always(*args, **kwargs):
        skill.write_text(f"{time.time_ns()}")
        return real(*args, **kwargs)

    monkeypatch.setattr(backup, "_write", always)
    with pytest.raises(BackupError, match=r"SKILL\.md kept changing"):
        made(world, "orion-backup-20261004-220000.tar.gz")
    folder = backup.backup_folder(world.cfg)
    assert [i.name for i in backup.archives(folder)] == ["orion-backup-20261004-210000.tar.gz"]
    assert sorted(p.name for p in folder.iterdir()) == [".backup.lock", "orion-backup-20261004-210000.tar.gz"]


def test_links_and_odd_files_are_left_out_and_said_so(world: World) -> None:
    """A link inside memory could lead a backup (and a restic copy off this machine) to any file the user can read:
    it is not followed, and the manifest lists what was left out."""
    outside = world.data.parent / "outside.txt"
    outside.write_text("a file the backup has no business with")
    (world.data / "memory/notes/link.md").symlink_to(outside)
    (world.data / "memory/linked").symlink_to(world.data.parent)
    os.mkfifo(world.data / "memory/notes/pipe")
    (world.data / "memory/notes/bad\udcffname.md").write_text("not text as a name")
    path = made(world)
    manifest = backup.verify(path)
    assert not any("outside" in name or "link" in name or "pipe" in name for name in manifest.files)
    assert manifest.skipped == ("memory/linked (a link)", r"memory/notes/bad\udcffname.md (a name that isn't text)",
                                "memory/notes/link.md (a link)", "memory/notes/pipe (not a plain file)")


def test_a_second_backup_in_the_same_second_gets_its_own_name(world: World) -> None:
    first, second = made(world), made(world)
    assert (first.name, second.name) == ("orion-backup-20261004-210000.tar.gz", "orion-backup-20261004-210001.tar.gz")


def test_only_one_backup_runs_at_a_time(world: World) -> None:
    folder = backup.backup_folder(world.cfg)
    folder.mkdir(parents=True)
    with FileLock(folder / backup.LOCK_FILE):
        with pytest.raises(BackupError, match="Another backup is running"):
            made(world)
        assert not list(folder.glob("*.part")) and not (folder / backup.WORK).exists()


def test_leftovers_of_a_crashed_backup_are_cleared(world: World) -> None:
    folder = backup.backup_folder(world.cfg)
    (folder / backup.WORK).mkdir(parents=True)
    (folder / backup.WORK / "orion.db").write_text("half")
    (folder / backup.STAGE).mkdir()
    (folder / "orion-backup-20250101-000000.tar.gz.part").write_text("half")
    (folder / "notes.txt").write_text("the user's, in the same folder")
    made(world)
    assert sorted(p.name for p in folder.iterdir()) == [".backup.lock", "notes.txt", "orion-backup-20261004-210000.tar.gz"]


def test_the_backup_folder_cannot_be_inside_what_is_backed_up(world: World) -> None:
    world.cfg.backup.folder = str(world.data / "memory" / "backups")
    with pytest.raises(BackupError, match="inside memory"):
        made(world)


def test_a_full_disk_is_refused_before_anything_is_written(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup.shutil, "disk_usage", lambda path: SimpleNamespace(total=100, used=99, free=1))
    with pytest.raises(BackupError, match="isn't enough room"):
        made(world)
    assert not list(backup.backup_folder(world.cfg).glob("orion-backup-*"))


def test_a_damaged_database_is_not_archived(world: World, tmp_path: Path) -> None:
    """The live file is checked as it is copied: a backup that would silently archive a broken database is worse than
    none, and the older archives stay."""
    older = made(world)
    world.store.close()
    raw = bytearray((world.data / "orion.db").read_bytes())
    raw[4096 * 2: 4096 * 2 + 64] = b"\xff" * 64  # inside a table's page
    (world.data / "orion.db").write_bytes(bytes(raw))
    for suffix in ("-wal", "-shm"):
        (world.data / f"orion.db{suffix}").unlink(missing_ok=True)
    with pytest.raises(BackupError, match=r"database (is damaged|couldn't be copied)"):
        made(world, "orion-backup-20261004-220000.tar.gz")
    assert [i.name for i in backup.archives(older.parent)] == [older.name]


# ---------------------------------------------------------------------------------------------------- keeping them
def test_retention_keeps_the_newest_and_touches_only_archives(world: World) -> None:
    folder = backup.backup_folder(world.cfg)
    folder.mkdir(parents=True)
    for name in ("notes.txt", "orion-backup-2020.tar.gz", "orion-backup-20200101-000000.tar.gz.bak",
                 "orion-backup-20200101-000000.zip"):
        (folder / name).write_text("not an archive of Orion's")
    (folder / "orion-backup-20200101-000000.tar.gz").mkdir()  # a folder named like one
    world.cfg.backup.keep = 3
    for second in range(5):
        made(world, f"orion-backup-20261004-2100{second:02d}.tar.gz")
    assert [i.name for i in backup.archives(folder)] == [f"orion-backup-20261004-2100{s:02d}.tar.gz" for s in (4, 3, 2)]
    assert sorted(p.name for p in folder.iterdir() if not p.name.startswith("orion-backup-2026")) == [
        ".backup.lock", "notes.txt", "orion-backup-2020.tar.gz", "orion-backup-20200101-000000.tar.gz",
        "orion-backup-20200101-000000.tar.gz.bak", "orion-backup-20200101-000000.zip"]


def test_a_backup_that_fails_deletes_no_older_one(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.cfg.backup.keep = 1
    first = made(world)
    monkeypatch.setattr(backup, "verify", lambda *a, **k: (_ for _ in ()).throw(BackupError("it failed its own check")))
    with pytest.raises(BackupError, match="own check"):
        made(world, "orion-backup-20261004-220000.tar.gz")
    assert [i.name for i in backup.archives(first.parent)] == [first.name]


def test_the_newest_archive_is_found(world: World) -> None:
    with pytest.raises(BackupError, match="no backups"):
        backup.latest(world.cfg)
    made(world, "orion-backup-20261004-210000.tar.gz")
    newest = made(world, "orion-backup-20261005-080000.tar.gz")
    assert backup.latest(world.cfg) == newest


# ---------------------------------------------------------------------------------------------------- bad archives
def test_damaged_archives_are_refused(world: World, tmp_path: Path) -> None:
    good = made(world)
    raw = good.read_bytes()
    cases = {
        "truncated": raw[: len(raw) // 2], "empty": b"", "not an archive": b"hello there",
        "a flipped byte": raw[: len(raw) // 2] + bytes([raw[len(raw) // 2] ^ 0xFF]) + raw[len(raw) // 2 + 1:],
        "a plain tar": None}
    for what, content in cases.items():
        path = tmp_path / f"{what.replace(' ', '-')}.tar.gz"
        if content is None:
            with tarfile.open(path, "w") as tar:  # not gzip at all
                tar.addfile(tarfile.TarInfo("x"))
        else:
            path.write_bytes(content)
        with pytest.raises(BackupError, match="damaged|isn't a backup"):
            backup.verify(path)
    with pytest.raises(BackupError, match="no backup at"):
        backup.verify(tmp_path / "missing.tar.gz")


def test_a_tampered_file_fails_its_checksum(tmp_path: Path) -> None:
    files = {"memory/USER.md": b"- The user lives in Athens.\n"}
    listed = {"memory/USER.md": b"- The user lives in Paris!.\n"}  # same length, other bytes
    with pytest.raises(BackupError, match="doesn't match its checksum"):
        backup.verify(forge(tmp_path / "a.tar.gz", files, listed=listed))
    with pytest.raises(BackupError, match="bytes in the archive but"):
        backup.verify(forge(tmp_path / "b.tar.gz", files, listed={"memory/USER.md": b"short"}))


@pytest.mark.parametrize("name", ["../evil.txt", "memory/../../evil.txt", "/etc/evil.txt", "memory/./x", "memory//x",
                                  "evil/x.txt", "orion.db/extra", "config.yaml/x", "", "memory\0x"])
def test_names_that_could_leave_the_folder_are_refused_and_nothing_is_written(tmp_path: Path, name: str) -> None:
    """Whether the manifest lists it or only the archive holds it, no unsafe or unknown name is accepted, and nothing
    lands outside the folder it is unpacked to."""
    target = tmp_path / "into"
    target.mkdir()
    with pytest.raises(BackupError):
        backup.verify(forge(tmp_path / "listed.tar.gz", {name: b"x"}), extract_to=target)
    with pytest.raises(BackupError, match="not a place|not in its list"):
        backup.verify(forge(tmp_path / "hidden.tar.gz", {"memory/ok.md": b"ok", name: b"x"},
                            listed={"memory/ok.md": b"ok"}), extract_to=target)
    assert not (tmp_path / "evil.txt").exists() and not Path("/etc/evil.txt").exists()
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")
                  if p.name == "evil.txt") == []


def test_only_plain_files_are_accepted(tmp_path: Path) -> None:
    for number, (kind, link) in enumerate(((tarfile.SYMTYPE, "/etc/passwd"), (tarfile.LNKTYPE, "memory/ok.md"),
                                           (tarfile.DIRTYPE, ""), (tarfile.CHRTYPE, ""), (tarfile.FIFOTYPE, ""))):
        target = tmp_path / f"into{number}"
        target.mkdir()
        info = tarfile.TarInfo("memory/odd")
        info.type, info.linkname = kind, link
        with pytest.raises(BackupError, match="not a plain file"):
            backup.verify(forge(tmp_path / "odd.tar.gz", {"memory/ok.md": b"ok"}, extra=(info,)), extract_to=target)
        assert list(target.rglob("odd")) == []


def test_the_archive_must_match_its_list(tmp_path: Path) -> None:
    files = {"memory/a.md": b"a", "memory/b.md": b"b"}
    with pytest.raises(BackupError, match="incomplete: memory/b.md is missing"):
        backup.verify(forge(tmp_path / "1.tar.gz", {"memory/a.md": b"a"}, listed=files))
    with pytest.raises(BackupError, match="not in its list"):
        backup.verify(forge(tmp_path / "2.tar.gz", files, listed={"memory/a.md": b"a"}))
    with pytest.raises(BackupError, match="in that archive twice"):
        backup.verify(forge(tmp_path / "3.tar.gz", {"memory/a.md": b"a"}, again=("memory/a.md",)))


def test_the_manifest_comes_first_and_is_understood(tmp_path: Path) -> None:
    files = {"memory/a.md": b"a"}
    with pytest.raises(BackupError, match="no list of its files"):
        backup.verify(forge(tmp_path / "1.tar.gz", files, first=False))
    for bad in (b"not json", b"[]", b'{"format": "one"}', b'{"format": true}', b'{}'):
        with pytest.raises(BackupError, match="list of files"):
            backup.verify(forge(tmp_path / "2.tar.gz", files, manifest=bad))
    with pytest.raises(BackupError, match="newer Orion .layout 2"):
        backup.verify(forge(tmp_path / "3.tar.gz", files, manifest={"format": 2}))
    for broken in ({"files": [{"path": "memory/a.md", "size": -1, "sha256": "0" * 64}]},
                   {"files": [{"path": "memory/a.md", "size": 1, "sha256": "xyz"}]},
                   {"files": [{"path": "memory/a.md", "size": 1}]},
                   {"files": [{"path": "memory/a.md", "size": 1, "sha256": "0" * 64}] * 2}):
        with pytest.raises(BackupError, match="malformed"):
            backup.verify(forge(tmp_path / "4.tar.gz", files, manifest=broken))


# ---------------------------------------------------------------------------------------------------- restoring
def test_restoring_is_refused_while_anything_has_the_database_open(world: World) -> None:
    """Orion, his tools, another app lent his tools: any process holding orion.db open stops a restore, named."""
    path = made(world)
    with pytest.raises(BackupError, match=rf"Orion is running \(process {os.getpid()} "):
        backup.restore(world.cfg, world.config_path, path)
    holder = ("import sqlite3, sys, time\ndb = sqlite3.connect(sys.argv[1])\ndb.execute('select 1').fetchall()\n"
              "print('open', flush=True)\ntime.sleep(60)")
    child = subprocess.Popen([sys.executable, "-c", holder, str(world.data / "orion.db")], stdout=subprocess.PIPE,
                             text=True)
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "open"
        assert child.pid in [pid for pid, _ in backup.holders(world.data / "orion.db")]
    finally:
        child.kill()
        child.wait()
    world.store.close()
    assert backup.restore(world.cfg, world.config_path, path).manifest.files  # nobody has it open now


def test_a_restore_that_fails_on_the_way_puts_everything_back(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    path = made(world)
    world.store.close()
    (world.data / "memory/USER.md").write_text("changed since the backup")
    before = world.tree()
    database = (world.data / "orion.db").read_bytes()
    real = backup.shutil.move
    for failing_call in range(1, 9):  # whichever move fails, the result is the same
        calls = []

        def move(source, destination, failing_call=failing_call, calls=calls):
            calls.append(1)
            if len(calls) == failing_call:
                raise OSError("disk trouble")
            return real(source, destination)

        monkeypatch.setattr(backup.shutil, "move", move)
        with pytest.raises(OSError, match="disk trouble"):
            backup.restore(world.cfg, world.config_path, path)
        monkeypatch.setattr(backup.shutil, "move", real)
        assert world.tree() == before and (world.data / "orion.db").read_bytes() == database
        assert not list(world.data.glob("before-restore-*")) and not list(world.data.glob(".restore-*")), failing_call


def test_a_database_this_orion_cannot_open_is_refused_before_anything_moves(world: World, tmp_path: Path) -> None:
    world.store.close()
    before = world.tree()
    for version, why in ((CURRENT_VERSION + 1, "newer Orion"), (1, "too old")):
        copy = tmp_path / f"v{version}.db"
        with sqlite3.connect(copy) as db:
            db.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
            db.execute(f"PRAGMA user_version = {version}")
        archive = forge(tmp_path / f"v{version}.tar.gz", {"orion.db": copy.read_bytes(), "memory/USER.md": b"new"})
        with pytest.raises(BackupError, match=why):
            backup.restore(world.cfg, world.config_path, archive)
    garbage = forge(tmp_path / "garbage.tar.gz", {"orion.db": b"SQLite format 3\0" + b"\xff" * 5000})
    with pytest.raises(BackupError, match="database"):
        backup.restore(world.cfg, world.config_path, garbage)
    assert world.tree() == before and not list(world.data.glob("before-restore-*"))


def test_a_restore_says_when_the_data_folder_moved(world: World, tmp_path: Path) -> None:
    """A backup from another machine names its own data folder: the restore puts the files in this one, and says that
    the restored settings may point elsewhere."""
    world.store.close()
    archive = forge(tmp_path / "moved.tar.gz", {"memory/USER.md": b"x"}, manifest={"data_dir": "/mnt/big/orion"})
    result = backup.restore(world.cfg, world.config_path, archive)
    assert "/mnt/big/orion" in result.warnings[0] and str(world.data) in result.warnings[0]
    assert (world.data / "memory/USER.md").read_bytes() == b"x"


def test_a_link_where_the_files_go_is_not_replaced(world: World, tmp_path: Path) -> None:
    path = made(world)
    world.store.close()
    shutil.move(world.data / "memory", tmp_path / "elsewhere")
    (world.data / "memory").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(BackupError, match="is a link"):
        backup.restore(world.cfg, world.config_path, path)
    assert (world.data / "memory").is_symlink() and not list(world.data.glob("before-restore-*"))


def test_the_settings_file_may_live_outside_the_data_folder(world: World, tmp_path: Path) -> None:
    elsewhere = tmp_path / "etc" / "orion.yaml"
    elsewhere.parent.mkdir()
    shutil.copy(world.config_path, elsewhere)
    shutil.copy(world.data / BASE_NAME, elsewhere.with_name(BASE_NAME))
    path = backup.backup_folder(world.cfg) / backup.create(world.cfg, elsewhere, name="orion-backup-20261004-210000.tar.gz").name
    assert backup.verify(path).files["config.yaml"][0] == elsewhere.stat().st_size
    world.store.close()
    elsewhere.write_text("changed")
    backup.restore(world.cfg, elsewhere, path)
    assert elsewhere.read_bytes() == world.config_path.read_bytes()


# ---------------------------------------------------------------------------------------------------- the schedule
def at(day: int, hour: int, minute: int = 0) -> float:
    """Epoch seconds of a local time in October 2026."""
    return time.mktime((2026, 10, day, hour, minute, 0, 0, 0, -1))


def test_the_schedule_makes_up_a_missed_backup_and_waits_after_a_failed_one(world: World) -> None:
    """Nightly at 03:30: with none ever made it is due at once; after one it is due at the next 03:30; one missed while
    Orion was off is made when he starts; a failed try waits for the next time instead of trying every minute."""
    service = Backups(world.cfg, world.config_path, publish=lambda *a, **k: None, record=lambda *a, **k: None,
                      password=lambda: None)
    assert world.cfg.backup.schedule == "30 3 * * *"
    assert service.next_run() == 0.0 and service.due(at(5, 9))
    folder = backup.backup_folder(world.cfg)
    folder.mkdir(parents=True)
    (folder / "orion-backup-20261005-033000.tar.gz").write_bytes(b"x")
    assert service.next_run() == at(6, 3, 30)
    assert not service.due(at(5, 23)) and service.due(at(6, 3, 30))
    assert service.due(at(8, 9))  # three nights missed: made once, at start
    service._attempted = at(8, 9)
    assert not service.due(at(8, 9, 1)) and service.next_run() == at(9, 3, 30)
    world.cfg.backup.schedule = ""
    assert service.next_run() is None and not service.due(at(30, 0))


# ---------------------------------------------------------------------------------------------------- the service
def test_a_backup_is_announced_to_the_hud_and_the_activity_log(world: World) -> None:
    events: list[tuple[str, dict]] = []
    records: list[tuple[str, dict]] = []
    service = Backups(world.cfg, world.config_path, publish=lambda kind, **data: events.append((kind, data)),
                      record=lambda kind, **data: records.append((kind, data)), password=lambda: None)

    async def run() -> list[backup.Made]:
        first = asyncio.ensure_future(service.run("hud"))
        await asyncio.sleep(0)
        second = asyncio.ensure_future(service.run("voice"))  # asked while the first runs: the same backup
        return await asyncio.gather(first, second)

    one, two = asyncio.run(run())
    assert one == two and len(backup.archives(backup.backup_folder(world.cfg))) == 1
    assert [(kind, data["phase"]) for kind, data in events] == [("backup", "start"), ("backup", "done")]
    assert events[0][1]["name"] == events[1][1]["name"] == one.name and events[1][1]["size"] == one.size
    (kind, record), = records
    assert kind == "backup" and record["source"] == "hud" and record["outcome"] == "ok" and one.name in record["detail"]
    listing = asyncio.run(service.listing())
    assert listing["keep"] == 7 and listing["schedule"] == "30 3 * * *" and listing["restic"] == {
        "configured": False, "available": backup.profile().has("restic"), "error": ""}
    assert [item["name"] for item in listing["items"]] == [one.name]  # type: ignore[union-attr]


def test_a_failed_backup_is_announced_too(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    events, records = [], []
    service = Backups(world.cfg, world.config_path, publish=lambda kind, **data: events.append(data),
                      record=lambda kind, **data: records.append(data), password=lambda: None)
    monkeypatch.setattr(backup, "_copy_database", lambda *a: (_ for _ in ()).throw(BackupError("no room")))
    with pytest.raises(BackupError, match="no room"):
        asyncio.run(service.run("system"))
    assert [e["phase"] for e in events] == ["start", "failed"] and events[1]["error"] == "no room"
    assert records[0]["outcome"] == "failed" and records[0]["source"] == "system" and not service.running


def test_an_unexpected_failure_is_announced_as_well(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """Whatever goes wrong, the HUD hears "failed" (a start without an end would leave its button waiting)."""
    events = []
    service = Backups(world.cfg, world.config_path, publish=lambda kind, **data: events.append(data),
                      record=lambda *a, **k: None, password=lambda: None)
    monkeypatch.setattr(backup, "_copy_database", lambda *a: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        asyncio.run(service.run("hud"))
    assert [e["phase"] for e in events] == ["start", "failed"] and "ZeroDivisionError" in events[1]["error"]


def test_starting_a_backup_does_not_wait_and_the_scheduler_does_not_spin(world: World) -> None:
    """``start`` returns at once with the task of the backup under way (a second call gets the same one, and its name is
    known for the HUD); while it runs the schedule has nothing to say, so the scheduler sleeps instead of looping."""
    service = Backups(world.cfg, world.config_path, publish=lambda *a, **k: None, record=lambda *a, **k: None,
                      password=lambda: None)

    async def run() -> tuple[object, ...]:
        assert service.due(time.time()) and not service.running and service.name == ""
        first = service.start("system")
        assert service.running and service.next_run() is None and not service.due(time.time())
        name = service.name
        assert service.start("hud") is first
        made_ = await first
        return name, made_.name, service.running, service.name

    name, made_name, running, after = asyncio.run(run())
    assert name == made_name and not running and after == ""


def test_a_backup_given_up_for_a_stopping_orion_leaves_nothing_behind(world: World) -> None:
    stop = threading.Event()
    stop.set()
    with pytest.raises(BackupError, match="Orion is stopping"):
        backup.create(world.cfg, world.config_path, name="orion-backup-20261004-210000.tar.gz", stop=stop)
    assert not [p for p in backup.backup_folder(world.cfg).iterdir() if p.name != backup.LOCK_FILE]


def test_stopping_orion_ends_a_restic_copy_under_way(world: World, fake_restic: tuple[Path, Path],
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """A slow restic must not hold up shutdown: the service waits a little, restic is ended, and the archive made
    before it stands."""
    monkeypatch.setenv("FAKE_RESTIC_MODE", "slow")
    world.cfg.backup.restic_repository = "repo"
    monkeypatch.setattr(backup, "profile", lambda: SimpleNamespace(restic=str(fake_restic[0]),
                                                                   has=lambda need: True))
    service = Backups(world.cfg, world.config_path, publish=lambda *a, **k: None, record=lambda *a, **k: None,
                      password=lambda: SECRET)

    async def run() -> tuple[backup.Made, float]:
        task = service.start("system")
        while not calls_made(fake_restic[1]):
            await asyncio.sleep(0.05)
        started = time.monotonic()
        await service.stop()
        return await task, time.monotonic() - started

    made_, took = asyncio.run(run())
    assert took < 5 and "ended because Orion is stopping" in made_.restic
    assert len(backup.archives(backup.backup_folder(world.cfg))) == 1


def calls_made(log: Path) -> bool:
    """Whether the fake restic has been run yet."""
    return log.exists() and bool(log.read_text())


def test_the_backup_runs_on_a_thread_of_its_own_that_yields_the_processor(world: World,
                                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """A scheduled backup lowers its thread's priority; that must never be the shared pool's thread, which could not
    be given its priority back."""
    seen = []
    monkeypatch.setattr(backup, "lower_priority", lambda nice: seen.append((threading.current_thread().name, nice)))
    asyncio.run(Backups(world.cfg, world.config_path, publish=lambda *a, **k: None, record=lambda *a, **k: None,
                        password=lambda: None).run("system"))
    assert seen[0][1] == backup.NICE and seen[0][0].startswith("backup")


# ---------------------------------------------------------------------------------------------------- restic
FAKE_RESTIC = """#!{python}
import json, os, sys, time
mode = os.environ.get("FAKE_RESTIC_MODE", "ok")
path = os.environ.get("RESTIC_PASSWORD_FILE", "")
with open(os.environ["FAKE_RESTIC_LOG"], "a") as log:
    log.write(json.dumps({{"argv": sys.argv[1:], "password_file": path, "password": open(path).read() if path else None,
                           "env_password": "RESTIC_PASSWORD" in os.environ, "repository": os.environ.get("RESTIC_REPOSITORY"),
                           "cwd": os.getcwd(), "files": sorted(os.listdir("."))}}) + "\\n")
command = sys.argv[1]
if mode == "missing" and command == "cat":
    sys.stderr.write("Fatal: repository does not exist\\nIs there a repository at the following location?\\n")
    sys.exit(10)
if mode == "wrong":
    sys.stderr.write("Fatal: wrong password or no key found\\n"); sys.exit(12)
if mode == "locked":
    sys.exit(11)
if mode == "boom":
    sys.stderr.write("line one\\nFatal: cannot reach rest:https://orion:hunter2@backup.example/repo\\n"); sys.exit(1)
if mode == "slow":
    time.sleep(30)
if command == "snapshots":
    print(json.dumps([{{"short_id": "aaaa1111", "time": "2026-10-04T21:00:00.123456789+03:00", "summary": {{"total_bytes_processed": 4096}}}},
                      {{"short_id": "bbbb2222", "time": "2026-10-05T21:00:00+03:00"}}]))
"""


@pytest.fixture
def fake_restic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Path, Path]]:
    script = tmp_path / "bin" / "restic"
    script.parent.mkdir()
    script.write_text(FAKE_RESTIC.format(python=sys.executable))
    script.chmod(0o755)
    log = tmp_path / "restic.log"
    monkeypatch.setenv("FAKE_RESTIC_LOG", str(log))
    monkeypatch.setenv("RESTIC_PASSWORD", "from the user's own environment")  # must not reach restic
    yield script, log


def calls(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_restic_gets_its_password_through_a_pipe_and_nowhere_else(fake_restic: tuple[Path, Path]) -> None:
    script, log = fake_restic
    Restic(str(script), "sftp:nas:/backups/orion", SECRET, timeout_s=30).run("snapshots", "--json")
    (call,) = calls(log)
    assert call["password"] == SECRET and call["password_file"].startswith("/dev/fd/") and not call["env_password"]
    assert call["repository"] == "sftp:nas:/backups/orion" and SECRET not in " ".join(call["argv"])


def test_a_repository_that_is_not_there_yet_is_created(fake_restic: tuple[Path, Path],
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    script, log = fake_restic
    restic = Restic(str(script), "repo", SECRET, timeout_s=30)
    restic.ready()
    assert [c["argv"] for c in calls(log)] == [["cat", "config"]]
    monkeypatch.setenv("FAKE_RESTIC_MODE", "missing")
    restic.ready()
    assert [c["argv"] for c in calls(log)][1:] == [["cat", "config"], ["init", "--quiet"]]


@pytest.mark.parametrize(("mode", "words"), [
    ("missing", "no restic repository"), ("wrong", "password is wrong"), ("locked", "locked by another run"),
    ("boom", r"orion:\*\*\*@backup\.example")])
def test_restic_failures_are_said_in_words(fake_restic: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
                                           mode: str, words: str) -> None:
    monkeypatch.setenv("FAKE_RESTIC_MODE", mode)
    with pytest.raises(BackupError, match=words) as raised:
        Restic(str(fake_restic[0]), "repo", SECRET, timeout_s=30).run("cat", "config")
    assert "hunter2" not in str(raised.value) and SECRET not in str(raised.value)


def test_a_restic_run_that_takes_too_long_is_stopped(fake_restic: tuple[Path, Path],
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_RESTIC_MODE", "slow")
    started = time.monotonic()
    with pytest.raises(BackupError, match="didn't finish within 1 seconds"):
        Restic(str(fake_restic[0]), "repo", SECRET, timeout_s=1).run("check")
    assert time.monotonic() - started < 10
    with pytest.raises(BackupError, match="couldn't be started"):
        Restic("/nonexistent/restic", "repo", SECRET, timeout_s=1).run("check")


def test_restic_keeps_the_tree_and_forgets_by_the_policy(world: World, fake_restic: tuple[Path, Path]) -> None:
    script, log = fake_restic
    world.cfg.backup.restic_keep_daily, world.cfg.backup.restic_keep_weekly = 7, 0
    result = backup.create(world.cfg, world.config_path, name="orion-backup-20261004-210000.tar.gz",
                           restic=Restic(str(script), "repo", SECRET, timeout_s=30))
    assert result.restic == "ok"
    arguments = [c["argv"] for c in calls(log)]
    assert arguments[0] == ["cat", "config"]
    assert arguments[1] == ["backup", "--quiet", "--host", "orion", "--tag", "orion", "--ignore-inode",
                            "--ignore-ctime", "."]
    assert arguments[2] == ["forget", "--quiet", "--host", "orion", "--tag", "orion", "--keep-daily", "7",
                            "--keep-monthly", "12", "--prune"]
    assert calls(log)[1]["files"] == sorted(["manifest.json", "memory", "skills", "plugins", "orion.db", "config.yaml",
                                              BASE_NAME])
    assert not (backup.backup_folder(world.cfg) / backup.STAGE).exists()


def test_a_failed_restic_copy_leaves_the_archive_and_says_so(world: World, fake_restic: tuple[Path, Path],
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_RESTIC_MODE", "wrong")
    events, records = [], []
    service = Backups(world.cfg, world.config_path, publish=lambda kind, **data: events.append(data),
                      record=lambda kind, **data: records.append(data), password=lambda: SECRET)
    world.cfg.backup.restic_repository = "repo"
    monkeypatch.setattr(backup, "profile", lambda: type("P", (), {"restic": str(fake_restic[0]),
                                                                  "has": lambda self, need: True})())
    made_ = asyncio.run(service.run("system"))
    assert made_.restic.startswith("restic says the password is wrong")
    assert len(backup.archives(backup.backup_folder(world.cfg))) == 1
    assert events[-1]["phase"] == "done" and "password is wrong" in events[-1]["error"]
    assert records[0]["outcome"] == "failed" and "restic copy failed" in records[0]["title"]


def test_restic_without_a_password_or_a_program_still_makes_the_archive(world: World,
                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    world.cfg.backup.restic_repository = "repo"
    service = Backups(world.cfg, world.config_path, publish=lambda *a, **k: None, record=lambda *a, **k: None,
                      password=lambda: None)
    monkeypatch.setattr(backup, "profile", lambda: type("P", (), {"restic": "", "has": lambda self, need: False})())
    assert "isn't installed" in asyncio.run(service.run("hud")).restic
    monkeypatch.setattr(backup, "profile", lambda: type("P", (), {"restic": "/usr/bin/restic",
                                                                  "has": lambda self, need: True})())
    second = asyncio.run(service.run("hud"))
    assert "needs its password" in second.restic and second.name != "" and len(backup.archives(
        backup.backup_folder(world.cfg))) == 2


def test_restic_snapshots_are_listed_for_the_hud(world: World, fake_restic: tuple[Path, Path],
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    world.cfg.backup.restic_repository = "repo"
    monkeypatch.setattr(backup, "profile", lambda: type("P", (), {"restic": str(fake_restic[0]),
                                                                  "has": lambda self, need: True})())
    service = Backups(world.cfg, world.config_path, publish=lambda *a, **k: None, record=lambda *a, **k: None,
                      password=lambda: SECRET)
    listing = asyncio.run(service.listing())
    items = listing["items"]
    assert [(i["name"], i["where"], i["size"]) for i in items] == [  # type: ignore[index,union-attr]
        ("bbbb2222", "restic", 0), ("aaaa1111", "restic", 4096)]
    assert listing["restic"] == {"configured": True, "available": True, "error": ""}
    assert len(calls(fake_restic[1])) == 1
    asyncio.run(service.listing())
    assert len(calls(fake_restic[1])) == 1  # asked at most once a minute


@pytest.fixture
def real_restic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Restic:
    binary = os.environ.get("ORION_TEST_RESTIC") or shutil.which("restic")
    if not binary:
        pytest.skip("restic isn't installed (ORION_TEST_RESTIC names one to test with)")
    monkeypatch.setenv("RESTIC_CACHE_DIR", str(tmp_path / "restic-cache"))
    monkeypatch.delenv("RESTIC_PASSWORD", raising=False)
    return Restic(binary, str(tmp_path / "repo"), SECRET, timeout_s=120)


def test_real_restic_keeps_forgets_and_restores(world: World, real_restic: Restic, tmp_path: Path) -> None:
    """The real program, a repository in a temporary folder: created on first use, one snapshot per backup, old ones
    forgotten by the policy, and a snapshot restores into a data folder that lost everything."""
    world.cfg.backup.restic_keep_daily, world.cfg.backup.restic_keep_weekly = 1, 0
    world.cfg.backup.restic_keep_monthly = 0
    first = backup.create(world.cfg, world.config_path, name="orion-backup-20261004-210000.tar.gz", restic=real_restic)
    assert first.restic == "ok" and len(real_restic.snapshots()) == 1
    (world.data / "memory/notes/new.md").write_text("a second backup's note")
    second = backup.create(world.cfg, world.config_path, name="orion-backup-20261004-220000.tar.gz", restic=real_restic)
    assert second.restic == "ok"
    snapshots = real_restic.snapshots()
    assert len(snapshots) == 1 and snapshots[0].where == "restic" and snapshots[0].size > 0  # daily 1: only the newest
    before = world.tree()
    world.store.close()
    for top in ("memory", "skills", "plugins"):
        shutil.rmtree(world.data / top)
    (world.data / "orion.db").unlink()
    result = backup.restore_from_restic(world.cfg, world.config_path, real_restic, snapshots[0].name)
    assert world.tree() == before and result.manifest.schema == CURRENT_VERSION
    with pytest.raises(BackupError, match="password is wrong"):
        Restic(real_restic._binary, real_restic._repository, "not the password", timeout_s=60).ready()


def test_a_restic_snapshot_that_is_not_ours_is_refused(real_restic: Restic, world: World, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    (other / "memory").mkdir()
    (other / "memory" / "x.md").write_text("not a backup of Orion's: it has no manifest")
    real_restic.ready()
    real_restic.run("backup", "--quiet", "--host", backup.RESTIC_HOST, "--tag", backup.RESTIC_TAG, ".", cwd=other)
    world.store.close()
    with pytest.raises(BackupError, match="isn't a backup of Orion's"):
        backup.restore_from_restic(world.cfg, world.config_path, real_restic, "latest")


# ---------------------------------------------------------------------------------------------------- the surfaces
def make_service(world: World) -> Backups:
    """The backups of a test Orion: nothing published or recorded anywhere."""
    return Backups(world.cfg, world.config_path, publish=lambda *a, **k: None, record=lambda *a, **k: None,
                   password=lambda: None)


def test_the_hud_lists_and_starts_backups(world: World) -> None:
    """GET lists what the HUD shows, POST names the backup it starts at once; the archive follows."""
    from fastapi.testclient import TestClient

    from orion.pairing import Pairing
    from orion.server import create_app

    rt = SimpleNamespace(cfg=world.cfg, controls=None, hub=None, backups=make_service(world), activity=None)
    app = create_app(rt, Pairing(world.data, world.cfg.server))  # type: ignore[arg-type]  # a stub of the runtime
    with TestClient(app) as client:
        empty = client.get("/api/backups").json()
        assert empty["items"] == [] and empty["schedule"] == "30 3 * * *" and empty["keep"] == 7
        assert empty["folder"] == str(backup.backup_folder(world.cfg)) and empty["restic"]["configured"] is False
        started = client.post("/api/backups").json()
        assert re.fullmatch(r"orion-backup-\d{8}-\d{6}\.tar\.gz", started["name"])
        for _ in range(100):
            listed = client.get("/api/backups").json()
            if listed["items"]:
                break
            time.sleep(0.1)
        (item,) = listed["items"]
        assert item["name"] == started["name"] and item["where"] == "local" and item["size"] > 0
        assert set(item) == {"name", "at", "size", "where"} and abs(item["at"] - time.time()) < 120


def test_the_tools_back_up_and_list(world: World) -> None:
    """Through a real hub, as the model meets them: a backup is made and described; a second one asked for by the model
    within five minutes is refused (the oldest ones would be pushed out), the user's own is not."""
    from orion.base import CallContext
    from orion.mcp import MCPHub, OpTrace
    from orion.tools import core

    controls = SimpleNamespace(rt=SimpleNamespace(cfg=world.cfg, backups=make_service(world)))
    hub = MCPHub(world.cfg, {}, ops=OpTrace(lambda kind, **data: None, world.cfg.hud, recent=10),
                 core=core.server(controls), core_groups={core.CORE.name: core.CORE})  # type: ignore[arg-type]

    async def run() -> list[str]:
        await hub.configure(wait_s=5)
        out = [await hub.call("orion", "backups_list", {}, CallContext("brain", "t1")),
               await hub.call("orion", "backup_now", {}, CallContext("brain", "t1", source="voice")),
               await hub.call("orion", "backup_now", {}, CallContext("brain", "t2")),
               await hub.call("orion", "backup_now", {}, CallContext("hud")),
               await hub.call("orion", "backups_list", {}, CallContext("brain", "t3"))]
        await hub.stop()
        return out

    none, first, refused, by_user, listed = asyncio.run(run())
    assert none.startswith("0 backup(s) in ") and "None yet." in none and "At 03:30" in none
    assert first.startswith("Backed up: orion-backup-") and "files)." in first
    assert "backed up 1 minute(s) ago already" in refused
    assert by_user.startswith("Backed up: orion-backup-")
    assert listed.startswith("2 backup(s) in ") and listed.count("- orion-backup-") == 2 and "on this PC" in listed
    assert len(backup.archives(backup.backup_folder(world.cfg))) == 2


def test_the_command_line_backs_up_lists_checks_and_restores(world: World) -> None:
    from click.testing import CliRunner

    from orion.__main__ import main

    runner = CliRunner()
    made_ = runner.invoke(main, ["backup"])
    assert made_.exit_code == 0 and "Backed up: orion-backup-" in made_.output, made_.output
    listed = runner.invoke(main, ["backup", "--list"])
    assert listed.exit_code == 0 and listed.output.count("orion-backup-") == 1 and "MB" in listed.output
    assert "is whole" in runner.invoke(main, ["restore", "latest", "--check"]).output
    running = runner.invoke(main, ["restore", "latest", "--yes"])  # the test holds the database open: he is "running"
    assert running.exit_code == 1 and "Restore failed: Orion is running" in running.output
    world.store.close()
    shutil.rmtree(world.data / "memory")
    declined = runner.invoke(main, ["restore", "latest"], input="n\n")
    assert declined.exit_code == 1 and not (world.data / "memory").exists()
    restored = runner.invoke(main, ["restore", "latest"], input="y\n")
    assert restored.exit_code == 0 and "Restored 10 files." in restored.output and "before-restore-" in restored.output
    assert "enter your keys again" in restored.output and (world.data / "memory/USER.md").is_file()
    missing = runner.invoke(main, ["restore", str(world.data / "nope.tar.gz")])
    assert missing.exit_code == 1 and "There is no backup at" in missing.output
    nothing = runner.invoke(main, ["restore", "restic", "--check"])
    assert nothing.exit_code == 1 and "--check works on an archive" in nothing.output


def test_the_command_line_says_when_restic_cannot_be_used(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """The archive is made and the restic problem is said (and the exit code is not a success)."""
    from click.testing import CliRunner

    from orion.__main__ import main

    world.cfg.backup.restic_repository = "repo"
    world.cfg.secrets.store = "files"  # (restic's password is looked for in the vault: not in this machine's keyring)
    world.config_path.write_text(render(world.cfg))  # the command line reads the settings file, not this object
    monkeypatch.setattr(backup, "profile", lambda: SimpleNamespace(restic="/usr/bin/restic", has=lambda need: True))
    result = CliRunner().invoke(main, ["backup"])
    assert result.exit_code == 1 and "Backed up: orion-backup-" in result.output
    assert "restic: restic needs its password" in result.output
    assert len(backup.archives(backup.backup_folder(world.cfg))) == 1


def test_orion_check_reports_the_backups(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    from orion.__main__ import _check_backups

    lines: list[tuple[bool, str]] = []

    def report(ok: bool, what: str, fix: str = "") -> None:
        lines.append((ok, what))

    _check_backups(world.cfg, report)
    assert lines[-1] == (True, "backups: none yet (the schedule, 30 3 * * *, makes one)")
    world.cfg.backup.schedule = ""
    _check_backups(world.cfg, report)
    assert lines[-1] == (False, "backups: none yet")
    made(world, backup.archive_name(time.time() - 3 * 86400))
    _check_backups(world.cfg, report)
    assert lines[-1][0] and "1 in " in lines[-1][1] and "3 day(s) old" in lines[-1][1]
    world.cfg.backup.restic_repository = "repo"
    monkeypatch.setattr("orion.host.profile", lambda: SimpleNamespace(has=lambda need: False))
    _check_backups(world.cfg, report)
    assert lines[-1] == (False, "restic, for the encrypted copy of the backups off this machine")
