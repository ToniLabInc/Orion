"""His eyes and the clipboard, on the device you use: served inside Orion's own process, next to his controls,
because they reach the HUDs connected now (``orion.surfaces``) as well as this machine.

What "your device" is follows the request: the HUD it was typed in, or the one carrying the voice; a request said to
this machine's own microphone means this machine. So, wherever Orion runs (a desktop, a laptop, a server, a
container), "what's on my screen?" looks at the screen you're in front of:

- the eyes: a screen or camera your HUD shares now; this machine's own screen when the request is from here (its
  desktop allows it: ``orion.host``, ``orion.screen``); otherwise your HUD is asked to share (you click Share,
  since browsers only let a person start that);
- the clipboard: your HUD's (its browser asks you to allow reading it once), or this machine's.
"""

import asyncio
import base64
import logging
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal

from pydantic import Field

from orion import images
from orion.base import GroupInfo
from orion.host import profile
from orion.screen import ScreenError, capture
from orion.surfaces import DeviceError, Surface
from orion.tools import Group, current_call

if TYPE_CHECKING:
    from orion.runtime import Runtime

log = logging.getLogger("orion.tools.device")

EYES: Final = GroupInfo("eyes", "Eyes", "What he sees: the screen of the device you use (or of this machine) and its "
                                        "camera, to read an error, describe a page, or look at something you hold up.")
CLIPBOARD: Final = GroupInfo("clipboard", "Clipboard", "The clipboard of the device you use: read what you copied, "
                                                       "and copy text for you to paste.")


def groups(rt: "Runtime") -> list[Group]:
    """The eyes and the clipboard, bound to this running Orion.

    Args:
        rt: the running Orion (its HUDs, its settings).

    Returns:
        The two groups.
    """
    eyes, clipboard = Group(EYES), Group(CLIPBOARD)

    def device_of_call() -> tuple[str, Surface | None]:
        """The request's device: its id, and its HUD when it is one."""
        device = current_call().device
        return device, rt.surfaces.get(device) if device else None

    def here(surface: Surface | None, device: str) -> bool:
        """Whether the request is from this machine itself (its microphone, or a HUD in its own browser)."""
        return not device or (surface is not None and surface.local)

    async def frame(surface: Surface, what: str) -> tuple[bytes, str]:
        """A frame of what a HUD shares, as JPEG bytes, and its label."""
        cfg = rt.cfg.eyes
        answer = await rt.surfaces.ask(surface, "frame", what=what, timeout_s=cfg.frame_timeout_s)
        picture = images.from_base64(str(answer.get("image") or ""), label=f"{surface.device}'s {what}",
                                     max_side=cfg.image_max_side, quality=cfg.image_quality)
        return base64.b64decode(picture["data"]), picture["label"]

    async def shared(what: Literal["screen", "camera"]) -> tuple[bytes, str]:
        """A picture of the screen or camera, from wherever the request's device allows (see the module docstring).

        Raises:
            DeviceError: nothing can show it.
        """
        cfg = rt.cfg.eyes
        device, surface = device_of_call()
        if surface is not None and what in surface.sharing:
            return await frame(surface, what)  # the request's own HUD shares it now
        host = profile()
        if what == "screen" and host.screen and here(surface, device):
            try:
                data = await capture(host.screen, timeout_s=cfg.capture_timeout_s)
            except ScreenError as e:
                raise DeviceError(str(e)) from e
            return data, "this machine's screen"
        if (other := rt.surfaces.pick(what, sharing=what)) is not None:
            return await frame(other, what)  # another HUD shares it now (a phone's camera, say)
        if device and surface is None:  # from a chat (Discord): nobody at a HUD to click Share
            raise DeviceError(f"no HUD shares its {what} now, and a request from {device} can't ask one to")
        asked = surface if surface is not None and what in surface.caps else rt.surfaces.pick(what)
        if asked is None:
            where = "this machine has no desktop to look at, and " if what == "screen" else ""
            raise DeviceError(f"{where}no HUD that could share its {what} is open")
        await rt.surfaces.ask(asked, "share", what=what, timeout_s=cfg.share_wait_s)
        return await frame(asked, what)

    @eyes.tool(read_only=True, title="Look at the screen or camera",
               examples=("What's on my screen?", "Read this error", "What am I holding?", "Look at this page"))
    async def look(what: Annotated[Literal["screen", "camera"], Field(
            description='"screen" for what the user sees on their screen, "camera" to see them or something they '
                        'hold up.')] = "screen") -> list[Any] | str:
        """See the user's screen or camera as it is now: returns a picture of it. Use it whenever the user asks
        about something on their screen (an error, a page, a window) or wants you to look at something."""
        from mcp.server.mcpserver import Image as McpImage

        try:
            data, label = await shared(what)
        except DeviceError as e:
            return f"Error: I can't see the {what}: {e}."
        return [f"Here is {label}, now.", McpImage(data=data, format="jpeg" if data[:2] == b"\xff\xd8" else "png")]

    @clipboard.tool(read_only=True, title="Read the clipboard", examples=("What did I copy?", "Read my clipboard"))
    async def clipboard_read() -> str:
        """Read the text on the clipboard of the device the user is using (what they last copied)."""
        device, surface = device_of_call()
        cfg = rt.cfg.eyes
        try:
            if surface is not None and "clipboard" in surface.caps:
                answer = await rt.surfaces.ask(surface, "clipboard_read", timeout_s=cfg.frame_timeout_s)
                text = str(answer.get("text") or "")
            elif here(surface, device) and profile().clipboard:
                text = await asyncio.to_thread(_paste)
            else:
                return "Error: the device you're using has no clipboard I can reach."
        except (DeviceError, RuntimeError) as e:
            return f"Error: the clipboard couldn't be read: {e}."
        return text if text else "The clipboard is empty."

    @clipboard.tool(destructive=True, title="Copy to the clipboard",
                    examples=("Copy that", "Put the address on my clipboard"))
    async def clipboard_write(text: Annotated[str, Field(description="The text to copy.", max_length=100_000)]) -> str:
        """Put text on the clipboard of the device the user is using, ready for them to paste."""
        device, surface = device_of_call()
        cfg = rt.cfg.eyes
        try:
            if surface is not None and "clipboard" in surface.caps:
                await rt.surfaces.ask(surface, "clipboard_write", text=text, timeout_s=cfg.frame_timeout_s)
            elif here(surface, device) and profile().clipboard:
                await asyncio.to_thread(_copy, text)
            else:
                return "Error: the device you're using has no clipboard I can reach."
        except (DeviceError, RuntimeError) as e:
            return f"Error: the text couldn't be copied: {e}."
        return f"Copied {len(text)} characters to the clipboard."

    return [eyes, clipboard]


def _paste() -> str:
    """This machine's clipboard text (pyperclip: wl-paste, xclip or xsel on Linux, the system's elsewhere)."""
    import pyperclip

    try:
        return str(pyperclip.paste() or "")
    except pyperclip.PyperclipException as e:
        raise RuntimeError(str(e)) from e


def _copy(text: str) -> None:
    """Put text on this machine's clipboard."""
    import pyperclip

    try:
        pyperclip.copy(text)
    except pyperclip.PyperclipException as e:
        raise RuntimeError(str(e)) from e
