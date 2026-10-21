"""What the AI may do with the user's connectors and plugins: install, update and remove a plugin and add or remove an
MCP server only after the user said yes (asked where the request came from, never from Discord or a job, no answer is
no); its installs add connectors switched off; names kept for Orion's own and the directory's are not taken; the
directory is what he reads when asked what he can connect to. The hub, the library, the settings file and the
confirmer are real; the network and the servers are stand-ins."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from test_connectors import World
from test_library import ENTRIES, FAR_SHA, GITHUB_FILES, LINEAR_FILES, Net, market_archive

from orion import plugins as p
from orion.base import CONTEXT_META
from orion.tools.core import Controls, installed_words, plugin_line, server


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return World(tmp_path, monkeypatch)


@pytest.fixture
def net(world: World, monkeypatch: pytest.MonkeyPatch) -> Net:
    network = Net()
    monkeypatch.setattr(p, "_download", network)
    network.serve("acme/market", market_archive(world.tmp, ENTRIES, {"linear": LINEAR_FILES, "github": GITHUB_FILES}),
                  '"m1"')
    world.cfg.plugins.marketplaces = {"test-market": "https://github.com/acme/market"}
    return network


class User:
    """The user at the HUD: answers each question the way the list says (and keeps them), or doesn't answer."""

    def __init__(self, world: World, answers: list[bool | None]) -> None:
        self.asked: list[dict[str, Any]] = []
        self.answers = answers
        world.bus.subscribe(self.hear)
        self.world = world

    def hear(self, event: dict[str, Any]) -> None:
        if event["type"] == "confirm":
            self.asked.append(event)
            answer = self.answers.pop(0)
            if answer is not None:
                self.world.confirmer.answer(event["id"], answer)


@asynccontextmanager
async def brain(world: World, *, source: str = "voice") -> AsyncIterator[Callable[..., Any]]:
    """Call Orion's own tools as the model does, for a request that came from ``source``."""
    async with Client(server(Controls(world))) as client:  # type: ignore[arg-type]

        async def call(tool_name: str, /, **args: Any) -> tuple[bool, str]:
            result = await client.call_tool(tool_name, args, meta={CONTEXT_META: {"turn": "t1", "source": source,
                                                                              "device": "hud-1"}})
            return result.is_error, result.content[0].text  # type: ignore[union-attr]

        yield call


def run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


def test_a_plugin_is_installed_by_the_ai_only_with_the_users_yes(world: World, net: Net) -> None:
    user = User(world, [False, True])

    async def go() -> None:
        await world.start()
        await world.library.refresh()
        async with brain(world) as call:
            assert await call("plugins", action="list") == (False, "No plugin is installed.")
            error, text = await call("plugins", action="install", name="linear")
            assert error and text.startswith("The user said no, so it wasn't done.")
            assert "linear" not in world.cfg.plugins.installed and not world.titles("plugin")
            error, text = await call("plugins", action="install", name="linear")
            assert not error and text.startswith("Installed linear: 1 skill, 1 command, 1 connector.")
            assert "off until the user switches them on" in text
            first, second = user.asked
            assert (first["tool"], first["reason"], first["title"], first["source_kind"], first["device"]) == (
                "plugins", "always", "Install a plugin", "voice", "hud-1")
            assert first["action"] == "install a plugin (linear)" and "marketplace: test-market" in first["detail"]
            assert "about: Linear issues and projects." in second["detail"]
            (connector,) = [name for name, s in world.cfg.mcp_servers.items() if s.get("plugin") == "linear"]
            assert world.cfg.mcp_servers[connector]["enabled"] is False  # only the user switches a connector on
            (row,) = [r for r in world.rows if r["kind"] == "plugin"]
            assert (row["title"], row["actor"], row["source"]) == ("Installed the linear plugin",
                                                                   "the brain, with your yes", "voice")
            assert await call("plugins", action="list") == (False, "- linear, on: 1 skill, 1 command, 1 connector")
            error, text = await call("plugins", action="install", name="linear")  # nothing to ask about
            assert error and text == "linear is installed already: update it instead." and len(user.asked) == 2
            await world.hub.stop()

    run(go())


def test_no_answer_is_no_and_discord_and_jobs_are_not_even_asked(world: World, net: Net) -> None:
    user = User(world, [None])
    world.cfg.safety.confirm_timeout_s = 0.05

    async def go() -> None:
        await world.start()
        await world.library.refresh()
        async with brain(world) as call:
            error, text = await call("plugins", action="install", name="linear")
            assert error and text.startswith("The user didn't answer in time, so it wasn't done.")
        for source, where in (("discord", "Discord"), ("job", "a job")):
            async with brain(world, source=source) as call:
                error, text = await call("plugins", action="install", name="linear")
                assert error and text == ("Only the user changes connectors and plugins, in the HUD under Customize: "
                                          f"it isn't done from {where}.")
                error, text = await call("mcp_servers", action="add", name="mine", url="https://mine.test/mcp")
                assert error and "isn't done from" in text
        assert len(user.asked) == 1 and not world.cfg.plugins.installed and "mine" not in world.cfg.mcp_servers
        await world.hub.stop()

    run(go())


def test_updating_removing_and_switching_off_a_plugin(world: World, net: Net) -> None:
    user = User(world, [True, False, True])

    async def go() -> None:
        await world.start()
        await world.library.refresh()
        async with brain(world) as call:
            await call("plugins", action="install", name="linear")
            error, text = await call("plugins", action="disable", name="linear")  # switching off asks nothing
            assert not error and text == "linear is switched off." and len(user.asked) == 1
            assert world.cfg.plugins.installed["linear"]["enabled"] is False
            assert world.rows[-1]["actor"] == "the brain, with your yes"
            error, text = await call("plugins", action="update", name="linear")
            assert error and text.startswith("The user said no") and user.asked[-1]["title"] == "Update a plugin"
            error, text = await call("plugins", action="remove", name="linear")
            assert not error and text == "linear is removed." and "linear" not in world.cfg.plugins.installed
            assert user.asked[-1]["title"] == "Remove a plugin"
            error, text = await call("plugins", action="enable", name="linear")  # only the user switches a plugin on
            assert error and "Input should be 'list'" in text
            error, text = await call("plugins", action="remove", name="nope")
            assert error and "no plugin called nope" in text.lower()
        await world.hub.stop()

    run(go())


def test_a_search_reads_the_marketplaces_and_says_what_it_cant_find(world: World, net: Net) -> None:
    User(world, [])

    async def go() -> None:
        await world.start()
        await world.library.refresh()
        async with brain(world) as call:
            error, text = await call("plugins", action="search", query="issues")
            assert not error and text == "- linear (test-market, productivity): Linear issues and projects."
            error, text = await call("plugins", action="search", query="zzz")
            assert (error, text) == (False, "No plugin matches.")
            error, text = await call("plugins", action="install", name="nope")
            assert error and "No marketplace has a plugin called nope" in text
            error, text = await call("plugins", action="install", name="")
            assert error and text == "Say which plugin."
        await world.hub.stop()

    run(go())


def test_a_plugin_that_cant_be_fetched_is_said_in_words(world: World, net: Net) -> None:
    User(world, [True])

    async def go() -> None:
        await world.start()
        await world.library.refresh()
        net.down.add(f"https://codeload.github.com/acme/far/tar.gz/{FAR_SHA}")
        async with brain(world) as call:
            error, text = await call("plugins", action="install", name="far")
            assert error and text.startswith("It couldn't be fetched:") and "offline" in text
        assert "far" not in world.cfg.plugins.installed
        await world.hub.stop()

    run(go())


def test_the_ai_adds_a_server_with_a_yes_and_takes_no_name_orion_keeps(world: World) -> None:
    user = User(world, [False, True, True])

    async def go() -> None:
        await world.start()
        async with brain(world) as call:
            error, text = await call("mcp_servers", action="add", name="home-assistant", url="https://x.test/mcp")
            assert error and "a name Orion keeps" in text and not user.asked
            error, text = await call("mcp_servers", action="add", name="mine", url="https://mine.test/mcp")
            assert error and text.startswith("The user said no") and "mine" not in world.cfg.mcp_servers
            error, text = await call("mcp_servers", action="add", name="mine", url="https://mine.test/mcp",
                                     description="My tracker")
            assert not error and world.cfg.mcp_servers["mine"] == {"url": "https://mine.test/mcp",
                                                                   "description": "My tracker"}
            asked = user.asked[-1]
            assert asked["title"] == "Add an MCP server" and asked["action"] == "add an MCP server (https://mine.test/mcp)"
            assert "name: mine" in asked["detail"]
            error, text = await call("mcp_servers", action="remove", name="mine")
            assert not error and "mine" not in world.cfg.mcp_servers and user.asked[-1]["title"] == "Remove an MCP server"
            error, text = await call("mcp_servers", action="disable", name="web")  # making less of him asks nothing
            assert not error and len(user.asked) == 3
        await world.hub.stop()

    run(go())


def test_a_server_that_comes_with_a_plugin_is_removed_with_the_plugin(world: World, net: Net) -> None:
    User(world, [True])

    async def go() -> None:
        await world.start()
        await world.library.refresh()
        await world.library.install("test-market", "linear")
        (connector,) = [name for name, s in world.cfg.mcp_servers.items() if s.get("plugin") == "linear"]
        async with brain(world) as call:
            error, text = await call("mcp_servers", action="remove", name=connector)
            assert error and text == (f"{connector} comes with the linear plugin: remove the plugin, or switch "
                                      f"{connector} off.")
        await world.hub.stop()

    run(go())


def test_the_integrations_tool_reads_the_directory(world: World, net: Net) -> None:
    async def go() -> None:
        await world.start()
        await world.library.refresh()
        async with brain(world) as call:
            error, text = await call("integrations", query="slack")
            assert not error and text.startswith("- Slack (") and ": not set up, needs a sign-in." in text
            error, text = await call("integrations", query="nothing like this")
            assert (error, text) == (False, "Nothing in the directory matches.")
            error, text = await call("integrations")
            assert not error and text.count("\n") >= 17 and "Home Assistant" in text
        await world.hub.stop()

    run(go())


def test_plugins_in_words() -> None:
    plugin: dict[str, Any] = {"name": "pdf", "version": "1.2.0", "enabled": False,
                              "components": {"skills": ["pdf:fill", "pdf:merge"], "commands": [], "connectors": ["x"]},
                              "update": {"version": "1.3.0", "sha": "a" * 40}}
    assert plugin_line(plugin) == ("- pdf 1.2.0, off: 2 skills, 0 commands, 1 connector; a newer version is out "
                                   "(1.3.0)")
    assert installed_words({**plugin, "components": {**plugin["components"], "connectors": []}}).endswith(
        "2 skills, 0 commands, 0 connectors; a newer version is out (1.3.0).")
