"""catalog: each integration builds an ordinary MCP server (or a calendar source) from the fields the user fills, the
same wherever Orion runs except the browser (its own container in Docker), with safe choices by default."""

import re
from pathlib import Path

import pytest

from orion.catalog import CATALOG, CATEGORIES, MAIL_PROVIDERS, NODE, entry, ids, settings_for

LUCIDE = Path(__file__).resolve().parents[1] / "web" / "node_modules" / "lucide-react" / "dist" / "esm" / "icons"


def build(integration_id: str, container: bool = False, **values: object) -> dict:
    return settings_for(entry(integration_id), values, container=container)


def test_every_integration_has_its_fields_and_a_unique_id() -> None:
    names = [i.id for i in CATALOG]
    assert len(names) == len(set(names)) and ids() == set(names)
    assert {"home-assistant", "browser", "mail", "google", "microsoft", "calendar", "discord", "notion", "linear",
            "asana", "atlassian", "slack", "dropbox", "box", "zapier", "canva", "github", "exa"} <= set(names)
    assert all(i.title and i.description and i.category in CATEGORIES for i in CATALOG)
    assert all(re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,40}", i.id) for i in CATALOG)  # an MCP server's name
    assert all(i.fields for i in CATALOG if not i.oauth or i.id in ("asana", "slack", "box"))
    with pytest.raises(LookupError):
        entry("fax")


def test_the_icons_are_lucide_names_the_hud_has() -> None:
    """The HUD draws a connector's icon by its lucide-react name: all of ours exist there (checked when the HUD's
    packages are installed)."""
    if not LUCIDE.is_dir():
        pytest.skip("the HUD's packages aren't installed")
    missing = [i.icon for i in CATALOG if not (LUCIDE / f"{i.icon}.mjs").exists()]
    assert missing == []


def test_how_each_integration_signs_in() -> None:
    """Remote servers that register Orion sign the user in; three need an OAuth app of the user's own; a token or an
    address is a field; the browser, whose token is optional, and Exa, whose key is, still take their settings."""
    assert {i.id for i in CATALOG if i.auth == "oauth"} == {"notion", "linear", "asana", "atlassian", "slack",
                                                           "dropbox", "box", "zapier", "canva"}
    own_app = {i.id for i in CATALOG if i.oauth and i.fields}
    assert own_app == {"asana", "slack", "box"} and all(
        [f.key for f in entry(i).fields] == ["client_id", "client_secret"] for i in own_app)
    assert all("{redirect}" in entry(i).docs and "http://localhost:8765/oauth/callback" in entry(i).docs_for(
        "http://localhost:8765/oauth/callback") for i in own_app)
    assert entry("home-assistant").auth == entry("github").auth == entry("exa").auth == "fields"
    assert entry("browser").auth == "fields" and entry("calendar").auth == "fields"


def test_remote_servers_are_their_makers_own_urls_and_sign_in_by_oauth() -> None:
    """Each remote connector is one https URL (checked against its plugin's .mcp.json); the ones that register Orion
    by themselves carry an empty oauth, the others the user's app (its secret as the setting that stands for it)."""
    urls = {"notion": "https://mcp.notion.com/mcp", "linear": "https://mcp.linear.app/mcp",
            "atlassian": "https://mcp.atlassian.com/v2/mcp", "dropbox": "https://mcp.dropbox.com/claude_app_mcp",
            "zapier": "https://mcp.zapier.com/api/v1/connect", "canva": "https://mcp.canva.com/mcp"}
    for name, url in urls.items():
        built = build(name)
        assert built["url"] == url and built["oauth"] == {} and "command" not in built
    mine = {"client_id": "id-1", "client_secret": "${secret:connector.asana.client_secret}"}
    assert build("asana", **mine) == {"url": "https://mcp.asana.com/v2/mcp", "oauth": mine,
                                      "description": "Asana: tasks and projects"}
    assert build("slack", **mine)["url"] == "https://mcp.slack.com/mcp"
    assert build("box", **mine)["url"] == "https://mcp.box.com"
    with pytest.raises(ValueError, match="OAuth client ID is needed"):
        build("asana", client_secret="x")


def test_github_takes_a_token_and_exa_works_without_a_key() -> None:
    github = build("github", token="${secret:connector.github.token}")
    assert github["url"] == "https://api.githubcopilot.com/mcp/"
    assert github["headers"] == {"Authorization": "Bearer ${secret:connector.github.token}"}
    with pytest.raises(ValueError, match="Personal access token is needed"):
        build("github")
    assert "headers" not in build("exa") and build("exa")["url"] == "https://mcp.exa.ai/mcp"
    assert build("exa", api_key=" k ")["headers"] == {"x-api-key": "k"}


def test_home_assistant_uses_its_mcp_endpoint_with_the_token() -> None:
    s = build("home-assistant", url="http://ha.local:8123/", token="tok")
    assert s["url"] == "http://ha.local:8123/api/mcp" and s["headers"] == {"Authorization": "Bearer tok"}
    with pytest.raises(ValueError, match="Long-lived access token is needed"):
        build("home-assistant", url="http://ha.local:8123", token=" ")


def test_the_browser_runs_with_node_from_pypi_or_as_the_docker_sidecar() -> None:
    native = build("browser")
    assert native["command"] == "uvx" and native["args"][:len(NODE)] == NODE and "--extension" not in native["args"]
    assert "browser_run_code_unsafe" in native["disabled_tools"] and "env" not in native
    own_chrome = build("browser", extension_token="t0k")
    assert "--extension" in own_chrome["args"] and own_chrome["env"] == {"PLAYWRIGHT_MCP_EXTENSION_TOKEN": "t0k"}
    docker = build("browser", container=True)
    assert docker["url"].endswith(":8931/mcp") and "command" not in docker


def test_mail_writes_drafts_unless_sending_is_allowed() -> None:
    s = build("mail", provider="icloud", email="me@icloud.com", password="app-pw")
    env = s["env"]
    assert (env["MCP_EMAIL_SERVER_IMAP_HOST"], env["MCP_EMAIL_SERVER_SMTP_PORT"]) == ("imap.mail.me.com", "587")
    assert env["MCP_EMAIL_SERVER_SMTP_START_SSL"] == "true" and env["MCP_EMAIL_SERVER_SMTP_SSL"] == "false"
    assert env["MCP_EMAIL_SERVER_ALLOWED_MUTATIONS"] == "draft,organize"
    assert env["MCP_EMAIL_SERVER_ALLOWED_RECIPIENTS"] == ""  # sending off
    sending = build("mail", provider="other", email="me@x.org", password="p", imap_host="imap.x.org",
                    smtp_host="smtp.x.org", allow_send=True)["env"]
    assert sending["MCP_EMAIL_SERVER_ALLOWED_MUTATIONS"].endswith(",send") and sending[
        "MCP_EMAIL_SERVER_SMTP_HOST"] == "smtp.x.org"
    bridge = build("mail", provider="proton", email="me@proton.me", password="b")["env"]
    assert bridge["MCP_EMAIL_SERVER_IMAP_START_SSL"] == "true" and bridge["MCP_EMAIL_SERVER_IMAP_VERIFY_SSL"] == "false"
    with pytest.raises(ValueError, match="Provider must be one of"):
        build("mail", provider="aol", email="a", password="b")
    with pytest.raises(ValueError, match="give the mail servers"):
        build("mail", provider="other", email="a", password="b")
    assert set(MAIL_PROVIDERS) >= {"icloud", "fastmail", "gmail"}


def test_discord_runs_this_install_and_pushes_messages() -> None:
    s = build("discord", token="bot", users="42")
    assert s["command"] == "orion" and s["args"] == ["discord-serve"] and s["intents"] is True
    assert s["env"] == {"DISCORD_TOKEN": "bot", "ORION_DISCORD_USERS": "42", "ORION_DISCORD_CHANNELS": ""}


def test_a_calendar_source_and_the_google_and_microsoft_servers() -> None:
    assert build("calendar", name="home", kind="caldav", url="https://caldav.icloud.com/", username="me",
                 password="pw") == {"caldav_url": "https://caldav.icloud.com/", "username": "me", "password": "pw"}
    assert build("calendar", name="work", kind="ics", url="https://x/y.ics") == {"ics_url": "https://x/y.ics"}
    google = build("google", email="me@gmail.com", client_id="id", client_secret="sec")
    assert "gmail:drafts" in google["args"] and google["env"]["USER_GOOGLE_EMAIL"] == "me@gmail.com"
    ms = build("microsoft", account="personal")
    assert "--read-only" in ms["args"] and ms["env"]["MS365_MCP_TENANT_ID"] == "consumers"
