"""His brains' costs and benchmarks, as his own tools: what OpenRouter cost against its daily budget
(``orion.spend``), and benchmarks of the brains (``orion.bench``). They join his controls (the core "orion" group,
served inside his process): the HUD's REST endpoints call the same services.

A benchmark spends your plan, your OpenRouter credit and your PC's time, so nothing but the user starts one unasked:
when the model, or another AI app, asks for one, the tool answers at once with the plan (how many requests, where) and
the user is asked, in the HUD, aloud or on Discord, with the plan in the question; the benchmark starts only on their
yes. The user's own call from the HUD starts it directly.
"""

from collections.abc import Sequence
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal

from pydantic import Field

from orion.base import CORE_SERVER
from orion.bench import BenchBusy, Plan, plan_text, results_text
from orion.tools import CallInfo, Group, current_call

if TYPE_CHECKING:
    from orion.runtime import Runtime

PURPOSE_WORDS: Final = {"conversation": "the conversation", "job": "jobs", "review": "the memory review",
                        "bench": "benchmarks"}
ASKED: Final = ("I've asked the user whether to run it, with this plan in the question. It starts only if they say "
                "yes: tell them what it will do, and don't ask again.")


def asker(info: CallInfo) -> str:
    """Who asked for a call, as the activity log says it (``activity.SOURCES``).

    Args:
        info: what the hub said about the call.

    Returns:
        "hud" or "app" for the user at the HUD or another app; for the model, where the request it answers came from.
    """
    if info.provenance in ("hud", "app"):
        return info.provenance
    return info.source or "system"  # the model's: a request from voice, the HUD, Discord or a job


def spend_text(report: dict[str, Any], key: dict[str, Any] | None, *, key_set: bool) -> str:
    """What OpenRouter cost, in words for the model.

    Args:
        report: ``Spend.report``.
        key: the key's own limits (``KeyInfo.as_dict``), None when unknown.
        key_set: whether a key is set at all.

    Returns:
        A few sentences.
    """
    budget = report["budget"]
    lines = [f"OpenRouter today: ${report['today']:.2f}" + (f" of the ${budget:.2f} daily budget" if budget else
                                                             ", with no daily budget")
             + " (the day starts at midnight on this PC)."]
    days = report["by_day"]
    total = sum(day["cost"] for day in days)
    parts = ", ".join(f"{PURPOSE_WORDS[item['purpose']]} ${item['cost']:.2f}" for item in report["by_purpose"])
    lines.append(f"The last {len(days)} days: ${total:.2f}" + (f" ({parts})." if parts else "."))
    if key is not None:
        limit = (f"${key['limit_remaining']:.2f} left of its ${key['limit']:.2f} limit"
                 + (f", which starts again {key['limit_reset']}" if key["limit_reset"] else "")
                 if key["limit"] is not None else "no spending limit")
        lines.append(f"The key: {limit}; OpenRouter counts ${key['usage_daily']:.2f} today (its day is UTC's).")
    else:
        lines.append("The key's own limits couldn't be read." if key_set else "No OpenRouter key is set.")
    return " ".join(lines)


def question_of(plan: Plan) -> str:
    """What the user is asked before a benchmark the model wants: "run a benchmark of claude: about 83 requests on
    your Claude plan" (the question reads "Shall I ...?").

    Args:
        plan: the plan.

    Returns:
        The words after "shall I".
    """
    costs = [f"{brain.brain}: " + {"plan": f"about {brain.typical} requests on your Claude plan",
                                   "money": f"about {brain.typical} paid requests to OpenRouter",
                                   "": "free"}[brain.paid] for brain in plan.brains]
    return f"run a benchmark ({'; '.join(costs)})"


def register(group: Group, rt: "Runtime") -> None:
    """Add the spending and benchmark tools to Orion's own controls.

    Args:
        group: the core group.
        rt: the running Orion (its ``spend``, ``bench`` and ``confirmer``).
    """
    from mcp.server.mcpserver.exceptions import ToolError

    async def start_when_told_yes(plan: Plan, brains: Sequence[str], suite: Literal["builtin", "history"] | None,
                                  limit: int | None, info: CallInfo) -> None:
        """Ask the user about a benchmark a model wants; start it if they say yes, and tell the HUD otherwise."""
        question = rt.confirmer.question(turn=info.turn, tool="benchmark_brains", title=question_of(plan),
                                         server=CORE_SERVER, args={}, reason="always", source="",
                                         source_kind=info.source, device=info.device)
        answer = await rt.confirmer.ask(question)
        if answer != "yes":
            rt.bus.publish("toast", title="BENCHMARK", text="No benchmark: you didn't say yes.")
            return
        try:
            await rt.bench.start(brains, suite, limit, source=asker(info))
        except (BenchBusy, ValueError) as e:
            rt.bus.publish("toast", title="BENCHMARK", text=str(e))

    @group.tool(read_only=True, title="Check what OpenRouter cost",
                examples=("How much has OpenRouter cost today?", "How much credit is left on my OpenRouter key?"))
    async def openrouter_spend(
        days: Annotated[int, Field(ge=1, le=365, description="How many days to sum, today included.")] = 7,
    ) -> str:
        """What the OpenRouter brain has cost: today against its daily budget, the last days by what it was for (the
        conversation, jobs, the memory review, benchmarks), and the key's own limit and what is left of it."""
        report = await rt.spend.report(days)
        key = await rt.spend.key_info()
        return spend_text(report, key.as_dict() if key is not None else None, key_set=bool(rt.cfg.openrouter.key))

    @group.tool(open_world=True, taints=False, title="Benchmark the brains",
                examples=("Benchmark the brains", "Which brain picks tools best?", "Test Ollama on my requests"))
    async def benchmark_brains(
        action: Literal["start", "stop"] = "start",
        brains: Annotated[Sequence[str], Field(description='The brains: claude, ollama, openrouter or script, a model '
                                                           'after a colon if you like ("ollama:qwen3.5:9b", '
                                                           '"claude:haiku"). Empty: the brain he thinks with.')] = (),
        suite: Annotated[Literal["builtin", "history"] | None, Field(
            description="builtin: his own requests over his tools; history: the user's latest requests. Empty: "
                        "the setting's choice.")] = None,
        limit: Annotated[int, Field(ge=0, le=500, description="The most requests per brain (0: the whole suite).")] = 0,
    ) -> str:
        """Benchmark brains in a dry run (no tool really runs): the same requests asked of each, scored on picking
        the right tools with the right arguments, speed and cost; it runs in the background. The user is asked
        first, with the plan (how many requests, and where they are paid). stop ends the one running."""
        bench = rt.bench
        if action == "stop":
            running = bench.running
            if running is None or not bench.stop(running.id):
                return "No benchmark is running."
            return f"Stopping benchmark #{running.id}; what it scored so far is kept."
        info = current_call()
        try:
            plan = await bench.plan(brains, suite, limit or None)
            words = plan_text(plan.view())
            if info.provenance != "hud":  # a model, or another AI app: it never spends by itself
                if bench.running is not None:
                    raise BenchBusy(f"Benchmark #{bench.running.id} is running: wait for it, or stop it.")
                rt.supervisor.spawn(start_when_told_yes(plan, brains, suite, limit or None, info),
                                    name="ask about a benchmark")
                return f"{words}\n{ASKED}"
            run = await bench.start(brains, suite, limit or None, source=asker(info))
        except ValueError as e:  # BenchBusy is one
            raise ToolError(str(e)) from None
        return (f"Started benchmark #{run['id']} in the background (it takes a few minutes; benchmark_results says "
                f"how it went).\n{words}")

    @group.tool(read_only=True, title="Read a benchmark's scores",
                examples=("How did the benchmark go?", "Which brain was fastest?"))
    async def benchmark_results(
        run: Annotated[int, Field(ge=0, description="The run's number (0: the latest).")] = 0,
    ) -> str:
        """The scores of a benchmark of the brains (the latest, or one by number; one still running says how far it
        got): each brain's right tools and arguments, forbidden calls, rounds, speed, cost and its failed requests."""
        bench = rt.bench
        if run:
            found = await bench.run(run)
            if found is None:
                raise ToolError(f"There's no benchmark #{run}.")
            return results_text(found)
        latest = await bench.runs()
        return results_text(latest[0]) if latest else "No benchmark has run yet."
