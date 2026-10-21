"""mcp: the hub's pure helpers, the operation trace, calls through the hub to a server inside the test, and the
intents a server pushes: the feed, the vocabulary, what each intent does, and following a hands server in both
protocol eras (no subprocess, no socket)."""

import asyncio
import contextlib
import json
import time
from types import SimpleNamespace

import numpy as np
import pytest

from orion.base import CallContext, GroupInfo, Tool, ToolRequest
from orion.config import default_config
from orion.mcp import (
    MCPHub,
    OpTrace,
    describe_error,
    preview_args,
    provenance,
    result_text,
    shown_name,
    summary_of,
    timeout_for,
    untitled,
    wrap_schema,
)
from orion.tools import CallInfo, Group, build_server, current_call, publish


def tool(remote: str, *, builtin: bool = True, read_only: bool = False) -> Tool:
    """A tool of Orion's own (or an external one)."""
    return Tool(remote, "", {}, "system" if builtin else "fetch", remote, hints={"read_only": read_only},
                builtin=builtin)


def test_schemas_names_and_summaries() -> None:
    """Titles go at every level but a property called title stays; names and first sentences read right."""
    schema = {"title": "Args", "type": "object", "properties": {
        "title": {"title": "Title", "type": "string"},
        "when": {"anyOf": [{"title": "A", "type": "string"}, {"type": "null"}]},
        "tags": {"type": "array", "items": {"title": "Tag", "type": "string"}}},
        "$defs": {"Spot": {"title": "Spot", "type": "object"}}}
    assert untitled(schema) == {"type": "object", "properties": {
        "title": {"type": "string"}, "when": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "tags": {"type": "array", "items": {"type": "string"}}}, "$defs": {"Spot": {"type": "object"}}}
    assert wrap_schema(None) == wrap_schema({"type": "string"}) == {"type": "object", "properties": {}}
    assert shown_name("system", "open", builtin=True) == "open"
    assert shown_name("Home Assistant!", "turn_on", builtin=False) == "home_assistant__turn_on"
    assert len(shown_name("x" * 50, "y" * 50, builtin=False)) == 64
    assert summary_of("Opens apps, e.g. Firefox. More.") == "Opens apps, e.g. Firefox."
    assert summary_of("Plays music by J. S. Bach. Then stops.") == "Plays music by J. S. Bach."


def test_results_errors_and_argument_previews() -> None:
    """Results read as one text; errors are worded and capped; previews are short and hide secrets."""

    def result(*texts: str, structured: object = None, error: bool = False, image: bool = False) -> SimpleNamespace:
        """An MCP CallToolResult stand-in."""
        content = [SimpleNamespace(type="text", text=t) for t in texts] + [SimpleNamespace(type="image")] * image
        return SimpleNamespace(content=content, structured_content=structured, is_error=error)

    assert result_text(result("21°C", "sunny")) == "21°C\nsunny"
    assert result_text(result(image=True)) == "[the tool returned a picture]"
    assert result_text(result(structured={"temp": 21})) == '{"temp": 21}' and result_text(result()) == "(no output)"
    assert result_text(result("bad city", error=True)) == "Error: bad city"
    group = ExceptionGroup("tasks", [ValueError("bad url"), FileNotFoundError(2, "No such file", "uvx")])
    assert describe_error(group) == "bad url; command not found (uvx)"
    assert len(describe_error(ValueError("x" * 999))) == 300
    preview = preview_args({"query": "w" * 1000, "api_key": "sk-1", "filters": {"a": [1, 2]}, "n": 3,
                            **{f"k{i}": i for i in range(10)}})
    assert len(preview) == 8 and len(preview["query"]) == 160 and preview["query"].endswith("…")
    assert (preview["api_key"], preview["filters"], preview["n"]) == ("(hidden)", '{"a": [1, 2]}', 3)
    assert preview_args({"keywords": "x"}) == {"keywords": "x"}  # not a secret, though it starts with "key"


def test_time_limits_and_provenance() -> None:
    """run_command gets its own limit plus 5 s; own read-only tools 20 s; external servers their own setting."""
    cfg = default_config()
    limit = {"system": cfg.system, "mcp": cfg.mcp}
    assert timeout_for(tool("run_command"), {"timeout": 120}, server_timeout_s=None, **limit) == 125
    assert timeout_for(tool("run_command"), {"timeout": 9999}, server_timeout_s=None, **limit) == 305
    assert timeout_for(tool("system_status", read_only=True), {}, server_timeout_s=None, **limit) == 20
    assert timeout_for(tool("open"), {}, server_timeout_s=None, **limit) == 60
    assert timeout_for(tool("fetch", builtin=False), {}, server_timeout_s=45, **limit) == 45
    assert timeout_for(tool("fetch", builtin=False), {}, server_timeout_s=None, **limit) == cfg.mcp.default_timeout_s
    assert (provenance("brain", tainted=True), provenance("brain", tainted=False)) == ("tainted", "brain")
    assert (provenance("app", tainted=True), provenance("hud", tainted=True)) == ("app", "hud")


def test_the_trace_publishes_and_records_each_operation() -> None:
    """Start and end events share an id; a memory change becomes a "memory" step; turns keep their steps."""
    events: list[tuple[str, dict[str, object]]] = []
    records: list[dict[str, object]] = []
    trace = OpTrace(lambda kind, **data: events.append((kind, data)), SimpleNamespace(tool_result_chars=20), recent=2,
                    on_record=records.append)
    op = trace.start(server="memory", name="memory", title="Remember", ctx=CallContext("brain", "t1"),
                     args={"text": "Likes tea."}, read_only=False)
    op.memory = {"id": "3fa2", "text": "Likes tea.", "profile": True}
    saved = trace.end(op, ok=True, result="Saved to USER.md as [#3fa2]. " * 3)
    failed = trace.record(server="orion", name="nope", title="nope",
                          ctx=CallContext("app", client="Claude Desktop"), args={}, ok=False,
                          result="Error: there is no tool called nope.\nmore")
    assert [data["phase"] for _, data in events] == ["start", "end", "start", "end"]
    assert events[0][1]["id"] == events[1][1]["id"] == saved["id"] and events[0][1]["turn"] == "t1"
    assert (saved["kind"], saved["title"], saved["args"]) == ("memory", "Memory saved", {"text": "Likes tea."})
    assert len(saved["result"]) == 20 and events[3][1]["error"] == "there is no tool called nope."
    assert (failed["source"], failed["client"], failed["turn"]) == ("app", "Claude Desktop", "")
    assert records == [saved, failed] and trace.turn_steps("t1") == [saved] and trace.recent(5) == [failed, saved]
    trace.end_turn("t1")
    assert trace.turn_steps("t1") == [] and trace.count == 2


def test_calls_through_the_hub_carry_who_asks_and_bring_back_events() -> None:
    """The hub lists a server's tools, sends provenance and query vector, taints the turn after a web tool, stamps
    the events a tool publishes, and traces every call, unknown names included."""
    group = Group(GroupInfo("demo", "Demo", "A demo group."))
    seen: list[CallInfo] = []

    @group.tool(title="Search the web", read_only=True, open_world=True, embed_arg="query", examples=("look it up",))
    def web_search(query: str) -> str:
        """Search the web."""
        seen.append(current_call())
        return f"found {query}"

    @group.tool(title="Remember")
    def remember(text: str) -> str:
        """Save a fact."""
        seen.append(current_call())
        publish("memory_update", id="3fa2", text=text, profile=True, ok=True)
        return "Saved."

    cfg = default_config()
    events: list[dict[str, object]] = []
    hub = MCPHub(cfg, {}, ops=OpTrace(lambda kind, **data: None, cfg.hud, recent=10), core=build_server([group]),
                 core_groups={"demo": group.info}, on_event=events.append, embed=lambda text: np.ones(4))

    async def run() -> tuple[object, ...]:
        """Connect, make three calls, read the turn's steps, stop."""
        await hub.configure(wait_s=5)
        ask = CallContext("brain", "t1")
        found = await hub.call_named(ToolRequest("web_search", {"query": "weather"}), ask)
        saved = await hub.call_named(ToolRequest("remember", {"text": "Likes tea."}), ask)
        missing = await hub.call_named(ToolRequest("nope", {}), ask)
        tools, steps = dict(hub.tools()), hub.ops.turn_steps("t1")
        await hub.stop()
        return tools, found, saved, missing, steps

    tools, found, saved, missing, steps = asyncio.run(run())
    search = tools["web_search"]
    assert set(tools) == {"web_search", "remember"} and search.examples == ("look it up",)
    assert search.taints and search.read_only and search.embed_arg == "query" and search.builtin
    assert (found, saved, missing) == ("found weather", "Saved.", "Error: there is no tool called nope.")
    assert [info.provenance for info in seen] == ["brain", "tainted"]
    assert seen[0].qvec is not None and abs(float(np.linalg.norm(seen[0].qvec)) - 1) < 1e-3 and seen[1].qvec is None
    assert [(s["kind"], s["name"], s["ok"]) for s in steps] == [
        ("tool", "web_search", True), ("memory", "remember", True), ("tool", "nope", False)]
    assert steps[1]["title"] == "Memory saved" and steps[0]["taints"]
    assert events == [{"type": "memory_update", "id": "3fa2", "text": "Likes tea.", "profile": True, "ok": True,
                       "call": steps[1]["id"], "turn": "t1"}]


def test_a_tool_of_orions_can_say_its_answer_is_someone_elses_text() -> None:
    """A skill from a plugin taints its turn through a ``taint`` event: the turn's later calls carry "tainted", the
    step says so, and the HUD never sees the event."""
    group = Group(GroupInfo("demo", "Demo", "A demo group."))
    seen: list[CallInfo] = []

    @group.tool(title="Run a skill", read_only=True)
    def run_skill() -> str:
        """Run a skill."""
        seen.append(current_call())
        publish("taint", source="The triage skill")
        return "Steps."

    @group.tool(title="Check")
    def check() -> str:
        """Check."""
        seen.append(current_call())
        return "ok"

    cfg = default_config()
    events: list[dict[str, object]] = []
    hub = MCPHub(cfg, {}, ops=OpTrace(lambda kind, **data: None, cfg.hud, recent=10), core=build_server([group]),
                 core_groups={"demo": group.info}, on_event=events.append)

    async def run() -> list[object]:
        """Call the skill, then another tool in the same turn, and another turn."""
        await hub.configure(wait_s=5)
        await hub.call_named(ToolRequest("run_skill", {}), CallContext("brain", "t1"))
        await hub.call_named(ToolRequest("check", {}), CallContext("brain", "t1"))
        await hub.call_named(ToolRequest("check", {}), CallContext("brain", "t2"))
        out = [hub.is_tainted("t1"), hub.is_tainted("t2"), hub.ops.turn_steps("t1")]
        await hub.stop()
        return out

    first, second, steps = asyncio.run(run())
    assert (first, second) == (True, False) and [info.provenance for info in seen] == ["brain", "tainted", "brain"]
    assert steps[0]["taints"] and events == []


def test_the_top_headlines_reach_the_news_panel_from_the_cache_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_news without a topic sends its headlines to the HUD through the hub, also when it answers from memory;
    with a topic it sends nothing."""
    from orion.tools import web

    cfg = default_config()
    searches: list[str] = []

    def search(kind: str, query: str, count: int, backend: str) -> list[dict[str, object]]:
        """Two headlines, counting the searches."""
        searches.append(query)
        return [{"title": f"{query} {n}", "source": "BBC", "date": "2026-10-09T08:00", "url": f"https://x/{n}",
                 "body": "..."} for n in (1, 2)]

    monkeypatch.setattr(web, "_search", search)
    monkeypatch.setattr(web, "_shared", web._Shared())
    monkeypatch.setattr(web, "ctx", SimpleNamespace(config=cfg))
    events: list[dict[str, object]] = []
    hub = MCPHub(cfg, {}, ops=OpTrace(lambda kind, **data: None, cfg.hud, recent=10), core=build_server([web.group]),
                 core_groups={"web": web.group.info}, on_event=events.append)

    async def run() -> list[str]:
        """Ask for the headlines twice, then for a topic."""
        await hub.configure(wait_s=5)
        ask = CallContext("brain", "t1")
        texts = [await hub.call("web", "get_news", args, ask) for args in ({}, {}, {"topic": "F1"})]
        await hub.stop()
        return texts

    first, second, topic = asyncio.run(run())
    assert first == second and "F1 1" in topic and searches == [cfg.web.news_default_query, "F1"]
    assert [event["type"] for event in events] == ["news", "news"]
    assert events[0]["items"] == events[1]["items"] and len(events[0]["items"]) == 2  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------------------------- intents
def test_the_intents_feed_freshness_and_vocabulary() -> None:
    """Entries need an integer id and an intent; an intent runs when its id is above the last handled and fresh by
    its id (this PC's milliseconds); a server asks for an action itself as a binding names it, never for a tool."""
    from orion.gestures import Binding
    from orion.intents import Intent, due, parse_feed, pushed

    now = 1_790_000_000_000
    feed = parse_feed(json.dumps({"intents": [
        {"id": now - 60_000, "intent": "stop"}, {"id": now - 500, "intent": "media", "args": {"action": "next"},
                                                  "gesture": "swipe_right", "hand": "Right", "score": 0.9, "ts": 1.5},
        {"id": "7", "intent": "stop"}, {"id": now - 200, "intent": None}, "junk", {"id": now - 100, "intent": "hide",
                                                                                   "args": None, "gesture": None}]}))
    assert [(i.id, i.name, i.gesture, i.hand) for i in feed.intents] == [
        (now - 60_000, "stop", "", ""), (now - 500, "media", "swipe_right", "Right"), (now - 100, "hide", "", "")]
    assert parse_feed("not json").intents == parse_feed('{"intents": 3}').intents == []
    assert (feed.instance, feed.now) == ("", None)  # a feed that doesn't say: its ids are this PC's clock
    ready, last = due(feed, 0, now)  # the first read after subscribing: only what is fresh runs
    assert [i.id for i in ready] == [now - 500, now - 100] and last == now - 100
    assert due(feed, last, now) == ([], last)  # nothing twice, however often it is read
    assert due(feed, 0, now + 60_000) == ([], now - 100)  # ten seconds later all of it is stale
    own = parse_feed(json.dumps({"intents": [{"id": 5_000, "intent": "stop"}], "instance": "a1", "now": 5_400}))
    assert (own.instance, own.now) == ("a1", 5_400) and [i.id for i in due(own, 0, now)[0]] == [5_000]  # its clock
    ok = {"show": {"panels": ["weather", "Weather", "news"]}, "hide": {}, "toggle": {"panel": "comms"},
          "dismiss": {}, "media": {"action": "play_pause"}, "volume": {"action": "mute"}, "mic": {"action": "mute"},
          "spoken": {"action": "off"}, "new_chat": {}, "say": {"text": "  what time is it?  "}}
    checked = {name: pushed(Intent(1, name, args)) for name, args in ok.items()}
    assert checked["show"] == Binding("show", {"panels": ("weather", "news")})
    assert checked["hide"] == Binding("hide", {"panels": ()}) and checked["say"] == Binding(
        "say", {"text": "what time is it?"})
    assert all(isinstance(value, Binding) for value in checked.values())
    bad = [("listen", {}), ("stop", {}), ("run_command", {"command": "rm -rf ~"}), ("power", {}),
           ("tool", {"server": "system", "tool": "run_command", "args": {"command": "rm -rf ~"}}),
           ("skill", {"name": "morning-briefing"}), ("timer", {"minutes": 5}), ("arm", {"seconds": 10}),
           ("media", {"action": "shutdown"}), ("volume", {"action": "up", "level": 100}),
           ("media", {"action": "next", "hold": 1}), ("show", {"panels": ["desktop"]}), ("show", {"panels": []}),
           ("show", {"panels": "weather"}), ("new_chat", {"now": True}), ("say", {"text": "  "}),
           ("say", {"text": 3})]
    assert all(isinstance(pushed(Intent(1, name, args)), str) for name, args in bad)


class Voice:
    """A stand-in voice session for intents: a microphone, unmuted."""

    def __init__(self) -> None:
        """A microphone, unmuted."""
        self.listener = object()
        self.muted = False


class Turns:
    """A stand-in turn runner for intents: spoken replies on, nothing asked."""

    def __init__(self) -> None:
        """Nothing asked yet."""
        self.quiet = False
        self.did: list[tuple[str, ...]] = []

    def submit(self, text: str, source: str) -> None:
        """A request, as typed."""
        self.did.append(("submit", text, source))


class Host:
    """The running Orion as the intent runner sees it, noting everything it is asked to do."""

    def __init__(self) -> None:
        """Nothing done yet."""
        self.cfg = default_config()
        self.events: list[tuple[str, dict[str, object]]] = []
        self.bus = SimpleNamespace(publish=lambda kind, **data: self.events.append((kind, data)))
        self.calls: list[tuple[object, ...]] = []
        self.records: list[dict[str, object]] = []
        self.hub = SimpleNamespace(call=self._call, ops=SimpleNamespace(record=lambda **kw: self.records.append(kw)))
        self.voice, self.turns = Voice(), Turns()

    async def _call(self, server: str, tool: str, args: dict[str, object], ctx: CallContext) -> str:
        """A tool call through the hub."""
        self.calls.append((server, tool, args, ctx.origin, ctx.client))
        return "Volume is 50%."


def test_each_intent_does_what_it_says_and_nothing_else() -> None:
    """Each action a server asks for runs its one MCP tool through the hub, as a call of the "hands" app (his own for
    the screen, the microphone, spoken replies and a new conversation; the system's for the media and the volume),
    and is announced to the HUD; asking him something goes in as typed; unknown or invalid ones do nothing."""
    from orion.intents import Intent, IntentRunner

    host = Host()
    runner = IntentRunner(host)  # type: ignore[arg-type]
    pushed = [("show", {"panels": ["weather", "timers"]}, "hands_apart"), ("hide", {"panels": []}, "hands_together"),
              ("toggle", {"panel": "comms"}, ""), ("dismiss", {}, "push"), ("mic", {"action": "mute"}, ""),
              ("spoken", {"action": "on"}, ""), ("media", {"action": "next"}, "swipe_right"),
              ("volume", {"action": "up"}, "thumb_up"), ("new_chat", {}, ""), ("say", {"text": " what time is it "},
                                                                              "pinch")]

    async def run_all(entries: list[tuple[str, dict[str, object], str]]) -> None:
        """Run the entries in order."""
        for n, (name, args, gesture) in enumerate(entries):
            await runner.run("hands", Intent(n, name, args, gesture, hand="Right"))

    asyncio.run(run_all(pushed))
    assert [call[:3] for call in host.calls] == [
        ("orion", "hud_show", {"parts": ["weather", "timers"]}), ("orion", "hud_hide", {"parts": []}),
        ("orion", "hud_show", {"parts": ["comms"], "toggle": True}), ("orion", "hud_hide", {"parts": [], "newest": True}),
        ("orion", "microphone", {"muted": True}), ("orion", "spoken_replies", {"on": True}),
        ("system", "media", {"action": "next"}), ("system", "volume", {"action": "up"}),
        ("orion", "new_conversation", {})]
    assert {call[3:] for call in host.calls} == {("app", "hands")}
    assert host.turns.did == [("submit", "what time is it", "hud")]
    shown = [data for kind, data in host.events if kind == "intent"]
    assert [d["intent"] for d in shown] == [name for name, _, _ in pushed] and all(d["ok"] for d in shown)
    assert shown[6]["text"] == "Swipe right → next track" and shown[1]["text"] == "Hands together → clear the screen"
    assert shown[4]["text"] == "Microphone off"
    assert (shown[0]["source"], shown[0]["hand"], shown[0]["gesture"]) == ("hands", "Right", "hands_apart")
    assert len(host.records) == 1 and host.records[0]["name"] == "say"  # the tool calls trace themselves
    assert host.records[0]["ctx"] == CallContext("app", client="hands") and host.records[0]["args"] == {
        "text": "what time is it", "gesture": "pinch"}
    before = (list(host.events), list(host.calls), list(host.records), list(host.turns.did))
    asyncio.run(run_all([("run_command", {"command": "rm -rf ~"}, ""), ("media", {"action": "shutdown"}, ""),
                         ("show", {"panels": ["desktop"]}, ""), ("stop", {}, ""), ("listen", {}, ""),
                         ("say", {}, ""), ("volume", {"action": "up", "level": 100}, ""),
                         ("tool", {"server": "system", "tool": "power", "args": {"action": "off"}}, "")]))
    assert (host.events, host.calls, host.records, host.turns.did) == before


def test_the_mute_gesture_toggles_the_sound() -> None:
    """volume {action: mute} mutes, and unmutes when the sound is already off: the same gesture undoes itself."""
    from orion.intents import Intent, IntentRunner

    host = Host()
    muted = {"now": False}

    async def call(server: str, tool: str, args: dict[str, object], ctx: CallContext) -> str:
        host.calls.append((tool, args["action"]))
        if args["action"] in ("mute", "unmute"):
            muted["now"] = args["action"] == "mute"
        return f"Volume is 50%{' (muted)' if muted['now'] else ''}."

    host.hub.call = call
    runner = IntentRunner(host)  # type: ignore[arg-type]
    asyncio.run(runner.run("hands", Intent(1, "volume", {"action": "mute"}, "thumb_down")))
    asyncio.run(runner.run("hands", Intent(2, "volume", {"action": "mute"}, "thumb_down")))
    assert host.calls == [("volume", "get"), ("volume", "mute"), ("volume", "get"), ("volume", "unmute")]
    texts = [data["text"] for kind, data in host.events if kind == "intent"]
    assert texts == ["Thumb down → sound off / on"] * 2 and not muted["now"]


def gesture(n: int, name: str, *, kind: str = "shape", phase: str = "start", held: float = 0.3,
            showing: int | None = None) -> object:
    """A gesture as the hands tracker reports it."""
    from orion.intents import Intent

    args: dict[str, object] = {"kind": kind, "phase": phase, "held": held}
    if kind == "shape":
        args["showing"] = showing or n
    return Intent(n, "gesture", args, name, hand="Right")


def run_gestures(host: Host, entries: list[object], server: str = "hands") -> object:
    """Run gestures in order through a fresh intent runner; returns the runner."""
    from orion.intents import IntentRunner

    runner = IntentRunner(host)  # type: ignore[arg-type]

    async def run() -> None:
        """In order, as the hub would."""
        for entry in entries:
            await runner.run(server, entry)  # type: ignore[arg-type]

    asyncio.run(run())
    return runner


def test_gestures_do_what_they_are_bound_to() -> None:
    """With no gestures of its own, a remote's gestures do the defaults: a motion at once, a held shape once held as
    long as its binding asks (and again at each step when it repeats); a gesture bound to nothing does nothing."""
    host = Host()
    host.cfg.mcp_servers = {"hands": {"command": "orion-hands", "intents": True}}
    run_gestures(host, [
        gesture(1, "swipe_right", kind="motion", held=0),
        gesture(2, "thumb_up"), gesture(3, "thumb_up", phase="hold", held=0.6, showing=2),
        gesture(4, "thumb_up", phase="hold", held=1.0, showing=2),
        gesture(5, "closed_fist"), gesture(6, "closed_fist", phase="hold", held=0.6, showing=5),
        gesture(7, "closed_fist", phase="hold", held=1.0, showing=5),
        gesture(8, "closed_fist", phase="hold", held=1.5, showing=5),
        gesture(9, "pointing_up"), gesture(10, "pinch"), gesture(11, "hands_together", kind="two_hands", held=0),
    ])
    assert [call[:3] for call in host.calls] == [
        ("system", "media", {"action": "next"}), *[("system", "volume", {"action": "up"})] * 3,
        ("system", "volume", {"action": "get"}), ("system", "volume", {"action": "mute"}),
        ("orion", "hud_show", {"parts": ["comms"], "toggle": True}), ("orion", "hud_hide", {"parts": []})]
    assert {call[3:] for call in host.calls} == {("app", "hands")}
    shown = [data for kind, data in host.events if kind == "intent"]
    assert [d["text"] for d in shown] == ["Swipe right → next track", *["Thumb up → volume up"] * 3,
                                          "Closed fist → sound off / on", "Pointing up → COMMS on / off",
                                          "Hands together → clear the screen"]
    assert all(d["ok"] and d["source"] == "hands" and d["hand"] == "Right" for d in shown)
    assert [(d["again"], d["showing"]) for d in shown[1:4]] == [(False, 2), (True, 2), (True, 2)]  # one showing
    assert host.records == []  # every one was a tool call, which traces itself


def test_a_users_own_bindings_replace_the_defaults_and_reach_any_tool() -> None:
    host = Host()
    host.cfg.mcp_servers = {"hands": {"command": "orion-hands", "intents": True, "gestures": {
        "circle_cw": {"do": "tool", "server": "spotify", "tool": "play", "args": {"uri": "spotify:x"}},
        "pinch": {"do": "timer", "minutes": 5}, "victory": {"do": "fly"}}}}
    run_gestures(host, [gesture(1, "circle_cw", kind="motion", held=0), gesture(2, "pinch"),
                        gesture(3, "victory"), gesture(4, "swipe_right", kind="motion", held=0)])
    assert [call[:3] for call in host.calls] == [("spotify", "play", {"uri": "spotify:x"}),
                                                 ("organizer", "set_timer", {"duration": "5 minutes"})]
    shown = [data["text"] for kind, data in host.events if kind == "intent"]
    assert shown == ["Circle cw → spotify.play (uri: spotify:x)", "Pinch → 5-minute timer"]  # nothing for the rest


def test_orions_own_actions() -> None:
    host = Host()
    host.cfg.mcp_servers = {"hands": {"command": "orion-hands", "intents": True, "gestures": {
        "call_me": {"do": "mic", "action": "toggle"}, "three": {"do": "spoken", "action": "toggle"},
        "ok_sign": {"do": "new_chat"}, "pinch": {"do": "say", "text": "What's on today?"},
        "wave": {"do": "skill", "name": "morning-briefing"}, "push": {"do": "dismiss"}}}}
    run_gestures(host, [gesture(1, "call_me"), gesture(2, "three"), gesture(3, "ok_sign"), gesture(4, "pinch"),
                        gesture(5, "wave", kind="motion", held=0), gesture(6, "push", kind="motion", held=0)])
    assert [call[:3] for call in host.calls] == [  # the toggles from what is on now: the microphone and the voice
        ("orion", "microphone", {"muted": True}), ("orion", "spoken_replies", {"on": False}),
        ("orion", "new_conversation", {}), ("orion", "hud_hide", {"parts": [], "newest": True})]
    assert host.turns.did == [("submit", "What's on today?", "hud"), ("submit", "Run your morning-briefing skill.",
                                                                       "hud")]
    assert all(data["ok"] for kind, data in host.events if kind == "intent")


def test_gestures_sleep_until_the_waking_gesture_while_one_is_bound() -> None:
    host = Host()
    host.cfg.mcp_servers = {"hands": {"command": "orion-hands", "intents": True, "gestures": {
        "open_palm": {"do": "arm", "seconds": 10}, "swipe_right": {"do": "media", "action": "next"}}}}
    runner = run_gestures(host, [gesture(1, "swipe_right", kind="motion", held=0), gesture(2, "open_palm"),
                                 gesture(3, "swipe_right", kind="motion", held=0)])
    assert [call[1:3] for call in host.calls] == [("media", {"action": "next"})]  # only once awake
    shown = [data for kind, data in host.events if kind == "intent"]
    assert [(d["intent"], d["ok"]) for d in shown] == [("asleep", False), ("arm", True), ("media", True)]
    assert shown[0]["text"] == "Swipe right → gestures are asleep"
    assert 9 < runner.awake_for("hands") <= 10  # type: ignore[attr-defined]


class Hands:
    """A hands tracker inside the test: the orion://intents resource, subscribable both ways (resources/subscribe on a
    2025-era connection, subscriptions/listen on a 2026-07-28 one), and no tools."""

    URI = "orion://intents"

    def __init__(self, *, skew_ms: int = 0, instance: str = "") -> None:
        """No intents yet; ids from this PC's clock, or (``skew_ms``, an ``instance``) a server's own clock and run."""
        import mcp_types as mt
        from mcp.server import Server
        from mcp.server.subscriptions import InMemorySubscriptionBus, ListenHandler

        self.mt = mt
        self.skew_ms, self.instance = skew_ms, instance
        self.items: list[dict[str, object]] = []
        self.sessions: list[object] = []
        self.bus = InMemorySubscriptionBus()
        self.server = Server("orion-hands", on_list_resources=self._list, on_read_resource=self._read,
                             on_subscribe_resource=self._subscribe, on_subscriptions_listen=ListenHandler(self.bus))

    async def _list(self, _ctx: object, _params: object) -> object:
        """The one resource."""
        return self.mt.ListResourcesResult(resources=[self.mt.Resource(uri=self.URI, name="intents",
                                                                       mime_type="application/json")])

    async def _read(self, _ctx: object, _params: object) -> object:
        """The last intents."""
        return self.mt.ReadResourceResult(contents=[self.mt.TextResourceContents(
            uri=self.URI, mime_type="application/json", text=json.dumps(self.feed()))])

    def feed(self) -> dict[str, object]:
        """The resource's document: with the server's own clock and run when it has them."""
        if not self.instance:
            return {"intents": self.items[-20:]}
        return {"intents": self.items[-20:], "instance": self.instance, "now": int(time.time() * 1000) + self.skew_ms}

    def restart(self, *, skew_ms: int, instance: str) -> None:
        """The server restarted: a new run, nothing in its feed, its clock maybe set back."""
        self.items, self.skew_ms, self.instance = [], skew_ms, instance

    async def _subscribe(self, ctx: object, _params: object) -> object:
        """A 2025-era subscriber."""
        self.sessions.append(ctx.session)  # type: ignore[attr-defined]
        return self.mt.EmptyResult()

    async def push(self, intent: str, args: dict[str, object] | None = None, *, age_s: float = 0.0) -> int:
        """A new intent (its id from this PC's clock, or its own), and the update notification."""
        from mcp.server.subscriptions import ResourceUpdated

        last = int(self.items[-1]["id"]) if self.items else 0  # type: ignore[call-overload]
        number = max(last + 1, int(time.time() * 1000 + self.skew_ms - age_s * 1000))
        self.items.append({"id": number, "intent": intent, "args": args or {}, "gesture": "pinch", "ts": time.time()})
        await self.bus.publish(ResourceUpdated(uri=self.URI))
        for session in list(self.sessions):
            try:
                await session.send_resource_updated(self.URI)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 (a subscriber from before a reconnect)
                self.sessions.remove(session)
        return number


@pytest.mark.parametrize("mode", ["legacy", "auto"])  # auto, in-process: the 2026-07-28 protocol
def test_the_hub_follows_a_servers_intents_without_replaying_them(mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A server with intents allowed is followed: a stale intent from before never runs, each new one runs once and in
    order, a reconnect replays nothing; a server without intents allowed is never asked; a server with no tools at all
    connects anyway."""
    from mcp import Client

    hands = Hands()
    cfg = default_config()
    cfg.mcp_servers = {"hands": {"command": "orion-hands", "intents": True}}
    ran: list[tuple[str, str, int]] = []

    async def opened(self: MCPHub, stack: object, link: object) -> object:
        """Connect the hands server inside the test, with the hub's own message handler."""
        client = Client(hands.server, mode=mode, message_handler=self._notifications(link))  # type: ignore[arg-type]
        return await stack.enter_async_context(client)  # type: ignore[attr-defined]

    async def on_intent(server: str, intent: object) -> None:
        """Note what runs."""
        ran.append((server, intent.name, intent.id))  # type: ignore[attr-defined]

    async def until(check: object, seconds: float = 5.0) -> None:
        """Wait until check() holds."""
        async with asyncio.timeout(seconds):
            while not check():  # type: ignore[operator]
                await asyncio.sleep(0.01)

    monkeypatch.setattr(MCPHub, "_open", opened)

    async def run() -> None:
        """Connect, push, reconnect, push."""
        stale = await hands.push("stop", age_s=60)
        hub = MCPHub(cfg, {}, ops=OpTrace(lambda kind, **data: None, cfg.hud, recent=10), on_intent=on_intent)
        await hub.configure(wait_s=5)
        assert hub.status()[0]["status"] == "connected" and hub.tools() == {}
        await until(lambda: hub._intents_last.get("hands") == stale)  # read once, the stale one passed over
        first = await hands.push("stop")
        await until(lambda: len(ran) == 1)
        second, third = await hands.push("listen"), await hands.push("media", {"action": "next"})
        await until(lambda: len(ran) == 3)
        await hub.restart("hands")
        await until(lambda: hub.status()[0]["status"] == "connected")
        await asyncio.sleep(0.2)  # the first read after resubscribing: nothing replays
        fourth = await hands.push("new_chat")
        await until(lambda: len(ran) == 4)
        await hub.stop()
        assert ran == [("hands", "stop", first), ("hands", "listen", second), ("hands", "media", third),
                       ("hands", "new_chat", fourth)]

    asyncio.run(run())
    cfg.mcp_servers = {"hands": {"command": "orion-hands"}}  # intents not allowed: never subscribed
    hands.sessions.clear()

    async def quiet() -> None:
        """Connect without intents and push one."""
        hub = MCPHub(cfg, {}, ops=OpTrace(lambda kind, **data: None, cfg.hud, recent=10), on_intent=on_intent)
        await hub.configure(wait_s=5)
        await hands.push("stop")
        await asyncio.sleep(0.2)
        await hub.stop()

    asyncio.run(quiet())
    assert len(ran) == 4 and not hands.sessions


def test_only_a_server_set_to_lend_has_its_tools_lent(monkeypatch: pytest.MonkeyPatch) -> None:
    """/mcp lends Orion's own tools, and another server's only while its settings say lend: true (which changes
    without a reconnect)."""
    import mcp_types as mt
    from mcp import Client
    from mcp.server import Server

    async def list_tools(_ctx: object, _params: object) -> mt.ListToolsResult:
        """One tool."""
        return mt.ListToolsResult(tools=[mt.Tool(name="lights_off", description="Switch the lights off.",
                                                 input_schema={"type": "object"})])

    home = Server("home", on_list_tools=list_tools)
    cfg = default_config()
    cfg.mcp_servers = {"home": {"command": "home-mcp"}}
    opened: list[str] = []

    async def open_link(self: MCPHub, stack: object, link: object) -> object:
        """Connect the home server inside the test."""
        opened.append(link.name)  # type: ignore[attr-defined]
        return await stack.enter_async_context(Client(home, mode="legacy"))  # type: ignore[attr-defined]

    monkeypatch.setattr(MCPHub, "_open", open_link)

    async def run() -> None:
        """Connect, lend, stop lending."""
        hub = MCPHub(cfg, {}, ops=OpTrace(lambda kind, **data: None, cfg.hud, recent=10))
        await hub.configure(wait_s=5)
        assert [tool.remote for tool in hub.tools().values()] == ["lights_off"] and hub.lent() == []
        cfg.mcp_servers = {"home": {"command": "home-mcp", "lend": True}}
        await hub.configure(wait_s=5)
        assert [tool.remote for tool in hub.lent()] == ["lights_off"] and opened == ["home"]  # no reconnect
        cfg.mcp_servers = {"home": {"command": "home-mcp"}}
        await hub.configure(wait_s=5)
        assert hub.lent() == []
        await hub.stop()

    asyncio.run(run())


def test_a_server_with_its_own_clock_is_followed_across_its_restarts(monkeypatch: pytest.MonkeyPatch) -> None:
    """A server that says its own clock and run (the hands tracker): its gestures run by its clock however far this
    PC's is (an hour here), and once it restarts (another run, its clock set back) it is followed from its first."""
    from mcp import Client

    hands = Hands(skew_ms=-3_600_000, instance="run-1")
    cfg = default_config()
    cfg.mcp_servers = {"hands": {"command": "orion-hands", "intents": True}}
    ran: list[int] = []

    async def opened(self: MCPHub, stack: object, link: object) -> object:
        """Connect the hands server inside the test, with the hub's own message handler."""
        client = Client(hands.server, message_handler=self._notifications(link))  # type: ignore[arg-type]
        return await stack.enter_async_context(client)  # type: ignore[attr-defined]

    async def on_intent(_server: str, intent: object) -> None:
        """Note what runs."""
        ran.append(intent.id)  # type: ignore[attr-defined]

    async def until(check: object, seconds: float = 5.0) -> None:
        """Wait until check() holds."""
        async with asyncio.timeout(seconds):
            while not check():  # type: ignore[operator]
                await asyncio.sleep(0.01)

    monkeypatch.setattr(MCPHub, "_open", opened)

    async def run() -> None:
        """Connect, push, restart the server with its clock set back, push."""
        hub = MCPHub(cfg, {}, ops=OpTrace(lambda kind, **data: None, cfg.hud, recent=10), on_intent=on_intent)
        await hub.configure(wait_s=5)
        await asyncio.sleep(0.2)  # subscribed and read once
        first = await hands.push("stop")  # an hour behind this PC: fresh by the server's own clock
        await until(lambda: ran == [first])
        hands.restart(skew_ms=-7_200_000, instance="run-2")  # set back another hour: its ids are lower now
        second = await hands.push("stop")
        assert second < first
        await until(lambda: ran == [first, second])
        await hub.stop()

    asyncio.run(run())


class LiveView:
    """A gesture remote's live view inside the test: orion://hands, subscribable both ways; each read counts."""

    URI = "orion://hands"

    def __init__(self) -> None:
        """Frame 0, nobody subscribed."""
        import mcp_types as mt
        from mcp.server import Server
        from mcp.server.subscriptions import InMemorySubscriptionBus, ListenHandler

        self.mt = mt
        self.seq = 0
        self.reads = 0
        self.sessions: list[object] = []
        self.unsubscribed = 0
        self.bus = InMemorySubscriptionBus()
        self.server = Server("orion-hands", on_list_resources=self._list, on_read_resource=self._read,
                             on_subscribe_resource=self._subscribe, on_unsubscribe_resource=self._unsubscribe,
                             on_subscriptions_listen=ListenHandler(self.bus))

    async def _list(self, _ctx: object, _params: object) -> object:
        """The one resource."""
        return self.mt.ListResourcesResult(resources=[self.mt.Resource(uri=self.URI, name="hands",
                                                                       mime_type="application/json")])

    async def _read(self, _ctx: object, _params: object) -> object:
        """The latest frame."""
        self.reads += 1
        return self.mt.ReadResourceResult(contents=[self.mt.TextResourceContents(
            uri=self.URI, mime_type="application/json", text=json.dumps({"seq": self.seq, "hands": []}))])

    async def _subscribe(self, ctx: object, _params: object) -> object:
        """A 2025-era subscriber."""
        self.sessions.append(ctx.session)  # type: ignore[attr-defined]
        return self.mt.EmptyResult()

    async def _unsubscribe(self, ctx: object, _params: object) -> object:
        """It stopped watching."""
        self.unsubscribed += 1
        return self.mt.EmptyResult()

    async def frame(self) -> None:
        """A new frame, and the update notification."""
        from mcp.server.subscriptions import ResourceUpdated

        self.seq += 1
        await self.bus.publish(ResourceUpdated(uri=self.URI))
        for session in list(self.sessions):
            with contextlib.suppress(Exception):
                await session.send_resource_updated(self.URI)  # type: ignore[attr-defined]


@pytest.mark.parametrize("mode", ["legacy", "auto"])
def test_the_hub_watches_a_resource_while_asked_and_across_reconnects(mode: str,
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """watch reads a resource at once and after each update it announces, follows it again after a reconnect, and
    stops (unsubscribing) when told; read_resource reads one once."""
    from mcp import Client

    view = LiveView()
    cfg = default_config()
    cfg.mcp_servers = {"hands": {"command": "orion-hands", "intents": True}}
    heard: list[int] = []

    async def opened(self: MCPHub, stack: object, link: object) -> object:
        """Connect the remote inside the test, with the hub's own message handler."""
        client = Client(view.server, mode=mode, message_handler=self._notifications(link))  # type: ignore[arg-type]
        return await stack.enter_async_context(client)  # type: ignore[attr-defined]

    async def until(check: object, seconds: float = 5.0) -> None:
        """Wait until check() holds."""
        async with asyncio.timeout(seconds):
            while not check():  # type: ignore[operator]
                await asyncio.sleep(0.01)

    monkeypatch.setattr(MCPHub, "_open", opened)

    async def run() -> None:
        """Watch, update, reconnect, stop."""
        hub = MCPHub(cfg, {}, ops=OpTrace(lambda kind, **data: None, cfg.hud, recent=10))
        await hub.configure(wait_s=5)
        assert json.loads(await hub.read_resource("hands", LiveView.URI))["seq"] == 0
        stop = hub.watch("hands", LiveView.URI, lambda text: heard.append(json.loads(text)["seq"]))
        await until(lambda: heard == [0])
        await view.frame()
        await until(lambda: heard[-1:] == [1])
        await hub.restart("hands")
        await until(lambda: hub.status()[0]["status"] == "connected")
        await until(lambda: len(heard) >= 3)  # read again once reconnected
        await view.frame()
        await until(lambda: heard[-1:] == [2])
        reads = view.reads
        await asyncio.sleep(0.35)  # no update meanwhile, and no `every`: not read again
        assert view.reads == reads
        stop()
        if mode == "legacy":
            await until(lambda: view.unsubscribed == 1)
        reads = view.reads
        await view.frame()
        await asyncio.sleep(0.2)
        assert view.reads == reads and heard[-1] == 2  # nobody follows it any more
        quiet = hub.watch("hands", LiveView.URI, lambda text: heard.append(json.loads(text)["seq"]), every=0.1)
        await until(lambda: view.reads >= reads + 4)  # read at once, then every 0.1 s without any update
        quiet()
        if mode == "legacy":
            await until(lambda: view.unsubscribed == 2)
        with pytest.raises(LookupError):
            await hub.read_resource("nobody", LiveView.URI)
        await hub.stop()

    asyncio.run(run())


def test_the_hands_live_view_reaches_the_hud_only_when_it_says_something_new() -> None:
    """A frame read again (nothing new) isn't sent on; a picture already sent isn't sent twice; a new viewer gets the
    next frame even if it says nothing new."""
    from orion.runtime import Runtime

    published: list[dict[str, object]] = []
    rt = SimpleNamespace(_hands_sent={}, _hands_viewers={}, bus=SimpleNamespace(
        publish=lambda kind, **data: published.append(data["frame"])), _follow_hands=lambda: None)
    frame = {"seq": 1, "hands": [], "link": {"connected": True}, "jpg": "AAAA", "jpg_seq": 1}

    def read(**change: object) -> None:
        """The remote's view read once."""
        Runtime._hands_frame(rt, "hands", json.dumps({**frame, **change}))  # type: ignore[arg-type]

    read()
    read()  # read again: the same
    assert [f["seq"] for f in published] == [1] and published[0]["jpg"] == "AAAA"
    read(seq=2)  # a new frame, the same picture
    assert published[-1]["seq"] == 2 and "jpg" not in published[-1] and published[-1]["jpg_seq"] == 1
    read(seq=2, link={"connected": False})  # only the link changed: that is news too
    assert len(published) == 3
    Runtime.hands_view(rt, 7, True)  # type: ignore[arg-type]
    read(seq=2, link={"connected": False})
    assert len(published) == 4 and published[-1]["jpg"] == "AAAA"  # the newcomer gets the picture as well


def test_the_live_view_says_when_orion_isnt_connected_to_the_remote() -> None:
    """Only a connected remote sends frames: one that isn't gets a frame saying so (and why), once, while a HUD shows
    the view; nobody watching, nothing."""
    from orion.runtime import Runtime

    published: list[dict[str, object]] = []
    status = [{"name": "hands", "status": "reconnecting", "error": "connection refused"},
              {"name": "spotify", "status": "error", "error": "no token"}]  # not a gesture remote
    rt = SimpleNamespace(_hands_sent={}, _hands_viewers={}, cfg=SimpleNamespace(mcp_servers={
                             "hands": {"command": "orion-hands", "intents": True}, "spotify": {"command": "spotify"}}),
                         hub=SimpleNamespace(status=lambda: status),
                         bus=SimpleNamespace(publish=lambda kind, **data: published.append(data["frame"])))
    rt._hands_frame = lambda server, text: Runtime._hands_frame(rt, server, text)  # type: ignore[arg-type]
    Runtime._hands_link(rt)  # type: ignore[arg-type]
    assert published == []  # nobody watches
    rt._hands_viewers[1] = False
    Runtime._hands_link(rt)  # type: ignore[arg-type]
    Runtime._hands_link(rt)  # type: ignore[arg-type]
    assert published == [{"seq": 0, "hands": [], "link": {"connected": False, "error": "connection refused",
                                                          "camera": False, "paused": False, "tracker": ""}}]
