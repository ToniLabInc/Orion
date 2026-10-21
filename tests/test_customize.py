"""customize: the REST of Customize, through Orion's real app, on a real hub, library, broker, skills and settings file.
What a page can do: list and open connectors (a device is told it can't sign in), add one from the directory, change
its settings without a secret ever coming back, switch it, sign in through the callback page, remove it; skills
(switch, edit, import, discover, delete); plugins (install, update, switch, upload, remove) and marketplaces; and how
each failure reaches the page (400 for what is wrong, 404 for what isn't there, 502 for a site that is down, 413 for
a file too big)."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fake_oauth import BASE, URL
from test_connectors import HOME_ASSISTANT, World
from test_library import ENTRIES, FAR_SHA, GITHUB_FILES, LINEAR_FILES, Net, market_archive, skills_archive
from test_plugins import tar_file, zip_bytes

from orion import plugins as p
from orion.customize import for_device, for_hud
from orion.server import COALESCE, PAGES, create_app


def as_peer(app: Any, peer: str) -> Callable[..., Any]:
    """The app as the envelope hands it requests: with who asks in the scope's state."""

    async def wrapped(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] in ("http", "websocket"):
            scope.setdefault("state", {})["peer"] = peer
        await app(scope, receive, send)

    return wrapped


class Pages:
    """The HUD on this PC, and on a paired device, as clients of the one app."""

    def __init__(self, world: World) -> None:
        self.app = create_app(world, SimpleNamespace())  # type: ignore[arg-type]
        self.pc = httpx.AsyncClient(transport=httpx.ASGITransport(app=as_peer(self.app, "local")),
                                    base_url="http://localhost")
        self.device = httpx.AsyncClient(transport=httpx.ASGITransport(app=as_peer(self.app, "device")),
                                        base_url="http://orion.local")


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return World(tmp_path, monkeypatch)


@pytest.fixture
def net(world: World, monkeypatch: pytest.MonkeyPatch) -> Net:
    network = Net()
    monkeypatch.setattr(p, "_download", network)
    network.serve("acme/market", market_archive(world.tmp, ENTRIES, {"linear": LINEAR_FILES, "github": GITHUB_FILES}),
                  '"m1"')
    network.serve("acme/skills", skills_archive(world.tmp))
    world.cfg.plugins.marketplaces = {"test-market": "https://github.com/acme/market"}
    world.cfg.skills.sources = ["https://github.com/acme/skills"]
    return network


@asynccontextmanager
async def served(world: World, net: Net | None = None) -> AsyncIterator[Pages]:
    await world.start()
    if net is not None:
        await world.library.refresh()
    pages = Pages(world)
    try:
        yield pages
    finally:
        await pages.pc.aclose()
        await pages.device.aclose()
        await world.hub.stop()


def run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


def zip_plugin(name: str = "mine", **extra: str) -> bytes:
    return zip_bytes({".claude-plugin/plugin.json": json.dumps({"name": name, "version": "1.0"}),
                      "skills/hello/SKILL.md": "---\nname: hello\ndescription: Says hello.\n---\nHi.", **extra})


# ---------------------------------------------------------------------------------------------------- connectors
def test_the_connectors_are_listed_and_a_device_is_told_it_cannot_sign_in(world: World) -> None:
    async def go() -> None:
        async with served(world) as pages:
            on_pc = (await pages.pc.get("/api/connectors")).json()["items"]
            on_device = (await pages.device.get("/api/connectors")).json()["items"]
            assert [i["id"] for i in on_pc] == [i["id"] for i in on_device] and "system" in [i["id"] for i in on_pc]
            assert all(i["local"] for i in on_pc) and not any(i["local"] for i in on_device)
            page = (await pages.pc.get("/api/connectors/system")).json()
            assert page["id"] == "system" and isinstance(page["activity"], list) and page["local"] is True
            assert (await pages.device.get("/api/connectors/system")).json()["local"] is False
            missing = await pages.pc.get("/api/connectors/nope")
            assert missing.status_code == 404 and "no connector called nope" in missing.json()["detail"]

    run(go())


def test_the_directory_is_searched_and_a_connector_added_from_it_never_shows_its_secret(world: World, net: Net) -> None:
    async def go() -> None:
        async with served(world, net) as pages:
            found = (await pages.pc.get("/api/connectors/directory", params={"q": "home assistant"})).json()["items"]
            assert [i["id"] for i in found] == ["home-assistant"] and found[0]["installed"] is False
            added = await pages.pc.post("/api/connectors", json={"from": "directory", "id": "home-assistant",
                                                                 "values": HOME_ASSISTANT})
            assert added.status_code == 200 and added.json()["id"] == "home-assistant"
            assert HOME_ASSISTANT["token"] not in added.text + (world.tmp / "data" / "config.yaml").read_text()
            assert (await pages.pc.get("/api/connectors/directory", params={"category": "smart home"})).json()[
                "items"][0]["installed"] is True
            saved = await pages.device.put("/api/connectors/home-assistant/settings",
                                           json={"values": {"url": "http://ha2.local:8123", "token": ""}})
            assert saved.status_code == 200 and saved.json()["local"] is False  # the secret left empty stays
            assert saved.json()["settings"]["values"]["url"] == "http://ha2.local:8123"
            assert world.vault.get("connector.home-assistant.token") == HOME_ASSISTANT["token"]
            bad = [await pages.pc.put("/api/connectors/home-assistant/settings", json={"values": "x"}),
                   await pages.pc.put("/api/connectors/home-assistant/settings", json={"values": {"nope": "x"}}),
                   await pages.pc.post("/api/connectors", json={"from": "elsewhere"}),
                   await pages.pc.post("/api/connectors", json={"from": "directory", "id": "no-such-thing"})]
            assert [r.status_code for r in bad] == [400, 400, 400, 404]
            removed = await pages.pc.delete("/api/connectors/home-assistant")
            assert removed.json() == {"ok": True} and world.vault.names() == ()
            assert (await pages.pc.delete("/api/connectors/home-assistant")).status_code == 404

    run(go())


def test_a_connector_is_switched_on_and_off_and_each_of_its_tools(world: World) -> None:
    async def go() -> None:
        async with served(world) as pages:
            made = await pages.pc.post("/api/connectors", json={"from": "custom", "name": "tracker",
                                                                "url": "https://tracker.test/mcp"})
            assert made.status_code == 200
            await world.until(lambda: world.hub.status()[0]["status"] == "connected")
            off = await pages.pc.patch("/api/connectors/tracker", json={"enabled": False})
            assert off.json()["status"] == "off"
            on = await pages.pc.patch("/api/connectors/tracker", json={"enabled": True})
            assert on.json()["status"] in ("connecting", "connected")
            await world.until(lambda: world.hub.status()[0]["status"] == "connected")
            tool = (await pages.pc.get("/api/connectors/tracker")).json()["tools"][0]["name"]
            switched = await pages.pc.patch(f"/api/connectors/tracker/tools/{tool}", json={"enabled": False})
            assert switched.json()["tools"][0]["enabled"] is False
            invalid = [await pages.pc.patch("/api/connectors/tracker", json={}),
                       await pages.pc.patch("/api/connectors/tracker", json={"enabled": "yes"}),
                       await pages.pc.patch(f"/api/connectors/tracker/tools/{tool}", json={}),
                       await pages.pc.patch("/api/connectors/tracker", json={"sandbox": True})]
            assert [r.status_code for r in invalid] == [400, 400, 400, 400]  # a remote server has nothing to sandbox
            assert (await pages.pc.patch("/api/connectors/nope", json={"enabled": True})).status_code == 404
            assert (await pages.pc.patch("/api/connectors/tracker/tools/none", json={"enabled": True})).status_code == 404

    run(go())


def test_signing_in_returns_through_the_callback_page(world: World) -> None:
    async def go() -> None:
        async with world.fake.serving(), served(world) as pages:
            await pages.pc.post("/api/connectors", json={"from": "custom", "name": "notes", "url": URL})
            await world.until(lambda: world.hub.status()[-1]["sign_in"])
            refused = await pages.device.post("/api/connectors/notes/connect")
            assert refused.status_code == 400 and "Sign in from the PC" in refused.json()["detail"]
            link = (await pages.pc.post("/api/connectors/notes/connect")).json()
            assert link["auth_url"].startswith(f"{BASE}/authorize?")
            back = await pages.pc.get("/oauth/callback", params=world.fake.authorize(link["auth_url"]))
            assert back.status_code == 200 and "Connected to Notes" in back.text and "<script" not in back.text
            assert back.headers["content-type"].startswith("text/html")
            again = await pages.pc.get("/oauth/callback", params={"state": "never-started", "code": "x"})
            assert again.status_code == 400 and "over, or was never started here" in again.text
            await world.until(lambda: world.hub.tools().get("notes__ping") is not None)
            assert (await pages.pc.get("/api/connectors/notes")).json()["signed_in"] is True
            out = await pages.pc.post("/api/connectors/notes/disconnect")
            assert out.json()["signed_in"] is False and out.json()["status"] != "connected"

    run(go())


def test_what_a_provider_says_on_the_callback_page_is_escaped(world: World) -> None:
    async def go() -> None:
        async with world.fake.serving(), served(world) as pages:
            await pages.pc.post("/api/connectors", json={"from": "custom", "name": "notes", "url": URL})
            await world.until(lambda: world.hub.status()[-1]["sign_in"])
            link = (await pages.pc.post("/api/connectors/notes/connect")).json()
            denied = world.fake.authorize(link["auth_url"], approve=False)
            back = await pages.pc.get("/oauth/callback", params={**denied,
                                                                 "error_description": "<script>alert(1)</script>"})
            assert back.status_code == 400 and "<script>" not in back.text and "&lt;script&gt;" in back.text

    run(go())


# ---------------------------------------------------------------------------------------------------- skills
def test_skills_are_listed_switched_edited_and_deleted(world: World) -> None:
    async def go() -> None:
        async with served(world) as pages:
            (world.skills.root / "morning-briefing").mkdir(parents=True)
            (world.skills.root / "morning-briefing" / "SKILL.md").write_text(
                "---\nname: morning-briefing\ndescription: A briefing.\n---\nSteps.\n")
            world.skills.invalidate()
            listed = (await pages.pc.get("/api/skills")).json()["items"]
            assert [(s["name"], s["source"], s["enabled"], s["editable"]) for s in listed] == [
                ("morning-briefing", "builtin", True, False)]
            starter = listed[0]["name"]
            assert (await pages.pc.put(f"/api/skills/{starter}", json={"content": "x"})).status_code == 400
            assert (await pages.pc.delete(f"/api/skills/{starter}")).status_code == 400  # a starter is switched off
            text = "---\nname: mine\ndescription: My own.\n---\nDo it.\n"
            saved = await pages.pc.put("/api/skills/mine", json={"content": text})
            assert saved.status_code == 200 and saved.json()["content"] == text and saved.json()["editable"] is True
            assert saved.json()["source"] == "yours"
            assert {k: v for k, v in world.events[-1].items() if k != "ts"} == {
                "type": "skill_changed", "name": "mine", "action": "saved"}
            off = await pages.pc.patch("/api/skills/mine", json={"enabled": False})
            assert off.json()["enabled"] is False and world.cfg.skills.disabled == ["mine"]
            assert world.skills.get("mine") is None  # the brain no longer sees it
            assert (await pages.pc.get("/api/skills/mine")).json()["content"] == text
            on = await pages.pc.patch("/api/skills/mine", json={"enabled": True})
            assert on.json()["enabled"] is True and world.cfg.skills.disabled == []
            bad = [await pages.pc.put("/api/skills/mine", json={}),
                   await pages.pc.put("/api/skills/mine", json={"content": "no front matter"}),
                   await pages.pc.patch("/api/skills/mine", json={"enabled": 1}),
                   await pages.pc.put("/api/skills/linear:triage", json={"content": text})]
            assert [r.status_code for r in bad] == [400, 400, 400, 400]
            assert (await pages.pc.get("/api/skills/nope")).status_code == 404
            assert (await pages.pc.patch("/api/skills/nope", json={"enabled": True})).status_code == 404
            assert (await pages.pc.delete("/api/skills/mine")).json() == {"ok": True}
            assert (await pages.pc.delete("/api/skills/mine")).status_code == 404
            assert world.titles("skill") == ["Saved the mine skill", "Switched the mine skill off",
                                             "Switched the mine skill on", "Deleted the mine skill"]

    run(go())


def test_skills_are_found_in_the_sources_and_imported_from_a_url_or_a_zip(world: World, net: Net) -> None:
    async def go() -> None:
        async with served(world, net) as pages:
            found = (await pages.pc.get("/api/skills/discover", params={"q": "pdf"})).json()["items"]
            assert [(s["name"], s["installed"]) for s in found] == [("pdf", False)]
            added = await pages.pc.post("/api/skills/import", json={"url": found[0]["url"]})
            assert [s["name"] for s in added.json()] == ["pdf"]  # a list of the skills, as the page expects
            assert added.json()[0]["files"] == ["LICENSE.txt", "SKILL.md", "references/forms.md", "scripts/fill.py"]
            assert (await pages.pc.get("/api/skills/discover", params={"q": "pdf"})).json()["items"][0]["installed"]
            archive = zip_bytes({"one/SKILL.md": "---\nname: one\ndescription: One.\n---\nx"})
            sent = await pages.device.post("/api/skills/import", files={"file": ("pack.zip", archive, "application/zip")})
            assert [s["name"] for s in sent.json()] == ["one"]
            bad = [await pages.pc.post("/api/skills/import", json={}),
                   await pages.pc.post("/api/skills/import", json={"url": "not a url"}),
                   await pages.pc.post("/api/skills/import", content="nope", headers={"content-type": "application/json"}),
                   await pages.pc.post("/api/skills/import", files={"file": ("bad.zip", b"nope", "application/zip")})]
            assert [r.status_code for r in bad] == [400, 400, 400, 400]
            net.down.add("https://codeload.github.com/acme/skills/tar.gz/")
            down = await pages.pc.post("/api/skills/import", json={"url": "acme/skills"})
            assert down.status_code == 502 and "offline" in down.json()["detail"]

    run(go())


# ---------------------------------------------------------------------------------------------------- plugins
def test_a_plugin_is_installed_switched_updated_and_removed(world: World, net: Net) -> None:
    async def go() -> None:
        async with served(world, net) as pages:
            found = (await pages.pc.get("/api/plugins/discover", params={"q": "issues"})).json()
            assert [i["name"] for i in found["items"]] == ["linear"] and "productivity" in found["categories"]
            installed = await pages.pc.post("/api/plugins", json={"marketplace": "test-market", "name": "linear"})
            plugin = installed.json()
            assert installed.status_code == 200 and plugin["name"] == "linear" and plugin["enabled"] is True
            assert plugin["components"]["connectors"] and plugin["source"]["marketplace"] == "test-market"
            assert (await pages.pc.get("/api/plugins")).json()["items"][0]["name"] == "linear"
            assert any(e["type"] == "plugins" for e in world.events)
            assert (await pages.pc.post("/api/plugins", json={"marketplace": "test-market", "name": "linear"})
                    ).status_code == 400  # installed already
            off = await pages.device.patch("/api/plugins/linear", json={"enabled": False})
            assert off.json()["enabled"] is False
            assert (await pages.pc.post("/api/plugins/linear/update")).json()["name"] == "linear"
            assert (await pages.pc.delete("/api/plugins/linear")).json() == {"ok": True}
            assert (await pages.pc.get("/api/plugins")).json() == {"items": []}
            bad = [await pages.pc.post("/api/plugins", json={"name": "linear"}),
                   await pages.pc.post("/api/plugins", json={"marketplace": "nope", "name": "linear"}),
                   await pages.pc.patch("/api/plugins/linear", json={"enabled": False}),
                   await pages.pc.patch("/api/plugins/linear", json={}),
                   await pages.pc.post("/api/plugins/linear/update"),
                   await pages.pc.delete("/api/plugins/linear")]
            assert [r.status_code for r in bad] == [400, 404, 404, 400, 404, 404]
            net.down.add(f"https://codeload.github.com/acme/far/tar.gz/{FAR_SHA}")
            down = await pages.pc.post("/api/plugins", json={"marketplace": "test-market", "name": "far"})
            assert down.status_code == 502 and "offline" in down.json()["detail"]

    run(go())


def test_a_plugin_is_uploaded_and_a_file_too_big_or_without_length_is_refused(world: World, net: Net) -> None:
    async def go() -> None:
        async with served(world, net) as pages:
            sent = await pages.pc.post("/api/plugins", files={"file": ("mine.plugin", zip_plugin(), "application/zip")})
            assert sent.status_code == 200 and sent.json()["name"] == "mine" and sent.json()["source"] == {
                "sha": sent.json()["source"]["sha"]}  # an upload has no marketplace
            assert (await pages.pc.get("/api/skills/mine:hello")).json()["source"] == "plugin"
            world.cfg.plugins.max_download_mb = 1
            big = await pages.pc.post("/api/plugins", files={"file": ("big.zip", b"x" * ((1 << 20) + 70_000), "")})
            assert big.status_code == 413 and "bigger than 1 MB" in big.json()["detail"]

            async def chunks() -> AsyncIterator[bytes]:
                yield b"--x\r\n"

            unsized = await pages.pc.post("/api/plugins", content=chunks(),
                                          headers={"content-type": "multipart/form-data; boundary=x"})
            assert unsized.status_code == 411
            empty = await pages.pc.post("/api/plugins", data={"note": "no file"}, files={"note2": (None, b"x")})
            assert empty.status_code == 400 and "Send the file" in empty.json()["detail"]
            broken = await pages.pc.post("/api/plugins", files={"file": ("a.zip", b"not a zip", "application/zip")})
            assert broken.status_code == 400
            two = await pages.pc.post("/api/plugins", files={"file": (
                "two.zip", zip_bytes({"a/.claude-plugin/plugin.json": "{}", "b/.claude-plugin/plugin.json": "{}"}), "")})
            assert two.status_code == 400

    run(go())


def test_marketplaces_are_added_listed_refreshed_and_removed(world: World, net: Net) -> None:
    net.serve("acme/second", tar_file(world.tmp / "second.tgz", {".claude-plugin/marketplace.json": json.dumps(
        {"name": "second-market", "plugins": [{"name": "mine", "source": "./mine"}]})}, root="s", comment="5" * 40
    ).read_bytes())

    async def go() -> None:
        async with served(world, net) as pages:
            (first,) = (await pages.pc.get("/api/plugins/marketplaces")).json()["items"]
            assert (first["name"], first["plugins"], first["url"]) == ("test-market", 5, "https://github.com/acme/market")
            added = await pages.pc.post("/api/plugins/marketplaces", json={"url": "acme/second"})
            assert added.json()["name"] == "second-market" and added.json()["plugins"] == 1
            names = [m["name"] for m in (await pages.pc.get("/api/plugins/marketplaces")).json()["items"]]
            assert names == ["test-market", "second-market"]
            before = len(net.asked)
            refreshed = await pages.pc.post("/api/plugins/refresh")
            assert len(refreshed.json()["items"]) == 2 and len(net.asked) > before
            only = (await pages.pc.get("/api/plugins/discover", params={"marketplace": "second-market"})).json()
            assert [i["name"] for i in only["items"]] == ["mine"]
            bad = [await pages.pc.post("/api/plugins/marketplaces", json={}),
                   await pages.pc.post("/api/plugins/marketplaces", json={"url": "not a repo"}),
                   await pages.pc.delete("/api/plugins/marketplaces/nope")]
            assert [r.status_code for r in bad] == [400, 400, 404]
            assert (await pages.pc.delete("/api/plugins/marketplaces/second-market")).json() == {"ok": True}
            assert [m["name"] for m in (await pages.pc.get("/api/plugins/marketplaces")).json()["items"]] == ["test-market"]

    run(go())


# ---------------------------------------------------------------------------------------------------- the app
def test_the_pages_of_customize_are_served_and_its_events_are_coalesced(world: World) -> None:
    assert {"/customize", "/customize/{rest:path}"} <= set(PAGES)
    assert COALESCE["connectors"] == COALESCE["plugins"] == ("latest", "")

    async def go() -> None:
        async with served(world) as pages:
            for address in ("/customize", "/customize/connectors", "/customize/plugins/discover/some/deep/path"):
                assert (await pages.pc.get(address)).status_code in (200, 503)  # 503: the HUD isn't built here
            for gone in ("/api/catalog", "/api/catalog/mail"):
                assert (await pages.pc.get(gone)).status_code == 404

    run(go())


def test_a_device_hears_the_connectors_as_ones_it_cannot_sign_in_to() -> None:
    event = {"type": "connectors", "ts": 1.0, "items": [{"id": "a", "local": True}, {"id": "b", "local": True}]}
    assert for_device(event) == {"type": "connectors", "ts": 1.0, "items": [{"id": "a", "local": False},
                                                                           {"id": "b", "local": False}]}
    assert event["items"][0]["local"] is True  # the PC's own copy is untouched
    other = {"type": "plugins", "ts": 1.0, "items": [{"local": True}]}
    assert for_device(other) is other
    assert for_hud({"id": "a", "local": True}, local=True) == {"id": "a", "local": True}
    assert for_hud({"id": "a", "local": True}, local=False) == {"id": "a", "local": False}


# ---------------------------------------------------------------------------------------------------- the contract
CONNECTOR_KEYS = {"id", "title", "description", "icon", "kind", "trust", "sandbox", "status", "error", "needs", "auth",
                  "signed_in", "local", "tools", "settings", "secrets", "removable", "calls"}
FIELD_KEYS = {"key", "label", "kind", "help", "placeholder", "options", "required", "secret"}
SKILL_KEYS = {"name", "description", "source", "enabled", "triggers", "tools", "files", "editable"}


def test_the_shapes_are_the_ones_the_hud_is_written_against(world: World, net: Net) -> None:
    """Every object Customize sends has exactly the keys of the contract (a plugin also says how far it is trusted, a
    marketplace whether it could be fetched); a key the page reads can't be missing."""

    async def go() -> None:
        async with served(world, net) as pages:
            await pages.pc.post("/api/connectors", json={"from": "directory", "id": "home-assistant",
                                                         "values": HOME_ASSISTANT})
            await pages.pc.post("/api/plugins", json={"marketplace": "test-market", "name": "linear"})
            items = (await pages.pc.get("/api/connectors")).json()["items"]
            for item in items:
                assert set(item) - {"plugin"} == CONNECTOR_KEYS, item["id"]
                assert item["kind"] in ("builtin", "catalog", "plugin", "custom")
                assert item["trust"] in ("orion", "reviewed", "unreviewed") and item["sandbox"] in ("none", "bwrap")
                assert item["status"] in ("connected", "needs_setup", "needs_sign_in", "connecting", "error", "off",
                                          "unavailable")
                assert item["auth"] in ("none", "fields", "oauth") and isinstance(item["needs"], list)
                for tool in item["tools"]:
                    assert set(tool) == {"name", "title", "description", "risk", "open_world", "enabled"}
                    assert tool["risk"] in ("read", "change", "destroy")
                settings = item["settings"]
                assert settings["kind"] in ("fields", "gestures", "folders", "none")
                if settings["kind"] == "fields":
                    assert set(settings) == {"kind", "fields", "values"}
                    assert all(FIELD_KEYS <= set(f) <= FIELD_KEYS | {"set"} for f in settings["fields"])
                for secret in item["secrets"]:
                    assert set(secret) == {"name", "set"}
            assert {i["kind"] for i in items} >= {"builtin", "catalog", "plugin"}
            by_id = {i["id"]: i for i in items}
            assert by_id["linear-linear"]["plugin"] == "linear" and "plugin" not in by_id["system"]
            for entry in (await pages.pc.get("/api/connectors/directory")).json()["items"]:
                assert set(entry) == {"id", "title", "description", "icon", "category", "source", "auth", "fields",
                                      "installed", "docs"}
                assert entry["source"] in ("orion", "claude-directory") and entry["auth"] in ("none", "fields", "oauth")
                assert all(FIELD_KEYS <= set(f) <= FIELD_KEYS | {"set"} for f in entry["fields"])
            page = (await pages.pc.get("/api/connectors/system")).json()
            assert set(page) == CONNECTOR_KEYS | {"activity"}
            (plugin,) = (await pages.pc.get("/api/plugins")).json()["items"]
            assert set(plugin) == {"name", "version", "description", "author", "homepage", "source", "enabled",
                                   "installed_at", "components", "trust"}
            assert set(plugin["source"]) <= {"marketplace", "url", "sha"}
            assert set(plugin["components"]) == {"skills", "commands", "connectors", "unused"}
            found = (await pages.pc.get("/api/plugins/discover")).json()
            assert set(found) == {"items", "categories"}
            assert all(set(i) == {"name", "description", "category", "marketplace", "author", "homepage", "installed",
                                  "components_known"} for i in found["items"])
            (market,) = (await pages.pc.get("/api/plugins/marketplaces")).json()["items"]
            assert set(market) == {"name", "url", "plugins", "updated_at", "error"}
            skills = (await pages.pc.get("/api/skills")).json()["items"]
            assert skills and all(SKILL_KEYS <= set(s) <= SKILL_KEYS | {"plugin"} for s in skills)
            assert {s["source"] for s in skills} == {"plugin"} and skills[0]["plugin"] == "linear"
            found_skills = (await pages.pc.get("/api/skills/discover")).json()["items"]
            assert all(set(i) == {"name", "description", "source", "url", "installed"} for i in found_skills)

    run(go())
