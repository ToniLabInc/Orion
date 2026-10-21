"""plugins: where plugins come from, unpacking what is fetched or uploaded without trusting it (zip-slip, links,
devices, limits), reading a plugin's manifest and both shapes of its .mcp.json, its ${VAR}s as the fields of its
connectors (never read from Orion's environment), and what a plugin's folder brings and what Orion never uses."""

import io
import json
import stat
import tarfile
import warnings
import zipfile
from pathlib import Path

import pytest

from orion import plugins as p
from orion.base import repo_url

SHA = "a" * 40
LIMITS = p.Limits(download=1 << 20, unpacked=1 << 20, files=100, timeout_s=5)


def tar_file(path: Path, files: dict[str, bytes | str], *, root: str = "repo-main", comment: str = "",
             extra: list[tarfile.TarInfo] | None = None) -> Path:
    """A gzip tarball as codeload makes them: a root folder, a pax header naming the commit."""
    with tarfile.open(path, "w:gz", format=tarfile.PAX_FORMAT, pax_headers={"comment": comment} if comment else {}) as t:
        for name, content in files.items():
            data = content.encode() if isinstance(content, str) else content
            info = tarfile.TarInfo(f"{root}/{name}" if root else name)
            info.size = len(data)
            info.mode = 0o755 if name.endswith(".sh") else 0o644
            t.addfile(info, io.BytesIO(data))
        for info in extra or ():
            t.addfile(info)
    return path


def zip_bytes(files: dict[str, bytes | str], links: dict[str, str] | None = None) -> bytes:
    """A zip of files (and, for the tests, links, which Orion never unpacks)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        for name, content in files.items():
            z.writestr(name, content)
        for name, target in (links or {}).items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(info, target)
    return buffer.getvalue()


def test_a_repository_is_written_in_full_and_never_with_credentials() -> None:
    assert repo_url("anthropics/skills") == "https://github.com/anthropics/skills"
    assert repo_url(" https://github.com/anthropics/skills.git/ ") == "https://github.com/anthropics/skills"
    assert repo_url("https://gitlab.com/me/market") == "https://gitlab.com/me/market"
    for bad in ("", "skills", "a/b/c", "http://example.org/x", "git@github.com:a/b.git", "https://me:pw@example.org/x",
                "https://example.org/a/../b", "ftp://example.org/x", "https://example.org"):
        with pytest.raises(ValueError, match="isn't a repository"):
            repo_url(bad)


def test_where_a_marketplaces_plugin_comes_from() -> None:
    market = p.GitRef("https://github.com/acme/market", sha=SHA)
    inside = p.plugin_source("./external_plugins/linear", market=market)
    assert inside == p.GitRef("https://github.com/acme/market", sha=SHA, path="external_plugins/linear")
    assert p.plugin_source("tools/x", market=market, plugin_root="./plugins") == p.GitRef(
        market.url, sha=SHA, path="plugins/tools/x")
    far = p.plugin_source({"source": "url", "url": "https://github.com/acme/far.git", "sha": "b" * 40}, market=market)
    assert far == p.GitRef("https://github.com/acme/far", sha="b" * 40)
    sub = p.plugin_source({"source": "git-subdir", "url": "https://github.com/acme/mono.git", "path": "plugins/sub",
                           "ref": "main", "sha": "c" * 40}, market=market)
    assert sub == p.GitRef("https://github.com/acme/mono", ref="main", sha="c" * 40, path="plugins/sub")
    short = p.plugin_source({"source": "github", "repo": "acme/short"}, market=market)
    assert short == p.GitRef("https://github.com/acme/short")
    other = p.plugin_source({"source": "url", "url": "https://git.example.org/acme/x.git"}, market=market)
    assert isinstance(other, p.GitRef) and other.github is None and other.web == "https://git.example.org/acme/x.git"
    assert far.web == f"https://github.com/acme/far/tree/{'b' * 40}"  # type: ignore[union-attr]
    assert sub.web.endswith("/tree/" + "c" * 40 + "/plugins/sub")  # type: ignore[union-attr]
    for raw, why in [({"source": "npm", "package": "x"}, "npm"), ({"source": "ftp"}, "doesn't know"),
                     ({"source": "url", "url": "https://github.com/a/b", "sha": "nothex"}, "isn't one"),
                     ({"source": "url", "url": "http://insecure.example/x"}, "isn't a repository"),
                     ({"source": "git-subdir", "url": "https://github.com/a/b", "path": "../x"}, "outside"),
                     ("../elsewhere", "outside the marketplace"), ("/etc", "outside the marketplace"),
                     (None, "no source")]:
        reason = p.plugin_source(raw, market=market)
        assert isinstance(reason, str) and why in reason, raw


# ---------------------------------------------------------------------------------------------------- unpacking
def test_a_tarball_unpacks_its_regular_files_and_names_its_commit(tmp_path: Path) -> None:
    archive = tar_file(tmp_path / "a.tgz", {"README.md": "hi", "plugins/x/run.sh": "#!/bin/sh", "plugins/x/a/b.txt": "b",
                                            "other/y.txt": "y"}, comment=SHA)
    dest = tmp_path / "out"
    assert p.unpack_tar(archive, dest, LIMITS, strip=1) == SHA
    assert sorted(f.relative_to(dest).as_posix() for f in dest.rglob("*") if f.is_file()) == [
        "README.md", "other/y.txt", "plugins/x/a/b.txt", "plugins/x/run.sh"]
    assert stat.S_IMODE((dest / "plugins/x/run.sh").stat().st_mode) == 0o755  # only what asked to run, may run
    assert stat.S_IMODE((dest / "README.md").stat().st_mode) == 0o644
    only = tmp_path / "only"
    p.unpack_tar(archive, only, LIMITS, strip=1, within="plugins/x")
    assert sorted(f.relative_to(only).as_posix() for f in only.rglob("*") if f.is_file()) == ["a/b.txt", "run.sh"]
    with pytest.raises(ValueError, match="no folder plugins/nope"):
        p.unpack_tar(archive, tmp_path / "none", LIMITS, strip=1, within="plugins/nope")
    assert p.unpack_tar(tar_file(tmp_path / "b.tgz", {"a": "1"}, comment="not a commit"), tmp_path / "c", LIMITS) == ""


@pytest.mark.parametrize("name", ["repo-main/../evil.txt", "/etc/evil.txt", "repo-main/a/../../evil.txt",
                                  "repo-main/..", "C:/evil.txt", "..\\evil.txt"])
def test_a_tarball_member_outside_its_folder_stops_everything(tmp_path: Path, name: str) -> None:
    """Zip-slip: nothing is written anywhere, inside or outside."""
    archive = tmp_path / "evil.tgz"
    with tarfile.open(archive, "w:gz") as t:
        good = tarfile.TarInfo("repo-main/ok.txt")
        good.size = 2
        t.addfile(good, io.BytesIO(b"ok"))
        bad = tarfile.TarInfo(name)
        bad.size = 4
        t.addfile(bad, io.BytesIO(b"evil"))
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    with pytest.raises(ValueError, match="outside its folder"):
        p.unpack_tar(archive, sandbox / "dest", LIMITS, strip=1)
    assert not (tmp_path / "evil.txt").exists() and not (sandbox / "evil.txt").exists()


def test_links_devices_and_pipes_in_a_tarball_are_never_made(tmp_path: Path) -> None:
    extra = []
    for name, kind, target in (("link", tarfile.SYMTYPE, "/etc/passwd"), ("hard", tarfile.LNKTYPE, "repo-main/ok.txt"),
                               ("dev", tarfile.CHRTYPE, ""), ("pipe", tarfile.FIFOTYPE, "")):
        info = tarfile.TarInfo(f"repo-main/{name}")
        info.type, info.linkname = kind, target
        extra.append(info)
    archive = tar_file(tmp_path / "links.tgz", {"ok.txt": "ok"}, extra=extra)
    dest = tmp_path / "out"
    p.unpack_tar(archive, dest, LIMITS, strip=1)
    assert [f.name for f in dest.iterdir()] == ["ok.txt"]


def test_an_archive_past_its_limits_is_refused(tmp_path: Path) -> None:
    many = tar_file(tmp_path / "many.tgz", {f"f{i}": "x" for i in range(5)})
    with pytest.raises(ValueError, match="more than 3 files"):
        p.unpack_tar(many, tmp_path / "a", p.Limits(1 << 20, 1 << 20, 3, 5), strip=1)
    folders = tmp_path / "folders.tgz"
    with tarfile.open(folders, "w:gz") as t:
        for i in range(5):
            info = tarfile.TarInfo(f"repo-main/d{i}")
            info.type = tarfile.DIRTYPE
            t.addfile(info)
    with pytest.raises(ValueError, match="more than 3 files"):  # folders count too
        p.unpack_tar(folders, tmp_path / "b", p.Limits(1 << 20, 1 << 20, 3, 5), strip=1)
    big = tar_file(tmp_path / "big.tgz", {"a": "x" * (600 << 10), "b": "x" * (600 << 10)})  # 1.2 MB, a few KB packed
    with pytest.raises(ValueError, match="unpacks to more than 1 MB"):
        p.unpack_tar(big, tmp_path / "c", p.Limits(1 << 20, 1 << 20, 100, 5), strip=1)
    assert not (tmp_path / "c" / "b").exists()  # it stopped at the file that went over


def test_a_file_that_holds_more_than_it_says_is_cut_off(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="holds more than it says"):
        p._write(io.BytesIO(b"x" * 100), tmp_path / "f", 10)


def test_names_that_clash_and_files_that_are_not_archives_are_told_plainly(tmp_path: Path) -> None:
    clash = tmp_path / "clash.tgz"
    with tarfile.open(clash, "w:gz") as t:
        for name in ("repo-main/a", "repo-main/a/b"):
            info = tarfile.TarInfo(name)
            info.size = 1
            t.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="twice, or as a file and a folder"):
        p.unpack_tar(clash, tmp_path / "out", LIMITS, strip=1)
    junk = tmp_path / "junk.tgz"
    junk.write_bytes(b"this is not a tarball")
    with pytest.raises(ValueError, match="isn't a readable archive"):
        p.unpack_tar(junk, tmp_path / "out2", LIMITS)
    with pytest.raises(ValueError, match="isn't a zip"):
        p.unpack_zip(b"this is not a zip", tmp_path / "z", LIMITS)


def test_a_zip_unpacks_files_and_leaves_links_out(tmp_path: Path) -> None:
    data = zip_bytes({".claude-plugin/plugin.json": "{}", "skills/a/SKILL.md": "x", "dir/": ""},
                     links={"skills/evil": "/etc/passwd"})
    dest = tmp_path / "out"
    p.unpack_zip(data, dest, LIMITS)
    assert sorted(f.relative_to(dest).as_posix() for f in dest.rglob("*") if f.is_file()) == [
        ".claude-plugin/plugin.json", "skills/a/SKILL.md"]
    assert (dest / "dir").is_dir() and not (dest / "skills/evil").exists()


@pytest.mark.parametrize("name", ["../evil.txt", "/etc/evil.txt", "a/../../evil.txt", "C:\\evil.txt", "..\\evil.txt"])
def test_a_zip_member_outside_its_folder_stops_everything(tmp_path: Path, name: str) -> None:
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    with pytest.raises(ValueError, match="outside its folder"):
        p.unpack_zip(zip_bytes({"ok.txt": "ok", name: "evil"}), sandbox / "dest", LIMITS)
    assert not (tmp_path / "evil.txt").exists() and not (sandbox / "evil.txt").exists()


def test_a_zip_past_its_limits_or_with_a_name_twice_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="more than 2 files"):
        p.unpack_zip(zip_bytes({"a": "1", "b": "2", "c": "3"}), tmp_path / "a", p.Limits(1 << 20, 1 << 20, 2, 5))
    with pytest.raises(ValueError, match="unpacks to more than"):
        p.unpack_zip(zip_bytes({"a": "x" * 2000}), tmp_path / "b", p.Limits(1 << 20, 1000, 100, 5))
    twice = io.BytesIO()
    with zipfile.ZipFile(twice, "w") as z, warnings.catch_warnings():
        warnings.simplefilter("ignore")  # the zipfile module itself warns about a name written twice
        z.writestr("a", "1")
        z.writestr("a", "2")
    with pytest.raises(ValueError, match="twice"):
        p.unpack_zip(twice.getvalue(), tmp_path / "c", LIMITS)


def test_a_folders_hash_follows_its_content_alone(tmp_path: Path) -> None:
    one, two = tmp_path / "one", tmp_path / "two"
    for folder, order in ((one, ("a", "b")), (two, ("b", "a"))):
        for name in order:
            (folder / name).parent.mkdir(parents=True, exist_ok=True)
            (folder / name).write_text(name)
    (two / "link").symlink_to("/etc/passwd")  # a link is not content
    assert p.digest(one) == p.digest(two) and len(p.digest(one)) == 64
    (two / "a").write_text("changed")
    assert p.digest(one) != p.digest(two)
    (two / "a").write_text("a")
    (two / "c").mkdir()
    (two / "c" / "d").write_text("d")
    assert p.digest(one) != p.digest(two)


# ---------------------------------------------------------------------------------------------------- manifests
LINEAR = {"linear": {"type": "http", "url": "https://mcp.linear.app/mcp"}}
GITHUB = {"github": {"type": "http", "url": "https://api.githubcopilot.com/mcp/",
                     "headers": {"Authorization": "Bearer ${GITHUB_PERSONAL_ACCESS_TOKEN}"}}}
DISCORD = {"mcpServers": {"discord": {"command": "bun", "args": ["run", "--cwd", "${CLAUDE_PLUGIN_ROOT}", "start"]}}}


def test_both_shapes_of_a_mcp_json_give_the_same_servers() -> None:
    assert p.servers_of(LINEAR) == LINEAR and p.servers_of({"mcpServers": LINEAR}) == LINEAR
    assert p.servers_of(DISCORD)["discord"]["command"] == "bun"
    assert p.servers_of(None) == {} and p.servers_of([1]) == {} and p.servers_of({"a": "not a server"}) == {}
    assert p.servers_of({"mcpServers": "x"}) == {}


def test_a_servers_settings_from_the_plugins_spec() -> None:
    assert p.server_settings(LINEAR["linear"]) == {"url": "https://mcp.linear.app/mcp"}
    assert p.server_settings({"url": "https://x.test/sse", "type": "sse", "headers": {"A": "b"}}) == {
        "url": "https://x.test/sse", "transport": "sse", "headers": {"A": "b"}}
    assert p.server_settings({"url": "https://x.test/mcp"}) == {"url": "https://x.test/mcp"}  # no type: http
    stdio = p.server_settings({"command": "uvx", "args": ["a", 1], "env": {"K": "v"}, "cwd": "/x"})
    assert stdio == {"command": "uvx", "args": ["a", "1"], "env": {"K": "v"}, "cwd": "/x"}
    for bad, why in [({"type": "http", "url": "ftp://x"}, "http"), ({"type": "websocket", "url": "ws://x"}, "can't connect"),
                     ({"command": ""}, "command"), ({"command": "x", "args": "not a list"}, "command"),
                     ({}, "neither a command")]:
        reason = p.server_settings(bad)
        assert isinstance(reason, str) and why in reason, bad
    slack = p.server_settings({"type": "http", "url": "https://mcp.slack.com/mcp",
                               "oauth": {"clientId": "1601185624273.8899143856786", "callbackPort": 3118}})
    assert slack == {"url": "https://mcp.slack.com/mcp",  # the maker's own client ID is not Orion's to use
                     "oauth": {"client_id": "${OAUTH_CLIENT_ID}", "client_secret": "${OAUTH_CLIENT_SECRET}"}}


def test_a_marketplace_entry_that_defines_the_plugin_overrides_its_manifest(tmp_path: Path) -> None:
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin/plugin.json").write_text(json.dumps({"name": "x", "version": "1", "skills": ["./old"]}))
    entry = {"name": "x", "source": "./x", "strict": False, "skills": ["./skills/a"], "description": "D"}
    merged = p.manifest_of(tmp_path, entry)
    assert merged["skills"] == ["./skills/a"] and merged["version"] == "1" and "source" not in merged
    strict = p.manifest_of(tmp_path, {"name": "x", "skills": ["./skills/a"], "description": "D"})
    assert strict["skills"] == ["./old"] and strict["description"] == "D"  # its own plugin.json wins
    assert p.manifest_of(tmp_path / "missing") == {} and p.author_of({"author": {"name": "Ann"}}) == "Ann"
    assert p.author_of({"author": "Bob"}) == "Bob" and p.author_of({}) == ""


# ---------------------------------------------------------------------------------------------------- fields
def test_placeholders_become_the_fields_of_the_connector() -> None:
    settings = {"url": "https://x.test/${REGION:-eu}/mcp", "headers": {"Authorization": "Bearer ${GITHUB_TOKEN}"},
                "args": ["--root", "${CLAUDE_PLUGIN_ROOT}", "${CLAUDE_PLUGIN_DATA}"], "env": {"DB": "${DB_PASSWORD}"}}
    assert p.placeholders(settings) == {"REGION": "eu", "GITHUB_TOKEN": None, "DB_PASSWORD": None}
    fields = {f["key"]: f for f in p.placeholder_fields(p.placeholders(settings))}
    assert fields["REGION"]["kind"] == "text" and fields["REGION"]["required"] is False
    assert fields["REGION"]["placeholder"] == "eu"
    assert fields["GITHUB_TOKEN"]["kind"] == "secret" and fields["GITHUB_TOKEN"]["secret"] is True
    assert fields["DB_PASSWORD"]["required"] is True and fields["GITHUB_TOKEN"]["label"] == "Github token"
    assert [p.secret_name(v) for v in ("API_KEY", "APIKEY", "MY_TOKEN", "CLIENT_SECRET", "DB_PASSWORD", "REGION",
                                       "KEYBOARD", "TOKENS")] == [True, True, True, True, True, False, False, False]
    own = {f["key"]: f for f in p.placeholder_fields(p.placeholders({"oauth": {"client_id": "${OAUTH_CLIENT_ID}",
                                                                              "client_secret": "${OAUTH_CLIENT_SECRET}"}}))}
    assert own["OAUTH_CLIENT_ID"]["label"] == "OAuth client ID" and "{redirect}" in own["OAUTH_CLIENT_ID"]["help"]
    assert own["OAUTH_CLIENT_SECRET"]["secret"] is True


def test_filling_a_connector_uses_its_own_fields_and_never_orions_environment(tmp_path: Path,
                                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """A placeholder named like one of Orion's own variables is a field of the connector, not that variable."""
    monkeypatch.setenv("GITHUB_TOKEN", "orions-own-token")
    monkeypatch.setenv("REGION", "orions-region")
    settings = {"url": "https://x.test/${REGION:-eu}/mcp", "headers": {"Authorization": "Bearer ${GITHUB_TOKEN}"},
                "args": ["--root", "${CLAUDE_PLUGIN_ROOT}", "${CLAUDE_PLUGIN_DATA}"], "values": {"REGION": "us"},
                "plugin": "gh", "description": "${NOT_A_FIELD}", "enabled": True}
    vault = {"connector.gh.github_token": "the-fields-token"}
    filled = p.fill(settings, connector="gh", root=tmp_path / "plugin", data=tmp_path / "data", secret=vault.get)
    assert filled["url"] == "https://x.test/us/mcp" and filled["headers"] == {"Authorization": "Bearer the-fields-token"}
    assert filled["args"] == ["--root", str(tmp_path / "plugin"), str(tmp_path / "data")]
    assert filled["values"] == {"REGION": "us"} and filled["description"] == "${NOT_A_FIELD}"  # not a connection key
    assert "orions" not in json.dumps(filled)
    del settings["values"]
    assert p.fill(settings, connector="gh", root=tmp_path, data=tmp_path, secret=vault.get)["url"] == (
        "https://x.test/eu/mcp")  # the default, not the environment's
    with pytest.raises(p.SetupNeeded, match="Github token isn't set"):
        p.fill(settings, connector="gh", root=tmp_path, data=tmp_path, secret={}.get)
    assert issubclass(p.SetupNeeded, Exception) and p.vault_name("gh", "GITHUB_TOKEN") == "connector.gh.github_token"


def test_connector_ids_avoid_what_is_taken() -> None:
    taken = {"linear"}
    assert p.connector_id("Linear", "lin", taken.__contains__) == "lin-linear"
    assert p.connector_id("sentry", "sentry-plugin", taken.__contains__) == "sentry"
    assert p.connector_id("My Server!", "x", set().__contains__) == "my-server"
    with pytest.raises(ValueError, match="can't be added"):
        p.connector_id("a", "b", lambda _: True)


# ---------------------------------------------------------------------------------------------------- parts
def build_plugin(folder: Path) -> Path:
    """A plugin with a bit of everything."""
    files = {
        ".claude-plugin/plugin.json": json.dumps({"name": "full", "version": "2.1", "description": "All of it.",
                                                  "author": {"name": "Ann"}}),
        "skills/triage/SKILL.md": "---\nname: triage\ndescription: Triage issues.\nallowed-tools: Read Bash\n---\nDo it.",
        "skills/triage/references/api.md": "# api",
        "commands/ship.md": "---\ndescription: Ship it.\n---\nShip $ARGUMENTS now.",
        ".mcp.json": json.dumps({"mcpServers": {"tracker": {"type": "http", "url": "https://t.test/mcp"},
                                                "local": {"command": "uvx", "args": ["x", "${CLAUDE_PLUGIN_ROOT}"],
                                                          "env": {"API_KEY": "${API_KEY}"}},
                                                "broken": {"type": "carrier-pigeon"}}}),
        "agents/reviewer.md": "---\nname: reviewer\n---\nReview.", "agents/fixer.md": "x",
        "hooks/hooks.json": "{}", "output-styles/terse.md": "x", ".lsp.json": json.dumps({"python": {"command": "x"}}),
        "mods/pane/mod.json": "{}",
    }
    for name, content in files.items():
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(content)
    return folder


def test_what_a_plugin_brings_and_what_orion_never_uses(tmp_path: Path) -> None:
    folder = build_plugin(tmp_path / "full")
    parts = p.parts_of(folder, p.manifest_of(folder), "full")
    assert parts.skills == ("triage",) and parts.commands == ("ship",)
    assert [s["name"] for s in parts.servers] == ["tracker", "local"]
    assert parts.servers[1]["settings"] == {"command": "uvx", "args": ["x", "${CLAUDE_PLUGIN_ROOT}"],
                                            "env": {"API_KEY": "${API_KEY}"}}
    assert sorted(parts.unused) == ["Agent: fixer", "Agent: reviewer", "Hooks", "LSP server: python", "Mod: pane",
                                    "Output style: terse"]
    assert parts.problems == ("MCP server broken: its kind 'carrier-pigeon' is one Orion can't connect to",)
    assert p.Parts.from_dict(parts.as_dict()) == p.Parts(parts.skills, parts.commands, tuple(
        dict(s) for s in parts.servers), parts.unused, parts.problems)  # type: ignore[arg-type]


def test_a_manifest_can_name_other_places_but_never_outside_the_plugin(tmp_path: Path) -> None:
    folder = tmp_path / "named"
    (folder / ".claude-plugin").mkdir(parents=True)
    (folder / "elsewhere/skills/a").mkdir(parents=True)
    (folder / "elsewhere/skills/a/SKILL.md").write_text("---\nname: a\ndescription: A.\n---\nx")
    (folder / "my-servers.json").write_text(json.dumps(LINEAR))
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside/SKILL.md").write_text("---\nname: out\ndescription: Out.\n---\nx")
    manifest = {"name": "named", "skills": ["./elsewhere/skills", "../outside"], "mcpServers": "./my-servers.json"}
    parts = p.parts_of(folder, manifest, "named")
    assert parts.skills == ("a",) and [s["name"] for s in parts.servers] == ["linear"]
    inline = p.parts_of(folder, {"mcpServers": GITHUB}, "named")
    assert [s["name"] for s in inline.servers] == ["github"]
    roots = p.roots_of(folder, manifest, "named")
    assert [r.path for r in roots] == [folder / "elsewhere/skills"] and roots[0].plugin == "named"
