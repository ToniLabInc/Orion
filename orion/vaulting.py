"""Secrets in the settings: which values are secret, moving them to the vault, and the references that stay.

config.yaml never holds a secret's value (``orion.secrets``): where it would, it says ``${secret:NAME}``. A value is
secret where the settings keep one:

- ``openrouter.api_key``, named ``openrouter.api_key``;
- a calendar source's ``password``, ``ics_url`` and ``caldav_url`` (a secret link, an address with a login in it), named
  ``calendar.<source>.<field>``;
- the values of a server's ``env`` and ``headers`` whose names say they are secret (``DISCORD_TOKEN``,
  ``Authorization``), named ``connector.<server>.<name>``: the only secrets that server may fill in.

``secure`` moves what is plaintext into the vault (a value that is a reference, a ``${VAR}`` or a ``$VAR`` of a set variable
already is none), forgets the secrets that were referred to and no longer are, and is what every settings change passes
through (``Runtime._commit``): the HUD, a catalog install, a hand edit. At start ``prepare`` does it to the settings as
they are, and ``scrub_backup`` takes the secrets out of ``config.yaml.bak`` too (a copy of the file as it was), so neither
file holds a value. ``used_by`` says which settings use each secret, for the HUD to show (names, never values).
"""

import copy
import json
import logging
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from orion import redact, secrets
from orion.base import write_atomic
from orion.secrets import REFERENCE, Vault, VaultError, has_reference, looks_secret, reference

if TYPE_CHECKING:
    from orion.config import Config, Settings

log = logging.getLogger("orion.vaulting")

KEYS: Final = ("openrouter.api_key", "calendar.sources", "mcp_servers")  # the settings that may hold a secret
CALENDAR_FIELDS: Final = ("password", "ics_url", "caldav_url")
SERVER_PARTS: Final = ("env", "headers")
BACKUP_SUFFIX: Final = ".bak"  # config.yaml.bak (``orion.config.BACKUP_SUFFIX``)
_MIN_CHARS: Final = 8  # the shortest piece of a secret looked for in a backup once it is clean


def _slug(text: str) -> str:
    """A part of a secret's name: lower case, runs of other characters made one hyphen, none at either end."""
    return re.sub(r"[^a-z0-9_-]+", "-", text.lower()).strip("-_")[:40] or "x"


def _plain(value: object) -> bool:
    """Whether a setting is a value to keep: text that refers to nothing."""
    return isinstance(value, str) and bool(value.strip()) and not has_reference(value)


class _Seen:
    """A stand-in for the vault that only notes what would be kept (``plaintext``)."""

    def __init__(self) -> None:
        self.values: list[str] = []

    def set(self, name: str, value: str) -> None:
        """Note a value."""
        self.values.append(value)

    def delete(self, name: str) -> bool:
        """Forget nothing."""
        return False


def secure(values: Mapping[str, Any], old: Mapping[str, Any], vault: "Vault | _Seen | None") -> dict[str, Any]:
    """Settings with their plaintext secrets in the vault and a reference in each one's place.

    Args:
        values: a change to some of ``KEYS`` (key -> new value), or the settings as they are; left as it is.
        old: those settings as they were before the change (``{}`` for none): the secrets they referred to that the new
            ones don't are forgotten.
        vault: where the secrets go (None: there is none, and a plaintext secret can't be kept).

    Returns:
        ``values`` with the references in; keys it doesn't name are not in it.

    Raises:
        VaultError: there is no vault, or it refused a secret.
    """
    out: dict[str, Any] = {}

    def store(name: str, value: str) -> str:
        if vault is None:
            raise VaultError("his vault isn't available, and a secret is never kept in config.yaml")
        vault.set(name, value)
        return reference(name)

    for key, value in values.items():
        if key == "openrouter.api_key":
            out[key] = store(key, value.strip()) if _plain(value) else value
        elif key == "calendar.sources":
            out[key] = _sources(value, store)
        elif key == "mcp_servers":
            out[key] = _servers(value, store)
        else:
            out[key] = value
    if vault is not None:
        for name in _referred(old, values.keys()) - _referred(out, values.keys()):  # no longer referred to: forgotten
            vault.delete(name)
    return out


def _sources(sources: Any, store: Callable[[str, str], str]) -> Any:
    """A calendar's sources with their secret fields in the vault."""
    if not isinstance(sources, dict):
        return sources
    fresh = copy.deepcopy(sources)
    for name, spec in fresh.items():
        if isinstance(spec, dict):
            for field in CALENDAR_FIELDS:
                if _plain(spec.get(field)):
                    spec[field] = store(f"calendar.{_slug(str(name))}.{field}", spec[field].strip())
    return fresh


def _servers(servers: Any, store: Callable[[str, str], str]) -> Any:
    """MCP servers with their secret env and header values in the vault."""
    if not isinstance(servers, dict):
        return servers
    fresh = copy.deepcopy(servers)
    for name, settings in fresh.items():
        if not isinstance(settings, dict):
            continue
        for part in SERVER_PARTS:
            items = settings.get(part)
            if isinstance(items, dict):
                for key, value in items.items():
                    if looks_secret(str(key)) and _plain(value):
                        items[key] = store(f"connector.{_slug(str(name))}.{_slug(str(key))}", value)
    return fresh


def _referred(settings: Mapping[str, Any], keys: Any) -> set[str]:
    """The secrets' names that these settings (those of ``keys``) refer to."""
    found: set[str] = set()
    stack: list[Any] = [settings.get(key) for key in keys]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            found.update(REFERENCE.findall(item))
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return found


def current(cfg: "Config") -> dict[str, Any]:
    """The settings that may hold a secret, as they are."""
    from orion.config import value_of

    return {key: value_of(cfg, key) for key in KEYS}


def plaintext(cfg: "Config") -> list[str]:
    """The secrets the settings still hold as plain text (while no vault could take them): the redactor hides them."""
    seen = _Seen()
    secure(current(cfg), {}, seen)
    return seen.values


def start(settings: "Settings") -> tuple[Vault | None, str]:
    """Open his vault and move the settings' secrets into it, before the file is written; read every secret once, so the
    redactor learns them without a wait. A worker thread: a keyring may take a moment.

    Args:
        settings: the settings (changed in place).

    Returns:
        The vault (None when it can't be opened) and what is wrong ("" when nothing is).
    """
    try:
        vault = secrets.open_vault(settings.cfg)
    except VaultError as e:
        log.warning("the vault can't be opened: %s", e)
        return None, str(e)
    try:
        moved = prepare(settings, vault)
        vault.values()
    except VaultError as e:
        log.warning("the vault refused a secret: %s", e)
        return vault, f"Not every secret could be moved into the vault: {e}"
    if moved:
        log.info("%d secret%s moved from the settings into the vault", moved, "s" * (moved != 1))
    return vault, ""


def prepare(settings: "Settings", vault: Vault) -> int:
    """Move the plaintext secrets of the settings as they are into the vault, before the file is written (the HUD never
    sees one, and ``config.yaml`` holds a reference). Run at start.

    Args:
        settings: the settings; their values are changed in place.
        vault: the vault.

    Returns:
        How many values moved.

    Raises:
        VaultError: the vault refused one (those moved before it are in the settings already).
    """
    cfg = settings.cfg
    now = current(cfg)
    secured = secure(now, {}, vault)
    moved = 0
    for key, value in secured.items():
        if value != now[key]:
            section, _, option = key.partition(".")
            setattr(getattr(cfg, section), option, value) if option else setattr(cfg, key, value)
            moved += 1
    return moved


def scrub_backup(config_file: Path, vault: Vault) -> bool:
    """Take the secrets out of ``config.yaml.bak``, the copy of the settings as they were: any it holds that the vault
    lacks are kept there, and the text says ``${secret:NAME}`` where they were. A backup that can't be made clean (it isn't
    valid YAML any more, or a value is written in a way the text can't be matched) is deleted.

    Args:
        config_file: the settings file (the backup sits beside it).
        vault: the vault.

    Returns:
        Whether a backup was there.
    """
    from orion.config import parse_yaml

    path = config_file.with_name(config_file.name + BACKUP_SUFFIX)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    try:
        raw = parse_yaml(text)
        values = {"openrouter.api_key": (raw.get("openrouter") or {}).get("api_key"),
                  "calendar.sources": (raw.get("calendar") or {}).get("sources"), "mcp_servers": raw.get("mcp_servers")}
        found: dict[str, str] = {}

        def keep(name: str, value: str) -> str:
            found[name] = value
            return reference(name)

        for key, value in values.items():
            if key == "calendar.sources":
                _sources(value, keep)
            elif key == "mcp_servers":
                _servers(value, keep)
            elif _plain(value):
                keep(key, value.strip())
        for name, value in found.items():
            if vault.get(name) is None:
                vault.set(name, value)  # an old secret nothing else holds: kept, not lost
        for name, value in sorted(found.items(), key=lambda item: -len(item[1])):  # (the longest first)
            for form in (value, json.dumps(value)[1:-1], value.replace("'", "''")):  # as plain, double- or single-quoted
                text = text.replace(form, reference(name))
        checker = redact.Redactor((), min_chars=_MIN_CHARS)  # nothing of a secret may be left, not even a line of one
        checker.learn({piece for value in found.values() for piece in (value, *value.split())})
        if checker.holds(text):
            raise ValueError("a secret is written in a form the text can't be matched in")
    except (ValueError, VaultError) as e:
        log.warning("%s can't be made free of secrets (%s): it is deleted", path.name, e)
        path.unlink(missing_ok=True)
        return True
    write_atomic(path, text, private=True)
    return True


def listing(cfg: "Config", vault: Vault | None) -> list[dict[str, Any]]:
    """Every secret by name, whether it is set and what uses it (never a value): what the HUD and ``orion secret list``
    show. A name the settings refer to and the vault lacks is listed as not set.

    Args:
        cfg: the settings.
        vault: the vault, None when there is none.

    Returns:
        ``{"name", "set", "used_by"}`` for each, by name.
    """
    used = used_by(cfg)
    names = vault.names() if vault is not None else ()
    return [{"name": name, "set": name in names, "used_by": used.get(name, [])} for name in sorted({*names, *used})]


def used_by(cfg: "Config") -> dict[str, list[str]]:
    """Which settings use each secret (names only): ``openrouter.api_key``, a calendar as ``calendar:<source>``, a server
    as its name.

    Args:
        cfg: the settings.

    Returns:
        Secret name -> the places that refer to it, each once, in order.
    """
    found: dict[str, list[str]] = {}

    def add(place: str, value: Any) -> None:
        for name in _referred({"x": value}, ("x",)):
            if place not in found.setdefault(name, []):
                found[name].append(place)

    add("openrouter.api_key", cfg.openrouter.api_key)
    for source, spec in cfg.calendar.sources.items():
        add(f"calendar:{source}", spec)
    for server, settings in (cfg.mcp_servers or {}).items():
        add(server, settings)
    return found
