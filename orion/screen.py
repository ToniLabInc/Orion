"""This machine's screen, captured the way its desktop allows (``orion.host`` says which).

- "portal": the desktop's screenshot portal over D-Bus (GNOME, KDE and others on Wayland). The first capture asks
  the user once whether apps may take screenshots; later ones are silent. The portal saves a PNG, which is read and
  deleted.
- "spectacle": KDE's own tool, in the background.
- "grim": wlroots compositors (Sway, Hyprland...), straight to its output.
- "mss": X11, macOS (with the Screen Recording permission) and Windows.

The capture is returned as the picture's bytes; ``orion.images`` prepares it for the models.
"""

import asyncio
import contextlib
import secrets
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

PORTAL_PATH = "/org/freedesktop/portal/desktop"
PORTAL_NAME = "org.freedesktop.portal.Desktop"


class ScreenError(RuntimeError):
    """The screen couldn't be captured; the message says why, in words."""


async def capture(backend: str, *, timeout_s: float) -> bytes:
    """The whole screen, as a picture's bytes (PNG).

    Args:
        backend: how (``HostProfile.screen``).
        timeout_s: how long it may take (the portal's first capture waits for the user's answer).

    Returns:
        The picture.

    Raises:
        ScreenError: there is no way here, the user said no, or it failed.
    """
    try:
        async with asyncio.timeout(timeout_s):
            match backend:
                case "portal":
                    return await _portal()
                case "grim":
                    return await _program(["grim", "-"])
                case "spectacle":
                    return await _to_file(["spectacle", "--background", "--nonotify", "--fullscreen", "--output"])
                case "mss":
                    return await asyncio.to_thread(_mss)
    except TimeoutError:
        raise ScreenError(f"the screen wasn't captured within {timeout_s:g} s") from None
    raise ScreenError("this machine has no screen to capture")


def _mss() -> bytes:
    """Every monitor as one picture, through mss."""
    import mss
    import mss.tools

    try:
        with mss.mss() as grabber:
            shot = grabber.grab(grabber.monitors[0])
            return mss.tools.to_png(shot.rgb, shot.size) or b""
    except mss.exception.ScreenShotError as e:
        raise ScreenError(f"the screen couldn't be read ({e})") from e


async def _program(command: list[str]) -> bytes:
    """A capture program that writes the picture to its output."""
    process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE,
                                                   stderr=asyncio.subprocess.PIPE)
    out, err = await process.communicate()
    if process.returncode != 0 or not out:
        raise ScreenError(f"{command[0]} failed: {err.decode(errors='replace').strip() or process.returncode}")
    return out


async def _to_file(command: list[str]) -> bytes:
    """A capture program that writes the picture to a file named last on its command line."""
    with tempfile.TemporaryDirectory(prefix="orion-screen-") as folder:
        path = Path(folder) / "screen.png"
        process = await asyncio.create_subprocess_exec(*command, str(path), stdout=asyncio.subprocess.DEVNULL,
                                                       stderr=asyncio.subprocess.PIPE)
        _, err = await process.communicate()
        if process.returncode != 0 or not path.is_file():
            raise ScreenError(f"{command[0]} failed: {err.decode(errors='replace').strip() or process.returncode}")
        return path.read_bytes()


async def _portal() -> bytes:
    """A screenshot through the desktop's portal (org.freedesktop.portal.Screenshot), without its own dialog."""
    from jeepney import DBusAddress, MatchRule, new_method_call
    from jeepney.bus_messages import message_bus
    from jeepney.io.asyncio import open_dbus_router

    portal = DBusAddress(PORTAL_PATH, bus_name=PORTAL_NAME, interface="org.freedesktop.portal.Screenshot")
    try:
        async with open_dbus_router() as router:
            token = "orion" + secrets.token_hex(4)
            sender = (router.unique_name or "").lstrip(":").replace(".", "_")
            rule = MatchRule(type="signal", interface="org.freedesktop.portal.Request", member="Response",
                             path=f"{PORTAL_PATH}/request/{sender}/{token}")
            await router.send_and_get_reply(message_bus.AddMatch(rule))
            with router.filter(rule) as answers:  # subscribed before asking: the answer can't be missed
                await router.send_and_get_reply(new_method_call(
                    portal, "Screenshot", "sa{sv}", ("", {"handle_token": ("s", token), "interactive": ("b", False)})))
                code, results = (await answers.get()).body
    except (OSError, KeyError) as e:
        raise ScreenError(f"the desktop's screenshot portal isn't reachable ({e})") from e
    if code == 1:
        raise ScreenError("the desktop said no to screenshots (it asks once: allow it to let Orion see the screen)")
    uri = results.get("uri", ("s", ""))[1] if code == 0 else ""
    if not uri:
        raise ScreenError("the desktop's screenshot portal failed")
    path = Path(unquote(urlsplit(uri).path))
    try:
        return path.read_bytes()
    finally:
        with contextlib.suppress(OSError):
            path.unlink()  # the portal saved it among the user's pictures: it was only for Orion
