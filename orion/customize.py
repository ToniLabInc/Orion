"""The REST of Customize: the HUD's pages for connectors, skills and plugins.

- Connectors: ``/api/connectors`` (the list; ``/directory`` what can be added; one, with its latest activity; its
  settings; on or off and the sandbox; each tool; sign in and out; add; remove) and ``/oauth/callback``, where a
  browser comes back from a provider's sign-in page.
- Skills: ``/api/skills`` (the list; ``/discover`` and ``/import``; one with its SKILL.md; save, switch, delete).
- Plugins: ``/api/plugins`` (the list; ``/discover``; install from a marketplace or an uploaded .zip or .plugin;
  switch, update, remove; ``/marketplaces``; ``/refresh``).

Everything is a thin layer over ``Connectors``, ``Skills`` and ``Library``: what the user can do here the AI can do only
through the tools of ``tools.core``, and with their yes. A paired device may do all of it except sign in to a
connector (the sign-in comes back to ``localhost``, so it is done on the PC Orion runs on).
"""

import html
import string
from collections.abc import Mapping
from typing import TYPE_CHECKING, Annotated, Any, Final

from orion.base import OrionError
from orion.plugins import WHO, FetchError, Limits
from orion.skills import skill_key

if TYPE_CHECKING:
    from fastapi import FastAPI

    from orion.base import Event
    from orion.runtime import Runtime

UPLOAD_SLACK: Final = 64 << 10  # the framing of a multipart upload, on top of the file
UPLOAD_TYPE: Final = "multipart/form-data"
SKILL_FILE_CHARS: Final = 2_000_000  # a SKILL.md is never read past this
PAGE: Final = string.Template("""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>ORION</title><link rel="icon" href="data:,">
<style>
html{color-scheme:dark}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b0606;color:#f0d8d4;
font:16px/1.6 system-ui,sans-serif}
main{max-width:30em;padding:2em}
h1{margin:0 0 .5em;font-size:.9em;letter-spacing:.3em;font-weight:600;color:#ff6b57}
p{margin:0;font-size:1.15em}.no{color:#ffb4a8}
</style>
<body><main><h1>ORION</h1><p class="$kind">$message</p></main>""")


def for_hud(view: Mapping[str, Any], *, local: bool) -> dict[str, Any]:
    """A connector as one HUD should see it: whether that HUD is on Orion's PC is its own (``Connectors`` says it is).

    Args:
        view: the connector.
        local: whether the HUD asking is on Orion's PC.

    Returns:
        The connector, with ``local`` set for it.
    """
    return dict(view) if local else {**view, "local": False}


def for_device(event: "Event") -> "Event":
    """An event for a HUD that is not on Orion's PC: the ``connectors`` list says none of them can sign in from there.

    Args:
        event: what the bus published.

    Returns:
        The same event, except a ``connectors`` one, whose connectors have ``local`` false.
    """
    items = event.get("items")
    if event.get("type") != "connectors" or not isinstance(items, list):
        return event
    return {**event, "items": [for_hud(item, local=False) if isinstance(item, dict) else item for item in items]}


def add_customize(app: "FastAPI", rt: "Runtime") -> None:
    """Add the REST of Customize to the HUD's app.

    Args:
        app: the HUD's app.
        rt: the running Orion.
    """
    from fastapi import Body, HTTPException, Request
    from fastapi.responses import HTMLResponse, JSONResponse, Response
    from starlette.datastructures import UploadFile

    async def fetch_failed(_request: Request, error: Exception) -> Response:
        """A download failed (the site is down, an answer was too big or not an archive): 502, with what happened."""
        return JSONResponse({"detail": str(error)}, 502)

    async def refused(_request: Request, error: Exception) -> Response:
        """Something Orion couldn't do (a sign-in that failed, a setting missing): 400, with its reason."""
        return JSONResponse({"detail": str(error)}, 400)

    app.add_exception_handler(FetchError, fetch_failed)
    app.add_exception_handler(OrionError, refused)

    def local(request: Request) -> bool:
        """Whether the HUD asking is on Orion's PC (the envelope says)."""
        return request.scope.get("state", {}).get("peer") == "local"

    async def json_body(request: Request) -> dict[str, Any]:
        """The JSON object a request sent.

        Raises:
            HTTPException: it sent something else.
        """
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "Send JSON.") from None
        if not isinstance(body, dict):
            raise HTTPException(400, "Send a JSON object.")
        return body

    def is_upload(request: Request) -> bool:
        """Whether a request carries a file (multipart) rather than JSON."""
        return request.headers.get("content-type", "").lower().startswith(UPLOAD_TYPE)

    async def upload(request: Request) -> tuple[bytes, str]:
        """The file of a multipart request, refused before it is read when it is bigger than ``plugins`` allows.

        Returns:
            (its bytes, its name).

        Raises:
            HTTPException: no length was sent (411), it is too big (413), or there is no file in it.
        """
        limit = Limits.of(rt.cfg).download
        length = request.headers.get("content-length", "")
        if not length.isdigit():
            raise HTTPException(411, "Send the file with its length.")
        if int(length) > limit + UPLOAD_SLACK:
            raise HTTPException(413, f"That file is bigger than {limit >> 20} MB.")
        form = await request.form()
        try:
            sent = next((part for _, part in form.multi_items() if isinstance(part, UploadFile)), None)
            if sent is None:
                raise HTTPException(400, "Send the file as multipart/form-data.")
            data = await sent.read(limit + 1)
            if len(data) > limit:
                raise HTTPException(413, f"That file is bigger than {limit >> 20} MB.")
            return data, sent.filename or ""
        finally:
            await form.close()

    def switch_of(body: Mapping[str, Any], key: str = "enabled") -> bool | None:
        """A boolean a body may carry (None when it carries none).

        Raises:
            HTTPException: it is not a boolean.
        """
        value = body.get(key)
        if value is not None and not isinstance(value, bool):
            raise HTTPException(400, f"{key} is true or false.")
        return value

    # ------------------------------------------------------------------------------------------------ connectors
    # The routes with a fixed word come before the ones that take an id.
    @app.get("/api/connectors")
    async def connectors(request: Request) -> dict[str, Any]:
        """Every connector: Orion's own groups of tools, the directory's, plugins' and the user's own."""
        return {"items": await rt.connectors.snapshot(local=local(request))}

    @app.get("/api/connectors/directory")
    async def directory(q: str = "", category: str = "") -> dict[str, Any]:
        """What can be added: Orion's own list, then the connectors of Claude's directory."""
        return {"items": await rt.connectors.directory(q, category)}

    @app.post("/api/connectors")
    async def add_connector(request: Request, body: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
        """Add a connector from the directory, or one of the user's own."""
        added = await rt.connectors.add(body)
        return for_hud(added, local=local(request))

    @app.get("/api/connectors/{connector_id}")
    async def connector(connector_id: str, request: Request) -> dict[str, Any]:
        """One connector, with what it did lately."""
        return for_hud(await rt.connectors.detail(connector_id, local=local(request)), local=local(request))

    @app.put("/api/connectors/{connector_id}/settings")
    async def connector_settings(connector_id: str, request: Request,
                                 body: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
        """Save a connector's own settings (a secret left empty stays as it is)."""
        values = body.get("values")
        if not isinstance(values, dict):
            raise HTTPException(400, 'Send {"values": {...}}.')
        return for_hud(await rt.connectors.save_settings(connector_id, values), local=local(request))

    @app.patch("/api/connectors/{connector_id}")
    async def change_connector(connector_id: str, request: Request,
                               body: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
        """Switch a connector on or off, or its sandbox."""
        enabled, sandbox = switch_of(body), switch_of(body, "sandbox")
        if enabled is None and sandbox is None:
            raise HTTPException(400, 'Send {"enabled": …} or {"sandbox": …}.')
        view = await rt.connectors.get(connector_id)
        if sandbox is not None:
            view = await rt.connectors.set_sandbox(connector_id, sandbox)
        if enabled is not None:
            view = await rt.connectors.set_enabled(connector_id, enabled)
        return for_hud(view, local=local(request))

    @app.patch("/api/connectors/{connector_id}/tools/{tool}")
    async def change_tool(connector_id: str, tool: str, request: Request,
                          body: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
        """Switch one tool of a connector on or off."""
        enabled = switch_of(body)
        if enabled is None:
            raise HTTPException(400, 'Send {"enabled": …}.')
        return for_hud(await rt.connectors.set_tool(connector_id, tool, enabled), local=local(request))

    @app.post("/api/connectors/{connector_id}/connect")
    async def connect(connector_id: str, request: Request) -> dict[str, Any]:
        """Start signing in: the page to open in a new tab (on the PC Orion runs on)."""
        started = await rt.connectors.connect(connector_id, local=local(request))
        return started if "auth_url" in started else for_hud(started, local=local(request))

    @app.post("/api/connectors/{connector_id}/disconnect")
    async def disconnect(connector_id: str, request: Request) -> dict[str, Any]:
        """Sign out: the connector's tokens are forgotten."""
        return for_hud(await rt.connectors.disconnect(connector_id), local=local(request))

    @app.delete("/api/connectors/{connector_id}")
    async def remove_connector(connector_id: str) -> dict[str, bool]:
        """Remove a connector the user added."""
        await rt.connectors.remove(connector_id)
        return {"ok": True}

    @app.get("/oauth/callback")
    async def oauth_callback(request: Request) -> Response:
        """Where a browser comes back from a provider's sign-in page: the code finishes the sign-in, and the page says
        how it went."""
        ok, message = await rt.broker.callback(dict(request.query_params))
        page = PAGE.substitute(kind="yes" if ok else "no", message=html.escape(message))
        return HTMLResponse(page, status_code=200 if ok else 400, headers={"cache-control": "no-store"})

    # ------------------------------------------------------------------------------------------------ skills
    # Plain functions where files are read: FastAPI runs them on its worker threads.
    def skill_page(name: str) -> dict[str, Any]:
        """A skill with the text of its SKILL.md.

        Raises:
            LookupError: there is no such skill.
        """
        skill = rt.skills.entry(name)
        if skill is None:
            raise LookupError(f"There's no skill called {skill_key(name) or name}.")
        try:
            content = skill.path.read_text(encoding="utf-8")[:SKILL_FILE_CHARS]
        except (OSError, UnicodeDecodeError):
            raise LookupError(f"The {skill.name} skill can't be read.") from None
        return {**rt.skills.view(skill), "content": content}

    def skill_done(action: str, name: str, title: str) -> None:
        """Tell the HUDs and the activity log that the user changed a skill."""
        rt.bus.publish("skill_changed", name=name, action=action)
        rt.activity.record("skill", source="hud", actor=WHO, title=title, outcome="ok")

    @app.get("/api/skills")
    def skills() -> dict[str, Any]:
        """Every skill, switched on or off, with where it comes from."""
        return {"items": [rt.skills.view(skill) for skill in rt.skills.everything().values()]}

    @app.get("/api/skills/discover")
    def discover_skills(q: str = "") -> dict[str, Any]:
        """The skills of the skill sources (``skills.sources``), for the Discover tab."""
        return {"items": rt.library.discover_skills(q)}

    @app.post("/api/skills/import")
    async def import_skills(request: Request) -> list[dict[str, Any]]:
        """Import skills from a GitHub URL (``{"url": …}``) or an uploaded .zip."""
        if is_upload(request):
            data, filename = await upload(request)
            added = await rt.library.import_skills(data=data, filename=filename)
        else:
            url = (await json_body(request)).get("url")
            if not isinstance(url, str) or not url.strip():
                raise HTTPException(400, 'Send {"url": "https://github.com/owner/repo"} or a .zip.')
            added = await rt.library.import_skills(url=url.strip())
        return [rt.skills.view(skill) for skill in added]

    @app.get("/api/skills/{name}")
    def skill(name: str) -> dict[str, Any]:
        """One skill with the text of its SKILL.md."""
        return skill_page(name)

    @app.put("/api/skills/{name}")
    def save_skill(name: str, body: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
        """Save a SKILL.md as written in the editor (the skills of Orion's own and of plugins can't be changed)."""
        content = body.get("content")
        if not isinstance(content, str):
            raise HTTPException(400, 'Send {"content": "the whole SKILL.md"}.')
        saved = rt.skills.save(name, content)
        skill_done("saved", saved.name, f"Saved the {saved.name} skill")
        return skill_page(saved.name)

    @app.patch("/api/skills/{name}")
    async def switch_skill(name: str, body: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
        """Switch a skill on or off."""
        enabled = switch_of(body)
        if enabled is None:
            raise HTTPException(400, 'Send {"enabled": …}.')
        found = rt.skills.entry(name)
        if found is None:
            raise LookupError(f"There's no skill called {skill_key(name) or name}.")
        off = [other for other in rt.cfg.skills.disabled if other != found.name]
        await rt.apply_own({"skills.disabled": off if enabled else [*off, found.name]})
        rt.skills.invalidate()
        skill_done("enabled" if enabled else "disabled", found.name,
                   f"Switched the {found.name} skill {'on' if enabled else 'off'}")
        return skill_page(found.name)

    @app.delete("/api/skills/{name}")
    def delete_skill(name: str) -> dict[str, bool]:
        """Delete a skill of the user's (the skills of Orion's own and of plugins are switched off instead)."""
        if not rt.skills.delete(name):
            raise LookupError(f"There's no skill called {skill_key(name) or name}.")
        skill_done("deleted", skill_key(name), f"Deleted the {skill_key(name)} skill")
        return {"ok": True}

    # ------------------------------------------------------------------------------------------------ plugins
    @app.get("/api/plugins")
    def plugins() -> dict[str, Any]:
        """The plugins installed."""
        return {"items": rt.library.plugins()}

    @app.get("/api/plugins/discover")
    def discover_plugins(q: str = "", category: str = "", marketplace: str = "") -> dict[str, Any]:
        """The plugins of the marketplaces, for the Discover tab."""
        return rt.library.discover(query=q, category=category, marketplace=marketplace)

    @app.get("/api/plugins/marketplaces")
    def marketplaces() -> dict[str, Any]:
        """The marketplaces plugins can be installed from."""
        return {"items": rt.library.marketplaces()}

    @app.post("/api/plugins/marketplaces")
    async def add_marketplace(body: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
        """Add a marketplace: a git repository with a .claude-plugin/marketplace.json."""
        url = body.get("url")
        if not isinstance(url, str) or not url.strip():
            raise HTTPException(400, 'Send {"url": "owner/repo"}.')
        return await rt.library.add_marketplace(url.strip())

    @app.delete("/api/plugins/marketplaces/{name}")
    async def remove_marketplace(name: str) -> dict[str, bool]:
        """Forget a marketplace (the plugins installed from it stay)."""
        await rt.library.remove_marketplace(name)
        return {"ok": True}

    @app.post("/api/plugins/refresh")
    async def refresh_marketplaces() -> dict[str, Any]:
        """Fetch the marketplaces and the skill sources again now (otherwise they are fetched daily)."""
        await rt.library.refresh(force=True)
        return {"items": rt.library.marketplaces()}

    @app.post("/api/plugins")
    async def install_plugin(request: Request) -> dict[str, Any]:
        """Install a plugin from a marketplace (``{"marketplace", "name"}``) or an uploaded .zip or .plugin."""
        if is_upload(request):
            data, filename = await upload(request)
            return await rt.library.upload(data, filename)
        body = await json_body(request)
        market, name = body.get("marketplace"), body.get("name")
        if not isinstance(market, str) or not isinstance(name, str) or not market or not name:
            raise HTTPException(400, 'Send {"marketplace": …, "name": …} or a .zip or .plugin.')
        return await rt.library.install(market, name)

    @app.patch("/api/plugins/{name}")
    async def switch_plugin(name: str, body: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
        """Switch a plugin on or off (its skills and connectors with it)."""
        enabled = switch_of(body)
        if enabled is None:
            raise HTTPException(400, 'Send {"enabled": …}.')
        return await rt.library.set_enabled(name, enabled)

    @app.post("/api/plugins/{name}/update")
    async def update_plugin(name: str) -> dict[str, Any]:
        """Update a plugin to its marketplace's current version."""
        return await rt.library.update(name)

    @app.delete("/api/plugins/{name}")
    async def remove_plugin(name: str) -> dict[str, bool]:
        """Remove a plugin, its connectors and their secrets with it."""
        await rt.library.remove(name)
        return {"ok": True}
