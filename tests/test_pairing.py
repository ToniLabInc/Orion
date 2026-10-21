"""Pairing and who the HUD answers: a code shown on the PC pairs one device, once, for a while and a few tries; a
device stays paired while it is used, its key kept only as a hash; and the envelope lets a device in only from the
local network, naming this PC, and once paired (the HUD's files and the pairing endpoints before that)."""

import asyncio
import json
import stat
from pathlib import Path
from typing import Any

import pytest

from orion.config import ServerConfig, default_config
from orion.pairing import COOKIE, DAY_S, Pairing
from orion.server import WS_NOT_LOCAL, WS_UNPAIRED, Envelope, add_pairing

PC = "192.168.1.10:8765"  # this PC on the network, as a device asks for it


class Clock:
    """A wall clock the test moves."""

    def __init__(self) -> None:
        self.now = 1_790_000_000.0

    def __call__(self) -> float:
        return self.now


def server(**changes: Any) -> ServerConfig:
    """The server settings as shipped, with some changed."""
    return default_config().server.model_copy(update=changes)


def test_a_code_pairs_one_device_once(tmp_path: Path) -> None:
    clock = Clock()
    pairing = Pairing(tmp_path, server(pair_tries=3, pair_code_s=120), clock)
    assert pairing.pair("12345678", "Phone") is None and pairing.waiting() is None  # no code waits
    code, ends = pairing.start()
    assert len(code) == 8 and code.isdigit() and ends == clock.now + 120 and pairing.waiting() == ends
    wrong = f"{(int(code) + 1) % 10**8:08d}"
    assert pairing.pair(wrong, "Phone") is None and pairing.pair(wrong, "Phone") is None
    paired = pairing.pair(f" {code} ", "  Living   room tablet ")
    assert paired is not None
    key, device = paired
    assert device.name == "Living room tablet" and len(key) >= 40
    assert pairing.pair(code, "Another") is None  # once
    code = pairing.start()[0]
    for _ in range(3):  # its tries used up, the right code doesn't work either
        assert pairing.pair(f"{(int(code) + 1) % 10**8:08d}", "Phone") is None
    assert pairing.waiting() is None and pairing.pair(code, "Phone") is None
    first, second = pairing.start()[0], pairing.start()[0]
    assert first == second or pairing.pair(first, "Phone") is None  # only the code shown last works
    code = pairing.start()[0]
    clock.now += 121
    assert pairing.pair(code, "Phone") is None  # too late
    with pytest.raises(ValueError, match="needs a name"):
        pairing.pair(code, "   ")


def test_a_device_stays_paired_while_it_is_used(tmp_path: Path) -> None:
    clock = Clock()
    settings = server(session_days=30)
    pairing = Pairing(tmp_path, settings, clock)
    key, device = pairing.pair(pairing.start()[0], "Phone") or ("", None)
    assert device is not None and pairing.device(key) is device
    assert pairing.device("") is None and pairing.device("not a key") is None
    clock.now += 20 * DAY_S
    assert pairing.device(key) is device  # used: it stays paired
    clock.now += 20 * DAY_S
    assert pairing.device(key) is device
    again = Pairing(tmp_path, settings, clock)  # what is kept on disk
    assert [d["name"] for d in again.devices()] == ["Phone"] and again.device(key) is not None
    text = (tmp_path / "devices.json").read_text()
    assert key not in text and stat.S_IMODE((tmp_path / "devices.json").stat().st_mode) == 0o600  # its hash alone
    clock.now += 31 * DAY_S  # unused too long
    assert again.device(key) is None and again.devices() == []
    second = Pairing(tmp_path, settings, clock)
    keys = [second.pair(second.start()[0], name) for name in ("Phone", "Tablet")]
    assert all(keys) and len(second.devices()) == 2
    assert second.unpair(second.devices()[0]["id"]) == 1 and [d["name"] for d in second.devices()] == ["Tablet"]
    assert second.unpair(None) == 1 and second.devices() == [] and second.unpair(None) == 0
    (tmp_path / "devices.json").write_text("{broken")
    assert Pairing(tmp_path, settings, clock).devices() == []  # every device pairs again


# ---------------------------------------------------------------------------------------------------- the envelope
def asgi(envelope: Envelope, *, client: str, path: str, host: str, method: str = "GET",
         headers: dict[str, str] | None = None, websocket: bool = False) -> dict[str, Any]:
    """One request through the envelope to an app that says what it was told: the status (or the WebSocket's
    close code), the response's headers, and who the envelope said asked."""
    seen: dict[str, Any] = {}
    sent: list[dict[str, Any]] = []

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        seen["peer"] = scope.get("state", {}).get("peer")
        if scope["type"] == "websocket":
            await send({"type": "websocket.accept"})
            return
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    envelope.app = app
    raw = [(k.encode(), v.encode()) for k, v in {"host": host, **(headers or {})}.items()]
    scope = {"type": "websocket" if websocket else "http", "path": path, "method": method, "headers": raw,
             "client": (client, 50000)}

    async def receive() -> dict[str, Any]:
        return {"type": "websocket.connect"} if websocket else {"type": "http.request", "body": b""}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    asyncio.run(envelope(scope, receive, send))
    if websocket:
        closed = next((m["code"] for m in sent if m["type"] == "websocket.close"), None)
        return {"status": closed or "open", "peer": seen.get("peer")}
    start = next(m for m in sent if m["type"] == "http.response.start")
    return {"status": start["status"], "headers": {k.decode(): v.decode() for k, v in start["headers"]},
            "peer": seen.get("peer")}


def envelope(tmp_path: Path, **changes: Any) -> tuple[Envelope, str]:
    """An envelope over this PC's names, and the key of one paired device."""
    pairing = Pairing(tmp_path, server(**changes))
    key, _ = pairing.pair(pairing.start()[0], "Phone") or ("", None)
    return Envelope(None, server=pairing._server, pairing=pairing, names=("mypc", "mypc.local")), key  # type: ignore[arg-type]


def test_this_pc_is_answered_as_before(tmp_path: Path) -> None:
    env, _ = envelope(tmp_path)
    mine = asgi(env, client="127.0.0.1", path="/api/settings", host="localhost:8765")
    assert (mine["status"], mine["peer"]) == (200, "local")
    assert "connect-src 'self' ws://localhost:* ws://127.0.0.1:*;" in mine["headers"]["content-security-policy"]
    assert mine["headers"]["x-frame-options"] == "DENY"
    assert asgi(env, client="127.0.0.1", path="/api/settings", host="mypc.local:8765")["peer"] == "local"
    assert asgi(env, client="::ffff:127.0.0.1", path="/ws", host="localhost:8765", websocket=True)["status"] == "open"
    rebound = asgi(env, client="127.0.0.1", path="/api/settings", host="evil.example:8765")
    assert rebound["status"] == 403  # another name pointed at this PC
    assert asgi(env, client="127.0.0.1", path="/", host="evil.example:8765")["status"] == 200  # the files: harmless
    assert asgi(env, client="127.0.0.1", path="/ws", host="localhost:8765", websocket=True,
                headers={"origin": "https://evil.example"})["status"] == WS_NOT_LOCAL


def test_a_device_on_the_network_is_answered_once_paired(tmp_path: Path) -> None:
    env, key = envelope(tmp_path)
    phone = "192.168.1.20"
    assert asgi(env, client=phone, path="/", host=PC)["status"] == 200  # the HUD's files, to show the pairing screen
    refused = asgi(env, client=phone, path="/api/settings", host=PC)
    assert refused["status"] == 401 and refused["peer"] is None
    assert asgi(env, client=phone, path="/api/pairing", host=PC)["peer"] == "unpaired"
    assert asgi(env, client=phone, path="/api/pairing", host=PC, method="POST")["status"] == 200
    assert asgi(env, client=phone, path="/api/pairing/code", host=PC, method="POST")["status"] == 401
    assert asgi(env, client=phone, path="/ws", host=PC, websocket=True)["status"] == WS_UNPAIRED
    assert asgi(env, client=phone, path="/mcp", host=PC, method="POST")["status"] == 401
    cookie = {"cookie": f"theme=dark; {COOKIE}={key}"}
    paired = asgi(env, client=phone, path="/api/settings", host=PC, headers=cookie)
    assert paired["status"] == 200 and paired["peer"] == "device"
    assert "ws://192.168.1.10:8765" in paired["headers"]["content-security-policy"]  # its /ws, by the name it used
    assert asgi(env, client=phone, path="/ws", host="mypc.local:8765", headers=cookie, websocket=True)["status"] == "open"
    app = asgi(env, client=phone, path="/mcp", host=PC, method="POST", headers={"authorization": f"Bearer {key}"})
    assert app["peer"] == "device"  # an app sends the key as a bearer token
    assert asgi(env, client=phone, path="/api/settings", host=PC,
                headers={**cookie, "origin": "http://evil.example"})["status"] == 403  # another site, with its cookie


def test_nobody_else_is_answered_at_all(tmp_path: Path) -> None:
    env, key = envelope(tmp_path)
    cookie = {"cookie": f"{COOKIE}={key}"}
    assert asgi(env, client="192.168.1.20", path="/", host="evil.example:8765")["status"] == 403  # DNS rebinding
    assert asgi(env, client="8.8.8.8", path="/", host=PC, headers=cookie)["status"] == 403  # off the network
    assert asgi(env, client="fe80::1", path="/", host="[fe80::2]:8765")["status"] == 200  # link-local is local
    assert asgi(env, client="10.0.0.5", path="/", host="orion.home.arpa:8765")["status"] == 403
    named, key = envelope(tmp_path / "named", names=["orion.home.arpa"], lan_only=False)
    assert asgi(named, client="10.0.0.5", path="/", host="ORION.home.arpa:8765")["status"] == 200
    assert asgi(named, client="8.8.8.8", path="/api/settings", host=PC, headers={"cookie": f"{COOKIE}={key}"})[
        "peer"] == "device"  # lan_only off: the internet too, paired


def test_pairing_over_http(tmp_path: Path) -> None:
    """The endpoints through a real HTTP client: this PC shows a code and sees the devices; the device pairs with it
    and gets its cookie (an app, its key too); only the PC unpairs."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    settings = server(host="0.0.0.0")
    pairing = Pairing(tmp_path, settings)
    app = FastAPI()
    add_pairing(app, pairing, settings)

    @app.get("/api/settings")
    async def settings_page() -> dict[str, bool]:
        return {"ok": True}

    wrapped = Envelope(app, server=settings, pairing=pairing, names=("mypc", "mypc.local"))
    pc = TestClient(wrapped, base_url="http://localhost:8765", client=("127.0.0.1", 40000))
    phone = TestClient(wrapped, base_url=f"http://{PC}", client=("192.168.1.20", 40001))
    state = phone.get("/api/pairing").json()
    assert state.pop("url").endswith(".local:8765") and state == {"local": False, "paired": False, "open": True,
                                                                  "tls": False, "devices": [], "code_ends": None}
    assert phone.get("/api/settings").status_code == 401
    assert phone.post("/api/pairing", json={"code": "00000000", "name": "Phone"}).status_code == 403
    assert phone.post("/api/pairing", json={"code": "1", "name": " "}).status_code == 400
    code = pc.post("/api/pairing/code").json()["code"]
    assert pc.get("/api/pairing").json()["code_ends"] is not None
    paired = phone.post("/api/pairing", json={"code": code, "name": "Phone"})
    assert paired.status_code == 200 and "key" not in paired.json()  # a browser: the cookie alone
    cookie = paired.headers["set-cookie"]
    assert f"{COOKIE}=" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie and "Secure" not in cookie
    assert phone.get("/api/settings").json() == {"ok": True}  # its cookie, sent back
    assert phone.get("/api/pairing").json()["paired"] is True
    assert phone.post("/api/pairing/code").status_code == 403  # a device doesn't pair others
    tool = TestClient(wrapped, base_url=f"http://{PC}", client=("192.168.1.30", 40002))
    app_key = tool.post("/api/pairing", json={"code": pc.post("/api/pairing/code").json()["code"],
                                              "name": "Claude Code", "app": True}).json()["key"]
    tool.cookies.clear()  # an app keeps no cookies: its key, as a bearer token
    assert tool.get("/api/settings").status_code == 401
    assert tool.get("/api/settings", headers={"Authorization": f"Bearer {app_key}"}).status_code == 200
    devices = pc.get("/api/pairing").json()["devices"]
    assert [d["name"] for d in devices] == ["Phone", "Claude Code"]
    assert phone.delete(f"/api/pairing/devices/{devices[0]['id']}").status_code == 403
    assert pc.delete(f"/api/pairing/devices/{devices[0]['id']}").json() == {"unpaired": 1}
    assert phone.get("/api/settings").status_code == 401  # its key is gone
    assert pc.delete("/api/pairing/devices").json() == {"unpaired": 1}
    assert json.loads((tmp_path / "devices.json").read_text()) == {"devices": []}
