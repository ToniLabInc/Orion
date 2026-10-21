"""The vault: Orion's keys, tokens and passwords, kept apart from his settings.

config.yaml never holds a secret's value: a secret option says ``${secret:NAME}`` and the value lives here, in one
of two stores, whichever the user chose (``secrets.store``; nothing falls back from one to the other):

- ``keyring``: the system's keyring (on Linux the desktop's Secret Service: GNOME Keyring, KWallet), encrypted at
  rest and unlocked with the user's session. The vault's folder then holds only its id and the secrets' names
  (a keyring can't list what it holds).
- ``files``: a folder only Orion's user can open (0700), one file per secret (0600). For a server or a container,
  where no keyring runs; protected like an SSH key is.

Each Orion has his own entries (the keyring service is ``orion/<the vault's id>``), so a second Orion on the same
machine, such as a throwaway one for tests, never sees or changes the first one's secrets.

Nothing the brain can reach reads the vault: no tool does, the settings tools show a secret option as set or not,
and every text a model or a channel receives passes through the redactor (``orion.redact``), which is given every
value here (``Known``) so that a secret that leaks into a tool's result is cut out before anyone reads it.

Where a secret is used, ``expand`` fills it in: a connector's settings (only its own secrets, ``connector.<id>.*``),
the OpenRouter key, a calendar's password. ``clean_environ`` and ``strip_environ`` keep what Orion's own environment
holds (``CLAUDE_CODE_OAUTH_TOKEN`` in a container) from the commands and servers he starts.

Both of Orion's processes may open the same vault; a change made by one is seen by the other at its next read (every
set or delete rewrites the names file, whose stamp says so).
"""

import contextlib
import json
import logging
import os
import re
import secrets as _random
import threading
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

from orion import redact
from orion.base import OrionError, file_stamp, write_atomic

if TYPE_CHECKING:
    from orion.config import Config, SafetyConfig
    from orion.redact import Redactor

log = logging.getLogger("orion.secrets")

type Store = Literal["keyring", "files"]

NAME: Final = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")  # "openrouter.api_key", "connector.notion.oauth"
REFERENCE: Final = re.compile(r"\$\{secret:([a-z0-9][a-z0-9._-]{0,127})\}")  # ${secret:NAME} in a setting
ID_FILE: Final = "id"  # the vault's own id (the keyring service's name ends with it)
NAMES_FILE: Final = "names.json"  # the secrets' names (never their values)
SERVICE_PREFIX: Final = "orion/"
# A name that says its value is secret: an environment variable, a header, an option, an argument. Whole words only,
# between the separators of such names ("_", "-", "."): GNOME_KEYRING_CONTROL and XAUTHORITY are not secret.
SECRET_NAME: Final = re.compile(
    r"(?:^|[_.-])(?:api[_-]?keys?|keys?|tokens?|secrets?|passwords?|passwd|pass|passphrase|auth|authorization|cookies?|"
    r"credentials?|bearer)(?:$|[_.-])", re.IGNORECASE)
_EXPANSION: Final = re.compile(r"\$\{secret:([a-z0-9][a-z0-9._-]{0,127})\}|\$\{(\w+)\}|\$(\w+)")
_SCHEME: Final = re.compile(r"(?:Bearer|Basic|Token|Digest|OAuth)\s+(\S+)", re.IGNORECASE)  # "Bearer abc": abc is it


class VaultError(OrionError):
    """The vault can't do what was asked (no keyring answers, a name that isn't one, a secret that isn't set)."""


def reference(name: str) -> str:
    """The text a setting holds in place of a secret: ``${secret:NAME}``."""
    return f"${{secret:{name}}}"


def references(text: str) -> list[str]:
    """The names of the secrets a setting's text refers to, in order."""
    return REFERENCE.findall(text)


def looks_secret(name: str) -> bool:
    """Whether a name (of a variable, a header, an argument) says its value is secret ("API_KEY", "Authorization")."""
    return SECRET_NAME.search(name) is not None


class Vault:
    """The secrets, by name.

    Args:
        store: where the values live ("keyring" or "files").
        folder: the vault's folder (its id, the names; with "files", the values too). Created 0700 when missing.
    """

    def __init__(self, store: Store, folder: Path) -> None:
        if store not in ("keyring", "files"):
            raise VaultError(f"secrets.store must be keyring or files, not {store!r}")
        self.store: Final[Store] = store
        self.folder: Final = folder
        self._lock = threading.RLock()
        self._cache: dict[str, str] = {}  # name -> value, for what has been read since the names last changed
        self._names: tuple[str, ...] = ()
        self._stamp: tuple[int, int, int] | None = None  # the names file's, when they were last read
        self._service = ""
        self._backend = None
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(folder, 0o700)
        if store == "keyring":
            self._backend = _keyring()
            self._service = SERVICE_PREFIX + self._own_id()

    # ------------------------------------------------------------------------------------------------ reading
    def names(self) -> tuple[str, ...]:
        """Every secret's name, sorted."""
        with self._lock:
            self._sync()
            return self._names

    def has(self, name: str) -> bool:
        """Whether a secret is set."""
        return _checked(name) in self.names()

    def revision(self) -> tuple[int, int, int] | None:
        """Something that differs whenever any process sets or deletes a secret: one ``stat`` of the names file, which
        every change rewrites (None while nothing was ever set)."""
        try:
            return file_stamp((self.folder / NAMES_FILE).stat())
        except FileNotFoundError:
            return None

    def get(self, name: str) -> str | None:
        """A secret's value, or None when it isn't set.

        Raises:
            VaultError: the name isn't one, or the keyring wouldn't answer.
        """
        name = _checked(name)
        with self._lock:
            self._sync()
            if name not in self._names:
                return None
            if name not in self._cache:
                value = self._read(name)
                if value is None:  # listed but gone from the store (removed by hand): not set
                    return None
                self._cache[name] = value
            return self._cache[name]

    def values(self) -> list[str]:
        """Every secret's value (for the redactor), longest first."""
        found = [value for name in self.names() if (value := self.get(name))]
        return sorted(found, key=len, reverse=True)

    def resolve(self, text: str) -> str:
        """A setting's text with each ``${secret:NAME}`` replaced by the secret.

        Raises:
            VaultError: a secret it names isn't set.
        """
        def value(match: re.Match[str]) -> str:
            found = self.get(match.group(1))
            if found is None:
                raise VaultError(f"the secret {match.group(1)} isn't set (Settings, or `orion secret set`)")
            return found

        return REFERENCE.sub(value, text)

    def __iter__(self) -> Iterator[str]:
        return iter(self.names())

    # ------------------------------------------------------------------------------------------------ changing
    def set(self, name: str, value: str) -> None:
        """Keep a secret (replacing any value it had).

        Raises:
            VaultError: the name isn't one, the value is empty, or the store refused it.
        """
        name = _checked(name)
        if not value:
            raise VaultError(f"{name}: a secret can't be empty (delete it instead)")
        with self._lock:
            self._sync()
            self._write(name, value)
            self._cache[name] = value
            self._save_names(tuple(sorted({*self._names, name})))  # rewritten even for a known name: others see it
        log.info("secret %s set", name)

    def delete(self, name: str) -> bool:
        """Forget a secret; False when it wasn't set."""
        name = _checked(name)
        with self._lock:
            self._sync()
            if name not in self._names:
                return False
            self._erase(name)
            self._cache.pop(name, None)
            self._save_names(tuple(n for n in self._names if n != name))
        log.info("secret %s deleted", name)
        return True

    # ------------------------------------------------------------------------------------------------ inside
    def _own_id(self) -> str:
        """The vault's id, made the first time (it names this Orion's keyring entries)."""
        path = self.folder / ID_FILE
        try:
            return path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            ident = _random.token_hex(8)
            write_atomic(path, ident + "\n", private=True)
            return ident

    def _sync(self) -> None:
        """Read the names again when the file changed (another process set or deleted one); drop the cache then."""
        path = self.folder / NAMES_FILE
        try:
            stamp = file_stamp(path.stat())
        except FileNotFoundError:
            stamp = None
        if stamp == self._stamp and (self._names or stamp is None):
            return
        names: tuple[str, ...] = ()
        if stamp is not None:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                raise VaultError(f"{path} can't be read: {e}") from None
            names = tuple(sorted(n for n in raw.get("names", ()) if isinstance(n, str) and NAME.match(n)))
        self._names, self._stamp = names, stamp
        self._cache.clear()

    def _save_names(self, names: tuple[str, ...]) -> None:
        """Write the names file; its stamp is bound to differ from the one before (the filesystem's clock ticks every
        few milliseconds, and a replaced file may get its old inode back), so every other process sees the change."""
        path = self.folder / NAMES_FILE
        try:
            before = path.stat().st_mtime_ns
        except FileNotFoundError:
            before = 0
        write_atomic(path, json.dumps({"names": sorted(names)}, indent=1) + "\n", private=True)
        if (now := path.stat().st_mtime_ns) <= before:
            os.utime(path, ns=(now, before + 1))
        self._names, self._stamp = tuple(sorted(names)), file_stamp(path.stat())

    def _read(self, name: str) -> str | None:
        if self.store == "files":
            try:
                return (self.folder / name).read_text(encoding="utf-8")
            except FileNotFoundError:
                return None
        try:
            return self._backend.get_password(self._service, name)  # type: ignore[union-attr]  # set for "keyring"
        except Exception as e:  # noqa: BLE001 (any keyring failure is the same to the user: it didn't answer)
            raise VaultError(f"the keyring didn't answer: {e}") from None

    def _write(self, name: str, value: str) -> None:
        if self.store == "files":
            write_atomic(self.folder / name, value, private=True)
            return
        try:
            self._backend.set_password(self._service, name, value)  # type: ignore[union-attr]  # set for "keyring"
        except Exception as e:  # noqa: BLE001
            raise VaultError(f"the keyring refused {name}: {e}") from None

    def _erase(self, name: str) -> None:
        if self.store == "files":
            (self.folder / name).unlink(missing_ok=True)
            return
        try:
            self._backend.delete_password(self._service, name)  # type: ignore[union-attr]  # set for "keyring"
        except Exception as e:  # noqa: BLE001 (gone already, or refused: either way it can't be read any more)
            log.debug("keyring delete of %s: %s", name, e)


def _checked(name: str) -> str:
    if not isinstance(name, str) or not NAME.match(name):
        raise VaultError(f"{name!r} isn't a secret's name: lower-case letters, digits, '.', '_' and '-'")
    return name


def _keyring():
    """The system keyring, or VaultError when this machine has none (a server, a container)."""
    try:
        import keyring
        from keyring.backends import fail
    except ImportError as e:  # pragma: no cover (a dependency)
        raise VaultError(f"the keyring library is missing: {e}") from None
    backend = keyring.get_keyring()
    if isinstance(backend, fail.Keyring) or type(backend).__module__.startswith("keyring.backends.null"):
        raise VaultError("no keyring answers on this machine (no Secret Service): set secrets.store to files")
    return backend


# ---------------------------------------------------------------------------------------------------- the process's
_active: Vault | None = None


def install(vault: Vault | None) -> None:
    """Make a vault the one this process fills secrets in from (``expand``)."""
    global _active
    _active = vault


def active() -> Vault | None:
    """The vault this process installed, None until it does."""
    return _active


@contextlib.contextmanager
def installed(vault: Vault | None) -> Iterator[Vault | None]:
    """Use a vault for the length of a block, then the one before it again (tests)."""
    before = _active
    install(vault)
    try:
        yield vault
    finally:
        install(before)


def open_vault(cfg: "Config") -> Vault:
    """The vault ``secrets.*`` names, installed as this process's own.

    Args:
        cfg: the settings.

    Returns:
        The vault.

    Raises:
        VaultError: the store can't be used (no keyring answers: set ``secrets.store`` to ``files``).
    """
    vault = Vault(cfg.secrets.store, cfg.secrets_path)
    install(vault)
    return vault


def expand(text: str, *, environ: Mapping[str, str] | None = None, only: str = "") -> str:
    """A setting's text with ``${secret:NAME}`` filled in from the vault and ``${VAR}`` or ``$VAR`` from the
    environment, in one pass: what is filled in is never read again (a secret that holds ``$HOME`` keeps it). A
    variable that isn't set stays as written.

    Args:
        text: the setting.
        environ: the environment (the process's own by default).
        only: when given, the prefix every secret named must have: a connector uses its own (``connector.<id>.``).

    Returns:
        The text.

    Raises:
        VaultError: no vault is open, a secret isn't set or isn't the caller's, or the vault can't be read.
    """
    if "$" not in text:
        return text
    env = os.environ if environ is None else environ

    def fill(match: re.Match[str]) -> str:
        name = match.group(1)
        if name is None:
            return env.get(match.group(2) or match.group(3), match.group(0))
        if only and not name.startswith(only):
            raise VaultError(f"{name} isn't one of this server's secrets (they are named {only}…)")
        if _active is None:
            raise VaultError(f"the secret {name} can't be filled in: no vault is open")
        found = _active.get(name)
        if found is None:
            raise VaultError(f"the secret {name} isn't set (Settings, or `orion secret set {name}`)")
        return found

    return _EXPANSION.sub(fill, text)


def has_reference(text: str, environ: Mapping[str, str] | None = None) -> bool:
    """Whether a setting's text refers to a secret (``${secret:NAME}``) or a variable (``${VAR}``, or ``$VAR`` of one
    that is set): then it isn't itself a value to keep. A ``$word`` that names nothing is just text (a password with a
    dollar in it).

    Args:
        text: the setting.
        environ: the environment (the process's own by default).
    """
    env = os.environ if environ is None else environ
    return any(match.group(1) or match.group(2) or match.group(3) in env for match in _EXPANSION.finditer(text))


def expand_all(value: Any, *, only: str = "") -> Any:
    """``expand`` throughout a setting's strings: its mappings and lists too (only when connecting, so a secret never
    reaches the HUD).

    Raises:
        VaultError: as ``expand``.
    """
    if isinstance(value, str):
        return expand(value, only=only)
    if isinstance(value, dict):
        return {key: expand_all(item, only=only) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_all(item, only=only) for item in value]
    return value


# ---------------------------------------------------------------------------------------------------- the environment
def inherited(environ: Mapping[str, str], allow: Collection[str]) -> dict[str, str]:
    """The variables of an environment that an allow-list names (a name ending in ``*`` is a prefix: ``LC_*``, which
    never lets a variable through that looks secret: only a name written out can).

    Args:
        environ: the environment.
        allow: ``safety.env_allow`` (and ``env_desktop`` for a server he trusts).

    Returns:
        A copy of just those.
    """
    exact = {name for name in allow if not name.endswith("*")}
    prefixes = tuple(name[:-1] for name in allow if name.endswith("*"))
    return {name: value for name, value in environ.items()
            if name in exact or (name.startswith(prefixes) and not looks_secret(name))}


def strip_environ(environ: Mapping[str, str]) -> dict[str, str]:
    """An environment without the variables that look secret (for his own helper programs: ``xdg-open`` and the
    apps it starts must not inherit a token)."""
    return {name: value for name, value in environ.items() if not looks_secret(name)}


def secret_environ(environ: Mapping[str, str]) -> dict[str, str]:
    """The variables that look secret by their name and hold something that isn't a path that exists (``SSH_AUTH_SOCK``
    names a socket, not a secret)."""
    return {name: value for name, value in environ.items() if value and looks_secret(name) and not _is_path(value)}


def _is_path(value: str) -> bool:
    """Whether a value is a file or folder that exists."""
    return value.startswith(("/", "~")) and os.path.exists(os.path.expanduser(value))


def parts(value: str) -> set[str]:
    """What else in a secret is secret: the credentials after a scheme (``Bearer abc``: abc), and the strings named
    like secrets inside a secret that is JSON (an OAuth sign-in's access and refresh tokens).

    Args:
        value: a secret.

    Returns:
        The extra values (not the value itself).
    """
    found: set[str] = set()
    if match := _SCHEME.fullmatch(value.strip()):
        found.add(match.group(1))
    if value.lstrip().startswith(("{", "[")):
        with contextlib.suppress(ValueError):
            stack: list[tuple[str, Any]] = [("", json.loads(value))]
            while stack:
                key, item = stack.pop()
                if isinstance(item, str):
                    if looks_secret(key) and not key.lower().endswith("type") and item:  # (token_type: Bearer)
                        found.add(item)
                        found |= parts(item)
                elif isinstance(item, dict):
                    stack.extend((str(k), v) for k, v in item.items())
                elif isinstance(item, list):
                    stack.extend((key, v) for v in item)
    return found


class Known:
    """What a process keeps secret, for its redactor to learn (``redact.Source``): the vault's values, the secret-looking
    variables of its environment (read once: it doesn't change), and what else those hold (``parts``).

    Args:
        vault: the vault, None when there is none (the Discord bridge has only its environment).
        environ: the environment.
        also: more values to hide, read when it is asked (the secrets still in the settings, when no vault could take them).
    """

    def __init__(self, vault: Vault | None, environ: Mapping[str, str],
                 also: Callable[[], Iterable[str]] = lambda: ()) -> None:
        self._vault = vault
        self._environ = dict(secret_environ(environ))
        self._also = also

    def revision(self) -> object:
        """Changes whenever a secret is set or deleted, by any process."""
        return self._vault.revision() if self._vault is not None else None

    def values(self) -> list[str]:
        """Every value that is secret now.

        Raises:
            VaultError: the vault can't be read.
        """
        values = [*self._environ.values(), *self._also(), *(self._vault.values() if self._vault is not None else ())]
        found = set(values)
        for value in values:
            found |= parts(value)
        return list(found)


def protect(safety: "SafetyConfig", vault: Vault | None, environ: Mapping[str, str],
            also: Callable[[], Iterable[str]] = lambda: ()) -> "Redactor":
    """Make this process hide its secrets: build its redactor from the safety settings and what it keeps secret,
    install it, and have every log line pass through it.

    Args:
        safety: ``cfg.safety`` (the patterns and the shortest value).
        vault: the process's vault, None when it has none.
        environ: its environment.
        also: more values it hides (``Known``).

    Returns:
        The redactor (the hub asks it to ``refresh`` when the vault changes).
    """
    redactor = redact.Redactor(safety.secret_patterns, min_chars=safety.redact_min_chars,
                               source=Known(vault, environ, also))
    redact.install(redactor)
    redact.guard_logs()
    redactor.refresh()
    return redactor


def names_in(values: Iterable[str]) -> list[str]:
    """The secrets' names a setting's strings refer to (``${secret:NAME}``), in order, each once."""
    return list(dict.fromkeys(name for text in values for name in references(text)))
