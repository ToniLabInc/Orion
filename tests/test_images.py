"""images: pictures sized down and compressed for the models, carried by each provider's format, kept out of the
history (a note stands for them), and returned by a tool through the hub to the brain."""

import asyncio
import base64
import io

import pytest
from PIL import Image as Pil

from orion import images
from orion.base import CallContext, GroupInfo, Message, ToolRequest
from orion.brain.context import HistoryWindow
from orion.config import default_config
from orion.llm import PICTURES_NOTE, ollama_messages, openrouter_messages
from orion.llm.claude import user_message
from orion.mcp import MCPHub, OpTrace
from orion.tools import Group, build_server


def png(width: int, height: int, mode: str = "RGB") -> bytes:
    out = io.BytesIO()
    Pil.new(mode, (width, height), (10, 20, 30, 128) if mode == "RGBA" else (10, 20, 30)).save(out, "PNG")
    return out.getvalue()


def test_a_picture_is_sized_down_and_sent_as_jpeg() -> None:
    picture = images.prepare(png(4000, 1000), label="the screen", max_side=1568, quality=80)
    assert picture["mime"] == "image/jpeg" and picture["label"] == "the screen"
    with Pil.open(io.BytesIO(base64.b64decode(picture["data"]))) as decoded:
        assert decoded.size == (1568, 392) and decoded.format == "JPEG"
    clear = images.prepare(png(200, 100, "RGBA"), label="icon", max_side=1568, quality=80)
    with Pil.open(io.BytesIO(base64.b64decode(clear["data"]))) as decoded:
        assert decoded.size == (200, 100)  # small pictures keep their size; transparency lies over white
    as_text = "data:image/png;base64," + base64.b64encode(png(50, 50)).decode()
    assert images.from_base64(as_text, label="x", max_side=64, quality=80)["mime"] == "image/jpeg"
    with pytest.raises(ValueError):
        images.prepare(b"not a picture", label="x", max_side=64, quality=80)
    with pytest.raises(ValueError):
        images.from_base64("%%%", label="x", max_side=64, quality=80)


PIC = images.prepare(png(20, 20), label="the screen", max_side=64, quality=80)
TURN: list[Message] = [
    {"role": "user", "content": "What's this?", "images": [PIC]},
    {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "look", "arguments": {}}]},
    {"role": "tool", "tool_call_id": "c1", "name": "look", "content": "Here.", "images": [PIC]},
    {"role": "assistant", "content": "A terminal."},
]


def test_openrouter_and_ollama_carry_pictures_only_to_models_that_see() -> None:
    sent = openrouter_messages(TURN, vision=True)
    assert [m["role"] for m in sent] == ["user", "assistant", "tool", "user", "assistant"]
    assert sent[0]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")  # type: ignore[index]
    assert sent[3]["content"][0]["text"].startswith(PICTURES_NOTE)  # type: ignore[index]
    blind = openrouter_messages(TURN, vision=False)
    assert [m["role"] for m in blind] == ["user", "assistant", "tool", "assistant"] and blind[0]["content"] == \
        "What's this?"
    local = ollama_messages(TURN, vision=True)
    assert local[0]["images"] == [PIC["data"]] and local[3]["role"] == "user" and local[3]["images"] == [PIC["data"]]
    assert "images" not in ollama_messages(TURN)[0]


def test_claude_gets_a_question_with_pictures_as_streaming_input() -> None:
    async def first() -> dict[str, object]:
        return await anext(user_message("What's this?", [PIC]))

    message = asyncio.run(first())
    content = message["message"]["content"]  # type: ignore[index]
    assert content[0] == {"type": "text", "text": "What's this?"}
    assert content[1]["source"] == {"type": "base64", "media_type": "image/jpeg", "data": PIC["data"]}


def test_the_history_keeps_a_note_instead_of_the_pictures() -> None:
    window = HistoryWindow(default_config().brain)
    window.append_turn(TURN)
    kept = window.messages()
    assert not any("images" in m for m in kept)
    assert kept[0]["content"] == "What's this?\n[a picture: the screen]" and kept[2]["content"].endswith(
        "[a picture: the screen]")


def test_a_tools_picture_reaches_the_brain_through_the_hub() -> None:
    group = Group(GroupInfo("demo", "Demo", "A demo group."))

    @group.tool(title="Take a screenshot", read_only=True)
    def screenshot() -> list[object]:
        """Look at the screen."""
        from mcp.server.mcpserver import Image as McpImage

        return ["The screen:", McpImage(data=png(3000, 2000), format="png")]

    cfg = default_config()
    hub = MCPHub(cfg, {}, ops=OpTrace(lambda kind, **data: None, cfg.hud, recent=10), core=build_server([group]),
                 core_groups={"demo": group.info})

    async def go():
        await hub.configure(wait_s=5)
        out = await hub.call_named_rich(ToolRequest("screenshot", {}), CallContext("brain", "t"))
        await hub.stop()
        return out

    out = asyncio.run(go())
    assert "The screen:" in out.text and len(out.images) == 1 and out.images[0]["label"] == "Take a screenshot"
    with Pil.open(io.BytesIO(base64.b64decode(out.images[0]["data"]))) as decoded:
        assert max(decoded.size) == cfg.eyes.image_max_side
