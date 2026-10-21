"""oauth: a remote connector signs in through the SDK's own client against a fake OAuth-protected server: the hub
doesn't wait for a person, a sign-in hands the page out and finishes when the browser comes back, the tokens live in
the vault (with when the access token ends, so a restart refreshes instead of asking again), and a connector with an
OAuth app of the user's own never registers."""

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fake_oauth import BASE, URL, FakeOAuthServer

from orion.base import CallContext, EventBus
from orion.config import Config, default_config
from orion.mcp import MCPHub, OpTrace
from orion.oauth import Broker, SignInError, VaultStorage, oauth_name, read_state, redirect_uri, signed_in
from orion.secrets import Vault

REDIRECT = "http://localhost:8765/oauth/callback"


class Hooks:
    """The connectors layer, as little as the hub needs of it: settings as they are, the broker's provider."""

    def __init__(self, broker: Broker) -> None:
        self.broker = broker

    async def prepare(self, name: str, settings: dict[str, Any], expand: Callable[[Any], Any]) -> dict[str, Any]:
        return expand(settings)  # type: ignore[no-any-return]

    def auth(self, name: str, settings: dict[str, Any], on_need: Callable[[], None]) -> Any:
        return self.broker.provider(name, settings, on_need)


class World:
    """A hub, a vault and a broker on one fake server."""

    def __init__(self, tmp_path: Path, server: FakeOAuthServer, monkeypatch: pytest.MonkeyPatch) -> None:
        self.cfg: Config = default_config()
        self.server = server
        self.vault = Vault("files", tmp_path / "vault")
        self.bus = EventBus()
        self.events: list[dict[str, Any]] = []
        self.bus.subscribe(self.events.append)
        self.records: list[dict[str, Any]] = []
        self.changed = 0
        self.activity = SimpleNamespace(record=lambda kind, **data: self.records.append({"kind": kind, **data}))
        self.hub = MCPHub(self.cfg, {}, ops=OpTrace(lambda kind, **data: None, self.cfg.hud, recent=10))
        self.broker = Broker(self, http=server.http)  # type: ignore[arg-type]
        self.hub.hooks = Hooks(self.broker)
        monkeypatch.setattr("mcp.client.streamable_http.create_mcp_http_client", server.http)

    def connectors_changed(self) -> None:
        self.changed += 1

    def status(self, name: str = "notes") -> dict[str, Any]:
        return next(s for s in self.hub.status() if s["name"] == name)  # type: ignore[return-value]

    async def until(self, check: Callable[[], bool], seconds: float = 5.0) -> None:
        async with asyncio.timeout(seconds):
            while not check():
                await asyncio.sleep(0.01)


@pytest.fixture
def make(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., World]:
    def build(server: FakeOAuthServer | None = None) -> World:
        return World(tmp_path, server or FakeOAuthServer(), monkeypatch)

    return build


def settings(**extra: Any) -> dict[str, Any]:
    return {"url": URL, "oauth": {}, "description": "Notes", **extra}


def test_the_address_a_sign_in_returns_to_is_this_huds_callback() -> None:
    cfg = default_config()
    assert redirect_uri(cfg.server) == REDIRECT
    cfg.server.tls_cert, cfg.server.port = "cert.pem", 9000
    assert redirect_uri(cfg.server) == "https://localhost:9000/oauth/callback"


def test_a_connector_without_tokens_waits_for_the_user_instead_of_retrying(make: Callable[..., World]) -> None:
    """The hub's provider never waits for a browser: the link says it needs a sign-in and stays quiet."""
    world = make()

    async def run() -> None:
        async with world.server.serving():
            world.cfg.mcp_servers = {"notes": settings()}
            await world.hub.configure(wait_s=5)
            state = world.status()
            assert state["status"] == "error" and state["sign_in"] is True and "sign in" in state["error"]
            asked = len(world.server.requests)
            await asyncio.sleep(0.3)
            assert len(world.server.requests) == asked  # not retried on a timer
            assert world.hub.tools() == {} and not signed_in(world.vault, "notes")
            await world.hub.stop()

    asyncio.run(run())


def test_signing_in_hands_the_page_out_and_finishes_when_the_browser_comes_back(make: Callable[..., World]) -> None:
    """The page to open, then the browser's return with the code: tokens in the vault, the link connected, the
    sign-in logged; a second callback with the same state is refused."""
    world = make()

    async def run() -> None:
        async with world.server.serving():
            world.cfg.mcp_servers = {"notes": settings()}
            await world.hub.configure(wait_s=5)
            url = await world.broker.sign_in("notes", world.cfg.mcp_servers["notes"], title="Notes")
            assert url is not None and url.startswith(f"{BASE}/authorize?") and world.broker.pending("notes")
            assert not signed_in(world.vault, "notes")
            query = world.server.authorize(url)
            ok, message = await world.broker.callback(query)
            assert ok and message == "Connected to Notes. You can close this tab."
            assert not world.broker.pending("notes") and signed_in(world.vault, "notes")
            await world.until(lambda: world.status()["status"] == "connected")
            assert [t.remote for t in world.hub.tools().values()] == ["ping"] and world.status()["sign_in"] is False
            assert await world.hub.call("notes", "ping", {}, CallContext("hud")) == "pong"
            await world.hub.stop()
            again = await world.broker.callback(query)
            assert again[0] is False and "over" in again[1]

    asyncio.run(run())
    state = read_state(world.vault, "notes")
    assert state["tokens"]["token_type"] == "Bearer" and state["client"]["client_id"] == "client-1"
    assert state["client"]["token_endpoint_auth_method"] == "none" and state["expires_at"] > time.time() + 3000
    assert world.server.registered["client-1"]["redirect_uris"] == [REDIRECT] and world.server.grants == [
        "authorization_code"]
    assert [(r["kind"], r["outcome"], r["connector"]) for r in world.records] == [("login", "ok", "notes")]
    assert world.changed >= 1 and oauth_name("notes") in world.vault.names()
    assert "access_token" not in json.dumps(world.events)  # nothing secret reaches the HUD's events


async def signed_in_world(world: World, **extra: Any) -> None:
    """Connect a notes connector and sign in to it, leaving the link connected (inside ``serving``)."""
    world.cfg.mcp_servers = {"notes": settings(**extra)}
    await world.hub.configure(wait_s=5)
    url = await world.broker.sign_in("notes", world.cfg.mcp_servers["notes"], title="Notes")
    assert url is not None
    assert (await world.broker.callback(world.server.authorize(url)))[0]
    await world.until(lambda: world.status()["status"] == "connected")


def after_a_restart(world: World, make: Callable[..., World]) -> World:
    """Orion starts again: a new hub and broker on the same vault and the same server."""
    again = make(world.server)
    again.vault = world.vault
    return again


def test_a_restart_refreshes_an_old_access_token_instead_of_asking_again(make: Callable[..., World]) -> None:
    """The vault remembers when the access token ends, so after a restart that has passed the refresh token is used
    at once (the SDK alone would send the old token, be refused, and want a new sign-in)."""
    world = make()

    async def run() -> None:
        async with world.server.serving():
            await signed_in_world(world)
            await world.hub.stop()
            state = read_state(world.vault, "notes")
            state["expires_at"] = time.time() - 60  # it ended while Orion was off
            world.vault.set(oauth_name("notes"), json.dumps(state))
            world.server.expire()
            later = after_a_restart(world, make)
            later.cfg.mcp_servers = {"notes": settings()}
            await later.hub.configure(wait_s=5)
            assert later.status()["status"] == "connected"
            assert world.server.grants == ["authorization_code", "refresh_token"] and len(world.server.registered) == 1
            fresh = read_state(world.vault, "notes")
            assert fresh["tokens"]["access_token"] != state["tokens"]["access_token"]
            assert fresh["expires_at"] > time.time() + 3000  # and the new end is kept
            await later.hub.stop()

    asyncio.run(run())


def test_without_the_stored_end_the_old_token_is_sent_and_refused(make: Callable[..., World]) -> None:
    """What the persisted end is for: a vault from before it (or a server that never says) leaves the SDK sending
    the old token; its 401 then means a new sign-in, which the hub reports instead of waiting for."""
    world = make()

    async def run() -> None:
        async with world.server.serving():
            await signed_in_world(world)
            await world.hub.stop()
            state = read_state(world.vault, "notes")
            state["expires_at"] = None
            world.vault.set(oauth_name("notes"), json.dumps(state))
            world.server.expire()
            later = after_a_restart(world, make)
            later.cfg.mcp_servers = {"notes": settings()}
            await later.hub.configure(wait_s=5)
            assert later.status()["sign_in"] is True and "refresh_token" not in world.server.grants
            await later.hub.stop()

    asyncio.run(run())


def test_a_revoked_refresh_token_means_signing_in_again(make: Callable[..., World]) -> None:
    world = make()

    async def run() -> None:
        async with world.server.serving():
            await signed_in_world(world)
            await world.hub.stop()
            state = read_state(world.vault, "notes")
            state["expires_at"] = time.time() - 60
            world.vault.set(oauth_name("notes"), json.dumps(state))
            world.server.revoke()
            later = after_a_restart(world, make)
            later.cfg.mcp_servers = {"notes": settings()}
            await later.hub.configure(wait_s=5)
            assert later.status()["status"] == "error" and later.status()["sign_in"] is True
            assert world.server.grants == ["authorization_code", "refresh_token"]  # tried, refused
            await later.hub.stop()

    asyncio.run(run())


def test_the_users_own_app_is_used_and_nothing_is_registered(make: Callable[..., World]) -> None:
    """A maker that registers no one (Asana, Slack, Box): the client ID and secret the user gave sign in; the
    secret is sent to the token endpoint and never stored with the sign-in."""
    world = make()
    world.server.preregister("my-app", REDIRECT)

    async def run() -> None:
        async with world.server.serving():
            await signed_in_world(world, oauth={"client_id": "my-app", "client_secret": "s3cr3t"})
            await world.hub.stop()

    asyncio.run(run())
    assert ("POST", "/register") not in world.server.requests and world.server.secrets_seen == ["s3cr3t"]
    state = read_state(world.vault, "notes")
    assert "client" not in state and "s3cr3t" not in json.dumps(state)


def test_a_server_that_registers_no_one_says_so(make: Callable[..., World]) -> None:
    """Without registration and without an app of the user's own, the sign-in says what failed, in words."""
    world = make(FakeOAuthServer(registers=False))

    async def run() -> None:
        async with world.server.serving():
            world.cfg.mcp_servers = {"notes": settings()}
            await world.hub.configure(wait_s=5)
            with pytest.raises(SignInError, match="[Rr]egistration"):
                await world.broker.sign_in("notes", world.cfg.mcp_servers["notes"], title="Notes")
            assert not world.broker.pending("notes")
            await world.hub.stop()

    asyncio.run(run())
    assert [r["outcome"] for r in world.records] == ["failed"]


def test_a_no_at_the_sign_in_page_is_told_to_the_page_and_the_toast(make: Callable[..., World]) -> None:
    world = make()

    async def run() -> None:
        async with world.server.serving():
            world.cfg.mcp_servers = {"notes": settings()}
            await world.hub.configure(wait_s=5)
            url = await world.broker.sign_in("notes", world.cfg.mcp_servers["notes"], title="Notes")
            assert url is not None
            ok, message = await world.broker.callback(world.server.authorize(url, approve=False))
            assert not ok and "Notes said no: access_denied" in message
            await world.until(lambda: not world.broker.pending("notes"))
            await world.hub.stop()

    asyncio.run(run())
    assert not signed_in(world.vault, "notes")
    assert next(e for e in world.events if e["type"] == "toast")["text"].startswith("Notes: Notes said no")


def test_a_sign_in_nobody_finishes_ends_by_itself(make: Callable[..., World]) -> None:
    world = make()
    world.cfg.mcp.signin_wait_s = 0.1

    async def run() -> None:
        async with world.server.serving():
            world.cfg.mcp_servers = {"notes": settings()}
            await world.hub.configure(wait_s=5)
            assert await world.broker.sign_in("notes", world.cfg.mcp_servers["notes"], title="Notes")
            await world.until(lambda: not world.broker.pending("notes"))
            await world.hub.stop()

    asyncio.run(run())
    assert "Nobody finished signing in to Notes" in next(e for e in world.events if e["type"] == "toast")["text"]


def test_a_newer_sign_in_replaces_the_one_waiting(make: Callable[..., World]) -> None:
    world = make()

    async def run() -> None:
        async with world.server.serving():
            world.cfg.mcp_servers = {"notes": settings()}
            await world.hub.configure(wait_s=5)
            first = await world.broker.sign_in("notes", world.cfg.mcp_servers["notes"], title="Notes")
            second = await world.broker.sign_in("notes", world.cfg.mcp_servers["notes"], title="Notes")
            assert first and second and first != second
            assert (await world.broker.callback(world.server.authorize(first)))[0] is False  # the old page is dead
            assert (await world.broker.callback(world.server.authorize(second)))[0] is True
            await world.hub.stop()

    asyncio.run(run())


def test_signing_in_to_a_connector_that_is_signed_in_needs_nothing(make: Callable[..., World]) -> None:
    world = make()

    async def run() -> None:
        async with world.server.serving():
            await signed_in_world(world)
            assert await world.broker.sign_in("notes", world.cfg.mcp_servers["notes"], title="Notes") is None
            await world.hub.stop()

    asyncio.run(run())


def test_signing_out_forgets_everything_and_the_link_wants_a_sign_in(make: Callable[..., World]) -> None:
    world = make()

    async def run() -> None:
        async with world.server.serving():
            await signed_in_world(world)
            await world.broker.sign_out("notes", title="Notes")
            assert not signed_in(world.vault, "notes") and oauth_name("notes") not in world.vault.names()
            await world.until(lambda: world.status()["sign_in"] is True)
            assert world.hub.tools() == {}
            await world.hub.stop()

    asyncio.run(run())
    assert [r["title"] for r in world.records] == ["Signed in to Notes", "Signed out of Notes"]


def test_a_registration_for_another_callback_address_is_dropped(tmp_path: Path) -> None:
    """The HUD's port changed since Orion registered: the old registration (and the tokens that belong to it) can't
    be used for the new address, so the sign-in starts over; a user's own app is never dropped."""
    vault = Vault("files", tmp_path / "vault")
    client = {"client_id": "c1", "redirect_uris": ["http://localhost:1111/oauth/callback"]}
    vault.set(oauth_name("notes"), json.dumps({"tokens": {"access_token": "a", "token_type": "Bearer"},
                                               "client": client}))

    async def load(storage: VaultStorage) -> tuple[Any, Any]:
        return await storage.get_tokens(), await storage.get_client_info()

    same_port = VaultStorage(vault, "notes", redirect="http://localhost:1111/oauth/callback", app=None)
    tokens, info = asyncio.run(load(same_port))
    assert tokens is not None and info is not None and info.client_id == "c1"
    moved = VaultStorage(vault, "notes", redirect=REDIRECT, app=None)
    assert asyncio.run(load(moved)) == (None, None)


def test_an_unreadable_sign_in_in_the_vault_is_a_signed_out_connector(tmp_path: Path) -> None:
    vault = Vault("files", tmp_path / "vault")
    vault.set(oauth_name("notes"), "not json")
    assert read_state(vault, "notes") == {} and not signed_in(vault, "notes")
    vault.set(oauth_name("notes"), json.dumps({"tokens": {"nope": 1}}))
    storage = VaultStorage(vault, "notes", redirect=REDIRECT, app=None)
    assert asyncio.run(storage.get_tokens()) is None
