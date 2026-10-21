"""The devices the user meets Orion on: every HUD open in a browser (this machine's, a laptop's, a phone's), and
what each can do for him.

A HUD says what its browser offers when it connects (``hello``): a microphone and speakers (it can carry his voice),
a screen or a camera to share (his eyes), a clipboard. Orion is then usable from wherever the user is, also when he
runs on a server or in a container with no sound and no desktop of his own:

- the voice: one device at a time carries the conversation (``VoiceSession.take_voice``), its microphone's samples
  and his voice going over the HUD's connection as binary frames;
- the eyes: a tool asks the device of the request (or the last one used) for a frame of what it shares;
- the clipboard: the device of the request reads or writes its own clipboard.

Requests to a device are numbered messages answered by the device (``ask`` and ``answered``), each with a time
limit, so a device that went away never leaves a tool waiting.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Final, Literal

from orion.base import JSON, Event, EventBus, new_id

log = logging.getLogger("orion.surfaces")

type Cap = Literal["mic", "speaker", "screen", "camera", "clipboard"]

CAPS: Final[frozenset[str]] = frozenset({"mic", "speaker", "screen", "camera", "clipboard"})
DEVICE_CHARS: Final = 80  # a device's name as its HUD says it


class DeviceError(RuntimeError):
    """A device couldn't do what was asked (not connected, said no, didn't answer in time); the message says it."""


@dataclass(eq=False)
class Surface:
    """One HUD connection.

    Attributes:
        id: its id ("hud-1a2b3c4d"), said to it in ``init``.
        local: the browser runs on this machine (it reached the loopback address).
        send: queues an event for this connection alone.
        send_audio: queues a binary frame (his voice) for this connection alone.
        device: its name, as the HUD says it ("Firefox on Linux").
        caps: what it offers.
        sharing: what it shares now ("screen", "camera").
        seen: when the user last did something there (epoch seconds).
    """

    id: str
    local: bool
    send: Callable[[Event], None]
    send_audio: Callable[[bytes], None]
    device: str = "a browser"
    caps: frozenset[str] = frozenset()
    sharing: frozenset[str] = frozenset()
    seen: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, JSON]:
        """The device as the HUDs list it."""
        return {"id": self.id, "device": self.device, "local": self.local, "caps": sorted(self.caps),
                "sharing": sorted(self.sharing), "seen": self.seen}


class Surfaces:
    """The HUDs connected now (see the module docstring). Use it on the event loop."""

    def __init__(self, bus: EventBus) -> None:
        """No device yet.

        Args:
            bus: the HUD's event bus (the list of devices is published on it).
        """
        self.bus = bus
        self._all: dict[str, Surface] = {}
        self._asks: dict[str, tuple[str, asyncio.Future[dict[str, JSON]]]] = {}  # request id -> (device, answer)
        self.on_gone: list[Callable[[str], None]] = []  # told when a device goes (the voice it carried comes home)

    # ------------------------------------------------------------------------------------------------ the list
    def add(self, *, local: bool, send: Callable[[Event], None], send_audio: Callable[[bytes], None]) -> Surface:
        """A HUD connected.

        Args:
            local: whether its browser runs on this machine.
            send: queues an event for it alone.
            send_audio: queues a binary frame for it alone.

        Returns:
            Its surface (what it offers comes with its ``hello``).
        """
        surface = Surface(id=f"hud-{new_id()[:8]}", local=local, send=send, send_audio=send_audio)
        self._all[surface.id] = surface
        return surface

    def hello(self, surface_id: str, device: object, caps: object) -> None:
        """A HUD said what its browser offers.

        Args:
            surface_id: the HUD.
            device: its name for people.
            caps: what it offers (unknown words are ignored).
        """
        surface = self._all.get(surface_id)
        if surface is None:
            return
        surface.device = str(device or "a browser")[:DEVICE_CHARS]
        surface.caps = frozenset(c for c in caps if isinstance(c, str) and c in CAPS) if isinstance(
            caps, Iterable) else frozenset()
        self.touch(surface_id)
        self._publish()

    def sharing(self, surface_id: str, what: object) -> None:
        """A HUD started or stopped sharing its screen or camera.

        Args:
            surface_id: the HUD.
            what: what it shares now ("screen", "camera").
        """
        surface = self._all.get(surface_id)
        if surface is None or not isinstance(what, Iterable):
            return
        surface.sharing = frozenset(w for w in what if w in ("screen", "camera"))
        self._publish()

    def remove(self, surface_id: str) -> None:
        """A HUD went away: its waiting requests fail, and whoever needs to know is told."""
        if self._all.pop(surface_id, None) is None:
            return
        for request, (device, answer) in list(self._asks.items()):
            if device == surface_id and not answer.done():
                answer.set_exception(DeviceError("the device went away"))
                self._asks.pop(request, None)
        for listener in self.on_gone:
            try:
                listener(surface_id)
            except Exception:
                log.exception("a listener of a device that went away failed")
        self._publish()

    def touch(self, surface_id: str) -> None:
        """The user did something on this HUD (a message, a click): it is the one in use."""
        surface = self._all.get(surface_id)
        if surface is not None:
            surface.seen = time.time()

    def get(self, surface_id: str) -> Surface | None:
        """A HUD by its id."""
        return self._all.get(surface_id)

    def all(self) -> list[Surface]:
        """Every HUD connected now, the most recently used first."""
        return sorted(self._all.values(), key=lambda s: s.seen, reverse=True)

    def pick(self, cap: str, prefer: str = "", *, sharing: str = "") -> Surface | None:
        """The device to ask: the preferred one (the request's) if it can, else the most recently used one that can.

        Args:
            cap: what it must offer ("screen", "clipboard"...).
            prefer: the request's device ("" for none).
            sharing: also require it to be sharing this now ("screen", "camera"; "" for no need).

        Returns:
            The device, or None.
        """
        def fits(s: Surface) -> bool:
            return cap in s.caps and (not sharing or sharing in s.sharing)

        chosen = self._all.get(prefer)
        if chosen is not None and fits(chosen):
            return chosen
        return next((s for s in self.all() if fits(s)), None)

    def snapshot(self) -> list[dict[str, JSON]]:
        """Every device, as the HUDs list them."""
        return [s.as_dict() for s in self.all()]

    # ------------------------------------------------------------------------------------------------ requests
    async def ask(self, surface: Surface, kind: str, *, timeout_s: float, **args: JSON) -> dict[str, JSON]:
        """Ask a device for something and wait for its answer (a frame of its screen, its clipboard...).

        Args:
            surface: the device.
            kind: what is asked ("frame", "clipboard_read", "clipboard_write", "share").
            timeout_s: how long to wait.
            **args: what the request carries.

        Returns:
            The device's answer (its message, without "type", "id" and "error").

        Raises:
            DeviceError: it said why it couldn't, went away, or didn't answer in time.
        """
        request = new_id()
        answer: asyncio.Future[dict[str, JSON]] = asyncio.get_running_loop().create_future()
        self._asks[request] = (surface.id, answer)
        surface.send({"type": "request", "ts": time.time(), "id": request, "kind": kind, **args})
        try:
            async with asyncio.timeout(timeout_s):
                return await answer
        except TimeoutError:
            raise DeviceError(f"{surface.device} didn't answer within {timeout_s:g} s") from None
        finally:
            self._asks.pop(request, None)

    def answered(self, surface_id: str, message: dict[str, JSON]) -> None:
        """A device answered a request (its ``answer`` message).

        Args:
            surface_id: the device that answered (only the one asked may answer).
            message: ``{"type": "answer", "id", "error"?, ...}``.
        """
        pending = self._asks.get(str(message.get("id") or ""))
        if pending is None or pending[0] != surface_id or pending[1].done():
            return
        if error := message.get("error"):
            pending[1].set_exception(DeviceError(str(error)))
            return
        pending[1].set_result({k: v for k, v in message.items() if k not in ("type", "id", "error")})

    def _publish(self) -> None:
        """Tell every HUD which devices are connected."""
        self.bus.publish("surfaces", items=self.snapshot())
