"""library: marketplaces are fetched once and then only asked whether they changed, cached for the next start and kept
when the network is down; Discover filters them; a plugin installs from its marketplace's own folders or another
repository at its pinned commit, updates without losing what the user set, uploads from a zip, switches off and goes
away with its connectors and their secrets; skills are found in sources and imported. The network is a fake; the
settings file is real."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_plugins import build_plugin, tar_file, zip_bytes

from orion import plugins as p
from orion.base import EventBus
from orion.config import Settings
from orion.secrets import Vault
from orion.skills import Skills

MARKET = "https://github.com/acme/market"
MARKET_TAR = "https://codeload.github.com/acme/market/tar.gz/HEAD"
MARKET_SHA, FAR_SHA, SUB_SHA = "1" * 40, "2" * 40, "3" * 40
RESERVED = {"system", "web", "orion", "notion", "mail"}  # Orion's own groups and the directory's ids


class Net:
    """The network: archives by URL (a repository's archive answers for any ref of it, as codeload does), ETags, the
    URLs asked for, and URLs that are down."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.etags: dict[str, str] = {}
        self.asked: list[tuple[str, str]] = []
        self.down: set[str] = set()

    def serve(self, repo: str, data: bytes, tag: str = "") -> None:
        """A GitHub repository's archive (``owner/name``), whatever ref is asked for."""
        self.files[f"https://codeload.github.com/{repo}/tar.gz/"] = data
        self.etags[f"https://codeload.github.com/{repo}/tar.gz/"] = tag

    def __call__(self, url: str, target: Path, _limits: p.Limits, etag: str) -> tuple[int, str]:
        self.asked.append((url, etag))
        key = url if url in self.files else next((k for k in self.files if k.endswith("/tar.gz/") and
                                                  url.startswith(k)), url)
        if url in self.down or key in self.down:
            raise p.FetchError(f"{url} couldn't be reached (offline)")
        if key not in self.files:
            raise p.FetchError(f"{url} answered 404")
        tag = self.etags.get(key, "")
        if etag and etag == tag:
            return 304, etag
        target.write_bytes(self.files[key])
        return 200, tag


class Host:
    """The running Orion as the library needs it: a real settings file, the skills, a vault, a recorder."""

    def __init__(self, tmp_path: Path) -> None:
        self.settings = Settings.open(tmp_path / "data" / "config.yaml")
        self.settings.sync_file()
        self.cfg = self.settings.cfg
        self.cfg.skills.sources = []  # the default one is Anthropic's: the tests that look in a source name their own
        self.skills = Skills.open(self.cfg)
        self.bus = EventBus()
        self.events: list[dict[str, Any]] = []
        self.bus.subscribe(self.events.append)
        self.vault = Vault("files", tmp_path / "vault")
        self.records: list[dict[str, Any]] = []
        self.activity = SimpleNamespace(record=lambda kind, **data: self.records.append({"kind": kind, **data}))

    def reserved(self, name: str) -> bool:
        return name in RESERVED

    async def apply_own(self, patch: dict[str, Any]) -> dict[str, Any]:
        changes = self.settings.validate(patch, actor="hud", own=True)
        changed = await self.settings.commit(dict(changes)) if changes else []
        self.skills.invalidate()
        return {"changed": changed}

    def titles(self, kind: str) -> list[str]:
        return [r["title"] for r in self.records if r["kind"] == kind]


def marketplace_json(*entries: dict[str, Any], name: str = "test-market") -> str:
    return json.dumps({"name": name, "plugins": list(entries)})


def market_archive(tmp_path: Path, entries: list[dict[str, Any]], plugins: dict[str, dict[str, str]],
                   name: str = "market.tgz") -> bytes:
    files: dict[str, bytes | str] = {".claude-plugin/marketplace.json": marketplace_json(*entries)}
    for plugin, content in plugins.items():
        files |= {f"external_plugins/{plugin}/{path}": text for path, text in content.items()}
    return tar_file(tmp_path / name, files, root="market-main", comment=MARKET_SHA).read_bytes()


LINEAR_FILES = {".claude-plugin/plugin.json": json.dumps({"name": "linear", "description": "Linear issues."}),
                ".mcp.json": json.dumps({"linear": {"type": "http", "url": "https://mcp.linear.app/mcp"}}),
                "skills/triage/SKILL.md": "---\nname: triage\ndescription: Triage issues.\n---\nDo it.",
                "commands/standup.md": "Write the standup for $ARGUMENTS."}
GITHUB_FILES = {".claude-plugin/plugin.json": json.dumps({"name": "github"}),
                ".mcp.json": json.dumps({"github": {"type": "http", "url": "https://api.githubcopilot.com/mcp/",
                                                    "headers": {"Authorization": "Bearer ${GITHUB_PERSONAL_ACCESS_TOKEN}"}}})}
ENTRIES = [
    {"name": "linear", "description": "Linear issues and projects.", "category": "productivity",
     "source": "./external_plugins/linear", "homepage": "https://linear.app", "author": {"name": "Linear"}},
    {"name": "github", "description": "GitHub.", "category": "development", "source": "./external_plugins/github"},
    {"name": "far", "description": "Far away.", "category": "development",
     "source": {"source": "url", "url": "https://github.com/acme/far.git", "sha": FAR_SHA}},
    {"name": "sub", "description": "In a monorepo.", "source": {"source": "git-subdir", "path": "plugins/sub",
                                                                 "url": "https://github.com/acme/mono.git",
                                                                 "ref": "main", "sha": SUB_SHA}},
    {"name": "pkg", "description": "From npm.", "source": {"source": "npm", "package": "x"}},
]


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    net = Net()
    monkeypatch.setattr(p, "_download", net)
    host = Host(tmp_path)
    host.cfg.plugins.marketplaces = {"test-market": MARKET}
    net.serve("acme/market", market_archive(tmp_path, ENTRIES, {"linear": LINEAR_FILES, "github": GITHUB_FILES}),
              '"market-1"')
    far = tar_file(tmp_path / "far.tgz", {".claude-plugin/plugin.json": json.dumps({"name": "far", "version": "0.3"}),
                                          "skills/far-skill/SKILL.md": "---\nname: far-skill\ndescription: Far.\n---\nx",
                                          ".mcp.json": json.dumps({"mcpServers": {"system": {"url": "https://far.test/mcp"}}})},
                   root=f"far-{FAR_SHA}", comment=FAR_SHA)
    net.files[f"https://codeload.github.com/acme/far/tar.gz/{FAR_SHA}"] = far.read_bytes()
    sub = tar_file(tmp_path / "sub.tgz", {"plugins/sub/.claude-plugin/plugin.json": json.dumps({"name": "sub"}),
                                          "plugins/sub/skills/s/SKILL.md": "---\nname: s\ndescription: S.\n---\nx",
                                          "plugins/other/README.md": "not mine"}, root=f"mono-{SUB_SHA}", comment=SUB_SHA)
    net.files[f"https://codeload.github.com/acme/mono/tar.gz/{SUB_SHA}"] = sub.read_bytes()
    return SimpleNamespace(net=net, host=host, lib=p.Library(host), tmp=tmp_path)  # type: ignore[arg-type]


def run[T](coro: Any) -> T:
    return asyncio.run(coro)  # type: ignore[no-any-return]


def test_a_marketplace_is_fetched_once_then_only_asked_if_it_changed(world: SimpleNamespace) -> None:
    lib, net, host = world.lib, world.net, world.host

    async def go() -> None:
        await lib.refresh()
        (market,) = lib.marketplaces()
        assert market["name"] == "test-market" and market["plugins"] == 5 and market["error"] == ""
        assert market["updated_at"] > 0 and net.asked == [(MARKET_TAR, "")]
        assert any(e["type"] == "plugins" for e in host.events)
        await lib.refresh()  # fresh: no request
        assert len(net.asked) == 1
        await lib.refresh(force=True)  # asked whether it changed: a 304
        assert net.asked[-1] == (MARKET_TAR, '"market-1"') and lib.marketplaces()[0]["plugins"] == 5

    run(go())
    cache = world.tmp / "data" / "plugins" / "cache" / "marketplaces" / "test-market.json"
    assert cache.is_file() and json.loads(cache.read_text())["sha"] == MARKET_SHA
    later = p.Library(host)  # Orion starts again: what was fetched is there without the network
    net.down.add(MARKET_TAR)
    assert later.marketplaces()[0]["plugins"] == 5 and len(net.asked) == 2


def test_a_marketplace_that_cant_be_fetched_keeps_what_was_known(world: SimpleNamespace) -> None:
    lib, net = world.lib, world.net

    async def go() -> None:
        await lib.refresh()
        net.down.add(MARKET_TAR)
        await lib.refresh(force=True)
        (market,) = lib.marketplaces()
        assert "couldn't be reached" in market["error"] and market["plugins"] == 5
        assert lib.discover()["items"]  # still browsable
        net.down.clear()
        await lib.refresh(force=True)
        assert lib.marketplaces()[0]["error"] == ""
        net.serve("acme/market", b"not an archive", '"market-2"')
        await lib.refresh(force=True)
        assert "isn't a readable archive" in lib.marketplaces()[0]["error"] and lib.marketplaces()[0]["plugins"] == 5

    run(go())


def test_a_marketplace_is_added_by_the_name_it_gives_itself(world: SimpleNamespace) -> None:
    lib, net, host = world.lib, world.net, world.host

    async def go() -> None:
        net.serve("acme/second", tar_file(
            world.tmp / "second.tgz", {".claude-plugin/marketplace.json": marketplace_json(
                {"name": "mine", "source": "./mine"}, name="second-market")}, root="second-main", comment=MARKET_SHA
        ).read_bytes())
        added = await lib.add_marketplace("acme/second")
        assert added["name"] == "second-market" and added["plugins"] == 1
        assert host.cfg.plugins.marketplaces["second-market"] == "https://github.com/acme/second"
        with pytest.raises(ValueError, match="isn't a repository"):
            await lib.add_marketplace("not a repo")
        net.serve("acme/market", tar_file(
            world.tmp / "clash.tgz", {".claude-plugin/marketplace.json": marketplace_json(name="second-market")},
            root="m", comment=MARKET_SHA).read_bytes())
        with pytest.raises(ValueError, match="There's a marketplace called second-market already"):
            await lib.add_marketplace("acme/market")
        net.serve("acme/empty", tar_file(world.tmp / "e.tgz", {"README.md": "x"}, root="e",
                                         comment=MARKET_SHA).read_bytes())
        with pytest.raises(ValueError, match="no .claude-plugin/marketplace.json"):
            await lib.add_marketplace("acme/empty")
        with pytest.raises(p.FetchError, match="404"):
            await lib.add_marketplace("acme/missing")
        await lib.remove_marketplace("second-market")
        assert "second-market" not in host.cfg.plugins.marketplaces and len(lib.marketplaces()) == 1
        with pytest.raises(LookupError):
            await lib.remove_marketplace("second-market")

    run(go())
    assert host.titles("plugin") == ["Added the second-market marketplace", "Removed the second-market marketplace"]


def test_discover_filters_what_can_be_installed(world: SimpleNamespace) -> None:
    lib, host = world.lib, world.host

    async def go() -> None:
        await lib.refresh()
        everything = lib.discover()
        assert [i["name"] for i in everything["items"]] == ["far", "github", "linear", "pkg", "sub"]
        assert everything["categories"] == ["development", "productivity"]
        linear = next(i for i in everything["items"] if i["name"] == "linear")
        assert linear == {"name": "linear", "description": "Linear issues and projects.", "category": "productivity",
                          "marketplace": "test-market", "author": "Linear", "homepage": "https://linear.app",
                          "installed": False, "components_known": True}
        assert next(i for i in everything["items"] if i["name"] == "far")["components_known"] is False
        assert [i["name"] for i in lib.discover(query="LINEAR project")["items"]] == ["linear"]
        assert [i["name"] for i in lib.discover(query="far")["items"]] == ["far"]  # a name match comes first
        assert [i["name"] for i in lib.discover(query="issues")["items"]] == ["linear"]
        assert [i["name"] for i in lib.discover(category="development")["items"]] == ["far", "github"]
        assert lib.discover(marketplace="elsewhere")["items"] == []
        await lib.install("test-market", "linear")
        assert next(i for i in lib.discover()["items"] if i["name"] == "linear")["installed"] is True
        host.cfg.plugins.marketplaces = {}  # a marketplace taken out of the settings stops listing
        assert lib.discover()["items"] == []

    run(go())


def test_installing_from_the_marketplaces_own_folders(world: SimpleNamespace) -> None:
    lib, host = world.lib, world.host

    async def go() -> dict[str, Any]:
        await lib.refresh()
        view = await lib.install("test-market", "linear")
        with pytest.raises(ValueError, match="installed already"):
            await lib.install("test-market", "linear")
        with pytest.raises(LookupError, match="no plugin called nope"):
            await lib.install("test-market", "nope")
        with pytest.raises(LookupError, match="no marketplace called elsewhere"):
            await lib.install("elsewhere", "linear")
        return view

    view = run(go())
    folder = world.tmp / "data" / "plugins" / "linear"
    assert (folder / "skills/triage/SKILL.md").is_file() and (folder / ".mcp.json").is_file()
    record = host.cfg.plugins.installed["linear"]
    assert record["source"] == "test-market" and len(record["sha"]) == 64 and record["enabled"] is True
    assert host.cfg.mcp_servers["linear"] == {"url": "https://mcp.linear.app/mcp", "plugin": "linear",
                                              "source_server": "linear", "description": "from the linear plugin"}
    assert view["description"] == "Linear issues." and view["author"] == "Linear" and view["enabled"] is True
    assert view["components"] == {"skills": ["linear:triage"], "commands": ["linear:standup"],
                                  "connectors": ["linear"], "unused": []}
    assert view["source"]["marketplace"] == "test-market" and view["trust"] == "unreviewed"
    assert "update" not in view  # it is what the marketplace has: the hash it was recorded with is the listing's
    assert set(host.skills.all()) >= {"linear:triage", "linear:standup"}
    assert "Do it." in host.skills.get("linear:triage").body and host.skills.get("linear:triage").source == "plugin"
    assert host.titles("plugin") == ["Installed the linear plugin"]
    assert next(r for r in host.records)["detail"]["connectors"] == "linear"
    assert any(e["type"] == "skill_changed" for e in host.events)
    assert (folder.parent / "cache").is_dir() and [f.name for f in folder.parent.iterdir() if f.name.startswith(".")] == []


def test_a_marketplaces_archive_is_kept_so_installing_from_it_needs_no_network(world: SimpleNamespace) -> None:
    """The archive downloaded with a marketplace is kept at its commit; a plugin of its own folders is installed from
    it offline, and a skill source's likewise; a damaged copy is fetched again; a removed marketplace's goes."""
    lib, net, host = world.lib, world.net, world.host
    host.cfg.skills.sources = ["https://github.com/acme/skills"]
    net.serve("acme/skills", skills_archive(world.tmp))
    kept = world.tmp / "data" / "plugins" / "cache" / "archives"

    async def go() -> None:
        await lib.refresh()
        assert sorted(f.name for f in kept.iterdir()) == [f"acme__market__{MARKET_SHA}.tar.gz",
                                                          f"acme__skills__{SHA_S}.tar.gz"]
        asked = len(net.asked)
        await lib.install("test-market", "linear")
        (found,) = [s for s in lib.discover_skills("pdf")]
        await lib.import_skills(url=found["url"])  # type: ignore[arg-type]
        assert len(net.asked) == asked  # both came out of the kept archives
        (kept / f"acme__market__{MARKET_SHA}.tar.gz").write_bytes(b"damaged")
        await lib.install("test-market", "github")
        assert len(net.asked) == asked + 1  # fetched again, and kept again
        assert (kept / f"acme__market__{MARKET_SHA}.tar.gz").stat().st_size > 100
        await lib.remove_marketplace("test-market")
        assert [f.name for f in kept.iterdir()] == [f"acme__skills__{SHA_S}.tar.gz"]
        host.cfg.skills.sources = []
        await lib.refresh(force=True)
        assert list(kept.iterdir()) == []

    run(go())


def test_only_a_commit_is_kept_and_a_newer_one_replaces_the_older(tmp_path: Path) -> None:
    cache = tmp_path / "archives"
    first, second = tmp_path / "first.tgz", tmp_path / "second.tgz"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    p.keep_archive(cache, ("acme", "market"), "a" * 40, first)
    p.keep_archive(cache, ("acme", "other"), "b" * 40, first)
    p.keep_archive(cache, ("acme", "market"), "c" * 40, second)
    p.keep_archive(cache, ("acme", "market"), "not-a-commit", second)  # never a name of the caller's making
    assert sorted(f.name for f in cache.iterdir()) == [f"acme__market__{'c' * 40}.tar.gz",
                                                      f"acme__other__{'b' * 40}.tar.gz"]
    assert p.archive_path(cache, ("acme", "market"), "c" * 40).read_bytes() == b"two"
    p.drop_archives(cache, [("acme", "other")])
    assert [f.name for f in cache.iterdir()] == [f"acme__other__{'b' * 40}.tar.gz"]
    p.drop_archives(tmp_path / "nowhere", [])  # no folder, nothing to do


def test_a_directory_marketplace_makes_its_plugins_reviewed(world: SimpleNamespace) -> None:
    host, lib, net = world.host, world.lib, world.net
    official = "https://github.com/anthropics/claude-plugins-official"
    host.cfg.plugins.marketplaces = {"claude-plugins-official": official}
    net.serve("anthropics/claude-plugins-official",
              market_archive(world.tmp, [ENTRIES[0]], {"linear": LINEAR_FILES}, name="official.tgz"))
    assert p.trust_of("claude-plugins-official", host.cfg.plugins.marketplaces) == "reviewed"
    assert p.trust_of("test-market", {"test-market": MARKET}) == "unreviewed" and p.trust_of(
        "upload", host.cfg.plugins.marketplaces) == "unreviewed"
    assert p.trust_of("claude-plugins-official", {"claude-plugins-official": "https://evil.example/anthropics/"
                                                  "claude-plugins-official"}) == "unreviewed"

    async def go() -> dict[str, Any]:
        await lib.refresh()
        return await lib.install("claude-plugins-official", "linear")

    assert run(go())["trust"] == "reviewed"


def test_a_plugin_from_another_repository_is_fetched_at_its_pinned_commit(world: SimpleNamespace) -> None:
    lib, host, net = world.lib, world.host, world.net

    async def go() -> None:
        await lib.refresh()
        far = await lib.install("test-market", "far")
        assert host.cfg.plugins.installed["far"]["sha"] == FAR_SHA and far["version"] == "0.3"
        assert net.asked[-1][0] == f"https://codeload.github.com/acme/far/tar.gz/{FAR_SHA}"
        sub = await lib.install("test-market", "sub")
        assert host.cfg.plugins.installed["sub"]["sha"] == SUB_SHA
        assert net.asked[-1][0] == f"https://codeload.github.com/acme/mono/tar.gz/{SUB_SHA}"
        assert sub["components"]["skills"] == ["sub:s"]
        folder = world.tmp / "data" / "plugins" / "sub"
        assert (folder / "skills/s/SKILL.md").is_file() and not (folder / "README.md").exists()  # only its folder
        with pytest.raises(ValueError, match="comes from npm"):
            await lib.install("test-market", "pkg")

    run(go())
    assert not (world.tmp / "data" / "plugins" / "pkg").exists() and "pkg" not in host.cfg.plugins.installed
    assert view_source(lib, "far") == f"https://github.com/acme/far/tree/{FAR_SHA}"


def view_source(lib: p.Library, name: str) -> str:
    return lib.view(name)["source"]["url"]  # type: ignore[index, return-value]


def test_a_commit_that_is_not_the_one_pinned_installs_nothing(world: SimpleNamespace) -> None:
    lib, host, net = world.lib, world.host, world.net
    url = f"https://codeload.github.com/acme/far/tar.gz/{FAR_SHA}"
    net.files[url] = tar_file(world.tmp / "other.tgz", {"a": "x"}, root="far", comment="9" * 40).read_bytes()

    async def go() -> None:
        await lib.refresh()
        with pytest.raises(p.FetchError, match="sent commit"):
            await lib.install("test-market", "far")

    run(go())
    assert host.cfg.plugins.installed == {} and not (world.tmp / "data" / "plugins" / "far").exists()


def test_a_connector_never_takes_a_name_that_is_not_its_own(world: SimpleNamespace) -> None:
    """The far plugin's server is called `system`: Orion's own group. It becomes `far-system`; another server called
    like the plugin's is left alone; when both names are taken nothing is installed."""
    lib, host = world.lib, world.host

    async def go() -> None:
        await lib.refresh()
        host.cfg.mcp_servers = {"linear": {"command": "mine"}, "far-system": {"command": "also mine"}}
        await lib.install("test-market", "linear")
        assert set(host.cfg.mcp_servers) == {"linear", "far-system", "linear-linear"}
        assert host.cfg.mcp_servers["linear"] == {"command": "mine"}
        with pytest.raises(ValueError, match="can't be added"):
            await lib.install("test-market", "far")
        assert "far" not in host.cfg.plugins.installed and not (world.tmp / "data" / "plugins" / "far").exists()
        host.cfg.mcp_servers = {"linear": {"command": "mine"}}
        await lib.install("test-market", "far")
        assert host.cfg.mcp_servers["far-system"]["plugin"] == "far"

    run(go())


def test_updating_keeps_what_the_user_set_and_says_what_changed(world: SimpleNamespace) -> None:
    lib, host, net = world.lib, world.host, world.net

    async def go() -> None:
        await lib.refresh()
        await lib.install("test-market", "linear")
        host.vault.set("connector.linear.old_key", "secret")
        servers = {**host.cfg.mcp_servers, "linear": {**host.cfg.mcp_servers["linear"], "disabled_tools": ["x"],
                                                      "values": {"A": "b"}, "sandbox": False, "enabled": False}}
        await host.apply_own({"mcp_servers": servers})
        changed = {**LINEAR_FILES, ".mcp.json": json.dumps({"linear-new": {"url": "https://new.test/mcp"}}),
                   "skills/extra/SKILL.md": "---\nname: extra\ndescription: Extra.\n---\nx"}
        changed.pop("commands/standup.md")
        net.serve("acme/market", market_archive(world.tmp, ENTRIES, {"linear": changed, "github": GITHUB_FILES}),
                  '"market-2"')
        await lib.refresh(force=True)
        assert lib.view("linear")["update"]["sha"] != host.cfg.plugins.installed["linear"]["sha"]  # type: ignore[index]
        updated = await lib.update("linear")
        assert "update" not in updated and set(updated["components"]["skills"]) == {"linear:triage", "linear:extra"}
        assert updated["components"]["commands"] == [] and updated["components"]["connectors"] == ["linear-new"]

    run(go())
    assert "linear" not in host.cfg.mcp_servers and host.cfg.mcp_servers["linear-new"]["url"] == "https://new.test/mcp"
    assert host.vault.has("connector.linear.old_key") is False  # the gone connector's secrets go with it
    record = next(r for r in host.records if r["title"] == "Updated the linear plugin")
    assert record["detail"]["skills"] == "+linear:extra" and record["detail"]["commands"] == "-linear:standup"
    assert record["detail"]["connectors"] == "+linear-new, -linear"


def test_an_update_keeps_the_settings_of_a_connector_that_stays(world: SimpleNamespace) -> None:
    lib, host, net = world.lib, world.host, world.net

    async def go() -> None:
        await lib.refresh()
        await lib.install("test-market", "linear")
        servers = {**host.cfg.mcp_servers, "linear": {**host.cfg.mcp_servers["linear"], "disabled_tools": ["t"],
                                                      "values": {"A": "b"}, "sandbox": False}}
        await host.apply_own({"mcp_servers": servers})
        net.serve("acme/market", market_archive(world.tmp, ENTRIES, {
            "linear": {**LINEAR_FILES, "skills/triage/SKILL.md": "---\nname: triage\ndescription: New.\n---\nx"},
            "github": GITHUB_FILES}), '"market-2"')
        await lib.update("linear")

    run(go())
    assert host.cfg.mcp_servers["linear"] == {"url": "https://mcp.linear.app/mcp", "plugin": "linear",
                                              "source_server": "linear", "description": "from the linear plugin",
                                              "disabled_tools": ["t"], "values": {"A": "b"}, "sandbox": False}
    assert host.skills.get("linear:triage").description == "New."


def zipped_plugin(tmp_path: Path, name: str = "mine", **extra: str) -> bytes:
    folder = build_plugin(tmp_path / f"zip-{name}")
    manifest = json.loads((folder / ".claude-plugin/plugin.json").read_text())
    (folder / ".claude-plugin/plugin.json").write_text(json.dumps({**manifest, "name": name}))
    files = {path.relative_to(folder).as_posix(): path.read_bytes() for path in folder.rglob("*") if path.is_file()}
    return zip_bytes({f"{name}/{path}": content for path, content in {**files, **extra}.items()})


def test_uploading_a_zip_with_exactly_one_plugin_in_it(world: SimpleNamespace) -> None:
    lib, host = world.lib, world.host

    async def go() -> None:
        view = await lib.upload(zipped_plugin(world.tmp), "mine.zip")
        assert view["name"] == "mine" and view["source"] == {"sha": host.cfg.plugins.installed["mine"]["sha"]}
        assert view["trust"] == "unreviewed" and view["components"]["skills"] == ["mine:triage"]
        assert host.cfg.plugins.installed["mine"]["source"] == "upload" and "tracker" in host.cfg.mcp_servers
        assert set(host.cfg.mcp_servers) == {"tracker", "local"}  # the broken one was left out, and said
        assert any("broken" in line for line in view["components"]["unused"])
        again = await lib.upload(zipped_plugin(world.tmp, **{"skills/new/SKILL.md": "---\nname: new\n"
                                                                                       "description: N.\n---\nx"}),
                                 "mine-2.plugin")
        assert set(again["components"]["skills"]) == {"mine:triage", "mine:new"}
        assert host.cfg.plugins.installed["mine"]["installed_at"] <= again["installed_at"] + 1
        with pytest.raises(ValueError, match="was uploaded"):
            await lib.update("mine")

    run(go())
    assert host.titles("plugin") == ["Uploaded the mine plugin", "Uploaded the mine plugin"]


def test_an_upload_that_is_not_exactly_one_safe_plugin_is_refused(world: SimpleNamespace) -> None:
    lib, host = world.lib, world.host
    two = zip_bytes({"a/.claude-plugin/plugin.json": '{"name": "a"}', "b/.claude-plugin/plugin.json": '{"name": "b"}'})
    none = zip_bytes({"README.md": "hi"})
    nameless = zip_bytes({".claude-plugin/plugin.json": "{}"})
    badname = zip_bytes({".claude-plugin/plugin.json": '{"name": "Not A Name!"}'})
    slip = zip_bytes({".claude-plugin/plugin.json": '{"name": "x"}', "../evil": "x"})
    huge = zip_bytes({".claude-plugin/plugin.json": '{"name": "x"}', "big": "x" * 3000})

    async def go() -> None:
        await lib.refresh()
        for data, why in [(two, "exactly one .claude-plugin/plugin.json \\(it holds 2\\)"),
                          (none, "it holds 0"), (nameless, "gives no name"), (badname, "isn't a plugin's name"),
                          (slip, "outside its folder"), (b"not a zip", "isn't a zip")]:
            with pytest.raises(ValueError, match=why):
                await lib.upload(data, "x.zip")
        host.cfg.plugins.max_download_mb = 1
        with pytest.raises(ValueError, match="bigger than 1 MB"):
            await lib.upload(b"x" * (2 << 20), "big.zip")
        host.cfg.plugins.max_files = 100
        host.cfg.plugins.max_unpacked_mb = 1
        await lib.upload(huge, "ok.zip")  # 3 KB is well under a megabyte
        await lib.install("test-market", "linear")
        clash = zip_bytes({".claude-plugin/plugin.json": '{"name": "linear"}'})
        with pytest.raises(ValueError, match="installed from test-market: remove it first"):
            await lib.upload(clash, "linear.zip")

    run(go())
    assert sorted(host.cfg.plugins.installed) == ["linear", "x"]
    plugins_folder = world.tmp / "data" / "plugins"
    assert not (world.tmp / "evil").exists() and [f.name for f in plugins_folder.iterdir() if f.name.startswith(".")] == []


def test_switching_a_plugin_off_and_on_takes_its_skills_and_connectors_with_it(world: SimpleNamespace) -> None:
    lib, host = world.lib, world.host

    async def go() -> None:
        await lib.refresh()
        await lib.install("test-market", "linear")
        await lib.install("test-market", "github")
        assert (await lib.set_enabled("linear", False))["enabled"] is False
        assert host.cfg.mcp_servers["linear"]["enabled"] is False and "enabled" not in host.cfg.mcp_servers["github"]
        assert not any(name.startswith("linear:") for name in host.skills.everything())  # not listed at all
        assert (await lib.set_enabled("linear", True))["enabled"] is True
        assert "enabled" not in host.cfg.mcp_servers["linear"] and "linear:triage" in host.skills.all()
        with pytest.raises(LookupError):
            await lib.set_enabled("nope", True)

    run(go())
    assert host.titles("plugin")[-2:] == ["Switched the linear plugin off", "Switched the linear plugin on"]


def test_removing_a_plugin_takes_everything_of_it_away(world: SimpleNamespace) -> None:
    lib, host = world.lib, world.host

    async def go() -> None:
        await lib.refresh()
        await lib.install("test-market", "github")
        await lib.install("test-market", "linear")
        host.vault.set("connector.github.github_personal_access_token", "ghp_secret")
        host.vault.set("connector.linear.oauth", "{}")
        host.vault.set("connector.mail.password", "not this plugin's")
        await host.apply_own({"skills.disabled": ["linear:triage", "movie-night", "github:x"]})
        await lib.remove("github")
        assert "github" not in host.cfg.plugins.installed and "github" not in host.cfg.mcp_servers
        assert host.vault.names() == ("connector.linear.oauth", "connector.mail.password")
        assert host.cfg.skills.disabled == ["linear:triage", "movie-night"]
        await lib.remove("linear")
        assert host.cfg.skills.disabled == ["movie-night"] and host.vault.names() == ("connector.mail.password",)
        with pytest.raises(LookupError):
            await lib.remove("linear")

    run(go())
    assert not (world.tmp / "data" / "plugins" / "github").exists() and not host.cfg.mcp_servers
    assert host.titles("plugin")[-2:] == ["Removed the github plugin", "Removed the linear plugin"]


def test_a_plugin_the_brain_installs_has_its_connectors_off_and_its_actor_logged(world: SimpleNamespace) -> None:
    """Only the user switches a connector on: an install or update the AI makes adds its connectors switched off
    (one already there keeps what the user set), and the log says who acted."""
    lib, host, net = world.lib, world.host, world.net

    async def go() -> None:
        await lib.refresh()
        await lib.install("test-market", "linear", source="voice", actor="the brain, with your yes", connectors_on=False)
        assert host.cfg.mcp_servers["linear"]["enabled"] is False
        assert host.cfg.plugins.installed["linear"]["enabled"] is True and "linear:triage" in host.skills.all()
        await host.apply_own({"mcp_servers": {**host.cfg.mcp_servers, "linear": {
            k: v for k, v in host.cfg.mcp_servers["linear"].items() if k != "enabled"}}})  # the user switches it on
        changed = {**LINEAR_FILES, ".mcp.json": json.dumps({"linear": {"url": "https://mcp.linear.app/mcp"},
                                                            "extra": {"url": "https://extra.test/mcp"}})}
        net.serve("acme/market", market_archive(world.tmp, ENTRIES, {"linear": changed, "github": GITHUB_FILES}),
                  '"market-2"')
        await lib.update("linear", source="voice", actor="the brain, with your yes", connectors_on=False)
        assert "enabled" not in host.cfg.mcp_servers["linear"]  # what the user switched on stays on
        assert host.cfg.mcp_servers["extra"]["enabled"] is False  # a connector the update brings is off
        await lib.set_enabled("linear", False, source="voice", actor="the brain")
        await lib.remove("linear", source="voice", actor="the brain, with your yes")

    run(go())
    plugin_rows = [r for r in host.records if r["kind"] == "plugin"]
    assert {(r["source"], r["actor"]) for r in plugin_rows} == {("voice", "the brain, with your yes"),
                                                                 ("voice", "the brain")}


def test_a_plugin_that_needs_fields_installs_with_them_unset(world: SimpleNamespace) -> None:
    """The github plugin's server wants ${GITHUB_PERSONAL_ACCESS_TOKEN}: a secret field of its connector."""
    lib, host = world.lib, world.host

    async def go() -> None:
        await lib.refresh()
        await lib.install("test-market", "github")

    run(go())
    server = host.cfg.mcp_servers["github"]
    assert server["headers"] == {"Authorization": "Bearer ${GITHUB_PERSONAL_ACCESS_TOKEN}"}  # placeholders stay in
    fields = p.placeholder_fields(p.placeholders(server))
    assert [(f["key"], f["kind"], f["required"]) for f in fields] == [("GITHUB_PERSONAL_ACCESS_TOKEN", "secret", True)]


# ---------------------------------------------------------------------------------------------------- skills

def skills_archive(tmp_path: Path, extra: dict[str, str] | None = None) -> bytes:
    files = {"skills/pdf/SKILL.md": "---\nname: pdf\ndescription: Work with PDFs.\nlicense: Apache-2.0\n---\nSteps.",
             "skills/pdf/LICENSE.txt": "Apache", "skills/pdf/references/forms.md": "# forms",
             "skills/pdf/scripts/fill.py": "print('hi')",
             "skills/broken/SKILL.md": "no front matter", "skills/nodesc/SKILL.md": "---\nname: nodesc\n---\nx",
             "template/SKILL.md": "---\nname: template-skill\ndescription: Replace me.\n---\nx"}
    return tar_file(tmp_path / "skills.tgz", {**files, **(extra or {})}, root="skills-main", comment=SHA_S).read_bytes()


SHA_S = "4" * 40


def test_skills_are_found_in_a_source_and_imported_with_their_files(world: SimpleNamespace) -> None:
    lib, host, net = world.lib, world.host, world.net
    host.cfg.skills.sources = ["https://github.com/acme/skills"]
    net.serve("acme/skills", skills_archive(world.tmp))

    async def go() -> None:
        await lib.refresh()
        found = lib.discover_skills()
        assert [(s["name"], s["installed"]) for s in found] == [("pdf", False), ("template-skill", False)]
        assert found[0] == {"name": "pdf", "description": "Work with PDFs.", "source": "acme/skills",
                            "url": f"https://github.com/acme/skills/tree/{SHA_S}/skills/pdf", "installed": False}
        assert [s["name"] for s in lib.discover_skills("work pdf")] == ["pdf"] and lib.discover_skills("zzz") == []
        (skill,) = await lib.import_skills(url=found[0]["url"])  # type: ignore[arg-type]
        assert skill.name == "pdf" and skill.source == "yours" and skill.origin == found[0]["url"]
        assert skill.trusted is False and "Steps." in skill.body
        assert host.skills.files("pdf") == ["LICENSE.txt", "SKILL.md", "references/forms.md", "scripts/fill.py"]
        assert lib.discover_skills("pdf")[0]["installed"] is True
        again = await lib.import_skills(url=found[0]["url"])  # the same again replaces it
        assert again[0].name == "pdf"
        host.skills.write("mine", "My own.", "Do.")
        net.serve("acme/other", tar_file(
            world.tmp / "other-skills.tgz", {"mine/SKILL.md": "---\nname: mine\ndescription: Clash.\n---\nx",
                                             "fresh/SKILL.md": "---\nname: fresh\ndescription: Fresh.\n---\nx"},
            root="o-main", comment=SHA_S).read_bytes())
        with pytest.raises(ValueError, match="already have a skill called mine"):
            await lib.import_skills(url="acme/other")
        assert host.skills.entry("fresh") is None  # every clash is said before anything is added

    run(go())
    assert host.titles("skill") == ["Imported the pdf skill", "Imported the pdf skill"]


def test_skills_are_imported_from_a_zip_and_from_a_github_folder_url(world: SimpleNamespace) -> None:
    lib, host, net = world.lib, world.host, world.net
    data = zip_bytes({"one/SKILL.md": "---\nname: one\ndescription: One.\n---\nx", "two/SKILL.md": "---\nname: two\n"
                      "description: Two.\n---\nx", "two/extra.txt": "e"}, links={"two/link": "/etc/passwd"})
    net.serve("acme/skills", skills_archive(world.tmp))

    async def go() -> None:
        added = await lib.import_skills(data=data, filename="pack.zip")
        assert [s.name for s in added] == ["one", "two"] and {s.origin for s in added} == {"pack.zip/one", "pack.zip/two"}
        assert host.skills.files("two") == ["SKILL.md", "extra.txt"]  # the link was never unpacked
        (pdf,) = await lib.import_skills(url="https://github.com/acme/skills/tree/main/skills/pdf")
        assert pdf.name == "pdf" and pdf.origin == "https://github.com/acme/skills/tree/main/skills/pdf"
        for bad, why in [(b"nope", "isn't a zip"), (zip_bytes({"README.md": "x"}), "no skill in it"),
                         (zip_bytes({"s/SKILL.md": "---\nname: s\n---\nx"}), "no skill in it")]:
            with pytest.raises(ValueError, match=why):
                await lib.import_skills(data=bad, filename="bad.zip")
        with pytest.raises(ValueError, match="isn't a repository"):
            await lib.import_skills(url="not a url")
        with pytest.raises(ValueError, match="outside"):
            await lib.import_skills(url="https://github.com/acme/skills/tree/main/..%2F..")

    run(go())
    assert (host.skills.root / ".sources.json").is_file()


def test_the_skills_of_a_plugin_that_is_gone_are_not_listed(world: SimpleNamespace) -> None:
    host = world.host
    run(host.apply_own({"plugins.installed": {"ghost": {"source": "upload", "version": "", "sha": "a" * 64,
                                                        "enabled": True, "installed_at": 1}}}))
    assert p.skill_roots(host.cfg) == () and "ghost:x" not in host.skills.all()
