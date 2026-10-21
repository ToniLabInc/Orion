"""secrets: the vault keeps values in the chosen store and only names beside them, sees another process's changes,
resolves ${secret:NAME} (a connector only its own), never falls back from the keyring to files, and says what is
secret in an environment (which commands and servers don't inherit) for the redactor to hide."""

import json
import logging
import stat

import pytest

from orion import redact, secrets
from orion.config import default_config
from orion.secrets import (
    Known,
    Vault,
    VaultError,
    expand,
    expand_all,
    inherited,
    looks_secret,
    parts,
    protect,
    reference,
    references,
    secret_environ,
    strip_environ,
)


class FakeKeyring:
    """An in-memory keyring (the tests never touch the real one)."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, name: str) -> str | None:
        return self.items.get((service, name))

    def set_password(self, service: str, name: str, value: str) -> None:
        self.items[(service, name)] = value

    def delete_password(self, service: str, name: str) -> None:
        del self.items[(service, name)]


@pytest.fixture
def keyring(monkeypatch) -> FakeKeyring:
    fake = FakeKeyring()
    monkeypatch.setattr(secrets, "_keyring", lambda: fake)
    return fake


def test_files_keep_each_value_private_and_names_apart(tmp_path) -> None:
    """A file per secret (0600) in a 0700 folder; the names file lists names only."""
    vault = Vault("files", tmp_path / "vault")
    vault.set("openrouter.api_key", "sk-or-v1-abc")
    vault.set("connector.notion.oauth", '{"access_token": "t"}')
    assert vault.get("openrouter.api_key") == "sk-or-v1-abc"
    assert vault.names() == ("connector.notion.oauth", "openrouter.api_key")
    folder = tmp_path / "vault"
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert stat.S_IMODE((folder / "openrouter.api_key").stat().st_mode) == 0o600
    listed = (folder / "names.json").read_text()
    assert "sk-or-v1-abc" not in listed and json.loads(listed)["names"] == sorted(vault.names())
    assert vault.delete("openrouter.api_key") and not vault.delete("openrouter.api_key")
    assert vault.get("openrouter.api_key") is None and not (folder / "openrouter.api_key").exists()


def test_the_keyring_holds_values_under_this_orions_own_service(tmp_path, keyring) -> None:
    """Values go to the keyring, named per vault, so two Orions on one machine never share an entry."""
    first, second = Vault("keyring", tmp_path / "a"), Vault("keyring", tmp_path / "b")
    first.set("discord.token", "one")
    second.set("discord.token", "two")
    assert (first.get("discord.token"), second.get("discord.token")) == ("one", "two")
    services = {service for service, _ in keyring.items}
    assert len(services) == 2 and all(s.startswith("orion/") for s in services)
    assert "one" not in (tmp_path / "a" / "names.json").read_text()
    assert Vault("keyring", tmp_path / "a").get("discord.token") == "one"  # the same id the next time


def test_another_process_sees_a_change_at_its_next_read(tmp_path) -> None:
    """Two vaults on one folder (Orion and his tools process): a set or a delete in one is read by the other."""
    main, tools = Vault("files", tmp_path / "v"), Vault("files", tmp_path / "v")
    assert tools.get("calendar.home") is None
    main.set("calendar.home", "pw1")
    assert tools.get("calendar.home") == "pw1"
    main.set("calendar.home", "pw2")  # a new value for a name that is there: seen too, even within one clock tick
    assert tools.get("calendar.home") == "pw2"
    main.set("calendar.home", "pw3")
    assert tools.get("calendar.home") == "pw3"
    main.delete("calendar.home")
    assert tools.get("calendar.home") is None


def test_every_change_gives_the_vault_a_new_revision(tmp_path) -> None:
    """The names file is rewritten by each set and delete, and its stamp can't repeat: a redactor in another process
    asks only for that."""
    vault = Vault("files", tmp_path / "v")
    seen = [vault.revision()]
    assert seen == [None]
    for step in range(30):
        vault.set("a.key", f"value-{step}")
        seen.append(vault.revision())
    vault.delete("a.key")
    seen.append(vault.revision())
    assert len(set(seen)) == len(seen)


def test_references_resolve_and_a_missing_secret_says_so(tmp_path) -> None:
    vault = Vault("files", tmp_path / "v")
    vault.set("ha.token", "abc")
    assert reference("ha.token") == "${secret:ha.token}"
    assert references("Bearer ${secret:ha.token} ${secret:x.y}") == ["ha.token", "x.y"]
    assert vault.resolve("Bearer ${secret:ha.token}") == "Bearer abc"
    with pytest.raises(VaultError, match="x.y isn't set"):
        vault.resolve("${secret:x.y}")


def test_values_are_longest_first_for_the_redactor(tmp_path) -> None:
    vault = Vault("files", tmp_path / "v")
    vault.set("a", "short")
    vault.set("b", "a-much-longer-secret")
    assert vault.values() == ["a-much-longer-secret", "short"]


@pytest.mark.parametrize("name", ["", "Upper", "-lead", "has space", "../escape", "a/b", "x" * 129])
def test_names_that_are_not_names_are_refused(tmp_path, name) -> None:
    vault = Vault("files", tmp_path / "v")
    with pytest.raises(VaultError):
        vault.set(name, "value")


def test_no_keyring_is_an_error_not_a_fallback(tmp_path, monkeypatch) -> None:
    """Without a keyring the vault refuses to start: it never quietly keeps secrets in files instead."""
    def none():
        raise VaultError("no keyring answers on this machine (no Secret Service): set secrets.store to files")

    monkeypatch.setattr(secrets, "_keyring", none)
    with pytest.raises(VaultError, match="set secrets.store to files"):
        Vault("keyring", tmp_path / "v")
    assert not list((tmp_path / "v").glob("*"))  # nothing written but the (empty) folder
    with pytest.raises(VaultError, match="keyring or files"):
        Vault("plaintext", tmp_path / "w")  # type: ignore[arg-type]
    with pytest.raises(VaultError, match="can't be empty"):
        Vault("files", tmp_path / "x").set("a", "")


# ---------------------------------------------------------------------------------------------------- filling in
@pytest.fixture
def vault(tmp_path) -> Vault:
    mine = Vault("files", tmp_path / "v")
    with secrets.installed(mine):
        yield mine


def test_expand_fills_secrets_and_variables_in_one_pass(vault, monkeypatch) -> None:
    vault.set("connector.mail.password", "p@$HOME-${USER}")
    monkeypatch.setenv("USER", "ana")
    env = {"USER": "ana", "HOME": "/home/ana"}
    assert expand("Bearer ${secret:connector.mail.password}", environ=env) == "Bearer p@$HOME-${USER}"  # not read again
    assert expand("$HOME/${USER}/$NOPE/${NOPE}", environ=env) == "/home/ana/ana/$NOPE/${NOPE}"  # unset: as written
    assert expand("no dollar here") == "no dollar here" and expand("${secret:UPPER} and $") == "${secret:UPPER} and $"
    assert expand_all({"a": ["${secret:connector.mail.password}", 1], "b": {"c": "$USER"}}, only="connector.mail.") == {
        "a": ["p@$HOME-${USER}", 1], "b": {"c": "ana"}}
    with pytest.raises(VaultError, match="x.y isn't set"):
        expand("${secret:x.y}")


def test_a_connector_fills_in_only_its_own_secrets(vault) -> None:
    vault.set("connector.mail.password", "mail-password")
    vault.set("openrouter.api_key", "sk-or-v1-the-brain-key")
    assert expand("${secret:connector.mail.password}", only="connector.mail.") == "mail-password"
    with pytest.raises(VaultError, match="isn't one of this server's secrets"):
        expand("${secret:openrouter.api_key}", only="connector.mail.")
    with pytest.raises(VaultError, match="connector.mail"):
        expand("${secret:connector.mailer.password}", only="connector.mail.")  # a longer name is another server's
    assert expand_all({"env": {"T": "${secret:connector.mail.password}"}}, only="connector.mail.") == {
        "env": {"T": "mail-password"}}


def test_without_a_vault_a_secret_cannot_be_filled_in() -> None:
    with secrets.installed(None), pytest.raises(VaultError, match="no vault is open"):
        expand("${secret:openrouter.api_key}")
    assert expand("${PATH}", environ={"PATH": "/bin"}) == "/bin"  # variables need no vault


def test_the_openrouter_key_may_be_a_secret_reference(vault) -> None:
    key = default_config().openrouter.model_copy(update={"api_key": "${secret:openrouter.api_key}"})
    assert key.config_key == ""  # not set yet
    vault.set("openrouter.api_key", "sk-or-v1-0123456789")
    assert key.config_key == "sk-or-v1-0123456789" and key.key == "sk-or-v1-0123456789"
    with secrets.installed(None):
        assert key.config_key == ""  # no vault: no key, and the reason is logged


# ---------------------------------------------------------------------------------------------------- the environment
@pytest.mark.parametrize(("name", "secret"), [
    ("OPENROUTER_API_KEY", True), ("CLAUDE_CODE_OAUTH_TOKEN", True), ("DISCORD_TOKEN", True), ("GITHUB_TOKEN", True),
    ("MCP_EMAIL_SERVER_PASSWORD", True), ("GOOGLE_OAUTH_CLIENT_SECRET", True), ("Authorization", True),
    ("X-Api-Key", True), ("Proxy-Authorization", True), ("DB_PASS", True), ("AWS_ACCESS_KEY_ID", True),
    ("SESSION_COOKIE", True), ("tokens", True), ("PRIVATE_KEY", True),
    ("GNOME_KEYRING_CONTROL", False), ("XAUTHORITY", False), ("PATH", False), ("PWD", False), ("LANG", False),
    ("COMPASS_X", False), ("SECRETARY", False), ("KEYBOARD", False), ("DISPLAY", False), ("HOME", False),
])
def test_names_that_say_their_value_is_secret(name: str, secret: bool) -> None:
    assert looks_secret(name) is secret


def test_what_commands_and_servers_inherit_is_an_allow_list(tmp_path) -> None:
    environ = {"LANG": "en_US.UTF-8", "LC_ALL": "C", "LC_TIME": "fr_FR", "TZ": "UTC", "HOME": "/home/x",
               "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-secret", "PATH": "/bin", "OPENROUTER_API_KEY": "sk-or-1",
               "DISPLAY": ":0", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"}
    allow = ["LANG", "LC_*", "TZ"]
    assert inherited(environ, allow) == {"LANG": "en_US.UTF-8", "LC_ALL": "C", "LC_TIME": "fr_FR", "TZ": "UTC"}
    assert inherited(environ, []) == {} and inherited(environ, ["LC_ALL*"]) == {"LC_ALL": "C"}
    assert set(strip_environ(environ)) == {"LANG", "LC_ALL", "LC_TIME", "TZ", "HOME", "PATH", "DISPLAY",
                                           "DBUS_SESSION_BUS_ADDRESS"}  # a helper program keeps its desktop
    socket = tmp_path / "agent.sock"
    socket.touch()
    found = secret_environ({**environ, "SSH_AUTH_SOCK": str(socket), "MY_TOKEN": "", "OTHER_TOKEN": "~/nope-xyz"})
    assert found == {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-secret", "OPENROUTER_API_KEY": "sk-or-1",
                     "OTHER_TOKEN": "~/nope-xyz"}  # a socket that exists is no secret; a path that doesn't, may be


def test_what_else_a_secret_holds() -> None:
    assert parts("Bearer abcdefghijklmnop") == {"abcdefghijklmnop"} and parts("basic dXNlcjpwYXNz") == {"dXNlcjpwYXNz"}
    assert parts("plain-value") == set() and parts("Bearer") == set() and parts("not json {") == set()
    oauth = {"access_token": "AT-0123456789", "token_type": "Bearer", "expires_in": 3600, "scope": "read write all",
             "refresh_token": "RT-0123456789", "client": {"client_secret": "CS-0123456789", "client_id": "ID-0123456789"},
             "tokens": ["T1-0123456789", "T2-0123456789"]}
    assert parts(json.dumps(oauth)) == {"AT-0123456789", "RT-0123456789", "CS-0123456789", "T1-0123456789",
                                        "T2-0123456789"}  # not the scheme, the scope or the client's id
    assert parts('{"token": "Bearer zyxwvutsrqpo"}') == {"Bearer zyxwvutsrqpo", "zyxwvutsrqpo"}
    state = {"tokens": {"access_token": "ya29.access-0123456789", "token_type": "Bearer", "expires_in": 3600,
                        "refresh_token": "1//refresh-0123456789", "scope": "mail"},
             "expires_at": 1_800_000_000.5, "client": {"client_id": "orion-app-1", "client_secret": "client-secret-0123456789",
                                                       "redirect_uris": ["http://localhost:8765/oauth/callback"]}}
    assert parts(json.dumps(state)) == {"ya29.access-0123456789", "1//refresh-0123456789", "client-secret-0123456789"}  # (the
    # shape ``orion.oauth.VaultStorage`` keeps a connector's sign-in in: ``connector.<id>.oauth``)


def test_a_process_knows_the_vault_and_its_environment_and_notices_changes(tmp_path) -> None:
    vault = Vault("files", tmp_path / "v")
    vault.set("connector.ha.authorization", "Bearer ha-long-lived-access-token")
    vault.set("connector.notion.oauth", json.dumps({"access_token": "notion-access-token-1"}))
    known = Known(vault, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-container", "LANG": "C"})
    assert set(known.values()) == {
        "Bearer ha-long-lived-access-token", "ha-long-lived-access-token", "notion-access-token-1",
        json.dumps({"access_token": "notion-access-token-1"}), "sk-ant-oat01-container"}
    before = known.revision()
    vault.set("connector.ha.authorization", "Bearer rotated-access-token-xyz")
    assert known.revision() != before and "rotated-access-token-xyz" in known.values()
    assert Known(None, {"DISCORD_TOKEN": "discord-bot-token-0123"}).values() == ["discord-bot-token-0123"]
    assert Known(None, {}).revision() is None


@pytest.fixture
def untouched_process():
    """protect() installs a redactor and the log guard for the process: put them back."""
    factory = logging.getLogRecordFactory()
    with redact.installed(redact.active()):
        yield
    logging.setLogRecordFactory(factory)


def test_protecting_a_process_hides_its_secrets_and_follows_the_vault(tmp_path, untouched_process) -> None:
    safety = default_config().safety
    vault = Vault("files", tmp_path / "v")
    vault.set("openrouter.api_key", "sk-or-v1-" + "a1" * 32)
    redactor = protect(safety, vault, {"CLAUDE_CODE_OAUTH_TOKEN": "container-oauth-token-0123456789"})
    assert redact.active() is redactor
    assert redact.redacted("k=" + "sk-or-v1-" + "a1" * 32 + " t=container-oauth-token-0123456789") == "k=[secret] t=[secret]"
    vault.set("calendar.home.password", "an app password")
    assert redact.stale() and redact.refresh() and redact.redacted("pw: an app password.") == "pw: [secret]."
    assert not redact.stale()
    vault.delete("openrouter.api_key")
    assert redact.refresh() and "sk-or-v1-" + "a1" * 32 not in redact.redacted("sk-or-v1-" + "a1" * 32).replace("[secret]", "")
