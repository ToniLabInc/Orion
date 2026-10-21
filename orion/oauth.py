"""Signing in to remote connectors with OAuth, through the MCP SDK's ``OAuthClientProvider``: PKCE, the server's own
discovery, and a dynamic registration of Orion as a client (``token_endpoint_auth_method: none``, a public client);
a few makers register no one, and for those the user's own OAuth app is used (``oauth.client_id`` and
``oauth.client_secret`` in the connector's settings).

What is kept, and where: the tokens and the registration are one JSON text in the vault, ``connector.<id>.oauth``
(``orion.secrets``), with the moment the access token ends (the SDK keeps that in memory only, and after a restart
would send an old token and go straight to a new sign-in instead of using the refresh token). Nothing else holds
them: not config.yaml, not the HUD, not a model.

Two kinds of provider:
- the hub's (``Broker.provider``) runs while a connector connects or stays connected: it uses and refreshes the
  tokens, and when a person is needed it doesn't wait for one: it raises ``SignInNeeded`` (the connector then shows
  "needs sign-in" and the hub stops retrying until a sign-in or a restart);
- a sign-in's (``Broker.sign_in``), started by the user's click in the HUD: it hands the page to open back to the
  click (``auth_url``) and waits, up to ``mcp.signin_wait_s``, for the browser to come back to
  ``/oauth/callback`` (``Broker.callback``). The address a sign-in returns to is
  ``http://localhost:<the HUD's port>/oauth/callback``, so it has to be done in a browser on Orion's own PC.
Afterwards the connector's link connects again with the tokens.
"""

import asyncio
import contextlib
import functools
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Protocol
from urllib.parse import parse_qs, urlsplit

from orion.base import JSON, NeedsUser, OrionError

if TYPE_CHECKING:
    import httpx2
    from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider
    from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
    from pydantic import BaseModel

    from orion.activity import Activity
    from orion.base import EventBus
    from orion.config import Config, ServerConfig
    from orion.mcp import MCPHub
    from orion.secrets import Vault

log = logging.getLogger("orion.oauth")

CALLBACK_PATH: Final = "/oauth/callback"
CLIENT_NAME: Final = "ORION"
PROTOCOL: Final = "2025-06-18"  # the MCP version a sign-in's first request names (the server then answers 401)
RETURNED_S: Final = 30.0  # how long the callback page waits for the token exchange that follows the browser's return


class SignInNeeded(NeedsUser):
    """A connector can't connect until the user signs in to it."""


class SignInError(OrionError):
    """A sign-in did not complete; the message says why, in words."""


def oauth_name(connector: str) -> str:
    """The vault's name for a connector's sign-in: ``connector.<id>.oauth``."""
    return f"connector.{connector}.oauth"


def redirect_uri(server: "ServerConfig") -> str:
    """The address a sign-in returns to: this HUD's callback on ``localhost`` (http, or https with ``server.tls_cert``)."""
    return f"{'https' if server.tls_cert else 'http'}://localhost:{server.port}{CALLBACK_PATH}"


def signed_in(vault: "Vault", connector: str) -> bool:
    """Whether a connector has tokens in the vault (they may have run out: the link then says it needs a sign-in)."""
    return read_state(vault, connector).get("tokens") is not None


def read_state(vault: "Vault", connector: str) -> dict[str, Any]:
    """A connector's sign-in as the vault keeps it: ``{"tokens", "expires_at", "client"}`` (what is missing is None);
    {} when there is none, or it isn't readable."""
    text = vault.get(oauth_name(connector))
    if not text:
        return {}
    try:
        state = json.loads(text)
    except ValueError:
        log.warning("the %s sign-in in the vault isn't readable: signed out", connector)
        return {}
    return state if isinstance(state, dict) else {}


class VaultStorage:
    """The SDK's ``TokenStorage``, in the vault. The vault may ask the system's keyring, so it is only touched off the
    event loop."""

    def __init__(self, vault: "Vault", connector: str, *, redirect: str, app: "OAuthClientInformationFull | None") -> None:
        """Keep one connector's sign-in.

        Args:
            vault: the vault.
            connector: the connector's id.
            redirect: the address a sign-in returns to now (a registration made for another one is dropped).
            app: the user's own OAuth app, when the connector has one (then there is no registration to keep).
        """
        self._vault = vault
        self._connector = connector
        self._redirect = redirect
        self._app = app
        self._lock = asyncio.Lock()

    async def get_tokens(self) -> "OAuthToken | None":
        """The tokens, or None."""
        from mcp.shared.auth import OAuthToken

        state = await asyncio.to_thread(self._load)
        return _model(OAuthToken, state.get("tokens"))

    async def expires_at(self) -> float | None:
        """When the access token ends (epoch seconds), None when that isn't known."""
        value = (await asyncio.to_thread(self._load)).get("expires_at")
        return float(value) if isinstance(value, int | float) else None

    async def set_tokens(self, tokens: "OAuthToken") -> None:
        """Keep new tokens, with when the access token ends."""
        async with self._lock:
            state = await asyncio.to_thread(self._load)
            state["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
            state["expires_at"] = time.time() + tokens.expires_in if tokens.expires_in else None
            await asyncio.to_thread(self._save, state)

    async def get_client_info(self) -> "OAuthClientInformationFull | None":
        """The user's own app, else the registration the server gave Orion (None when there is none)."""
        from mcp.shared.auth import OAuthClientInformationFull

        if self._app is not None:
            return self._app
        state = await asyncio.to_thread(self._load)
        return _model(OAuthClientInformationFull, state.get("client"))

    async def set_client_info(self, client_info: "OAuthClientInformationFull") -> None:
        """Keep the registration the server gave Orion."""
        async with self._lock:
            state = await asyncio.to_thread(self._load)
            state["client"] = client_info.model_dump(mode="json", exclude_none=True)
            await asyncio.to_thread(self._save, state)

    def _load(self) -> dict[str, Any]:
        """The state; one registered for another callback address is forgotten, tokens and all (they belong to it)."""
        state = read_state(self._vault, self._connector)
        client = state.get("client")
        if self._app is None and isinstance(client, dict) and self._redirect not in (client.get("redirect_uris") or ()):
            log.info("%s was registered for another address than %s: signing in again", self._connector,
                     self._redirect)
            return {}
        return state

    def _save(self, state: dict[str, Any]) -> None:
        """Write the state."""
        self._vault.set(oauth_name(self._connector), json.dumps(state))


def _model[M: BaseModel](kind: type[M], data: object) -> M | None:
    """A stored model, None when it isn't one (the sign-in is then done again)."""
    if not isinstance(data, dict):
        return None
    try:
        return kind.model_validate(data)
    except ValueError as e:
        log.warning("a stored %s isn't readable: %s", kind.__name__, e)
        return None


@functools.cache
def _provider_class() -> type["OAuthClientProvider"]:
    """The SDK's provider, taught to restore when the stored access token ends, defined on first use (the SDK loads
    then). A quiet logger for the one failure that isn't one: the SDK logs a trace for every sign-in needed."""
    from mcp.client.auth import OAuthClientProvider

    class Provider(OAuthClientProvider):
        """``OAuthClientProvider`` that knows, after a restart, how old the stored access token is: one that has ended
        is refreshed with the refresh token at once, instead of being sent and refused."""

        async def _initialize(self) -> None:
            await super()._initialize()
            storage = self.context.storage
            if isinstance(storage, VaultStorage) and self.context.current_tokens is not None:
                self.context.token_expiry_time = await storage.expires_at()

    class Expected(logging.Filter):
        """Drops the SDK's "OAuth flow error" trace when the error is the one Orion raises to say it needs a person."""

        def filter(self, record: logging.LogRecord) -> bool:
            """Whether to keep the record."""
            error = record.exc_info[1] if record.exc_info else None
            return not isinstance(error, SignInNeeded)

    logging.getLogger("mcp.client.auth.oauth2").addFilter(Expected())
    return Provider


class BrokerHost(Protocol):
    """What signing in needs of the running Orion (``runtime.Runtime`` provides it)."""

    cfg: "Config"
    bus: "EventBus"
    hub: "MCPHub"

    @property
    def vault(self) -> "Vault":
        """The vault."""

    @property
    def activity(self) -> "Activity":
        """The activity log."""

    def connectors_changed(self) -> None:
        """Tell the HUD the connectors changed."""


@dataclass(eq=False, slots=True)
class _SignIn:
    """A sign-in the user started and the browser has yet to finish."""

    connector: str
    title: str
    url: "asyncio.Future[str]"
    code: "asyncio.Future[AuthorizationCodeResult]"
    task: "asyncio.Task[None] | None" = None
    state: str = ""
    error: str = ""
    started: float = field(default_factory=time.monotonic)


class Broker:
    """Signs the user in to connectors, and gives the hub the providers that keep them signed in."""

    def __init__(self, host: BrokerHost, *, http: "Callable[..., httpx2.AsyncClient] | None" = None) -> None:
        """Hold the running Orion.

        Args:
            host: the running Orion.
            http: makes the HTTP client a sign-in's first request goes through (the SDK's own, with the MCP
                transports' timeouts, by default; tests pass one on an in-process server).
        """
        self.host = host
        self._http = http
        self._open: dict[str, _SignIn] = {}  # connector -> its sign-in
        self._by_state: dict[str, _SignIn] = {}  # the page's ``state`` -> the sign-in it belongs to

    # ------------------------------------------------------------------------------------------------ the hub's
    def provider(self, name: str, settings: dict[str, Any], on_need: Callable[[], None]) -> "OAuthClientProvider":
        """The provider for a connector that is connecting or connected: it signs in with the tokens, refreshes them,
        and when only a person can help says so (``on_need``, then ``SignInNeeded``) instead of waiting for one.

        Args:
            name: the connector's id.
            settings: its settings, secrets filled in (``url``, and ``oauth`` with the user's app, if any).
            on_need: called just before ``SignInNeeded`` is raised.

        Returns:
            The provider, for the connector's HTTP client.
        """
        needs = f"{name} needs you to sign in (Customize, Connectors)."

        async def redirect(_url: str) -> None:
            """No browser is waiting: the connector needs the user to sign in."""
            on_need()
            raise SignInNeeded(needs)

        async def never() -> "AuthorizationCodeResult":
            """Unreachable: the redirect above always raises."""
            raise SignInNeeded(needs)

        return self._build(name, settings, redirect, never)

    # ------------------------------------------------------------------------------------------------ a sign-in
    async def sign_in(self, name: str, settings: dict[str, Any], *, title: str) -> str | None:
        """Start signing in to a connector: the page to open in a browser on this PC. The sign-in goes on in the
        background until the browser comes back to ``callback`` (or ``mcp.signin_wait_s`` passes); a sign-in already
        open for the connector is replaced.

        Args:
            name: the connector's id.
            settings: its settings, secrets filled in.
            title: its name for people (the activity log's).

        Returns:
            The address to open; None when the connector is signed in already and nothing was needed.

        Raises:
            SignInError: the server's sign-in could not start (it doesn't answer, refuses Orion, or isn't a remote
                server).
        """
        url = settings.get("url")
        if not isinstance(url, str):
            raise SignInError(f"{title} is not a remote server: there is nothing to sign in to.")
        self._forget(name)
        loop = asyncio.get_running_loop()
        session = _SignIn(name, title, loop.create_future(), loop.create_future())
        self._open[name] = session

        async def redirect(address: str) -> None:
            """The page the user must open: handed to ``sign_in``'s caller."""
            parts = urlsplit(address)
            if parts.scheme not in ("http", "https"):
                raise SignInError(f"{title} sent a sign-in page that isn't a web address.")
            session.state = parse_qs(parts.query).get("state", [""])[0]
            self._by_state[session.state] = session
            session.url.set_result(address)

        async def callback() -> "AuthorizationCodeResult":
            """Wait for the browser to come back."""
            wait = self.host.cfg.mcp.signin_wait_s
            try:
                async with asyncio.timeout(wait):
                    return await session.code
            except TimeoutError:
                raise SignInError(f"Nobody finished signing in to {title} within {wait / 60:g} minutes.") from None

        provider = self._build(name, settings, redirect, callback)
        session.task = asyncio.create_task(self._drive(session, provider, url), name=f"sign-in {name}")
        await asyncio.wait({session.url, session.task}, return_when=asyncio.FIRST_COMPLETED)
        if session.url.done() and not session.url.cancelled():
            return session.url.result()
        if session.task.cancelled():
            raise SignInError(f"A newer sign-in to {title} replaced this one.")
        session.task.result()  # its error, if it ended with one
        return None

    async def callback(self, params: dict[str, str]) -> tuple[bool, str]:
        """The browser came back from the sign-in page (``GET /oauth/callback``): hand its code to the sign-in and
        say how it ended.

        Args:
            params: the query: ``code`` and ``state`` (``iss``), or ``error`` (``error_description``).

        Returns:
            (whether it worked, a sentence for the page the user sees).
        """
        session = self._by_state.pop(params.get("state", ""), None)
        if session is None or session.code.done():
            return False, "That sign-in is over, or was never started here. Start it again from Orion."
        if error := params.get("error"):
            session.code.set_exception(SignInError(f"{session.title} said no: {params.get('error_description') or error}"))
        elif not params.get("code"):
            session.code.set_exception(SignInError(f"{session.title} sent nothing to finish the sign-in with."))
        else:
            from mcp.client.auth import AuthorizationCodeResult

            session.code.set_result(AuthorizationCodeResult(code=params["code"], state=params.get("state"),
                                                            iss=params.get("iss") or None))
        task = session.task
        done = task is None or task.done()
        if task is not None and not done:
            done = bool((await asyncio.wait({task}, timeout=RETURNED_S))[0])
        if session.error:
            return False, session.error
        return True, (f"Connected to {session.title}. You can close this tab." if done
                      else f"Connecting to {session.title}… you can close this tab.")

    async def sign_out(self, name: str, *, title: str, source: str = "hud") -> None:
        """Forget a connector's tokens and registration, and disconnect it.

        Args:
            name: the connector's id.
            title: its name for people.
            source: who asked (the activity log's).
        """
        self._forget(name)
        vault = self.host.vault
        await asyncio.to_thread(vault.delete, oauth_name(name))
        await self._reconnect(name)
        self.host.activity.record("login", source=source, title=f"Signed out of {title}", outcome="ok", connector=name)
        self.host.connectors_changed()

    def pending(self, name: str) -> bool:
        """Whether a sign-in to a connector is waiting for the browser."""
        return name in self._open

    def cancel(self, name: str) -> None:
        """Stop a connector's sign-in that is waiting for the browser (it was removed, say)."""
        self._forget(name)

    async def close(self) -> None:
        """Stop every sign-in still waiting."""
        for name in list(self._open):
            self._forget(name)

    # ------------------------------------------------------------------------------------------------ inside
    def _build(self, name: str, settings: dict[str, Any], redirect: Callable[[str], Any],
               callback: Callable[[], Any]) -> "OAuthClientProvider":
        """A provider for a connector, its tokens in the vault."""
        url = str(settings.get("url") or "")
        address = redirect_uri(self.host.cfg.server)
        storage = VaultStorage(self.host.vault, name, redirect=address, app=_app(settings.get("oauth"), address))
        return _provider_class()(url, _metadata(address), storage, redirect_handler=redirect, callback_handler=callback)

    async def _drive(self, session: _SignIn, provider: "OAuthClientProvider", url: str) -> None:
        """Make the first request, which runs the SDK's sign-in when the server asks for one; then connect the link.

        The task ends with an error only when it ends before the page is known (``sign_in`` raises it); later errors
        are kept in ``session.error`` for the callback page, and said in a toast.
        """
        from mcp.shared._httpx_utils import stream_within_origin  # redirects only within the server's own origin

        title, name = session.title, session.connector
        try:
            async with self._client(provider) as client, stream_within_origin(
                    client, "POST", url, json=_hello(),
                    headers={"accept": "application/json, text/event-stream",
                             "mcp-protocol-version": PROTOCOL}) as response:
                status = response.status_code
            if status in (401, 403):
                raise SignInError(f"{title} still refuses Orion after the sign-in ({status}).")
            if status >= 400:
                raise SignInError(f"{title} answered {status}.")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # any failure of the flow is for the user to read, never a crash
            message = _plain(e)
            session.error = message
            log.warning("sign-in to %s failed: %s", name, message, exc_info=log.isEnabledFor(logging.DEBUG))
            self.host.activity.record("login", source="hud", title=f"Signing in to {title} failed", outcome="failed",
                                      connector=name, detail=message)
            if session.url.done():
                self.host.bus.publish("toast", title="SIGN-IN", text=f"{title}: {message}")
            else:
                raise SignInError(message) from None
        else:
            self.host.activity.record("login", source="hud", title=f"Signed in to {title}", outcome="ok",
                                      connector=name)
            await self._reconnect(name)
        finally:
            if not session.url.done():
                session.url.cancel()
            if self._open.get(name) is session:
                del self._open[name]
            self._by_state.pop(session.state, None)
            self.host.connectors_changed()

    def _client(self, provider: "OAuthClientProvider") -> "httpx2.AsyncClient":
        """The HTTP client a sign-in's first request goes through."""
        if self._http is not None:
            return self._http(auth=provider)
        from mcp.client.streamable_http import create_mcp_http_client

        return create_mcp_http_client(auth=provider)

    async def _reconnect(self, name: str) -> None:
        """Connect a connector's link again (with the new tokens, or without them)."""
        with contextlib.suppress(ValueError):  # switched off, or no longer there: nothing to reconnect
            await self.host.hub.restart(name)

    def _forget(self, name: str) -> None:
        """Stop a connector's sign-in, if one is waiting."""
        session = self._open.pop(name, None)
        if session is None:
            return
        self._by_state.pop(session.state, None)
        if session.task is not None and not session.task.done():
            session.task.cancel()
        if not session.url.done():
            session.url.cancel()


def _metadata(address: str) -> "OAuthClientMetadata":
    """What Orion tells a server that registers clients: a public client that returns to this HUD."""
    from mcp.shared.auth import OAuthClientMetadata
    from pydantic import AnyUrl

    return OAuthClientMetadata(redirect_uris=[AnyUrl(address)], client_name=CLIENT_NAME,
                               token_endpoint_auth_method="none", grant_types=["authorization_code", "refresh_token"],
                               response_types=["code"])


def _app(oauth: object, address: str) -> "OAuthClientInformationFull | None":
    """The user's own OAuth app from a connector's ``oauth`` setting; None when it doesn't name one."""
    from mcp.shared.auth import OAuthClientInformationFull
    from pydantic import AnyUrl

    if not isinstance(oauth, dict) or not oauth.get("client_id"):
        return None
    secret = str(oauth.get("client_secret") or "") or None
    return OAuthClientInformationFull(
        client_id=str(oauth["client_id"]), client_secret=secret, redirect_uris=[AnyUrl(address)],
        token_endpoint_auth_method="client_secret_post" if secret else "none",
        grant_types=["authorization_code", "refresh_token"], response_types=["code"], client_name=CLIENT_NAME)


def _hello() -> dict[str, JSON]:
    """The request a sign-in starts with: MCP's initialize, which a server that wants a sign-in answers with 401."""
    from orion import __version__

    return {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "orion", "version": __version__}}}


def _plain(error: BaseException) -> str:
    """A sign-in failure in words: the SDK's own messages, or the server's, cut to a line."""
    from orion.mcp import describe_error

    return describe_error(error)
