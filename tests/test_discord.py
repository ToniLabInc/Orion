"""discord: who the bot hears, answers split as Discord needs (code blocks kept whole), the feed it serves, a message's way
through Orion: a turn from Discord, answered there (no Discord connection: the client is never started), and that nothing it
sends carries a secret."""

import asyncio
import base64
import io
import json
import logging
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image as Pil

from orion import discord_bridge, redact
from orion.base import TurnResult
from orion.config import default_config
from orion.discord_bridge import Bridge, ids_of, picture_url, split_message
from orion.intents import Intent, IntentRunner
from orion.redact import Redactor

TOKEN = "bot-secret-Zx9Qm4Lp7Rk2Vb8Nc5Td1Wh6Yg3Jf0A"  # no shipped shape knows it: only being told hides it


def test_ids_and_who_is_heard() -> None:
    assert ids_of("123, 456 x 789") == {123, 456, 789} and ids_of("") == set()
    bridge = Bridge("token", {42}, {7})
    assert bridge.heard(42, is_bot=False, direct=True, mentioned=False, channel_id=1)
    assert bridge.heard(42, is_bot=False, direct=False, mentioned=True, channel_id=7)
    assert not bridge.heard(42, is_bot=False, direct=False, mentioned=True, channel_id=8)  # not an allowed channel
    assert not bridge.heard(42, is_bot=False, direct=False, mentioned=False, channel_id=7)  # not for him
    assert not bridge.heard(99, is_bot=False, direct=True, mentioned=False, channel_id=1)  # someone else
    assert not bridge.heard(42, is_bot=True, direct=True, mentioned=False, channel_id=1)


def test_long_answers_are_split_and_code_blocks_stay_whole() -> None:
    assert split_message("") == [] and split_message("hi") == ["hi"]
    words = " ".join(["word"] * 900)
    pieces = split_message(words, limit=500)
    assert all(len(p) <= 500 for p in pieces) and " ".join(pieces).split() == words.split()
    code = "Here:\n```py\n" + "\n".join(f"print({i})" for i in range(200)) + "\n```\nDone."
    pieces = split_message(code, limit=400)
    assert all(len(p) <= 400 and p.count("```") % 2 == 0 for p in pieces)
    assert pieces[1].startswith("```py\n") and pieces[-1].endswith("Done.")


def test_the_feed_and_a_picture() -> None:
    bridge = Bridge("token", {42}, set())
    bridge.feed.append({"id": 5, "intent": "message", "args": {"text": "hi"}, "ts": 1.0})
    feed = json.loads(bridge.feed_text())
    assert feed["intents"][0]["intent"] == "message" and feed["instance"] == bridge.instance and feed["now"] > 0
    out = io.BytesIO()
    Pil.new("RGBA", (3000, 1000), (0, 0, 0, 0)).save(out, "PNG")
    url = picture_url(out.getvalue())
    assert url is not None and url.startswith("data:image/jpeg;base64,")
    with Pil.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))) as picture:
        assert max(picture.size) == 1568
    assert picture_url(b"not a picture") is None
    names = [tool.name for tool in Bridge._tools()]
    assert names == ["discord_reply", "discord_send", "discord_confirm", "discord_status"]


def test_a_message_is_a_turn_answered_where_it_came_from() -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []
    turns: list[dict[str, Any]] = []

    async def handle(text: str, source: str, **kw: Any) -> TurnResult:
        turns.append({"text": text, "source": source, **kw})
        return TurnResult("t1", interrupted=False, text="It's 21 °C and sunny.")

    async def call(server: str, tool: str, args: dict[str, Any], ctx: Any) -> str:
        calls.append((server, tool, args))
        return "Answered on Discord (1 message)."

    async def transcribe(audio: bytes) -> str:
        return "and tomorrow?"

    host = SimpleNamespace(cfg=default_config(), bus=SimpleNamespace(publish=lambda *a, **k: None),
                           turns=SimpleNamespace(handle=handle), hub=SimpleNamespace(call=call),
                           voice=SimpleNamespace(transcribe=transcribe))
    runner = IntentRunner(host)  # type: ignore[arg-type]
    ogg = "data:audio/ogg;base64," + base64.b64encode(b"OggS...").decode()
    intent = Intent(5, "message", {"text": "What's the weather?", "audio": ogg, "images": [],
                                    "reply": {"tool": "discord_reply", "args": {"channel": "7", "message": "9"}}})
    asyncio.run(runner.run("discord", intent))
    assert turns == [{"text": "What's the weather?\nand tomorrow?", "source": "discord", "device": "discord",
                      "pictures": []}]
    assert calls == [("discord", "discord_reply", {"channel": "7", "message": "9", "text": "It's 21 °C and sunny."})]


@pytest.mark.parametrize("tool", ["discord_reply", "nope"])
def test_a_bad_call_is_said_not_raised(tool: str) -> None:
    bridge = Bridge("token", {42}, set())
    result = asyncio.run(bridge._call_tool(None, SimpleNamespace(name=tool, arguments={})))
    assert result.is_error and result.content[0].text.startswith("Error:")


# ---------------------------------------------------------------------------------------------------- nothing leaves with a secret
class Owner:
    """The user's direct messages, as the bot sees them."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str, view: Any = None) -> "Owner":
        self.sent.append(text)
        return self

    async def edit(self, **kw: Any) -> None:
        self.sent.append(kw["content"])


@pytest.fixture
def knowing() -> Iterator[Redactor]:
    """A process whose redactor knows the bot's token and the shipped shapes."""
    with redact.installed(Redactor(default_config().safety.secret_patterns, min_chars=8)) as redactor:
        redactor.learn([TOKEN])
        yield redactor


def bridge_with(owner: Owner) -> Bridge:
    bridge = Bridge(TOKEN, {42}, set())

    async def fetch_user(_id: int) -> Owner:
        return owner

    bridge.client = SimpleNamespace(fetch_user=fetch_user)  # type: ignore[assignment]
    return bridge


def test_a_direct_message_carries_no_secret_even_one_split_across_the_cut(knowing: Redactor) -> None:
    owner = Owner()
    bridge = bridge_with(owner)
    text = ("word " * 395) + f"the token is {TOKEN}, and a key sk-or-v1-{'0123456789abcdef' * 4} too"  # the token sits at the 2000 cut
    assert asyncio.run(bridge.send(text, title=f"About {TOKEN}")) == "Sent on Discord."
    assert len(owner.sent) >= 2 and owner.sent[0].startswith("**About [secret]**")
    assert TOKEN not in "".join(owner.sent) and "sk-or-v1" not in "".join(owner.sent)
    assert "the token is [secret], and a key [secret] too" in "".join(owner.sent)


def test_a_reply_carries_no_secret(knowing: Redactor) -> None:
    sent: list[str] = []

    class Message:
        async def reply(self, text: str, mention_author: bool) -> None:
            sent.append(text)

    class Channel:
        def get_partial_message(self, _id: int) -> Message:
            return Message()

        async def send(self, text: str) -> None:
            sent.append(text)

    bridge = Bridge(TOKEN, {42}, set())
    bridge.client = SimpleNamespace(get_channel=lambda _id: Channel())  # type: ignore[assignment]
    asyncio.run(bridge.reply(7, 9, f"Your token: {TOKEN}"))
    assert sent == ["Your token: [secret]"]


def test_a_question_carries_no_secret_in_its_words_or_its_detail(knowing: Redactor) -> None:
    owner = Owner()
    bridge = bridge_with(owner)
    answer = asyncio.run(bridge.confirm(f"Send {TOKEN} to the site?", f"POST body: {TOKEN}", timeout_s=0.01))  # type: ignore[arg-type]
    assert answer == "timeout"
    assert TOKEN not in "".join(owner.sent) and owner.sent[0] == "Send [secret] to the site?\n```\nPOST body: [secret]\n```"
    assert owner.sent[1] == "Send [secret] to the site?\n*No answer: not done.*"


def test_the_bot_protects_itself_when_it_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    """`serve` hides the bot's own token (from its env) before anything else, then stops for a missing user as before."""
    assert Redactor(default_config().safety.secret_patterns, min_chars=8).redact(TOKEN) == TOKEN
    factory = logging.getLogRecordFactory()
    monkeypatch.setenv("DISCORD_TOKEN", TOKEN)
    monkeypatch.delenv("ORION_DISCORD_USERS", raising=False)
    try:
        with redact.installed(redact.active()):
            with pytest.raises(SystemExit, match="ORION_DISCORD_USERS isn't set"):
                asyncio.run(discord_bridge.serve())
            assert redact.redacted(f"login with {TOKEN}") == "login with [secret]"
    finally:
        logging.setLogRecordFactory(factory)
