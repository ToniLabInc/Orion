"""The data and the REST of the stand-in Orion's newer pages (web/scripts/mock_orion.py serves them): connectors with
their directory, skills, plugins and marketplaces, the activity log, the vault, the channels' limits, timings, spend,
benchmarks, backups and documents. Everything is shaped as section 3.7 of the round's spec says, with realistic
content, and held in memory: a switch, a setting, a secret or a limit you change here stays changed until the mock is
restarted, so the HUD can be driven through its flows without a backend.

`handle(method, path, query, body)` answers a request for these pages, or returns None when the path isn't theirs.
"""

import threading
import time
from typing import Any

PORT = 8790  # (the page that starts a sign-in comes back here: set by mock_orion.py)
LOCK = threading.RLock()


def ago(seconds: float) -> float:
    return time.time() - seconds


# ---------------------------------------------------------------------------------------------------- connectors
def tool(
    name: str, title: str, description: str, risk: str = "read", open_world: bool = False, enabled: bool = True
) -> dict:
    return {
        "name": name,
        "title": title,
        "description": description,
        "risk": risk,
        "open_world": open_world,
        "enabled": enabled,
    }


def field(
    key: str,
    label: str,
    kind: str = "text",
    help: str = "",
    placeholder: str = "",
    options: list | None = None,
    required: bool = False,
    set_: bool | None = None,
) -> dict:
    secret = kind == "secret"
    out = {
        "key": key,
        "label": label,
        "kind": kind,
        "help": help,
        "placeholder": placeholder,
        "options": options or [],
        "required": required,
        "secret": secret,
    }
    if secret:
        out["set"] = bool(set_)
    return out


def connector(
    id: str,
    title: str,
    description: str,
    icon: str,
    *,
    kind: str = "builtin",
    trust: str = "orion",
    sandbox: str = "none",
    status: str = "connected",
    error: str = "",
    needs: list | None = None,
    auth: str = "none",
    signed_in: bool = False,
    tools: list | None = None,
    settings: dict | None = None,
    secrets: list | None = None,
    removable: bool = False,
    calls: int = 0,
    plugin: str | None = None,
) -> dict:
    out = {
        "id": id,
        "title": title,
        "description": description,
        "icon": icon,
        "kind": kind,
        "trust": trust,
        "sandbox": sandbox,
        "status": status,
        "error": error,
        "needs": needs or [],
        "auth": auth,
        "signed_in": signed_in,
        "local": True,
        "tools": tools or [],
        "settings": settings or {"kind": "none"},
        "secrets": secrets or [],
        "removable": removable,
        "calls": calls,
    }
    if plugin:
        out["plugin"] = plugin
    return out


def fields_settings(fields: list[dict], values: dict) -> dict:
    return {"kind": "fields", "fields": fields, "values": values}


def make_connectors() -> list[dict]:
    return [
        connector(
            "orion",
            "Orion",
            "His own settings, his voice, his brain, and what he did lately.",
            "Orbit",
            calls=212,
            tools=[
                tool("get_settings", "Read his settings", "Read his settings, secrets left out."),
                tool("set_setting", "Change a setting", "Change one of his settings.", "change"),
                tool("switch_brain", "Switch his brain", "Switch the brain he thinks with.", "change"),
                tool("list_models", "List the models", "The models on this machine."),
                tool("backup_now", "Back up now", "Make a backup of everything he keeps.", "change"),
                tool("backups_list", "List the backups", "The backups, newest first."),
                tool("activity_recent", "What he did lately", "The latest entries of the activity log."),
                tool(
                    "mcp_servers",
                    "Manage connectors",
                    "List, restart, add or remove connectors: adding or removing always asks you.",
                    "change",
                ),
            ],
        ),
        connector(
            "system",
            "This machine",
            "Apps, files, the volume, media, the screen lock and the shell.",
            "Monitor",
            calls=1284,
            tools=[
                tool("open_target", "Open an app, a file or a site", "Open an app, a file or a website.", "change"),
                tool("volume", "The volume", "Get or change the volume.", "change"),
                tool("media", "Control the music", "Play, pause, skip.", "change"),
                tool("lock_screen", "Lock the screen", "Lock this PC's screen.", "change"),
                tool("run_command", "Run a shell command", "Run a command line, in the sandbox.", "destroy", True),
                tool("power", "Power", "Suspend, restart or shut down.", "destroy"),
                tool("system_status", "How the PC is doing", "CPU, memory, disk, GPU and temperatures."),
                tool("top_processes", "The busiest programs", "What uses the most CPU and memory."),
            ],
            settings=fields_settings(
                [
                    field(
                        "command_timeout_s",
                        "Longest a command may run",
                        "number",
                        "Seconds before a shell command is stopped.",
                        "60",
                    ),
                    field(
                        "sandbox_network",
                        "Commands may use the network",
                        "switch",
                        "Off cuts a command off from the network, in the sandbox.",
                    ),
                ],
                {"command_timeout_s": 60, "sandbox_network": True},
            ),
        ),
        connector(
            "web",
            "The web",
            "Search, read pages, the weather and the news.",
            "Globe",
            calls=531,
            tools=[
                tool("web_search", "Search the web", "Search the web.", "read", True),
                tool("read_page", "Read a web page", "Read a page as text.", "read", True),
                tool("get_weather", "Weather and forecast", "The weather now and the days ahead.", "read", True),
                tool("get_news", "The news", "The top headlines, or on a topic.", "read", True),
            ],
            settings=fields_settings(
                [
                    field(
                        "search_engine",
                        "Search engine",
                        "select",
                        "Where his searches go.",
                        options=["duckduckgo", "brave", "searxng"],
                    ),
                    field("units", "Units", "select", "For the weather.", options=["metric", "imperial"]),
                    field("news_country", "News from", "text", "A country's code: ro, gb, us…", "ro"),
                ],
                {"search_engine": "duckduckgo", "units": "metric", "news_country": "ro"},
            ),
        ),
        connector(
            "organizer",
            "Organizer",
            "Timers, reminders and your tasks.",
            "ListChecks",
            calls=402,
            tools=[
                tool("set_timer", "Set a timer", "Start a timer for a length of time.", "change"),
                tool("set_reminder", "Set a reminder", "Remind you of something at a time.", "change"),
                tool("cancel_timer_or_reminder", "Cancel a timer or reminder", "Take one away.", "destroy"),
                tool("list_reminders", "List timers and reminders", "What is counting down."),
                tool("todo", "Your tasks", "Add, list, complete or drop a task.", "change"),
            ],
        ),
        connector(
            "memory",
            "Memory",
            "What he remembers about you, his notes and the journal.",
            "Brain",
            calls=377,
            tools=[
                tool("memory_save", "Remember something", "Keep a fact about you or a note.", "change"),
                tool("memory_search", "Search his memory", "Find what he remembers, by meaning."),
                tool("memory_forget", "Forget something", "Remove a memory for good.", "destroy"),
                tool(
                    "memory_review_now",
                    "Review the conversation",
                    "Look over the latest conversation for what to keep.",
                    "change",
                ),
            ],
        ),
        connector(
            "skills",
            "Skills",
            "Follow, write and read the routines in his skills folder.",
            "Sparkles",
            calls=64,
            tools=[
                tool("list_skills", "List his skills", "Every skill, with when to use it."),
                tool("use_skill", "Follow a skill", "Read a skill and do what it says.", "change"),
                tool("skill_file", "Read a skill's file", "A text file of a skill's references or assets."),
                tool("save_skill", "Write a skill", "Save a SKILL.md he wrote for a routine you taught him.", "change"),
                tool("delete_skill", "Delete a skill", "Remove a skill he learned.", "destroy"),
            ],
        ),
        connector(
            "calendar",
            "Calendar",
            "Your Google, iCloud and Nextcloud calendars (CalDAV, ICS).",
            "CalendarDays",
            calls=188,
            tools=[
                tool("list_events", "Read your calendar", "Events today, tomorrow or in a range.", "read", True),
                tool("add_event", "Add an event", "Put an event on a calendar.", "change", True),
                tool("update_event", "Change an event", "Move or rename an event.", "change", True),
                tool("delete_event", "Delete an event", "Take an event off a calendar.", "destroy", True),
            ],
            settings=fields_settings(
                [
                    field(
                        "sources",
                        "Calendars",
                        "list",
                        "CalDAV or ICS addresses. A secret in one belongs in the vault.",
                        "https://…",
                    ),
                    field(
                        "days_ahead", "Days ahead", "number", "How far the Today card and the Calendar card look.", "7"
                    ),
                ],
                {
                    "sources": [
                        "https://calendar.google.com/calendar/ical/you/basic.ics",
                        "https://cal.example.org/dav/work",
                    ],
                    "days_ahead": 7,
                },
            ),
        ),
        connector(
            "eyes",
            "Eyes",
            "What he sees: a screen or camera you share, this machine's screen, a picture.",
            "ScanEye",
            calls=41,
            tools=[
                tool("look_at_screen", "Look at the screen", "A picture of the screen you share, or this machine's."),
                tool("look_through_camera", "Look through the camera", "A picture from a camera you share."),
            ],
            settings=fields_settings(
                [
                    field(
                        "image_max_side",
                        "Longest side of a picture",
                        "number",
                        "Pixels: a larger picture is sized down before he sees it.",
                        "1600",
                    ),
                    field("share_wait_s", "How long he waits for you to share", "number", "Seconds.", "30"),
                ],
                {"image_max_side": 1600, "share_wait_s": 30},
            ),
        ),
        connector(
            "clipboard",
            "Clipboard",
            "Copy and paste on this machine.",
            "Clipboard",
            status="unavailable",
            needs=["a clipboard tool (wl-clipboard or xclip)"],
            tools=[
                tool("clipboard_read", "Read the clipboard", "What is copied."),
                tool("clipboard_write", "Copy to the clipboard", "Put some text on it.", "change"),
            ],
        ),
        connector(
            "jobs",
            "Jobs",
            "Requests he runs on a schedule, and where the answers go.",
            "CalendarClock",
            calls=96,
            tools=[
                tool("list_jobs", "List the jobs", "His scheduled jobs."),
                tool("add_job", "Add a job", "Schedule a request.", "change"),
                tool("remove_job", "Remove a job", "Delete a scheduled job.", "destroy"),
                tool("run_job", "Run a job now", "Run one now.", "change"),
            ],
            settings=fields_settings(
                [
                    field("deliver", "Where answers go", "list", "speak, hud or discord."),
                    field("run_timeout_s", "Longest a run may take", "number", "Seconds.", "300"),
                ],
                {"deliver": ["hud"], "run_timeout_s": 300},
            ),
        ),
        connector(
            "watchers",
            "Watchers",
            "His eye on this machine: the heat, the disk, the battery.",
            "Radar",
            calls=0,
            tools=[
                tool("list_watchers", "List the watchers", "The rules and their limits."),
                tool("set_watcher", "Change a watcher", "Move a limit, or switch a rule.", "change"),
            ],
        ),
        connector(
            "documents",
            "Documents",
            "Your folders and your Obsidian vault, found by words and by meaning.",
            "Files",
            calls=73,
            tools=[
                tool("documents_search", "Search your documents", "Find passages by words and by meaning."),
                tool("documents_read", "Read a document", "A file inside your folders, as text."),
                tool("documents_list", "List your documents", "The files of a folder."),
                tool("documents_status", "How far the reading is", "Files and passages found, and any that failed."),
            ],
            settings={"kind": "folders", "path": "documents.folders"},
        ),
        connector(
            "home-assistant",
            "Home Assistant",
            "The lights, the heating and the plugs at home.",
            "House",
            kind="catalog",
            trust="reviewed",
            status="error",
            error="Connection refused (192.168.1.40:8123)",
            auth="fields",
            calls=58,
            settings=fields_settings(
                [
                    field(
                        "url",
                        "Address",
                        "text",
                        "Where Home Assistant answers.",
                        "http://homeassistant.local:8123",
                        required=True,
                    ),
                    field(
                        "token",
                        "Long-lived token",
                        "secret",
                        "Profile, Security, Long-lived access tokens.",
                        required=True,
                        set_=True,
                    ),
                ],
                {"url": "http://192.168.1.40:8123"},
            ),
            secrets=[{"name": "connector.home-assistant.token", "set": True}],
            removable=True,
        ),
        connector(
            "hands",
            "ORION hands",
            "The gesture camera on the Pi: your hands become commands.",
            "Hand",
            kind="custom",
            trust="unreviewed",
            sandbox="none",
            calls=312,
            removable=True,
            tools=[
                tool(
                    "hands_pause",
                    "Pause the gestures",
                    "Switch the gestures (and the camera) off for a while.",
                    "change",
                ),
                tool("hands_resume", "Resume the gestures", "Switch them on again.", "change"),
                tool("hands_status", "How the camera is doing", "Frames per second and what it sees."),
            ],
            settings={"kind": "gestures"},
        ),
        connector(
            "github",
            "GitHub",
            "Your repositories, issues and pull requests.",
            "Code",
            kind="catalog",
            trust="reviewed",
            status="needs_setup",
            auth="fields",
            removable=True,
            tools=[
                tool("search_repositories", "Search repositories", "Find repositories.", "read", True),
                tool("get_issue", "Read an issue", "An issue with its comments.", "read", True),
                tool("create_issue", "Open an issue", "Open an issue on a repository.", "change", True),
                tool("merge_pull_request", "Merge a pull request", "Merge one into its branch.", "destroy", True),
            ],
            settings=fields_settings(
                [
                    field(
                        "token",
                        "Personal access token",
                        "secret",
                        "Settings, Developer settings, Personal access tokens.",
                        required=True,
                        set_=False,
                    ),
                    field(
                        "default_owner",
                        "Your account or organisation",
                        "text",
                        "Used when you don't name one.",
                        "octocat",
                    ),
                ],
                {"default_owner": ""},
            ),
            secrets=[{"name": "connector.github.token", "set": False}],
        ),
        connector(
            "notion",
            "Notion",
            "Your pages and databases.",
            "NotebookPen",
            kind="catalog",
            trust="reviewed",
            status="needs_sign_in",
            auth="oauth",
            removable=True,
            tools=[
                tool("notion_search", "Search Notion", "Find pages and databases.", "read", True),
                tool("notion_fetch", "Read a page", "A page's content.", "read", True),
                tool("notion_create_page", "Create a page", "Add a page.", "change", True),
                tool("notion_update_page", "Change a page", "Edit a page.", "change", True),
            ],
        ),
        connector(
            "context7",
            "Context7",
            "Up-to-date documentation for libraries, from the Context7 service.",
            "BookOpen",
            kind="plugin",
            plugin="context7",
            trust="reviewed",
            sandbox="none",
            calls=19,
            removable=False,
            tools=[
                tool("resolve-library-id", "Find a library", "The Context7 id of a library.", "read", True),
                tool("get-library-docs", "Read a library's docs", "Documentation for a library.", "read", True),
            ],
        ),
        connector(
            "daily-digest",
            "Daily digest",
            "Reads the feeds you follow and writes a short digest.",
            "Newspaper",
            kind="plugin",
            plugin="daily-digest",
            trust="unreviewed",
            sandbox="bwrap",
            calls=6,
            tools=[
                tool("fetch_feeds", "Fetch your feeds", "Read the feeds in the plugin's list.", "read", True),
                tool("write_digest", "Write the digest", "Save today's digest as a note.", "change"),
            ],
            settings=fields_settings(
                [
                    field(
                        "FEEDS_URL",
                        "FEEDS_URL",
                        "text",
                        "An OPML file or a list of feeds, one per line.",
                        "https://example.org/feeds.opml",
                        required=True,
                    ),
                    field(
                        "DIGEST_API_KEY", "DIGEST_API_KEY", "secret", "For the summaries.", required=False, set_=True
                    ),
                ],
                {"FEEDS_URL": "https://example.org/feeds.opml"},
            ),
            secrets=[{"name": "connector.daily-digest.DIGEST_API_KEY", "set": True}],
        ),
        connector(
            "my-notes",
            "my-notes",
            "An MCP server you added: uvx mcp-server-notes.",
            "Package",
            kind="custom",
            trust="unreviewed",
            sandbox="none",
            calls=2,
            removable=True,
            tools=[
                tool("notes_search", "Search notes", "Search the notes folder.", "read"),
                tool("notes_append", "Add to a note", "Append a line to a note.", "change"),
            ],
        ),
    ]


def make_directory() -> list[dict]:
    def item(
        id: str,
        title: str,
        description: str,
        icon: str,
        category: str,
        source: str,
        auth: str,
        fields: list | None = None,
        docs: str = "",
    ) -> dict:
        return {
            "id": id,
            "title": title,
            "description": description,
            "icon": icon,
            "category": category,
            "source": source,
            "auth": auth,
            "fields": fields or [],
            "installed": False,
            "docs": docs,
        }

    o, c = "orion", "claude-directory"
    app = [
        field("client_id", "OAuth client ID", required=True),
        field("client_secret", "OAuth client secret", "secret", required=True),
    ]
    send = field("allow_send", "May send", "switch", "Off: he writes drafts you send yourself (safest).")
    own_app = "Make an OAuth app of your own, add this redirect URL, then copy its client ID and secret here: http://localhost:8765/oauth/callback"
    return [
        item(
            "home-assistant",
            "Home Assistant",
            "Lights, climate, media and everything you exposed to voice assistants, through Home Assistant's own MCP server.",
            "house",
            "Smart home",
            o,
            "fields",
            [
                field("url", "Home Assistant's address", "text", "", "http://homeassistant.local:8123", required=True),
                field(
                    "token",
                    "Long-lived access token",
                    "secret",
                    "Your profile, Security, Long-lived access tokens (a user that isn't an admin is best).",
                    required=True,
                ),
            ],
            "Turn on Settings, Devices & services, Model Context Protocol Server in Home Assistant first, and expose the entities he may control.",
        ),
        item(
            "browser",
            "Browser control",
            "He browses for you: opens pages, reads them, clicks and fills in forms (Playwright).",
            "globe",
            "Browser",
            o,
            "fields",
            [
                field(
                    "extension_token",
                    "Playwright extension token",
                    "secret",
                    "Optional: install the Playwright Extension in Chrome and copy its token, so he uses your own Chrome. Empty: a browser window of his own.",
                )
            ],
        ),
        item(
            "mail",
            "E-mail (any provider)",
            "Read, search and organise your mail and write drafts, over IMAP and SMTP with an app password.",
            "mail",
            "Mail & calendar",
            o,
            "fields",
            [
                field(
                    "provider",
                    "Provider",
                    "select",
                    options=["icloud", "fastmail", "gmail", "yahoo", "other"],
                    required=True,
                ),
                field("email", "Address", "text", "", "you@example.com", required=True),
                field(
                    "password",
                    "App password",
                    "secret",
                    "An app password from your provider's security settings, not your normal password.",
                    required=True,
                ),
                field("imap_host", "IMAP server (other)", "text", "", "imap.example.com"),
                field("smtp_host", "SMTP server (other)", "text", "", "smtp.example.com"),
                send,
            ],
        ),
        item(
            "google",
            "Google Workspace",
            "Gmail and Google Calendar with full access, through your own Google Cloud OAuth client.",
            "mails",
            "Mail & calendar",
            o,
            "fields",
            [
                field("email", "Google account", "text", "", "you@gmail.com", required=True),
                field("client_id", "OAuth client ID", required=True),
                field("client_secret", "OAuth client secret", "secret", required=True),
                send,
            ],
            "Create a Desktop OAuth client in Google Cloud (APIs: Gmail, Calendar), set it 'In production'; the first use opens a sign-in page.",
        ),
        item(
            "microsoft",
            "Microsoft 365 / Outlook",
            "Outlook mail and calendar through Microsoft Graph.",
            "briefcase",
            "Mail & calendar",
            o,
            "fields",
            [field("account", "Account", "select", options=["personal", "work"], required=True), send],
        ),
        item(
            "calendar",
            "Calendar (CalDAV or link)",
            "Your calendars from any provider: CalDAV with an app password, or a secret iCal link (read-only).",
            "calendar",
            "Mail & calendar",
            o,
            "fields",
            [
                field("name", "Name", "text", "", "home", required=True),
                field("kind", "Kind", "select", options=["caldav", "ics"], required=True),
                field("url", "CalDAV address or iCal link", "secret", "", "https://caldav.icloud.com/", required=True),
                field("username", "User (CalDAV)"),
                field("password", "App password (CalDAV)", "secret"),
            ],
        ),
        item(
            "discord",
            "Discord",
            "Talk to him from anywhere by direct message; he answers there, sends you job results and warnings, and asks you before risky actions with buttons.",
            "message-circle",
            "Messaging",
            o,
            "fields",
            [
                field(
                    "token",
                    "Bot token",
                    "secret",
                    "discord.com/developers, New Application, Bot, Token.",
                    required=True,
                ),
                field(
                    "users",
                    "Your Discord user ID",
                    "text",
                    "Discord: Settings, Advanced, Developer Mode, then right-click your name, Copy User ID.",
                    required=True,
                ),
                field(
                    "channels",
                    "Channels where a mention works",
                    "text",
                    "Channel IDs, comma-separated; empty: direct messages only.",
                ),
            ],
        ),
        item(
            "notion",
            "Notion",
            "Search, read and write your Notion pages and databases.",
            "notebook-pen",
            "Notes & tasks",
            o,
            "oauth",
            [],
            "Sign in to Notion when asked: there is nothing to copy.",
        ),
        item(
            "linear",
            "Linear",
            "Look up, create and update Linear issues, projects and cycles.",
            "square-kanban",
            "Notes & tasks",
            o,
            "oauth",
            [],
            "Sign in to Linear when asked: there is nothing to copy.",
        ),
        item(
            "asana",
            "Asana",
            "Create and manage Asana tasks, search projects and update assignments.",
            "list-checks",
            "Notes & tasks",
            o,
            "oauth",
            app,
            own_app,
        ),
        item(
            "atlassian",
            "Jira and Confluence",
            "Search and update Jira issues and Confluence pages through Atlassian's own server.",
            "kanban",
            "Notes & tasks",
            o,
            "oauth",
            [],
            "Sign in with your Atlassian account when asked.",
        ),
        item(
            "slack",
            "Slack",
            "Search your Slack workspace, read channels and threads, and send messages.",
            "message-square",
            "Messaging",
            o,
            "oauth",
            app,
            own_app,
        ),
        item(
            "dropbox",
            "Dropbox",
            "Search and read your Dropbox files, and save new ones.",
            "cloud",
            "Files",
            o,
            "oauth",
            [],
            "Sign in to Dropbox when asked: there is nothing to copy.",
        ),
        item(
            "box",
            "Box",
            "Search your Box files, ask Box AI about them, and organise folders.",
            "box",
            "Files",
            o,
            "oauth",
            app,
            own_app,
        ),
        item(
            "zapier",
            "Zapier",
            "Run the thousands of actions your Zapier account can reach.",
            "zap",
            "Automation",
            o,
            "oauth",
            [],
            "Sign in to Zapier when asked.",
        ),
        item(
            "canva",
            "Canva",
            "Search, create and edit your Canva designs.",
            "palette",
            "Design",
            o,
            "oauth",
            [],
            "Sign in to Canva when asked: there is nothing to copy.",
        ),
        item(
            "github",
            "GitHub",
            "Browse repositories, read and write issues and pull requests, and search code.",
            "git-branch",
            "Developer",
            o,
            "fields",
            [
                field(
                    "token",
                    "Personal access token",
                    "secret",
                    "github.com, Settings, Developer settings, Personal access tokens; give it only the access he needs.",
                    required=True,
                )
            ],
        ),
        item(
            "exa",
            "Exa search",
            "Web search, research and page contents built for models. It works without a key; a key lifts the rate limit.",
            "search",
            "Search",
            o,
            "fields",
            [field("api_key", "Exa API key", "secret", "Optional: from dashboard.exa.ai/api-keys.")],
        ),
        # the marketplaces' plugins that bring MCP servers: Claude's directory
        item(
            "sentry",
            "Sentry",
            "Errors and performance from Sentry.",
            "plug",
            "Monitoring",
            c,
            "oauth",
            [],
            "https://github.com/anthropics/claude-plugins-official/tree/main/plugins/sentry",
        ),
        item(
            "stripe",
            "Stripe",
            "Payments, customers and subscriptions in Stripe.",
            "plug",
            "Payments",
            c,
            "oauth",
            [],
            "https://github.com/anthropics/claude-plugins-official/tree/main/plugins/stripe",
        ),
        item(
            "supabase",
            "Supabase",
            "A Supabase project's database, auth and storage.",
            "plug",
            "Database",
            c,
            "fields",
            [field("SUPABASE_ACCESS_TOKEN", "Supabase access token", "secret", required=True)],
            "https://github.com/anthropics/claude-plugins-official/tree/main/plugins/supabase",
        ),
        item(
            "vercel",
            "Vercel",
            "Deployments and logs on Vercel.",
            "plug",
            "Deployment",
            c,
            "oauth",
            [],
            "https://github.com/anthropics/claude-plugins-official/tree/main/plugins/vercel",
        ),
    ]


# ---------------------------------------------------------------------------------------------------- skills
def skill_md(name: str, description: str, body: str, triggers: str = "", tools: str = "") -> str:
    extra = (f"allowed-tools: {tools}\n" if tools else "") + (
        f"metadata:\n  triggers: {triggers}\n" if triggers else ""
    )
    return f"---\nname: {name}\ndescription: {description}\n{extra}---\n\n{body}\n"


def make_skills() -> list[dict]:
    def s(
        name: str,
        description: str,
        source: str,
        body: str,
        *,
        triggers: list | None = None,
        tools: list | None = None,
        files: list | None = None,
        enabled: bool = True,
        plugin: str | None = None,
    ) -> dict:
        out = {
            "name": name,
            "description": description,
            "source": source,
            "enabled": enabled,
            "triggers": triggers or [],
            "tools": tools or [],
            "files": ["SKILL.md", *(files or [])],
            "editable": source in ("yours", "learned"),
            "content": skill_md(name, description, body, ", ".join(triggers or []), " ".join(tools or [])),
        }
        if plugin:
            out["plugin"] = plugin
        return out

    return [
        s(
            "movie-night",
            "Volume down, the lights low, Netflix open.",
            "yours",
            '1. Call volume with action "set" and level 25.\n2. Call open_target with target "netflix.com".\n3. Say it is ready, in one short sentence.',
            triggers=["movie night", "start a movie"],
            tools=["volume", "open_target"],
        ),
        s(
            "pc-checkup",
            "How the PC is doing: heat, disk, the busiest programs.",
            "yours",
            "1. Call system_status.\n2. Call top_processes.\n3. Tell the user, in two sentences, if anything needs a look.",
            triggers=["check the pc"],
            tools=["system_status", "top_processes"],
            files=["references/limits.md"],
        ),
        s(
            "morning-briefing",
            "The weather, your calendar and the news, every morning.",
            "learned",
            "1. Call get_weather for the user's city.\n2. Call list_events for today.\n3. Call get_news with no topic.\n4. Brief the user in a few sentences.",
            triggers=["brief me", "good morning"],
            tools=["get_weather", "list_events", "get_news"],
        ),
        s(
            "focus-mode",
            "Silence notifications and set a 50 minute timer.",
            "learned",
            "1. Call set_timer for 50 minutes named focus.\n2. Call volume with action mute.",
            triggers=["focus", "heads down"],
        ),
        s(
            "weather-brief",
            "A short weather report with what to wear.",
            "builtin",
            "Call get_weather, then say the temperature, the rain chance and one line on what to wear.",
            tools=["get_weather"],
        ),
        s(
            "summarize-inbox",
            "Summarise unread mail into a few lines.",
            "plugin",
            "Read the unread messages with the mail connector and give the sender and the point of each.",
            tools=["mail_list", "mail_read"],
            plugin="daily-digest",
            files=["references/style.md", "scripts/digest.py"],
        ),
        s(
            "standup-notes",
            "Turn yesterday's commits and calendar into stand-up notes.",
            "plugin",
            "List yesterday's events and commits and write three bullets: done, doing, blocked.",
            plugin="daily-digest",
        ),
        s(
            "commit",
            "Write a commit message from the staged diff.",
            "plugin",
            "Read the staged diff and write one conventional commit message.",
            plugin="commit-commands",
        ),
        s(
            "frontend-design",
            "Design a distinctive, production-grade interface.",
            "plugin",
            "Choose a clear aesthetic direction before writing any code, then build it with care.",
            plugin="frontend-design",
            files=["references/typography.md", "assets/palette.json"],
        ),
    ]


SKILL_FILES = {
    "references/limits.md": "# Limits\n\nWarn above 85 C on the GPU, 90 C on the CPU.\nDisk: warn at 90 % full.\n",
    "references/style.md": "# Style\n\nOne line per message: sender, then the point.\nNever quote the whole mail.\n",
    "scripts/digest.py": '#!/usr/bin/env python3\n"""Group feed items by day and print the titles."""\nimport sys\n\nfor line in sys.stdin:\n    print(line.strip())\n',
    "references/typography.md": "# Typography\n\nPick a distinctive pair: a serif for display, a grotesque for text.\n",
    "assets/palette.json": '{\n  "ink": "#0b0b0b",\n  "paper": "#fcfcfb",\n  "accent": "#2a78d6"\n}\n',
}


def make_skill_discover() -> list[dict]:
    base = "https://github.com/anthropics/skills/tree/main/skills"

    def d(name: str, description: str, installed: bool = False) -> dict:
        return {
            "name": name,
            "description": description,
            "source": "anthropics/skills",
            "url": f"{base}/{name}",
            "installed": installed,
        }

    return [
        d("pdf", "Extract text and tables from PDFs, create, merge, split and fill them."),
        d("docx", "Create, read and edit Word documents with tracked changes."),
        d("pptx", "Create and edit presentations, layouts and speaker notes."),
        d("xlsx", "Read, build and clean spreadsheets, with formulas and charts."),
        d("skill-creator", "Guide for writing a new skill: structure, triggers and testing."),
        d("mcp-builder", "Build a well-formed MCP server, in Python or TypeScript."),
        d("webapp-testing", "Test a local web app with a headless browser and screenshots."),
        d("brand-guidelines", "Apply a brand's colours and type to what he makes.", installed=True),
    ]


# ---------------------------------------------------------------------------------------------------- plugins
def make_plugins() -> list[dict]:
    def p(
        name: str,
        version: str,
        description: str,
        author: Any,
        *,
        skills: list | None = None,
        commands: list | None = None,
        connectors: list | None = None,
        unused: list | None = None,
        marketplace: str | None = "claude-plugins-official",
        enabled: bool = True,
        update: dict | None = None,
        homepage: str = "",
        age_days: float = 12,
    ) -> dict:
        out = {
            "name": name,
            "version": version,
            "description": description,
            "author": author,
            "homepage": homepage,
            "source": (
                {"marketplace": marketplace, "sha": "9f2c1ab7e3d44c6b8e5f0a1d2c3b4a5968778695"}
                if marketplace
                else {"url": "upload"}
            ),
            "enabled": enabled,
            "installed_at": ago(age_days * 86400),
            "trust": "reviewed" if marketplace == "claude-plugins-official" else "unreviewed",
            "components": {
                "skills": skills or [],
                "commands": commands or [],
                "connectors": connectors or [],
                "unused": unused or [],
            },
        }
        if update:
            out["update"] = update
        return out

    return [
        p(
            "context7",
            "1.0.3",
            "Up-to-date documentation and code examples for any library, pulled in as you ask.",
            {"name": "Upstash", "url": "https://upstash.com"},
            connectors=["context7"],
            homepage="https://context7.com",
            age_days=40,
        ),
        p(
            "frontend-design",
            "1.1.0",
            "Design distinctive, production-grade web interfaces instead of generic ones.",
            "Anthropic",
            skills=["frontend-design"],
            homepage="https://github.com/anthropics/claude-plugins-official",
            age_days=22,
        ),
        p(
            "commit-commands",
            "1.0.0",
            "Slash commands for committing: commit, push, open a pull request.",
            "Anthropic",
            commands=["commit", "commit-push-pr", "clean_gone"],
            skills=["commit"],
            unused=["hooks/pre-commit-lint"],
            age_days=9,
        ),
        p(
            "daily-digest",
            "0.4.0",
            "Reads the feeds you follow and writes a short digest each morning.",
            {"name": "Alex", "url": "https://github.com/alex"},
            skills=["summarize-inbox", "standup-notes"],
            commands=["digest"],
            connectors=["daily-digest"],
            unused=["agents/digest-writer", "hooks/session-start", "output-styles/terse"],
            marketplace="my-plugins",
            update={"version": "0.5.0", "sha": "1d2e3f4a5b6c7d8e9f00112233445566778899aa"},
            age_days=3,
        ),
        p(
            "local-experiments",
            "0.1.0",
            "A zip you uploaded: nothing is known of where it came from.",
            "unknown",
            skills=[],
            unused=["agents/helper"],
            marketplace=None,
            enabled=False,
            age_days=1,
        ),
    ]


def make_plugin_discover() -> list[dict]:
    def d(
        name: str,
        description: str,
        category: str,
        author: str = "Anthropic",
        marketplace: str = "claude-plugins-official",
        known: bool = True,
    ) -> dict:
        return {
            "name": name,
            "description": description,
            "category": category,
            "marketplace": marketplace,
            "author": author,
            "homepage": f"https://github.com/anthropics/claude-plugins-official/tree/main/plugins/{name}",
            "installed": False,
            "components_known": known,
        }

    return [
        d(
            "github",
            "GitHub's MCP server: repositories, issues, pull requests and code search.",
            "development",
            "GitHub",
        ),
        d(
            "feature-dev",
            "A guided workflow for building a feature: explore, design, implement, review.",
            "development",
        ),
        d("code-review", "Review a pull request with several focused reviewers and a confidence score.", "development"),
        d(
            "pr-review-toolkit",
            "Specialised agents for reviewing pull requests: tests, comments, types, errors.",
            "development",
        ),
        d("security-guidance", "Warns about security problems in the code as it is written.", "security"),
        d("playwright", "Browser testing and automation with Playwright.", "testing", "Microsoft"),
        d("figma", "Read Figma designs and turn them into code.", "design", "Figma"),
        d("linear", "Linear's issue tracker, from a conversation.", "productivity", "Linear"),
        d("notion", "Search and edit your Notion workspace.", "productivity", "Notion"),
        d("slack", "Read channels, search, and post to Slack.", "productivity", "Slack"),
        d("asana", "Asana's tasks and projects.", "productivity", "Asana"),
        d("sentry", "Errors and performance from Sentry.", "monitoring", "Sentry"),
        d("supabase", "A Supabase project's database, auth and storage.", "database", "Supabase"),
        d("firebase", "Firebase projects: Firestore, auth and hosting.", "database", "Google"),
        d("stripe", "Payments, customers and subscriptions in Stripe.", "payments", "Stripe"),
        d("vercel", "Deployments and logs on Vercel.", "deployment", "Vercel"),
        d("agent-sdk-dev", "Build and verify an app on the Claude Agent SDK.", "development", known=False),
        d("explanatory-output-style", "Explains the code it writes, and why.", "learning", known=False),
        d("learning-output-style", "Asks you to write the small pieces yourself.", "learning", known=False),
        d("orion-recipes", "Recipes and a shopping list skill.", "lifestyle", "alex", "my-plugins"),
    ]


def make_marketplaces() -> list[dict]:
    return [
        {
            "name": "claude-plugins-official",
            "url": "https://github.com/anthropics/claude-plugins-official",
            "plugins": 315,
            "updated_at": ago(6 * 3600),
            "error": "",
        },
        {
            "name": "my-plugins",
            "url": "https://github.com/alex/orion-plugins",
            "plugins": 3,
            "updated_at": ago(2 * 86400),
            "error": "",
        },
        {
            "name": "team-tools",
            "url": "https://github.com/acme/claude-plugins",
            "plugins": 0,
            "updated_at": 0,
            "error": "github.com answered 404: there is no .claude-plugin/marketplace.json in acme/claude-plugins.",
        },
    ]
