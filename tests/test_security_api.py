"""The security pages' backend: what a settings change does with a secret (the vault gets it, config.yaml keeps a
reference, a hand-typed one is moved too, none is kept without a vault), the vault's and the limits' endpoints, the
activity log's page, pairings in the log, and every HUD event redacted on its way out."""

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orion import redact, secrets
from orion.activity import Activity
from orion.base import Tool
from orion.config import Refused, Settings, default_config
from orion.memory.store import Store
from orion.pairing import Pairing
from orion.redact import Redactor
from orion.runtime import Runtime
from orion.secrets import Vault
from orion.server import Envelope, add_pairing, create_app, encode

MEMORY = SimpleNamespace(db_timeout_s=5)
KEY = "sk-or-v1-" + "0123456789abcdef" * 4
TOKEN = "discord-bot-token-Zx9Qm4Lp7Rk2Vb8Nc5Td1Wh6"
TOOLS = [("system", True, Tool("power", "", {}, "system", "power", title="Suspend, restart or shut down",
                                hints={"read_only": False, "destructive": True}, builtin=True)),
         ("system", True, Tool("set_timer", "", {}, "organizer", "set_timer", title="Start a timer",
                                hints={"read_only": False, "destructive": False}, builtin=True)),
         ("home-assistant", False, Tool("home_assistant__turn_on", "", {}, "home-assistant", "turn_on", title="Turn on"))]


class Fixture:
    """A Runtime that was never built (no models, no tools process) with just what these pages touch."""

    def __init__(self, tmp_path: Path, *, with_vault: bool = True) -> None:
        self.path = tmp_path / "data" / "config.yaml"
        settings = Settings.open(self.path, role="main")
        settings.cfg.data_dir = str(tmp_path / "data")
        settings.sync_file()
        self.vault = Vault("files", tmp_path / "vault") if with_vault else None
        self.rt = Runtime(settings, microphone=False, vault=self.vault,
                          vault_error="" if with_vault else "no keyring answers on this machine")
        self.store = Store(tmp_path / "data" / "orion.db", MEMORY, role="main")
        self.store.ensure_schema()
        self.events: list[tuple[str, dict]] = []
        self.rt.activity = Activity(self.store, lambda kind, **data: self.events.append((kind, data)), redact.redacted)
        self.rt._publish_capabilities = lambda: None  # type: ignore[method-assign]
        self.rt.hub = SimpleNamespace(offered=lambda: TOOLS, configure=lambda: asyncio.sleep(0))  # type: ignore[assignment]
        self.rt.controls = None  # type: ignore[assignment]
        self.rt._switch_llm = lambda: asyncio.sleep(0)  # type: ignore[method-assign]  # (a new key would swap the brain)
        self.rt.memory = SimpleNamespace(reindex=lambda: None, store=self.store)  # type: ignore[assignment]
        self.rt.spend = SimpleNamespace(changed=lambda: None)  # type: ignore[assignment]
        self.rt.documents = SimpleNamespace(reconfigure=lambda: None)  # type: ignore[assignment]

    def log(self) -> list[dict]:
        self.store.submit(lambda: None).result(timeout=5)
        return list(reversed(self.rt.activity.recent(limit=100)))

    def text(self) -> str:
        return self.path.read_text()


@pytest.fixture
def fx(tmp_path: Path):
    mine = Fixture(tmp_path)
    with redact.installed(Redactor(mine.rt.cfg.safety.secret_patterns, min_chars=8)), secrets.installed(mine.vault):
        yield mine
    mine.store.close()


def commit(fx: Fixture, patch: dict, actor: str = "hud") -> dict:
    changes = fx.rt.settings.validate(patch, actor=actor, own=True)  # type: ignore[arg-type]
    return asyncio.run(fx.rt._commit(changes, "hud"))


# ---------------------------------------------------------------------------------------------------- settings changes
def test_a_secret_typed_in_the_hud_goes_to_the_vault_and_the_file_keeps_a_reference(fx: Fixture) -> None:
    result = commit(fx, {"openrouter": {"api_key": KEY}})
    assert result["changed"] == ["openrouter.api_key"]
    assert KEY not in fx.text() and "${secret:openrouter.api_key}" in fx.text()
    assert fx.vault.get("openrouter.api_key") == KEY
    assert fx.rt.cfg.openrouter.api_key == "${secret:openrouter.api_key}" and fx.rt.cfg.openrouter.key == KEY
    assert redact.redacted(f"k={KEY}") == "k=[secret]"  # hidden from the very moment it was set
    commit(fx, {"openrouter": {"api_key": ""}})  # taken out in the HUD: gone from the vault too
    assert fx.vault.get("openrouter.api_key") is None and "${secret:openrouter.api_key}" not in fx.text()


def test_a_connector_installed_with_its_token_is_kept_in_the_vault_not_the_file(fx: Fixture) -> None:
    servers = {"discord": {"command": "orion", "args": ["discord-serve"], "intents": True,
                           "env": {"DISCORD_TOKEN": TOKEN, "ORION_DISCORD_USERS": "42"}}}
    commit(fx, {"mcp_servers": servers})
    assert TOKEN not in fx.text() and "ORION_DISCORD_USERS" in fx.text()
    assert fx.rt.cfg.mcp_servers["discord"]["env"]["DISCORD_TOKEN"] == "${secret:connector.discord.discord_token}"
    assert fx.vault.get("connector.discord.discord_token") == TOKEN
    commit(fx, {"mcp_servers": {}})  # removed: its token goes with it
    assert fx.vault.get("connector.discord.discord_token") is None


def test_a_calendar_removed_takes_its_secrets_along(fx: Fixture) -> None:
    sources = {"home": {"caldav_url": "https://caldav.icloud.com/", "username": "me", "password": "app-pass-word"}}
    commit(fx, {"calendar": {"sources": sources}})
    assert "app-pass-word" not in fx.text() and fx.vault.get("calendar.home.password") == "app-pass-word"
    asyncio.run(fx.rt.remove_calendar("home"))
    assert fx.vault.names() == ()


def test_without_a_vault_no_secret_is_ever_written_to_the_file(tmp_path: Path) -> None:
    mine = Fixture(tmp_path, with_vault=False)
    try:
        before = mine.text()
        with pytest.raises(Refused, match="vault isn't available"):
            commit(mine, {"openrouter": {"api_key": KEY}})
        assert mine.text() == before and KEY not in mine.text() and mine.rt.cfg.openrouter.api_key == ""
        commit(mine, {"hud": {"sounds": False}})  # everything else still works
        assert commit(mine, {"openrouter": {"api_key": "${OPENROUTER_API_KEY}"}})["changed"]  # a variable isn't a secret
    finally:
        mine.store.close()


def test_a_secret_typed_into_config_yaml_by_hand_is_moved_when_he_notices(fx: Fixture) -> None:
    commit(fx, {"calendar": {"sources": {"work": {"ics_url": "${WORK_ICS}"}}}})
    text = fx.text().replace('${WORK_ICS}', "https://calendar.example/ical/secret-token-0123456789/basic.ics")
    assert text != fx.text()
    fx.path.write_text(text)
    asyncio.run(fx.rt._config_changed())
    assert "secret-token-0123456789" not in fx.text() and "${secret:calendar.work.ics_url}" in fx.text()
    assert fx.vault.get("calendar.work.ics_url").endswith("secret-token-0123456789/basic.ics")
    assert fx.rt.cfg.calendar.sources["work"]["ics_url"] == "${secret:calendar.work.ics_url}"
    assert redact.redacted("see secret-token-0123456789 here").count("[secret]") == 0  # (a part of it: not the secret)


# ---------------------------------------------------------------------------------------------------- the vault's page
def test_the_vault_shows_names_whether_they_are_set_and_what_uses_them_never_values(fx: Fixture) -> None:
    commit(fx, {"openrouter": {"api_key": KEY}, "mcp_servers": {"mail": {"command": "uvx", "env": {
        "MCP_EMAIL_SERVER_PASSWORD": "app-password!", "X_TOKEN": "${secret:connector.mail.missing}"}}}})
    fx.vault.set("backup.restic.password", "a-restic-password")
    view = fx.rt.secrets_view()
    assert view["store"] == "keyring" and view["ok"] is True and view["error"] == ""
    assert view["items"] == [
        {"name": "backup.restic.password", "set": True, "used_by": []},
        {"name": "connector.mail.mcp_email_server_password", "set": True, "used_by": ["mail"]},
        {"name": "connector.mail.missing", "set": False, "used_by": ["mail"]},
        {"name": "openrouter.api_key", "set": True, "used_by": ["openrouter.api_key"]}]
    assert KEY not in json.dumps(view) and "app-password!" not in json.dumps(view)


def test_keeping_and_forgetting_a_secret_is_recorded_by_name_and_hidden_at_once(fx: Fixture) -> None:
    fx.rt.redactor = redact.active()
    redact.active()._source = secrets.Known(fx.vault, {})  # as protect() makes it
    fx.rt.keep_secret("backup.restic.password", "correct-horse-battery-staple")
    assert fx.vault.get("backup.restic.password") == "correct-horse-battery-staple"
    assert redact.redacted("pw correct-horse-battery-staple") == "pw [secret]"
    fx.rt.forget_secret("backup.restic.password")
    assert redact.redacted("pw correct-horse-battery-staple") == "pw correct-horse-battery-staple"
    entries = [(e["kind"], e["title"], e["outcome"], e["actor"], e["source"]) for e in fx.log()]
    assert entries == [("secret", "Set the secret backup.restic.password", "ok", "you, in the HUD", "hud"),
                       ("secret", "Deleted the secret backup.restic.password", "ok", "you, in the HUD", "hud")]
    assert "correct-horse" not in json.dumps(fx.log()) and fx.events[0][0] == "activity"
    with pytest.raises(LookupError, match="isn't set"):
        fx.rt.forget_secret("nothing.here")
    with pytest.raises(Refused, match="isn't a secret's name"):
        fx.rt.keep_secret("Not A Name", "value-value")


def test_with_no_vault_the_page_says_why_and_nothing_can_be_kept(tmp_path: Path) -> None:
    mine = Fixture(tmp_path, with_vault=False)
    try:
        assert mine.rt.secrets_view() == {"store": "keyring", "ok": False, "error": "no keyring answers on this machine",
                                          "items": []}
        with pytest.raises(Refused, match="vault isn't available: no keyring answers"):
            mine.rt.keep_secret("a.b", "value-value")
    finally:
        mine.store.close()


# ---------------------------------------------------------------------------------------------------- the limits' page
def test_the_safety_page_lists_limits_the_sandbox_and_what_a_limit_can_name(fx: Fixture) -> None:
    view = fx.rt.safety_view()
    assert set(view["channels"]) == {"voice", "hud", "discord", "job", "app"} and "power" in view["channels"]["discord"]["never"]
    sandbox_view = view["sandbox"]
    assert set(sandbox_view) >= {"available", "commands", "network", "hidden", "writable"}
    assert sandbox_view["available"] in ("bwrap", "container", "") and "~/.ssh" in sandbox_view["hidden"]
    assert view["choices"]["classes"] == ["@read", "@change", "@destroy", "@open_world"]
    assert view["choices"]["tools"] == [
        {"name": "home_assistant__turn_on", "title": "Turn on", "connector": "home-assistant", "risk": "destroy"},
        {"name": "power", "title": "Suspend, restart or shut down", "connector": "system", "risk": "destroy"},
        {"name": "set_timer", "title": "Start a timer", "connector": "organizer", "risk": "change"}]


def test_a_channels_limits_are_set_checked_saved_and_recorded(fx: Fixture) -> None:
    result = asyncio.run(fx.rt.set_channel("hud", ["power", "@destroy", "power"], ["system:*"]))
    assert result["changed"] == ["safety.channels"]
    assert fx.rt.cfg.safety.channels["hud"] == {"never": ["power", "@destroy"], "confirm": ["system:*"]}
    assert fx.rt.cfg.safety.channels["discord"]["never"]  # the others as they were
    assert "system:*" in fx.text()
    assert Settings.open(fx.path, role="main").cfg.safety.channels["hud"]["never"] == ["power", "@destroy"]
    assert [e["title"] for e in fx.log()] == ["Changed what hud may do"]
    with pytest.raises(Refused, match="isn't a tool"):
        asyncio.run(fx.rt.set_channel("hud", ["not a tool!"], []))
    with pytest.raises(LookupError, match="no channel called web"):
        asyncio.run(fx.rt.set_channel("web", [], []))
    assert fx.rt.cfg.safety.channels["hud"]["confirm"] == ["system:*"]  # a refusal changed nothing


# ---------------------------------------------------------------------------------------------------- over HTTP
def test_the_endpoints_over_http(fx: Fixture, tmp_path: Path) -> None:
    app = create_app(fx.rt, Pairing(tmp_path / "pair", fx.rt.cfg.server))
    web = TestClient(app, base_url="http://localhost:8765")
    for n in range(5):
        fx.rt.activity.record("tool", source="voice", title=f"Set timer {n}", tool="set_timer", outcome="ok")
    fx.rt.activity.record("deny", source="discord", title="Refused Power", tool="power", outcome="denied")
    fx.log()
    page = web.get("/api/activity?limit=4").json()
    assert [i["title"] for i in page["items"]] == ["Refused Power", "Set timer 4", "Set timer 3", "Set timer 2"]
    assert page["next"] == page["items"][-1]["id"]
    rest = web.get(f"/api/activity?limit=4&before={page['next']}").json()
    assert [i["title"] for i in rest["items"]] == ["Set timer 1", "Set timer 0"] and rest["next"] is None
    assert [i["kind"] for i in web.get("/api/activity?kind=deny,block").json()["items"]] == ["deny"]
    assert len(web.get("/api/activity?source=voice").json()["items"]) == 5
    assert [i["title"] for i in web.get("/api/activity?q=POWER").json()["items"]] == ["Refused Power"]
    assert set(web.get("/api/activity").json()["items"][0]) >= {"id", "at", "kind", "source", "actor", "connector", "tool",
                                                                 "title", "detail", "outcome", "turn", "ms"}
    assert web.put("/api/secrets/backup.restic.password", json={"value": "hunter2-hunter2"}).json() == {"ok": True}
    shown = web.get("/api/secrets").json()
    assert shown["items"] == [{"name": "backup.restic.password", "set": True, "used_by": []}]
    assert "hunter2" not in json.dumps(shown) and web.put("/api/secrets/x.y", json={"value": ""}).status_code == 422
    assert web.put("/api/secrets/Bad Name", json={"value": "abcdefgh"}).status_code in (400, 404)
    assert web.delete("/api/secrets/backup.restic.password").json() == {"ok": True}
    assert web.delete("/api/secrets/backup.restic.password").status_code == 404
    safety = web.get("/api/safety").json()
    assert safety["channels"]["voice"] == {"never": [], "confirm": ["power"]} and safety["sandbox"]["commands"] is True
    saved = web.put("/api/safety/channels/job", json={"never": ["power"], "confirm": []})
    assert saved.status_code == 200 and saved.json()["changed"] == ["safety.channels"]
    assert web.get("/api/safety").json()["channels"]["job"] == {"never": ["power"], "confirm": []}
    assert web.put("/api/safety/channels/job", json={"never": ["???"], "confirm": []}).status_code == 400
    assert web.put("/api/safety/channels/web", json={"never": [], "confirm": []}).status_code == 404
    assert web.get("/activity").status_code in (200, 503)  # the HUD's page (503 when the HUD isn't built here)


def test_pairings_and_refused_codes_are_in_the_log(fx: Fixture, tmp_path: Path) -> None:
    settings = default_config().server.model_copy(update={"host": "0.0.0.0"})
    pairing = Pairing(tmp_path / "pair", settings)
    app = FastAPI()
    add_pairing(app, pairing, settings, fx.rt.activity)
    wrapped = Envelope(app, server=settings, pairing=pairing, names=("mypc", "mypc.local"))
    pc = TestClient(wrapped, base_url="http://localhost:8765", client=("127.0.0.1", 40000))
    phone = TestClient(wrapped, base_url="http://mypc.local:8765", client=("192.168.1.20", 40001))
    assert phone.post("/api/pairing", json={"code": "00000000", "name": "Phone"}).status_code == 403
    code = pc.post("/api/pairing/code").json()["code"]
    assert phone.post("/api/pairing", json={"code": code, "name": "Phone"}).status_code == 200
    device = pc.get("/api/pairing").json()["devices"][0]["id"]
    pc.delete(f"/api/pairing/devices/{device}")
    assert [(e["kind"], e["title"], e["outcome"], e["actor"]) for e in fx.log()] == [
        ("login", "A pairing code from “Phone” was refused", "denied", "Phone"),
        ("login", "Paired “Phone”", "ok", "Phone"), ("login", "Unpaired a device", "ok", "you, at the PC")]
    assert code not in json.dumps(fx.log())


# ---------------------------------------------------------------------------------------------------- the HUD's events
def test_every_event_a_hud_receives_is_redacted_field_by_field(fx: Fixture) -> None:
    fx.vault.set("calendar.home.password", "an-app-password-123")
    redact.active().learn(fx.vault.values())
    event = {"type": "tool", "phase": "end", "result": "the password is an-app-password-123!", "args": {"a": ["x", "an-app-password-123"]},
             "n": 3, "ok": True}
    sent = json.loads(encode(event))
    assert sent == {"type": "tool", "phase": "end", "result": "the password is [secret]!", "args": {"a": ["x", "[secret]"]},
                    "n": 3, "ok": True}
    assert event["result"].endswith("123!")  # the event itself is left as it was (it goes to other clients too)
    clean = {"type": "level", "source": "mic", "value": 0.5}
    started = time.perf_counter()
    for _ in range(2000):
        encode(clean)
    assert (time.perf_counter() - started) / 2000 < 5e-4  # a level event at 30 Hz costs nothing noticeable
