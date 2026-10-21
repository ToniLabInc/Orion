"""watchers: a reading past its limit warns only after it stays there, clears when it comes back, waits before
warning again, and a reading the machine doesn't have (or a plugged-in battery) never warns."""

from typing import Any

from orion.config import default_config
from orion.watchers import Alert, Watchers


def stats(**changes: Any) -> dict[str, Any]:
    base = {"cpu": 10.0, "ram": 40.0, "ram_used": 6.0, "ram_total": 16.0, "gpu": None, "disk": 50.0,
            "disk_free": 200.0, "battery": {"percent": 80.0, "plugged": False}, "cpu_temp": 50.0, "uptime": 1.0,
            "top": []}
    return base | changes


def watching() -> tuple[Watchers, list[Alert], list[list[Alert]]]:
    cfg = default_config()
    cfg.watchers.hold_s, cfg.watchers.repeat_min = 30, 10
    fired: list[Alert] = []
    changes: list[list[Alert]] = []
    return Watchers(cfg, on_alert=fired.append, on_change=changes.append), fired, changes


def test_a_low_battery_warns_after_it_stays_low_and_clears_when_plugged_in() -> None:
    w, fired, changes = watching()
    low = stats(battery={"percent": 15.0, "plugged": False})
    w.check(low, now=1000)
    w.check(low, now=1020)
    assert fired == []  # not long enough yet
    w.check(low, now=1031)
    assert [a.id for a in fired] == ["battery_low"] and fired[0].text == "Sir, the battery is down to 15%. A " \
                                                                         "charger would be wise."
    w.check(low, now=1100)
    assert len(fired) == 1  # said once
    w.check(stats(battery={"percent": 15.0, "plugged": True}), now=1110)
    assert w.active() == [] and len(changes) == 2  # plugged in: no battery to warn about
    w.check(low, now=1200)
    w.check(low, now=1240)
    assert len(fired) == 1  # cleared less than repeat_min ago: not again yet
    w.check(low, now=1110 + 10 * 60 + 31)
    assert len(fired) == 2


def test_hot_and_full_readings_warn_and_off_switches_them_off() -> None:
    w, fired, _ = watching()
    hot = stats(cpu_temp=97.0, disk=95.0, gpu={"name": "RTX", "util": 99.0, "mem_used": 1.0, "mem_total": 6.0,
                                               "temp": 90.0})
    w.check(hot, now=0)
    w.check(hot, now=31)
    assert {a.id for a in fired} == {"cpu_hot", "gpu_hot", "disk_full"}
    assert w.active()[0].level == "warn"
    w.cfg.watchers.disk_full = 0
    w.check(stats(cpu_temp=97.0, disk=99.0), now=40)
    assert {a.id for a in w.active()} == {"cpu_hot"}  # the GPU went away (no reading) and the disk is off


def test_a_critical_battery_comes_first() -> None:
    w, _, _ = watching()
    empty = stats(battery={"percent": 5.0, "plugged": False})
    w.check(empty, now=0)
    w.check(empty, now=31)
    assert [a.id for a in w.active()] == ["battery_critical", "battery_low"]
