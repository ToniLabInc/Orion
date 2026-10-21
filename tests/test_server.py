"""server: who may talk to the HUD's server, what its pages may load, the built HUD's files, and the per-client
outbox (coalesced levels, joined reply text that never jumps a tool, a slow client noticed)."""

import asyncio
import json
import re
from typing import Any

import pytest

from orion.server import CSP, DIST, Outbox, from_this_pc


@pytest.mark.parametrize(("headers", "allowed"), [
    ({"host": "localhost"}, True),
    ({"host": "127.0.0.1:8765", "origin": "http://127.0.0.1:8765"}, True),
    ({"host": "[::1]:8765"}, True),
    ({"host": "127.0.0.1:5173", "origin": "http://127.0.0.1:5173"}, True),
    ({"host": "attacker.example"}, False),
    ({"host": "localhost.attacker.example"}, False),
    ({"host": "localhost:8765", "origin": "https://attacker.example"}, False),
    ({"host": "localhost:8765", "origin": "null"}, False),
    ({"host": "localhost:8765", "origin": "chrome-extension://x"}, False),
    ({"host": "localhost:8765", "origin": "http://localhost:5173"}, False),
    ({"host": "localhost:8765", "sec-fetch-site": "cross-site"}, False),
])
def test_only_his_own_pages_on_this_pc(headers: dict[str, str], allowed: bool) -> None:
    """Local names with a matching origin (or none) pass; DNS rebinding, other sites and extensions don't."""
    assert from_this_pc(headers) is allowed


def test_pages_load_from_orion_alone() -> None:
    """Every source is his own (or inline styles, data, blobs, his WebSocket); scripts only his; no framing."""
    rules = dict(part.split(" ", 1) for part in CSP.split("; "))
    allowed = {"'self'", "'none'", "'unsafe-inline'", "data:", "blob:", "ws://localhost:*", "ws://127.0.0.1:*"}
    assert all(set(sources.split()) <= allowed for sources in rules.values())
    assert rules["default-src"] == rules["script-src"] == "'self'" and rules["frame-ancestors"] == "'none'"


def test_the_built_hud_has_every_asset_it_names() -> None:
    """Each /assets/ file index.html references exists (nothing to check before the HUD is built)."""
    index = DIST / "index.html"
    if not index.is_file():
        pytest.skip("the HUD isn't built")
    for asset in re.findall(r'"/(assets/[^"]+)"', index.read_text(encoding="utf-8")):
        assert (DIST / asset).is_file(), asset


def test_the_outbox_coalesces_without_reordering() -> None:
    """Levels collapse to the newest; reply text joins only at the tail; a backlog over the limit is flagged."""

    def drain(box: Outbox) -> list[dict[str, Any]]:
        """Every queued event, decoded."""

        async def take() -> list[dict[str, Any]]:
            """Get until empty."""
            return [json.loads(await box.get()) for _ in range(len(box))]

        return asyncio.run(take())

    levels = Outbox()
    for n in range(100):
        levels.push({"type": "level", "source": "mic", "value": n})
    assert [e["value"] for e in drain(levels)] == [99]
    text = Outbox()
    for piece in "abc":
        text.push({"type": "assistant_delta", "id": "t1", "text": piece})
    text.push({"type": "tool", "phase": "start", "id": "op1"})
    text.push({"type": "assistant_delta", "id": "t1", "text": "d"})
    assert [(e["type"], e.get("text")) for e in drain(text)] == [("assistant_delta", "abc"), ("tool", None),
                                                                 ("assistant_delta", "d")]
    slow = Outbox(limit=3)
    for n in range(4):
        slow.push({"type": "state", "state": str(n)})
    assert slow.overflowed and len(slow) == 4


def test_the_hands_live_view_goes_only_to_a_tab_that_shows_it() -> None:
    """A frame of the live view reaches a tab only once it says it shows the view, and a newer frame of the same
    remote replaces one still waiting."""
    box = Outbox()
    box.push({"type": "hands", "server": "hands", "frame": {"seq": 1}})
    assert len(box) == 0
    box.hands = True
    for seq in range(2, 6):
        box.push({"type": "hands", "server": "hands", "frame": {"seq": seq}})
    box.push({"type": "hands", "server": "other", "frame": {"seq": 1}})

    async def take() -> list[dict[str, Any]]:
        """Get until empty."""
        return [json.loads(await box.get()) for _ in range(len(box))]

    assert [(e["server"], e["frame"]["seq"]) for e in asyncio.run(take())] == [("hands", 5), ("other", 1)]


def test_the_gestures_page_and_saving_what_gestures_do() -> None:
    """The Gestures page lists each remote with its gestures and bindings, the actions with their options, and the
    tools a gesture can use; the user saves a whole set (checked) or goes back to the defaults; the AI can't."""
    from types import SimpleNamespace

    from orion.config import Refused, default_config
    from orion.gestures import default_bindings
    from orion.tools.core import Controls

    cfg = default_config()
    cfg.mcp_servers = {"hands": {"command": "orion-hands", "intents": True}, "time": {"command": "uvx"}}
    saved: list[dict[str, Any]] = []
    tool = SimpleNamespace(remote="open", title="Open an app", summary="Open an app.", hints={"destructive": False},
                           parameters={"type": "object", "properties": {"target": {"type": "string"}}})
    hidden = SimpleNamespace(remote="power", title="Power", summary="Power.", hints={"destructive": True},
                             parameters={"type": "object"})
    status = [{"name": "hands", "status": "connected", "error": "", "enabled": True, "tools": []},
              {"name": "system", "status": "connected", "error": "", "enabled": True,
               "tools": [{"remote": "open", "enabled": True}, {"remote": "power", "enabled": False}]}]

    async def read_resource(server: str, uri: str) -> str:
        """The remote's vocabulary."""
        assert (server, uri) == ("hands", "orion://gestures")
        return json.dumps({"gestures": [{"name": "victory", "title": "Victory", "kind": "shape", "how": "A V."}],
                           "hold_steps": [0.6, 1.0, 1.5, 2.0], "max_hold": 8.0,
                           "two_hands": {"together": 1.8, "apart": 3.0}, "static_hold": 0.3})

    async def apply_servers(servers: dict[str, Any], *, actor: str) -> dict[str, Any]:
        """Save them."""
        saved.append(servers)
        cfg.mcp_servers = servers
        return {"changed": ["mcp_servers"], "restart": []}

    rt = SimpleNamespace(cfg=cfg, hub=SimpleNamespace(status=lambda: status, read_resource=read_resource,
                                                      offered=lambda: [("system", True, tool),
                                                                       ("system", True, hidden)]),
                         skills=SimpleNamespace(all=lambda: {"morning-briefing": object()}),
                         intents=SimpleNamespace(awake_for=lambda server: 0.0), apply_servers=apply_servers)
    controls = Controls(rt)  # type: ignore[arg-type]
    page = asyncio.run(controls.gestures_page())
    (remote,) = page["remotes"]
    assert remote["name"] == "hands" and remote["custom"] is False and remote["gestures"][0]["name"] == "victory"
    defaults = {g: b.as_dict() for g, b in default_bindings(cfg.gestures.defaults).items()}
    assert remote["bindings"] == page["defaults"] == defaults and remote["hold_steps"] == [0.6, 1.0, 1.5, 2.0]
    assert remote["two_hands"] == {"together": 1.8, "apart": 3.0} and remote["static_hold"] == 0.3
    assert remote["words"]["swipe_right"] == "next track" and remote["vocabulary_error"] == ""
    assert [t["name"] for t in page["tools"]] == ["open"]  # a tool switched off can't be bound
    assert page["skills"] == ["morning-briefing"] and {"value": "hands", "label": "HANDS"} in page["parts"]
    assert page["actions"]["tool"]["params"][2] == {"name": "args", "kind": "args", "choices": [], "required": False,
                                                    "default": {}}
    actions = page["actions"]  # each option's choices in words, and what a new binding of each starts with
    assert actions["media"]["params"][0]["choices"][1] == {"value": "next", "label": "Next track"}
    assert {"value": 5, "label": "5 min"} in actions["timer"]["params"][0]["choices"]
    assert actions["arm"]["params"][0]["choices"][1] == {"value": 8, "label": "Awake 8 s"}
    assert actions["say"]["start"] == {"do": "say", "text": "What's on today?"}
    assert actions["tool"]["start"] == {"do": "tool", "server": "system", "tool": "open"}
    assert actions["media"]["start"] == {"do": "media", "action": "play_pause"} and page["pause_minutes"] == 30
    status[0]["status"] = "connecting"  # gone for a moment: what it said last stays, with why it can't say now
    (remote,) = asyncio.run(controls.gestures_page())["remotes"]
    assert remote["gestures"][0]["kind"] == "shape" and remote["vocabulary_error"] == "it isn't connected"
    status[0]["status"] = "connected"
    asyncio.run(controls.save_gestures("hands", {"victory": {"do": "new_chat"}, "wave": None}, actor="hud"))
    assert saved[-1]["hands"]["gestures"] == {"victory": {"do": "new_chat"}}
    assert saved[-1]["time"] == {"command": "uvx"}  # the other servers untouched
    with pytest.raises(Refused, match="victory: unknown action"):
        asyncio.run(controls.save_gestures("hands", {"victory": {"do": "fly"}}, actor="hud"))
    with pytest.raises(Refused, match="Only the user"):
        asyncio.run(controls.save_gestures("hands", {}, actor="ai"))
    with pytest.raises(Refused, match="doesn't send gestures"):
        asyncio.run(controls.save_gestures("time", {}, actor="hud"))
    cfg.mcp_servers["hands"]["gestures"] = {"victory": {"do": "media", "action": "pley"}, "wave": {"do": "dismiss"}}
    asyncio.run(controls.save_gestures("hands", {"wave": {"do": "new_chat"}}, actor="hud"))
    assert saved[-1]["hands"]["gestures"] == {"victory": {"do": "media", "action": "pley"},  # the typo stays...
                                              "wave": {"do": "new_chat"}}
    asyncio.run(controls.save_gestures("hands", {"victory": None, "wave": {"do": "new_chat"}}, actor="hud"))
    assert saved[-1]["hands"]["gestures"] == {"wave": {"do": "new_chat"}}  # ...until the page names its gesture
    asyncio.run(controls.reset_gestures("hands", actor="hud"))
    assert "gestures" not in saved[-1]["hands"] and len(saved) == 4
    assert asyncio.run(controls.reset_gestures("hands", actor="hud")) == {"changed": [], "restart": []}


def test_his_screen_tools_show_toggle_hide_and_dismiss() -> None:
    """hud_show shows or toggles parts of the HUD, hud_hide hides them (everything when none is named) or only the
    newest panel; each is one summon to the HUD. A new conversation asked for between answers says so."""
    from types import SimpleNamespace

    from mcp import Client

    from orion.config import default_config
    from orion.tools.core import Controls, server

    events: list[tuple[str, dict[str, Any]]] = []
    spawned: list[str] = []
    rt = SimpleNamespace(cfg=default_config(), bus=SimpleNamespace(publish=lambda kind, **data: events.append(
        (kind, data))), turns=SimpleNamespace(hud_open=lambda: True, busy=False, new_conversation=lambda: None),
        supervisor=SimpleNamespace(spawn=lambda job, name: spawned.append(name) or job.close()))
    calls = [("hud_show", {"parts": ["weather"], "toggle": True}), ("hud_hide", {"parts": [], "newest": True}),
             ("hud_hide", {"parts": []}), ("hud_show", {"parts": []}), ("new_conversation", {})]

    async def run() -> list[tuple[bool, str]]:
        """Each call, in order."""
        async with Client(server(Controls(rt))) as client:  # type: ignore[arg-type]
            results = [await client.call_tool(name, args) for name, args in calls]
        return [(r.is_error, r.content[0].text) for r in results]  # type: ignore[union-attr]

    results = asyncio.run(run())
    assert results[0] == (False, "On or off the screen: weather.")
    assert results[1] == (False, "The newest panel is off the screen.")
    assert results[3][0] and results[3][1].startswith("Say what to show")
    assert results[4] == (False, "A new conversation.") and spawned == ["after the reply"]
    assert [(data["action"], data["panels"][:2]) for kind, data in events if kind == "summon"] == [
        ("toggle", ["weather"]), ("dismiss", []), ("hide", ["comms", "vitals"])]


def test_his_voice_goes_to_a_browser_before_events_and_never_backs_up() -> None:
    """Frames of his voice jump the events (they are timed); a browser more than the backlog behind loses the oldest."""
    from orion.server import AUDIO_BACKLOG_BYTES

    async def run() -> list[str | bytes]:
        box = Outbox()
        box.push({"type": "state", "ts": 1.0, "state": "speaking"})
        box.push_audio(b"\x02one")
        box.push_audio(b"\x02two")
        return [await box.get() for _ in range(3)]

    first, second, event = asyncio.run(run())
    assert (first, second) == (b"\x02one", b"\x02two") and json.loads(event)["state"] == "speaking"
    box = Outbox()
    for i in range(30):
        box.push_audio(bytes([2, i]) + bytes(AUDIO_BACKLOG_BYTES // 20))
    oldest = asyncio.run(box.get())
    assert isinstance(oldest, bytes) and oldest[1] > 0  # the first frames were dropped
