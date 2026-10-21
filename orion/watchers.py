"""Watchers: he speaks up when the machine needs you, from the vital signs he reads anyway (``service.Vitals``):
the battery running low, the processor or the graphics card running hot, the disk or the memory filling up.

Each rule watches one reading against a limit in the settings (``watchers.*``; 0 switches it off). A reading must
stay past its limit for ``watchers.hold_s`` before it warns (no flapping), and a warning that cleared waits
``watchers.repeat_min`` before it may come again. A rule whose reading this machine doesn't have (a desktop's
battery, a server's GPU) simply never fires, and says so in its status. Nothing here reads a sensor or speaks: the
runtime hands each snapshot in (``check``) and says what comes out (``Alert``).
"""

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Final, Literal

from orion.base import JSON

if TYPE_CHECKING:
    from orion.config import Config, WatchersConfig
    from orion.service import Stats

CLEAR_MARGIN: Final = 2.0  # a reading must come this far back past its limit before its warning clears


@dataclass(frozen=True, slots=True)
class Rule:
    """One thing to watch.

    Attributes:
        id: its name, also its setting under ``watchers``.
        title: for people.
        above: it warns when the reading goes above the limit (False: below).
        unit: the reading's unit, as said ("%", "°C").
        read: the reading in a vitals snapshot (None when this machine doesn't have it, or it doesn't apply now,
            such as a battery while plugged in).
        level: how serious its warning is.
        needs: what the machine must have for its reading ("battery", "temperatures"; "" for every machine).
    """

    id: str
    title: str
    above: bool
    unit: str
    read: Callable[["Stats"], float | None]
    level: Literal["info", "warn", "critical"] = "warn"
    needs: str = ""  # what the machine must have for the reading (``orion.host.NEEDS``; "" for every machine)


def _unplugged_battery(stats: "Stats") -> float | None:
    battery = stats.get("battery")
    return None if battery is None or battery.get("plugged") is True else float(battery["percent"])


def _gpu_temp(stats: "Stats") -> float | None:
    gpu = stats.get("gpu")
    return float(gpu["temp"]) if gpu else None


RULES: Final[tuple[Rule, ...]] = (
    Rule("battery_low", "Battery low", above=False, unit="%", read=_unplugged_battery, needs="battery"),
    Rule("battery_critical", "Battery critical", above=False, unit="%", read=_unplugged_battery, level="critical",
         needs="battery"),
    Rule("cpu_hot", "Processor hot", above=True, unit="°C", read=lambda s: s.get("cpu_temp"), needs="temperatures"),
    Rule("gpu_hot", "Graphics card hot", above=True, unit="°C", read=_gpu_temp, needs="nvidia"),
    Rule("disk_full", "Disk nearly full", above=True, unit="%", read=lambda s: s.get("disk")),
    Rule("memory_full", "Memory nearly full", above=True, unit="%", read=lambda s: s.get("ram")),
)


@dataclass(frozen=True, slots=True)
class Alert:
    """A warning that is on now.

    Attributes:
        id: its rule's id.
        level: how serious.
        title: for people.
        text: what he says.
        value: the reading when it fired.
        at: when it fired (epoch seconds).
    """

    id: str
    level: str
    title: str
    text: str
    value: float
    at: float

    def as_dict(self) -> dict[str, JSON]:
        """For the HUD."""
        return dict(asdict(self))


@dataclass(slots=True)
class _State:
    since: float | None = None  # when the reading went past its limit (None: it isn't)
    alert: Alert | None = None  # the warning on now
    cleared: float | None = None  # when the last warning cleared (None: never)
    value: float | None = None  # the last reading


@dataclass(slots=True)
class Watchers:
    """The rules' states, checked against each vitals snapshot (see the module docstring).

    Attributes:
        cfg: the live settings.
        on_alert: told about each new warning.
        on_change: told whenever the warnings on now change.
    """

    cfg: "Config"
    on_alert: Callable[[Alert], None]
    on_change: Callable[[list[Alert]], None]
    _states: dict[str, _State] = field(default_factory=dict)

    def check(self, stats: "Stats", now: float | None = None) -> None:
        """Look at a snapshot: warn about readings past their limits long enough, clear those back in range.

        Args:
            stats: the vital signs.
            now: the time (epoch seconds; now by default).
        """
        now = time.time() if now is None else now
        settings = self.cfg.watchers
        changed = False
        for rule in RULES:
            state = self._states.setdefault(rule.id, _State())
            limit = float(getattr(settings, rule.id))
            value = rule.read(stats) if limit > 0 else None
            state.value = value
            past = value is not None and (value > limit if rule.above else value < limit)
            back = value is None or (value < limit - CLEAR_MARGIN if rule.above else value > limit + CLEAR_MARGIN)
            if state.alert is not None:
                if back:
                    state.alert, state.cleared, state.since = None, now, None
                    changed = True
                continue
            if not past:
                state.since = None
                continue
            state.since = now if state.since is None else state.since
            rested = state.cleared is None or now - state.cleared >= settings.repeat_min * 60
            if now - state.since >= settings.hold_s and rested and value is not None:
                state.alert = Alert(rule.id, rule.level, rule.title, self._text(rule, value), round(value, 1), now)
                changed = True
                self.on_alert(state.alert)
        if changed:
            self.on_change(self.active())

    def active(self) -> list[Alert]:
        """The warnings on now, the most serious first."""
        order = {"critical": 0, "warn": 1, "info": 2}
        return sorted((s.alert for s in self._states.values() if s.alert is not None),
                      key=lambda a: (order.get(a.level, 3), -a.at))

    def status(self) -> list[dict[str, JSON]]:
        """Every rule: its limit, whether this machine has its reading, the last reading and whether it warns now."""
        from orion.host import profile

        host = profile()
        settings = self.cfg.watchers
        out: list[dict[str, JSON]] = []
        for rule in RULES:
            state = self._states.get(rule.id, _State())
            limit = float(getattr(settings, rule.id))
            available = host.has(rule.needs) if rule.needs else True
            out.append({"id": rule.id, "title": rule.title, "limit": limit, "unit": rule.unit, "above": rule.above,
                        "enabled": limit > 0, "available": available, "value": state.value,
                        "alert": state.alert is not None})
        return out

    def _text(self, rule: Rule, value: float) -> str:
        """What he says for a warning (``prompts.watch_<rule>``)."""
        user = self.cfg.user
        template: str = getattr(self.cfg.prompts, f"watch_{rule.id}")
        return template.format(honorific=user.honorific, Honorific=user.honorific[:1].upper() + user.honorific[1:],
                               value=f"{value:.0f}")


def limits_of(settings: "WatchersConfig") -> dict[str, float]:
    """Each rule's limit (0: off)."""
    return {rule.id: float(getattr(settings, rule.id)) for rule in RULES}
