"""The connectors directory: what Orion can be connected to in a click, each set up from a few fields the HUD asks for
(Customize, Connectors, Discover). Installing one writes an ordinary MCP server under ``mcp_servers`` (or, for
calendars, a source under ``calendar.sources``): nothing here is special afterwards, and every server can still be
edited, switched off or removed like any other. Plugins from Claude's directory bring more (``orion.plugins``).

Each server runs where Orion runs, natively or in his container: Python servers through ``uvx``, Node ones through
``uvx --from nodejs-wheel npx`` (Node from PyPI: nothing to install but uv), his own ones as ``orion …``; the remote
ones are the maker's own hosted servers. Versions are pinned, and tools that run arbitrary code in a page are switched
off from the start.

How each one signs in (``Integration.auth``):
- "fields": the user gives a token, a key or an address (the fields below); a secret goes to the vault, and the
  server's settings hold ``${secret:connector.<id>.<field>}`` in its place, which the hub fills in as it connects;
- "oauth": a remote server that signs the user in in their browser (``orion.oauth``): most register Orion by
  themselves; a few (``own_app``) give no such registration, so the user creates an OAuth app of their own at the
  maker's console and gives its client ID and secret.

Every remote URL below is the one its maker publishes in its Claude plugin's ``.mcp.json`` (Asana's and Box's, which
publish none, in the plugin's own setup notes), checked to answer with an OAuth challenge.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

from orion.base import JSON

type Auth = Literal["none", "fields", "oauth"]

NODE: Final = ["--from", "nodejs-wheel==24.19.0", "npx", "-y"]  # uvx args that give npx (Node from PyPI)
PLAYWRIGHT_MCP: Final = "@playwright/mcp@0.0.83"
MS365_MCP: Final = "@softeria/ms-365-mcp-server@0.160.0"
EMAIL_MCP: Final = "mcp-email-server@1.12.0"
WORKSPACE_MCP: Final = "workspace-mcp@2.1.0"
BROWSER_IMAGE_URL: Final = "http://127.0.0.1:8931/mcp"  # the browser sidecar of compose.yaml (profile "browser")
# The remote servers of Anthropic's directory, each URL read from its plugin in anthropics/claude-plugins-official: the
# plugin's .mcp.json (Asana's names its server in plugin.json; Box's is the mcp.json of box/skills; Exa's plugin adds a
# ?client= tag that is left off here).
NOTION_MCP: Final = "https://mcp.notion.com/mcp"
LINEAR_MCP: Final = "https://mcp.linear.app/mcp"
ASANA_MCP: Final = "https://mcp.asana.com/v2/mcp"
ATLASSIAN_MCP: Final = "https://mcp.atlassian.com/v2/mcp"
SLACK_MCP: Final = "https://mcp.slack.com/mcp"
DROPBOX_MCP: Final = "https://mcp.dropbox.com/claude_app_mcp"
BOX_MCP: Final = "https://mcp.box.com"
ZAPIER_MCP: Final = "https://mcp.zapier.com/api/v1/connect"
CANVA_MCP: Final = "https://mcp.canva.com/mcp"
GITHUB_MCP: Final = "https://api.githubcopilot.com/mcp/"
EXA_MCP: Final = "https://mcp.exa.ai/mcp"

MAIL_PROVIDERS: Final[Mapping[str, tuple[str, int, str, int, str]]] = {  # imap host, port, smtp host, port, tls
    "icloud": ("imap.mail.me.com", 993, "smtp.mail.me.com", 587, "starttls"),
    "fastmail": ("imap.fastmail.com", 993, "smtp.fastmail.com", 465, "ssl"),
    "gmail": ("imap.gmail.com", 993, "smtp.gmail.com", 465, "ssl"),
    "yahoo": ("imap.mail.yahoo.com", 993, "smtp.mail.yahoo.com", 465, "ssl"),
    "outlook": ("outlook.office365.com", 993, "smtp.office365.com", 587, "starttls"),
    "proton": ("127.0.0.1", 1143, "127.0.0.1", 1025, "starttls"),  # through Proton Bridge on this machine (STARTTLS
}                                                                  # for IMAP too)
CATEGORIES: Final = ("Smart home", "Browser", "Mail & calendar", "Messaging", "Notes & tasks", "Files", "Automation",
                     "Design", "Search", "Developer")


@dataclass(frozen=True, slots=True)
class Field_:
    """One thing the HUD asks for.

    Attributes:
        key: its name.
        label: for people.
        kind: "text", "secret", "select" or "switch".
        help: one line under it.
        placeholder: an example.
        options: a select's choices.
        required: it can't be empty.
    """

    key: str
    label: str
    kind: str = "text"
    help: str = ""
    placeholder: str = ""
    options: tuple[str, ...] = ()
    required: bool = True

    def as_dict(self) -> dict[str, JSON]:
        """For the HUD."""
        return {"key": self.key, "label": self.label, "kind": self.kind, "help": self.help,
                "placeholder": self.placeholder, "options": list(self.options), "required": self.required,
                "secret": self.kind == "secret"}


@dataclass(frozen=True, slots=True)
class Integration:
    """Something Orion can be connected to.

    Attributes:
        id: its name; also the MCP server's name it installs as.
        title: for people.
        description: what it gives him.
        kind: "mcp" (a server) or "calendar" (a calendar source).
        fields: what the HUD asks for.
        build: makes the server's settings from the values (and whether Orion runs in a container).
        docs: how to get what the fields ask for, in a line or two (``{redirect}`` stands for the address a sign-in
            returns to).
        icon: a lucide icon name for the HUD.
        category: where Discover files it (``CATEGORIES``).
        oauth: it signs the user in in their browser.
    """

    id: str
    title: str
    description: str
    kind: str
    fields: tuple[Field_, ...]
    build: Callable[[Mapping[str, Any], bool], dict[str, Any]]
    docs: str = ""
    icon: str = "plug"
    category: str = ""
    oauth: bool = False

    @property
    def auth(self) -> Auth:
        """How it signs in: "oauth", "fields" when it takes a secret or something required, else "none"."""
        if self.oauth:
            return "oauth"
        return "fields" if any(f.required or f.kind == "secret" for f in self.fields) else "none"

    def docs_for(self, redirect: str) -> str:
        """Its docs line, with the address a sign-in returns to written in."""
        return self.docs.replace("{redirect}", redirect)


def _home_assistant(v: Mapping[str, Any], _container: bool) -> dict[str, Any]:
    url = str(v["url"]).rstrip("/")
    return {"url": f"{url}/api/mcp", "headers": {"Authorization": f"Bearer {v['token']}"},
            "description": "Home Assistant: the devices you exposed to voice assistants"}


def _browser(v: Mapping[str, Any], container: bool) -> dict[str, Any]:
    off = {"disabled_tools": ["browser_run_code_unsafe", "browser_evaluate"]}  # code in a page: never by default
    if container:
        return {"url": BROWSER_IMAGE_URL, "description": "A browser he drives (headless, in its own container)",
                **off}
    args = ["--no-webmcp", "--image-responses", "allow"]
    env: dict[str, str] = {}
    if token := str(v.get("extension_token") or "").strip():
        args = ["--extension", *args]
        env["PLAYWRIGHT_MCP_EXTENSION_TOKEN"] = token
    settings: dict[str, Any] = {"command": "uvx", "args": [*NODE, PLAYWRIGHT_MCP, *args], **off,
                                "description": "A browser he drives: your own Chrome (with the Playwright extension) "
                                               "or a window of his own"}
    if env:
        settings["env"] = env
    return settings


def _mail(v: Mapping[str, Any], _container: bool) -> dict[str, Any]:
    imap, imap_port, smtp, smtp_port, tls = MAIL_PROVIDERS.get(str(v.get("provider") or ""), (
        str(v.get("imap_host") or ""), 993, str(v.get("smtp_host") or ""), 465, "ssl"))
    if not imap or not smtp:
        raise ValueError("give the mail servers (imap_host, smtp_host) or pick a provider")
    send = v.get("allow_send") is True
    bridge = str(v.get("provider") or "") == "proton"  # Proton Bridge: STARTTLS on both, its own certificate
    return {"command": "uvx", "args": [EMAIL_MCP, "stdio"], "env": {
        "MCP_EMAIL_SERVER_EMAIL_ADDRESS": str(v["email"]), "MCP_EMAIL_SERVER_PASSWORD": str(v["password"]),
        "MCP_EMAIL_SERVER_IMAP_HOST": imap, "MCP_EMAIL_SERVER_IMAP_PORT": str(imap_port),
        "MCP_EMAIL_SERVER_IMAP_SSL": "false" if bridge else "true",
        "MCP_EMAIL_SERVER_IMAP_START_SSL": "true" if bridge else "false",
        "MCP_EMAIL_SERVER_IMAP_VERIFY_SSL": "false" if bridge else "true",
        "MCP_EMAIL_SERVER_SMTP_HOST": smtp, "MCP_EMAIL_SERVER_SMTP_PORT": str(smtp_port),
        "MCP_EMAIL_SERVER_SMTP_SSL": "true" if tls == "ssl" else "false",
        "MCP_EMAIL_SERVER_SMTP_START_SSL": "true" if tls == "starttls" else "false",
        "MCP_EMAIL_SERVER_SMTP_VERIFY_SSL": "false" if bridge else "true",
        "MCP_EMAIL_SERVER_ALLOWED_MUTATIONS": "draft,organize,send" if send else "draft,organize",
        "MCP_EMAIL_SERVER_ALLOWED_RECIPIENTS": "*" if send else ""},  # empty: sending is off
        "description": f"E-mail ({v['email']})"}


def _google(v: Mapping[str, Any], _container: bool) -> dict[str, Any]:
    permissions = ["gmail:full" if v.get("allow_send") is True else "gmail:drafts", "calendar:full"]
    return {"command": "uvx", "args": [WORKSPACE_MCP, "--single-user", "--permissions", *permissions], "env": {
        "GOOGLE_OAUTH_CLIENT_ID": str(v["client_id"]), "GOOGLE_OAUTH_CLIENT_SECRET": str(v["client_secret"]),
        "USER_GOOGLE_EMAIL": str(v["email"]), "WORKSPACE_MCP_CREDENTIALS_DIR": "data/google"},
        "description": f"Google Workspace: Gmail and Calendar ({v['email']})"}


def _microsoft(v: Mapping[str, Any], _container: bool) -> dict[str, Any]:
    personal = str(v.get("account") or "personal") == "personal"
    args = [*NODE, MS365_MCP, "--preset", "mail,calendar"]
    if v.get("allow_send") is not True:
        args += ["--read-only"]
    return {"command": "uvx", "args": args, "env": {"MS365_MCP_TENANT_ID": "consumers" if personal else "common"},
            "description": "Microsoft 365 / Outlook: mail and calendar"}


def _discord(v: Mapping[str, Any], _container: bool) -> dict[str, Any]:
    return {"command": "orion", "args": ["discord-serve"], "intents": True, "env": {
        "DISCORD_TOKEN": str(v["token"]), "ORION_DISCORD_USERS": str(v["users"]),
        "ORION_DISCORD_CHANNELS": str(v.get("channels") or "")},
        "description": "Discord: your direct messages reach him, and he answers there"}


def _calendar(v: Mapping[str, Any], _container: bool) -> dict[str, Any]:
    url = str(v["url"]).strip()
    if str(v.get("kind") or "caldav") == "ics":
        return {"ics_url": url}
    return {"caldav_url": url, "username": str(v.get("username") or ""), "password": str(v.get("password") or "")}


def _remote(url: str, about: str) -> Callable[[Mapping[str, Any], bool], dict[str, Any]]:
    """The builder of a remote server that registers Orion by itself and signs the user in (``oauth: {}``)."""

    def build(_values: Mapping[str, Any], _container: bool) -> dict[str, Any]:
        return {"url": url, "oauth": {}, "description": about}

    return build


def _own_app(url: str, about: str) -> Callable[[Mapping[str, Any], bool], dict[str, Any]]:
    """The builder of a remote server that signs the user in with an OAuth app of their own (its ID and secret)."""

    def build(v: Mapping[str, Any], _container: bool) -> dict[str, Any]:
        return {"url": url, "oauth": {"client_id": str(v["client_id"]), "client_secret": str(v["client_secret"])},
                "description": about}

    return build


def _github(v: Mapping[str, Any], _container: bool) -> dict[str, Any]:
    return {"url": GITHUB_MCP, "headers": {"Authorization": f"Bearer {v['token']}"},
            "description": "GitHub: repositories, issues, pull requests and code search"}


def _exa(v: Mapping[str, Any], _container: bool) -> dict[str, Any]:
    settings: dict[str, Any] = {"url": EXA_MCP, "description": "Exa: web search and research built for models"}
    if key := str(v.get("api_key") or "").strip():
        settings["headers"] = {"x-api-key": key}
    return settings


SEND = Field_("allow_send", "May send", "switch", "Off: he writes drafts you send yourself (safest).", required=False)
APP_ID = Field_("client_id", "OAuth client ID")
APP_SECRET = Field_("client_secret", "OAuth client secret", "secret")

CATALOG: Final[tuple[Integration, ...]] = (
    Integration("home-assistant", "Home Assistant", "Lights, climate, media and everything you exposed to voice "
                "assistants, through Home Assistant's own MCP server.", "mcp", (
                    Field_("url", "Home Assistant's address", placeholder="http://homeassistant.local:8123"),
                    Field_("token", "Long-lived access token", "secret",
                           "Your profile → Security → Long-lived access tokens (a user that isn't an admin is best)."),
                ), _home_assistant, "Turn on Settings → Devices & services → Model Context Protocol Server in Home "
                                   "Assistant first, and expose the entities he may control.",
                icon="house", category="Smart home"),
    Integration("browser", "Browser control", "He browses for you: opens pages, reads them, clicks and fills in forms "
                "(Playwright). Natively: your own Chrome through the Playwright extension, or a window of his own; in "
                "Docker: a headless browser in its own container.", "mcp", (
                    Field_("extension_token", "Playwright extension token", "secret",
                           "Optional: install the Playwright Extension in Chrome and copy its token, so he uses your "
                           "own Chrome (and your sign-ins). Empty: a browser window of his own.", required=False),
                ), _browser, "In Docker, start the browser container too: docker compose --profile browser up -d.",
                icon="globe", category="Browser"),
    Integration("mail", "E-mail (any provider)", "Read, search and organise your mail and write drafts, over IMAP and "
                "SMTP with an app password: iCloud, Fastmail, Gmail, Yahoo, Proton (Bridge) or your own server.",
                "mcp", (
                    Field_("provider", "Provider", "select", options=(*MAIL_PROVIDERS, "other")),
                    Field_("email", "Address", placeholder="you@example.com"),
                    Field_("password", "App password", "secret",
                           "An app password from your provider's security settings, not your normal password."),
                    Field_("imap_host", "IMAP server (other)", required=False, placeholder="imap.example.com"),
                    Field_("smtp_host", "SMTP server (other)", required=False, placeholder="smtp.example.com"),
                    SEND,
                ), _mail, icon="mail", category="Mail & calendar"),
    Integration("google", "Google Workspace", "Gmail and Google Calendar with full access, through your own Google "
                "Cloud OAuth client.", "mcp", (
                    Field_("email", "Google account", placeholder="you@gmail.com"),
                    Field_("client_id", "OAuth client ID"),
                    Field_("client_secret", "OAuth client secret", "secret"),
                    SEND,
                ), _google, "Create a Desktop OAuth client in Google Cloud (APIs: Gmail, Calendar), set it 'In "
                            "production'; the first use opens a sign-in page.",
                icon="mails", category="Mail & calendar"),
    Integration("microsoft", "Microsoft 365 / Outlook", "Outlook mail and calendar through Microsoft Graph.", "mcp", (
                    Field_("account", "Account", "select", options=("personal", "work")),
                    SEND,
                ), _microsoft, "Sign in once in a terminal: uvx --from nodejs-wheel==24.19.0 npx -y "
                               f"{MS365_MCP} --login", icon="briefcase", category="Mail & calendar"),
    Integration("calendar", "Calendar (CalDAV or link)", "Your calendars from any provider: CalDAV with an app "
                "password (iCloud, Fastmail, Nextcloud...), or a secret iCal link (Google, Outlook, Proton: "
                "read-only).", "calendar", (
                    Field_("name", "Name", placeholder="home"),
                    Field_("kind", "Kind", "select", options=("caldav", "ics")),
                    Field_("url", "CalDAV address or iCal link", "secret",
                           placeholder="https://caldav.icloud.com/"),
                    Field_("username", "User (CalDAV)", required=False),
                    Field_("password", "App password (CalDAV)", "secret", required=False),
                ), _calendar, icon="calendar", category="Mail & calendar"),
    Integration("discord", "Discord", "Talk to him from anywhere by direct message; he answers there, sends you job "
                "results and warnings, and asks you before risky actions with buttons.", "mcp", (
                    Field_("token", "Bot token", "secret", "discord.com/developers → New Application → Bot → Token."),
                    Field_("users", "Your Discord user ID", help="Discord: Settings → Advanced → Developer Mode, then "
                                                                 "right-click your name → Copy User ID."),
                    Field_("channels", "Channels where a mention works", required=False,
                           help="Channel IDs, comma-separated; empty: direct messages only."),
                ), _discord, "Invite the bot to a server you're in (OAuth2 → bot), so you can message it.",
                icon="message-circle", category="Messaging"),
    Integration("notion", "Notion", "Search, read and write your Notion pages and databases.", "mcp", (),
                _remote(NOTION_MCP, "Notion: your pages and databases"), "Sign in to Notion when asked: there is "
                "nothing to copy.", icon="notebook-pen", category="Notes & tasks", oauth=True),
    Integration("linear", "Linear", "Look up, create and update Linear issues, projects and cycles.", "mcp", (),
                _remote(LINEAR_MCP, "Linear: issues, projects and cycles"), "Sign in to Linear when asked: there is "
                "nothing to copy.", icon="square-kanban", category="Notes & tasks", oauth=True),
    Integration("asana", "Asana", "Create and manage Asana tasks, search projects and update assignments.", "mcp",
                (APP_ID, APP_SECRET), _own_app(ASANA_MCP, "Asana: tasks and projects"),
                "Asana gives no sign-in to apps it doesn't know, so make an OAuth app of your own: "
                "https://app.asana.com/0/my-apps → New app → OAuth, add this redirect URL, then copy its client ID "
                "and secret here: {redirect}", icon="list-checks", category="Notes & tasks", oauth=True),
    Integration("atlassian", "Jira and Confluence", "Search and update Jira issues and Confluence pages through "
                "Atlassian's own server.", "mcp", (), _remote(ATLASSIAN_MCP, "Jira and Confluence"),
                "Sign in with your Atlassian account when asked.", icon="kanban", category="Notes & tasks",
                oauth=True),
    Integration("slack", "Slack", "Search your Slack workspace, read channels and threads, and send messages.", "mcp",
                (APP_ID, APP_SECRET), _own_app(SLACK_MCP, "Slack: channels, threads and messages"),
                "Slack gives no sign-in to apps it doesn't know, so make a Slack app of your own: "
                "https://api.slack.com/apps → Create new app → OAuth & Permissions, add this redirect URL, then copy "
                "its client ID and secret here: {redirect}", icon="message-square", category="Messaging",
                oauth=True),
    Integration("dropbox", "Dropbox", "Search and read your Dropbox files, and save new ones.", "mcp", (),
                _remote(DROPBOX_MCP, "Dropbox: your files"), "Sign in to Dropbox when asked: there is nothing to copy.",
                icon="cloud", category="Files", oauth=True),
    Integration("box", "Box", "Search your Box files, ask Box AI about them, and organise folders.", "mcp",
                (APP_ID, APP_SECRET), _own_app(BOX_MCP, "Box: your files and Box AI"),
                "Box gives no sign-in to apps it doesn't know, so make an app of your own: "
                "https://app.box.com/developers/console → Create platform app → OAuth 2.0, add this redirect URL, "
                "then copy its client ID and secret here: {redirect}", icon="box", category="Files", oauth=True),
    Integration("zapier", "Zapier", "Run the thousands of actions your Zapier account can reach.", "mcp", (),
                _remote(ZAPIER_MCP, "Zapier: your actions"), "Sign in to Zapier when asked; the actions he may use "
                "are the ones you add to your Zapier MCP server.", icon="zap", category="Automation", oauth=True),
    Integration("canva", "Canva", "Search, create and edit your Canva designs.", "mcp", (),
                _remote(CANVA_MCP, "Canva: your designs"), "Sign in to Canva when asked: there is nothing to copy.",
                icon="palette", category="Design", oauth=True),
    Integration("github", "GitHub", "Browse repositories, read and write issues and pull requests, and search code.",
                "mcp", (
                    Field_("token", "Personal access token", "secret", "github.com → Settings → Developer settings → "
                           "Personal access tokens; give it only the access he needs."),
                ), _github, icon="git-branch", category="Developer"),
    Integration("exa", "Exa search", "Web search, research and page contents built for models. It works without a "
                "key; a key lifts the rate limit.", "mcp", (
                    Field_("api_key", "Exa API key", "secret", "Optional: from dashboard.exa.ai/api-keys.",
                           required=False),
                ), _exa, icon="search", category="Search"),
)
_BY_ID: Final = {item.id: item for item in CATALOG}


def ids() -> frozenset[str]:
    """Every integration's id (also the name of the MCP server it installs as)."""
    return frozenset(_BY_ID)


def entry(integration_id: str) -> Integration:
    """An integration by its id.

    Raises:
        LookupError: there is none.
    """
    found = _BY_ID.get(integration_id)
    if found is None:
        raise LookupError(f"no integration called {integration_id}")
    return found


def settings_for(integration: Integration, values: Mapping[str, Any], *, container: bool) -> dict[str, Any]:
    """What installing an integration writes, from the values the user gave.

    Args:
        integration: the integration.
        values: the fields' values (a secret one as the text that stands for it in the settings).
        container: whether Orion runs in a container.

    Returns:
        The MCP server's settings (or the calendar source).

    Raises:
        ValueError: a required field is empty, or a value doesn't fit.
    """
    clean = clean_values(integration, values)
    return integration.build(clean, container)


def clean_values(integration: Integration, values: Mapping[str, Any]) -> dict[str, Any]:
    """The values of an integration's fields, checked: strings trimmed, switches true or false, nothing required empty.

    Args:
        integration: the integration.
        values: what the user gave (keys that aren't its fields are left out).

    Returns:
        One value per field.

    Raises:
        ValueError: a required field is empty, or a select's value isn't one of its choices.
    """
    clean = {f.key: values.get(f.key) for f in integration.fields}
    for f in integration.fields:
        value = clean[f.key]
        if f.kind == "switch":
            clean[f.key] = value is True
        elif isinstance(value, str):
            clean[f.key] = value.strip()
        if f.required and f.kind != "switch" and not clean[f.key]:
            raise ValueError(f"{f.label} is needed")
        if f.kind == "select" and clean[f.key] and clean[f.key] not in f.options:
            raise ValueError(f"{f.label} must be one of {', '.join(f.options)}")
    return clean
