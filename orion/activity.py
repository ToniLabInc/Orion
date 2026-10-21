"""The activity log: everything Orion did and who allowed it, in one place.

Each tool call (whoever asked: you, his brain, a job, Discord, a gesture, another app), each question he asked
before a risky action and its answer, each call a channel's limits refused or the redactor blocked, each alert,
job run, backup, and each connector, plugin, skill or secret added or removed (a secret by name only). The HUD's
Activity page reads it, live; so does he, when you ask what he did ("activity_recent").

It is an audit trail, so nothing in it may hold a secret: ``record`` passes every text through the redactor it was
given. Entries are written on the store's thread (``Store.submit``), never waited for, and pruned after
``activity.keep_days``.
"""

import logging
import time
from collections.abc import Callable, Collection, Mapping
from concurrent.futures import Future
from typing import TYPE_CHECKING, Any, Final, Literal

from orion.base import JSON

if TYPE_CHECKING:
    from orion.memory.store import ActivityRow, Store

log = logging.getLogger("orion.activity")

type Kind = Literal["tool", "ask", "deny", "block", "alert", "job", "backup", "connector", "plugin", "skill", "secret",
                    "instant", "login", "bench", "system"]
KINDS: Final[tuple[str, ...]] = ("tool", "ask", "deny", "block", "alert", "job", "backup", "connector", "plugin",
                                 "skill", "secret", "instant", "login", "bench", "system")
SOURCES: Final[tuple[str, ...]] = ("voice", "hud", "discord", "job", "watcher", "gesture", "app", "user", "system")
DETAIL_CHARS: Final = 2000  # the longest detail kept (a tool's arguments, a reason)
TITLE_CHARS: Final = 200


class Activity:
    """The log.

    Args:
        store: the database.
        publish: ``EventBus.publish`` (thread-safe): the HUD's ``activity`` event carries each new entry.
        redact: cuts secrets out of a text (``orion.redact``); every text recorded passes through it.
    """

    def __init__(self, store: "Store", publish: Callable[..., None], redact: Callable[[str], str]) -> None:
        self._store = store
        self._publish = publish
        self._redact = redact

    def record(self, kind: Kind, *, source: str, title: str, outcome: str = "", actor: str = "", connector: str = "",
               tool: str = "", detail: str | Mapping[str, Any] = "", turn: str = "", ms: int = 0) -> Future[int]:
        """Add an entry (written in the background, so the caller needn't wait; the HUD hears of it at once).

        Args:
            kind: what happened (``KINDS``).
            source: who or what asked (``SOURCES``).
            title: one line for people ("Ran a shell command", "Asked before sending an e-mail").
            outcome: "ok", "failed", "denied", "blocked", "approved", "declined", "expired" ("" none).
            actor: who acted or answered ("the brain", "you, on Discord", a job's name).
            connector: the connector involved.
            tool: the tool involved.
            detail: more (a mapping is shown as ``key: value`` lines); cut to ``DETAIL_CHARS``.
            turn: the turn it belongs to.
            ms: how long it took.

        Returns:
            The write's future, for a process about to end (``orion secret``) to wait on.
        """
        if kind not in KINDS or source not in SOURCES:
            raise ValueError(f"unknown activity kind {kind!r} or source {source!r}")
        text = detail if isinstance(detail, str) else "\n".join(f"{key}: {value}" for key, value in detail.items())
        row: ActivityRow = {
            "id": 0, "at": time.time(), "kind": kind, "source": source, "actor": self._redact(actor)[:TITLE_CHARS],
            "connector": connector, "tool": tool, "title": self._redact(title)[:TITLE_CHARS],
            "detail": self._redact(text)[:DETAIL_CHARS], "outcome": outcome, "turn": turn, "ms": max(0, int(ms)),
        }
        future = self._store.submit(self._store.log_activity, row)
        future.add_done_callback(lambda done: self._published(row, done.result()) if done.exception() is None
                                 else log.warning("activity not saved: %s", done.exception()))
        return future

    def recent(self, *, before: int | None = None, limit: int = 50, kinds: Collection[str] = (),
               sources: Collection[str] = (), text: str = "") -> list["ActivityRow"]:
        """Entries, newest first (see ``Store.activity``)."""
        return self._store.activity(before=before, limit=limit, kinds=kinds, sources=sources, text=text)

    def prune(self, keep_days: float) -> int:
        """Forget what is older than ``keep_days`` days; returns how many entries went."""
        return self._store.prune_activity(time.time() - keep_days * 86400)

    def _published(self, row: "ActivityRow", ident: int) -> None:
        """Tell the HUD about an entry once it has its id (on the store's thread: ``EventBus.publish`` is
        thread-safe and never raises)."""
        item: dict[str, JSON] = {**row, "id": ident}
        self._publish("activity", item=item)
