"""The ``orion`` command: start, stop and check him, download his models, keep his secrets, look into his memory, score
his brains (``orion --help``).

Everything else (settings, models, skills, MCP servers, his memory) is in the HUD. Every command imports what it
needs itself, so each starts quickly.
"""

import asyncio
import contextlib
import json
import logging
import os
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import click

if TYPE_CHECKING:
    import httpx

    from orion.backup import Restic
    from orion.config import Config
    from orion.memory import MemorySystem
    from orion.memory.index import Candidate, Span
    from orion.secrets import Vault
    from orion.service import Ollama

BANNER: Final = r"""
 ██████╗ ██████╗ ██╗ ██████╗ ███╗   ██╗
██╔═══██╗██╔══██╗██║██╔═══██╗████╗  ██║
██║   ██║██████╔╝██║██║   ██║██╔██╗ ██║
██║   ██║██╔══██╗██║██║   ██║██║╚██╗██║
╚██████╔╝██║  ██║██║╚██████╔╝██║ ╚████║
 ╚═════╝ ╚═╝  ╚═╝╚═╝ ╚═════╝ ╚═╝  ╚═══╝
   say my name
"""
START_TIMEOUT_S: Final = 120.0  # `systemctl start` returns once the HUD answers (a few seconds); this is the limit
STOP_TIMEOUT_S: Final = 60.0
BENCH_POLL_S: Final = 1.0  # `orion bench` looks at a running benchmark this often
BENCH_CALL_S: Final = 30.0  # ...and waits this long for one answer of Orion's API
OPEN_HUD: Final = click.option("--open", "open_hud", is_flag=True, help="Open the HUD in your browser as well.")


def _config() -> "Config":
    """The settings, read only (the CLI never writes them, except ``setup``)."""
    from orion.config import load_config

    return load_config()


def _quiet(cfg: "Config") -> None:
    """Log the chatty libraries at warnings only (``logging.quiet``)."""
    for name in cfg.logging.quiet:
        logging.getLogger(name).setLevel(logging.WARNING)


@click.group(invoke_without_command=True, context_settings={"help_option_names": ["-h", "--help"]})
@click.option("-v", "--verbose", is_flag=True, help="Log debug messages too (before the command: orion -v run).")
@click.pass_context
def main(ctx: click.Context, verbose: bool) -> None:
    """ORION, your personal assistant. Without a command: is he running?

    Everything else (settings, models, skills, MCP servers, his memory) is in the HUD."""
    logging.basicConfig(  # on stderr: for mcp-serve, stdout carries the MCP protocol
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname).1s %(name)s: %(message)s", datefmt="%H:%M:%S")
    if ctx.invoked_subcommand is None:
        ctx.invoke(status)


# ---------------------------------------------------------------------------------------------------- the service
@main.command()
@OPEN_HUD
def start(open_hud: bool) -> None:
    """Start him in the background."""
    from orion.config import ROOT
    from orion.server import hud_url
    from orion.service import ORION_UNIT, OllamaUnit, notify, refresh_units, systemctl, unit_active

    cfg = _config()
    url = hud_url(cfg.server)
    if unit_active(ORION_UNIT):
        print(f"Orion is already running. HUD: {url}")
    else:
        refresh_units(ROOT, OllamaUnit.from_config(cfg))
        print("Starting Orion...", flush=True)
        try:  # returns once the HUD answers: the service says so (Type=notify); the models load after
            result = systemctl("start", ORION_UNIT, timeout_s=START_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            sys.exit("Still starting; follow it with: journalctl --user -u orion -f")
        if result.returncode:
            sys.exit(f"Orion failed to start: {result.stderr.strip()}\nSee: journalctl --user -u orion -e "
                     "(not installed yet? run ./install.sh)")
        print(f"Online (his models are loading). HUD: {url}")
        notify("ORION", "Online. Say “Orion”.")
    if open_hud:
        subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@main.command()
def stop() -> None:
    """Stop him, freeing the GPU and the microphone."""
    from orion.service import ORION_UNIT, notify, systemctl, unit_active

    if not unit_active(ORION_UNIT):
        print("Orion is not running.")
        return
    result = systemctl("stop", ORION_UNIT, timeout_s=STOP_TIMEOUT_S)  # Ollama stops with him (PartOf)
    if result.returncode:
        sys.exit(f"Orion didn't stop: {result.stderr.strip()}")
    print("Orion stopped. GPU and microphone released.")
    notify("ORION", "Offline.")


@main.command()
@OPEN_HUD
@click.pass_context
def restart(ctx: click.Context, open_hud: bool) -> None:
    """Stop and start again."""
    ctx.invoke(stop)
    ctx.invoke(start, open_hud=open_hud)


@main.command()
def status() -> None:
    """Is he running?"""
    from orion.server import hud_url
    from orion.service import ORION_UNIT, systemctl, unit_active

    cfg = _config()
    if not unit_active(ORION_UNIT):
        print("Orion is offline. Start him with: orion start")
        return
    stage = systemctl("show", "--property=StatusText", "--value", ORION_UNIT).stdout.strip()
    print(f"Orion is online{f' ({stage})' if stage else ''}. HUD: {hud_url(cfg.server)}")


@main.command()
@click.option("--no-voice", is_flag=True, help="Don't open the microphone.")
def run(no_voice: bool) -> None:
    """Run in this terminal instead (Ctrl+C stops him)."""
    from orion.server import serve
    from orion.service import ORION_UNIT, UNDER_SERVICE, unit_active

    if not UNDER_SERVICE and unit_active(ORION_UNIT):
        sys.exit("Orion is already running in the background. Stop him first with: orion stop")
    action = None
    with contextlib.suppress(KeyboardInterrupt):
        action = asyncio.run(serve(microphone=not no_voice, banner=BANNER))
    if action == "restart" and not UNDER_SERVICE:  # under systemd, the service restarts him
        print("\nRestarting...", flush=True)
        os.execv(sys.executable, [sys.executable, "-m", "orion", *sys.argv[1:]])
    print(f"\nGoodbye, {_config().user.honorific}.")


# ---------------------------------------------------------------------------------------------------- setup and check
@main.command()
@click.option("--with-llm", is_flag=True, help="Also download the local brain's model (ollama.model).")
def setup(with_llm: bool) -> None:
    """Write data/config.yaml and download the models he needs."""
    from orion.config import ROOT, Settings
    from orion.inference import ensure, registry, whisper_path
    from orion.service import OllamaUnit, refresh_units
    from orion.voice.stt import resolve

    settings = Settings.open(role="main")
    if settings.sync_file():
        print(f"Settings: {settings.path} written (edit them in the HUD's Settings).")
    cfg = settings.cfg
    _quiet(cfg)
    print("Models:")
    files = [item for group in registry(cfg).values() for item in group]
    for item in files:
        if item.present:
            print(f"  ✓ {item.dest.name}")
    ensure(files, _download_progress)
    model, device, _ = resolve(cfg.stt)
    for name in dict.fromkeys(filter(None, (model, cfg.wake.candidate_model))):
        print(f"  Whisper {name} ({'GPU' if device == 'cuda' and name == model else 'CPU'})...", flush=True)
        whisper_path(cfg.models_dir, name)
    if installed := refresh_units(ROOT, OllamaUnit.from_config(cfg), install=True):
        print(f"Service and app menu: {', '.join(installed)} written.")
    if with_llm or cfg.brain.provider == "ollama":
        asyncio.run(_pull(cfg))
    print("\nAll models ready.")


def _download_progress(name: str, done: int, total: int) -> None:
    """One download's progress on one console line."""
    if total:
        print(f"\r  ↓ {name}: {done * 100 // total:3d}% of {total / 1e6:.0f} MB", end="", flush=True)
        if done >= total:
            print()


@contextlib.asynccontextmanager
async def _ollama(cfg: "Config") -> AsyncIterator["Ollama"]:
    """Ollama for one command: started for it, and stopped again after if it wasn't running.

    Raises:
        OllamaError: it could not be started.
    """
    from orion.service import Ollama

    async with contextlib.aclosing(Ollama(cfg.ollama.host, timeout_s=cfg.ollama.api_timeout_s)) as ollama:
        started = await ollama.start(cfg.ollama.start_timeout_s)
        try:
            yield ollama
        finally:
            if started:
                await ollama.stop()


async def _pull(cfg: "Config") -> None:
    """The local brain's model, through Ollama."""
    from orion.service import OllamaError

    model = cfg.ollama.model

    def report(_kind: str, /, **event: Any) -> None:
        """Ollama's download progress on one console line."""
        total, done = int(event.get("total") or 0), int(event.get("completed") or 0)
        if event.get("status") == "error":
            print(f"\n  ✗ {event.get('error')}")
        elif total:
            print(f"\r  ↓ {model}: {done * 100 // total:3d}% of {total / 1e9:.1f} GB", end="", flush=True)

    try:
        async with _ollama(cfg) as ollama:
            print(f"LLM: {model}")
            if await ollama.pull(model, report):
                print(f"\n  ✓ {model}")
    except OllamaError as e:
        print(f"LLM {model}: {e}")


@main.command()
def check() -> None:
    """Diagnose the installation: the brain, the models, the GPU, the audio devices and his tools."""
    import shutil

    from orion.config import Settings
    from orion.inference import Embedder, registry, whisper_ready
    from orion.tools import builtin_groups
    from orion.voice.stt import cuda_available, resolve

    settings = Settings.open(role="tools")
    cfg = settings.cfg
    _quiet(cfg)
    good = True

    def report(ok: bool, what: str, fix: str = "") -> None:
        """One line: a tick or a cross, and how to fix it."""
        nonlocal good
        good &= ok
        print(f"  {'✓' if ok else '✗'} {what}" + (f"  → {fix}" if fix and not ok else ""))

    print("Orion diagnostics")
    report(settings.broken is None, f"settings ({settings.path})",
           settings.broken.message if settings.broken is not None else "")
    for problem in settings.problems:
        print(f"    note: {problem.message}")
    asyncio.run(_check_brain(cfg, report))
    report(bool(Embedder.from_config(cfg).id),
           f"embedding model {cfg.models.embedding_repo} (memory search by meaning)", "orion setup")
    for group, files in registry(cfg).items():
        if group != "embeddings":
            for item in files:
                report(item.present, f"{group} model {item.dest.name}", "orion setup")
    model, device, _ = resolve(cfg.stt)
    report(whisper_ready(cfg.models_dir, model),
           f"speech recognition: Whisper {model} on the {'GPU' if device == 'cuda' else 'CPU'}", "orion setup")
    if cfg.wake.candidate_model:
        report(whisper_ready(cfg.models_dir, cfg.wake.candidate_model),
               f"name spotting: Whisper {cfg.wake.candidate_model}", "orion setup")
    if device == "cpu" and shutil.which("nvidia-smi"):
        report(cuda_available(), "GPU speech recognition", "uv sync (installs the CUDA libraries)")
    _check_audio(report)
    _check_security(cfg, report)
    _check_voice_processing(cfg, report)
    for name, group in builtin_groups().items():
        report(bool(group.tools), f"tools: {group.title} ({name}, {len(group.tools)} tools)")
    _check_backups(cfg, report)
    _check_documents(cfg, report)
    _check_machine(builtin_groups())
    print("\nAll good: start him with `orion start`." if good else "\nFix the ✗ items above.")


def _check_backups(cfg: "Config", report: Any) -> None:
    """Whether backups work: the folder can be read, the newest one's age, and restic when it is set up."""
    import time

    from orion import backup
    from orion.host import profile

    folder = backup.backup_folder(cfg)
    try:
        found = backup.archives(folder)
    except backup.BackupError as e:
        report(False, f"backups: {e}", "choose another backup folder in Settings")
    else:
        if found:
            report(True, f"backups: {len(found)} in {folder}, the newest {(time.time() - found[0].at) / 86400:.0f} "
                         "day(s) old")
        else:
            schedule = cfg.backup.schedule
            report(bool(schedule), "backups: none yet" + (f" (the schedule, {schedule}, makes one)" if schedule else ""),
                   "orion backup, or give backup.schedule in Settings")
    if cfg.backup.restic_repository:
        report(profile().has("restic"), "restic, for the encrypted copy of the backups off this machine",
               "sudo apt install restic")


def _check_documents(cfg: "Config", report: Any) -> None:
    """Whether each folder of documents can be read (nothing is said when none is chosen)."""
    from orion import documents

    for folder in documents.check_folders(cfg.documents.folders, documents.secret_places(cfg)):
        report(not folder.error, f"documents: {folder.shown}" + (f" ({folder.error})" if folder.error else ""),
               "choose another folder in Settings, under Customize, Documents")


def _check_machine(groups: dict[str, Any]) -> None:
    """What this machine has, and the tools it can't run (not errors: a server has no volume to turn down)."""
    from orion.host import NEEDS, detect

    host = detect()
    where = "a container" if host.container else host.platform
    desktop = f"{host.session} ({host.desktop or 'unknown desktop'})" if host.session else "no desktop session"
    print(f"\nThis machine: {where}, {desktop}; sound: {host.sound or 'no sound server'}; clipboard: "
          f"{host.clipboard or 'none'}; screen: {host.screen or 'none'}; battery: {'yes' if host.battery else 'no'}; "
          f"NVIDIA GPU: {'yes' if host.nvidia else 'no'}")
    missing = {}
    for group in groups.values():
        for name, (_, options) in group.tools.items():
            needs = options["meta"]["orion"].get("requires") or []
            if lacks := host.missing(needs):
                missing[name] = lacks
    for name, lacks in sorted(missing.items()):
        print(f"  · {name} isn't offered here: it needs {lacks}")
    if not missing:
        print(f"  · every tool can run here ({len(NEEDS)} kinds of need checked)")
    if host.session and not host.clipboard:
        package = "wl-clipboard" if host.session == "wayland" else "xclip"
        print(f"  · this machine's clipboard needs {package} (e.g. sudo apt install {package}); a HUD's works anyway")


async def _check_brain(cfg: "Config", report: Any) -> None:
    """Whether the brain the settings name can answer (nothing is asked of it)."""
    provider = cfg.brain.provider
    if provider == "claude":
        from orion.llm.claude import claude_status

        status = await claude_status(fresh=True)
        fix = ("sign in: run `claude`" if status.installed else
               "install Claude Code: curl -fsSL https://claude.ai/install.sh | bash")
        report(status.logged_in, f"Claude Code signed in ({status.plan or 'no plan'}, {cfg.claude.model})", fix)
        return
    if provider == "openrouter":
        import httpx

        from orion.base import LLMError
        from orion.llm import APP_HEADERS, openrouter_key

        openrouter = cfg.openrouter
        try:
            async with httpx.AsyncClient(headers=APP_HEADERS) as client:
                key = await openrouter_key(client, f"{openrouter.base_url.rstrip('/')}/key", openrouter.key)
            left = f", ${key.limit_remaining:.2f} left of its limit" if key.limit_remaining is not None else ""
            budget = f", daily budget ${openrouter.daily_budget_usd:.2f}" if openrouter.daily_budget_usd else ""
            report(True, f"OpenRouter key ({openrouter.model}{left}{budget})")
        except LLMError as e:
            report(False, f"OpenRouter key ({openrouter.model}): {e}", "add a valid key in Settings, Brain")
        return
    if provider == "script":
        from orion.llm.script import parse_script

        path = cfg.script_path
        try:
            rules = len(parse_script(path.read_text(encoding="utf-8")).rules)
            report(True, f"script {path.name} ({rules} rules): he answers from it, with no model")
        except (OSError, ValueError) as e:
            report(False, f"script {path}: {getattr(e, 'strerror', None) or e}", "fix script.path in data/config.yaml")
        return
    from orion.service import OllamaError, tagged

    try:
        async with _ollama(cfg) as ollama:
            installed, why = await ollama.models()
    except OllamaError as e:
        report(False, f"Ollama at {cfg.ollama.host}: {e}", "./install.sh --local-brain")
        return
    report(not why, f"Ollama at {cfg.ollama.host}", why)
    report(tagged(cfg.ollama.model) in {model["name"] for model in installed}, f"model {cfg.ollama.model}",
           "orion setup --with-llm")


def _check_audio(report: Any) -> None:
    """Whether there are a microphone and speakers."""
    try:
        import sounddevice as sd
    except (ImportError, OSError) as e:  # no PortAudio library
        report(False, f"audio devices ({e})", "install PortAudio (libportaudio2)")
        return
    try:
        mic, speakers = sd.query_devices(kind="input"), sd.query_devices(kind="output")
    except (sd.PortAudioError, ValueError) as e:  # no sound server, or no default device
        report(False, f"audio devices ({e})", "check PipeWire or PulseAudio")
        return
    report(True, f"microphone: {mic['name']}")
    report(True, f"speakers: {speakers['name']}")


def _check_security(cfg: "Config", report: Any) -> None:
    """Whether his vault opens and the settings hold no secret, how his commands are kept in bounds, and what each channel
    may never do."""
    from orion import landlock, sandbox, secrets, vaulting
    from orion.host import profile

    try:
        vault = secrets.open_vault(cfg)
        count = len(vault.names())
    except secrets.VaultError as e:
        report(False, f"secrets vault ({cfg.secrets.store})", str(e))
    else:
        report(True, f"secrets vault ({cfg.secrets.store}): {count} secret{'s' * (count != 1)} (`orion secret list`)")
        if left := vaulting.plaintext(cfg):
            report(False, f"{len(left)} secret{'s' * (len(left) != 1)} in the settings as plain text",
                   "start Orion: he moves them into the vault")
    safety = cfg.safety
    if profile().container:
        report(True, "commands run in this container, which is their sandbox")
    elif not safety.sandbox_commands:
        report(False, "commands run as you do: the sandbox is off", "turn on Settings, Safety, Sandbox commands")
    else:
        found = sandbox.status(fresh=True)
        report(found.works, f"command sandbox (bubblewrap, Landlock {found.landlock or 'none'})", found.why)
        if found.works and safety.sandbox_network and not found.network:
            report(False, "commands can't have the network here: its sandbox needs Landlock "
                          f"{landlock.NEEDS_SCOPES} (Linux 6.12), this kernel has {found.landlock}",
                   "update the kernel, or turn off Settings, Safety, Network for commands")
    for channel, limits in safety.channels.items():
        if limits.get("never"):
            print(f"  · {channel} may never use: {', '.join(limits['never'])}")


# ---------------------------------------------------------------------------------------------------- secrets
@main.group()
def secret() -> None:
    """Keep the secrets he uses (an API key, a calendar password) in his vault; only names are ever shown."""


def _open_vault(cfg: "Config") -> "Vault":
    """His vault, or the reason for stopping."""
    from orion import secrets

    try:
        return secrets.open_vault(cfg)
    except secrets.VaultError as e:
        raise click.ClickException(str(e)) from None


def _secret_value(name: str) -> str:
    """The value of a secret: asked for with nothing shown at a terminal, otherwise read from standard input (never an
    argument, which the process list and the shell history would show)."""
    if sys.stdin.isatty():
        value = click.prompt(f"Value of {name}", hide_input=True, confirmation_prompt=True)
    else:
        value = sys.stdin.read().removesuffix("\n").removesuffix("\r")
    if not value:
        raise click.ClickException("The value is empty.")
    return value


def _note_secret(cfg: "Config", title: str) -> None:
    """Record that a secret was set or deleted (its name only) in the activity log, so the HUD's Activity page shows it."""
    import sqlite3

    from orion import redact
    from orion.activity import Activity
    from orion.base import OrionError
    from orion.memory.store import Store

    try:
        store = Store(cfg.data_path / "orion.db", cfg.memory, role="serve")
        try:
            store.ensure_schema()
            Activity(store, lambda *args, **kwargs: None, redact.redacted).record(
                "secret", source="user", title=title, outcome="ok", actor="you, in the terminal").result()
        finally:
            store.close()
    except (OrionError, sqlite3.Error) as e:
        print(f"note: not written to the activity log: {e}", file=sys.stderr)


@secret.command("list")
def secret_list() -> None:
    """List the secrets by name, whether each is set and what uses it (never a value)."""
    from orion import vaulting

    cfg = _config()
    items = vaulting.listing(cfg, _open_vault(cfg))
    for item in items:
        used = f"  used by {', '.join(item['used_by'])}" if item["used_by"] else ""
        print(f"{item['name']}  {'set' if item['set'] else 'NOT SET'}{used}")
    if not items:
        print("No secrets yet.")


@secret.command("set")
@click.argument("name")
def secret_set(name: str) -> None:
    """Set the secret NAME (e.g. openrouter.api_key, connector.discord.DISCORD_TOKEN).

    The value is asked for with nothing shown, or read from standard input (echo "$TOKEN" | orion secret set NAME)."""
    from orion import secrets

    cfg = _config()
    vault = _open_vault(cfg)
    try:
        vault.set(name, _secret_value(name))
    except secrets.VaultError as e:
        raise click.ClickException(str(e)) from None
    _note_secret(cfg, f"Set the secret {name}")
    print(f"Set {name}.")


@secret.command("delete")
@click.argument("name")
def secret_delete(name: str) -> None:
    """Delete the secret NAME (a setting that still refers to it will then say it isn't set)."""
    from orion import secrets

    cfg = _config()
    try:
        gone = _open_vault(cfg).delete(name)
    except secrets.VaultError as e:
        raise click.ClickException(str(e)) from None
    if not gone:
        raise click.ClickException(f"The secret {name} isn't set.")
    _note_secret(cfg, f"Deleted the secret {name}")
    print(f"Deleted {name}.")


# ---------------------------------------------------------------------------------------------------- benchmarks
def _bench_call(orion: "httpx.Client", method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """One call to the running Orion's API for ``orion bench``; Orion's reason is the command's if it refuses."""
    response = orion.request(method, path, json=body)
    if response.is_success:
        return dict(response.json())
    try:
        detail = response.json()["detail"]
    except (ValueError, KeyError, TypeError):
        detail = response.text or f"Orion answered {response.status_code}."
    sys.exit(str(detail))


@main.command()
@click.option("--brain", "brains", multiple=True, metavar="BRAIN",
              help="A brain to score: claude, ollama, openrouter or script, with a model after a colon if you like "
                   "(ollama:qwen3.5:9b); repeat it for several. Default: the brain he thinks with.")
@click.option("--suite", type=click.Choice(["builtin", "history"]),
              help="builtin: his own requests over his tools; history: your latest requests (default: bench.suite).")
@click.option("--limit", type=click.IntRange(min=1), help="The most requests asked of each brain.")
@click.option("--yes", is_flag=True, help="Don't ask first when the run makes paid requests.")
def bench(brains: tuple[str, ...], suite: str | None, limit: int | None, yes: bool) -> None:
    """Score brains on picking his tools: the same requests asked of each, in a dry run (no tool really runs).

    Each brain is scored on calling the right tools with the right arguments, how fast it answers and what it costs.
    He tells how many requests a run makes, and where they are paid (your Claude plan, OpenRouter), before it starts.
    It runs inside the running Orion (his tools, skills and memory), so he must be started."""
    import httpx

    from orion.bench import results_text

    cfg = _config()
    _quiet(cfg)
    port = cfg.server.port
    body: dict[str, Any] = {"brains": list(brains), "suite": suite, "limit": limit}
    # verify=False: his own certificate, on this PC, has nothing to be checked against
    client = httpx.Client(base_url=f"{'https' if cfg.server.tls_cert else 'http'}://127.0.0.1:{port}",
                          headers={"Host": f"localhost:{port}"}, verify=False, timeout=BENCH_CALL_S)
    with client as orion:
        try:
            plan = _bench_call(orion, "POST", "/api/bench/plan", body)
            print(plan["text"])
            costs = any(brain["paid"] and brain["ready"] for brain in plan["brains"])
            if costs and not yes:
                click.confirm("Start it?", abort=True)
            run = _bench_call(orion, "POST", "/api/bench", {**body, "confirm": costs})
            state = _bench_wait(orion, run["id"])
        except httpx.ConnectError:
            sys.exit("Orion isn't running: a benchmark uses his tools, skills and memory. Start him with: orion start")
    print(f"\n{results_text(state)}")
    sys.exit(0 if state["status"] == "done" else 1)


def _bench_wait(orion: "httpx.Client", run_id: int) -> dict[str, Any]:
    """Follow a benchmark to its end, its progress on one line; Ctrl+C stops it (what it scored so far is kept)."""
    stopping = False
    while True:
        try:
            state = _bench_call(orion, "GET", f"/api/bench/{run_id}")
            print(f"\r  {state['progress']:4.0%} of the requests asked", end="", flush=True)
            if state["status"] != "running":
                return state
            time.sleep(BENCH_POLL_S)
        except KeyboardInterrupt:
            if stopping:
                raise
            stopping = True
            print("\n  stopping...", flush=True)
            orion.post(f"/api/bench/{run_id}/stop")


def _check_voice_processing(cfg: "Config", report: Any) -> None:
    """Whether echo cancellation can run here (it needs LiveKit's native library), and instant commands' sentences."""
    if cfg.voice.echo_cancellation:
        from orion.voice.aec import unavailable

        why = unavailable()
        report(not why, "echo cancellation (WebRTC's canceller)", f"{why}; or switch it off in Settings, Voice")
    if cfg.instant.enabled:
        from orion.instant import Matcher

        try:
            matcher = Matcher.english(cfg.instant.intents)
        except Exception as e:  # noqa: BLE001 (whatever is wrong with the package of sentences is the answer)
            report(False, f"instant commands ({e})", "uv sync")
        else:
            report(True, f"instant commands: {len(matcher.intents.intents)} kinds of request done without the model")


# ---------------------------------------------------------------------------------------------------- lending
@main.command("mcp-serve")
@click.argument("groups", nargs=-1)
@click.option("--hub", is_flag=True, hidden=True, help="Serve Orion's own hub (every tool; the hub filters).")
def mcp_serve(groups: tuple[str, ...], hub: bool) -> None:
    """Lend his tools to other AI apps over stdio: all groups, or those named (e.g. system web)."""
    from orion.tools import serve

    serve(groups=groups, lend=not hub)


@main.command("discord-serve")
def discord_serve() -> None:
    """Be Orion's Discord bot, as an MCP server over stdio (Orion starts it: Customize, Connectors, Discord)."""
    import asyncio
    import logging

    from orion.discord_bridge import serve

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")  # stderr
    asyncio.run(serve())


# ---------------------------------------------------------------------------------------------------- memory
@main.group()
def memory() -> None:
    """Look into his memory (why a memory is recalled or not, thresholds from labelled cases), or forget it all."""


@memory.command()
@click.option("--yes", is_flag=True, help="Don't ask first.")
def reset(yes: bool) -> None:
    """Forget everything he knows: every conversation, the memory files, the index and the logs. Settings, timers,
    reminders, tasks, skills and models stay. While he runs, he restarts to do it."""
    import httpx

    from orion.memory import wipe
    from orion.service import ORION_UNIT, unit_active

    cfg = _config()
    if not yes:
        click.confirm("Forget everything he knows? It can't be undone", abort=True)
    port = cfg.server.port
    try:  # verify=False: his own certificate, on this PC, has nothing to be checked against
        status = httpx.post(f"{'https' if cfg.server.tls_cert else 'http'}://127.0.0.1:{port}/api/memory/forget",
                            headers={"Host": f"localhost:{port}"}, verify=False, timeout=10).status_code
    except httpx.ConnectError:
        status = 0  # not running: forget it here
    except httpx.HTTPError as e:
        sys.exit(f"Orion didn't answer ({e}); stop him first (orion stop), then run this again.")
    if status == 200:
        print("Forgetting everything: he restarts with a blank memory.")
        return
    if status or unit_active(ORION_UNIT):
        sys.exit(f"Orion refused ({status or 'no answer'}); stop him first (orion stop), then run this "
                 "again.")
    for what in wipe(cfg):
        print(f"  ✓ {what}")
    print("Forgot everything. He starts with a blank memory.")


def _open_memory(cfg: "Config") -> "MemorySystem":
    """His memory, opened to read like another app (the index synced first when Orion isn't running)."""
    from orion.inference import Embedder
    from orion.memory import MemorySystem

    embedder = Embedder.from_config(cfg)
    embedder.load()
    system = MemorySystem.open(cfg, embedder, role="serve")
    system.refresh()
    return system


def _candidates(system: "MemorySystem", query: str, now: datetime) -> tuple["Span | None", list["Candidate"],
                                                                             dict[int, str]]:
    """What recall weighs for a request made at `now`: the time it asks about, its candidates and their texts."""
    from orion.base import Query
    from orion.memory.index import parse_span

    span = parse_span(query, now)
    found = system.index.candidates(Query(query, system.index.embedder.encode_query(query)), conv=0, span=span)
    return span, found, system.index.texts([c.id for c in found])


@memory.command()
@click.argument("query")
def explain(query: str) -> None:
    """What recall weighs for QUERY: every candidate, its ranks and cosine, and the gate it passes (or none)."""
    from orion.base import local_now
    from orion.memory.index import gate

    cfg = _config()
    _quiet(cfg)
    system = _open_memory(cfg)
    try:
        span, found, texts = _candidates(system, query, local_now())
        r = cfg.retrieval
        print(f"{len(found)} candidates" + (f"; time asked about: {span}" if span else "") + ":")
        for c in sorted(found, key=lambda c: (c.bm25_rank if c.bm25_rank is not None else 999,
                                              c.cos_rank if c.cos_rank is not None else 999)):
            passed = gate(c, span_hard=span is not None and span.hard, window=r.recall_rank_window,
                          min_cosine=r.recall_min_cosine, solo_cosine=r.recall_solo_cosine)
            cos = f"{c.cos:.2f}" if c.cos is not None else "-"
            text = " ".join(texts.get(c.id, "").split())[:90]
            print(f"  {passed or '-':8} {c.kind:10} kw#{c.bm25_rank if c.bm25_rank is not None else '-':<3} "
                  f"cos {cos} #{c.cos_rank if c.cos_rank is not None else '-':<3}"
                  f"{' concept' if c.concept else ''}{' in-span' if c.in_span else ''}  {text}")
    finally:
        system.close()


@memory.command()
@click.argument("cases", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--router", is_flag=True, help="Calibrate tool and skill routing instead of recall.")
def calibrate(cases: Path, router: bool) -> None:
    """Suggest thresholds from labelled cases (JSON lines).

    Recall: {"query": "...", "expect": ["words", "each recalled memory holds"], "now": "2026-10-03T21:00"};
    an empty expect means nothing should be recalled. Routing (--router): {"query": "...", "expect": "tool_or_skill"}.
    """
    from orion.base import local_now, local_zone

    cfg = _config()
    _quiet(cfg)
    rows = [json.loads(line) for line in cases.read_text(encoding="utf-8").splitlines() if line.strip()]
    if router:
        _calibrate_router(cfg, [(str(row["query"]), str(row["expect"])) for row in rows])
        return
    from orion.memory.index import EvalCase, calibrate_recall

    system = _open_memory(cfg)
    try:
        def when(row: dict[str, Any]) -> datetime:
            """The case's time, local."""
            if not row.get("now"):
                return local_now()
            moment = datetime.fromisoformat(str(row["now"]))
            return moment if moment.tzinfo else moment.replace(tzinfo=local_zone())

        labelled = [EvalCase(str(row["query"]), when(row), tuple(map(str, row.get("expect") or ()))) for row in rows]

        def candidates(case: EvalCase) -> list[tuple[Any, str, bool]]:
            """A case's candidates with their texts."""
            span, found, texts = _candidates(system, case.query, case.now)
            return [(c, texts.get(c.id, ""), span is not None and span.hard) for c in found]

        best = calibrate_recall(labelled, candidates)
    finally:
        system.close()
    print(f"retrieval.recall_min_cosine: {best.min_cosine:.2f}\nretrieval.recall_rank_window: {best.rank_window}\n"
          f"retrieval.recall_solo_cosine: {best.solo_cosine:.2f}\n"
          f"(precision {best.precision:.0%}, recall {best.recall:.0%}, false injections {best.false_injection:.0%})")


def _calibrate_router(cfg: "Config", cases: list[tuple[str, str]]) -> None:
    """Score tool and skill routing on labelled requests and print the thresholds it suggests."""
    from orion.base import Tool, ToolSet
    from orion.brain.context import Router
    from orion.inference import Embedder
    from orion.mcp import summary_of
    from orion.skills import Skills
    from orion.tools import builtin_groups

    embedder = Embedder.from_config(cfg)
    if not embedder.load():
        sys.exit("No embedding model: run `orion setup` first.")
    tools: dict[str, Tool] = {}
    for group in builtin_groups().values():
        for name, (_, options) in group.tools.items():
            examples = tuple(options["meta"]["orion"]["examples"])
            tools[name] = Tool(name, options["description"], {}, group.name, name, title=options["title"],
                               summary=summary_of(options["description"]), examples=examples, builtin=True)
    skills = Skills(cfg.skills_path)
    routing = Router(embedder, cfg)
    routing.index_tools(ToolSet(tools, 1))
    routing.index_skills(skills.all(), skills.version)
    result = routing.calibrate(cases)
    print(f"tools.find_min_score: {result.suggested_find_min:.2f}\n"
          f"skills.auto_score: {result.suggested_auto_score:.2f}\n"
          f"skills.auto_margin: {result.suggested_auto_margin:.2f}\n({len(result.right)} of {len(cases)} cases scored)")


# ---------------------------------------------------------------------------------------------------- backups
def _restic_password(cfg: "Config") -> str | None:
    """The restic repository's password: the secret ``backup.restic.password`` of the vault."""
    return _open_vault(cfg).get("backup.restic.password")


def _restic(cfg: "Config") -> "Restic":
    """The restic repository of the settings.

    Raises:
        BackupError: none is set, restic is missing, or there is no password.
    """
    from orion.backup import open_restic

    return open_restic(cfg, _restic_password(cfg))


@main.command("backup")
@click.option("--list", "listing", is_flag=True, help="List the backups instead of making one.")
def backup_command(listing: bool) -> None:
    """Back up his memory, conversations, settings, skills and plugins now: an archive in the backup folder, and a
    copy in the restic repository when one is set up. Not in a backup: the vault of secrets, the models, the logs."""
    import time

    from orion import backup
    from orion.config import Settings

    settings = Settings.open(role="tools")
    cfg = settings.cfg
    _quiet(cfg)
    folder = backup.backup_folder(cfg)
    try:
        if listing:
            items = backup.archives(folder)
            if cfg.backup.restic_repository:
                try:
                    items = sorted([*items, *_restic(cfg).snapshots()], key=lambda item: item.at, reverse=True)
                except backup.BackupError as e:
                    print(f"restic: {e}")
            for item in items:
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(item.at))
                print(f"{item.name}  {when}  {item.size / 1e6:6.1f} MB  {'restic' if item.where == 'restic' else folder}")
            if not items:
                print(f"No backups in {folder}.")
            return
        restic, problem = None, ""
        if cfg.backup.restic_repository:
            try:
                restic = _restic(cfg)
            except backup.BackupError as e:
                problem = str(e)
        made = backup.create(cfg, settings.path, name=backup.archive_name(time.time()), restic=restic)
    except backup.BackupError as e:
        sys.exit(f"Backup failed: {e}")
    print(f"Backed up: {made.name} ({made.size / 1e6:.1f} MB, {made.files} files, {made.seconds:.1f} s) in {folder}")
    note = problem or made.restic
    if note:
        print(f"restic: {'a copy is in the repository too' if note == 'ok' else note}")
        if note != "ok":
            sys.exit(1)


@main.command("restore")
@click.argument("source")
@click.option("--check", is_flag=True, help="Only check that the backup is whole; restore nothing.")
@click.option("--yes", is_flag=True, help="Don't ask first.")
def restore_command(source: str, check: bool, yes: bool) -> None:
    """Put a backup back while he is stopped. SOURCE is an archive file, "latest" (the newest in the backup folder),
    or "restic" / "restic:ID" for a snapshot of the restic repository.

    The backup is checked as it is unpacked; his current memory and settings are kept in data/before-restore-<time>/
    and put back if anything fails. The vault of secrets is not in a backup: on a new machine enter your keys again
    (orion secret set)."""
    import time

    from orion import backup
    from orion.config import Settings

    settings = Settings.open(role="tools")
    cfg = settings.cfg
    _quiet(cfg)
    kind, _, snapshot = source.partition(":")
    try:
        if kind == "restic":
            if check:
                sys.exit("--check works on an archive; restic keeps its own snapshots whole (restic check).")
            snapshot = snapshot or "latest"
            what = f"the restic snapshot {snapshot}"
        else:
            archive = backup.latest(cfg) if source == "latest" else Path(source).expanduser()
            manifest = backup.verify(archive)
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(manifest.created))
            what = f"{archive.name} (made {when}, by Orion {manifest.orion}, {len(manifest.files)} files)"
            if check:
                print(f"{what} is whole." + (f" Left out: {', '.join(manifest.skipped)}." if manifest.skipped else ""))
                return
        if not yes:
            click.confirm(f"Replace his memory and settings with {what}? Today's are kept in "
                          f"{cfg.data_path}/before-restore-<time>", abort=True)
        if kind == "restic":
            result = backup.restore_from_restic(cfg, settings.path, _restic(cfg), snapshot)
        else:
            result = backup.restore(cfg, settings.path, archive)
    except backup.BackupError as e:
        sys.exit(f"Restore failed: {e}")
    print(f"Restored {len(result.manifest.files)} files." + (f" What they replaced is in {result.kept}." if result.kept
                                                           else ""))
    for warning in result.warnings:
        print(f"  note: {warning}")
    print("The vault of secrets isn't in a backup: if this is a new machine, enter your keys again (orion secret "
          "set). Then start him with: orion start")


if __name__ == "__main__":
    main()
