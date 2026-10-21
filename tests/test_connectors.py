"""connectors: every source of tools as the HUD lists it, with its own settings; adding one from the directory or by
hand keeps its secrets in the vault and out of the settings, the HUD and the model; a plugin's fields and a server's
secrets are put in only as it connects; signing in, the sandbox, removing; and the status each one shows. The hub is
real; the servers are stand-ins in this process (or the fake OAuth server); the settings file is real."""

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import mcp_types as mt
import pytest
from fake_oauth import BASE, URL, FakeOAuthServer
from mcp import Client
from mcp.server import Server
from test_library import ENTRIES, GITHUB_FILES, LINEAR_FILES, Net, market_archive

from orion import connectors as cn
from orion import plugins as p
from orion.base import EventBus, GroupInfo
from orion.config import Settings, option_label
from orion.confirm import Confirmer
from orion.connectors import Connectors
from orion.mcp import MCPHub, OpTrace
from orion.oauth import Broker
from orion.secrets import Vault
from orion.skills import Skills
from orion.tools.core import Controls

GROUPS = {name: GroupInfo(name, title, about) for name, title, about in (
    ("system", "PC control", "This Linux PC."), ("web", "The internet", "The web."),
    ("watchers", "Watchers", "The machine's health."), ("memory", "Memory", "What he remembers."),
    ("clipboard", "Clipboard", "The clipboard."), ("skills", "Skills", "Saved routines."), ("calendar", "Calendar", "The calendars."))}
CLIPBOARD_MISSING = "a clipboard program (wl-clipboard on Wayland, xclip or xsel on X11)"


def tool(name: str, *, group: str = "", read_only: bool | None = None, destructive: bool | None = None,
         open_world: bool | None = None, requires: tuple[str, ...] = (), title: str = "") -> mt.Tool:
    """A tool as a server lists it."""
    meta = {"orion": {"group": group, "requires": list(requires)}} if group else None
    return mt.Tool(name=name, title=title or None, description=f"{name} does it.", input_schema={"type": "object"},
                   annotations=mt.ToolAnnotations(read_only_hint=read_only, destructive_hint=destructive,
                                                  open_world_hint=open_world), meta=meta)


def server_of(*tools: mt.Tool, resources: dict[str, str] | None = None) -> Server:
    """A stand-in MCP server with these tools (and resources)."""

    async def list_tools(_ctx: object, _params: object) -> mt.ListToolsResult:
        return mt.ListToolsResult(tools=list(tools))

    async def call(_ctx: object, params: Any) -> mt.CallToolResult:
        return mt.CallToolResult(content=[mt.TextContent(type="text", text=f"{params.name} done")])

    async def list_resources(_ctx: object, _params: object) -> mt.ListResourcesResult:
        return mt.ListResourcesResult(resources=[mt.Resource(uri=uri, name=uri) for uri in resources or {}])

    async def read(_ctx: object, params: Any) -> mt.ReadResourceResult:
        return mt.ReadResourceResult(contents=[mt.TextResourceContents(uri=str(params.uri), text=(resources or {})[
            str(params.uri)])])

    if resources:
        return Server("stand-in", on_list_tools=list_tools, on_call_tool=call, on_list_resources=list_resources,
                      on_read_resource=read)
    return Server("stand-in", on_list_tools=list_tools, on_call_tool=call)


class World:
    """A running Orion as far as the connectors need it."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp = tmp_path
        self.settings = Settings.open(tmp_path / "data" / "config.yaml")
        self.settings.sync_file()
        self.cfg = self.settings.cfg
        self.cfg.skills.sources = []
        self.cfg.plugins.marketplaces = {}
        self.bus = EventBus()
        self.events: list[dict[str, Any]] = []
        self.bus.subscribe(self.events.append)
        self.confirmer = Confirmer(self.cfg, self.bus)
        self.vault = Vault("files", tmp_path / "vault")
        self.rows: list[dict[str, Any]] = []
        self.activity = SimpleNamespace(record=self._record, recent=self._recent)
        self.changed = 0
        self.skills = Skills.open(self.cfg)
        self.fake = FakeOAuthServer()
        self.stand_ins: dict[str, Server] = {}
        self.prepared: dict[str, dict[str, Any]] = {}
        self.hub = MCPHub(self.cfg, GROUPS, ops=OpTrace(lambda kind, **data: None, self.cfg.hud, recent=10),
                          spawn=("tools",))
        self.library = p.Library(self)  # type: ignore[arg-type]
        self.broker = Broker(self, http=self.fake.http)  # type: ignore[arg-type]
        self.controls = Controls(self)  # type: ignore[arg-type]
        self.connectors = Connectors(self)  # type: ignore[arg-type]
        self.hub.hooks = self.connectors
        self.groups_server = server_of(
            tool("run_command", group="system", destructive=True, open_world=False, title="Run a shell command"),
            tool("volume", group="system", destructive=False, open_world=False),
            tool("web_search", group="web", read_only=True, open_world=True),
            tool("read_clipboard", group="clipboard", read_only=True, open_world=False, requires=("clipboard",)),
            tool("battery", group="watchers", read_only=True),
            tool("memory", group="memory", destructive=True, open_world=False),
            tool("use_skill", group="skills", read_only=True))
        original = MCPHub._open
        world = self

        async def opened(hub: MCPHub, stack: Any, link: Any) -> Any:
            if link.kind == "tools":
                return await stack.enter_async_context(Client(world.groups_server, mode="legacy"))
            if link.settings.get("url") == URL:
                return await original(hub, stack, link)  # the real way, to the fake OAuth server
            world.prepared[link.name] = await hub.prepared(link.name, link.settings)
            standing = world.stand_ins.get(link.name) or server_of(tool("ping", read_only=True, open_world=True))
            return await stack.enter_async_context(Client(standing, mode="legacy"))

        monkeypatch.setattr(MCPHub, "_open", opened)
        monkeypatch.setattr("mcp.client.streamable_http.create_mcp_http_client", self.fake.http)
        monkeypatch.setattr("orion.mcp.profile", lambda: SimpleNamespace(
            missing=lambda needs: CLIPBOARD_MISSING if "clipboard" in needs else ""))

    # what the connectors need of the running Orion
    def reserved(self, name: str) -> bool:
        return cn.is_reserved(name)

    def connectors_changed(self) -> None:
        self.changed += 1

    async def apply_own(self, patch: dict[str, Any]) -> dict[str, Any]:
        changes = self.settings.validate(patch, actor="hud", own=True)
        changed = await self.settings.commit(dict(changes)) if changes else []
        self.skills.invalidate()
        if any(key in changed for key in ("mcp_servers", "plugins.installed")):
            await self.hub.configure(wait_s=5)
        return {"changed": changed, "restart": []}

    async def apply_servers(self, servers: dict[str, Any], *, actor: str) -> dict[str, Any]:
        return await self.apply_own({"mcp_servers": dict(servers)})

    def _record(self, kind: str, **data: Any) -> None:
        self.rows.append({"id": len(self.rows) + 1, "at": time.time(), "kind": kind, "source": "", "actor": "",
                          "connector": "", "tool": "", "title": "", "detail": "", "outcome": "", "turn": "",
                          "ms": 0, **data})

    def _recent(self, *, limit: int = 50, text: str = "", **_: Any) -> list[dict[str, Any]]:
        return [r for r in reversed(self.rows) if text in json.dumps(r)][:limit]

    # helpers
    async def start(self) -> None:
        await self.hub.configure(wait_s=5)

    async def until(self, check: Callable[[], bool], seconds: float = 5.0) -> None:
        async with asyncio.timeout(seconds):
            while not check():
                await asyncio.sleep(0.01)

    async def view(self, name: str, *, local: bool = True) -> dict[str, Any]:
        return await self.connectors.get(name, local=local)  # type: ignore[return-value]

    def titles(self, kind: str = "connector") -> list[str]:
        return [r["title"] for r in self.rows if r["kind"] == kind]


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return World(tmp_path, monkeypatch)


def run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


HOME_ASSISTANT = {"url": "http://ha.local:8123", "token": "ha-long-lived-token"}


def test_the_pure_rules(world: World) -> None:
    cfg = world.cfg
    assert [cn.kind_of(n, s, builtin=b) for n, s, b in (("system", {}, True), ("notion", {"url": "x"}, False),
                                                       ("mine", {"command": "x"}, False),
                                                       ("tracker", {"plugin": "p"}, False))] == [
        "builtin", "catalog", "custom", "plugin"]
    assert [cn.risk_of(h) for h in ({"read_only": True}, {"read_only": False, "destructive": False},
                                    {"read_only": False, "destructive": True}, {}, {"destructive": None})] == [
        "read", "change", "destroy", "destroy", "destroy"]  # a tool that says nothing is destructive, as in MCP
    assert cn.humanize("my-tracker_2") == "My Tracker 2"
    assert [option_label(n) for n in ("cache_s", "keep_days", "max_unpacked_mb", "s", "write_to", "max_s_gb")] == [
        "Cache (s)", "Keep (days)", "Max unpacked (MB)", "S", "Write to", "Max s (GB)"]  # a lone "s" is a word
    assert cn.is_reserved("system") and cn.is_reserved("eyes") and cn.is_reserved("home-assistant")
    assert not cn.is_reserved("my-tracker")
    cfg.plugins.installed = {"p": {"source": "claude-plugins-official", "version": "", "sha": "a" * 40,
                                   "enabled": True, "installed_at": 1}}
    cfg.plugins.marketplaces = {"claude-plugins-official": "https://github.com/anthropics/claude-plugins-official"}
    assert [cn.trust_of(cfg, k, s) for k, s in (("builtin", {}), ("catalog", {}), ("custom", {}), ("plugin", {"plugin": "p"}),
                                                ("plugin", {"plugin": "gone"}))] == [
        "orion", "reviewed", "unreviewed", "reviewed", "unreviewed"]
    assert cn.sandboxed(cfg, "mine", {"command": "x"}) and not cn.sandboxed(cfg, "mine", {"command": "x", "sandbox": False})
    assert not cn.sandboxed(cfg, "mine", {"url": "https://x"}) and not cn.sandboxed(cfg, "mail", {"command": "uvx"})
    assert cn.sandboxed(cfg, "tracker", {"command": "x", "plugin": "p"})
    assert cn.connector_data(cfg, "mine") == cfg.data_path / "connectors" / "mine"


def test_orions_own_groups_are_connectors_with_their_own_settings(world: World) -> None:
    async def go() -> None:
        await world.start()
        items = await world.connectors.snapshot(local=True)
        by = {i["id"]: i for i in items}
        assert list(by) == ["system", "web", "watchers", "memory", "clipboard", "skills", "calendar"]
        system = by["system"]
        assert (system["kind"], system["trust"], system["sandbox"], system["removable"], system["auth"]) == (
            "builtin", "orion", "none", False, "none")
        assert system["title"] == "PC control" and system["icon"] == "monitor" and system["status"] == "connected"
        assert system["secrets"] == [] and system["signed_in"] is False and system["local"] is True
        assert system["tools"] == [
            {"name": "run_command", "title": "Run a shell command", "description": "run_command does it.",
             "risk": "destroy", "open_world": False, "enabled": True},
            {"name": "volume", "title": "Volume", "description": "volume does it.", "risk": "change",
             "open_world": False, "enabled": True}]
        block = system["settings"]
        assert block["kind"] == "fields" and block["values"]["system.command_timeout_s"] == 30
        timeout = next(f for f in block["fields"] if f["key"] == "system.command_timeout_s")
        assert timeout["kind"] == "number" and timeout["secret"] is False and timeout["required"] is False
        assert timeout["help"] and "options" in timeout and "placeholder" in timeout
        assert {f["kind"] for f in block["fields"]} == {"number", "list", "text"}
        web = by["web"]["settings"]["values"]
        assert "web.page_chars" in web and "weather.refresh_s" in web  # two sections, one connector
        assert by["web"]["tools"][0]["risk"] == "read" and by["web"]["tools"][0]["open_world"] is True
        assert by["memory"]["settings"] == {"kind": "none"}
        skills = by["skills"]["settings"]
        assert "skills.sources" in skills["values"] and "skills.disabled" not in skills["values"]
        watchers = next(f for f in by["watchers"]["settings"]["fields"] if f["key"] == "watchers.speak")
        assert watchers["kind"] == "switch"
        # a group that needs something this machine lacks: unavailable, saying what
        assert by["clipboard"]["status"] == "unavailable" and by["clipboard"]["needs"] == [CLIPBOARD_MISSING]
        assert by["system"]["needs"] == []
        await world.hub.stop()

    run(go())


def test_saving_the_options_of_a_group_and_nothing_else(world: World) -> None:
    async def go() -> None:
        await world.start()
        view = await world.connectors.save_settings("watchers", {"watchers.battery_low": 15, "watchers.speak": False})
        assert view["settings"]["values"]["watchers.battery_low"] == 15 and world.cfg.watchers.battery_low == 15
        assert world.cfg.watchers.speak is False
        assert world.titles() == ["Changed Watchers's settings"] and world.changed >= 1
        assert world.rows[0]["detail"] == {"changed": "watchers.battery_low, watchers.speak"}
        for key in ("server.host", "safety.confirm_after_content", "watchers.nope", "voice.chime", "openrouter.api_key"):
            with pytest.raises(ValueError, match=f"has no setting {key}"):
                await world.connectors.save_settings("watchers", {key: 1})
        with pytest.raises(ValueError, match="integer"):
            await world.connectors.save_settings("watchers", {"watchers.battery_low": "15"})  # strict, as everywhere
        with pytest.raises(ValueError, match="less than or equal to 60"):
            await world.connectors.save_settings("watchers", {"watchers.battery_low": 99})
        with pytest.raises(ValueError, match="no settings here|has no setting"):
            await world.connectors.save_settings("memory", {"memory.review": False})
        assert world.cfg.watchers.battery_low == 15
        with pytest.raises(LookupError):
            await world.connectors.save_settings("nope", {})
        await world.hub.stop()

    run(go())


def test_a_directory_connector_keeps_its_secret_in_the_vault_and_nowhere_else(world: World) -> None:
    async def go() -> None:
        await world.start()
        view = await world.connectors.add({"from": "directory", "id": "home-assistant", "values": HOME_ASSISTANT})
        assert (view["id"], view["kind"], view["trust"], view["auth"], view["removable"]) == (
            "home-assistant", "catalog", "reviewed", "fields", True)
        await world.until(lambda: world.prepared.get("home-assistant") is not None)
        server = world.cfg.mcp_servers["home-assistant"]
        assert server["headers"] == {"Authorization": "Bearer ${secret:connector.home-assistant.token}"}
        assert server["values"] == {"url": "http://ha.local:8123"} and server["url"] == "http://ha.local:8123/api/mcp"
        assert world.vault.get("connector.home-assistant.token") == "ha-long-lived-token"
        # the server connects with the real token, and only then
        assert world.prepared["home-assistant"]["headers"] == {"Authorization": "Bearer ha-long-lived-token"}
        everywhere = (world.tmp / "data" / "config.yaml").read_text() + json.dumps(view) + json.dumps(world.events)
        assert "ha-long-lived-token" not in everywhere + json.dumps(world.hub.status())
        block = view["settings"]
        assert block["values"] == {"url": "http://ha.local:8123", "token": ""}
        token = next(f for f in block["fields"] if f["key"] == "token")
        assert token["secret"] is True and token["set"] is True and token["required"] is True
        assert view["secrets"] == [{"name": "connector.home-assistant.token", "set": True}]
        assert view["status"] == "connected" and view["tools"][0]["name"] == "home-assistant__ping"
        assert world.titles() == ["Added the Home Assistant connector"]
        with pytest.raises(ValueError, match="added already"):
            await world.connectors.add({"from": "directory", "id": "home-assistant", "values": HOME_ASSISTANT})
        await world.hub.stop()

    run(go())


def test_what_is_missing_stops_an_install_and_leaves_nothing_behind(world: World) -> None:
    async def go() -> None:
        await world.start()
        with pytest.raises(ValueError, match="Long-lived access token is needed"):
            await world.connectors.add({"from": "directory", "id": "home-assistant",
                                        "values": {"url": "http://ha.local:8123"}})
        with pytest.raises(ValueError, match="Provider must be one of"):
            await world.connectors.add({"from": "directory", "id": "mail", "values": {
                "provider": "aol", "email": "a@b.c", "password": "pw"}})
        with pytest.raises(LookupError, match="nothing called fax"):
            await world.connectors.add({"from": "directory", "id": "fax"})
        with pytest.raises(ValueError, match="Say where"):
            await world.connectors.add({"id": "x"})
        assert world.vault.names() == () and not world.cfg.mcp_servers
        await world.hub.stop()

    run(go())


def test_changing_a_directory_connector_keeps_the_secret_unless_a_new_one_is_given(world: World) -> None:
    async def go() -> None:
        await world.start()
        await world.connectors.add({"from": "directory", "id": "home-assistant", "values": HOME_ASSISTANT})
        await world.until(lambda: world.prepared.get("home-assistant") is not None)
        world.prepared.clear()
        servers = dict(world.cfg.mcp_servers)
        servers["home-assistant"] = {**servers["home-assistant"], "disabled_tools": ["ping"], "timeout": 9}
        await world.apply_own({"mcp_servers": servers})  # what the user switched off stays through a change
        view = await world.connectors.save_settings("home-assistant", {"url": "http://other.local:8123/", "token": ""})
        await world.until(lambda: "home-assistant" in world.prepared)
        assert world.prepared["home-assistant"]["url"] == "http://other.local:8123/api/mcp"
        assert world.prepared["home-assistant"]["headers"] == {"Authorization": "Bearer ha-long-lived-token"}
        server = world.cfg.mcp_servers["home-assistant"]
        assert server["disabled_tools"] == ["ping"] and server["timeout"] == 9
        assert server["values"] == {"url": "http://other.local:8123/"} and view["settings"]["values"]["token"] == ""
        # a new token alone changes no setting: the link is connected again by hand, with it
        world.prepared.clear()
        await world.connectors.save_settings("home-assistant", {"token": "a-new-token"})
        await world.until(lambda: "home-assistant" in world.prepared)
        assert world.prepared["home-assistant"]["headers"] == {"Authorization": "Bearer a-new-token"}
        assert world.vault.get("connector.home-assistant.token") == "a-new-token"
        with pytest.raises(ValueError, match="has no setting colour"):
            await world.connectors.save_settings("home-assistant", {"colour": "red"})
        with pytest.raises(ValueError, match="Home Assistant's address is needed"):
            await world.connectors.save_settings("home-assistant", {"url": " "})
        assert world.cfg.mcp_servers["home-assistant"]["values"]["url"] == "http://other.local:8123/"
        await world.hub.stop()

    run(go())


def test_removing_a_connector_takes_its_secrets_and_files_with_it(world: World) -> None:
    async def go() -> None:
        await world.start()
        await world.connectors.add({"from": "directory", "id": "home-assistant", "values": HOME_ASSISTANT})
        world.vault.set("connector.home-assistant.extra", "x")
        world.vault.set("connector.other.token", "not this one's")
        data = cn.connector_data(world.cfg, "home-assistant")
        data.mkdir(parents=True)
        (data / "state").write_text("x")
        await world.connectors.remove("home-assistant")
        assert "home-assistant" not in world.cfg.mcp_servers and world.vault.names() == ("connector.other.token",)
        assert not data.exists() and "home-assistant" not in {s["name"] for s in world.hub.status()}
        assert world.titles()[-1] == "Removed the Home Assistant connector"
        for name, error, why in (("home-assistant", LookupError, "no connector called"), ("system", ValueError, "Orion's own"),
                                 ("nope", LookupError, "no connector")):
            with pytest.raises(error, match=why):
                await world.connectors.remove(name)
        await world.hub.stop()

    run(go())


def test_a_connector_of_the_users_own_runs_a_program_or_reaches_a_remote_server(world: World) -> None:
    async def go() -> None:
        await world.start()
        program = await world.connectors.add({"from": "custom", "name": "my-tool", "command": "uvx",
                                              "args": ["mcp-thing"], "env": {"API_KEY": "k-123", "HOME_DIR": "${HOME}"},
                                              "description": "My own tool"})
        assert (program["kind"], program["trust"], program["sandbox"], program["auth"]) == (
            "custom", "unreviewed", "bwrap", "fields")
        server = world.cfg.mcp_servers["my-tool"]
        assert server == {"command": "uvx", "args": ["mcp-thing"], "description": "My own tool", "env": {
            "API_KEY": "${secret:connector.my-tool.env.api_key}", "HOME_DIR": "${HOME}"}}
        assert world.vault.names() == ("connector.my-tool.env.api_key",) and world.vault.get(
            "connector.my-tool.env.api_key") == "k-123"
        await world.until(lambda: "my-tool" in world.prepared)
        assert world.prepared["my-tool"]["env"]["API_KEY"] == "k-123"
        remote = await world.connectors.add({"from": "custom", "name": "wire", "url": "https://x.test/mcp",
                                             "headers": {"Authorization": "Bearer abc"}})
        assert (remote["sandbox"], remote["settings"]) == ("none", {"kind": "none"})
        assert world.cfg.mcp_servers["wire"]["headers"] == {"Authorization": "${secret:connector.wire.header.authorization}"}
        sse = await world.connectors.add({"from": "custom", "name": "old", "url": "https://x.test/sse"})
        assert world.cfg.mcp_servers["old"]["transport"] == "sse" and sse["status"] in ("connecting", "connected")
        assert [s["secrets"] for s in (program, remote)] == [
            [{"name": "connector.my-tool.env.api_key", "set": True}],
            [{"name": "connector.wire.header.authorization", "set": True}]]
        await world.hub.stop()

    run(go())


@pytest.mark.parametrize(("body", "why"), [
    ({"name": "Bad Name", "command": "x"}, "lower-case letters"),
    ({"name": "system", "command": "x"}, "already"), ({"name": "mail", "command": "x"}, "already"),
    ({"name": "a"}, "either a command"), ({"name": "a", "command": "x", "url": "https://x.test"}, "not both"),
    ({"name": "a", "url": "ftp://x.test"}, "http:// or https://"),
    ({"name": "a", "command": "x", "args": "no"}, "list of texts"),
    ({"name": "a", "command": "x", "env": {"1BAD": "v"}}, "environment variable"),
    ({"name": "a", "url": "https://x.test", "headers": {"Bad Header": "v"}}, "header"),
    ({"name": "a", "url": "https://x.test", "env": {"A": "b"}}, "Headers belong"),
    ({"name": "a", "command": "x", "headers": {"A": "b"}}, "Headers belong")])
def test_a_custom_connector_that_is_not_one_is_refused(world: World, body: dict[str, Any], why: str) -> None:
    async def go() -> None:
        await world.start()
        with pytest.raises(ValueError, match=why):
            await world.connectors.add({"from": "custom", **body})
        assert world.vault.names() == () and "a" not in world.cfg.mcp_servers
        await world.hub.stop()

    run(go())


def test_the_sandbox_is_switched_for_a_program_only(world: World) -> None:
    async def go() -> None:
        await world.start()
        await world.connectors.add({"from": "custom", "name": "my-tool", "command": "uvx"})
        await world.connectors.add({"from": "custom", "name": "wire", "url": "https://x.test/mcp"})
        off = await world.connectors.set_sandbox("my-tool", False)
        assert off["sandbox"] == "none" and world.cfg.mcp_servers["my-tool"]["sandbox"] is False
        on = await world.connectors.set_sandbox("my-tool", True)
        assert on["sandbox"] == "bwrap" and "sandbox" not in world.cfg.mcp_servers["my-tool"]
        for name in ("wire", "system"):
            with pytest.raises(ValueError, match="own on this PC|Orion's own"):
                await world.connectors.set_sandbox(name, True)
        with pytest.raises(LookupError):
            await world.connectors.set_sandbox("nope", True)
        await world.hub.stop()

    run(go())


def test_switching_a_connector_and_its_tools(world: World) -> None:
    async def go() -> None:
        await world.start()
        await world.connectors.add({"from": "custom", "name": "my-tool", "command": "uvx"})
        await world.until(lambda: world.hub.tools().get("my-tool__ping") is not None)
        off = await world.connectors.set_enabled("my-tool", False)
        assert off["status"] == "off" and world.cfg.mcp_servers["my-tool"]["enabled"] is False
        assert (await world.connectors.set_enabled("my-tool", True))["status"] in ("connecting", "connected")
        await world.until(lambda: world.hub.tools().get("my-tool__ping") is not None)
        tool_off = await world.connectors.set_tool("my-tool", "my-tool__ping", False)
        assert tool_off["tools"][0]["enabled"] is False and world.cfg.mcp_servers["my-tool"]["disabled_tools"] == ["ping"]
        assert (await world.connectors.set_tool("my-tool", "ping", True))["tools"][0]["enabled"] is True
        for name, tool_name, why in (("my-tool", "nope", "no tool called nope"), ("nope", "x", "no connector")):
            with pytest.raises(LookupError, match=why):
                await world.connectors.set_tool(name, tool_name, False)
        with pytest.raises(LookupError):
            await world.connectors.set_enabled("nope", True)
        system = await world.connectors.set_enabled("system", False)  # his own groups switch off too
        assert system["status"] == "off"
        await world.hub.stop()

    run(go())


# ---------------------------------------------------------------------------------------------------- signing in
def test_a_remote_connector_that_wants_a_sign_in_is_signed_in_from_the_pc_only(world: World) -> None:
    async def go() -> None:
        async with world.fake.serving():
            await world.start()
            added = await world.connectors.add({"from": "custom", "name": "notes", "url": URL})
            await world.until(lambda: world.hub.status()[-1]["sign_in"])
            view = await world.view("notes")
            assert (view["status"], view["auth"], view["signed_in"], view["removable"]) == (
                "needs_sign_in", "oauth", False, True)
            assert added["name" if False else "id"] == "notes" and view["error"] == ""
            with pytest.raises(ValueError, match="Sign in from the PC"):
                await world.connectors.connect("notes", local=False)
            assert (await world.view("notes", local=False))["local"] is False
            link = await world.connectors.connect("notes", local=True)
            assert link["auth_url"].startswith(f"{BASE}/authorize?")
            ok, _ = await world.broker.callback(world.fake.authorize(link["auth_url"]))
            assert ok
            await world.until(lambda: world.hub.tools().get("notes__ping") is not None)
            signed = await world.view("notes")
            assert (signed["status"], signed["signed_in"], signed["secrets"]) == (
                "connected", True, [{"name": "connector.notes.oauth", "set": True}])
            assert "access_token" not in json.dumps(signed) + json.dumps(world.events)
            out = await world.connectors.disconnect("notes")
            await world.until(lambda: world.hub.status()[-1]["sign_in"])
            assert (await world.view("notes"))["status"] == "needs_sign_in" and out["signed_in"] is False
            assert json.loads(world.vault.get("connector.notes.oauth") or "{}").get("tokens") is None
            assert "Signed in to Notes" in world.titles("login") and "Signed out of Notes" in world.titles("login")
            with pytest.raises(ValueError, match="not a remote server"):
                await world.connectors.add({"from": "custom", "name": "prog", "command": "x"})
                await world.connectors.connect("prog", local=True)
            with pytest.raises(ValueError, match="Orion's own"):
                await world.connectors.connect("system", local=True)
            with pytest.raises(LookupError):
                await world.connectors.connect("nope", local=True)
            await world.hub.stop()

    run(go())


def test_an_oauth_app_of_the_users_own_signs_in_with_its_secret_from_the_vault(world: World) -> None:
    """Asana, Slack and Box give no sign-in to apps they don't know: the settings hold the app's ID and a reference to
    its secret; the broker gets the secret only as the connection is made."""
    world.fake.preregister("my-app", "http://localhost:8765/oauth/callback")
    world.vault.set("connector.notes.client_secret", "s3cr3t")

    async def go() -> None:
        async with world.fake.serving():
            world.cfg.mcp_servers = {"notes": {"url": URL, "oauth": {
                "client_id": "my-app", "client_secret": "${secret:connector.notes.client_secret}"}}}
            await world.start()
            view = await world.view("notes")
            assert view["status"] == "needs_sign_in" and view["auth"] == "oauth"
            link = await world.connectors.connect("notes", local=True)
            assert (await world.broker.callback(world.fake.authorize(link["auth_url"])))[0]
            await world.until(lambda: world.hub.tools().get("notes__ping") is not None)
            assert world.fake.secrets_seen == ["s3cr3t"] and ("POST", "/register") not in world.fake.requests
            assert "s3cr3t" not in json.dumps(await world.view("notes")) + (world.tmp / "data" / "config.yaml").read_text()
            await world.hub.stop()

    run(go())


def test_headers_of_the_users_own_mean_the_server_wants_no_oauth(world: World) -> None:
    async def go() -> None:
        async with world.fake.serving():
            world.cfg.mcp_servers = {"notes": {"url": URL, "headers": {"Authorization": "Bearer nope"}}}
            await world.start()
            await world.until(lambda: world.hub.status()[-1]["status"] == "error")
            state = world.hub.status()[-1]
            assert state["sign_in"] is False and not world.fake.requests[1:] or ("GET", "/.well-known/oauth-protected-resource/mcp") not in world.fake.requests
            await world.hub.stop()

    run(go())


# ---------------------------------------------------------------------------------------------------- environment
def test_the_environment_fills_a_servers_settings_before_the_secrets_so_a_secret_stays_as_it_is(
        world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROJECT_DIR", "/work/project")
    world.vault.set("connector.my-tool.env.password", "pa$$word-$PROJECT_DIR-${HOME}")

    async def go() -> None:
        world.cfg.mcp_servers = {"my-tool": {"command": "uvx", "args": ["--dir", "${PROJECT_DIR}"], "env": {
            "PASSWORD": "${secret:connector.my-tool.env.password}"}}}
        await world.start()
        await world.until(lambda: "my-tool" in world.prepared)
        assert world.prepared["my-tool"]["args"] == ["--dir", "/work/project"]
        assert world.prepared["my-tool"]["env"]["PASSWORD"] == "pa$$word-$PROJECT_DIR-${HOME}"  # not expanded again
        await world.hub.stop()

    run(go())


def test_a_secret_that_is_not_there_makes_the_connector_wait_instead_of_retrying(world: World) -> None:
    async def go() -> None:
        world.cfg.mcp_servers = {"my-tool": {"command": "uvx", "env": {"K": "${secret:connector.my-tool.env.k}"}}}
        await world.start()
        state = world.hub.status()[-1]
        view = await world.view("my-tool")
        assert state["status"] == "error" and "isn't set" in state["error"] and state["sign_in"] is False
        assert (view["status"], view["secrets"]) == ("needs_setup", [{"name": "connector.my-tool.env.k", "set": False}])
        assert "my-tool" not in world.prepared
        world.vault.set("connector.my-tool.env.k", "now")
        await world.hub.restart("my-tool")
        await world.until(lambda: world.prepared.get("my-tool") is not None)
        assert (await world.view("my-tool"))["status"] == "connected"
        await world.hub.stop()

    run(go())


# ---------------------------------------------------------------------------------------------------- plugins
@pytest.fixture
def net(world: World, monkeypatch: pytest.MonkeyPatch) -> Net:
    network = Net()
    monkeypatch.setattr(p, "_download", network)
    network.serve("acme/market", market_archive(world.tmp, ENTRIES, {"linear": LINEAR_FILES, "github": GITHUB_FILES}),
                  '"m1"')
    world.cfg.plugins.marketplaces = {"test-market": "https://github.com/acme/market"}
    return network


def test_a_plugins_connector_has_the_plugins_fields_and_waits_until_they_are_set(
        world: World, net: Net, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "orions-own-token")

    async def go() -> None:
        await world.start()
        await world.library.refresh()
        await world.library.install("test-market", "github")
        view = await world.view("github-github")
        assert (view["kind"], view["plugin"], view["trust"], view["removable"], view["sandbox"]) == (
            "plugin", "github", "unreviewed", False, "none")
        field = view["settings"]["fields"][0]
        assert field["key"] == "GITHUB_PERSONAL_ACCESS_TOKEN" and field["secret"] is True and field["set"] is False
        assert (view["status"], view["auth"]) == ("needs_setup", "fields")
        assert view["secrets"] == [{"name": "connector.github-github.github_personal_access_token", "set": False}]
        assert "github-github" not in world.prepared  # waiting: not tried, not retried
        with pytest.raises(ValueError, match="has no setting nope"):
            await world.connectors.save_settings("github-github", {"nope": "x"})
        saved = await world.connectors.save_settings("github-github", {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_mine"})
        await world.until(lambda: "github-github" in world.prepared)
        assert world.prepared["github-github"]["headers"] == {"Authorization": "Bearer ghp_mine"}  # not Orion's own variable
        assert saved["settings"]["fields"][0]["set"] is True and saved["settings"]["values"] == {
            "GITHUB_PERSONAL_ACCESS_TOKEN": ""}
        assert "ghp_mine" not in (world.tmp / "data" / "config.yaml").read_text() + json.dumps(saved)
        with pytest.raises(ValueError, match="comes with the github plugin"):
            await world.connectors.remove("github-github")
        await world.library.remove("github")
        assert world.vault.names() == () and "github-github" not in world.cfg.mcp_servers
        await world.hub.stop()

    run(go())


def test_the_directory_lists_orions_own_and_the_directorys_plugins_and_adds_either(world: World, net: Net) -> None:
    net.serve("acme/market", market_archive(world.tmp, [
        *ENTRIES, {"name": "ctx", "description": "Docs lookup.", "category": "development",
                   "source": "./external_plugins/ctx"},
        {"name": "ghpat", "description": "Uses a token.", "category": "development", "source": "./external_plugins/ghpat"}],
        {"linear": LINEAR_FILES, "github": GITHUB_FILES,
         "ctx": {".mcp.json": json.dumps({"mcpServers": {"ctx": {"type": "http", "url": "https://ctx.test/mcp"}}})},
         "ghpat": {".mcp.json": json.dumps({"ghpat": {"url": "https://gh.test/mcp", "headers": {
             "Authorization": "Bearer ${GH_TOKEN}"}}})}}), '"m2"')

    async def go() -> None:
        await world.start()
        await world.library.refresh()
        items = await world.connectors.directory()
        mine = [i for i in items if i["source"] == "orion"]
        theirs = {i["id"]: i for i in items if i["source"] == "claude-directory"}
        assert [i["id"] for i in mine][:3] == ["home-assistant", "browser", "mail"] and len(mine) == 18
        assert set(theirs) == {"ctx", "ghpat"}  # linear and github are in Orion's own list, which wins
        assert theirs["ctx"] == {"id": "ctx", "title": "Ctx", "description": "Docs lookup.", "icon": "plug",
                                 "category": "Development", "source": "claude-directory", "auth": "none",
                                 "fields": [], "installed": False, "docs": ""}
        assert theirs["ghpat"]["auth"] == "fields" and theirs["ghpat"]["fields"][0]["key"] == "GH_TOKEN"
        assert [i["id"] for i in await world.connectors.directory("slack")] == ["slack"]
        assert {i["id"] for i in await world.connectors.directory(category="files")} == {"dropbox", "box"}
        assert [i["id"] for i in await world.connectors.directory("docs lookup")] == ["ctx"]
        asana = next(i for i in mine if i["id"] == "asana")
        assert asana["auth"] == "oauth" and "http://localhost:8765/oauth/callback" in asana["docs"]
        assert [f["key"] for f in asana["fields"]] == ["client_id", "client_secret"]
        added = await world.connectors.add({"from": "directory", "id": "ghpat", "values": {"GH_TOKEN": "tok"}})
        await world.until(lambda: "ghpat" in world.prepared)
        assert added["kind"] == "plugin" and added["plugin"] == "ghpat"
        assert world.prepared["ghpat"]["headers"] == {"Authorization": "Bearer tok"}
        assert next(i for i in await world.connectors.directory() if i["id"] == "ghpat")["installed"] is True
        with pytest.raises(LookupError, match="nothing called nope"):
            await world.connectors.add({"from": "directory", "id": "nope"})
        await world.connectors.add({"from": "directory", "id": "ctx"})
        assert (await world.view("ctx"))["status"] in ("connecting", "connected")
        await world.hub.stop()

    run(go())


def test_a_calendar_is_a_source_of_the_calendar_connector(world: World) -> None:
    async def go() -> None:
        await world.start()
        view = await world.connectors.add({"from": "directory", "id": "calendar", "values": {
            "name": "Home", "kind": "caldav", "url": "https://caldav.icloud.com/", "username": "me",
            "password": "app-pw"}})
        assert view["id"] == "calendar" and view["kind"] == "builtin"
        assert world.cfg.calendar.sources == {"home": {"caldav_url": "https://caldav.icloud.com/", "username": "me",
                                                       "password": "app-pw"}}
        assert "home" not in json.dumps(view) and "app-pw" not in json.dumps(view)  # sources are secret: not shown
        listed = {i["id"]: i for i in await world.connectors.directory()}
        assert listed["calendar"]["installed"] is True
        with pytest.raises(ValueError, match="calendar's name uses"):
            await world.connectors.add({"from": "directory", "id": "calendar", "values": {
                "name": "bad name!", "kind": "ics", "url": "https://x/y.ics"}})
        assert world.titles() == ["Added the home calendar"]
        await world.hub.stop()

    run(go())


def test_a_server_that_says_what_gestures_it_sees_gets_the_gesture_editor(world: World) -> None:
    vocabulary = json.dumps({"gestures": [{"name": "victory", "title": "Victory", "kind": "shape", "how": "A V."}]})
    world.stand_ins["hands"] = server_of(tool("ping", read_only=True), resources={"orion://gestures": vocabulary,
                                                                                    "orion://intents": '{"intents": []}'})
    world.stand_ins["bridge"] = server_of(tool("ping", read_only=True))

    async def go() -> None:
        world.cfg.mcp_servers = {"hands": {"url": "http://pi.local:8770/mcp", "intents": True},
                                 "bridge": {"url": "http://other.local/mcp", "intents": True}}
        await world.start()
        views = {i["id"]: i for i in await world.connectors.snapshot(local=True)}
        assert views["hands"]["settings"] == {"kind": "gestures"} and views["hands"]["icon"] == "hand"
        assert views["bridge"]["settings"] == {"kind": "none"} and views["bridge"]["icon"] == "plug"
        server = {**world.cfg.mcp_servers["bridge"], "gestures": {"victory": {"do": "new_chat"}}}
        await world.apply_own({"mcp_servers": {**world.cfg.mcp_servers, "bridge": server}})
        assert (await world.view("bridge"))["settings"] == {"kind": "gestures"}  # bindings of its own are gestures too
        await world.hub.stop()

    run(go())


def test_what_each_connector_says_of_where_it_stands(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    slow = asyncio.Event()
    current = MCPHub._open

    async def hold(hub: MCPHub, stack: Any, link: Any) -> Any:
        if link.name == "dead":
            raise ConnectionError("it will not start")
        if link.name == "slow":
            await slow.wait()
        return await current(hub, stack, link)

    monkeypatch.setattr(MCPHub, "_open", hold)

    async def go() -> None:
        world.cfg.mcp_servers = {
            "up": {"command": "x"}, "down": {"command": "x", "enabled": False},
            "dead": {"command": "x"}, "slow": {"url": "http://slow.test/mcp"}}
        await world.hub.configure(wait_s=0.3)
        await world.until(lambda: {s["name"]: s["status"] for s in world.hub.status()}["up"] == "connected")
        states = {i["id"]: (i["status"], i["error"]) for i in await world.connectors.snapshot(local=True)}
        assert states["up"] == ("connected", "") and states["down"] == ("off", "")
        assert states["dead"] == ("error", "it will not start") and states["slow"][0] == "connecting"
        slow.set()
        await world.until(lambda: {s["name"]: s["status"] for s in world.hub.status()}["slow"] == "connected")
        await world.hub.stop()

    run(go())


def test_the_events_the_hud_hears_of_follow_changes(world: World) -> None:
    async def go() -> None:
        await world.start()
        before = world.changed
        await world.connectors.add({"from": "custom", "name": "my-tool", "command": "uvx"})
        await world.connectors.set_enabled("my-tool", False)
        await world.connectors.remove("my-tool")
        assert world.changed - before == 3
        await world.hub.stop()

    run(go())


def test_a_connectors_page_shows_what_it_did(world: World) -> None:
    async def go() -> None:
        await world.start()
        await world.connectors.add({"from": "custom", "name": "my-tool", "command": "uvx"})
        await world.connectors.set_enabled("my-tool", False)
        world.rows.append({"id": 99, "at": 1.0, "kind": "tool", "source": "voice", "actor": "the brain",
                           "connector": "other", "tool": "x", "title": "Used another", "detail": "my-tool", "outcome": "ok",
                           "turn": "", "ms": 5})  # mentions my-tool in its detail but belongs to another connector
        page = await world.connectors.detail("my-tool")
        assert [r["title"] for r in page["activity"]] == ["Switched My Tool off", "Added the My Tool connector"]
        assert page["id"] == "my-tool" and page["status"] == "off"
        with pytest.raises(LookupError):
            await world.connectors.detail("nope")
        await world.hub.stop()

    run(go())
