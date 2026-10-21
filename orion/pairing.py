"""Pairing: another device on your network uses the HUD once paired, with a code shown on this PC.

Orion has full access to the PC, so his HUD answers this PC alone unless ``server.host`` opens it to your network
(``server.py``'s envelope). Then any device that isn't this PC must be paired first. On the PC, Settings → General →
Devices → Pair a device shows a code of ``CODE_DIGITS`` digits; it works once, for ``server.pair_code_s`` and
``server.pair_tries`` wrong tries, and only the code shown last does. Typed on the device, it gives the device a key:
a cookie in a browser (HttpOnly, SameSite=Strict, Secure over HTTPS) and, for an app such as an MCP client, the same
key as a bearer token. A device stays paired while it is used at least once every ``server.session_days``; unpairing
it, or every device, takes its key away at once. Only each key's hash is kept, in ``<data>/devices.json``, readable by
you alone. Over plain HTTP the key crosses your network as it is, as everything else the HUD says does; with
``server.tls_cert`` and ``server.tls_key`` the HUD is served over HTTPS instead.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from orion.base import write_atomic

if TYPE_CHECKING:
    from orion.config import ServerConfig

log = logging.getLogger("orion.pairing")

FILE_NAME: Final = "devices.json"
COOKIE: Final = "orion_device"  # the key, in a browser
COOKIE_MAX_AGE_S: Final = 400 * 86400  # the longest a browser keeps a cookie; the pairing itself lapses unused first
CODE_DIGITS: Final = 8
NAME_MAX: Final = 40
SEEN_EVERY_S: Final = 3600  # a device's last use is written down at most this often
DAY_S: Final = 86400


@dataclass(slots=True)
class Device:
    """A paired device.

    Attributes:
        id: its short id (to unpair it).
        name: what it was called when it was paired.
        paired: when it was paired (epoch seconds).
        seen: when it was last used (to the hour).
        key: the hash of its key (sha256, hex).
    """

    id: str
    name: str
    paired: float
    seen: float
    key: str


def _hash(key: str) -> str:
    """A key's hash, as kept."""
    return hashlib.sha256(key.encode()).hexdigest()


class Pairing:
    """The pairing code waiting (one at a time) and the devices paired (see the module docstring); safe to use from
    any thread (the HUD's server asks on its worker threads, where devices.json is written)."""

    def __init__(self, folder: Path, server: ServerConfig, clock: Callable[[], float] = time.time) -> None:
        """Read the devices paired so far.

        Args:
            folder: where ``devices.json`` lives (his data folder).
            server: the live ``server`` settings (their changes apply at once).
            clock: the wall clock, in epoch seconds (tests pass their own).
        """
        self._path = folder / FILE_NAME
        self._server = server
        self._clock = clock
        self._code: tuple[str, float, int] | None = None  # the code waiting, when it ends, the wrong tries left
        self._lock = threading.RLock()
        self._devices = self._load()

    # ------------------------------------------------------------------------------------------------ the code
    def start(self) -> tuple[str, float]:
        """A new code for a device to type (any code before stops working).

        Returns:
            (the code, when it stops working, in epoch seconds).
        """
        code = f"{secrets.randbelow(10 ** CODE_DIGITS):0{CODE_DIGITS}d}"
        ends = self._clock() + self._server.pair_code_s
        with self._lock:
            self._code = (code, ends, self._server.pair_tries)
        log.info("a code waits for %d s", self._server.pair_code_s)
        return code, ends

    def waiting(self) -> float | None:
        """When the code waiting stops working; None when none waits."""
        with self._lock:
            if self._code is not None and self._clock() >= self._code[1]:
                self._code = None
            return self._code[1] if self._code is not None else None

    def pair(self, code: str, name: str) -> tuple[str, Device] | None:
        """Pair the device that typed the code shown on the PC.

        Args:
            code: what it typed.
            name: what to call it (cut to ``NAME_MAX``).

        Returns:
            (its key, the device), or None for a wrong code, a late one, or none waiting (a wrong one uses up a try).

        Raises:
            ValueError: the name is empty.
        """
        name = " ".join(name.split())[:NAME_MAX]
        if not name:
            raise ValueError("a device needs a name")
        with self._lock:
            if self.waiting() is None or self._code is None:
                return None
            want, ends, left = self._code
            if not hmac.compare_digest(code.strip().encode(), want.encode()):
                self._code = (want, ends, left - 1) if left > 1 else None
                log.warning("a wrong code (%s)", f"{left - 1} tries left" if left > 1 else "it stopped working")
                return None
            self._code = None
            key = secrets.token_urlsafe(32)
            now = self._clock()
            device = Device(secrets.token_hex(4), name, now, now, _hash(key))
            self._devices[device.key] = device
            self._save()
        log.info("%s is paired", device.name)
        return key, device

    # ------------------------------------------------------------------------------------------------ the devices
    def device(self, key: str) -> Device | None:
        """The paired device a key belongs to, its use noted; None for no one's, or a pairing unused too long.

        Args:
            key: what the request carried ("" for nothing).
        """
        if not key:
            return None
        with self._lock:
            found = self._devices.get(_hash(key))
            if found is None:
                return None
            now = self._clock()
            if now - found.seen > self._server.session_days * DAY_S:
                del self._devices[found.key]
                self._save()
                log.info("%s was unused for %d days: unpaired", found.name, self._server.session_days)
                return None
            if now - found.seen >= SEEN_EVERY_S:
                found.seen = now
                self._save()
            return found

    def devices(self) -> list[dict[str, str | float]]:
        """The devices paired, oldest first: id, name, when paired and when last used (never their keys)."""
        with self._lock:
            return [{"id": d.id, "name": d.name, "paired": d.paired, "seen": d.seen}
                    for d in sorted(self._devices.values(), key=lambda d: d.paired)]

    def unpair(self, device_id: str | None) -> int:
        """Take a device's key away, or every device's.

        Args:
            device_id: the device's id; None for every device.

        Returns:
            How many were unpaired.
        """
        with self._lock:
            gone = [d for d in self._devices.values() if device_id is None or d.id == device_id]
            for d in gone:
                del self._devices[d.key]
            if gone:
                self._save()
        if gone:
            log.info("unpaired %s", ", ".join(d.name for d in gone))
        return len(gone)

    # ------------------------------------------------------------------------------------------------ the file
    def _load(self) -> dict[str, Device]:
        """The devices in devices.json; none when it is missing, or unreadable (they pair again)."""
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            devices = [Device(**entry) for entry in raw["devices"]]
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, TypeError, KeyError) as e:
            log.warning("%s can't be read (%s): every device pairs again", self._path, e)
            return {}
        return {d.key: d for d in devices}

    def _save(self) -> None:
        """Write devices.json (readable by you alone; under the lock)."""
        text = json.dumps({"devices": [asdict(d) for d in self._devices.values()]}, indent=1)
        write_atomic(self._path, text + "\n", private=True)
