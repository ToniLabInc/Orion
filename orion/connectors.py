"""The connectors: everything that gives Orion tools, as Customize lists it, each with its own settings; and the hub's
way of connecting the added ones (secrets and a plugin's fields put in only as a server connects, OAuth for a remote
one).

A connector is one of:
- builtin: a group of Orion's own tools (system, web, eyes...). Its settings are the config options of the sections it
  owns (``LOOKS``), edited on its own page, not among Orion's: the settings are the connector's;
- catalog: a server set up from the directory (``orion.catalog``) from a few fields; the fields' values are kept in
  its settings (``values``), its secrets in the vault (``connector.<id>.<field>``), and the server's settings hold
  ``${secret:…}`` where they belong, which the hub fills in as it connects;
- plugin: a server a plugin brings (``orion.plugins``): its ``${VAR}``s are its fields, the same way;
- custom: any other server the user added.
Gesture remotes (servers that push intents and say what gestures they see) have the gesture editor as their settings.

Trust says how much was looked at: "orion" (his own), "reviewed" (the directory's, at pinned versions; a plugin from
Anthropic's directory) and "unreviewed" (the rest). Every unreviewed server that is a program on this PC runs in a
sandbox unless the user switched it off for it (``sandboxed``).

This is also the hub's ``Hooks``: ``prepare`` fills a server's secrets and a plugin's fields in as it connects (on a
worker thread, since the vault may ask the system's keyring), and ``auth`` gives a remote server its OAuth provider
unless the user set headers of their own for it.
"""

import asyncio
import contextlib
import logging
import re
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol, cast
from urllib.parse import urlsplit

from orion import catalog, plugins
from orion.base import JSON, server_on
from orion.config import options_of, value_of
from orion.host import profile
from orion.oauth import oauth_name, redirect_uri, signed_in
from orion.plugins import WHO
from orion.secrets import VaultError, reference, references
from orion.tools.core import SERVER_NAME, is_builtin

if TYPE_CHECKING:
    from orion.activity import Activity
    from orion.config import Config
    from orion.mcp import MCPHub, ServerStatus
    from orion.oauth import Broker
    from orion.plugins import Library
    from orion.secrets import Vault
    from orion.tools.core import Controls

log = logging.getLogger("orion.connectors")

type Kind = Literal["builtin", "catalog", "plugin", "custom"]
type Trust = Literal["orion", "reviewed", "unreviewed"]
type Risk = Literal["read", "change", "destroy"]
type Status = Literal["connected", "needs_setup", "needs_sign_in", "connecting", "error", "off", "unavailable"]

USER_KEYS: Final = ("enabled", "disabled_tools", "sandbox", "gestures", "lend", "timeout")  # what a rebuild keeps
ENV_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
HEADER_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}")
PLAIN_REF: Final = re.compile(r"\$\{\w+\}")  # a value that only names a variable of Orion's environment
LEARN_S: Final = 3.0  # a list of connectors waits this long to learn what a gesture remote can say
ACTIVITY_SHOWN: Final = 20  # entries of a connector's own activity on its page
ACTIVITY_SEARCHED: Final = 200  # recent entries looked through to find them


@dataclass(frozen=True, slots=True)
class Look:
    """How a group of Orion's own tools appears among the connectors.

    Attributes:
        icon: a lucide icon name.
        sections: the config sections whose options its page edits.
        skip: options of those sections that have a page of their own.
        folders: the option its folders editor edits (instead of any other setting).
    """

    icon: str
    sections: tuple[str, ...] = ()
    skip: tuple[str, ...] = ()
    folders: str = ""


LOOKS: Final[Mapping[str, Look]] = {
    "system": Look("monitor", ("system",)),
    "web": Look("globe", ("web", "weather")),
    "organizer": Look("timer", ("reminders",)),
    "memory": Look("brain"),
    "skills": Look("sparkles", ("skills",), skip=("skills.disabled",)),
    "calendar": Look("calendar", ("calendar",)),
    "eyes": Look("eye", ("eyes",)),
    "clipboard": Look("clipboard"),
    "jobs": Look("clock", ("jobs",)),
    "watchers": Look("activity", ("watchers",)),
    "documents": Look("folder-search", ("documents",), folders="documents.folders"),
    "orion": Look("orbit"),
}


# ---------------------------------------------------------------------------------------------------- pure helpers
def is_reserved(name: str) -> bool:
    """Whether a name belongs to Orion's own groups or the directory (no added server may take it)."""
    return name in LOOKS or is_builtin(name) or name in catalog.ids()


def kind_of(name: str, settings: Mapping[str, Any] | None, *, builtin: bool) -> Kind:
    """What kind of connector a server is: a group of his own, a plugin's, one the directory sets up (its name), or
    the user's own."""
    if builtin:
        return "builtin"
    if settings and settings.get("plugin"):
        return "plugin"
    return "catalog" if name in catalog.ids() else "custom"


def trust_of(cfg: "Config", kind: Kind, settings: Mapping[str, Any] | None) -> Trust:
    """How much was looked at: his own, the directory's and Anthropic's plugins, or nothing."""
    if kind == "builtin":
        return "orion"
    if kind == "catalog":
        return "reviewed"
    if kind == "plugin":
        record = cfg.plugins.installed.get(str((settings or {}).get("plugin")))
        return plugins.trust_of(record["source"], cfg.plugins.marketplaces) if record else "unreviewed"
    return "unreviewed"


def sandboxed(cfg: "Config", name: str, settings: Mapping[str, Any]) -> bool:
    """Whether an added server runs inside the sandbox: a program (not a remote server) that nobody at Orion
    reviewed, unless the user switched the sandbox off for it.

    Args:
        cfg: the settings.
        name: the server.
        settings: its ``mcp_servers`` entry.
    """
    kind = kind_of(name, settings, builtin=False)
    return kind in ("plugin", "custom") and bool(settings.get("command")) and settings.get("sandbox") is not False


def risk_of(hints: Mapping[str, bool | None]) -> Risk:
    """How much a tool can do, from MCP's hints: it only reads, it changes (declared not destructive), or it may
    destroy (declared so, or says nothing, which MCP reads as destructive)."""
    if hints.get("read_only") is True:
        return "read"
    return "change" if hints.get("destructive") is False else "destroy"


def connector_data(cfg: "Config", name: str) -> Path:
    """The folder a connector keeps its own files in (a sandboxed one's home; a plugin's ``${CLAUDE_PLUGIN_DATA}``)."""
    return cfg.data_path / "connectors" / name


def humanize(name: str) -> str:
    """A server's name for people: "my-tracker" -> "My Tracker"."""
    return " ".join(word.capitalize() for word in re.split(r"[-_]+", name) if word)


def field_dict(key: str, label: str, kind: str, *, about: str = "", options: Sequence[str] = ()) -> dict[str, JSON]:
    """An option of a group of Orion's own as the HUD's ``Field`` (its contract): never required, never secret."""
    return {"key": key, "label": label, "kind": kind, "help": about, "placeholder": "", "options": list(options),
            "required": False, "secret": False}


@dataclass(frozen=True, slots=True)
class _Facts:
    """What the vault says, read once for a whole list: the names kept, and the connectors that hold tokens."""

    names: frozenset[str] = frozenset()
    signed: frozenset[str] = frozenset()


class ConnectorsHost(Protocol):
    """What the connectors need of the running Orion (``runtime.Runtime`` provides it)."""

    cfg: "Config"
    hub: "MCPHub"
    library: "Library"
    broker: "Broker"
    controls: "Controls"

    @property
    def vault(self) -> "Vault":
        """The vault."""

    @property
    def activity(self) -> "Activity":
        """The activity log."""

    def connectors_changed(self) -> None:
        """Tell the HUDs the connectors changed."""

    async def apply_own(self, patch: Mapping[str, Any]) -> dict[str, JSON]:
        """Save settings the user changed on a page of their own (validated, then applied)."""


class Connectors:
    """The connectors: listing them, their settings, adding, changing and removing them; and the hub's hooks."""

    def __init__(self, host: ConnectorsHost) -> None:
        """Hold the running Orion.

        Args:
            host: the running Orion.
        """
        self.host = host
        self._asked: set[str] = set()  # gesture remotes whose gestures were asked for once

    @property
    def cfg(self) -> "Config":
        """The live settings."""
        return self.host.cfg

    # ------------------------------------------------------------------------------------------------ the hub's hooks
    async def prepare(self, name: str, settings: Mapping[str, Any], expand: Callable[[Any], Any]) -> dict[str, Any]:
        """A server's settings as it connects (``mcp.Hooks``): a plugin's fields and a server's secrets put in. The
        environment is read for the servers the user or the directory added, never for a plugin's, whose fields are
        its own.

        Raises:
            SetupNeeded: a field or a secret isn't set.
            VaultError: the settings name a secret that isn't this server's own, or the vault can't be read.
        """
        return await asyncio.to_thread(self._prepare, name, settings, expand)

    def _prepare(self, name: str, settings: Mapping[str, Any], expand: Callable[[Any], Any]) -> dict[str, Any]:
        """``prepare``, on a worker thread."""
        vault = self.host.vault
        if plugin := settings.get("plugin"):
            return plugins.fill(settings, connector=name, root=self.cfg.plugins_path / str(plugin),
                                data=connector_data(self.cfg, name), secret=vault.get)
        filled: dict[str, Any] = expand(dict(settings))
        wanted = sorted({ref for text in plugins.strings(filled) for ref in references(text)})
        if foreign := [ref for ref in wanted if not ref.startswith(f"connector.{name}.")]:  # (G4: only its own)
            raise VaultError(f"{foreign[0]} isn't one of {name}'s secrets (they are named connector.{name}.…)")
        values: dict[str, str] = {}
        for secret in wanted:
            value = vault.get(secret)
            if value is None:
                raise plugins.SetupNeeded(f"{_words(secret)} of {name} isn't set (Customize, Connectors, {name}).")
            values[secret] = value
        return filled if not values else plugins.map_strings(filled, lambda text: vault.resolve(text))

    def auth(self, name: str, settings: Mapping[str, Any], on_need: Callable[[], None]) -> Any:
        """A remote server's OAuth provider (``mcp.Hooks``), unless the user gave it headers of its own to sign in with
        (an OAuth server the user did set up that way has an ``oauth`` setting)."""
        if not settings.get("url") or ("oauth" not in settings and settings.get("headers")):
            return None
        return self.host.broker.provider(name, dict(settings), on_need)

    # ------------------------------------------------------------------------------------------------ the list
    async def snapshot(self, *, local: bool) -> list[dict[str, JSON]]:
        """Every connector, as the HUD shows it.

        Args:
            local: whether the HUD asking is on Orion's own PC (a sign-in can be done only there).

        Returns:
            The added ones in the settings' order, then Orion's own groups.
        """
        facts = await asyncio.to_thread(self._facts)
        status = {entry["name"]: entry for entry in self.host.hub.status()}
        await self._learn(status)
        added = [name for name, settings in self.cfg.mcp_servers.items()
                 if isinstance(settings, dict) and not is_group(name, status)]
        own = [name for name in status if is_group(name, status)]
        return [self._view(name, status.get(name), facts, local=local) for name in [*added, *own]]

    async def get(self, name: str, *, local: bool = True) -> dict[str, JSON]:
        """One connector.

        Raises:
            LookupError: there is none called that.
        """
        status = {entry["name"]: entry for entry in self.host.hub.status()}
        if name not in status and name not in self.cfg.mcp_servers:
            raise LookupError(f"There's no connector called {name}.")
        facts = await asyncio.to_thread(self._facts)
        await self._learn({name: status[name]} if name in status else {})
        return self._view(name, status.get(name), facts, local=local)

    async def detail(self, name: str, *, local: bool = True) -> dict[str, JSON]:
        """One connector with its latest activity (the connector's page).

        Raises:
            LookupError: there is none called that.
        """
        view = await self.get(name, local=local)
        rows = await asyncio.to_thread(lambda: self.host.activity.recent(limit=ACTIVITY_SEARCHED, text=name))
        own = [dict(row) for row in rows if row["connector"] == name][:ACTIVITY_SHOWN]
        return {**view, "activity": cast("list[JSON]", own)}

    def _facts(self) -> _Facts:
        """What the vault holds (on a worker thread); nothing when it can't be read (its secrets then show as not
        set)."""
        vault = self.host.vault
        try:
            names = frozenset(vault.names())
            signed = frozenset(name for name in self.cfg.mcp_servers if oauth_name(name) in names
                               and signed_in(vault, name))
        except VaultError as e:
            log.warning("the vault can't be read: %s", e)
            return _Facts()
        return _Facts(names, signed)

    async def _learn(self, status: Mapping[str, "ServerStatus"]) -> None:
        """Ask each connected remote that pushes intents (and isn't from the directory) what gestures it sees, once:
        that is what makes it a gesture remote."""
        controls = self.host.controls
        pending = [name for name, entry in status.items() if entry["status"] == "connected" and not entry["builtin"]
                   and name not in self._asked and name not in catalog.ids()
                   and (self.cfg.mcp_servers.get(name) or {}).get("intents") is True]
        if not pending:
            return
        self._asked.update(pending)
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(LEARN_S):
                await asyncio.gather(*(controls.vocabulary(name) for name in pending))

    # ------------------------------------------------------------------------------------------------ one connector
    def _view(self, name: str, entry: "ServerStatus | None", facts: _Facts, *, local: bool) -> dict[str, JSON]:
        """A connector as the HUD's ``Connector`` (its contract)."""
        cfg = self.cfg
        builtin = (entry is not None and entry["builtin"]) or name in LOOKS
        settings: dict[str, Any] = dict(cfg.mcp_servers.get(name) or {})
        kind = kind_of(name, settings, builtin=builtin)
        title, about, icon = self._identity(name, kind, entry, settings)
        redirect = redirect_uri(cfg.server)
        fields, values = self._fields(name, kind, settings, facts, redirect)
        block = self._settings(name, kind, settings, fields, values)
        refs = {ref for text in plugins.strings(settings) for ref in references(text)}
        sign_in = entry is not None and entry["sign_in"]
        oauth = "oauth" in settings or sign_in or oauth_name(name) in facts.names
        secret_fields = [f for f in fields if f["secret"]]
        tools = self._tools(entry)
        listed = entry["tools"] if entry is not None else []
        unavailable = sorted({part for t in listed if t["unavailable"] for part in t["unavailable"].split("; ")})
        # a directory connector set up before its fields were kept (no ``values``) is not asked what it never said
        checked = kind in ("plugin", "custom") or (kind == "catalog" and "values" in settings)
        missing = checked and (any(ref not in facts.names for ref in refs) or _unset(name, fields, values, facts))
        auth = "oauth" if oauth else "fields" if secret_fields or refs or (
            kind == "catalog" and catalog.entry(name).auth == "fields") else "none"
        status = self._status(enabled=server_on(settings), entry=entry,
                              unsigned="oauth" in settings and name not in facts.signed, needs_setup=missing,
                              unavailable=bool(listed) and all(t["unavailable"] for t in listed))
        sandbox = "bwrap" if kind != "builtin" and sandboxed(cfg, name, settings) else "none"
        view: dict[str, JSON] = {
            "id": name, "title": title, "description": about, "icon": icon, "kind": kind,
            "trust": trust_of(cfg, kind, settings), "sandbox": sandbox, "status": status,
            "error": entry["error"] if entry is not None and status == "error" else "", "needs": unavailable,
            "auth": auth, "signed_in": name in facts.signed and not sign_in, "local": local, "tools": tools,
            "settings": block, "secrets": _secrets(name, refs, secret_fields, oauth, facts),
            "removable": kind in ("catalog", "custom"), "calls": entry["calls"] if entry is not None else 0}
        if kind == "plugin":
            view["plugin"] = str(settings["plugin"])
        return view

    def _identity(self, name: str, kind: Kind, entry: "ServerStatus | None",
                  settings: Mapping[str, Any]) -> tuple[str, str, str]:
        """(title, description, icon) of a connector."""
        if kind == "builtin":
            look = LOOKS.get(name)
            return (entry["title"] if entry else humanize(name), entry["description"] if entry else "",
                    look.icon if look else "plug")
        if kind == "catalog":
            item = catalog.entry(name)
            return item.title, item.description, item.icon
        if kind == "plugin":
            plugin = str(settings["plugin"])
            manifest = plugins.cached_manifest(self.cfg.plugins_path / plugin)
            return humanize(name), str(manifest.get("description") or settings.get("description") or ""), "blocks"
        remote = settings.get("intents") is True and self.host.controls.said(name)["gestures"]
        return humanize(name), str(settings.get("description") or ""), "hand" if remote else "plug"

    def _fields(self, name: str, kind: Kind, settings: Mapping[str, Any], facts: _Facts,
                redirect: str) -> tuple[list[dict[str, JSON]], dict[str, JSON]]:
        """A connector's fields as the HUD shows them (a secret's value never: only whether it is set) and their
        values."""
        given = settings.get("values") if isinstance(settings.get("values"), dict) else {}
        if kind == "catalog" and catalog.entry(name).kind == "mcp":
            item = catalog.entry(name)
            raw = [f.as_dict() for f in item.fields]
            if raw and not raw[0]["help"] and item.docs:
                raw[0]["help"] = item.docs_for(redirect)
        elif kind == "plugin":
            raw = plugins.placeholder_fields(plugins.placeholders(settings))
        else:
            return [], {}
        fields: list[dict[str, JSON]] = []
        values: dict[str, JSON] = {}
        for field in raw:
            shown = {**field, "help": str(field["help"]).replace("{redirect}", redirect)}
            key = str(field["key"])
            if field["secret"]:
                shown["set"] = plugins.vault_name(name, key) in facts.names
                values[key] = ""
            else:
                values[key] = given.get(key, False if field["kind"] == "switch" else "")
            fields.append(shown)
        return fields, values

    def _settings(self, name: str, kind: Kind, settings: Mapping[str, Any], fields: list[dict[str, JSON]],
                  values: dict[str, JSON]) -> dict[str, JSON]:
        """The settings block of a connector: its own editor, or its fields."""
        if kind == "builtin":
            look = LOOKS.get(name)
            if look is None:
                return {"kind": "none"}
            if look.folders:
                return {"kind": "folders", "path": look.folders}
            options = options_of(look.sections, skip=look.skip)
            if not options:
                return {"kind": "none"}
            return {"kind": "fields",
                    "fields": [field_dict(o.key, o.label, o.kind, about=o.help, options=o.options) for o in options],
                    "values": {o.key: value_of(self.cfg, o.key) for o in options}}
        if settings.get("intents") is True and kind != "catalog" and (
                "gestures" in settings or self.host.controls.said(name)["gestures"]):
            return {"kind": "gestures"}
        if fields:
            return {"kind": "fields", "fields": fields, "values": values}
        return {"kind": "none"}

    @staticmethod
    def _tools(entry: "ServerStatus | None") -> list[dict[str, JSON]]:
        """A connector's tools as the HUD shows them."""
        if entry is None:
            return []
        return [{"name": t["name"], "title": t["title"] or t["remote"].replace("_", " ").capitalize(),
                 "description": t["description"], "risk": risk_of(t["hints"]),
                 "open_world": t["hints"].get("open_world") is not False, "enabled": t["enabled"]}
                for t in entry["tools"]]

    @staticmethod
    def _status(*, enabled: bool, entry: "ServerStatus | None", unsigned: bool, needs_setup: bool,
                unavailable: bool) -> Status:
        """Where a connector stands, in the HUD's words."""
        if not enabled:
            return "off"
        if needs_setup:
            return "needs_setup"
        if unsigned or (entry is not None and entry["sign_in"]):
            return "needs_sign_in"
        if entry is None:
            return "connecting"
        match entry["status"]:
            case "connected":
                return "unavailable" if unavailable else "connected"
            case "connecting" | "reconnecting":
                return "connecting"
            case "error":
                return "error"
        return "off"

    # ------------------------------------------------------------------------------------------------ the directory
    async def directory(self, query: str = "", category: str = "") -> list[dict[str, JSON]]:
        """What can be added: Orion's own list, then the connectors of Claude's directory (the marketplaces' plugins
        that bring some).

        Args:
            query: words that must all appear in an item's id, title, description or category.
            category: only this category (any case).

        Returns:
            The items, as the HUD's directory entries.
        """
        await asyncio.to_thread(self.host.library.load)
        words = query.lower().split()
        redirect = redirect_uri(self.cfg.server)
        have = set(self.cfg.mcp_servers) | ({"calendar"} if self.cfg.calendar.sources else set())
        items: list[dict[str, JSON]] = []

        def wanted(text: str, kind: str) -> bool:
            return (not category or kind.lower() == category.lower()) and all(word in text.lower() for word in words)

        for item in catalog.CATALOG:
            if wanted(f"{item.id} {item.title} {item.description} {item.category}", item.category):
                items.append({"id": item.id, "title": item.title, "description": item.description, "icon": item.icon,
                              "category": item.category, "source": "orion", "auth": item.auth,
                              "fields": [f.as_dict() for f in item.fields], "installed": item.id in have,
                              "docs": item.docs_for(redirect)})
        known = catalog.ids()
        listed: list[dict[str, JSON]] = []
        for _market_name, _market, entry, parts in self.host.library.connector_listings():
            plugin = str(entry["name"])
            kind = str(entry.get("category") or "").capitalize()
            about = str(entry.get("description") or "")
            if plugin in known or not wanted(f"{plugin} {entry.get('displayName') or ''} {about} {kind}", kind):
                continue
            fields = _merged([plugins.placeholder_fields(plugins.placeholders(server["settings"]))
                              for server in parts.servers], redirect)
            own = any("oauth" in server["settings"] for server in parts.servers)
            listed.append({"id": plugin, "title": str(entry.get("displayName") or humanize(plugin)),
                           "description": about, "icon": "plug", "category": kind, "source": "claude-directory",
                           "auth": "oauth" if own else "fields" if fields else "none", "fields": fields,
                           "installed": plugin in self.cfg.plugins.installed, "docs": str(entry.get("homepage") or "")})
        return [*items, *sorted(listed, key=lambda item: str(item["title"]).lower())]

    # ------------------------------------------------------------------------------------------------ adding
    async def add(self, body: Mapping[str, Any]) -> dict[str, JSON]:
        """Add a connector: from the directory, or one of the user's own.

        Args:
            body: ``{from: "directory", id, values}`` or ``{from: "custom", name, command?, args?, env?, url?,
                headers?}``.

        Returns:
            The new connector (the Calendar one, for a calendar).

        Raises:
            LookupError: nothing in the directory has that id.
            ValueError: something given is wrong or missing, or the name is taken.
        """
        origin = body.get("from")
        if origin == "directory":
            values = body.get("values")
            return await self._from_directory(str(body.get("id") or ""), values if isinstance(values, dict) else {})
        if origin == "custom":
            return await self._custom(body)
        raise ValueError('Say where it comes from: {"from": "directory", "id", "values"} or {"from": "custom", ...}.')

    async def _from_directory(self, ident: str, values: Mapping[str, Any]) -> dict[str, JSON]:
        """Add a connector of the directory."""
        if ident in catalog.ids():
            return await self._from_catalog(catalog.entry(ident), values)
        await asyncio.to_thread(self.host.library.load)
        found = [(market, entry) for market, _, entry, _ in self.host.library.connector_listings()
                 if entry["name"] == ident]
        if not found:
            raise LookupError(f"There's nothing called {ident} in the directory.")
        await self.host.library.install(found[0][0], ident)
        servers = [server for server, settings in self.cfg.mcp_servers.items()
                   if isinstance(settings, dict) and settings.get("plugin") == ident]
        if not servers:
            raise LookupError(f"The {ident} plugin brought no connector.")
        for server in servers:
            keys = {str(f["key"]) for f in plugins.placeholder_fields(
                plugins.placeholders(self.cfg.mcp_servers[server]))}
            if given := {key: value for key, value in values.items() if key in keys}:
                await self.save_settings(server, given)
        return await self.get(servers[0])

    async def _from_catalog(self, item: catalog.Integration, values: Mapping[str, Any]) -> dict[str, JSON]:
        """Set up an integration of the catalog from its fields' values."""
        name = item.id
        if item.kind == "mcp" and name in self.cfg.mcp_servers:
            raise ValueError(f"{item.title} is added already: change its settings instead.")
        clean = catalog.clean_values(item, values)
        if item.kind == "calendar":
            return await self._calendar(item, clean)
        secrets = [f.key for f in item.fields if f.kind == "secret"]
        texts, created = await asyncio.to_thread(self._store, name, secrets, clean)
        try:
            built = catalog.settings_for(item, {**clean, **texts}, container=profile().container)
            server = {**built, "values": _plain(item, clean)}
            await self.host.apply_own({"mcp_servers": {**self.cfg.mcp_servers, name: server}})
        except Exception:
            await asyncio.to_thread(self._unstore, created)
            raise
        self._log(f"Added the {item.title} connector", name)
        return await self.get(name)

    async def _calendar(self, item: catalog.Integration, clean: Mapping[str, Any]) -> dict[str, JSON]:
        """Add a calendar source (not a server: the Calendar connector's own)."""
        source = str(clean.get("name") or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,40}", source):
            raise ValueError("a calendar's name uses lower-case letters, digits, - and _")
        settings = catalog.settings_for(item, clean, container=profile().container)
        await self.host.apply_own({"calendar.sources": {**self.cfg.calendar.sources, source: settings}})
        self._log(f"Added the {source} calendar", "calendar")
        return await self.get("calendar")

    async def _custom(self, body: Mapping[str, Any]) -> dict[str, JSON]:
        """Add a server of the user's own: a program on this PC, or a remote one."""
        name = str(body.get("name") or "").strip()
        if not SERVER_NAME.fullmatch(name):
            raise ValueError("A connector's name uses lower-case letters, digits, - and _, up to 41 characters.")
        if is_reserved(name) or name in self.cfg.mcp_servers:
            raise ValueError(f"There's a connector called {name} already.")
        command, url = str(body.get("command") or "").strip(), str(body.get("url") or "").strip()
        if bool(command) == bool(url):
            raise ValueError("Give either a command to run on this PC or the URL of a remote server, not both.")
        args = body.get("args") or []
        env, headers = body.get("env") or {}, body.get("headers") or {}
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise ValueError("The arguments are a list of texts.")
        for label, mapping, valid in (("environment variable", env, ENV_NAME), ("header", headers, HEADER_NAME)):
            if not isinstance(mapping, dict) or not all(
                    isinstance(k, str) and valid.fullmatch(k) and isinstance(v, str) for k, v in mapping.items()):
                raise ValueError(f"A {label} is a name and a text.")
        if url and urlsplit(url).scheme not in ("http", "https"):
            raise ValueError("A remote server's address starts with http:// or https://.")
        if command and headers or url and env:
            raise ValueError("Headers belong to a remote server, and environment variables to a program.")
        server: dict[str, Any] = {}
        if command:
            server |= {"command": command, **({"args": list(args)} if args else {})}
        else:
            server |= {"url": url}
            if urlsplit(url).path.rstrip("/").endswith("/sse"):
                server["transport"] = "sse"
        if isinstance(description := body.get("description"), str) and description.strip():
            server["description"] = description.strip()
        pairs = {**{f"env.{k.lower()}": v for k, v in env.items()},
                 **{f"header.{k.lower()}": v for k, v in headers.items()}}
        texts, created = await asyncio.to_thread(self._store_pairs, name, pairs)
        try:
            if env:
                server["env"] = {k: texts[f"env.{k.lower()}"] for k in env}
            if headers:
                server["headers"] = {k: texts[f"header.{k.lower()}"] for k in headers}
            await self.host.apply_own({"mcp_servers": {**self.cfg.mcp_servers, name: server}})
        except Exception:
            await asyncio.to_thread(self._unstore, created)
            raise
        self._log(f"Added the {humanize(name)} connector", name, detail={"runs": "a program on this PC" if command
                                                                else "a remote server"})
        return await self.get(name)

    # ------------------------------------------------------------------------------------------------ changing
    async def save_settings(self, name: str, values: Mapping[str, Any]) -> dict[str, JSON]:
        """Save a connector's settings: a built-in group's options, or the fields of a directory or plugin connector
        (a secret left empty stays as it is).

        Raises:
            LookupError: there is no such connector.
            ValueError: a value is wrong, a field isn't one of its own, or it has no settings.
        """
        if name in LOOKS:
            await self._save_group(name, values)
        else:
            kind, settings = self._added(name)
            if kind == "catalog":
                await self._save_catalog(name, settings, values)
            elif kind == "plugin":
                await self._save_plugin(name, settings, values)
            else:
                raise ValueError(f"{humanize(name)} has no settings here: remove it and add it again to change them.")
        return await self.get(name)

    async def _save_group(self, name: str, values: Mapping[str, Any]) -> None:
        """Save the options of one of Orion's own groups (only the ones its page shows)."""
        look = LOOKS[name]
        allowed = {o.key for o in options_of(look.sections, skip=look.skip)}
        if unknown := sorted(set(values) - allowed):
            raise ValueError(f"{humanize(name)} has no setting {unknown[0]}.")
        if changed := (await self.host.apply_own(dict(values))).get("changed"):
            self._log(f"Changed {humanize(name)}'s settings", name, detail={"changed": ", ".join(map(str, changed))})

    async def _save_catalog(self, name: str, settings: Mapping[str, Any], values: Mapping[str, Any]) -> None:
        """Save the fields of a directory connector: rebuild its settings, keeping what the user set on the server."""
        item = catalog.entry(name)
        if item.kind != "mcp":
            raise ValueError(f"{item.title} has no settings here.")
        if unknown := sorted(set(values) - {f.key for f in item.fields}):
            raise ValueError(f"{item.title} has no setting {unknown[0]}.")
        current = settings.get("values") if isinstance(settings.get("values"), dict) else {}
        merged = {f.key: values.get(f.key, current.get(f.key)) for f in item.fields if f.kind != "secret"}
        secrets = [f.key for f in item.fields if f.kind == "secret"]
        texts, created = await asyncio.to_thread(self._store, name, secrets, values)
        try:
            clean = catalog.clean_values(item, {**merged, **texts})
            rebuilt = {**catalog.settings_for(item, clean, container=profile().container),
                       **{k: v for k, v in settings.items() if k in USER_KEYS}, "values": _plain(item, clean)}
            await self.host.apply_own({"mcp_servers": {**self.cfg.mcp_servers, name: rebuilt}})
        except Exception:
            await asyncio.to_thread(self._unstore, created)
            raise
        await self._reconnect_if_unchanged(name, settings, rebuilt, any(values.get(key) for key in secrets))
        self._log(f"Changed {item.title}'s settings", name, detail={"fields": ", ".join(sorted(values))})

    async def _save_plugin(self, name: str, settings: Mapping[str, Any], values: Mapping[str, Any]) -> None:
        """Save the fields of a plugin's connector (its ``${VAR}``s)."""
        fields = {str(f["key"]): f for f in plugins.placeholder_fields(plugins.placeholders(settings))}
        if unknown := sorted(set(values) - set(fields)):
            raise ValueError(f"{humanize(name)} has no setting {unknown[0]}.")
        secrets = [key for key, f in fields.items() if f["secret"] and key in values]
        _, created = await asyncio.to_thread(self._store, name, secrets, values)
        kept = dict(settings.get("values") or {})
        for key, value in values.items():
            if key in fields and not fields[key]["secret"]:
                if str(value).strip():
                    kept[key] = str(value).strip()
                else:
                    kept.pop(key, None)
        updated = {k: v for k, v in settings.items() if k != "values"} | ({"values": kept} if kept else {})
        try:
            await self.host.apply_own({"mcp_servers": {**self.cfg.mcp_servers, name: updated}})
        except Exception:
            await asyncio.to_thread(self._unstore, created)
            raise
        await self._reconnect_if_unchanged(name, settings, updated, any(str(values[key]).strip() for key in secrets))
        self._log(f"Changed {humanize(name)}'s settings", name, detail={"fields": ", ".join(sorted(values))})

    async def _reconnect_if_unchanged(self, name: str, before: Mapping[str, Any], after: Mapping[str, Any],
                                      secret_changed: bool) -> None:
        """A new secret changes nothing in the settings (they hold only its name), so the link is connected again by
        hand; when the settings changed too, the hub does that itself."""
        if secret_changed and dict(before) == dict(after):
            with contextlib.suppress(ValueError):
                await self.host.hub.restart(name)

    async def set_enabled(self, name: str, enabled: bool) -> dict[str, JSON]:
        """Switch a connector on or off (its tools and settings stay).

        Raises:
            LookupError: there is no such connector.
        """
        self._exists(name)
        await self.host.controls.switch_server(name, enabled, actor="hud")
        self._log(f"Switched {humanize(name)} {'on' if enabled else 'off'}", name)
        return await self.get(name)

    async def set_sandbox(self, name: str, on: bool) -> dict[str, JSON]:
        """Switch the sandbox of a connector that runs a program on this PC on or off.

        Raises:
            LookupError: there is no such connector.
            ValueError: it is not a program of the user's or a plugin's.
        """
        kind, settings = self._added(name)
        if kind not in ("plugin", "custom") or not settings.get("command"):
            raise ValueError(f"{humanize(name)} runs no program of its own on this PC: there is nothing to sandbox.")
        updated = {k: v for k, v in settings.items() if k != "sandbox"} | ({} if on else {"sandbox": False})
        await self.host.apply_own({"mcp_servers": {**self.cfg.mcp_servers, name: updated}})
        self._log(f"Switched the sandbox of {humanize(name)} {'on' if on else 'off'}", name)
        return await self.get(name)

    async def set_tool(self, name: str, tool: str, enabled: bool) -> dict[str, JSON]:
        """Switch one tool of a connector on or off.

        Raises:
            LookupError: there is no such connector or tool.
        """
        self._exists(name)
        try:
            await self.host.controls.switch_tool(name, tool, enabled, actor="hud")
        except ValueError as e:
            raise LookupError(str(e)) from None
        self._log(f"Switched {tool} {'on' if enabled else 'off'}", name, tool=tool)
        return await self.get(name)

    async def connect(self, name: str, *, local: bool) -> dict[str, JSON]:
        """Start signing in to a connector.

        Args:
            name: the connector.
            local: whether the HUD asking is on Orion's own PC: the sign-in returns to ``localhost`` there.

        Returns:
            ``{"auth_url": …}`` (the page to open in a new tab), or the connector when nothing was needed.

        Raises:
            LookupError: there is no such connector.
            ValueError: the HUD isn't on this PC, or the connector is not a remote one.
        """
        _, settings = self._added(name)
        if not local:
            raise ValueError("Sign in from the PC Orion runs on: the sign-in returns to localhost there.")
        if not settings.get("url"):
            raise ValueError(f"{humanize(name)} is not a remote server: there is nothing to sign in to.")
        prepared = await self.host.hub.prepared(name, settings)
        url = await self.host.broker.sign_in(name, prepared, title=self._title(name))
        if url is not None:
            return {"auth_url": url}
        return await self.get(name)

    async def disconnect(self, name: str) -> dict[str, JSON]:
        """Sign out of a connector: its tokens and registration are forgotten.

        Raises:
            LookupError: there is no such connector.
        """
        self._added(name)
        await self.host.broker.sign_out(name, title=self._title(name))
        return await self.get(name)

    async def remove(self, name: str) -> None:
        """Remove a connector the user added (its secrets and files with it).

        Raises:
            LookupError: there is no such connector.
            ValueError: it is one of Orion's own, or a plugin's (the plugin is removed instead).
        """
        kind, settings = self._added(name)
        if kind == "plugin":
            raise ValueError(f"{humanize(name)} comes with the {settings['plugin']} plugin: remove the plugin, or "
                             "switch the connector off.")
        self.host.broker.cancel(name)
        await self.host.apply_own({"mcp_servers": {k: v for k, v in self.cfg.mcp_servers.items() if k != name}})
        await asyncio.to_thread(self._forget_files, name)
        self._log(f"Removed the {humanize(name)} connector", name)

    # ------------------------------------------------------------------------------------------------ inside
    def _exists(self, name: str) -> None:
        """Raise LookupError when no connector has this name."""
        if name not in LOOKS and name not in self.cfg.mcp_servers and not any(
                entry["name"] == name for entry in self.host.hub.status()):
            raise LookupError(f"There's no connector called {name}.")

    def _added(self, name: str) -> tuple[Kind, dict[str, Any]]:
        """A connector someone added (the directory, a plugin or the user): its kind and settings.

        Raises:
            LookupError: there is none called that.
            ValueError: it is one of Orion's own groups.
        """
        settings = self.cfg.mcp_servers.get(name)
        if name in LOOKS or is_builtin(name):
            raise ValueError(f"{humanize(name)} is one of Orion's own: it can be switched off, not changed this way.")
        if settings is None:
            raise LookupError(f"There's no connector called {name}.")
        return kind_of(name, settings, builtin=False), dict(settings)

    def _title(self, name: str) -> str:
        """A connector's name for people."""
        return catalog.entry(name).title if name in catalog.ids() else humanize(name)

    def _store(self, name: str, keys: Sequence[str], given: Mapping[str, Any]) -> tuple[dict[str, str], list[str]]:
        """Keep the secret fields the user gave in the vault (on a worker thread): the text that stands for each in
        the settings (its reference, or "" when it is not set), and the names newly made (to undo them)."""
        vault = self.host.vault
        texts: dict[str, str] = {}
        created: list[str] = []
        for key in keys:
            secret = plugins.vault_name(name, key)
            value = given.get(key)
            if isinstance(value, str) and value.strip():
                if not vault.has(secret):
                    created.append(secret)
                vault.set(secret, value.strip())
            texts[key] = reference(secret) if vault.has(secret) else ""
        return texts, created

    def _store_pairs(self, name: str, pairs: Mapping[str, str]) -> tuple[dict[str, str], list[str]]:
        """Keep every value of a custom connector's environment and headers (``env.<name>``, ``header.<name>``) in the
        vault, except a value that only names a variable of Orion's environment (on a worker thread)."""
        vault = self.host.vault
        texts: dict[str, str] = {}
        created: list[str] = []
        for label, value in pairs.items():
            if PLAIN_REF.fullmatch(value) or not value.strip():
                texts[label] = value
                continue
            secret = f"connector.{name}.{label}"
            if not vault.has(secret):
                created.append(secret)
            vault.set(secret, value)
            texts[label] = reference(secret)
        return texts, created

    def _unstore(self, created: Sequence[str]) -> None:
        """Take back secrets made for a change that failed."""
        for secret in created:
            self.host.vault.delete(secret)

    def _forget_files(self, name: str) -> None:
        """Delete a removed connector's secrets and its own folder (on a worker thread)."""
        vault = self.host.vault
        for secret in vault.names():
            if secret.startswith(f"connector.{name}."):
                vault.delete(secret)
        shutil.rmtree(connector_data(self.cfg, name), ignore_errors=True)

    def _log(self, title: str, name: str, *, tool: str = "", detail: Mapping[str, Any] | str = "") -> None:
        """Write a change to the activity log (never a value)."""
        self.host.activity.record("connector", source="hud", title=title, outcome="ok", actor=WHO, connector=name,
                                  tool=tool, detail=detail)
        self.host.connectors_changed()


# ---------------------------------------------------------------------------------------------------- views
def is_group(name: str, status: Mapping[str, "ServerStatus"]) -> bool:
    """Whether a name is one of Orion's own groups of tools (its entry in ``mcp_servers``, if any, only switches the
    group or some of its tools off)."""
    return name in LOOKS or is_builtin(name) or (name in status and status[name]["builtin"])


def _words(secret: str) -> str:
    """A vault name in words: "connector.github.api_key" -> "The api key"."""
    return "The " + secret.rsplit(".", 1)[-1].replace("_", " ").replace("-", " ")


def _plain(item: catalog.Integration, clean: Mapping[str, Any]) -> dict[str, Any]:
    """The values of a directory connector's fields that are kept in its settings: not the secrets, nothing empty, a
    switch only when it is on."""
    return {f.key: clean[f.key] for f in item.fields if f.kind != "secret" and clean[f.key] not in ("", None, False)}


def _unset(name: str, fields: Sequence[Mapping[str, JSON]], values: Mapping[str, JSON], facts: _Facts) -> bool:
    """Whether a field that is required has no value (a secret: none in the vault)."""
    for field in fields:
        if not field["required"]:
            continue
        key = str(field["key"])
        if field["secret"]:
            if plugins.vault_name(name, key) not in facts.names:
                return True
        elif field["kind"] != "switch" and values.get(key) in ("", None):
            return True
    return False


def _secrets(name: str, refs: set[str], secret_fields: Sequence[Mapping[str, JSON]], oauth: bool,
             facts: _Facts) -> list[dict[str, JSON]]:
    """The vault entries a connector uses, and whether each is set."""
    names = set(refs) | {plugins.vault_name(name, str(f["key"])) for f in secret_fields}
    if oauth:
        names.add(oauth_name(name))
    # a sign-in is "set" once it holds tokens (a registration alone, made when a connector first looked, is not)
    return [{"name": secret, "set": name in facts.signed if secret == oauth_name(name) else secret in facts.names}
            for secret in sorted(names)]


def _merged(groups: Sequence[Sequence[dict[str, JSON]]], redirect: str) -> list[dict[str, JSON]]:
    """The fields of several servers as one list, each once, with the sign-in address written in."""
    seen: dict[str, dict[str, JSON]] = {}
    for fields in groups:
        for field in fields:
            seen.setdefault(str(field["key"]), {**field, "help": str(field["help"]).replace("{redirect}", redirect)})
    return list(seen.values())
