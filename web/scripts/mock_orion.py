"""A stand-in Orion for looking at the HUD: it serves a built HUD, answers its REST calls with realistic data, and keeps
a /ws that sends a realistic `init`, his stats every two seconds, a scripted conversation (a message, tools that start
and end, a recall, a streaming reply he speaks, a question before a risky action) and, now and then, a live activity
entry. Every endpoint of the round's spec exists: the connectors, skills, plugins, activity log, vault, channel
limits, timings, spend, benchmarks, backups and documents are in mock_state.py (their data in mock_data.py) and keep
what the HUD changes until the mock is restarted.

For screenshots, a scene: open the HUD with ?scene=<name> and the page is fed that scene's events straight through
window.__orion.receive (a module script the mock adds to the page, timed with setTimeout from the moment the app is
up), and its WebSocket is a stand-in that opens at once and stays quiet, so only the scene speaks. Scenes: idle,
conversation, cards, today, chips (a timer, a question and an alert in the top bar), instant (an instant command's
reply), phone, offline (the link drops), unpaired (the pairing screen). Any page works with any scene
(/customize/connectors?scene=idle).

    uv run --no-project --with pydantic --with pyyaml --with cronsim --with tzlocal --with google-re2 python web/scripts/mock_orion.py --dist /tmp/hud-dist
    # then http://127.0.0.1:8790/ (live) or http://127.0.0.1:8790/?scene=conversation (a scene)

Python's standard library (the WebSocket too), plus pydantic, PyYAML, cronsim, tzlocal and google-re2 (what orion/config.py imports): the Settings pages' schema and defaults come
from orion/config.py itself, so the settings it shows are the real ones (an option of a backend that
orion/config.py does not have is added to them here).
"""

import argparse
import base64
import hashlib
import json
import math
import mimetypes
import random
import socket
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import mock_state

ROOT = Path(__file__).resolve().parents[2]
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # RFC 6455
SURFACE = "hud-a1b2c3d4"
PHONE = "hud-9f8e7d6c"
STATE: mock_state.State = mock_state.State(8790)  # the newer pages' state (main() makes it again for its port)
LIVE = "conversation"  # the scene a page without ?scene= is played (--live)


# ---------------------------------------------------------------------------------------------------- the data
# Everything with a time in it is made as it is sent, so a timer is always seven minutes from done and a job's next run
# always agrees with its schedule, however long the mock has been running.
def ago(seconds: float) -> float:
    return time.time() - seconds


def at_clock(hour: int, minute: int = 0, days: int = 0) -> float:
    """Today (or `days` later) at hour:minute, local time."""
    t = time.localtime(time.time() + days * 86400)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, hour, minute, 0, 0, 0, -1))


def next_clock(hour: int, minute: int = 0, weekdays: bool = False, on: int | None = None) -> float:
    """The next hour:minute to come (on a weekday, or on weekday `on` (0 is Monday), if asked)."""
    for days in range(8):
        when = at_clock(hour, minute, days)
        wday = time.localtime(when).tm_wday
        if when > time.time() and (not weekdays or wday < 5) and (on is None or wday == on):
            return when
    return at_clock(hour, minute, 1)


def iso(ts: float, day_only: bool = False) -> str:
    return time.strftime("%Y-%m-%d" if day_only else "%Y-%m-%dT%H:%M:%S", time.localtime(ts))


def stats_sample(i: int) -> dict:
    """One reading of the vitals, a smooth wander."""
    t = i / 3.0
    cpu = 18 + 14 * math.sin(t * 0.7) + 9 * math.sin(t * 1.9 + 1) + random.uniform(-3, 3)
    gpu = 34 + 20 * math.sin(t * 0.45 + 2) + random.uniform(-4, 4)
    ram = 46 + 4 * math.sin(t * 0.2) + random.uniform(-0.6, 0.6)
    return {
        "cpu": round(max(2, min(98, cpu)), 1),
        "cpu_temp": round(52 + cpu * 0.25, 1),
        "ram_used": round(32 * ram / 100, 1),
        "ram_total": 32.0,
        "ram": round(ram, 1),
        "disk": 71.4,
        "disk_free": 268.0,
        "battery": None,
        "gpu": {
            "name": "NVIDIA GeForce RTX 4070",
            "util": round(max(1, min(99, gpu)), 1),
            "mem_used": 6150,
            "mem_total": 12282,
            "temp": round(48 + gpu * 0.3),
        },
        "uptime": 36000 + i * 2,
        "top": [
            {"name": "Lunar Client", "cpu": 21.4, "mem": 3.2},
            {"name": "Firefox", "cpu": 6.1, "mem": 2.4},
            {"name": "Orion", "cpu": 3.8, "mem": 1.6},
            {"name": "Code", "cpu": 1.2, "mem": 1.1},
        ],
    }


STATS_HISTORY = [stats_sample(i) for i in range(40)]


def weather() -> dict:
    now = time.time()
    days = [
        ("partly cloudy", 2, 17, 8, 10),
        ("light rain", 61, 13, 7, 70),
        ("overcast", 3, 12, 6, 30),
        ("clear sky", 0, 16, 5, 0),
        ("clear sky", 0, 18, 6, 0),
        ("mainly clear", 1, 19, 8, 5),
        ("drizzle", 51, 15, 9, 45),
    ]
    hour0 = now - now % 3600
    return {
        "place": "Lisbon, Portugal",
        "temp": 14,
        "feels_like": 12,
        "humidity": 64,
        "wind": 11,
        "condition": "partly cloudy",
        "code": 2,
        "lat": 44.43,
        "lon": 26.1,
        "days": [
            {
                "date": iso(now + d * 86400, True),
                "condition": c,
                "code": code,
                "max": mx,
                "min": mn,
                "rain_chance": rain,
                "sunrise": "07:24",
                "sunset": "18:41",
            }
            for d, (c, code, mx, mn, rain) in enumerate(days)
        ],
        "hours": [
            {
                "time": time.strftime("%Y-%m-%dT%H:00", time.localtime(hour0 + h * 3600)),
                "temp": round(
                    14 - 5 * math.cos((time.localtime(hour0 + h * 3600).tm_hour - 4) / 24 * 2 * math.pi) - 2, 1
                ),
                "code": 2 if h < 14 else 61 if h < 22 else 3,
                "rain_chance": 10 if h < 14 else 65 if h < 22 else 30,
            }
            for h in range(24)
        ],
    }


def news() -> list[dict]:
    items = [
        ("ESA's Hera probe sends its first close-up images of the asteroid Didymos", "Reuters", 1800),
        ("European markets edge higher as inflation cools for a third month", "Financial Times", 5400),
        ("A new open-source speech model runs in real time on a laptop GPU", "The Verge", 9000),
        ("A new night train links Lisbon and Porto", "Example News", 15000),
        ("Webb telescope finds water vapour round a young star in Orion", "BBC News", 26000),
    ]
    return [
        {
            "title": title,
            "source": source,
            "url": f"https://example.com/{i}",
            "date": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(ago(age))),
        }
        for i, (title, source, age) in enumerate(items, 1)
    ]


def reminders() -> list[dict]:
    return [
        {"id": 7, "text": "pasta", "due": time.time() + 7 * 60 + 12, "kind": "timer", "ts": ago(168)},
        {"id": 8, "text": "Take the bins out", "due": next_clock(21), "kind": "reminder", "ts": ago(3 * 3600)},
    ]


def todos() -> list[dict]:
    return [
        {"id": 1, "text": "Renew the car insurance", "ts": ago(86400)},
        {"id": 2, "text": "Order the new SSD", "ts": ago(7200)},
        {"id": 3, "text": "Send Sam the photos from the hike", "ts": ago(600)},
    ]


def jobs() -> list[dict]:
    now = time.time()
    hour = now - now % 3600
    return [
        {
            "id": 1,
            "name": "Morning briefing",
            "schedule": "30 7 * * 1-5",
            "explain": "At 07:30 on Monday through Friday",
            "prompt": "Brief me: the weather, my calendar today and the top news, in a few sentences.",
            "deliver": ["speak", "hud"],
            "enabled": True,
            "next_run": next_clock(7, 30, weekdays=True),
            "last_run": at_clock(7, 30) if at_clock(7, 30) < time.time() else at_clock(7, 30, -1),
            "last_status": "ok",
            "last_text": "Good morning, sir. 9° and clear, a stand-up at 10:00, and the markets are up.",
            "created": ago(30 * 86400),
        },
        {
            "id": 2,
            "name": "Build watch",
            "schedule": "0 * * * *",
            "explain": "At minute 0 of every hour",
            "prompt": "Check the CI of the Orion repository and tell me only if a build is red.",
            "deliver": ["hud"],
            "enabled": True,
            "next_run": hour + 3600,
            "last_run": hour,
            "last_status": "error",
            "last_text": "The CI page didn't answer within 20 s.",
            "created": ago(9 * 86400),
        },
        {
            "id": 3,
            "name": "Weekly review",
            "schedule": "0 18 * * 0",
            "explain": "At 18:00 on Sunday",
            "prompt": "Go over the week's notes and tell me what is still open.",
            "deliver": ["speak"],
            "enabled": False,
            "next_run": None,
            "last_run": None,
            "last_status": "",
            "last_text": "",
            "created": ago(3 * 86400),
        },
    ]


def calendar_events() -> list[dict]:
    now = time.time()
    review = now - now % 1800 + 3600  # on the half hour, 30 to 60 minutes from now
    gym = at_clock(19, 30) if at_clock(19, 30) > now + 3600 else at_clock(19, 30, 1)
    return [
        {
            "id": "e1",
            "source": "google",
            "calendar": "Work",
            "title": "Design review",
            "start": iso(review),
            "end": iso(review + 3600),
            "all_day": False,
            "location": "Meet",
        },
        {
            "id": "e2",
            "source": "google",
            "calendar": "Personal",
            "title": "Gym",
            "start": iso(gym),
            "end": iso(gym + 3600),
            "all_day": False,
            "location": "City Gym",
        },
        {
            "id": "e3",
            "source": "google",
            "calendar": "Work",
            "title": "Stand-up",
            "start": iso(at_clock(10, 0, 1)),
            "end": iso(at_clock(10, 15, 1)),
            "all_day": False,
            "location": "",
        },
        {
            "id": "e4",
            "source": "ics",
            "calendar": "Holidays",
            "title": "Sam's birthday",
            "start": iso(now + 2 * 86400, True),
            "end": iso(now + 3 * 86400, True),
            "all_day": True,
            "location": "",
        },
    ]


HOST = {
    "platform": "linux",
    "container": False,
    "session": "wayland",
    "desktop": "ubuntu:GNOME",
    "dbus": True,
    "opener": "xdg-open",
    "sound": "wpctl",
    "power": True,
    "battery": False,
    "temperatures": True,
    "nvidia": True,
    "clipboard": "wl",
    "screen": "portal",
}


def surfaces() -> list[dict]:
    return [
        {
            "id": SURFACE,
            "device": "Chrome on Linux",
            "local": True,
            "caps": ["mic", "speaker", "screen", "camera", "clipboard"],
            "sharing": [],
            "seen": time.time(),
        },
        {
            "id": PHONE,
            "device": "Safari on iPhone",
            "local": False,
            "caps": ["mic", "speaker", "camera", "clipboard"],
            "sharing": [],
            "seen": ago(900),
        },
    ]


CAPABILITIES = {
    "tools": 64,
    "servers": 7,
    "calls": 1284,
    "skills": 4,
    "models": {
        "llm": "Claude Sonnet 4.6",
        "provider": "claude",
        "stt": "large-v3-turbo",
        "stt_device": "cuda",
        "voice": "bm_george",
        "fallback": "OpenRouter · qwen3-235b",
        "standing_in": False,
        "reason": "",
    },
}

HUD = {
    "quick_timers": [5, 10, 25],
    "max_message_chars": 4000,
    "stats_interval_s": 2,
    "tool_result_chars": 2000,
    "sounds": True,
    "max_pictures": 4,
}

BOOT = {
    "ready": True,
    "steps": [{"name": n, "status": "ok", "detail": ""} for n in ("tools", "brain", "memory", "voice", "ears")],
}


def step(
    sid: str,
    turn: str,
    name: str,
    title: str,
    server: str,
    args: dict,
    at: float,
    ms: int,
    result: str,
    kind: str = "tool",
    ok: bool = True,
) -> dict:
    return {
        "id": sid,
        "turn": turn,
        "kind": kind,
        "name": name,
        "title": title,
        "server": server,
        "args": args,
        "source": "brain",
        "client": "",
        "at": at,
        "ms": ms,
        "ok": ok,
        "result": result,
        "taints": False,
        "memory": None,
    }


def history() -> list[dict]:
    return [
        {
            "id": "m1",
            "role": "user",
            "content": "Good morning! What do I have today?",
            "source": "voice",
            "ts": ago(5400),
        },
        {
            "id": "t0",
            "role": "assistant",
            "source": "hud",
            "ts": ago(5390),
            "content": "Good morning, sir. A stand-up at **10:00** and lunch with Jo at **13:30**. "
            "It's 9° and clear; it should reach 17° by the afternoon.",
            "steps": [
                step(
                    "s1",
                    "t0",
                    "list_events",
                    "Read your calendar",
                    "calendar",
                    {"when": "today"},
                    ago(5392),
                    640,
                    "10:00 Stand-up · 13:30 Lunch with Jo",
                ),
                step(
                    "s2",
                    "t0",
                    "get_weather",
                    "Weather and forecast",
                    "web",
                    {"location": "Lisbon"},
                    ago(5391),
                    820,
                    "9°C, clear, high 17°",
                ),
            ],
        },
        {
            "id": "x-o1",
            "role": "ops",
            "content": "",
            "source": "app",
            "ts": ago(4000),
            "client": "Claude Code",
            "steps": [
                step(
                    "o1",
                    "",
                    "web_search",
                    "Search the web",
                    "web",
                    {"query": "vite 8 rolldown options"},
                    ago(4000),
                    1300,
                    "- Rolldown options: …",
                )
            ],
        },
        {
            "id": "m2",
            "role": "user",
            "content": "Set a timer for the pasta, 10 minutes.",
            "source": "voice",
            "ts": ago(175),
        },
        {
            "id": "t1",
            "role": "assistant",
            "source": "hud",
            "ts": ago(170),
            "content": "Ten minutes on the clock for the pasta, sir. I'll tell you when it's done.",
            "steps": [
                step(
                    "s3",
                    "t1",
                    "set_timer",
                    "Set a timer",
                    "organizer",
                    {"duration": "10 minutes", "name": "pasta"},
                    ago(172),
                    90,
                    "Timer #7 'pasta' set for 10 min",
                )
            ],
        },
    ]


def init_event(**more) -> dict:
    """The `init` a HUD gets as it connects."""
    event = {
        "type": "init",
        "ts": time.time(),
        "state": "idle",
        "loading": "",
        "boot": BOOT,
        "muted": False,
        "voice_available": True,
        "voice": True,
        "conv": 12,
        "history": history(),
        "live_turn": None,
        "reminders": reminders(),
        "todos": todos(),
        "weather": weather(),
        "news": news(),
        "stats": STATS_HISTORY[-1],
        "stats_history": STATS_HISTORY,
        "capabilities": CAPABILITIES,
        "restart": [],
        "honorific": "sir",
        "user_name": "Alex",
        "brain_error": "",
        "hud": HUD,
        "surface": SURFACE,
        "confirms": [],
        "host": HOST,
        "surfaces": surfaces(),
        "voice_holder": {"device": "", "name": ""},
        "jobs": jobs(),
        "alerts": [],
        "spend": {"today": STATE.spend(1)["today"], "budget": 1.0},
    }
    event.update(more)
    return event


CONFIRM = {
    "type": "confirm",
    "id": "q1",
    "turn": "t2",
    "tool": "run_command",
    "title": "Run a shell command",
    "server": "system",
    "action": "run a shell command (rm -rf ~/Downloads/*.part)",
    "detail": "command: rm -rf ~/Downloads/*.part\ncwd: /home/you",
    "reason": "content",
    "source": "Read a web page",
    "text": "Sir, this request used content from a web page. Shall I run a shell command (rm -rf ~/Downloads/*.part)?",
    "source_kind": "hud",
    "expires": 0,
}


def conversation(delay0: int = 300) -> list[tuple[int, dict]]:
    """A turn, timed in ms from the start: a message, a recall, tools, a reply he speaks, then a question."""
    now = time.time()
    reply = (
        "It's **14°** and partly cloudy in Lisbon, sir, with rain likely around noon tomorrow.\n\n"
        "- I've set a reminder to **call your mother at 18:00**.\n- Your design review starts in half an hour.\n\n"
        "Shall I clear the half-finished downloads as well?"
    )
    events: list[tuple[int, dict]] = [
        (0, {"type": "summon", "action": "show", "panels": ["comms"], "source": "voice"}),
        (
            delay0,
            {
                "type": "user_message",
                "text": "What's the weather tomorrow, and remind me to call mom at six?",
                "source": "voice",
                "turn": "t2",
                "device": SURFACE,
            },
        ),
        (delay0 + 50, {"type": "state", "state": "thinking"}),
        (delay0 + 60, {"type": "assistant_start", "id": "t2", "channel": "voice"}),
        (
            delay0 + 200,
            {
                "type": "recall",
                "turn": "t2",
                "ms": 120,
                "items": [
                    {"id": "a", "text": "Alex's mother is called Rosa"},
                    {"id": "b", "text": "Alex lives in Lisbon"},
                ],
            },
        ),
        (
            delay0 + 350,
            {
                "type": "tool",
                "phase": "start",
                "id": "c1",
                "turn": "t2",
                "name": "get_weather",
                "title": "Weather and forecast",
                "server": "web",
                "args": {"location": "Lisbon", "days": 2},
                "source": "brain",
                "client": "",
                "read_only": True,
            },
        ),
        (
            delay0 + 500,
            {
                "type": "tool",
                "phase": "start",
                "id": "c2",
                "turn": "t2",
                "name": "set_reminder",
                "title": "Set a reminder",
                "server": "organizer",
                "args": {"message": "Call mom", "when": "today at 18:00"},
                "source": "brain",
                "client": "",
                "read_only": False,
            },
        ),
        (
            delay0 + 900,
            {
                "type": "tool",
                "phase": "end",
                "id": "c2",
                "turn": "t2",
                "name": "set_reminder",
                "title": "Set a reminder",
                "server": "organizer",
                "source": "brain",
                "client": "",
                "ok": True,
                "ms": 400,
                "result": "Reminder #9 set for 18:00",
                "error": "",
                "attempts": 1,
            },
        ),
        (
            delay0 + 950,
            {
                "type": "reminders",
                "items": reminders()
                + [{"id": 9, "text": "Call mom", "due": next_clock(18), "kind": "reminder", "ts": now}],
            },
        ),
        (
            delay0 + 1200,
            {
                "type": "tool",
                "phase": "end",
                "id": "c1",
                "turn": "t2",
                "name": "get_weather",
                "title": "Weather and forecast",
                "server": "web",
                "source": "brain",
                "client": "",
                "ok": True,
                "ms": 850,
                "result": "Tomorrow: light rain, 13° / 7°, 70% rain",
                "error": "",
                "attempts": 1,
            },
        ),
        (delay0 + 1250, {"type": "weather", "weather": weather()}),
        (
            delay0 + 1300,
            {
                "type": "tool",
                "phase": "start",
                "id": "c3",
                "turn": "t2",
                "name": "list_events",
                "title": "Read your calendar",
                "server": "calendar",
                "args": {"when": "today"},
                "source": "brain",
                "client": "",
                "read_only": True,
            },
        ),
    ]
    at = delay0 + 1500
    for i in range(0, len(reply), 24):
        events.append((at, {"type": "assistant_delta", "id": "t2", "text": reply[i : i + 24]}))
        at += 45
    events += [
        (delay0 + 1700, {"type": "state", "state": "speaking"}),
        (
            delay0 + 1720,
            {
                "type": "caption",
                "text": "It's 14° and partly cloudy in Lisbon, sir, with rain likely around noon tomorrow.",
            },
        ),
        (
            at + 100,
            {
                "type": "tool",
                "phase": "start",
                "id": "c4",
                "turn": "t2",
                "name": "run_command",
                "title": "Run a shell command",
                "server": "system",
                "args": {"command": "rm -rf ~/Downloads/*.part"},
                "source": "brain",
                "client": "",
                "read_only": False,
            },
        ),
        (at + 200, {**CONFIRM, "expires": now + 55 + (at + 200) / 1000}),
        (
            at + 210,
            {
                "type": "tool",
                "phase": "ask",
                "id": "c4",
                "turn": "t2",
                "name": "run_command",
                "question": "q1",
                "text": "Shall I run a shell command (rm -rf ~/Downloads/*.part)?",
            },
        ),
    ]
    return events


def scene(name: str) -> list[tuple[int, dict]]:
    """A scene's events, timed in ms."""
    if name == "idle":
        return [(0, init_event())]
    if name == "conversation":
        return (
            [(0, init_event())]
            + conversation()
            + [(1200, {"type": "summon", "action": "show", "panels": ["weather", "timers"], "source": "voice"})]
        )
    if name == "cards":
        return [
            (0, init_event()),
            (
                200,
                {
                    "type": "summon",
                    "action": "show",
                    "panels": ["timers", "calendar", "vitals", "weather"],
                    "source": "voice",
                },
            ),
        ]
    if name == "today":
        return [(0, init_event()), (200, {"type": "summon", "action": "show", "panels": ["today"], "source": "voice"})]
    if name == "chips":
        alert = {
            "id": "gpu_hot",
            "rule": "gpu_hot",
            "level": "warn",
            "title": "The GPU runs hot",
            "text": "87 °C for a minute: a game, or a stuck job?",
            "at": time.time() - 120,
        }
        return [
            (0, init_event(alerts=[alert])),
            (150, {**CONFIRM, "expires": time.time() + 55}),
            (200, {"type": "summon", "action": "show", "panels": ["today", "devices"], "source": "palette"}),
        ]
    if name == "instant":
        reply = "Five minutes on the clock, sir."
        return [
            (0, init_event()),
            (150, {"type": "user_message", "text": "Set a timer for five minutes", "source": "voice", "turn": "t9"}),
            (200, {"type": "assistant_start", "id": "t9", "channel": "voice", "instant": True}),
            (230, {"type": "assistant_done", "id": "t9", "text": reply, "instant": True, "ms": 320}),
            (
                250,
                {
                    "type": "timing",
                    "turn": "t9",
                    "at": time.time(),
                    "source": "voice",
                    "brain": "instant",
                    "instant": True,
                    "heard_ms": 480,
                    "think_ms": 0,
                    "tools_ms": 64,
                    "speak_ms": 240,
                    "total_ms": 784,
                },
            ),
            (260, {"type": "summon", "action": "show", "panels": ["comms"], "source": "voice"}),
        ]
    if name == "phone":
        return [(0, init_event())] + conversation(200)
    if name == "unpaired":
        return []  # (the stand-in socket closes with 4401: a device that isn't paired yet)
    return [(0, init_event())]  # idle, and offline (the stand-in socket drops after a moment and never comes back)


# ---------------------------------------------------------------------------------------------------- REST
EXTRA_SCHEMA = {
    "voice.echo_cancellation": {
        "kind": "bool",
        "label": "Echo cancellation",
        "default": True,
        "help": "Take his own voice out of the microphone, so you can interrupt him on the speakers of a laptop without headphones",
    },
    "voice.noise_suppression": {
        "kind": "choice",
        "label": "Noise suppression",
        "default": "moderate",
        "help": "How much steady noise (a fan, a hum) is taken out of what he hears",
        "choices": [{"value": v, "label": v.capitalize()} for v in ("off", "low", "moderate", "high")],
    },
    "instant.enabled": {
        "kind": "bool",
        "label": "Answer without his brain",
        "default": True,
        "help": "Timers, the volume, media, lights and the time are answered in under half a second, without his brain, and still work when it is down",
    },
    "instant.intents": {
        "kind": "list",
        "label": "Which commands",
        "default": [
            "HassStartTimer",
            "HassCancelTimer",
            "HassSetVolume",
            "HassMediaPause",
            "HassTurnOn",
            "HassTurnOff",
        ],
        "help": "Home Assistant's intent names he answers at once",
    },
    "openrouter.daily_budget_usd": {
        "kind": "float",
        "label": "Daily budget",
        "default": 1.0,
        "min": 0,
        "max": 1000,
        "step": 0.1,
        "unit": "$",
        "help": "The most he spends on OpenRouter in a local day; past it the stand-in brain answers. 0 is no budget",
    },
    "safety.sandbox_commands": {
        "kind": "bool",
        "label": "Run commands in a sandbox",
        "default": True,
        "help": "His shell commands run in a sandbox: the system read-only, your keys hidden. Off, they run with his full access (each still asks you first)",
    },
    "safety.sandbox_network": {
        "kind": "bool",
        "label": "Network for commands",
        "default": True,
        "help": "Whether a command in the sandbox may reach the network",
    },
    "backup.schedule": {
        "kind": "text",
        "label": "Schedule",
        "default": "0 3 * * *",
        "help": "When he backs up by himself: a cron line (minute hour day month weekday)",
    },
    "backup.keep": {
        "kind": "int",
        "label": "Keep",
        "default": 7,
        "min": 1,
        "max": 90,
        "unit": "backups",
        "help": "How many backups he keeps; the oldest go",
    },
    "backup.folder": {"kind": "text", "label": "Folder", "default": "", "help": "Where the backups are kept"},
    "activity.keep_days": {
        "kind": "int",
        "label": "Keep the log",
        "default": 365,
        "min": 7,
        "max": 3650,
        "unit": "days",
        "help": "How long the activity log keeps what he did",
    },
    "documents.extensions": {
        "kind": "list",
        "label": "File types",
        "default": ["md", "txt", "pdf", "docx"],
        "help": "The kinds of file he reads in your folders",
    },
    "documents.max_size_mb": {
        "kind": "int",
        "label": "Largest file",
        "default": 50,
        "min": 1,
        "max": 500,
        "unit": "MB",
        "help": "A larger file is left out",
    },
}


def settings_response() -> dict:
    """GET /api/settings: Orion's own schema and defaults, from orion/config.py."""
    sys.path.insert(0, str(ROOT))
    from orion.config import default_config, hud_schema  # needs pydantic and PyYAML

    cfg = default_config().model_dump(mode="json")
    cfg["user"].update({"name": "Alex", "honorific": "sir", "city": "Lisbon"})
    schema = hud_schema()
    voices = [
        {"id": "bm_george", "name": "George", "accent": "British", "gender": "male"},
        {"id": "bm_lewis", "name": "Lewis", "accent": "British", "gender": "male"},
        {"id": "bf_emma", "name": "Emma", "accent": "British", "gender": "female"},
        {"id": "am_michael", "name": "Michael", "accent": "American", "gender": "male"},
        {"id": "af_heart", "name": "Heart", "accent": "American", "gender": "female"},
        {"id": "af_bella", "name": "Bella", "accent": "American", "gender": "female"},
    ]
    for key, spec in EXTRA_SCHEMA.items():
        if key not in schema:
            schema[key] = {"restart": False, "help": "", **spec}
            section, option = key.split(".", 1)
            cfg.setdefault(section, {}).setdefault(option, spec["default"])
    cfg["documents"]["folders"] = ["/home/you/Documents/Notes", "/home/you/Obsidian/Vault"]
    cfg["openrouter"]["daily_budget_usd"] = 1.0
    for key in ("brain.provider", "brain.fallback"):  # (this branch's config.py predates the scripted test brain)
        if key in schema and not any(c["value"] == "script" for c in schema[key].get("choices", [])):
            schema[key].setdefault("choices", []).append({"value": "script", "label": "script"})
    for spec in schema.values():
        if spec.get("kind") == "choice" and not spec.get("choices"):
            spec["choices"] = [{"value": "auto", "label": "auto"}]
    if "tts.voice" in schema:
        schema["tts.voice"]["choices"] = [{"value": v["id"], "label": v["name"]} for v in voices]
    for key in ("voice.input_device", "voice.output_device"):
        if key in schema:
            schema[key]["choices"] = [
                {"value": "pipewire", "label": "pipewire"},
                {"value": "Blue Yeti", "label": "Blue Yeti"},
            ]
    return {
        "config": cfg,
        "schema": schema,
        "restart": [],
        "problems": [],
        "broken": None,
        "secrets": {"openrouter.api_key": "vault"},
        "devices": {"input": ["pipewire", "Blue Yeti"], "output": ["pipewire"]},
        "voices": voices,
        "loading": False,
        "stt_models": [
            {"name": "auto", "size": "", "note": "the best this machine runs", "downloaded": True},
            {"name": "large-v3-turbo", "size": "1.6 GB", "note": "fast and accurate", "downloaded": True},
            {"name": "small.en", "size": "470 MB", "note": "English only", "downloaded": False},
        ],
        "claude_models": [
            {"value": "sonnet", "detail": "Fast and capable: the everyday choice"},
            {"value": "opus", "detail": "The deepest thinker, slower, uses your plan faster"},
            {"value": "haiku", "detail": "The quickest, for simple requests"},
        ],
        "paths": {
            "config": "/home/you/.local/share/orion/config.yaml",
            "data": "/home/you/.local/share/orion",
            "memory": "/home/you/.local/share/orion/memory",
        },
    }


GESTURES = {
    "remotes": [
        {
            "name": "hands",
            "status": "connected",
            "enabled": True,
            "error": "",
            "custom": True,
            "gestures": [
                {
                    "name": "open_palm",
                    "title": "Open palm",
                    "kind": "shape",
                    "how": "Hold your hand up, fingers apart.",
                },
                {"name": "thumb_up", "title": "Thumb up", "kind": "shape", "how": "A thumb up, the fist closed."},
                {
                    "name": "swipe_right",
                    "title": "Swipe right",
                    "kind": "motion",
                    "how": "Move an open hand to the right.",
                },
                {
                    "name": "swipe_left",
                    "title": "Swipe left",
                    "kind": "motion",
                    "how": "Move an open hand to the left.",
                },
            ],
            "hold_steps": [1, 2, 3],
            "static_hold": 0.6,
            "two_hands": None,
            "vocabulary_error": "",
            "bindings": {
                "open_palm": {"do": "media", "action": "play_pause"},
                "thumb_up": {"do": "volume", "action": "up", "repeat": True},
                "swipe_right": {"do": "media", "action": "next"},
            },
            "problems": {},
            "words": {"open_palm": "play / pause", "thumb_up": "volume up", "swipe_right": "next track"},
            "awake": 0,
        }
    ],
    "defaults": {},
    "actions": {
        "media": {
            "title": "Music: play, pause, next",
            "group": "Media",
            "repeats": False,
            "params": [
                {
                    "name": "action",
                    "kind": "choice",
                    "choices": [
                        {"value": "play_pause", "label": "Play / pause"},
                        {"value": "next", "label": "Next track"},
                        {"value": "previous", "label": "Previous track"},
                    ],
                    "required": True,
                    "default": "play_pause",
                }
            ],
            "start": {"do": "media", "action": "play_pause"},
        },
        "volume": {
            "title": "The volume",
            "group": "Media",
            "repeats": True,
            "params": [
                {
                    "name": "action",
                    "kind": "choice",
                    "choices": [{"value": "up", "label": "Up"}, {"value": "down", "label": "Down"}],
                    "required": True,
                    "default": "up",
                }
            ],
            "start": {"do": "volume", "action": "up"},
        },
        "new_chat": {
            "title": "A new conversation",
            "group": "Orion",
            "repeats": False,
            "params": [],
            "start": {"do": "new_chat"},
        },
    },
    "parts": [{"value": "comms", "label": "Conversation"}, {"value": "weather", "label": "Weather"}],
    "skills": ["morning-briefing"],
    "tools": [],
    "pause_minutes": 30,
}

NEURAL_NODES = [
    ("you", "Alex", "you", 9),
    ("orion", "Orion", "orion", 7),
    ("fact:1", "Lives in Lisbon", "fact", 3),
    ("fact:2", "His mother is Rosa", "fact", 2),
    ("fact:3", "Plays Minecraft on Lunar Client", "fact", 2),
    ("fact:4", "Likes his coffee black", "fact", 1),
    ("insight:1", "Prefers short answers in the morning", "insight", 2),
    ("concept:family", "family", "concept", 3),
    ("concept:games", "games", "concept", 3),
    ("concept:home", "home", "concept", 3),
    ("day:2026-10-10", "Fri 10 Oct", "day", 3),
    ("day:2026-10-11", "Sun 11 Oct", "day", 2),
    ("note:1", "Ideas for the HUD", "note", 2),
    ("skill:morning-briefing", "morning-briefing", "skill", 2),
    ("unverified:1", "Might travel to Porto in November", "unverified", 1),
]
NEURAL_LINKS = [
    ("fact:1", "you"),
    ("fact:2", "you"),
    ("fact:3", "you"),
    ("fact:4", "you"),
    ("insight:1", "orion"),
    ("fact:2", "concept:family"),
    ("day:2026-10-10", "concept:family"),
    ("fact:3", "concept:games"),
    ("day:2026-10-11", "concept:games"),
    ("fact:1", "concept:home"),
    ("note:1", "concept:home"),
    ("note:1", "you"),
    ("skill:morning-briefing", "orion"),
    ("day:2026-10-10", "orion"),
    ("day:2026-10-11", "orion"),
    ("unverified:1", "you"),
    ("you", "orion"),
]


def rest(method: str, path: str, query: dict, body: dict | None) -> tuple[int, object]:
    """The REST API's answers."""
    if (answer := STATE.handle(method, path, query, body)) is not None:  # connectors, skills, plugins, activity…
        return answer
    if path == "/api/settings" and method == "GET":
        return 200, settings_response()
    if path == "/api/settings" and method == "PUT":
        return 200, {"changed": list((body or {}).keys()), "restart": []}
    if path == "/api/models":
        return 200, {
            "llm": {
                "embed": "potion-retrieval-32M",
                "installed": [
                    {
                        "name": "qwen3.5:9b",
                        "size": 6.6e9,
                        "params": "9B",
                        "quant": "Q4_K_M",
                        "capabilities": ["completion", "tools", "thinking"],
                        "loaded": False,
                    }
                ],
                "error": "",
                "brain_error": "",
                "ollama": {"installed": True, "running": True},
            },
            "stt": {"model": "large-v3-turbo", "device": "cuda", "loaded": True},
            "gpu": {"name": "NVIDIA GeForce RTX 4070", "util": 31, "mem_used": 6150, "mem_total": 12282, "temp": 61},
        }
    if path == "/api/claude":
        return 200, {"installed": True, "logged_in": True, "plan": "max", "method": "claude.ai"}
    if path == "/api/gestures":
        return 200, GESTURES
    if path == "/api/pairing":
        return 200, {
            "local": True,
            "paired": True,
            "open": True,
            "tls": False,
            "url": "http://192.168.1.20:8765",
            "devices": [{"id": "d1", "name": "Alex's iPhone", "paired": ago(20 * 86400), "seen": ago(900)}],
            "code_ends": None,
        }
    if path == "/api/neural":
        return 200, {
            "nodes": [{"id": i, "label": name, "kind": k, "degree": d} for i, name, k, d in NEURAL_NODES],
            "links": [{"source": a, "target": b} for a, b in NEURAL_LINKS],
            "counts": {"concept": 3, "fact": 4, "insight": 1, "day": 2, "note": 1, "skill": 1, "unverified": 1},
        }
    if path.startswith("/api/neural/node/"):
        node = unquote(path.removeprefix("/api/neural/node/"))
        label = next((name for i, name, _, _ in NEURAL_NODES if i == node), node)
        return 200, {
            "id": node,
            "label": label,
            "kind": node.split(":")[0],
            "text": label,
            "learned_at": ago(3 * 86400),
            "source": "brain",
        }
    if path == "/api/jobs":
        return 200, jobs()
    if path == "/api/jobs/explain":
        schedule = str((body or {}).get("schedule", "")).strip()
        now = time.time()
        known = {
            "30 7 * * *": ("At 07:30", next_clock(7, 30)),
            "30 7 * * 1-5": ("At 07:30 on Monday through Friday", next_clock(7, 30, weekdays=True)),
            "0 8 * * 1-5": ("At 08:00 on Monday through Friday", next_clock(8, 0, weekdays=True)),
            "0 * * * *": ("At minute 0 of every hour", now - now % 3600 + 3600),
            "0 18 * * 0": ("At 18:00 on Sunday", next_clock(18, 0, on=6)),
        }
        fields = len(schedule.split())
        if fields != 5:
            return 400, {"detail": f"a schedule has five fields (minute hour day month weekday); this one has {fields}"}
        explain, next_run = known.get(schedule, ("At the times it says", now + 3600 * 9))
        return 200, {"schedule": schedule, "explain": explain, "next_run": next_run}
    if path == "/api/watchers":
        s = STATS_HISTORY[-1]
        rules = [
            ("battery_low", "Battery low", 20, "%", False, False),
            ("battery_critical", "Battery very low", 8, "%", False, False),
            ("cpu_hot", "Processor hot", 92, "°C", True, True),
            ("gpu_hot", "Graphics card hot", 87, "°C", True, True),
            ("disk_full", "Disk full", 92, "%", True, True),
            ("memory_full", "Memory full", 95, "%", True, True),
        ]
        return 200, {
            "sensors": {
                "temperatures": {"cpu": s["cpu_temp"], "gpu": s["gpu"]["temp"]},
                "disk": {"percent": s["disk"], "free_gb": s["disk_free"]},
                "memory": {"percent": s["ram"]},
            },
            "rules": [
                {
                    "id": i,
                    "title": t,
                    "enabled": True,
                    "available": a,
                    "limit": lim,
                    "unit": u,
                    "above": above,
                    "detail": f"warns {'above' if above else 'below'} {lim}{u}"
                    if a
                    else "this machine has no such reading",
                }
                for i, t, lim, u, above, a in rules
            ],
            "alerts": [],
        }
    if path == "/api/calendar":
        return 200, {
            "sources": [{"name": "google", "ok": True, "error": ""}, {"name": "ics", "ok": True, "error": ""}],
            "events": calendar_events(),
        }
    if path == "/api/tools/call":
        tool_name = (body or {}).get("tool")
        if tool_name == "get_news":
            return 200, {"ok": True, "result": "- " + news()[0]["title"], "ms": 300}
        if tool_name == "skill_file":
            args = (body or {}).get("args") or {}
            ok, text = STATE.skill_file(args.get("skill", ""), args.get("path", ""))
            return 200, {"ok": ok, "result": text, "ms": 40}
        return 200, {"ok": True, "result": "Done.", "ms": 120}
    return 404, {"detail": "Not Found"}


# ---------------------------------------------------------------------------------------------------- the page
def feed_script(name: str) -> str:
    """A module script that feeds the page a scene's events through window.__orion.receive, timed."""
    events = [[ms, ev] for ms, ev in scene(name)]
    return f"""const events = {json.dumps(events)}
const receive = (ev) => window.__orion.receive(ev)
let n = 0
function stats() {{
  n++
  const t = n / 3
  receive({{type: "stats", stats: {{
    cpu: 20 + 15 * Math.sin(t * 0.7) + 8 * Math.sin(t * 1.9), cpu_temp: 58, ram_used: 14.8, ram_total: 32,
    ram: 46 + 3 * Math.sin(t * 0.2), disk: 71.4, disk_free: 268, battery: null,
    gpu: {{name: "NVIDIA GeForce RTX 4070", util: 35 + 18 * Math.sin(t * 0.45), mem_used: 6150, mem_total: 12282,
      temp: 61}},
    uptime: 36100 + n * 2,
    top: [{{name: "Lunar Client", cpu: 21.4, mem: 3.2}}, {{name: "Firefox", cpu: 6.1, mem: 2.4}},
      {{name: "Orion", cpu: 3.8, mem: 1.6}}]}}}})
}}
// the app sets its hook as it starts; the scene starts once it is there, and his stats tick every two seconds
function start() {{
  if (!window.__orion?.receive) return void setTimeout(start, 20)
  for (const [ms, ev] of events) setTimeout(() => receive(ev), ms)
  setInterval(stats, 2000)
}}
start()
"""


# A scene page's WebSocket: open at once, quiet, never closed (a classic script, so it runs before the app's module).
QUIET_SOCKET = """const scene = new URLSearchParams(location.search).get("scene")
let made = 0
class QuietSocket extends EventTarget {
  static CONNECTING = 0
  static OPEN = 1
  static CLOSING = 2
  static CLOSED = 3
  constructor(url) {
    super()
    Object.assign(this, { url, readyState: 0, binaryType: "blob", protocol: "", extensions: "", bufferedAmount: 0 })
    const first = made++ === 0
    if (scene === "offline" && !first) {
      setTimeout(() => this.close(1006), 200) // he is gone: every attempt fails
      return
    }
    setTimeout(() => {
      this.readyState = 1
      this.dispatchEvent(new Event("open"))
    }, 0)
    if (scene === "unpaired") setTimeout(() => this.close(4401), 100)
    if (scene === "offline") setTimeout(() => this.close(1006), 1500)
  }
  send() {}
  close(code = 1000, reason = "") {
    this.readyState = 3
    this.dispatchEvent(new CloseEvent("close", { code, reason }))
  }
}
window.WebSocket = QuietSocket
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # a browser takes a WebSocket's 101 only from HTTP/1.1
    dist: Path

    def log_message(self, *args) -> None:  # quiet
        pass

    def _send(self, status: int, body: bytes, kind: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: object) -> None:
        self._send(status, json.dumps(data).encode(), "application/json")

    def _body(self) -> dict | None:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return None
        try:
            return json.loads(self.rfile.read(length))
        except ValueError:
            return None

    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path == "/ws":
            return self._websocket()
        if url.path.startswith("/api/"):
            status, data = rest("GET", url.path, parse_qs(url.query), None)
            return self._json(status, data)
        if url.path == "/mock/feed.js":
            name = parse_qs(url.query).get("scene", ["idle"])[0]
            return self._send(200, feed_script(name).encode(), "text/javascript")
        if url.path == "/mock/socket.js":
            return self._send(200, QUIET_SOCKET.encode(), "text/javascript")
        if url.path == "/oauth/callback":
            return self._send(200, STATE.oauth_done().encode(), "text/html; charset=utf-8")
        file = (self.dist / url.path.lstrip("/")).resolve()
        if url.path != "/" and file.is_file() and self.dist in file.parents:
            kind = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
            return self._send(200, file.read_bytes(), kind)
        # the app's own routes: index.html (a scene adds its quiet socket before the app, its feed after it)
        html = (self.dist / "index.html").read_text()
        name = parse_qs(url.query).get("scene", [""])[0]
        if name:
            html = html.replace("</head>", '<script src="/mock/socket.js"></script></head>')
            html = html.replace("</body>", f'<script type="module" src="/mock/feed.js?scene={name}"></script></body>')
        return self._send(200, html.encode(), "text/html; charset=utf-8")

    def do_POST(self) -> None:
        url = urlparse(self.path)
        status, data = rest("POST", url.path, parse_qs(url.query), self._body())
        self._json(status, data)

    def do_PUT(self) -> None:
        url = urlparse(self.path)
        status, data = rest("PUT", url.path, parse_qs(url.query), self._body())
        self._json(status, data)

    def do_PATCH(self) -> None:
        url = urlparse(self.path)
        status, data = rest("PATCH", url.path, parse_qs(url.query), self._body())
        self._json(status, data)

    def do_DELETE(self) -> None:
        url = urlparse(self.path)
        status, data = rest("DELETE", url.path, parse_qs(url.query), self._body())
        self._json(status, data)

    # ---------- a minimal WebSocket: text frames out, frames in read and dropped ----------
    def _websocket(self) -> None:
        key = self.headers.get("Sec-WebSocket-Key", "")
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        sock = self.connection
        lock = threading.Lock()
        alive = threading.Event()
        alive.set()

        def send(event: dict) -> None:
            data = json.dumps(event).encode()
            head = bytes([0x81])
            n = len(data)
            head += (
                bytes([n])
                if n < 126
                else bytes([126]) + struct.pack(">H", n)
                if n < 65536
                else bytes([127]) + struct.pack(">Q", n)
            )
            with lock:
                try:
                    sock.sendall(head + data)
                except OSError:
                    alive.clear()

        def script() -> None:
            start = time.time()
            for ms, ev in scene(LIVE):
                delay = start + ms / 1000 * 3 - time.time()  # the live scene, three times slower than a screenshot's
                if delay > 0:
                    time.sleep(delay)
                if not alive.is_set():
                    return
                send(ev)
            i = 40
            while alive.is_set():
                time.sleep(2)
                i += 1
                send({"type": "stats", "stats": stats_sample(i)})

        threading.Thread(target=script, daemon=True).start()
        STATE.listeners.append(send)
        try:
            while alive.is_set():
                head = self._read(sock, 2)
                if not head:
                    break
                opcode, length = head[0] & 0x0F, head[1] & 0x7F
                if length == 126:
                    length = struct.unpack(">H", self._read(sock, 2))[0]
                elif length == 127:
                    length = struct.unpack(">Q", self._read(sock, 8))[0]
                self._read(sock, 4 + length)  # the mask and the payload, dropped
                if opcode == 0x8:
                    break
        finally:
            alive.clear()
            if send in STATE.listeners:
                STATE.listeners.remove(send)
            self.close_connection = True

    @staticmethod
    def _read(sock: socket.socket, n: int) -> bytes:
        out = b""
        while len(out) < n:
            chunk = sock.recv(n - len(out))
            if not chunk:
                return b""
            out += chunk
        return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dist", type=Path, required=True, help="the built HUD (vite build --outDir …)")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--live", default="conversation", help="the scene a page without ?scene= is played")
    args = parser.parse_args()
    Handler.dist = args.dist.resolve()
    global STATE, LIVE
    LIVE = args.live
    STATE = mock_state.State(args.port)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    scenes = "idle|conversation|cards|today|chips|instant|phone|offline|unpaired"
    print(f"a stand-in Orion on http://127.0.0.1:{args.port}/ (scenes: ?scene={scenes})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
