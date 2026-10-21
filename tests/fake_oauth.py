"""A fake remote MCP server that asks for OAuth, entirely in-process: its protected-resource and authorization-server
metadata, dynamic client registration, authorization codes with PKCE, access and refresh tokens, and one tool behind
the bearer token. Tests hand the SDK's own client an ASGI transport onto it (``FakeOAuthServer.http``), so no socket
opens and nothing leaves the machine."""

import base64
import hashlib
import json
import secrets
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2
import mcp_types as mt
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

BASE = "https://mcp.example.test"
PATH = "/mcp"
URL = BASE + PATH


class FakeOAuthServer:
    """The server. ``requests`` logs every ``(method, path)``; ``registered``, ``tokens`` and ``grants`` show what the
    client did; ``decide`` is the person at the sign-in page."""

    def __init__(self, *, registers: bool = True, ttl_s: int = 3600, issues_refresh: bool = True) -> None:
        """A server that registers clients itself (or not), whose access tokens last ``ttl_s``."""
        self.registers, self.ttl_s, self.issues_refresh = registers, ttl_s, issues_refresh
        self.requests: list[tuple[str, str]] = []
        self.registered: dict[str, dict[str, Any]] = {}
        self.codes: dict[str, dict[str, str]] = {}
        self.tokens: dict[str, float] = {}  # access token -> when it ends
        self.refresh_tokens: dict[str, str] = {}  # refresh token -> its client
        self.grants: list[str] = []  # "authorization_code" / "refresh_token", in order
        self.secrets_seen: list[str] = []  # client secrets the token endpoint was given
        self.calls = 0  # tool calls served
        server: Server[Any] = Server("fake", on_list_tools=self._list_tools, on_call_tool=self._call_tool)
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
        self.manager = StreamableHTTPSessionManager(server, stateless=True, json_response=True,
                                                    security_settings=security)

    # ------------------------------------------------------------------------------------------------ the tool
    async def _list_tools(self, _ctx: object, _params: object) -> mt.ListToolsResult:
        """One read-only tool."""
        return mt.ListToolsResult(tools=[mt.Tool(
            name="ping", description="Answer pong.", input_schema={"type": "object"},
            annotations=mt.ToolAnnotations(read_only_hint=True, open_world_hint=True))])

    async def _call_tool(self, _ctx: object, _params: object) -> mt.CallToolResult:
        """Answer pong."""
        self.calls += 1
        return mt.CallToolResult(content=[mt.TextContent(type="text", text="pong")])

    # ------------------------------------------------------------------------------------------------ the person
    def authorize(self, address: str, *, approve: bool = True) -> dict[str, str]:
        """What the browser does at the sign-in page: check the address, let the person decide, and come back with
        the query the callback receives.

        Args:
            address: the sign-in page Orion handed out.
            approve: whether the person says yes.

        Returns:
            ``{code, state}``, or ``{error, state}`` when the person said no.
        """
        query = {k: v[0] for k, v in parse_qs(urlsplit(address).query).items()}
        assert urlsplit(address).path == "/authorize" and query["response_type"] == "code"
        assert query["code_challenge_method"] == "S256" and query["client_id"] in self.registered
        assert query["redirect_uri"] in self.registered[query["client_id"]]["redirect_uris"]
        if not approve:
            return {"error": "access_denied", "state": query["state"]}
        code = secrets.token_urlsafe(16)
        self.codes[code] = {"client_id": query["client_id"], "challenge": query["code_challenge"],
                            "redirect_uri": query["redirect_uri"]}
        return {"code": code, "state": query["state"]}

    def expire(self) -> None:
        """Every access token ends now (the refresh tokens still work)."""
        for token in self.tokens:
            self.tokens[token] = 0.0

    def revoke(self) -> None:
        """Every token, refresh tokens included, stops working."""
        self.tokens.clear()
        self.refresh_tokens.clear()

    def preregister(self, client_id: str, redirect: str) -> None:
        """A client the maker registered for the user (their own OAuth app)."""
        self.registered[client_id] = {"redirect_uris": [redirect], "token_endpoint_auth_method": "client_secret_post"}

    # ------------------------------------------------------------------------------------------------ the wire
    def http(self, headers: dict[str, str] | None = None, timeout: httpx2.Timeout | None = None,
             auth: httpx2.Auth | None = None) -> httpx2.AsyncClient:
        """The factory the SDK's ``create_mcp_http_client`` is replaced by: a client on this server."""
        return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self), base_url=BASE, headers=headers,
                                  timeout=timeout or httpx2.Timeout(10), auth=auth)

    @asynccontextmanager
    async def serving(self) -> AsyncIterator["FakeOAuthServer"]:
        """Run its session manager while the test uses it."""
        async with self.manager.run():
            yield self

    async def __call__(self, scope: dict[str, Any], receive: Callable[..., Any], send: Callable[..., Any]) -> None:
        """Serve one request."""
        if scope["type"] != "http":
            return
        request = Request(scope, receive)
        path = scope["path"]
        self.requests.append((scope["method"], path))
        if path.startswith("/.well-known/oauth-protected-resource"):
            response: Response = JSONResponse({"resource": URL, "authorization_servers": [BASE],
                                               "scopes_supported": ["read"]})
        elif path.startswith("/.well-known/oauth-authorization-server"):
            response = JSONResponse(self._metadata())
        elif path == "/register" and self.registers:
            response = await self._register(request)
        elif path == "/token":
            response = await self._token(request)
        elif path == PATH:
            await self._mcp(scope, receive, send, request)
            return
        else:
            response = Response(status_code=404)
        await response(scope, receive, send)

    def _metadata(self) -> dict[str, Any]:
        """The authorization server's metadata."""
        meta: dict[str, Any] = {
            "issuer": BASE, "authorization_endpoint": f"{BASE}/authorize", "token_endpoint": f"{BASE}/token",
            "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"], "scopes_supported": ["read"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_post"]}
        if self.registers:
            meta["registration_endpoint"] = f"{BASE}/register"
        return meta

    async def _register(self, request: Request) -> Response:
        """Dynamic client registration."""
        body = await request.json()
        client_id = f"client-{len(self.registered) + 1}"
        self.registered[client_id] = body
        return JSONResponse({**body, "client_id": client_id}, status_code=201)

    async def _token(self, request: Request) -> Response:
        """The token endpoint: an authorization code (with its PKCE verifier) or a refresh token."""
        form = {k: str(v) for k, v in (await request.form()).items()}
        grant = form.get("grant_type", "")
        self.grants.append(grant)
        if secret := form.get("client_secret"):
            self.secrets_seen.append(secret)
        if grant == "authorization_code":
            issued = self.codes.pop(form.get("code", ""), None)
            digest = base64.urlsafe_b64encode(hashlib.sha256(form.get("code_verifier", "").encode()).digest())
            if (issued is None or issued["client_id"] != form.get("client_id")
                    or issued["challenge"] != digest.decode().rstrip("=")
                    or issued["redirect_uri"] != form.get("redirect_uri")):
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            client = issued["client_id"]
        elif grant == "refresh_token" and form.get("refresh_token") in self.refresh_tokens:
            client = self.refresh_tokens.pop(form["refresh_token"])
        else:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        access = secrets.token_urlsafe(16)
        self.tokens[access] = time.time() + self.ttl_s
        reply: dict[str, Any] = {"access_token": access, "token_type": "Bearer", "expires_in": self.ttl_s,
                                 "scope": "read"}
        if self.issues_refresh:
            reply["refresh_token"] = secrets.token_urlsafe(16)
            self.refresh_tokens[reply["refresh_token"]] = client
        return JSONResponse(reply)

    async def _mcp(self, scope: dict[str, Any], receive: Callable[..., Any], send: Callable[..., Any],
                   request: Request) -> None:
        """The MCP endpoint: 401 with a pointer to the metadata unless the bearer token is good."""
        header = request.headers.get("authorization", "")
        token = header[7:] if header.lower().startswith("bearer ") else ""
        if self.tokens.get(token, 0.0) <= time.time():
            challenge = f'Bearer resource_metadata="{BASE}/.well-known/oauth-protected-resource{PATH}"'
            await Response(status_code=401, headers={"www-authenticate": challenge})(scope, receive, send)
            return
        await self.manager.handle_request(scope, receive, send)


def stored(state: str) -> dict[str, Any]:
    """The sign-in as the vault keeps it, parsed."""
    return json.loads(state)
