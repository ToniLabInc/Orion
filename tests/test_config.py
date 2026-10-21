"""config: one template holds every value; code holds the policy; loading is lenient, saving atomic and faithful."""

import asyncio
import json
from pathlib import Path

import pytest
import yaml

from orion import config as c
from orion.tools.policy import NEVER_AI, settings_refusal

AI_WRITABLE = {
    "user.name", "user.honorific", "user.city", "brain.provider", "brain.fallback", "brain.think", "brain.temperature",
    "brain.max_tool_rounds", "brain.history_chars", "claude.model", "ollama.model", "ollama.num_ctx",
    "openrouter.model", "voice.name_wake", "voice.barge_in", "voice.wake_threshold", "voice.follow_up_seconds",
    "voice.end_silence_ms", "voice.max_utterance_s", "voice.chime", "tts.voice", "tts.speed", "stt.model",
    "stt.device", "memory.review", "memory.review_after_idle_s", "memory.recall", "memory.user_chars",
    "memory.self_chars", "tools.max_per_turn", "hud.quick_timers", "hud.sounds", "jobs.provider", "watchers.battery_low",
    "watchers.battery_critical", "watchers.cpu_hot", "watchers.gpu_hot", "watchers.disk_full", "watchers.memory_full",
    "watchers.speak", "watchers.discord"}


def test_the_template_is_the_one_source_and_agrees_with_the_policy() -> None:
    """Every key is in the template with an explanation; [restart] marks exactly the restart keys; the AI may write
    the 30 everyday keys and nothing NEVER_AI forbids; the HUD schema carries ranges, steps, units and choices."""
    template = c.template_values()
    flat = {f"{s}.{o}" for s, v in template.items() if isinstance(v, dict) and s != "mcp_servers" for o in v}
    every_key = c.keys()
    assert flat | {"data_dir", "mcp_servers"} == set(every_key) and c.Config.model_validate(template, strict=True)
    helps = c.template_help()
    for key in every_key:
        assert helps[key] and ("[restart]" in helps[key]) == (c.Apply.RESTART in c.meta(key).apply), key
    assert c.ai_writable() == AI_WRITABLE
    assert all(settings_refusal([key], "ai", c.ai_writable().__contains__) is None for key in AI_WRITABLE)
    assert not any(key.startswith(tuple(NEVER_AI)) or key in NEVER_AI for key in AI_WRITABLE)
    schema = c.hud_schema()
    assert len(schema) == 67 and all(f["kind"] != "float" or "step" in f for f in schema.values())
    assert {"voice.echo_cancellation", "voice.noise_suppression", "instant.enabled"} <= schema.keys()
    assert not {"voice.echo_cancellation", "voice.noise_suppression", "instant.enabled"} & c.ai_writable()
    assert "server.host" in schema and "server.host" not in c.ai_writable()  # opening the HUD is the user's alone
    assert {k: schema["voice.end_silence_ms"][k] for k in ("kind", "min", "max", "step", "unit")} == {
        "kind": "int", "min": 300, "max": 2000, "step": 50, "unit": "ms"}
    assert schema["server.port"]["restart"] and schema["voice.input_device"]["kind"] == "device"
    assert [x["value"] for x in schema["claude.model"]["choices"]] == ["haiku", "sonnet", "opus"]
    assert [x["value"] for x in schema["ollama.num_ctx"]["choices"]] == [4096, 8192, 16384, 32768]


def test_instant_commands_are_checked() -> None:
    """The intents must be ones Orion does at once; every reply is there and uses only its own placeholders."""
    cfg = c.default_config()
    replies = dict(cfg.instant.replies)

    def refused(patch: dict[str, object]) -> str:
        """Why Orion refuses this change to the instant section (a change of his own code: no key rights)."""
        with pytest.raises(c.Refused) as error:
            c.validate_patch(cfg, patch, actor="hud", own=True)
        return str(error.value)

    assert "HassVacuumStart" in refused({"instant.intents": ["HassVacuumStart"]})
    assert "replies lacks time" in refused({"instant.replies": {k: v for k, v in replies.items() if k != "time"}})
    assert "no reply called nope" in refused({"instant.replies": {**replies, "nope": "x"}})
    assert "{clock}" in refused({"instant.replies": {**replies, "time": "It is {clock}."}})
    assert "replies.timer_started" in refused(
        {"instant.replies": {**replies, "timer_started": "Your {minutes} is running."}})
    ok = c.validate_patch(cfg, {"instant.replies": {**replies, "time": "{Honorific}, it's {time}."}}, actor="hud",
                          own=True)
    assert ok["instant.replies"]["time"] == "{Honorific}, it's {time}."


def test_prompt_placeholders() -> None:
    """A prompt may use only its own names, plainly; required names stay; a placeholder-free prompt is free text."""
    spec = c.meta("prompts.memory_review").prompt
    assert spec is not None
    for bad in ("{nope} {transcript}", "{0} {transcript}", "{} {transcript}", "{transcript.__class__}",
                "{transcript!r}", "a { b {transcript}", "no transcript at all"):
        with pytest.raises(ValueError):
            c.check_placeholders(bad, spec)
    c.check_placeholders("{curly} braces are fine here", c.Prompt())
    names = {"who": "", "who_pos": "", "honorific": "", "time": "", "tz": "", "city": "", "name": "", "channel": "",
             "user": "", "orion": "", "facts": "", "fill": "", "transcript": "", "max_concepts": 6, "memory": "",
             "error": "", "Honorific": "", "timer": "", "text": "", "primary": "", "fallback": "", "reason": "", "action": "", "source": "", "when": "", "prompt": "", "value": ""}
    for key, text in c.default_config().prompts.model_dump().items():
        if c.meta(f"prompts.{key}").prompt.allowed:  # type: ignore[union-attr]
            text.format(**names)


def test_lenient_loading_keeps_every_good_value_and_the_users_text(tmp_path: Path) -> None:
    """Unknown options are reported, numbers out of range are clamped, a bad value takes the factory one while its
    raw text is written back, and a file with a syntax error is never overwritten."""
    path = tmp_path / "config.yaml"
    path.write_text("version: 2\nuser: {name: Tony}\nvoice: {foo: 1, chime: 1}\nserver: {port: 80}\n"
                    "prompts: {persona: 'Hi {nmae}'}\n")
    s = c.Settings.open(path)
    assert (s.cfg.user.name, s.cfg.server.port, s.cfg.voice.chime) == ("Tony", 1024, True)
    assert {(p.kind, p.key) for p in s.problems} >= {("unknown", "voice.foo"), ("invalid", "server.port"),
                                                       ("invalid", "voice.chime"), ("invalid", "prompts.persona")}
    assert s.cfg.prompts.persona == c.default_config().prompts.persona and s.sync_file()
    written = yaml.load(path.read_text(), Loader=yaml.CSafeLoader)
    assert written["prompts"]["persona"] == "Hi {nmae}"  # the raw text is written back until the user fixes it
    assert written["voice"]["chime"] == 1 and "foo" not in written["voice"]
    path.write_text("user:\n\tname: Tony\n")
    broken = c.Settings.open(path)
    assert broken.broken and not broken.writable and not broken.sync_file()
    assert path.read_text() == "user:\n\tname: Tony\n"  # left exactly as it was
    assert broken.cfg == c.default_config()
    with pytest.raises(ValueError, match="problem to fix"):
        asyncio.run(broken.commit({"voice.chime": False}))


def test_untouched_options_follow_improved_defaults(tmp_path: Path) -> None:
    """An option still at the factory value it was written from takes the improved one; one the user changed stays;
    the file before is kept as config.yaml.bak."""
    path = tmp_path / "config.yaml"
    old = "tts: {speed: 1.2}\ntools: {max_per_turn: 15}\n"  # no `version`: this layout
    path.write_text(old)
    base = c.factory_fingerprints() | {"tts.speed": c.fingerprint(1.2)}  # 1.2 was the factory speed then
    (tmp_path / c.BASE_NAME).write_text(json.dumps({"version": c.VERSION, "keys": base}))
    s = c.Settings.open(path)
    assert (s.cfg.tts.speed, s.cfg.tools.max_per_turn) == (c.default_config().tts.speed, 15)
    assert [(p.kind, p.key) for p in s.problems] == [("updated_default", "tts.speed")] and s.sync_file()
    assert (tmp_path / "config.yaml.bak").read_text() == old and (path.stat().st_mode & 0o777) == 0o600
    assert c.read_baseline(path) == c.factory_fingerprints() and c.Settings.open(path).problems == []


def test_render_round_trips_tricky_values() -> None:
    """Multi-line prompts stay blocks; words YAML 1.1 misreads are quoted; servers are written; comments survive."""
    cfg = c.default_config()
    cfg.prompts.persona = "Line one {who}.\nLine two {honorific}."
    cfg.phrases.stop = [*cfg.phrases.stop, "no"]
    cfg.user.honorific, cfg.user.city = "no", "12:30"
    cfg.mcp_servers = {"time": {"command": "uvx", "args": ["mcp-server-time"]}}
    text = c.render(cfg)
    back = yaml.load(text, Loader=yaml.CSafeLoader)
    assert back.pop("version") == c.VERSION and back == cfg.model_dump()
    assert "  persona: |-" in text and "# ── advanced ──" in text and "PART 3" in text


def test_patches_by_actor() -> None:
    """The HUD changes its pages' keys, the AI the allow-list; types are strict; choices and rules hold."""
    cfg = c.default_config()
    assert c.validate_patch(cfg, {"voice": {"end_silence_ms": 650}}, actor="hud") == {"voice.end_silence_ms": 650}
    assert c.validate_patch(cfg, {"tts.speed": 1.2}, actor="ai") == {"tts.speed": 1.2}
    assert c.validate_patch(cfg, {"tts.speed": cfg.tts.speed}, actor="ai") == {}
    with pytest.raises(ValueError, match=r"data/config\.yaml"):
        c.validate_patch(cfg, {"voice.vad_threshold": 0.6}, actor="hud")
    for key, value in [("prompts.persona", "x"), ("ollama.host", "http://evil:1"), ("openrouter.api_key", "k"),
                       ("server.port", 9000), ("voice.input_device", "mic"), ("data_dir", "/tmp"), ("mcp_servers", {})]:
        with pytest.raises(ValueError, match="Only the user can change"):
            c.validate_patch(cfg, {key: value}, actor="ai")
    for patch, why in (({"ollama.num_ctx": "8192"}, "integer"), ({"voice.chime": 1}, "boolean"),
                       ({"voice.nope": 1}, "no setting"), ({"voice.end_silence_ms": 400}, "at most")):
        with pytest.raises(ValueError, match=why):
            c.validate_patch(cfg, patch, actor="hud")
    with pytest.raises(ValueError, match="choose one of"):
        c.validate_patch(cfg, {"ollama.model": "huge:70b"}, actor="ai", choices={"ollama.model": ["qwen3.5:4b"]})
    with pytest.raises(ValueError, match=r"mcp_servers\.hands\.intents: input should be a valid boolean"):
        c.validate_patch(cfg, {"mcp_servers": {"hands": {"command": "uv", "intents": "yes"}}}, actor="hud")


def test_commit_reload_refresh_and_views(tmp_path: Path) -> None:
    """A commit keeps section identity and is not reported back as an outside edit; a hand edit is, even one back to
    Orion's own text; the tools process picks changes up with one stat; the HUD and the AI see only what they may."""
    path = tmp_path / "config.yaml"
    main, tools = c.Settings.open(path), None
    main.sync_file()
    tools = c.Settings.open(path, role="tools")
    held = main.cfg.voice

    async def run() -> tuple[list[str], c.Reload, c.Reload]:
        """Commit a change, reload (own write), then hand-edit and reload."""
        changed = await main.commit({"voice.end_silence_ms": 650})
        own = await main.reload()
        path.write_text(path.read_text().replace("vad_threshold: 0.5", "vad_threshold: 0.6"))
        return changed, own, await main.reload()

    changed, own, edit = asyncio.run(run())
    assert changed == ["voice.end_silence_ms"] and held.end_silence_ms == 650 and own == c.Reload([], [])
    assert edit.changed == ["voice.vad_threshold"] and edit.problems[-1].kind == "external"
    assert tools.refresh_if_changed() == ["voice.end_silence_ms", "voice.vad_threshold"]
    assert tools.refresh_if_changed() == []  # nothing changed since: one stat, no parsing
    path.write_text(path.read_text().replace("vad_threshold: 0.6", "vad_threshold: 0.5"))  # Orion's text again
    assert asyncio.run(main.reload()).changed == ["voice.vad_threshold"]
    view = c.public_view(main.cfg)
    assert "prompts" not in view and view["openrouter"]["api_key"] == ""
    ai = c.ai_view(main.cfg)
    assert ai["voice"]["name_wake"] and "vad_threshold" not in ai["voice"]
    assert "openrouter" in ai and "api_key" not in ai["openrouter"]


def test_the_openrouter_key_may_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty key, or a reference to a variable that is not set, falls back to $OPENROUTER_API_KEY."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-from-env")
    monkeypatch.setenv("MY_KEY", "sk-or-mine")
    openrouter = c.default_config().openrouter
    for written, used in (("", "sk-or-from-env"), ("${UNSET_KEY}", "sk-or-from-env"), ("${MY_KEY}", "sk-or-mine"),
                          ("sk-or-typed", "sk-or-typed")):
        assert openrouter.model_copy(update={"api_key": written}).key == used
    assert c.secret_sources(c.default_config()) == {"openrouter.api_key": "env"}


def test_the_safety_defaults_say_what_each_channel_may_never_do() -> None:
    """Discord never gets the PC, commands, servers, plugins, settings, the brain, a restart or the models, even with a
    yes; a job and another app never change his connectors."""
    safety = c.default_config().safety
    assert set(safety.channels) == {"voice", "hud", "discord", "job", "app"}
    assert set(safety.channels["discord"]["never"]) >= {"power", "run_command", "mcp_servers", "plugins", "settings_set",
                                                        "orion_restart", "orion_shutdown", "local_models"}
    assert safety.channels["discord"]["confirm"] == ["@destroy"] == safety.channels["job"]["confirm"]
    assert safety.channels["job"]["never"] == ["power", "mcp_servers", "plugins", "benchmark_brains"]
    assert safety.channels["voice"]["confirm"] == ["power"] and safety.channels["app"] == {
        "never": ["mcp_servers", "plugins"], "confirm": []}
    assert not any(safety.channels["hud"][kind] for kind in ("never", "confirm"))
    assert safety.sandbox_commands and safety.sandbox_network and "~/.ssh" in safety.sandbox_hidden
    assert {"~/.gnupg", "~/.mozilla", "~/.aws", "~/.claude"} <= set(safety.sandbox_hidden)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in safety.env_allow and "DBUS_SESSION_BUS_ADDRESS" not in safety.env_allow
    assert "DBUS_SESSION_BUS_ADDRESS" in safety.env_desktop and "LC_*" in safety.env_allow


CHANNELS = c.default_config().safety.channels


@pytest.mark.parametrize(("key", "value", "why"), [
    ("safety.channels", {"voice": {"never": [], "confirm": []}}, "hud is missing"),
    ("safety.channels", {**CHANNELS, "web": {"never": [], "confirm": []}}, "web isn't one"),
    ("safety.channels", {**CHANNELS, "hud": {"never": []}}, "never list and a confirm list"),
    ("safety.channels", {**CHANNELS, "hud": {"never": ["bad name!"], "confirm": []}}, "isn't a tool"),
    ("safety.channels", {**CHANNELS, "hud": {"never": ["@nothing"], "confirm": []}}, "isn't a tool"),
    ("safety.sandbox_hidden", ["relative/path"], "absolute path"),
    ("safety.sandbox_writable", [".ssh"], "absolute path"),
    ("safety.sandbox_writable", ["~"], "holds startup files"),
    ("safety.sandbox_writable", ["/"], "holds startup files"),
    ("safety.sandbox_writable", ["~/.config"], "holds startup files"),
    ("safety.sandbox_writable", ["~/.local/share"], "holds startup files"),
    ("safety.sandbox_writable", ["~/Documents", "~/.config/autostart/apps"], "holds startup files"),
    ("safety.env_allow", ["NOT A NAME"], "environment variable"),
    ("safety.env_desktop", ["LC_*_X"], "environment variable"),
    ("safety.secret_patterns", ["(?<=a)b"], "RE2 can run"),
    ("safety.secret_patterns", ["a*"], "match nothing"),
    ("safety.redact_min_chars", 2, "greater than or equal to 4"),
    ("secrets.store", "plaintext", "keyring"),
    ("activity.keep_days", 0, "greater than or equal to 1"),
])
def test_a_safety_option_that_cannot_work_is_refused(key: str, value: object, why: str) -> None:
    with pytest.raises(c.Refused, match=why):
        c.validate_patch(c.default_config(), {key: value}, actor="hud", own=True)


def test_a_folder_commands_may_write_in_holds_no_startup_file() -> None:
    cfg = c.default_config()
    for fine in (["~/Documents", "~/Downloads"], ["~/code", "/srv/work"], ["~/.local/share/Steam"], []):
        assert c.validate_patch(cfg, {"safety.sandbox_writable": fine}, actor="hud") == (
            {} if fine == cfg.safety.sandbox_writable else {"safety.sandbox_writable": fine})


def test_the_user_edits_safety_through_the_hud_but_the_ai_never_does() -> None:
    cfg = c.default_config()
    for key, value in (("safety.sandbox_network", False), ("safety.sandbox_hidden", ["~/.ssh", "/srv/keys"]),
                       ("safety.sandbox_writable", ["~/code"]), ("activity.keep_days", 30)):
        assert c.validate_patch(cfg, {key: value}, actor="hud") == {key: value}
        with pytest.raises(c.Refused):
            c.validate_patch(cfg, {key: value}, actor="ai")
    with pytest.raises(c.Refused, match="data/config.yaml"):  # the channels have an editor of their own
        c.validate_patch(cfg, {"safety.channels": cfg.safety.channels}, actor="hud")
    for key in ("safety.channels", "safety.secret_patterns", "secrets.store", "secrets.folder", "activity.keep_days",
                "plugins.installed", "safety.confirm_after_content", "safety.sandbox_commands"):
        assert settings_refusal([key], "ai", lambda _: True) is not None, key
    assert "safety" not in c.ai_view(cfg) or set(c.ai_view(cfg)["safety"]) == {"confirm_after_content"}


def test_the_vault_folder_is_inside_the_data_folder_unless_it_is_absolute(tmp_path: Path) -> None:
    cfg = c.default_config()
    assert cfg.secrets_path == cfg.data_path / "vault"
    elsewhere = cfg.model_copy(update={"secrets": cfg.secrets.model_copy(update={"folder": str(tmp_path / "keys")})})
    assert elsewhere.secrets_path == tmp_path / "keys"
