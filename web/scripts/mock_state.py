"""The stand-in Orion's state and REST for the newer pages (see mock_data.py for the data it starts from): `State`
holds connectors, skills, plugins, the vault, the channels' limits and the rest in memory, and `State.handle` answers
the requests of section 3.7 of the round's spec, changing that state as the HUD asks. Times are made as they are asked
for, so "6 hours ago" is always six hours ago.
"""

import random
import re
import threading
import time
from typing import Any
from urllib.parse import unquote

import mock_data as data
from mock_bench import Bench
from mock_data import ago

BUDGET = 1.0  # openrouter.daily_budget_usd
CHANNELS = ("voice", "hud", "discord", "job", "app")
CLASSES = ["@read", "@change", "@destroy", "@open_world"]


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(p * len(ordered)))] if ordered else 0


def stage_at(rows: list[dict], p: float) -> dict:
    """Each stage's percentile over some turns."""
    return {
        k: int(percentile([r[k] for r in rows], p))
        for k in ("heard_ms", "think_ms", "tools_ms", "speak_ms", "total_ms")
    }


class State:
    """Everything the newer pages read and change."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.lock = threading.RLock()
        rng = random.Random(11)
        self.connectors = data.make_connectors()
        self.directory = data.make_directory()
        self.skills = data.make_skills()
        self.skill_discover = data.make_skill_discover()
        self.plugins = data.make_plugins()
        self.plugin_discover = data.make_plugin_discover()
        self.marketplaces = data.make_marketplaces()
        self.activity = self._activity(rng)
        self.timings = self._timings(rng)
        self.spend_days = self._spend_days(rng)
        self.secrets = {
            "openrouter.api_key": {"set": True, "used_by": ["openrouter.api_key"]},
            "connector.home-assistant.token": {"set": True, "used_by": ["home-assistant"]},
            "connector.github.token": {"set": False, "used_by": ["github"]},
            "connector.daily-digest.DIGEST_API_KEY": {"set": True, "used_by": ["daily-digest"]},
            "backup.restic.password": {"set": False, "used_by": []},
        }
        self.channels = {
            "voice": {"never": [], "confirm": ["power"]},
            "hud": {"never": [], "confirm": []},
            "discord": {
                "never": ["power", "run_command", "mcp_servers", "set_setting", "switch_brain"],
                "confirm": ["@destroy"],
            },
            "job": {"never": ["power"], "confirm": ["@destroy"]},
            "app": {"never": [], "confirm": []},
        }
        self.listeners: list = []  # a send function for each open WebSocket (mock_orion.py)
        self.bench = Bench(self.emit, lambda: self.spend_days[-1]["cost"], BUDGET)
        self.backups = [
            {"name": "orion-backup-20261011-030000.tar.gz", "at": ago(16 * 3600), "size": 48_300_000, "where": "local"},
            {"name": "orion-backup-20261010-030000.tar.gz", "at": ago(40 * 3600), "size": 48_100_000, "where": "local"},
            {"name": "orion-backup-20261009-030000.tar.gz", "at": ago(64 * 3600), "size": 47_900_000, "where": "local"},
            {"name": "orion-backup-20261008-030000.tar.gz", "at": ago(88 * 3600), "size": 47_850_000, "where": "local"},
            {
                "name": "orion-backup-20261007-030000.tar.gz",
                "at": ago(112 * 3600),
                "size": 47_700_000,
                "where": "local",
            },
        ]
        self.pending_oauth = ""

    # ------------------------------------------------------------------------------------------------ builders
    def _activity(self, rng: random.Random) -> list[dict]:
        tools = [
            ("organizer", "set_timer", "Set a timer", "duration: 10 minutes\nname: pasta"),
            ("web", "get_weather", "Weather and forecast", "location: Lisbon\ndays: 2"),
            ("web", "web_search", "Search the web", "query: vite 8 rolldown options"),
            ("calendar", "list_events", "Read your calendar", "when: today"),
            ("system", "volume", "The volume", "action: set\nlevel: 30"),
            ("system", "media", "Control the music", "action: next"),
            ("system", "system_status", "How the PC is doing", ""),
            ("memory", "memory_save", "Remember something", "text: Plays Minecraft on Lunar Client"),
            ("documents", "documents_search", "Search your documents", "query: tax return 2025\nlimit: 5"),
            ("system", "open_target", "Open an app, a file or a site", "target: youtube.com"),
            ("organizer", "todo", "Your tasks", "action: add\nitem: Renew the car insurance"),
            ("web", "get_news", "The news", "topic: space"),
        ]
        sources = ["voice"] * 9 + ["hud"] * 5 + ["job"] * 2 + ["discord"] * 2 + ["app", "gesture"]
        entries: list[dict] = []
        at = time.time() - 40
        for _ in range(330):
            at -= rng.expovariate(1 / 2300) + 20
            roll = rng.random()
            source = rng.choice(sources)
            e = {
                "at": at,
                "source": source,
                "actor": {
                    "voice": "you",
                    "hud": "you",
                    "gesture": "you",
                    "app": "Claude Code",
                    "discord": "Alex",
                    "job": "Orion",
                    "watcher": "Orion",
                    "user": "you",
                    "system": "Orion",
                }[source],
                "connector": "",
                "tool": "",
                "title": "",
                "detail": "",
                "outcome": "ok",
                "turn": f"t{rng.randrange(900)}",
                "ms": 0,
            }
            if roll < 0.62:
                connector, tool, title, detail = rng.choice(tools)
                ok = rng.random() > 0.05
                e.update(
                    kind="tool",
                    connector=connector,
                    tool=tool,
                    title=title,
                    detail=detail,
                    outcome="ok" if ok else "failed",
                    ms=int(rng.lognormvariate(5.9, 0.9)),
                )
            elif roll < 0.70:
                e.update(
                    kind="ask",
                    connector="system",
                    tool="run_command",
                    title="Asked before running a command",
                    detail="command: rm -rf ~/Downloads/*.part",
                    outcome=rng.choice(["yes", "yes", "no", "timeout"]),
                    actor=rng.choice(["you", "Alex", "you"]),
                )
            elif roll < 0.73:
                e.update(
                    kind="deny",
                    source="discord",
                    connector="system",
                    tool="run_command",
                    actor="Alex",
                    title="Refused: run_command from Discord",
                    detail="Discord may never run commands, even with a yes.",
                    outcome="denied",
                )
            elif roll < 0.74:
                e.update(
                    kind="block",
                    connector="web",
                    tool="read_page",
                    title="Blocked: a secret in a request",
                    detail="The arguments held the value of a secret in the vault: the call was not made.",
                    outcome="blocked",
                )
            elif roll < 0.77:
                e.update(
                    kind="alert",
                    source="watcher",
                    title=rng.choice(["The GPU runs hot", "Disk is 92 % full", "Memory is 95 % used"]),
                    detail="87 °C for a minute",
                    outcome="",
                )
            elif roll < 0.84:
                e.update(
                    kind="job",
                    source="job",
                    connector="jobs",
                    tool="run_job",
                    title=rng.choice(["Morning briefing", "Build watch"]),
                    detail="Ran on schedule",
                    outcome=rng.choice(["ok", "ok", "ok", "failed"]),
                    ms=int(rng.lognormvariate(8.2, 0.5)),
                )
            elif roll < 0.90:
                e.update(
                    kind="instant",
                    connector="organizer",
                    tool="set_timer",
                    title=rng.choice(["Timer: 5 minutes", "Volume to 40", "The time"]),
                    detail="Answered without the brain",
                    outcome="ok",
                    ms=int(rng.uniform(30, 180)),
                )
            elif roll < 0.915:
                e.update(
                    kind="backup",
                    source="system",
                    title="Backup made",
                    detail="orion-backup.tar.gz, 48 MB",
                    outcome="ok",
                    ms=21000,
                )
            elif roll < 0.935:
                e.update(
                    kind="connector",
                    source="hud",
                    connector=rng.choice(["notion", "github", "home-assistant"]),
                    title=rng.choice(["Connector added", "Connector switched off", "Tool switched off"]),
                    outcome="ok",
                )
            elif roll < 0.95:
                e.update(
                    kind="plugin",
                    source="hud",
                    title=rng.choice(["Plugin installed: context7", "Plugin updated: daily-digest"]),
                    outcome="ok",
                )
            elif roll < 0.965:
                e.update(kind="skill", source="voice", title="Skill learned: focus-mode", outcome="ok")
            elif roll < 0.975:
                e.update(
                    kind="secret",
                    source="hud",
                    title="Secret set: connector.github.token",
                    detail="(the value is never recorded)",
                    outcome="ok",
                )
            elif roll < 0.985:
                e.update(kind="login", source="hud", connector="notion", title="Signed in to Notion", outcome="ok")
            elif roll < 0.99:
                e.update(
                    kind="bench",
                    source="hud",
                    title="Benchmark finished",
                    detail="3 brains, 40 requests",
                    outcome="ok",
                    ms=184000,
                )
            else:
                e.update(kind="system", source="system", title="Orion started", outcome="")
            entries.append(e)
        entries.sort(key=lambda x: x["at"])
        for i, e in enumerate(entries, 1):
            e["id"] = i
        return entries[::-1]

    def _timings(self, rng: random.Random) -> list[dict]:
        brains = ["Claude Sonnet 4.6"] * 14 + ["qwen3.5:9b"] * 4 + ["OpenRouter · qwen3-235b"] * 2
        out: list[dict] = []
        for i in range(70):
            brain = rng.choice(brains)
            instant = rng.random() < 0.1
            voice = rng.random() < 0.7
            scale = {"Claude Sonnet 4.6": 1.0, "qwen3.5:9b": 1.9, "OpenRouter · qwen3-235b": 1.3}[brain]
            heard = int(rng.uniform(250, 700)) if voice else 0
            think = 0 if instant else int(rng.lognormvariate(7.2, 0.45) * scale)
            tools = int(rng.lognormvariate(6.3, 0.9)) if rng.random() < 0.6 else 0
            speak = int(rng.uniform(220, 520))
            if instant:
                tools, speak, brain = int(rng.uniform(30, 120)), int(rng.uniform(180, 300)), "instant"
            out.append(
                {
                    "turn": f"t{i}",
                    "at": ago(i * 2900 + 120),
                    "source": "voice" if voice else "hud",
                    "brain": brain,
                    "instant": instant,
                    "heard_ms": heard,
                    "think_ms": think,
                    "tools_ms": tools,
                    "speak_ms": speak,
                    "total_ms": heard + think + tools + speak,
                }
            )
        # the turns of the scripted conversation: a fixed look
        fixed = {"t0": (520, 1480, 1460, 410), "t1": (480, 1210, 90, 380), "t2": (610, 1760, 1210, 450)}
        for turn, (h, th, to, sp) in fixed.items():
            for t in out:
                if t["turn"] == turn:
                    t.update(
                        brain="Claude Sonnet 4.6",
                        instant=False,
                        source="voice",
                        heard_ms=h,
                        think_ms=th,
                        tools_ms=to,
                        speak_ms=sp,
                        total_ms=h + th + to + sp,
                    )
        return out

    def _spend_days(self, rng: random.Random) -> list[dict]:
        days = []
        for i in range(29, -1, -1):
            t = time.localtime(time.time() - i * 86400)
            cost = 0.0 if rng.random() < 0.18 else round(rng.lognormvariate(-2.2, 0.8), 3)
            days.append({"day": time.strftime("%Y-%m-%d", t), "cost": min(cost, 1.4)})
        days[-1]["cost"] = 0.42
        return days

    # ------------------------------------------------------------------------------------------------ helpers
    def find(self, items: list[dict], key: str, value: str) -> dict | None:
        return next((i for i in items if i[key] == value), None)

    def connector_detail(self, c: dict) -> dict:
        mine = [a for a in self.activity if a["connector"] == c["id"]][:30]
        return {**c, "activity": mine}

    def public(self, c: dict) -> dict:
        return {k: v for k, v in c.items() if k != "content"}

    def tools_choices(self) -> list[dict]:
        out = []
        for c in self.connectors:
            for t in c["tools"]:
                out.append({"name": t["name"], "title": t["title"], "connector": c["id"], "risk": t["risk"]})
        return out

    def timings_stats(self) -> list[dict]:
        """Each brain's speeds, the one with the most turns first (the instant answers are a brain, "instant")."""
        out = []
        for brain in {t["brain"] for t in self.timings}:
            rows = [t for t in self.timings if t["brain"] == brain][:200]
            out.append({"brain": brain, "turns": len(rows), "p50": stage_at(rows, 0.5), "p95": stage_at(rows, 0.95)})
        return sorted(out, key=lambda b: -b["turns"])

    def spend(self, days: int) -> dict:
        shown = self.spend_days[-days:]
        by_purpose = [
            {"purpose": "conversation", "cost": 5.12},
            {"purpose": "job", "cost": 1.38},
            {"purpose": "review", "cost": 0.46},
            {"purpose": "bench", "cost": 0.23},
        ]
        return {
            "today": shown[-1]["cost"],
            "budget": BUDGET,
            "currency": "USD",
            "by_day": shown,
            "by_purpose": by_purpose,
            "key": {
                "limit": 20.0,
                "limit_remaining": 14.37,
                "usage_daily": shown[-1]["cost"],
                "limit_reset": "monthly",
            },
        }

    # ------------------------------------------------------------------------------------------------ the REST
    def handle(self, method: str, path: str, query: dict, body: dict | None) -> tuple[int, Any] | None:
        with self.lock:
            return self._handle(method, path, query, body or {})

    def _handle(self, method: str, path: str, query: dict, body: dict) -> tuple[int, Any] | None:
        parts = [unquote(p) for p in path.strip("/").split("/")]
        if len(parts) < 2 or parts[0] != "api":
            return None
        head, rest = parts[1], parts[2:]

        if head == "activity" and method == "GET" and not rest:
            return 200, self.activity_page(query)
        if head == "secrets":
            return self.secrets_api(method, rest, body)
        if head == "safety":
            return self.safety_api(method, rest, body)
        if head == "connectors":
            return self.connectors_api(method, rest, query, body)
        if head == "skills":
            return self.skills_api(method, rest, query, body)
        if head == "plugins":
            return self.plugins_api(method, rest, query, body)
        if head == "timings" and method == "GET":
            limit = int((query.get("limit") or ["50"])[0])
            return 200, {"items": self.timings[:limit], "stats": self.timings_stats()}
        if head == "spend" and method == "GET":
            return 200, self.spend(int((query.get("days") or ["30"])[0]))
        if head == "bench":
            return self.bench.handle(method, rest, body)
        if head == "backups":
            if method == "POST":
                name = time.strftime("orion-backup-%Y%m%d-%H%M%S.tar.gz")
                self.backups.insert(0, {"name": name, "at": time.time(), "size": 48_400_000, "where": "local"})
                return 200, {"name": name}
            return 200, {
                "folder": "/home/you/.local/share/orion/backups",
                "schedule": "0 3 * * *",
                "keep": 7,
                "restic": {"configured": False, "available": True},
                "items": self.backups,
            }
        if head == "documents" and method == "GET":
            return 200, {
                "folders": [
                    {
                        "path": "/home/you/Documents/Notes",
                        "files": 412,
                        "chunks": 5830,
                        "errors": 0,
                        "indexing": False,
                    },
                    {
                        "path": "/home/you/Obsidian/Vault",
                        "files": 1287,
                        "chunks": 19644,
                        "errors": 3,
                        "indexing": True,
                    },
                ],
                "last_error": "Scans.pdf: no text found (an image only).",
            }
        return None

    # ---------- activity
    def activity_page(self, query: dict) -> dict:
        def listed(name: str) -> list[str]:
            return [v for v in (query.get(name) or [""])[0].split(",") if v]

        kinds, sources = listed("kind"), listed("source")
        words = (query.get("q") or [""])[0].lower().split()
        before = int((query.get("before") or ["0"])[0] or 0)
        limit = int((query.get("limit") or ["50"])[0])
        out = []
        for e in self.activity:
            if before and e["id"] >= before:
                continue
            if kinds and e["kind"] not in kinds:
                continue
            if sources and e["source"] not in sources:
                continue
            text = " ".join([e["title"], e["detail"], e["tool"], e["connector"], e["actor"], e["outcome"]]).lower()
            if words and not all(w in text for w in words):
                continue
            out.append(e)
            if len(out) > limit:
                break
        more = len(out) > limit
        items = out[:limit]
        return {"items": items, "next": items[-1]["id"] if more and items else None}

    # ---------- vault, limits
    def secrets_api(self, method: str, rest: list[str], body: dict) -> tuple[int, Any]:
        if not rest and method == "GET":
            items = [{"name": n, "set": s["set"], "used_by": s["used_by"]} for n, s in self.secrets.items()]
            return 200, {"store": "keyring", "ok": True, "error": "", "items": items}
        name = rest[0] if rest else ""
        if method == "PUT" and name in self.secrets and body.get("value"):
            self.secrets[name]["set"] = True
            self._sync_secret(name, True)
            return 200, {"ok": True}
        if method == "DELETE" and name in self.secrets:
            self.secrets[name]["set"] = False
            self._sync_secret(name, False)
            return 200, {"ok": True}
        return 404, {"detail": "No such secret."}

    def _sync_secret(self, name: str, is_set: bool) -> None:
        for c in self.connectors:
            for s in c["secrets"]:
                if s["name"] == name:
                    s["set"] = is_set
            if c["settings"]["kind"] == "fields":
                for f in c["settings"]["fields"]:
                    if f["kind"] == "secret" and name.endswith("." + f["key"]):
                        f["set"] = is_set

    def safety_api(self, method: str, rest: list[str], body: dict) -> tuple[int, Any]:
        if not rest and method == "GET":
            return 200, {
                "channels": self.channels,
                "sandbox": {
                    "available": "bwrap",
                    "commands": True,
                    "network": True,
                    "network_ok": True,
                    "why": "",
                    "hidden": [
                        "~/.ssh",
                        "~/.gnupg",
                        "~/.password-store",
                        "~/.local/share/keyrings",
                        "~/.mozilla",
                        "~/.config/google-chrome",
                        "~/.aws",
                        "~/.kube",
                        "~/.docker",
                        "~/.claude",
                        "~/.local/share/orion",
                    ],
                    "writable": ["~/Downloads", "~/Documents/Orion", "/tmp"],
                },
                "choices": {"tools": self.tools_choices(), "classes": CLASSES},
            }
        if len(rest) == 2 and rest[0] == "channels" and method == "PUT" and rest[1] in CHANNELS:
            self.channels[rest[1]] = {"never": list(body.get("never", [])), "confirm": list(body.get("confirm", []))}
            return 200, self.channels[rest[1]]
        return 404, {"detail": "Not Found"}

    # ---------- connectors
    def connectors_api(self, method: str, rest: list[str], query: dict, body: dict) -> tuple[int, Any]:
        if not rest:
            if method == "GET":
                return 200, {"items": [self.public(c) for c in self.connectors]}
            if method == "POST":
                return self.add_connector(body)
        if rest == ["directory"] and method == "GET":
            text = (query.get("q") or [""])[0].lower().split()
            category = (query.get("category") or [""])[0]
            installed = {c["id"] for c in self.connectors}
            items = []
            for d in self.directory:
                blob = f"{d['title']} {d['description']} {d['category']}".lower()
                if (category and d["category"] != category) or not all(w in blob for w in text):
                    continue
                items.append({**d, "installed": d["id"] in installed})
            return 200, {"items": items}
        c = self.find(self.connectors, "id", rest[0]) if rest else None
        if c is None:
            return 404, {"detail": f"There is no connector called {rest[0] if rest else ''}."}
        tail = rest[1:]
        if not tail:
            if method == "GET":
                return 200, self.connector_detail(c)
            if method == "PATCH":
                if "enabled" in body:
                    c["status"] = "connected" if body["enabled"] else "off"
                    if body["enabled"] and c["auth"] == "oauth" and not c["signed_in"]:
                        c["status"] = "needs_sign_in"
                    if body["enabled"] and c["id"] == "home-assistant":
                        c["status"] = "error"
                if "sandbox" in body:
                    c["sandbox"] = "bwrap" if body["sandbox"] else "none"
                return 200, self.public(c)
            if method == "DELETE" and c["removable"]:
                self.connectors.remove(c)
                return 200, {"ok": True}
        if tail == ["settings"] and method == "PUT":
            return self.save_connector(c, body.get("values", {}))
        if len(tail) == 2 and tail[0] == "tools" and method == "PATCH":
            t = self.find(c["tools"], "name", tail[1])
            if t:
                t["enabled"] = bool(body.get("enabled"))
            return 200, self.public(c)
        if tail == ["connect"] and method == "POST":
            if c["auth"] == "oauth":
                self.pending_oauth = c["id"]
                return 200, {"auth_url": f"http://127.0.0.1:{self.port}/oauth/callback?code=demo&state=demo"}
            c["status"] = "connected"
            c["error"] = ""
            return 200, self.public(c)
        if tail == ["disconnect"] and method == "POST":
            c["signed_in"] = False
            c["status"] = "needs_sign_in"
            return 200, self.public(c)
        return 404, {"detail": "Not Found"}

    def save_connector(self, c: dict, values: dict) -> tuple[int, Any]:
        settings = c["settings"]
        if settings["kind"] != "fields":
            return 400, {"detail": "This connector has no fields to save."}
        for f in settings["fields"]:
            if f["key"] not in values:
                continue
            if f["kind"] == "secret":
                if values[f["key"]]:
                    f["set"] = True
                    for s in c["secrets"]:
                        if s["name"].endswith("." + f["key"]):
                            s["set"] = True
                    if not any(s["name"].endswith("." + f["key"]) for s in c["secrets"]):
                        c["secrets"].append({"name": f"connector.{c['id']}.{f['key']}", "set": True})
            else:
                settings["values"][f["key"]] = values[f["key"]]
        required = [f for f in settings["fields"] if f["required"]]
        done = all((f.get("set") if f["kind"] == "secret" else settings["values"].get(f["key"])) for f in required)
        if c["status"] == "needs_setup" and done:
            c["status"] = "connected"
        return 200, self.public(c)

    def add_connector(self, body: dict) -> tuple[int, Any]:
        if body.get("from") == "directory":
            d = self.find(self.directory, "id", body.get("id", ""))
            if not d:
                return 404, {"detail": "The directory has no such connector."}
            values = body.get("values", {})
            fields = [
                {**f, **({"set": bool(values.get(f["key"]))} if f["kind"] == "secret" else {})} for f in d["fields"]
            ]
            needs_setup = any(f["required"] and not values.get(f["key"]) for f in d["fields"])
            c = data.connector(
                d["id"],
                d["title"],
                d["description"],
                d["icon"],
                kind="catalog",
                trust="reviewed",
                auth=d["auth"],
                status="needs_sign_in" if d["auth"] == "oauth" else "needs_setup" if needs_setup else "connected",
                removable=True,
                tools=[
                    data.tool(f"{d['id']}_search", f"Search {d['title']}", "Find things.", "read", True),
                    data.tool(f"{d['id']}_create", f"Add to {d['title']}", "Create something.", "change", True),
                ],
                settings=data.fields_settings(
                    fields,
                    {k: v for k, v in values.items() if k not in {f["key"] for f in fields if f["kind"] == "secret"}},
                )
                if fields
                else None,
                secrets=[
                    {"name": f"connector.{d['id']}.{f['key']}", "set": bool(values.get(f["key"]))}
                    for f in d["fields"]
                    if f["kind"] == "secret"
                ],
            )
        else:
            name = body.get("name", "")
            if self.find(self.connectors, "id", name):
                return 409, {"detail": f"There is already a connector called {name}."}
            where = body.get("url") or " ".join([body.get("command", ""), *body.get("args", [])])
            c = data.connector(
                name,
                name,
                f"An MCP server you added: {where}",
                "Package",
                kind="custom",
                trust="unreviewed",
                sandbox="bwrap" if body.get("command") else "none",
                removable=True,
                tools=[data.tool("ping", "Ping", "Check that it answers.", "read")],
            )
        self.connectors.append(c)
        return 200, self.public(c)

    # ---------- skills
    def skills_api(self, method: str, rest: list[str], query: dict, body: dict) -> tuple[int, Any]:
        if not rest and method == "GET":
            return 200, {"items": [self.public_skill(s) for s in self.skills]}
        if rest == ["discover"] and method == "GET":
            words = (query.get("q") or [""])[0].lower().split()
            have = {s["name"] for s in self.skills}
            items = [
                {**d, "installed": d["installed"] or d["name"] in have}
                for d in self.skill_discover
                if all(w in f"{d['name']} {d['description']}".lower() for w in words)
            ]
            return 200, {"items": items}
        if rest == ["import"] and method == "POST":
            url = body.get("url", "")
            name = re.sub(r"[^a-z0-9-]+", "-", url.rstrip("/").split("/")[-1].lower()).strip("-") or "imported-skill"
            for d in self.skill_discover:
                if d["url"] == url:
                    d["installed"] = True
            s = {
                "name": name,
                "description": f"Added from {url or 'a file'}.",
                "source": "yours",
                "enabled": True,
                "triggers": [],
                "tools": [],
                "files": ["SKILL.md"],
                "editable": True,
                "content": data.skill_md(name, f"Added from {url or 'a file'}.", "Follow the steps in this skill."),
            }
            self.skills = [x for x in self.skills if x["name"] != name] + [s]
            return 200, [self.public_skill(s)]
        s = self.find(self.skills, "name", rest[0]) if rest else None
        if method == "PUT" and rest:
            content = body.get("content", "")
            if s is None:
                s = {
                    "name": rest[0],
                    "description": "",
                    "source": "yours",
                    "enabled": True,
                    "triggers": [],
                    "tools": [],
                    "files": ["SKILL.md"],
                    "editable": True,
                    "content": content,
                }
                self.skills.append(s)
            m = re.search(r"^description:\s*(.*)$", content, re.MULTILINE)
            s["content"] = content
            if m:
                s["description"] = m.group(1).strip()
            return 200, {"ok": True}
        if s is None:
            return 404, {"detail": f"There is no skill called {rest[0] if rest else ''}."}
        if method == "GET":
            return 200, s
        if method == "PATCH":
            s["enabled"] = bool(body.get("enabled"))
            return 200, self.public_skill(s)
        if method == "DELETE" and s["editable"]:
            self.skills.remove(s)
            return 200, {"ok": True}
        return 404, {"detail": "Not Found"}

    def public_skill(self, s: dict) -> dict:
        return {k: v for k, v in s.items() if k != "content"}

    def skill_file(self, skill: str, path: str) -> tuple[bool, str]:
        s = self.find(self.skills, "name", skill)
        if s is None or path not in s["files"]:
            return False, f"{skill} has no file {path}."
        return True, data.SKILL_FILES.get(path, "(empty)")

    # ---------- plugins
    def plugins_api(self, method: str, rest: list[str], query: dict, body: dict) -> tuple[int, Any]:
        if not rest:
            if method == "GET":
                return 200, {"items": self.plugins}
            if method == "POST":
                name = body.get("name") or "uploaded-plugin"
                listing = self.find(self.plugin_discover, "name", name)
                p = {
                    "name": name,
                    "version": "1.0.0",
                    "description": (listing or {}).get("description", "A plugin you uploaded."),
                    "author": (listing or {}).get("author", "unknown"),
                    "homepage": (listing or {}).get("homepage", ""),
                    "source": {"marketplace": body["marketplace"]} if body.get("marketplace") else {"url": "upload"},
                    "enabled": True,
                    "installed_at": time.time(),
                    "trust": "reviewed" if body.get("marketplace") == "claude-plugins-official" else "unreviewed",
                    "components": {"skills": [name], "commands": [], "connectors": [], "unused": []},
                }
                self.plugins = [x for x in self.plugins if x["name"] != name] + [p]
                return 200, p
        if rest == ["discover"] and method == "GET":
            words = (query.get("q") or [""])[0].lower().split()
            category = (query.get("category") or [""])[0]
            market = (query.get("marketplace") or [""])[0]
            have = {p["name"] for p in self.plugins}
            items = [
                {**d, "installed": d["name"] in have}
                for d in self.plugin_discover
                if (not category or d["category"] == category)
                and (not market or d["marketplace"] == market)
                and all(w in f"{d['name']} {d['description']} {d['category']}".lower() for w in words)
            ]
            return 200, {"items": items, "categories": sorted({d["category"] for d in self.plugin_discover})}
        if rest == ["refresh"] and method == "POST":
            for m in self.marketplaces:
                if not m["error"]:
                    m["updated_at"] = time.time()
            return 200, {"items": self.marketplaces}
        if rest[0] == "marketplaces":
            if len(rest) == 1 and method == "GET":
                return 200, {"items": self.marketplaces}
            if len(rest) == 1 and method == "POST":
                url = body.get("url", "")
                m = {
                    "name": url.rstrip("/").split("/")[-1] or "marketplace",
                    "url": url,
                    "plugins": 12,
                    "updated_at": time.time(),
                    "error": "",
                }
                self.marketplaces.append(m)
                return 200, m
            if len(rest) == 2 and method == "DELETE":
                self.marketplaces = [m for m in self.marketplaces if m["name"] != rest[1]]
                return 200, {"ok": True}
        p = self.find(self.plugins, "name", rest[0])
        if p is None:
            return 404, {"detail": f"There is no plugin called {rest[0]}."}
        if len(rest) == 1 and method == "PATCH":
            p["enabled"] = bool(body.get("enabled"))
            return 200, p
        if len(rest) == 1 and method == "DELETE":
            self.plugins.remove(p)
            return 200, {"ok": True}
        if rest[1:] == ["update"] and method == "POST":
            if p.get("update"):
                p["version"] = p.pop("update")["version"]
            return 200, p
        return 404, {"detail": "Not Found"}

    # ---------- events
    def emit(self, event: dict) -> None:
        """Tell every open WebSocket something, as the server's own events reach the HUD."""
        for send in list(self.listeners):
            send(event)

    def oauth_done(self) -> str:
        """The page a sign-in ends on: the connector that asked is signed in."""
        with self.lock:
            c = self.find(self.connectors, "id", self.pending_oauth)
            if c:
                c["signed_in"] = True
                c["status"] = "connected"
            self.pending_oauth = ""
        return (
            '<!doctype html><meta charset="utf-8"><title>Connected</title><body style="background:#02040b;color:#e8edf3;'
            'font:16px system-ui;display:grid;place-items:center;height:100vh;margin:0"><p>Connected. You can close this tab.</p>'
        )
