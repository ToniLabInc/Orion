"""Every test runs against temporary folders: no test can read or write the real data/ (data/config.yaml holds an
API key, data/memory and data/orion.db are personal)."""

from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from orion import config, redact
from orion.base import LLMError, Tool
from orion.llm import Purpose, Usage


class FakeMeter:
    """A stand-in for the OpenRouter spend meter (``orion.spend.Spend``): what was checked and counted, and an error
    to refuse every request with.

    Attributes:
        checked: the models asked, one per request.
        counted: (model, purpose, usage) per answer.
        refuse: the error ``check`` raises, None to let requests pass.
    """

    def __init__(self, refuse: LLMError | None = None) -> None:
        self.checked: list[str] = []
        self.counted: list[tuple[str, Purpose, Usage]] = []
        self.refuse = refuse

    def check(self, model: str) -> None:
        self.checked.append(model)
        if self.refuse is not None:
            raise self.refuse

    def record(self, *, model: str, purpose: Purpose, usage: Usage) -> None:
        self.counted.append((model, purpose, usage))


@pytest.fixture
def meter() -> FakeMeter:
    """A meter that lets every request pass and remembers them."""
    return FakeMeter()


IDLE_REDACTOR = redact.active()  # what a process that installed nothing uses: it knows no secret


@pytest.fixture(autouse=True)
def an_idle_redactor() -> Iterator[None]:
    """Every test starts with no secret known to the process (``secrets.protect`` installs one for good: a test that
    starts a runtime would otherwise make the streaming of every test after it hold back its last word)."""
    redact.install(IDLE_REDACTOR)
    yield
    redact.install(IDLE_REDACTOR)


@pytest.fixture(autouse=True)
def not_in_a_container(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test behaves the same on a PC and inside a container (the Docker image, a devcontainer): Orion treats a
    container as its own sandbox, so a test that wants that case says so (tests/test_host.py, ``container=True``)."""
    from orion import host

    monkeypatch.setattr(host, "in_container", lambda: False)


@pytest.fixture(autouse=True)
def never_your_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the settings file, the folder a relative data_dir is counted from, and HOME at temporary folders."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ORION_CONFIG", str(tmp_path / "data" / "config.yaml"))
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "data" / "config.yaml")


@pytest.fixture(scope="session")
def toolbox() -> "Mapping[str, Tool]":
    """Orion's real tools (the built-in groups, his controls, the jobs and the machine's eyes), as the hub lists them
    to a model: names, hints and argument schemas. They are served in-process and listed; none is ever run. The hub
    offers only what the machine can do, so they are listed as on a full desktop: the same on a laptop and on a CI box
    with no sound server."""
    import asyncio
    import time
    from types import SimpleNamespace

    from orion import host
    from orion.config import default_config
    from orion.mcp import MCPHub, OpTrace
    from orion.tools import build_server, builtin_groups
    from orion.tools.automation import JOBS, WATCHERS
    from orion.tools.core import CORE, Controls
    from orion.tools.core import server as core_server
    from orion.tools.device import CLIPBOARD, EYES

    cfg = default_config()
    groups = builtin_groups()
    trace = OpTrace(lambda kind, /, **data: None, cfg.hud, recent=1)
    hubs = [MCPHub(cfg, {}, ops=trace, core=build_server(groups.values()),
                   core_groups={name: group.info for name, group in groups.items()}),
            MCPHub(cfg, {}, ops=trace, core=core_server(Controls(SimpleNamespace(cfg=cfg))),  # type: ignore[arg-type]
                   core_groups={CORE.name: CORE, EYES.name: EYES, CLIPBOARD.name: CLIPBOARD, JOBS.name: JOBS,
                                WATCHERS.name: WATCHERS})]

    async def listed() -> dict[str, Tool]:
        """Connect both, take their tools, disconnect."""
        tools: dict[str, Tool] = {}
        for hub in hubs:
            await hub.configure(wait_s=30)
            tools |= hub.tools()
            await hub.stop()
        return tools

    desktop = host.HostProfile("linux", False, "wayland", "ubuntu:GNOME", True, "xdg-open", "wpctl", True, True, True, True,
                               "wl", "portal", "bwrap", "/usr/bin/restic")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(host, "_cached", (time.monotonic() + 10**6, desktop))  # what host.profile() answers
        return asyncio.run(listed())
