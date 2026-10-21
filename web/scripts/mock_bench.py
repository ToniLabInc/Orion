"""The stand-in Orion's benchmarks, shaped as orion/bench.py and the REST of orion/server.py answer them:

    POST /api/bench/plan   what a run would do and cost (nothing starts)       -> {suite, cases, dry_run, skipped, brains, paid_requests, text}
    POST /api/bench        start one; 409 {detail, plan} while a brain that costs hasn't been confirmed, 409 {detail} while
                           another runs                                         -> {id}
    POST /api/bench/N/stop stop a run under way (it keeps what it scored)      -> {ok: true}, 404 when it isn't running
    GET  /api/bench        the latest runs, newest first                       -> {items: [BenchRun]}

A run under way advances a step every 0.7 s and says so with a `bench` event (the whole run) to every open WebSocket.
"""

import random
import threading
import time
from collections.abc import Callable
from typing import Any

from mock_data import ago

MAX_ROUNDS = 8  # brain.max_tool_rounds + 2: the most times a brain is asked per request
TICKS = 14  # steps from start to the end of a run
TICK_S = 0.7
WHERE = {"claude": "your Claude plan", "ollama": "this PC", "openrouter": "OpenRouter", "script": "no model"}

# how each provider does, by the id the HUD sends
PROFILES: dict[str, dict[str, Any]] = {
    "claude": {
        "label": "Claude Sonnet 4.6",
        "paid": "plan",
        "tool": 0.975,
        "args": 0.925,
        "forbidden": 0,
        "rounds": 1.4,
        "first": (920, 1600),
        "total": (2310, 4100),
        "errors": 0,
        "cost": 0.0,
        "fail": [
            (
                "Remind me to call mom at six",
                {"tool": "set_reminder", "args": {"when": "18:00"}},
                {"tool": "set_reminder", "args": {"when": "tomorrow 18:00"}},
            )
        ],
    },
    "ollama": {
        "label": "qwen3.5:9b",
        "paid": "",
        "tool": 0.8,
        "args": 0.7,
        "forbidden": 1,
        "rounds": 1.8,
        "first": (410, 780),
        "total": (3920, 7200),
        "errors": 2,
        "cost": 0.0,
        "fail": [
            ("Shut the PC down", {"tool": None}, {"tool": "power", "args": {"action": "shutdown"}}),
            ("What's on my calendar tomorrow?", {"tool": "list_events"}, {"tool": "web_search"}),
            (
                "Turn the volume to 30",
                {"tool": "volume", "args": {"level": 30}},
                {"tool": "volume", "args": {"level": 3}},
            ),
        ],
    },
    "openrouter": {
        "label": "Qwen3 235B",
        "paid": "money",
        "tool": 0.9,
        "args": 0.85,
        "forbidden": 0,
        "rounds": 1.5,
        "first": (880, 1900),
        "total": (2640, 5200),
        "errors": 0,
        "cost": 0.034,
        "fail": [("Search my documents for the tax return", {"tool": "documents_search"}, {"tool": "web_search"})],
    },
    "script": {
        "label": "Scripted brain",
        "paid": "",
        "tool": 1.0,
        "args": 1.0,
        "forbidden": 0,
        "rounds": 1.0,
        "first": (3, 6),
        "total": (12, 30),
        "errors": 0,
        "cost": 0.0,
        "fail": [],
    },
}

# requests of the built-in suite whose tools this Orion doesn't have
SKIPPED = [
    {"say": "Turn off the living room lights", "why": "needs home_assistant_call, which isn't here"},
    {"say": "Is the front door locked?", "why": "needs home_assistant_state, which isn't here"},
]


class Refused(Exception):
    """A request the server answers with an error: its status and its detail (and, for a 409, the plan)."""

    def __init__(self, status: int, detail: str, plan: dict | None = None) -> None:
        super().__init__(detail)
        self.status, self.detail, self.plan = status, detail, plan

    def answer(self) -> tuple[int, dict]:
        return self.status, {"detail": self.detail, **({"plan": self.plan} if self.plan else {})}


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def split(brain: str) -> tuple[str, str]:
    provider, _, model = brain.strip().partition(":")
    return provider, model.strip()


class Bench:
    """The runs, the plan, and the thread that advances a run."""

    def __init__(self, emit: Callable[[dict], None], spent: Callable[[], float], budget: float) -> None:
        self.emit = emit
        self.spent = spent
        self.budget = budget
        self.lock = threading.RLock()
        self.runs = self._history()
        self.next_id = max(r["id"] for r in self.runs) + 1
        self.live: dict[str, Any] | None = None  # {"run": …, "stop": Event}

    # ------------------------------------------------------------------------------------------------ the plan
    def plan(self, brains: list[str], suite: str | None, limit: int | None) -> dict:
        suite = suite or "builtin"
        asked = list(dict.fromkeys(brains or ["claude"]))
        skipped = SKIPPED if suite == "builtin" else []
        cases = 40 if suite == "builtin" else (limit or 25)
        planned = []
        for brain in asked:
            provider, model = split(brain)
            if provider not in PROFILES:
                raise Refused(
                    400,
                    f"There's no brain called {provider or brain!r}: say {', '.join(PROFILES)}, with a model after a "
                    "colon if you like (ollama:qwen3.5:9b).",
                )
            profile = PROFILES[provider]
            paid = "" if provider == "openrouter" and model.endswith(":free") else profile["paid"]
            over = paid == "money" and 0 < self.budget <= self.spent()
            planned.append(
                {
                    "brain": brain.strip(),
                    "label": model or profile["label"],
                    "paid": paid,
                    "where": WHERE[provider],
                    "cloud": provider in ("claude", "openrouter"),
                    "requests": {"typical": round(cases * profile["rounds"]), "most": cases * MAX_ROUNDS},
                    "ready": not over,
                    "why": f"today's OpenRouter budget is spent (${self.spent():.2f} of ${self.budget:.2f})"
                    if over
                    else "",
                }
            )
        paid_ready = [b for b in planned if b["paid"] and b["ready"]]
        return {
            "suite": suite,
            "cases": cases,
            "dry_run": True,
            "skipped": skipped,
            "brains": planned,
            "paid_requests": {
                "typical": sum(b["requests"]["typical"] for b in paid_ready),
                "most": sum(b["requests"]["most"] for b in paid_ready),
            },
        }

    @staticmethod
    def text(plan: dict) -> str:
        history = plan["suite"] == "history"
        suite = "your latest requests" if history else "the built-in suite"
        lines = [f"Benchmark on {suite}: {plan['cases']} requests per brain, a dry run (no tool really runs)."]
        for b in plan["brains"]:
            r = b["requests"]
            cost = {
                "plan": f"on your Claude plan: about {r['typical']} requests, at most {r['most']}",
                "money": f"paid: about {r['typical']} requests, at most {r['most']}",
                "": "free",
            }[b["paid"]]
            sent = f" Your requests are sent to {b['where']}." if history and b["cloud"] else ""
            state = "" if b["ready"] else f" Won't run: {b['why']}."
            lines.append(f"- {b['brain']} ({b['label']}, {b['where']}): {cost}.{sent}{state}")
        if plan["skipped"]:
            why = "; ".join(s["why"] for s in plan["skipped"][:3])
            lines.append(f"Left out: {len(plan['skipped'])} requests whose tools aren't here ({why}).")
        return "\n".join(lines)

    # ------------------------------------------------------------------------------------------------ the REST
    def handle(self, method: str, rest: list[str], body: dict) -> tuple[int, Any]:
        try:
            with self.lock:
                return self._handle(method, rest, body)
        except Refused as e:
            return e.answer()

    def _handle(self, method: str, rest: list[str], body: dict) -> tuple[int, Any]:
        if not rest and method == "GET":
            return 200, {"items": self.runs}
        if rest == ["plan"] and method == "POST":
            plan = self.plan(body.get("brains", []), body.get("suite"), body.get("limit"))
            return 200, {**plan, "text": self.text(plan)}
        if not rest and method == "POST":
            return 200, {"id": self.start(body)}
        if len(rest) == 2 and rest[1] == "stop" and method == "POST":
            if not (self.live and str(self.live["run"]["id"]) == rest[0]):
                raise Refused(404, f"Benchmark #{rest[0]} isn't running.")
            self.live["stop"].set()
            return 200, {"ok": True}
        run = next((r for r in self.runs if str(r["id"]) == (rest[0] if rest else "")), None)
        if run and len(rest) == 1 and method == "GET":
            return 200, run
        raise Refused(404, "Not Found")

    def start(self, body: dict) -> int:
        plan = self.plan(body.get("brains", []), body.get("suite"), body.get("limit"))
        if any(b["paid"] and b["ready"] for b in plan["brains"]) and body.get("confirm") is not True:
            raise Refused(409, f"{self.text(plan)}\nSend confirm: true to start it.", plan)
        if self.live:
            run = self.live["run"]
            raise Refused(409, f"Benchmark #{run['id']} is running: wait for it, or stop it.")
        if not any(b["ready"] for b in plan["brains"]):
            raise Refused(
                400, "No brain can run: " + "; ".join(f"{b['brain']}: {b['why']}" for b in plan["brains"]) + "."
            )
        run = {
            "id": self.next_id,
            "at": time.time(),
            "suite": plan["suite"],
            "brains": [b["brain"] for b in plan["brains"]],
            "status": "running",
            "progress": 0.0,
            "results": [self.result(b, 0, 0) for b in plan["brains"]],
            "plan": plan,
            "error": "",
        }
        self.next_id += 1
        self.runs.insert(0, run)
        stop = threading.Event()
        self.live = {"run": run, "stop": stop}
        self.publish(run)
        threading.Thread(target=self.go, args=(run, stop), daemon=True).start()
        return run["id"]

    # ------------------------------------------------------------------------------------------------ a run under way
    def go(self, run: dict, stop: threading.Event) -> None:
        brains = run["plan"]["brains"]
        cases = run["plan"]["cases"]
        for tick in range(1, TICKS + 1):
            if stop.wait(TICK_S):
                with self.lock:
                    run["status"] = "stopped"
                    self.live = None
                    self.publish(run)
                return
            with self.lock:
                run["progress"] = round(tick / TICKS, 4)
                done = run["progress"] * cases * len(brains)
                run["results"] = [
                    self.result(b, max(0, min(cases, round(done - i * cases))), cases) for i, b in enumerate(brains)
                ]
                if tick == TICKS:
                    run["status"] = "done"
                    self.live = None
                self.publish(run)

    def publish(self, run: dict) -> None:
        self.emit({"type": "bench", "ts": time.time(), **run})

    def result(self, brain: dict, cases: int, suite_cases: int) -> dict:
        """One brain's scores after `cases` of the suite's requests, as orion.bench._Tally.view says them."""
        profile = PROFILES[split(brain["brain"])[0]]
        rng = random.Random(f"{brain['brain']}{cases}")
        errors = round(profile["errors"] * cases / suite_cases) if suite_cases else 0
        answered = cases - errors

        def share(value: float) -> float:
            return round(min(1.0, max(0.0, value + rng.uniform(-0.02, 0.02))), 4) if cases else 0.0

        def ms(pair: tuple[int, int], high: bool) -> float:
            return round(pair[1 if high else 0] * rng.uniform(0.96, 1.04), 1) if answered else 0.0

        done = bool(suite_cases) and cases == suite_cases
        return {
            "brain": brain["brain"],
            "label": brain["label"],
            "paid": brain["paid"],
            "cases": cases,
            "answered": answered,
            "passed": round(cases * profile["args"]),
            "tool_accuracy": share(profile["tool"]),
            "args_accuracy": share(profile["args"]),
            "forbidden": profile["forbidden"] if done else 0,
            "rounds": round(profile["rounds"], 2) if answered else 0.0,
            "p50_first_token_ms": ms(profile["first"], False),
            "p95_first_token_ms": ms(profile["first"], True),
            "p50_total_ms": ms(profile["total"], False),
            "p95_total_ms": ms(profile["total"], True),
            "cost": round(profile["cost"] * cases / suite_cases, 6)
            if suite_cases and brain["paid"] == "money"
            else 0.0,
            "errors": errors,
            "skipped": 0,
            "error": "",
            "failures": [{"say": s, "expected": e, "got": g} for s, e, g in profile["fail"]] if done else [],
        }

    # ------------------------------------------------------------------------------------------------ the past
    def _history(self) -> list[dict]:
        """Two runs from before: a long one that finished, and one of your requests that you stopped."""

        def planned(provider: str, label: str | None = None) -> dict:
            profile = PROFILES[provider]
            return {
                "brain": provider,
                "label": label or profile["label"],
                "paid": profile["paid"],
                "where": WHERE[provider],
                "cloud": provider in ("claude", "openrouter"),
                "requests": {"typical": 56, "most": 320},
                "ready": True,
                "why": "",
            }

        def view(suite: str, cases: int, brains: list[dict], skipped: list[dict]) -> dict:
            paid = [b for b in brains if b["paid"]]
            return {
                "suite": suite,
                "cases": cases,
                "dry_run": True,
                "skipped": skipped,
                "brains": brains,
                "paid_requests": {
                    "typical": sum(b["requests"]["typical"] for b in paid),
                    "most": sum(b["requests"]["most"] for b in paid),
                },
            }

        three = [planned("claude"), planned("ollama"), planned("openrouter")]
        two = [planned("claude"), planned("openrouter")]
        finished = [self.result(b, 40, 40) for b in three]
        stopped = [self.result(two[0], 11, 25), self.result(two[1], 0, 25)]
        return [
            {
                "id": 4,
                "at": ago(70),
                "suite": "history",
                "brains": ["claude", "openrouter"],
                "status": "stopped",
                "progress": 0.22,
                "results": stopped,
                "plan": view("history", 25, two, []),
                "error": "",
            },
            {
                "id": 3,
                "at": ago(5 * 3600),
                "suite": "builtin",
                "brains": ["claude", "ollama", "openrouter"],
                "status": "done",
                "progress": 1.0,
                "results": finished,
                "plan": view("builtin", 40, three, SKIPPED),
                "error": "",
            },
        ]
