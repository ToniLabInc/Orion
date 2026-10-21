"""surfaces: the HUDs connected, what each offers, requests to a device and their answers; a browser's microphone and
speakers standing in for this machine's; and the eyes and clipboard serving the request's device."""

import asyncio
import base64
import io
import queue
import struct
import time
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from PIL import Image as Pil

from orion.base import EventBus
from orion.config import default_config
from orion.host import HostProfile
from orion.surfaces import DeviceError, Surfaces
from orion.tools import CallInfo, _call
from orion.tools import device as device_tools
from orion.voice import MIC_FRAME
from orion.voice.listener import PushedInput
from orion.voice.tts import AUDIO_FRAME, BLOCK, RemoteOutput


def surfaces() -> tuple[Surfaces, list[dict[str, Any]]]:
    bus = EventBus()
    published: list[dict[str, Any]] = []
    bus.subscribe(published.append)
    return Surfaces(bus), published


def test_hello_pick_and_the_most_recently_used_device() -> None:
    s, _ = surfaces()
    sent: list[dict[str, Any]] = []
    phone = s.add(local=False, send=sent.append, send_audio=lambda b: None)
    laptop = s.add(local=True, send=lambda e: None, send_audio=lambda b: None)
    s.hello(phone.id, "Safari on iPhone", ["mic", "speaker", "camera", "clipboard", "laser"])
    s.hello(laptop.id, "Firefox on Linux", ["mic", "speaker", "screen", "clipboard"])
    assert phone.caps == {"mic", "speaker", "camera", "clipboard"} and phone.device == "Safari on iPhone"
    assert s.pick("screen") is laptop and s.pick("camera") is phone
    s.touch(phone.id)
    assert s.pick("clipboard") is phone and s.pick("clipboard", laptop.id) is laptop
    assert s.pick("screen", sharing="screen") is None
    s.sharing(laptop.id, ["screen", "nonsense"])
    assert s.pick("screen", sharing="screen") is laptop and laptop.sharing == {"screen"}


def test_a_request_is_answered_by_its_device_only_and_fails_when_it_goes() -> None:
    async def run() -> tuple[dict[str, Any], str, list[str]]:
        s, _ = surfaces()
        sent: list[dict[str, Any]] = []
        gone: list[str] = []
        s.on_gone.append(gone.append)
        hud = s.add(local=True, send=sent.append, send_audio=lambda b: None)
        other = s.add(local=True, send=lambda e: None, send_audio=lambda b: None)
        asked = asyncio.create_task(s.ask(hud, "clipboard_read", timeout_s=2))
        await asyncio.sleep(0)
        request = sent[0]
        s.answered(other.id, {"type": "answer", "id": request["id"], "text": "forged"})  # not the one asked
        s.answered(hud.id, {"type": "answer", "id": request["id"], "text": "copied text"})
        answer = await asked
        lost = asyncio.create_task(s.ask(hud, "frame", what="screen", timeout_s=2))
        await asyncio.sleep(0)
        s.remove(hud.id)
        try:
            await lost
            why = ""
        except DeviceError as e:
            why = str(e)
        return answer, why, gone

    answer, why, gone = asyncio.run(run())
    assert answer == {"text": "copied text"} and why == "the device went away" and len(gone) == 1


def test_a_device_that_doesnt_answer_times_out() -> None:
    async def run() -> str:
        s, _ = surfaces()
        hud = s.add(local=True, send=lambda e: None, send_audio=lambda b: None)
        try:
            await s.ask(hud, "frame", timeout_s=0.05)
        except DeviceError as e:
            return str(e)
        return ""

    assert "didn't answer within 0.05 s" in asyncio.run(run())


def test_a_browsers_microphone_arrives_in_whole_frames() -> None:
    frames: queue.Queue[Any] = queue.Queue()
    mic = PushedInput()
    mic.push(np.ones(500, dtype=np.float32))  # not started: dropped
    mic.start(frames)
    mic.push(np.ones(1000, dtype=np.float32))
    mic.push(np.full(2000, 0.5, dtype=np.float32))
    got = [frames.get_nowait() for _ in range(frames.qsize())]
    assert [len(f) for f in got] == [MIC_FRAME, MIC_FRAME] and float(got[1][-1]) == 0.5
    mic.close()
    mic.push(np.ones(MIC_FRAME * 3, dtype=np.float32))
    assert frames.empty() and mic.closed


def test_a_browsers_speakers_get_timed_frames_and_drop_what_they_hold_on_a_stop() -> None:
    sent: list[bytes] = []
    flushed: list[int] = []
    output = RemoteOutput(send=sent.append, flush=flushed.append)
    pulls = {"n": 0}

    def fill(out: Any, frames: int, _t: Any, _s: Any) -> None:
        pulls["n"] += 1
        out[:, 0] = 0.25

    busy = {"on": True}
    stream = output.open(fill, busy=lambda: busy["on"])
    stream.start()
    time.sleep(0.3)
    busy["on"] = False
    time.sleep(0.1)
    count = len(sent)
    stream.abort()
    stream.close()
    assert 4 <= count <= 15  # the first four at once, then one every 40 ms
    frame = sent[0]
    assert frame[0] == AUDIO_FRAME and struct.unpack("<I", frame[1:5])[0] == 0 and len(frame) == 5 + BLOCK * 2
    assert np.frombuffer(frame[5:], dtype="<i2")[0] == round(0.25 * 32767)
    assert flushed == [1] and output.gen == 1 and len(sent) == count  # silence isn't sent


def jpeg_url() -> str:
    out = io.BytesIO()
    Pil.new("RGB", (32, 20), (200, 30, 30)).save(out, "JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()


def runtime(monkeypatch: pytest.MonkeyPatch, host: HostProfile) -> tuple[SimpleNamespace, Surfaces]:
    s, _ = surfaces()
    monkeypatch.setattr(device_tools, "profile", lambda: host)
    return SimpleNamespace(surfaces=s, cfg=default_config()), s


DESKTOP = HostProfile("linux", False, "wayland", "GNOME", True, "xdg-open", "wpctl", True, False, False, False, "wl",
                      "portal")
SERVER = HostProfile("linux", True, "", "", False, "", "", False, False, False, False, "", "")


def tools(rt: SimpleNamespace) -> dict[str, Any]:
    return {name: fn for group in device_tools.groups(rt) for name, (fn, _) in group.tools.items()}  # type: ignore[arg-type]


def answering(s: Surfaces, surface_id: str, reply: dict[str, Any]) -> Any:
    """A HUD that answers every request it gets with `reply`."""
    def send(event: dict[str, Any]) -> None:
        if event["type"] == "request":
            asyncio.get_running_loop().call_soon(s.answered, surface_id, {"type": "answer", "id": event["id"],
                                                                          **reply(event)})
    return send


def test_the_eyes_look_at_the_requests_own_shared_screen_first(monkeypatch: pytest.MonkeyPatch) -> None:
    rt, s = runtime(monkeypatch, DESKTOP)
    hud = s.add(local=False, send=lambda e: None, send_audio=lambda b: None)
    hud.send = answering(s, hud.id, lambda e: {"image": jpeg_url()})
    s.hello(hud.id, "Chrome on Windows", ["screen"])
    s.sharing(hud.id, ["screen"])
    token = _call.set(CallInfo("brain", None, device=hud.id))
    try:
        result = asyncio.run(tools(rt)["look"]("screen"))
    finally:
        _call.reset(token)
    assert result[0] == "Here is Chrome on Windows's screen, now." and result[1].data[:2] == b"\xff\xd8"


def test_the_eyes_use_this_machines_screen_for_a_request_from_here(monkeypatch: pytest.MonkeyPatch) -> None:
    rt, _ = runtime(monkeypatch, DESKTOP)

    async def shot(backend: str, *, timeout_s: float) -> bytes:
        assert backend == "portal"
        return b"\x89PNG..."

    monkeypatch.setattr(device_tools, "capture", shot)
    result = asyncio.run(tools(rt)["look"]("screen"))
    assert result[0] == "Here is this machine's screen, now." and result[1].data == b"\x89PNG..."


def test_on_a_server_the_eyes_ask_a_hud_to_share_or_say_why_they_cant(monkeypatch: pytest.MonkeyPatch) -> None:
    rt, s = runtime(monkeypatch, SERVER)
    assert asyncio.run(tools(rt)["look"]("screen")) == \
        "Error: I can't see the screen: this machine has no desktop to look at, and no HUD that could share its " \
        "screen is open."
    phone = s.add(local=False, send=lambda e: None, send_audio=lambda b: None)
    kinds: list[str] = []

    def reply(event: dict[str, Any]) -> dict[str, Any]:
        kinds.append(event["kind"])
        return {} if event["kind"] == "share" else {"image": jpeg_url()}

    phone.send = answering(s, phone.id, reply)
    s.hello(phone.id, "Safari on iPhone", ["camera"])
    token = _call.set(CallInfo("brain", None, device=phone.id))
    try:
        result = asyncio.run(tools(rt)["look"]("camera"))
    finally:
        _call.reset(token)
    assert kinds == ["share", "frame"] and result[0] == "Here is Safari on iPhone's camera, now."


def test_the_clipboard_of_the_requests_device_or_of_this_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    rt, s = runtime(monkeypatch, DESKTOP)
    hud = s.add(local=False, send=lambda e: None, send_audio=lambda b: None)
    written: list[str] = []
    hud.send = answering(s, hud.id, lambda e: written.append(e.get("text", "")) or {"text": "from the phone"})
    s.hello(hud.id, "a phone", ["clipboard"])
    t = tools(rt)
    token = _call.set(CallInfo("brain", None, device=hud.id))
    try:
        assert asyncio.run(t["clipboard_read"]()) == "from the phone"
        assert asyncio.run(t["clipboard_write"]("hello")) == "Copied 5 characters to the clipboard."
    finally:
        _call.reset(token)
    assert written[-1] == "hello"
    monkeypatch.setattr(device_tools, "_paste", lambda: "from this machine")
    assert asyncio.run(t["clipboard_read"]()) == "from this machine"  # said to this machine's microphone
    rt2, _ = runtime(monkeypatch, SERVER)
    assert asyncio.run(tools(rt2)["clipboard_read"]()).startswith("Error: the device you're using has no clipboard")
