"""tools/policy: what the AI may change, switch, re-point or remember, and which processes close_app ends."""

import pytest

from orion.config import default_config
from orion.tools import policy
from orion.tools.policy import ProcRow, Refusal

HA = {"url": "https://ha.local/mcp", "headers": {"Authorization": "Bearer ${HASS_TOKEN}"}}  # a server holding a token
UVX = {"command": "uvx", "args": ["mcp-server-time"]}  # a server run by a command
NEVER = ["prompts.persona", " Prompts.Persona", "ollama.host", "ollama.keep_alive", "openrouter.api_key", "server.port",
         "voice.input_device", "data_dir", "mcp_servers", "version", "tools.always", "retrieval.recall_min_cosine"]


@pytest.mark.parametrize("key", NEVER)
def test_never_ai_holds_even_if_the_allow_list_slips(key: str) -> None:
    """These keys are refused to the AI whatever the allow-list says; the user is never refused here."""
    assert isinstance(policy.settings_refusal([key], "ai", lambda _: True), Refusal)
    assert policy.settings_refusal([key], "hud", lambda _: False) is None


def test_the_allow_list_decides_everyday_keys() -> None:
    """Keys on the allow-list pass; the first key off it is named in the refusal."""
    allowed = {"user.name", "ollama.model"}.__contains__
    assert policy.settings_refusal(["user.name", "ollama.model"], "ai", allowed) is None
    assert policy.settings_refusal(["user.name", "tts.speed"], "ai", allowed) == Refusal(
        "Only the user can change tts.speed (in Settings or data/config.yaml).")


@pytest.mark.parametrize(("action", "builtin", "old", "new", "run_on", "refused"), [
    ("enable", False, {**UVX, "enabled": False}, None, True, True),  # switching on is the user's alone
    ("enable", False, UVX, None, True, False),  # already on: a harmless no-op
    ("disable", True, None, None, False, False),  # switching off is always allowed
    ("restart", True, None, None, False, False),
    ("remove", False, HA, None, False, False),
    ("add", True, None, {"description": "x"}, True, True),  # his own groups only switch off or restart
    ("update", True, {}, {"description": "x"}, True, True),
    ("remove", True, {}, None, True, True),
    ("add", False, None, UVX, False, True),  # a server run by a command needs run_command on
    ("add", False, None, UVX, True, False),
    ("update", False, UVX, {**UVX, "args": ["other"]}, False, True),
    ("update", False, HA, {**HA, "url": "https://evil.example/mcp"}, True, True),  # tokens stay where they point
    ("update", False, HA, {**HA, "description": "lights"}, True, False),
    ("update", False, HA, {**HA, "headers": {"Authorization": "Bearer abc"}}, True, True),  # tokens are the user's
    ("add", False, None, {"url": "https://x.example/mcp?key=${TOKEN}"}, True, True),  # nothing from the environment
    ("update", False, {**UVX, "enabled": False}, UVX, True, True),  # dropping "enabled: false" switches it on
    ("update", False, {**UVX, "disabled_tools": ["a"]}, {**UVX, "disabled_tools": ["a", "b"]}, True, False),
    ("update", False, {**UVX, "disabled_tools": ["a"]}, UVX, True, True),  # a tool switched back on
    ("update", False, UVX, {**UVX, "intents": True}, True, True),  # letting a server push intents is the user's
    ("add", False, None, {"url": "https://x.example/mcp", "intents": True}, True, True),
    ("update", False, {**UVX, "intents": True}, UVX, True, False),  # ...stopping it is anyone's
    ("update", False, UVX, {**UVX, "lend": True}, True, True),  # lending its tools to other apps is the user's too
    ("update", False, {**UVX, "lend": True}, UVX, True, False),  # ...stopping it is anyone's
    ("update", False, {**UVX, "intents": True}, {**UVX, "intents": True, "gestures": {"victory": {"do": "tool"}}},
     True, True),  # what gestures do is the user's alone: one bound to a tool runs it unasked
    ("update", False, {**UVX, "gestures": {}}, {**UVX, "gestures": {}, "description": "x"}, True, False),
])
def test_server_changes_by_the_ai(action: str, builtin: bool, old: dict | None, new: dict | None, run_on: bool,
                                  refused: bool) -> None:
    """The AI's server changes follow the switch-off, command, token and environment rules; the user may do them all."""
    kw = {"action": action, "builtin": builtin, "old": old, "new": new, "run_command_on": run_on}
    assert (policy.server_change_refusal(actor="ai", **kw) is not None) is refused
    assert policy.server_change_refusal(actor="hud", **kw) is None


def test_switches_secrets_provenance_and_taint() -> None:
    """Tools switch off, never on; the in-process link never restarts; secrets are hidden; provenance and taint."""
    switch = [("ai", True, True, True), ("ai", True, False, False), ("ai", True, True, False),
              ("ai", False, True, False), ("hud", True, False, True)]
    assert [policy.tool_switch_refusal(enabled=e, actor=a, server_enabled=s, tool_disabled=d) is None
            for a, e, s, d in switch] == [False, False, True, True, True]
    for actor in ("ai", "hud"):
        assert policy.server_change_refusal(action="restart", actor=actor, builtin=True, old=None, new=None,
                                            run_command_on=True, name="orion")
    servers = {"gh": {"command": "x", "env": {"A": "${GH_TOKEN}", "B": "ghp_x"}}, "ha": HA}
    masked = policy.mask_secrets({"openrouter": {"api_key": "", "model": "m"}, "mcp_servers": servers},
                                 openrouter_key_set=True)
    assert masked["openrouter"] == {"api_key": "(hidden)", "model": "m"} and servers["gh"]["env"]["B"] == "ghp_x"
    assert masked["mcp_servers"]["gh"]["env"] == {"A": "${GH_TOKEN}", "B": "(hidden)"}
    assert masked["mcp_servers"]["ha"]["headers"] == {"Authorization": "(hidden)"}
    assert [policy.core_memory_allowed(p) for p in ("brain", "hud", "review", "hand", "tainted", "app")] == [
        True, True, True, True, False, False]
    assert [policy.taints(open_world=o, external=x, override=v) for o, x, v in [
        (True, False, None), (False, False, None), (None, True, None), (True, False, False), (False, True, False)]] == [
        True, False, True, False, True]
    claims = [("brain", True), ("brain", False), ("tainted", False), ("hud", False), (None, False), ("root", False)]
    assert [policy.trusted_provenance(v, lend=lend) for v, lend in claims] == ["app", "brain", "tainted", "hud", "app",
                                                                               "app"]


def test_close_app_spares_the_desktop_orion_and_others() -> None:
    """Short names are refused; protected processes, other users', Orion himself and his ancestors are never
    chosen."""
    rows = [ProcRow(1, "firefox", "firefox", "me"), ProcRow(2, "firefox-bin", "firefox-bin", "me"),
            ProcRow(3, "gnome-shell", "gnome-shell", "me"), ProcRow(4, "python3.12", "python3.12", "me"),
            ProcRow(5, "firefox", "firefox", "you"), ProcRow(6, "firefox", "firefox", "me"),
            ProcRow(7, "firefox", "firefox", "me"), ProcRow(8, "code", "code", "me"),
            ProcRow(9, "pipewire", "pipewire", "me"), ProcRow(10, "meld", "python3", "me")]
    kw = {"me": 6, "ancestors": frozenset({7}), "user": "me", "exec_names": frozenset()}
    assert policy.close_victims(rows, "", **kw) == policy.close_victims(rows, " x ", **kw) == Refusal(
        "Tell me which application to close.")
    assert [policy.close_victims(rows, n, **kw) for n in ("gnome-shell", "pipewire", "python", "Firefox")] == [
        [], [], [], [1, 2]]
    assert policy.close_victims(rows, "Visual Studio Code", **{**kw, "exec_names": frozenset({"code"})}) == [8]
    assert policy.close_victims(rows, "Some App", **{**kw, "exec_names": frozenset({"/usr/bin/python3"})}) == []


# ---------------------------------------------------------------------------------------------------- channel limits
READ, CHANGE, DESTROY = {"read_only": True}, {"read_only": False, "destructive": False}, {"read_only": False}
RULES = default_config().safety.channels


@pytest.mark.parametrize(("origin", "source", "channel"), [
    ("brain", "voice", "voice"), ("brain", "discord", "discord"), ("brain", "job", "job"), ("brain", "hud", "hud"),
    ("hud", "", "hud"), ("brain", "", "hud"), ("app", "", "app"), ("app", "discord", "app"), ("brain", "nonsense", "hud"),
])
def test_a_call_counts_as_the_channel_it_came_from(origin: str, source: str, channel: str) -> None:
    assert policy.channel_of(origin, source) == channel


def test_risk_is_what_the_tool_says_and_mcp_assumes_the_worst() -> None:
    assert [policy.risk_of(h) for h in (READ, CHANGE, DESTROY, {}, {"read_only": False, "destructive": True},
                                        {"read_only": True, "destructive": True})] == [
        "read", "change", "destroy", "destroy", "destroy", "read"]


@pytest.mark.parametrize(("selector", "name", "server", "hints", "named"), [
    ("power", "power", "system", DESTROY, True), ("power", "power_off", "system", DESTROY, False),
    ("system:*", "volume", "system", CHANGE, True), ("system:*", "volume", "systemd", CHANGE, False),
    ("home-assistant:*", "home_assistant__turn_on", "home-assistant", {}, True),
    ("@read", "x", "s", READ, True), ("@read", "x", "s", DESTROY, False), ("@change", "x", "s", CHANGE, True),
    ("@destroy", "x", "s", {}, True), ("@destroy", "x", "s", CHANGE, False),
    ("@open_world", "x", "s", {"read_only": True, "open_world": True}, True), ("@open_world", "x", "s", READ, False),
])
def test_what_a_channel_rule_can_name(selector: str, name: str, server: str, hints: dict, named: bool) -> None:
    assert policy.selects(selector, name=name, server=server, hints=hints) is named


def verdict(channel: str, name: str, server: str = "system", hints: dict | None = None,
            rules: dict | None = None) -> str:
    return policy.channel_verdict(rules or RULES, channel, name=name, server=server,
                                  hints=DESTROY if hints is None else hints)


def test_the_default_limits_of_each_channel() -> None:
    """Discord never gets the PC, commands, servers or settings; what destroys asks there and in jobs; power asks aloud."""
    for tool in ("power", "run_command", "mcp_servers", "settings_set", "orion_restart", "orion_shutdown",
                 "local_models", "brain_switch"):
        assert verdict("discord", tool) == "never", tool
    assert verdict("job", "power") == "never" and verdict("job", "run_command") == "confirm"
    assert verdict("discord", "write_file") == "confirm" and verdict("discord", "web_search", hints=READ) == ""
    assert verdict("discord", "set_timer", hints=CHANGE) == "" and verdict("job", "set_timer", hints=CHANGE) == ""
    assert verdict("voice", "power") == "confirm" and verdict("voice", "run_command") == ""
    assert verdict("hud", "power") == verdict("hud", "run_command") == verdict("app", "power") == ""
    assert verdict("unknown", "power") == ""  # not a channel: nothing says otherwise


def test_never_beats_confirm_and_a_class_or_connector_can_be_named() -> None:
    rules = {**RULES, "hud": {"never": ["@open_world", "browser:*"], "confirm": ["@destroy", "browser_click"]}}
    assert verdict("hud", "browser_click", "browser", DESTROY, rules) == "never"  # both say something: never wins
    assert verdict("hud", "read_page", "web", {"read_only": True, "open_world": True}, rules) == "never"
    assert verdict("hud", "wipe", "files", DESTROY, rules) == "confirm"
    assert verdict("hud", "list", "files", READ, rules) == ""


def test_the_channels_must_be_complete_and_well_formed() -> None:
    policy.check_channels(RULES)
    for bad in ({}, {**RULES, "x": {"never": [], "confirm": []}}, {**RULES, "hud": {"never": []}},
                {**RULES, "hud": {"never": ["a b"], "confirm": []}}, {**RULES, "hud": {"never": ["@nope"], "confirm": []}},
                {**RULES, "hud": {"never": [""], "confirm": []}}):
        with pytest.raises(ValueError):
            policy.check_channels(bad)


def test_what_widens_his_reach_is_found_by_its_argument() -> None:
    assert policy.widening("run_command", {"command": "ls", "outside_sandbox": True}) == "outside_sandbox"
    assert policy.widening("run_command", {"command": "ls", "outside_sandbox": False}) == ""
    assert policy.widening("run_command", {"command": "ls"}) == ""
    for action in ("add", "update", "remove", "list", "restart"):  # (the connectors tools ask about those themselves)
        assert policy.widening("mcp_servers", {"action": action}) == ""
    assert policy.widening("power", {"outside_sandbox": True}) == ""  # only the tools that have the argument
    assert policy.widening("run_command", {"outside_sandbox": {"a": 1}}) == policy.widening(
        "run_command", {"outside_sandbox": [True]}) == ""  # a list or a dict can't be one (and can't break the check)
    assert set(policy.UNATTENDED) == {"discord", "job"}


def test_why_a_call_asks_first() -> None:
    kw = {"origin": "brain", "tainted": False, "name": "x", "hints": DESTROY, "after_content": True, "always": (),
          "never": ()}
    assert policy.confirm_reason(**kw) == ""
    assert policy.confirm_reason(**kw, widens="outside_sandbox") == "outside_sandbox"
    assert policy.confirm_reason(**kw, widens="always", channel_asks=True) == "always"  # widening is named first
    assert policy.confirm_reason(**kw, channel_asks=True) == "channel"
    assert policy.confirm_reason(**{**kw, "never": ("x",), "tainted": True}, channel_asks=True) == "channel"  # not lifted
    assert policy.confirm_reason(**{**kw, "never": ("x",)}, widens="outside_sandbox") == "outside_sandbox"
    assert policy.confirm_reason(**{**kw, "tainted": True, "never": ("x",)}) == ""
    for origin in ("hud", "app", "user"):  # only the model's calls are asked about
        assert policy.confirm_reason(**{**kw, "origin": origin}, widens="always", channel_asks=True) == ""


def test_the_ai_cannot_change_a_servers_sandbox_or_hand_it_a_secret_of_the_vault() -> None:
    server = {"command": "uvx", "args": ["mcp-server-time"]}
    ai = {"actor": "ai", "builtin": False, "run_command_on": True}
    assert policy.server_change_refusal(action="update", old=server, new={**server, "sandbox": False}, **ai)
    assert policy.server_change_refusal(action="update", old={**server, "sandbox": True}, new=server, **ai)
    assert policy.server_change_refusal(action="add", old=None, new={**server, "sandbox": True}, **ai)
    assert policy.server_change_refusal(action="update", old={**server, "sandbox": True},
                                        new={**server, "sandbox": True, "description": "time"}, **ai) is None
    assert policy.server_change_refusal(action="update", old=server, new={**server, "sandbox": False}, actor="hud",
                                        builtin=False, run_command_on=True) is None
    for hand in ({"args": ["--key", "${secret:openrouter.api_key}"]}, {"url": "https://x.example/?k=${secret:a.b}"},
                 {"command": "${secret:a.b}"}):
        refusal = policy.server_change_refusal(action="add", old=None, new={"command": "uvx", **hand}, **ai)
        assert refusal is not None and "environment" in refusal.reason


@pytest.mark.parametrize("key", ["secrets.store", "secrets.folder", "activity.keep_days", "plugins.installed",
                                 "safety.channels", "safety.sandbox_commands", "safety.secret_patterns", "script.path", "bench.suite"])
def test_the_new_sections_are_never_the_ais(key: str) -> None:
    assert isinstance(policy.settings_refusal([key], "ai", lambda _: True), Refusal)
    assert policy.settings_refusal([key], "hud", lambda _: False) is None
