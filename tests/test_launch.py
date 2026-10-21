"""How servers are started: an environment of their own and never Orion's, only their own secrets, and (when nobody
reviewed them) the sandbox with a folder of their own as home."""

import asyncio
import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace

import pytest

import orion
from orion import sandbox, secrets
from orion.config import default_config
from orion.connectors import Connectors
from orion.mcp import MCPHub, OpTrace, _Link
from orion.plugins import SetupNeeded

REAL_ROOT = Path(orion.__file__).resolve().parent.parent
TOKEN = "sk-ant-oat01-" + "Zx9Qm4Lp7R" * 4


def hub_for(cfg, **kw) -> MCPHub:
    return MCPHub(cfg, {}, ops=OpTrace(lambda kind, **data: None, cfg.hud, recent=10), **kw)


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-" + "a1" * 32)
    monkeypatch.setenv("LC_ALL", "C")
    monkeypatch.setenv("LC_API_KEY", "x" * 20)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")


def test_a_trusted_server_starts_with_its_own_environment_and_the_desktop(environment: None, tmp_path: Path) -> None:
    cfg = default_config()
    params = hub_for(cfg)._launch("mail", {"command": "uvx", "args": ["mcp-email-server", "stdio"], "cwd": "/srv",
                                           "env": {"MCP_EMAIL_SERVER_PASSWORD": "the-app-password"}})
    assert params.command.endswith("uvx") or params.command == "uvx"
    assert params.args == ["mcp-email-server", "stdio"] and params.cwd == "/srv"
    env = params.env or {}
    assert env["MCP_EMAIL_SERVER_PASSWORD"] == "the-app-password" and env["LC_ALL"] == "C"
    assert env["DISPLAY"] == ":0" and "DBUS_SESSION_BUS_ADDRESS" in env  # a server he trusts may draw windows
    assert env["HOME"] == str(Path.home()) and "PATH" in env
    assert not {"CLAUDE_CODE_OAUTH_TOKEN", "OPENROUTER_API_KEY", "LC_API_KEY"} & env.keys()  # none of Orion's own


def test_his_own_servers_know_where_his_settings_are(environment: None, fake_machine: None, tmp_path: Path) -> None:
    cfg = default_config()
    hub = hub_for(cfg, spawn_env={"ORION_CONFIG": str(tmp_path / "config.yaml")})
    own = hub._launch("discord", {"command": "orion", "args": ["discord-serve"], "env": {"DISCORD_TOKEN": "bot-token-value"}})
    assert own.command == sys.executable and own.args == ["-m", "orion", "discord-serve"]
    assert own.env["ORION_CONFIG"] == str(tmp_path / "config.yaml") and own.env["DISCORD_TOKEN"] == "bot-token-value"
    other = hub._launch("x", {"command": "uvx"})
    assert "ORION_CONFIG" not in (other.env or {})  # only his own


def test_the_tools_process_gets_no_variable_that_looks_secret(environment: None, monkeypatch: pytest.MonkeyPatch) -> None:
    import mcp
    import mcp.client.stdio

    seen: list[object] = []

    def fake_stdio(params, errlog=None):
        seen.append(params)

    class Stop(Exception):
        pass

    def fake_client(*args, **kwargs):
        raise Stop

    monkeypatch.setattr(mcp.client.stdio, "stdio_client", fake_stdio)
    monkeypatch.setattr(mcp, "Client", fake_client)
    cfg = default_config()
    hub = hub_for(cfg, spawn=(sys.executable, "-m", "orion", "mcp-serve", "--hub"), spawn_env={"ORION_CONFIG": "/x/config.yaml"})

    async def run() -> None:
        with contextlib.suppress(Stop):
            async with AsyncExitStack() as stack:
                await hub._open(stack, _Link("tools", "tools"))

    asyncio.run(run())
    env = seen[0].env  # type: ignore[attr-defined]
    assert env["DISPLAY"] == ":0" and env["ORION_CONFIG"] == "/x/config.yaml" and env["LC_ALL"] == "C"
    assert not {"CLAUDE_CODE_OAUTH_TOKEN", "OPENROUTER_API_KEY", "LC_API_KEY"} & env.keys()


def test_a_server_fills_in_only_its_own_secrets_when_it_connects(tmp_path: Path) -> None:
    vault = secrets.Vault("files", tmp_path / "v")
    vault.set("connector.mail.password", "the-app-password")
    vault.set("openrouter.api_key", "sk-or-v1-" + "a1" * 32)
    cfg = default_config()
    hub = hub_for(cfg)

    async def connect(name: str, env: dict[str, str]) -> None:
        link = _Link(name, "external", {"command": "uvx", "env": env})
        async with AsyncExitStack() as stack:
            await hub._open(stack, link)

    with secrets.installed(vault):
        with pytest.raises(secrets.VaultError, match="isn't one of this server's secrets"):
            asyncio.run(connect("mail", {"K": "${secret:openrouter.api_key}"}))  # another's: refused before anything starts
        with pytest.raises(secrets.VaultError, match="isn't one of this server's secrets"):
            asyncio.run(connect("evil", {"K": "${secret:connector.mail.password}"}))
        with pytest.raises(secrets.VaultError, match="isn't set"):
            asyncio.run(connect("mail", {"K": "${secret:connector.mail.nothing}"}))


def test_with_the_connectors_layer_a_server_still_fills_in_only_its_own_secrets(tmp_path: Path) -> None:
    """The connectors layer (``Connectors.prepare``, the hub's hook) resolves the secrets as a server connects: a server
    asking for another's, or the user's own, is refused before anything is read or started."""
    vault = secrets.Vault("files", tmp_path / "v")
    vault.set("connector.mail.password", "the-app-password")
    vault.set("openrouter.api_key", "sk-or-v1-" + "a1" * 32)
    host = SimpleNamespace(cfg=default_config(), vault=vault)
    hub = hub_for(host.cfg)
    hub.hooks = Connectors(host)  # type: ignore[arg-type]

    def prepared(name: str, env: dict[str, str]) -> dict:
        return asyncio.run(hub.prepared(name, {"command": "uvx", "env": env}))

    assert prepared("mail", {"PASSWORD": "${secret:connector.mail.password}"})["env"] == {"PASSWORD": "the-app-password"}
    with pytest.raises(secrets.VaultError, match=r"isn't one of mail's secrets"):
        prepared("mail", {"K": "${secret:openrouter.api_key}"})
    with pytest.raises(secrets.VaultError, match=r"isn't one of evil's secrets"):
        prepared("evil", {"K": "x ${secret:connector.mail.password}"})
    with pytest.raises(SetupNeeded, match="isn't set"):
        prepared("mail", {"K": "${secret:connector.mail.nothing}"})


def sandboxed(tmp_path: Path) -> tuple[MCPHub, Path]:
    cfg = default_config().model_copy(deep=True)
    cfg.data_dir = str(tmp_path / "data")
    cfg.safety.sandbox_hidden = [str(tmp_path / ".ssh")]
    return hub_for(cfg, config_file=tmp_path / "data" / "config.yaml"), tmp_path / "data" / "connectors" / "plugin-x"


@pytest.fixture
def fake_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "_status", (sandbox.time.monotonic() + 10**6, sandbox.Status("/usr/bin/bwrap", True, 8, "")))


def test_a_server_nobody_reviewed_starts_in_the_sandbox_with_a_home_of_its_own(
        environment: None, fake_machine: None, tmp_path: Path) -> None:
    hub, folder = sandboxed(tmp_path)
    params = hub._launch("plugin-x", {"command": "uvx", "args": ["tool"], "env": {"API_TOKEN": "its-own"}})
    assert params.command == "/usr/bin/bwrap" and params.env == {} and params.cwd is None and folder.is_dir()
    line = [params.command, *params.args]
    assert ["--setenv", "HOME", str(folder)] == line[line.index("HOME") - 1:line.index("HOME") + 2]
    assert ["--bind", str(folder), str(folder)] in [line[i:i + 3] for i in range(len(line) - 2)]
    assert "--share-net" in line and "--chdir" in line and line[line.index("--chdir") + 1] == str(folder)
    setenv = {line[i + 1]: line[i + 2] for i, part in enumerate(line) if part == "--setenv"}
    assert setenv["API_TOKEN"] == "its-own" and not {"DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "CLAUDE_CODE_OAUTH_TOKEN",
                                                       "OPENROUTER_API_KEY"} & setenv.keys()  # not even the desktop's
    assert line[-2:] == [shutil.which("uvx") or "uvx", "tool"]


def test_a_server_nobody_reviewed_cannot_start_where_no_sandbox_can_be_made(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sandbox, "_status", (sandbox.time.monotonic() + 10**6, sandbox.Status("", False, 8, "bubblewrap isn't installed")))
    hub, _ = sandboxed(tmp_path)
    with pytest.raises(sandbox.SandboxError, match="bubblewrap isn't installed"):
        hub._launch("x", {"command": "uvx"})  # nobody reviewed it: never quietly run outside the box it belongs in
    assert hub._launch("x", {"command": "uvx", "sandbox": False}).command  # the user said otherwise: it starts as always
    assert hub._launch("mail", {"command": "uvx"}).command  # one of the directory's is reviewed: no box


def test_in_a_container_a_server_nobody_reviewed_gets_its_folder_as_home_and_no_desktop_without_a_sandbox_inside_it(
        environment: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The container is the box: no bubblewrap (there may be none that works), but the same environment and home."""
    monkeypatch.setattr("orion.mcp.profile", lambda **kw: SimpleNamespace(container=True))
    hub, folder = sandboxed(tmp_path)
    params = hub._launch("plugin-x", {"command": "uvx", "args": ["tool"], "env": {"API_TOKEN": "its-own"}})
    assert params.command == (shutil.which("uvx") or "uvx") and params.args == ["tool"] and params.cwd == str(folder)
    env = params.env or {}
    assert env["HOME"] == str(folder) and env["API_TOKEN"] == "its-own" and folder.is_dir()
    assert not {"DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "CLAUDE_CODE_OAUTH_TOKEN", "OPENROUTER_API_KEY"} & env.keys()


@pytest.mark.skipif(not (sandbox.status().works and sandbox.status().network), reason="no sandbox with the network here")
def test_a_sandboxed_server_really_runs_with_its_own_home_and_sees_nothing_of_orions(
        environment: None, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = Path(tempfile.mkdtemp(prefix=".sandbox-test-", dir=REAL_ROOT))
    try:
        (folder / ".ssh").mkdir()
        (folder / ".ssh" / "id_rsa").write_text("PLANTED-PRIVATE-KEY")
        cfg = default_config().model_copy(deep=True)
        cfg.data_dir = str(folder / "data")
        cfg.safety.sandbox_hidden = [str(folder / ".ssh")]
        hub = hub_for(cfg, config_file=folder / "data" / "config.yaml")
        script = "echo HOME=$HOME; env | grep -c -E 'TOKEN|API_KEY|DISPLAY|DBUS' ; cat " + str(folder / ".ssh" / "id_rsa") \
            + " 2>&1 | head -1; touch ~/state && echo wrote-home; touch " + str(folder / "outside") + " 2>&1 | head -1"
        params = hub._launch("plugin-x", {"command": "bash", "args": ["-c", script]})
        done = subprocess.run([params.command, *params.args], capture_output=True, text=True, env=params.env, timeout=30, check=False)
        data = folder / "data" / "connectors" / "plugin-x"
        assert f"HOME={data}" in done.stdout and "wrote-home" in done.stdout and (data / "state").exists()
        assert "PLANTED" not in done.stdout and "No such file" in done.stdout and "Read-only file system" in done.stdout
        assert "\n0\n" in done.stdout and not (folder / "outside").exists()
        assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] not in done.stdout and done.returncode == 0
    finally:
        shutil.rmtree(folder, ignore_errors=True)
