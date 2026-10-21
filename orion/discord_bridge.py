"""Orion on Discord: an MCP server (``orion discord-serve``, over stdio) that is a Discord bot.

You talk to Orion by direct message, or by mentioning the bot in a channel you allow; he answers in the same place.
Only the Discord users named in ``ORION_DISCORD_USERS`` are heard (everyone else is ignored), so a bot others can
reach still answers you alone.

How it reaches Orion (the same path the hand-gesture camera uses): each message you send becomes an entry of the
resource ``orion://intents`` (``intents.py``'s contract: ids from this machine's clock in milliseconds, the run's
``instance``, its ``now``), and every new one sends ``notifications/resources/updated`` to the listeners. The entry
is a "message" intent: your words, pictures you attached (sized down), a voice message's audio, and how to answer
(``reply``: the ``discord_reply`` tool with the channel and message). Orion runs the request as a turn, then calls
that tool with his reply. His tools here:

- ``discord_reply``: answer a message (long answers are split as Discord needs);
- ``discord_send``: send you a direct message (a job's result, a warning, "message me when...");
- ``discord_confirm``: ask you yes or no with two buttons (his question before a risky action), and wait;
- ``discord_status``: whether the bot is connected, and as whom.

Settings come from the environment (Orion's ``mcp_servers.discord.env``, ``${VAR}``s filled in):
``DISCORD_TOKEN`` (the bot's token), ``ORION_DISCORD_USERS`` (your user id, or several, comma-separated) and
``ORION_DISCORD_CHANNELS`` (channel ids where a mention is heard; empty: direct messages only). The bot needs no
privileged intent: Discord gives a bot the words of direct messages and of messages that mention it.

What the bot sends is the last place a secret could leave by: every message passes the redactor first (``orion.redact``:
the bot's own token, and whatever looks like a secret by the shapes in ``safety.secret_patterns``), whatever Orion asked
it to say. The hub already refuses a call whose arguments hold a secret (``mcp.py``); this second lock is inside the bot,
which holds a token itself, so no message can carry it out even if a call got past the hub.
"""

import asyncio
import base64
import io
import json
import logging
import os
import re
import secrets
import time
from collections import deque
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, Final

from orion import redact

if TYPE_CHECKING:
    import discord

log = logging.getLogger("orion.discord")

INTENTS_URI: Final = "orion://intents"
FEED_KEEP: Final = 12  # entries the feed keeps (pictures and audio make them large)
MESSAGE_CHARS: Final = 2000  # Discord's limit for one message
PICTURE_SIDE: Final = 1568  # pictures are sized down to this before they go to Orion
PICTURE_QUALITY: Final = 82
MAX_ATTACHMENT_BYTES: Final = 12_000_000  # bigger attachments are left out
TYPING_S: Final = 120.0  # the bot shows "typing" this long at most while Orion works on an answer
COMPACT: Final = json.JSONEncoder(separators=(",", ":"))


def ids_of(text: str) -> set[int]:
    """Discord ids from a comma- or space-separated list (anything that isn't a number is ignored)."""
    return {int(part) for part in re.split(r"[,\s]+", text or "") if part.isdigit()}


def split_message(text: str, limit: int = MESSAGE_CHARS) -> list[str]:
    """A reply cut into Discord-sized messages, at a paragraph, then a line, then a space; a code block cut in two
    is closed and reopened, so each piece renders.

    Args:
        text: the reply.
        limit: the most characters a message may hold.

    Returns:
        The pieces, in order (none for an empty reply).
    """
    pieces: list[str] = []
    rest = text.strip()
    fence = ""  # the code fence open at the end of the last piece ("```py"), reopened in the next
    while rest:
        room = limit - len(fence) - 5  # a reopened fence in front, a closing one at the end
        if len(rest) <= room:
            pieces.append(fence + rest)
            break
        cut = max(rest.rfind("\n\n", 0, room), rest.rfind("\n", 0, room), rest.rfind(" ", 0, room))
        cut = cut if cut > room // 2 else room
        piece, rest = fence + rest[:cut].rstrip(), rest[cut:].lstrip("\n ")
        fences = re.findall(r"^```(\S*)", piece, flags=re.MULTILINE)
        if len(fences) % 2 == 1:
            piece += "\n```"
            fence = f"```{fences[-1]}\n"
        else:
            fence = ""
        pieces.append(piece)
    return [p for p in pieces if p.strip()]


def picture_url(data: bytes) -> str | None:
    """An attached picture as a JPEG data URL, sized down (None when it isn't a picture Pillow reads)."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as opened:
            picture = ImageOps.exif_transpose(opened).convert("RGB")
    except (UnidentifiedImageError, OSError):
        return None
    picture.thumbnail((PICTURE_SIDE, PICTURE_SIDE))
    out = io.BytesIO()
    picture.save(out, "JPEG", quality=PICTURE_QUALITY, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii")


class Bridge:
    """The bot and the MCP server it serves (see the module docstring)."""

    def __init__(self, token: str, users: set[int], channels: set[int]) -> None:
        import mcp_types as types
        from mcp.server import Server
        from mcp.server.subscriptions import InMemorySubscriptionBus, ListenHandler

        self.token, self.users, self.channels = token, users, channels
        self.feed: deque[dict[str, Any]] = deque(maxlen=FEED_KEEP)
        self.instance = secrets.token_hex(6)
        self.last_id = 0
        self.bus = InMemorySubscriptionBus()
        self._listen = ListenHandler(self.bus, max_subscriptions=8)
        self._typing: dict[int, asyncio.Task[None]] = {}  # channel -> its "typing" while Orion answers
        self.client = self._client()
        self.server: Server[Any] = Server(
            "orion-discord", version="1", title="Discord",
            description="Talk to Orion on Discord: your direct messages and mentions reach him; he answers there.",
            instructions="Messages from the user on Discord arrive through orion://intents (intent 'message'); answer "
                         "each with discord_reply. discord_send messages the user; discord_confirm asks yes or no.",
            on_list_tools=lambda _ctx, _params: self._ok(types.ListToolsResult(tools=list(self._tools()))),
            on_call_tool=self._call_tool,
            on_list_resources=lambda _ctx, _params: self._ok(types.ListResourcesResult(resources=[types.Resource(
                uri=INTENTS_URI, name="intents", title="Messages", mime_type="application/json",
                description="The latest messages from the user: {intents: [{id, intent: message, args: {text, user, "
                            "images, audio, reply}, ts}], instance, now}.")])),
            on_read_resource=self._read_resource,
            on_subscriptions_listen=lambda ctx, params: self._listen(ctx, params))

    @staticmethod
    async def _ok[T](result: T) -> T:
        return result

    # ------------------------------------------------------------------------------------------------ Discord
    def _client(self) -> "discord.Client":
        import discord

        client = discord.Client(intents=discord.Intents.default())  # DMs and mentions carry their words without
        bridge = self                                               # the privileged message-content intent

        @client.event
        async def on_ready() -> None:
            log.info("connected to Discord as %s", client.user)

        @client.event
        async def on_message(message: "discord.Message") -> None:
            await bridge.on_message(message)

        return client

    def heard(self, author_id: int, *, is_bot: bool, direct: bool, mentioned: bool, channel_id: int) -> bool:
        """Whether a message is for Orion: from an allowed user, a direct message or a mention where allowed."""
        if is_bot or author_id not in self.users:
            return False
        return direct or (mentioned and channel_id in self.channels)

    async def on_message(self, message: "discord.Message") -> None:
        """A message: when it is for Orion, add it to the feed and tell the listeners."""
        import discord

        client = self.client
        direct = isinstance(message.channel, discord.DMChannel)
        mentioned = client.user is not None and client.user in message.mentions
        if not self.heard(message.author.id, is_bot=message.author.bot, direct=direct, mentioned=mentioned,
                          channel_id=message.channel.id):
            return
        text = message.content
        if client.user is not None:
            text = re.sub(rf"<@!?{client.user.id}>", "", text).strip()
        images, audio = await self._attachments(message)
        if not text and not images and not audio:
            return
        args: dict[str, Any] = {"text": text, "user": message.author.display_name, "images": images,
                                "reply": {"tool": "discord_reply",
                                          "args": {"channel": str(message.channel.id), "message": str(message.id)}}}
        if audio:
            args["audio"] = audio
        self.last_id = max(self.last_id + 1, int(time.time() * 1000))
        self.feed.append({"id": self.last_id, "intent": "message", "args": args, "ts": time.time()})
        self._start_typing(message.channel)
        await self.bus.publish(self._updated())
        log.info("a message from %s (%d characters%s%s)", message.author, len(text),
                 f", {len(images)} picture(s)" if images else "", ", a voice message" if audio else "")

    async def _attachments(self, message: "discord.Message") -> tuple[list[str], str]:
        """The pictures (data URLs) and the voice message's audio (an Ogg data URL) a message carries."""
        images: list[str] = []
        audio = ""
        for attachment in message.attachments:
            if attachment.size > MAX_ATTACHMENT_BYTES:
                continue
            kind = (attachment.content_type or "").split(";")[0]
            if kind.startswith("image/"):
                if (url := picture_url(await attachment.read())) is not None:
                    images.append(url)
            elif kind.startswith("audio/") or attachment.is_voice_message():
                data = await attachment.read()
                audio = f"data:{kind or 'audio/ogg'};base64," + base64.b64encode(data).decode("ascii")
        return images, audio

    def _start_typing(self, channel: Any) -> None:
        """Show "Orion is typing" in a channel until the answer comes (or a while passed)."""
        async def typing() -> None:
            async with channel.typing():
                await asyncio.sleep(TYPING_S)

        if (task := self._typing.get(channel.id)) is not None:
            task.cancel()
        self._typing[channel.id] = asyncio.get_running_loop().create_task(typing())

    def _stop_typing(self, channel_id: int) -> None:
        if (task := self._typing.pop(channel_id, None)) is not None:
            task.cancel()

    def _updated(self) -> Any:
        from mcp.server.subscriptions import ResourceUpdated

        return ResourceUpdated(uri=INTENTS_URI)

    # ------------------------------------------------------------------------------------------------ the MCP side
    def feed_text(self) -> str:
        """orion://intents."""
        return COMPACT.encode({"intents": list(self.feed), "instance": self.instance, "now": int(time.time() * 1000)})

    async def _read_resource(self, _ctx: Any, params: Any) -> Any:
        import mcp_types as types
        from mcp.shared.exceptions import MCPError

        uri = str(params.uri)
        if uri != INTENTS_URI:
            raise MCPError(code=types.INVALID_PARAMS, message=f"unknown resource {uri}; there is {INTENTS_URI}")
        return types.ReadResourceResult(contents=[types.TextResourceContents(
            uri=uri, mime_type="application/json", text=self.feed_text())])

    @staticmethod
    def _tools() -> Iterable[Any]:
        import mcp_types as types

        def tool(name: str, title: str, description: str, properties: dict[str, Any], required: list[str], *,
                 read_only: bool = False, destructive: bool = False) -> types.Tool:
            return types.Tool(name=name, title=title, description=description, input_schema={
                "type": "object", "properties": properties, "required": required, "additionalProperties": False},
                annotations=types.ToolAnnotations(read_only_hint=read_only, destructive_hint=destructive,
                                                  idempotent_hint=False, open_world_hint=True))

        text = {"type": "string", "description": "The text (Markdown)."}
        yield tool("discord_reply", "Answer on Discord", "Answer a Discord message where it was sent.",
                   {"channel": {"type": "string"}, "message": {"type": "string"}, "text": text},
                   ["channel", "message", "text"], destructive=True)
        yield tool("discord_send", "Message the user on Discord",
                   "Send the user a direct message on Discord (a result, a warning, something they asked to be told).",
                   {"text": text, "title": {"type": "string", "description": "A short title in bold, if any."}},
                   ["text"], destructive=True)
        yield tool("discord_confirm", "Ask the user on Discord",
                   "Ask the user a yes/no question on Discord with two buttons and wait for the answer: yes, no or "
                   "timeout.",
                   {"question": {"type": "string"}, "detail": {"type": "string"},
                    "timeout_s": {"type": "integer", "minimum": 5, "maximum": 900}}, ["question"])
        yield tool("discord_status", "Discord status", "Whether the Discord bot is connected, and as whom.", {}, [],
                   read_only=True)

    async def _call_tool(self, _ctx: Any, params: Any) -> Any:
        import mcp_types as types

        args = dict(params.arguments or {})
        try:
            match params.name:
                case "discord_reply":
                    text = await self.reply(int(args["channel"]), int(args["message"]), str(args["text"]))
                case "discord_send":
                    text = await self.send(str(args["text"]), str(args.get("title") or ""))
                case "discord_confirm":
                    text = await self.confirm(str(args["question"]), str(args.get("detail") or ""),
                                              int(args.get("timeout_s") or 60))
                case "discord_status":
                    user = self.client.user
                    text = f"Connected as {user}." if user is not None and self.client.is_ready() else \
                        "Not connected to Discord (check the token)."
                case _:
                    raise ValueError(f"there is no tool called {params.name}")
        except (KeyError, ValueError, RuntimeError) as e:
            return types.CallToolResult(content=[types.TextContent(type="text", text=f"Error: {e}")], is_error=True)
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)])

    # ------------------------------------------------------------------------------------------------ actions
    async def reply(self, channel_id: int, message_id: int, text: str) -> str:
        """Answer a message, split as Discord needs."""
        import discord

        self._stop_typing(channel_id)
        channel = self.client.get_channel(channel_id) or await self.client.fetch_channel(channel_id)
        pieces = split_message(redact.redacted(text)) or ["…"]  # (before the cuts: a secret isn't split in two)
        try:
            first = channel.get_partial_message(message_id)  # type: ignore[union-attr]
            await first.reply(pieces[0], mention_author=False)
            for piece in pieces[1:]:
                await channel.send(piece)  # type: ignore[union-attr]
        except discord.HTTPException as e:
            raise RuntimeError(f"Discord refused the answer ({e})") from e
        return f"Answered on Discord ({len(pieces)} message{'s' * (len(pieces) != 1)})."

    async def _owner(self) -> "discord.User":
        if not self.users:
            raise RuntimeError("no Discord user is set (ORION_DISCORD_USERS)")
        return await self.client.fetch_user(min(self.users))

    async def send(self, text: str, title: str = "") -> str:
        """Send the user a direct message."""
        import discord

        body = redact.redacted(f"**{title}**\n{text}" if title else text)
        try:
            owner = await self._owner()
            for piece in split_message(body):
                await owner.send(piece)
        except discord.HTTPException as e:
            raise RuntimeError(f"Discord refused the message ({e})") from e
        return "Sent on Discord."

    async def confirm(self, question: str, detail: str, timeout_s: int) -> str:
        """Ask the user yes or no with two buttons; "yes", "no" or "timeout"."""
        import discord

        users = self.users
        question, detail = redact.redacted(question), redact.redacted(detail)
        answer: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

        class Ask(discord.ui.View):
            async def interaction_check(self, interaction: discord.Interaction) -> bool:
                return interaction.user.id in users

            async def _answer(self, interaction: discord.Interaction, yes: bool) -> None:
                await interaction.response.edit_message(content=f"{question}\n**{'Yes' if yes else 'No'}.**",
                                                        view=None)
                if not answer.done():
                    answer.set_result(yes)

            @discord.ui.button(label="Yes", style=discord.ButtonStyle.success)
            async def yes(self, interaction: discord.Interaction, _button: discord.ui.Button[Any]) -> None:
                await self._answer(interaction, True)

            @discord.ui.button(label="No", style=discord.ButtonStyle.danger)
            async def no(self, interaction: discord.Interaction, _button: discord.ui.Button[Any]) -> None:
                await self._answer(interaction, False)

        body = question + (f"\n```\n{detail[:1500]}\n```" if detail else "")
        try:
            owner = await self._owner()
            sent = await owner.send(body, view=Ask(timeout=timeout_s))
        except discord.HTTPException as e:
            raise RuntimeError(f"Discord refused the question ({e})") from e
        try:
            async with asyncio.timeout(timeout_s):
                return "yes" if await answer else "no"
        except TimeoutError:
            await sent.edit(content=f"{question}\n*No answer: not done.*", view=None)
            return "timeout"


async def serve() -> None:
    """Run the bot and its MCP server over stdio until the client goes away."""
    from mcp.server.stdio import stdio_server

    from orion.config import load_config
    from orion.secrets import protect

    protect(load_config().safety, None, os.environ)  # the bot's token (its env) and the shapes of secrets: hidden in what it sends and logs
    token = os.environ.get("DISCORD_TOKEN", "").strip()
    users = ids_of(os.environ.get("ORION_DISCORD_USERS", ""))
    if not token:
        raise SystemExit("DISCORD_TOKEN isn't set: give the bot's token in the Discord server's env")
    if not users:
        raise SystemExit("ORION_DISCORD_USERS isn't set: give your Discord user id, so only you are heard")
    bridge = Bridge(token, users, ids_of(os.environ.get("ORION_DISCORD_CHANNELS", "")))
    bot = asyncio.get_running_loop().create_task(bridge.client.start(token), name="discord")
    try:
        async with stdio_server() as (read, write):
            await bridge.server.run(read, write, bridge.server.create_initialization_options())
    finally:
        await bridge.client.close()
        bot.cancel()
