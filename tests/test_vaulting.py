"""vaulting: a plaintext secret in the settings moves to the vault and a reference stays; what nothing refers to any more is
forgotten; a hand-written config.yaml and its backup are left holding no value."""

import copy
import json
import logging
from pathlib import Path

import pytest
import yaml

from orion import config as c
from orion import vaulting
from orion.secrets import Vault, VaultError, has_reference
from orion.vaulting import scrub_backup, secure, used_by

KEY = "sk-or-v1-" + "0123456789abcdef" * 4
TOKEN = "discord-bot-token-Zx9Qm4Lp7Rk2Vb8Nc5Td1Wh6"


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    return Vault("files", tmp_path / "vault")


@pytest.mark.parametrize(("text", "reference"), [
    ("${secret:a.b}", True), ("${HASS_TOKEN}", True), ("Bearer ${HASS_TOKEN}", True), ("$HOME/x", True),
    ("$UNSET_VARIABLE_XYZ", False), ("pa$word-with-dollar", False), ("plain", False), ("", False),
    ("${not valid}", False), ("100$", False),
])
def test_what_counts_as_a_reference_and_what_as_a_value(text: str, reference: bool) -> None:
    assert has_reference(text, {"HOME": "/home/x", "HASS_TOKEN": "t"}) is reference


def test_the_openrouter_key_goes_to_the_vault_and_a_reference_stays(vault: Vault) -> None:
    assert secure({"openrouter.api_key": f"  {KEY} "}, {}, vault) == {"openrouter.api_key": "${secret:openrouter.api_key}"}
    assert vault.get("openrouter.api_key") == KEY
    for kept in ("${secret:openrouter.api_key}", "${OPENROUTER_API_KEY}", ""):
        assert secure({"openrouter.api_key": kept}, {}, vault) == {"openrouter.api_key": kept}
    assert vault.get("openrouter.api_key") == KEY
    # taken out: the key is forgotten, in the vault too
    assert secure({"openrouter.api_key": ""}, {"openrouter.api_key": "${secret:openrouter.api_key}"}, vault) == {
        "openrouter.api_key": ""}
    assert vault.get("openrouter.api_key") is None


def test_a_calendars_secret_fields_move_and_the_rest_stays(vault: Vault) -> None:
    sources = {"home": {"caldav_url": "https://caldav.icloud.com/", "username": "me@icloud.com", "password": "app-pass-word",
                        "read_only": False},
               "work": {"ics_url": "https://calendar.google.com/calendar/ical/secret-token/basic.ics"},
               "mine": {"caldav_url": "${CALDAV_URL}", "password": "${secret:calendar.mine.password}"}}
    before = copy.deepcopy(sources)
    out = secure({"calendar.sources": sources}, {}, vault)["calendar.sources"]
    assert sources == before  # the input is left as it is
    assert out["home"] == {"caldav_url": "${secret:calendar.home.caldav_url}", "username": "me@icloud.com",
                           "password": "${secret:calendar.home.password}", "read_only": False}
    assert out["work"] == {"ics_url": "${secret:calendar.work.ics_url}"} and out["mine"] == sources["mine"]
    assert vault.get("calendar.home.password") == "app-pass-word"
    assert vault.get("calendar.work.ics_url").endswith("secret-token/basic.ics") and "calendar.mine.password" not in vault.names()
    # a source removed: what it kept in the vault goes with it; one changed keeps its secrets
    fewer = {"home": out["home"]}
    assert secure({"calendar.sources": fewer}, {"calendar.sources": out}, vault) == {"calendar.sources": fewer}
    assert set(vault.names()) == {"calendar.home.caldav_url", "calendar.home.password"}


def test_a_servers_secret_values_move_and_hosts_and_variables_stay(vault: Vault) -> None:
    servers = {
        "discord": {"command": "orion", "args": ["discord-serve"], "intents": True,
                    "env": {"DISCORD_TOKEN": TOKEN, "ORION_DISCORD_USERS": "1234567890"}},
        "home-assistant": {"url": "http://ha.local/api/mcp", "headers": {"Authorization": "Bearer ha-long-lived-token",
                                                                           "X-Other": "Bearer ${HASS_TOKEN}"}},
        "mail": {"command": "uvx", "env": {"MCP_EMAIL_SERVER_PASSWORD": "app-password!", "MCP_EMAIL_SERVER_IMAP_HOST": "imap.x.com",
                                           "MCP_EMAIL_SERVER_EMAIL_ADDRESS": "me@x.com"}},
        "Odd Name!": {"command": "x", "env": {"my.api-key": "k" * 20}},
        "plain": {"command": "uvx", "args": ["--token", "not-an-env-value"]},
    }
    out = secure({"mcp_servers": servers}, {}, vault)["mcp_servers"]
    assert out["discord"]["env"] == {"DISCORD_TOKEN": "${secret:connector.discord.discord_token}",
                                     "ORION_DISCORD_USERS": "1234567890"}
    assert out["home-assistant"]["headers"] == {"Authorization": "${secret:connector.home-assistant.authorization}",
                                                "X-Other": "Bearer ${HASS_TOKEN}"}
    assert out["mail"]["env"]["MCP_EMAIL_SERVER_PASSWORD"] == "${secret:connector.mail.mcp_email_server_password}"
    assert out["mail"]["env"]["MCP_EMAIL_SERVER_IMAP_HOST"] == "imap.x.com" and out["plain"] == servers["plain"]
    assert out["Odd Name!"]["env"] == {"my.api-key": "${secret:connector.odd-name.my-api-key}"}
    assert vault.get("connector.discord.discord_token") == TOKEN and vault.get("connector.home-assistant.authorization") == (
        "Bearer ha-long-lived-token")
    # a server removed takes its secrets along; its OAuth state (kept by the connectors) is not touched here
    vault.set("connector.mail.oauth", json.dumps({"access_token": "a" * 20}))
    without = {name: value for name, value in out.items() if name != "mail"}
    secure({"mcp_servers": without}, {"mcp_servers": out}, vault)
    assert "connector.mail.mcp_email_server_password" not in vault.names() and "connector.mail.oauth" in vault.names()
    assert "connector.discord.discord_token" in vault.names()


def test_a_secret_that_two_places_use_is_kept_while_one_does(vault: Vault) -> None:
    vault.set("connector.shared.token", "shared-token-value")
    old = {"mcp_servers": {"a": {"env": {"T": "${secret:connector.shared.token}"}},
                           "b": {"env": {"T": "${secret:connector.shared.token}"}}}}
    new = {"mcp_servers": {"a": old["mcp_servers"]["a"]}}
    secure(new, old, vault)
    assert vault.get("connector.shared.token") == "shared-token-value"


def test_a_vault_that_refuses_leaves_the_value_where_it_was(vault: Vault, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(name: str, value: str) -> None:
        raise VaultError("the keyring refused")

    monkeypatch.setattr(vault, "set", refuse)
    with pytest.raises(VaultError, match="keyring refused"):
        secure({"openrouter.api_key": KEY}, {}, vault)


def test_what_uses_each_secret_is_listed_by_name() -> None:
    cfg = c.default_config()
    cfg.openrouter.api_key = "${secret:openrouter.api_key}"
    cfg.calendar.sources = {"home": {"password": "${secret:calendar.home.password}", "ics_url": "x"}}
    cfg.mcp_servers = {"discord": {"env": {"DISCORD_TOKEN": "${secret:connector.discord.discord_token}"}},
                       "b": {"args": ["${secret:connector.discord.discord_token}"]}}
    assert used_by(cfg) == {"openrouter.api_key": ["openrouter.api_key"], "calendar.home.password": ["calendar:home"],
                            "connector.discord.discord_token": ["discord", "b"]}


# ---------------------------------------------------------------------------------------------------- the files
def written(tmp_path: Path) -> Path:
    path = tmp_path / "data" / "config.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump({
        "version": 2, "openrouter": {"api_key": KEY}, "future_section": {"x": 1},  # (unknown: the file gets backed up)
        "calendar": {"sources": {"home": {"caldav_url": "https://caldav.icloud.com/", "password": "app-pass-word"}}},
        "mcp_servers": {"discord": {"command": "orion", "env": {"DISCORD_TOKEN": TOKEN, "ORION_DISCORD_USERS": "42"}}}}))
    return path


def test_a_config_written_with_secrets_is_left_holding_none_and_so_is_its_backup(tmp_path: Path, vault: Vault) -> None:
    path = written(tmp_path)
    settings = c.Settings.open(path, role="main")
    assert vaulting.prepare(settings, vault) == 3
    assert settings.sync_file()  # (rewritten with references: the old file becomes config.yaml.bak)
    assert scrub_backup(path, vault)
    for file in (path, path.with_name("config.yaml.bak")):
        text = file.read_text()
        assert file.exists() and not any(secret in text for secret in (KEY, TOKEN, "app-pass-word")), file.name
        assert "${secret:openrouter.api_key}" in text and "${secret:connector.discord.discord_token}" in text
        assert "${secret:calendar.home.password}" in text and "ORION_DISCORD_USERS" in text
    assert vault.get("openrouter.api_key") == KEY and vault.get("connector.discord.discord_token") == TOKEN
    again = c.Settings.open(path, role="main")  # read back: references, valid
    assert again.cfg.openrouter.api_key == "${secret:openrouter.api_key}" and not again.problems
    assert vaulting.prepare(again, vault) == 0  # nothing left to move


def test_a_backup_keeps_a_secret_the_vault_lacks_and_never_overwrites_a_newer_one(tmp_path: Path, vault: Vault) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("version: 2\n")
    bak = tmp_path / "config.yaml.bak"
    bak.write_text(f"openrouter:\n  api_key: '{KEY}'\nmcp_servers:\n  old:\n    env:\n      OLD_TOKEN: \"quoted \\\"tok\\\" 12345\"\n")
    vault.set("openrouter.api_key", "the-newer-key-0123456789")
    assert scrub_backup(path, vault)
    text = bak.read_text()
    assert KEY not in text and "quoted" not in text and "${secret:openrouter.api_key}" in text
    assert vault.get("openrouter.api_key") == "the-newer-key-0123456789"  # the current one wins
    assert vault.get("connector.old.old_token") == 'quoted "tok" 12345'  # an old one nothing else holds is kept
    assert not scrub_backup(tmp_path / "other.yaml", vault)


def test_a_backup_that_cannot_be_made_clean_is_deleted(tmp_path: Path, vault: Vault, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "config.yaml"
    bak = tmp_path / "config.yaml.bak"
    bak.write_text("this: [is not\nvalid yaml")
    with caplog.at_level(logging.WARNING, logger="orion.vaulting"):
        assert scrub_backup(path, vault) and not bak.exists()
    assert "config.yaml.bak can't be made free of secrets" in caplog.text
    folded = tmp_path / "config.yaml.bak"
    folded.write_text("openrouter:\n  api_key: >-\n    sk-or-v1-0123456789abcdef\n    0123456789abcdef0123456789abcdef\n")  # folded text
    assert scrub_backup(path, vault) and not folded.exists()  # (the value isn't in the text as one run: not trusted)
