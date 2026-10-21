"""The running Orion, built once in the main process: every part, its startup, the settings it applies live, its
background loops, and the façade the HUD's server and Orion's own controls use (``server.py``, ``tools/core.py``).

The HUD is served first. ``build`` makes every part without loading a model or touching the network, then ``start``
loads in the background and reports its progress on the HUD's welcome screen (``boot`` events):
- Phase A runs these at once: the tools (the hub connects his own tools process and the added servers), then the
  brain (warmed up with every tool read); the voice (Kokoro); the memory index (the embedder, then a sync of the
  files and the log); and speech recognition, unless the brain runs on this PC, which gets the GPU first.
- Phase B loads speech recognition after a local brain and opens the microphone. The memory review, the reminders,
  the file watchers and the HUD's stats and weather start. A step that fails is shown and logged, and the rest
  carries on.

Settings change through ``apply_settings`` (the HUD and the AI) and through hand edits of config.yaml (the watcher).
Either way the change is validated, saved and assigned in place (every part holds the live sections). Then each
``Apply`` flag of the changed keys runs its action once, in a fixed order. A secret in a change goes to the vault and
config.yaml keeps a reference (``orion.vaulting``), also when it was typed into the file by hand.

The security parts live here too: the vault (``orion.secrets``) and the redactor that hides what it holds, the activity
log (the hub records every call; job runs and alerts reach it from the event bus; a day's old entries are forgotten
once a day), and what the HUD's Safety page does (the channels' limits, the secrets).

Little polls. The reminders sleep until the next one is due (or the database changes) and the watchers wait for the
files. The vital signs are read every hud.stats_interval_s (well under a millisecond) but sent only while a HUD is
open, and the weather is fetched only then. The live view of the hands (HANDS, and the Gestures page) is followed
only while a HUD shows it: its frames go to those HUDs alone (``hands_view``).
"""

import asyncio
import collections
import contextlib
import dataclasses
import functools
import json
import logging
import operator
import os
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Coroutine, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, cast

from orion import redact, secrets, vaulting
from orion.activity import Activity
from orion.base import JSON, Actor, CallContext, EventBus, LLMError, StepRecord, local_zone, log_conversations
from orion.config import ROOT, Apply, Refused, Settings, ai_writable, meta, normalize_patch, value_of
from orion.gestures import gesture_remotes
from orion.host import profile
from orion.llm import PROVIDERS, SCRIPT, build_llm, label, make_llm
from orion.llm.fallback import FallbackLLM, members
from orion.secrets import Vault, VaultError
from orion.service import (
    OLLAMA_UNIT,
    ORION_UNIT,
    UNDER_SERVICE,
    OllamaError,
    OllamaUnit,
    asystemctl,
    refresh_units,
    sd_notify,
    unit_active,
)
from orion.tools.core import ExitAction, enforce
from orion.tools.policy import CLASSES, risk_of, settings_refusal
from orion.voice import STT_MODELS

if TYPE_CHECKING:
    from orion.config import Config
    from orion.confirm import Question
    from orion.intents import Intent
    from orion.llm import LLM
    from orion.llm.fallback import Switch
    from orion.memory.index import SyncReport
    from orion.memory.store import JobRow, ReminderRow, TodoRow
    from orion.service import Stats
    from orion.surfaces import Surface
    from orion.tools.web import Weather
    from orion.watchers import Alert

log = logging.getLogger("orion")

type BootName = Literal["tools", "brain", "memory", "voice", "ears"]
type BootStatus = Literal["pending", "ok", "error", "skipped"]
type Outcome = tuple[BootStatus, str]  # how a startup step ended, and its detail for the HUD

BOOT_ORDER: Final[tuple[BootName, ...]] = ("tools", "brain", "memory", "voice", "ears")
OWNS_OLLAMA: Final = "ORION_OWNS_OLLAMA"  # set across a restart in a terminal: the Ollama it started is its own
CONTEXT_KEYS: Final = frozenset({"brain.snapshot_journal_chars", "skills.context_max"})  # changes that need a new epoch
SCHEDULER_MAX_SLEEP_S: Final = 60.0  # the scheduler looks at the wall clock again at least this often
CONNECTORS_PAUSE_S: Final = 0.15  # changes of the connectors that come together are told to the HUD once, this late
STALE_CHECK_S: Final = 3600.0  # the marketplaces and skill sources are looked at this often for being older than plugins.refresh_h
STATS_SLOW_EVERY: Final = 15  # the slow vital signs (disk, battery, temperature) once per this many stats ticks
STATS_TOP_EVERY: Final = 3  # the busiest programs once per this many stats ticks, while a HUD is open
STATS_HISTORY_S: Final = 80  # the vital signs kept, so an opening HUD draws its charts full at once
WEATHER_RETRY_S: Final = 300  # a failed weather fetch is tried again after this long
WATCH_DEBOUNCE_MS: Final = 400  # file changes this close together are handled together
DB_WAL: Final = "orion.db-wal"  # changes whenever anyone commits to the database (another process included)
HANDS_LIVE: Final = "orion://hands"  # a gesture remote's live view of the hands...
HANDS_CAMERA: Final = "orion://hands/camera"  # ...and the same with the camera's picture
HANDS_REFRESH_S: Final = 2.0  # it is read again this often without news: the remote keeps its view on while read


@dataclass(slots=True)
class BootStep:
    """One startup step as the HUD's welcome screen shows it.

    Attributes:
        name: "tools", "brain", "memory", "voice" or "ears".
        status: "pending", "ok", "error" or "skipped".
        detail: what it loaded, or why it failed.
    """

    name: BootName
    status: BootStatus = "pending"
    detail: str = ""


def announcement(item: "ReminderRow", now: float, cfg: "Config") -> str:
    """What he says when a timer or reminder falls due (``prompts.timer_done``, ``reminder_due``, ``reminder_late``).

    Args:
        item: the timer or reminder.
        now: the time, in epoch seconds.
        cfg: the settings (the prompts, ``user.honorific``, ``reminders.late_after_s``).

    Returns:
        The sentence.
    """
    from orion.tools.organizer import timer_name

    honorific = cfg.user.honorific
    title = honorific[:1].upper() + honorific[1:]
    prompts = cfg.prompts
    if item["kind"] == "timer":
        return prompts.timer_done.format(Honorific=title, timer=timer_name(item))
    if now - item["due"] > cfg.reminders.late_after_s:
        when = datetime.fromtimestamp(item["due"], local_zone()).strftime("%H:%M")
        return prompts.reminder_late.format(Honorific=title, time=when, text=item["text"])
    return prompts.reminder_due.format(Honorific=title, text=item["text"])


class Supervisor:
    """Owns the background tasks: each is kept referenced and its failure logged; loops restart after a pause."""

    def __init__(self) -> None:
        """Start with no tasks."""
        self._tasks: set[asyncio.Task[Any]] = set()

    def spawn[T](self, job: Coroutine[Any, Any, T], *, name: str) -> "asyncio.Task[T]":
        """Run a coroutine in the background.

        Args:
            job: the coroutine.
            name: the task's name, for the logs.

        Returns:
            The task.
        """
        task = asyncio.get_running_loop().create_task(job, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._done)
        return task

    def loop(self, body: Callable[[], Coroutine[Any, Any, None]], *, name: str,
             backoff_s: tuple[float, float] = (5.0, 60.0)) -> "asyncio.Task[None]":
        """Run a long-lived coroutine; when it fails, run it again after a pause that doubles up to a limit.

        Args:
            body: makes the coroutine (a loop that returns only when its work is over).
            name: the task's name, for the logs.
            backoff_s: the first pause and the longest one, in seconds.

        Returns:
            The task.
        """

        async def run() -> None:
            """Run the body until it returns; a failure is logged and retried."""
            pause = backoff_s[0]
            while True:
                started = time.monotonic()
                try:
                    await body()
                    return
                except Exception:
                    log.exception("%s failed; restarting it in %.0f s", name, pause)
                await asyncio.sleep(pause)
                ran_long = time.monotonic() - started > backoff_s[1]
                pause = backoff_s[0] if ran_long else min(pause * 2, backoff_s[1])

        return self.spawn(run(), name=name)

    async def aclose(self, timeout_s: float = 5.0) -> None:
        """Cancel every task and wait for them a little.

        Args:
            timeout_s: the longest wait.
        """
        tasks = [task for task in self._tasks if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=timeout_s)

    def _done(self, task: "asyncio.Task[Any]") -> None:
        """Forget a finished task, logging a failure."""
        self._tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            log.error("%s failed", task.get_name(), exc_info=error)


class Runtime:
    """The running Orion: what the HUD's server and his own controls talk to (see the module docstring).

    Attributes:
        settings: the settings and their file.
        cfg: the live settings every part holds.
        bus: the HUD's event bus.
        supervisor: the background tasks.
        ready: set once startup is over (whatever failed is shown).
        hud_open: set while a HUD is connected (only then are its stats and weather worked out).
        boot: the startup steps.
        brain_error: why the brain is not ready ("" when it is).
        exit_action: "stop" or "restart" once asked; None while running.
        on_exit: stops the HUD's server (set by ``server.serve``).
    """

    def __init__(self, settings: Settings, *, microphone: bool, vault: Vault | None = None, vault_error: str = "") -> None:
        """Hold the settings; nothing is built yet (``build``).

        Args:
            settings: the settings (role "main").
            microphone: whether to open the microphone at all (``orion run --no-voice`` says no).
            vault: his vault, opened (and the settings' secrets moved into it) before the file was written; None when it
                couldn't be.
            vault_error: why it couldn't ("" when it was).
        """
        self.settings = settings
        self.cfg = settings.cfg
        self._opened = vault
        self.vault_error = vault_error
        log_conversations(lambda: self.cfg.memory.log_conversations)  # what log lines may show of a conversation
        self.microphone = microphone
        self.bus = EventBus()
        self.supervisor = Supervisor()
        self.ready = asyncio.Event()
        self.hud_open = asyncio.Event()
        self.boot: dict[BootName, BootStep] = {name: BootStep(name) for name in BOOT_ORDER}
        self.brain_error = ""
        self.exit_action: ExitAction | None = None
        self.forget_on_stop = False  # forget everything once everything has stopped (then he restarts)
        self.on_exit: Callable[[], None] = lambda: None
        self.last_stats: Stats | None = None
        # 80 s at the fastest interval (hud.stats_interval_s is at least 0.5 s)
        self.stats_history: collections.deque[Stats] = collections.deque(maxlen=STATS_HISTORY_S * 2)
        self.last_weather: Weather | None = None
        self.last_news: list[JSON] | None = None  # the top headlines the NEWS panel shows (get_news, no topic)
        self._clients = 0
        self._hands_viewers: dict[int, bool] = {}  # HUD connections showing the hands' live view: with the picture?
        self._hands_watch: dict[str, tuple[str, Callable[[], None]]] = {}  # remote -> (resource followed, its stop)
        self._hands_sent: dict[str, tuple[object, ...]] = {}  # remote -> what its last frame sent to the HUD said
        self._owns_ollama = os.environ.pop(OWNS_OLLAMA, "") == "1"
        self._reminders_changed = asyncio.Event()
        self._city_changed = asyncio.Event()
        self._stopping = asyncio.Event()
        self._unmute: asyncio.TimerHandle | None = None
        self._tools_version = -1
        self._released = 0.0
        self._data_version = -1
        self._reminders: list[ReminderRow] | None = None
        self._todos: list[TodoRow] | None = None
        self._started = time.monotonic()
        self._connectors_due: asyncio.TimerHandle | None = None
        self._connectors_sent: list[JSON] | None = None

    # ------------------------------------------------------------------------------------------------ build
    def build(self) -> None:
        """Make every part, on the event loop: no model loads, no network (a fraction of a second)."""
        from orion.backup import Backups
        from orion.bench import Bench
        from orion.brain import Brain, TurnRunner
        from orion.brain.context import Context
        from orion.confirm import Confirmer
        from orion.connectors import Connectors
        from orion.documents import Documents
        from orion.inference import Embedder
        from orion.instant import Instant
        from orion.intents import IntentRunner
        from orion.jobs import JobRunner, Jobs
        from orion.mcp import MCPHub, OpTrace
        from orion.memory import MemorySystem
        from orion.memory.review import Reviewer
        from orion.oauth import Broker
        from orion.plugins import Library
        from orion.service import Ollama, Vitals
        from orion.skills import Skills
        from orion.spend import Spend
        from orion.surfaces import Surfaces
        from orion.tools import GROUPS
        from orion.tools.automation import JOBS, WATCHERS
        from orion.tools.core import CORE, Controls
        from orion.tools.core import server as core_server
        from orion.tools.device import CLIPBOARD, EYES
        from orion.tools.documents import DOCUMENTS
        from orion.tools.web import DiskCache, WeatherService
        from orion.voice.session import VoiceSession
        from orion.watchers import Watchers

        cfg = self.cfg
        self._loop = asyncio.get_running_loop()
        self.bus.bind(self._loop)
        self.redactor = secrets.protect(cfg.safety, self._opened, os.environ,  # first: every part below logs
                                        also=lambda: vaulting.plaintext(self.cfg) if self._opened is None else ())
        self.embedder: Embedder = Embedder.from_config(cfg)
        self.memory: MemorySystem = MemorySystem.open(cfg, self.embedder, role="main")
        self.memory.on_indexed = self._indexed
        self.activity: Activity = Activity(self.memory.store, self.bus.publish, redact.redacted)
        self.bus.subscribe(self._activity_event)
        self.skills: Skills = Skills.open(cfg)
        self.spend: Spend = Spend(cfg, self.memory.store, self.bus.publish)
        self.ops: OpTrace = OpTrace(self.bus.publish, cfg.hud, recent=cfg.mcp.recent_calls, on_record=self._record)
        self.controls: Controls = Controls(self)
        self.surfaces: Surfaces = Surfaces(self.bus)
        self.confirmer: Confirmer = Confirmer(cfg, self.bus)
        self.hub: MCPHub = MCPHub(
            cfg, GROUPS, ops=self.ops, spawn=(sys.executable, "-m", "orion", "mcp-serve", "--hub"),
            spawn_env={"ORION_CONFIG": str(self.settings.path)}, core=core_server(self.controls),
            core_groups={CORE.name: CORE, EYES.name: EYES, CLIPBOARD.name: CLIPBOARD, JOBS.name: JOBS,
                         WATCHERS.name: WATCHERS, DOCUMENTS.name: DOCUMENTS},
            on_change=self._tools_changed,
            on_event=self._tool_event,
            embed=lambda text: self.brain.embed(text), on_intent=self._intent, confirmer=self.confirmer,
            activity=self.activity, config_file=self.settings.path)
        self.library: Library = Library(self)  # plugins, marketplaces and skill sources
        self.broker: Broker = Broker(self)  # signing in to remote connectors
        self.connectors: Connectors = Connectors(self)  # every source of tools as Customize lists it
        self.hub.hooks = self.connectors
        self.ollama: Ollama = Ollama(cfg.ollama.host, timeout_s=cfg.ollama.api_timeout_s)
        self.context: Context = Context(cfg, self.memory, self.skills, self.embedder)
        self.brain: Brain = Brain(cfg, self.memory, self.skills, self.embedder, self.context, self.hub,
                                  self._make_llm(), self.supervisor.spawn)
        self.voice: VoiceSession = VoiceSession(cfg, self.bus, cfg.models_dir, microphone=self.microphone,
                                                levels_wanted=self.hud_open.is_set, on_idle=self._on_idle)
        self.reviewer: Reviewer = Reviewer(cfg, self.memory, self.bus, lambda: self.brain.llm, lambda: self.busy,
                                           on_reviewed=self.brain.rewarm)
        self.instant: Instant = Instant(cfg, self.hub, self.memory.store, weather=self._forecast,
                                        home_city=self.memory.home_city, activity=self.activity,
                                        spawn=self.supervisor.spawn)
        self.turns: TurnRunner = TurnRunner(cfg, self.bus, self.memory, self.brain, self.hub, self.voice,
                                            self.reviewer, self.supervisor.spawn, instant=self.instant)
        self.turns.hud_open = self.hud_open.is_set
        self.voice.attach(self.turns)
        self.surfaces.on_gone.append(self.voice.release_voice)
        self.confirmer.askers["voice"] = lambda q, timeout_s: self.voice.ask_aloud(q.text, timeout_s)
        self.confirmer.askers["discord"] = self.confirmer.askers["job"] = self._ask_on_discord
        self.intents: IntentRunner = IntentRunner(self)
        self.jobs: Jobs = Jobs(cfg, self.memory.store, self.bus)
        self.jobs.changed = lambda: self._loop.call_soon_threadsafe(self._reminders_changed.set)  # any thread
        self.watchers: Watchers = Watchers(cfg, on_alert=self._alert,
                                           on_change=lambda items: self.bus.publish(
                                               "alerts", items=[a.as_dict() for a in items]))
        self.job_runner: JobRunner = JobRunner(
            cfg, self.jobs, self.brain, self.bus,
            make_llm=lambda provider: build_llm(self.cfg, provider, meter=self.spend, purpose="job"),
            announce=lambda text, title, spoken: self.turns.announce(text, title, spoken=spoken),
            send=self.send_on_discord)
        self.weather: WeatherService = WeatherService(DiskCache(cfg.data_path / "cache.db"), lambda: self.cfg)
        self.vitals: Vitals = Vitals(cfg.system.disk_path, 0)
        self.bench: Bench = Bench(self, redact=redact.redacted)  # (what a benchmark's brains read and wrote may hold an old secret)
        self.backups: Backups = Backups(cfg, self.settings.path, publish=self.bus.publish,
                                        record=self.activity.record, password=self._restic_password)
        self.documents: Documents = Documents(cfg, self.embedder, publish=self.bus.publish,
                                              record=self.activity.record)

    # ------------------------------------------------------------------------------------------------ start
    async def start(self) -> None:
        """Load everything in the background (see the module docstring); ``ready`` is set at the end."""
        self._publish_boot()
        if self.settings.problems or self.settings.broken is not None:
            self._publish_problems()
        local = self.cfg.brain.provider == "ollama"
        self.supervisor.spawn(self.instant.load(), name="instant commands")
        await asyncio.gather(self._tools_then_brain(), self._step("voice", self._load_voice),
                             self._step("memory", self._load_memory),
                             *(() if local else (self._step("ears", self._load_ears),)))
        if local:
            await self._step("ears", self._load_ears)
        self._listen()
        self.reviewer.start()
        for body, name in ((self._scheduler, "reminders"), (self._watch_memory, "memory watcher"),
                           (self._watch_data, "settings watcher"), (self._stats_loop, "stats"),
                           (self._weather_loop, "weather"), (self._prune_activity, "activity"),
                           (self._library_loop, "marketplaces"), (self.documents.run, "documents"),
                           (self.documents.watch, "documents watcher")):
            self.supervisor.loop(body, name=name)
        if self.vault_error:
            self.bus.publish("toast", title="VAULT", text=self.vault_error)
        self.activity.record("system", source="system", title="Orion started", outcome="ok")
        self.ready.set()
        self._publish_boot()
        self._publish_capabilities()
        sd_notify("STATUS=ready")
        log.info("ready in %.1f s: %s", time.monotonic() - self._started,
                 "; ".join(f"{s.name} {s.status}" + (f" ({s.detail})" if s.detail else "") for s in self.boot.values()))

    async def _tools_then_brain(self) -> None:
        """Connect the tools, then warm the brain up with all of them read (the first question is then quick)."""
        await self._step("tools", self._load_tools)
        await self._step("brain", self._load_brain)

    async def _step(self, name: BootName, work: Callable[[], Awaitable[Outcome]]) -> bool:
        """Run one startup step, showing its progress; a failure is shown and logged, never raised.

        Args:
            name: the step.
            work: does it, returning (status, detail).

        Returns:
            Whether it succeeded (or was skipped).
        """
        try:
            status, detail = await work()
        except (LLMError, OllamaError, OSError) as e:
            log.warning("%s: %s", name, e)
            self._set_boot(name, "error", str(e) or type(e).__name__)
            return False
        except Exception as e:
            log.exception("%s failed to start", name)
            self._set_boot(name, "error", f"{type(e).__name__}: {e}")
            return False
        self._set_boot(name, status, detail)
        return True

    async def _load_tools(self) -> Outcome:
        """Connect his own tools process, his controls and the added servers."""
        await self.hub.configure(wait_s=self.cfg.mcp.startup_wait_s)
        self._tools_changed()
        failing = [s["name"] for s in self.hub.status() if s["status"] in ("error", "reconnecting")]
        detail = f"{len(self.hub.tools())} tools" + (f"; not connected: {', '.join(failing)}" if failing else "")
        return "ok", detail

    async def _load_brain(self) -> Outcome:
        """Start Ollama when the brain runs on this PC, then warm the brain up."""
        try:
            if self.cfg.brain.provider == "ollama":
                await self._start_ollama()
            await self.brain.warm_up()
        except Exception as e:
            self.brain_error = str(e) or type(e).__name__
            raise
        self.brain_error = ""
        return "ok", label(self.cfg)

    async def _load_voice(self) -> Outcome:
        """Load Kokoro and open the speaker.

        Raises:
            FileNotFoundError: the voice's files are missing (``orion setup`` fetches them).
        """
        models = self.cfg.models
        if missing := [name for name in (models.kokoro_model, models.kokoro_voices)
                       if not (self.cfg.models_dir / name).is_file()]:
            raise FileNotFoundError(f"the voice's files are missing ({', '.join(missing)}): run orion setup")
        await self.voice.load_tts()
        return "ok", self.cfg.tts.voice

    async def _load_memory(self) -> Outcome:
        """Load the embedder and bring the memory index up to date (in a worker thread)."""
        report = await asyncio.to_thread(self.memory.warm)
        self._apply_vocabulary_now()
        how = "by meaning" if self.embedder.loaded else "by keywords (no embedding model: orion setup)"
        return "ok", f"search {how}; {report.files} files and {report.turns} turns indexed"

    async def _load_ears(self) -> Outcome:
        """Load speech recognition (nothing without a microphone or with voice input off)."""
        from orion.inference import release_memory_async

        if not self.microphone or not self.cfg.voice.enabled:
            return "skipped", "voice input is off"
        await self.voice.load_ears()
        await release_memory_async()  # the copy the model was loaded from
        stt = self.voice.info["stt"]
        return "ok", f"{stt['model']} on the {'GPU' if stt['device'] == 'cuda' else 'CPU'}" if stt else "loaded"

    def _listen(self) -> None:
        """Open the microphone once speech recognition is loaded."""
        if self.boot["ears"].status != "ok":
            return
        if not self.voice.start_listening():
            self._set_boot("ears", "error", "the microphone could not be opened")

    # ------------------------------------------------------------------------------------------------ the façade
    @property
    def busy(self) -> bool:
        """Whether he is answering, speaking or in a voice conversation."""
        return self.turns.busy or self.voice.speaking or self.voice.conversation_open

    def hud_connected(self, connected: bool) -> None:
        """A HUD connected or went away.

        Args:
            connected: True for a new connection, False for one that closed.
        """
        self._clients = max(0, self._clients + (1 if connected else -1))
        if self._clients:
            self.hud_open.set()
        else:
            self.hud_open.clear()

    def hands_view(self, viewer: int, on: bool, *, picture: bool = False) -> None:
        """A HUD connection shows the live view of the hands, or stops showing it: while any does, each gesture remote's
        view is followed (with the camera's picture while any asks for it), and its frames go to those connections.

        Args:
            viewer: the connection (any number that is its own).
            on: whether it shows the view now.
            picture: whether it shows the camera's picture too.
        """
        if on:
            if viewer not in self._hands_viewers:
                self._hands_sent.clear()  # a new viewer gets the next frame even if nothing in it changed
            self._hands_viewers[viewer] = picture
        else:
            self._hands_viewers.pop(viewer, None)
        self._follow_hands()

    def _follow_hands(self) -> None:
        """Follow each gesture remote's live view as the HUDs showing it want (stopping what nobody wants)."""
        picture = any(self._hands_viewers.values())
        uri = HANDS_CAMERA if picture else HANDS_LIVE if self._hands_viewers else ""
        remotes = set(gesture_remotes(self.cfg.mcp_servers, on_only=True)) if uri else set()
        for name, (followed, stop) in list(self._hands_watch.items()):
            if name not in remotes or followed != uri:
                stop()
                del self._hands_watch[name]
        for name in remotes - self._hands_watch.keys():
            self._hands_watch[name] = (uri, self.hub.watch(name, uri, functools.partial(self._hands_frame, name),
                                                           every=HANDS_REFRESH_S))
        self._hands_link()

    def _hands_link(self) -> None:
        """Tell the HUDs showing the live view of each gesture remote Orion isn't connected to (the view says so,
        with why): only a connected remote sends frames."""
        if not self._hands_viewers:
            return
        remotes = set(gesture_remotes(self.cfg.mcp_servers, on_only=True))
        for entry in self.hub.status():
            if entry["name"] in remotes and entry["status"] != "connected":
                link = {"connected": False, "error": entry["error"] or entry["status"], "camera": False,
                        "paused": False, "tracker": ""}
                self._hands_frame(entry["name"], json.dumps({"seq": 0, "hands": [], "link": link}))

    def _hands_frame(self, server: str, text: str) -> None:
        """A frame of a remote's live view: on to the HUDs showing it, unless it says nothing new (a read again of the
        same frame), and with its picture only when that is new too (the HUD keeps the last one)."""
        try:
            frame = json.loads(text)
        except ValueError:
            return
        if not isinstance(frame, dict):
            return
        said = (frame.get("seq"), json.dumps(frame.get("link"), sort_keys=True, default=str), frame.get("jpg_seq"))
        before = self._hands_sent.get(server)
        if said == before:
            return
        if before is not None and said[2] == before[2]:
            frame.pop("jpg", None)
        self._hands_sent[server] = said
        self.bus.publish("hands", server=server, frame=frame)

    async def take_voice(self, surface: "Surface", on: bool) -> None:
        """A HUD takes the voice (its microphone and speakers carry the conversation) or lets go of it.

        Args:
            surface: the HUD.
            on: take it (True) or let go (False).
        """
        if not on:
            self.voice.release_voice(surface.id)
            return
        try:
            await self.voice.take_voice(surface.id, send=surface.send, send_audio=surface.send_audio,
                                        name=surface.device)
        except Exception as e:
            log.warning("%s couldn't take the voice: %s", surface.device, e, exc_info=not isinstance(e, RuntimeError))
            surface.send({"type": "toast", "ts": time.time(), "title": "VOICE",
                          "text": f"This device can't carry the voice: {e}"})
            holder = self.surfaces.get(self.voice.holder)  # who has it, said to this tab alone: it lets go at once
            surface.send({"type": "voice_holder", "ts": time.time(), "device": self.voice.holder,
                          "name": holder.device if holder is not None else ""})
        self._publish_capabilities()

    def set_muted(self, muted: bool, minutes: int = 0) -> None:
        """Mute or unmute the microphone. A mute for some minutes ends by itself unless changed again before then.

        Args:
            muted: mute or unmute.
            minutes: unmute by itself after this many minutes (0: until unmuted).
        """
        if self._unmute is not None:
            self._unmute.cancel()
            self._unmute = None
        self.voice.set_muted(muted)
        self.bus.publish("mic", muted=self.voice.muted)
        if muted and minutes > 0:
            self._unmute = asyncio.get_running_loop().call_later(minutes * 60, self.set_muted, False)

    def request_exit(self, action: ExitAction) -> None:
        """Stop or restart Orion (under systemd, as a proper job, so Ollama follows).

        Args:
            action: "stop" or "restart".
        """
        self.exit_action = action
        if UNDER_SERVICE:
            verb = "restart" if action == "restart" else "stop"
            self.supervisor.spawn(asystemctl(verb, "--no-block", ORION_UNIT), name="exit")
        else:
            self.on_exit()

    def forget_everything(self) -> None:
        """Forget everything he knows (the HUD's Settings, Memory, or ``orion memory reset``): he restarts, and
        while everything is stopped (the tools process too, so nothing writes meanwhile) ``memory.wipe`` clears it
        all."""
        log.info("forgetting everything: restarting with a blank memory")
        self.forget_on_stop = True
        self.bus.publish("toast", title="MEMORY", text="Forgetting everything. He'll be back in a moment.")
        self.request_exit("restart")

    async def retry_brain(self) -> None:
        """Try to get a brain that failed at startup ready again (the HUD's models page, after signing in)."""
        if self.brain_error and not self.turns.loading:
            await self._step("brain", self._load_brain)
            self._publish_capabilities()

    def capabilities(self) -> dict[str, JSON]:
        """What he has to work with, for the HUD's status line."""
        cfg = self.cfg
        stt = self.voice.info["stt"]
        llm = self.brain.llm
        standin = isinstance(llm, FallbackLLM) and llm.on_fallback
        return {"tools": len(self.hub.tools()), "servers": self.hub.connected(), "calls": self.ops.count,
                "skills": len(self.skills.all()),
                "models": {"llm": label(cfg, cfg.brain.fallback) if standin else label(cfg),
                           "provider": cfg.brain.fallback if standin else cfg.brain.provider,
                           "fallback": label(cfg, cfg.brain.fallback) if cfg.brain.fallback not in (
                               "none", cfg.brain.provider) else "",
                           "standing_in": standin, "reason": llm.reason if isinstance(llm, FallbackLLM) else "",
                           "stt": stt["model"] if stt else "", "stt_device": stt["device"] if stt else "",
                           "voice": cfg.tts.voice}}

    def _make_llm(self) -> "LLM":
        """The brain the settings name, with its stand-in (``brain.fallback``) wired to the HUD and the voice."""
        return make_llm(self.cfg, meter=self.spend, on_switch=self._brain_switched,
                        before_fallback=self._before_fallback)

    def _brain_switched(self, switch: "Switch") -> None:
        """The stand-in brain took over, or the main one answers again: show it, and say it once per outage."""
        self._publish_capabilities()
        if switch.to == "fallback":
            self.bus.publish("toast", title="BRAIN", text=f"{switch.primary} can't answer: {switch.fallback} stands "
                                                          f"in. ({switch.reason})")
            if switch.announce:
                prompts, user = self.cfg.prompts, self.cfg.user
                self.turns.announce(prompts.fallback_switched.format(
                    honorific=user.honorific, Honorific=user.honorific[:1].upper() + user.honorific[1:],
                    primary=switch.primary, fallback=switch.fallback, reason=switch.reason), "BRAIN")
        else:
            self.bus.publish("toast", title="BRAIN", text=f"{switch.primary} answers again.")

    async def _before_fallback(self) -> None:
        """Before the stand-in first answers: a local one needs Ollama running."""
        if self.cfg.brain.fallback == "ollama":
            await self._start_ollama()

    def snapshot(self) -> dict[str, Any]:
        """The HUD's ``init`` event: everything it shows, safe to build while starting."""
        cfg = self.cfg
        store = self.memory.store
        hud = cfg.hud
        return {
            "state": self.turns.state, "loading": self.turns.loading, "boot": self._boot_payload(),
            "muted": self.voice.muted, "voice_available": self.voice.listener is not None,
            "voice": not self.turns.quiet,
            "conv": self.brain.conv.id, "history": self._history(), "live_turn": self.turns.live(),
            "reminders": store.pending_reminders(), "todos": store.open_todos(), "weather": self.last_weather,
            "news": self.last_news,
            "stats": self.last_stats, "stats_history": self._stats_history(), "capabilities": self.capabilities(),
            "restart": self.settings.restart_pending(), "confirms": self.confirmer.pending(),
            "host": profile().as_dict(), "surfaces": self.surfaces.snapshot(), "jobs": self.jobs.all(),
            "alerts": [alert.as_dict() for alert in self.watchers.active()],
            "voice_holder": {"device": self.voice.holder, "name": (
                holder.device if (holder := self.surfaces.get(self.voice.holder)) is not None else "")},
            "honorific": cfg.user.honorific, "user_name": cfg.user.name, "brain_error": self.brain_error,
            "spend": self.spend.summary(),
            "hud": {"quick_timers": list(hud.quick_timers), "max_message_chars": hud.max_message_chars,
                    "stats_interval_s": hud.stats_interval_s, "tool_result_chars": hud.tool_result_chars,
                    "sounds": hud.sounds},
        }

    def _stats_history(self) -> "list[Stats]":
        """The last ``STATS_HISTORY_S`` seconds of vital signs, oldest first, for the HUD's charts."""
        keep = max(2, round(STATS_HISTORY_S / self.cfg.hud.stats_interval_s))
        return list(self.stats_history)[-keep:]

    def _history(self) -> list[dict[str, Any]]:
        """The current conversation as the HUD rebuilds it: its messages with their turns' steps, and other apps'
        operations in between, by time."""
        found = self.memory.store.hud_history(self.brain.conv.id, self.cfg.hud.history_messages)
        rows: list[dict[str, Any]] = []
        for m in found.messages:
            if m["role"] == "user" or m["source"] == "announcement":
                rows.append({"id": f"m{m['id']}", "role": m["role"], "content": m["content"],
                             "source": m["source"] or "hud", "ts": m["ts"]})
            else:
                rows.append({"id": m["turn"] or f"m{m['id']}", "role": "assistant", "content": m["content"],
                             "source": m["source"] or "hud", "ts": m["ts"], "steps": found.steps.get(m["turn"], [])})
        rows += [{"id": f"x-{s['id']}", "role": "ops", "content": "", "source": "app", "ts": s["at"], "steps": [s],
                  "client": s["client"]} for s in found.external]
        return sorted(rows, key=lambda row: row["ts"])

    # ------------------------------------------------------------------------------------------------ settings
    async def apply_settings(self, patch: Mapping[str, Any], *, actor: Actor) -> dict[str, JSON]:
        """Validate (with the live choices), save and apply a settings change.

        Args:
            patch: the change, as sections of options or flat keys.
            actor: "hud" or "ai".

        Returns:
            ``{changed, restart}``.

        Raises:
            Refused: the change is invalid or not the actor's to make.
        """
        changes = await self.validate_settings(patch, actor=actor)
        return await self._commit(changes, actor)

    async def validate_settings(self, patch: Mapping[str, Any], *, actor: Actor) -> dict[str, Any]:
        """Validate a settings change without saving it (the AI's brain switches are checked now, made later).

        Args:
            patch: the change.
            actor: "hud" or "ai".

        Returns:
            The keys that change, with their values.

        Raises:
            Refused: the change is invalid or not the actor's to make.
        """
        flat = normalize_patch(patch)
        enforce(settings_refusal(flat, actor, ai_writable().__contains__))
        return self.settings.validate(patch, actor=actor, choices=await self._choices(actor, flat))

    async def apply_servers(self, servers: Mapping[str, Any], *, actor: Actor) -> dict[str, JSON]:
        """Save ``mcp_servers`` whole and reconfigure the hub (``tools.core.Controls`` applied the server policy).

        Args:
            servers: every server's settings.
            actor: who asked (the settings event's source).

        Returns:
            ``{changed, restart}``.
        """
        changes = self.settings.validate({"mcp_servers": dict(servers)}, actor="hud")
        return await self._commit(changes, actor)

    async def apply_own(self, patch: Mapping[str, Any]) -> dict[str, JSON]:
        """Save settings the user changed on a page of Customize (a connector's, a plugin's, a skill's): checked like
        any other change, without the key rights of the Settings pages, since the page's own code decides what it may
        write.

        Args:
            patch: the change, as sections of options or flat keys.

        Returns:
            ``{changed, restart}``.

        Raises:
            Refused: the change is invalid.
        """
        return await self._commit(self.settings.validate(patch, actor="hud", own=True), "hud")

    def reserved(self, name: str) -> bool:
        """Whether a name is one of Orion's own groups of tools or the directory's: no added connector may take it."""
        from orion.connectors import is_reserved

        return is_reserved(name)

    async def set_channel(self, channel: str, never: Sequence[str], confirm: Sequence[str]) -> dict[str, JSON]:
        """What a channel may never do and always asks about (the HUD's Safety page).

        Args:
            channel: voice, hud, discord, job or app.
            never: tools, ``<connector>:*`` or classes (``@destroy``) it may never use.
            confirm: those he always asks about first.

        Returns:
            ``{changed, restart}``.

        Raises:
            LookupError: there is no such channel.
            Refused: an entry isn't a tool, a connector or a class.
        """
        channels = {name: {kind: list(entries) for kind, entries in rule.items()}
                    for name, rule in self.cfg.safety.channels.items()}
        if channel not in channels:
            raise LookupError(f"there is no channel called {channel}")
        channels[channel] = {"never": list(dict.fromkeys(never)), "confirm": list(dict.fromkeys(confirm))}
        changes = self.settings.validate({"safety.channels": channels}, actor="hud", own=True)
        result = await self._commit(changes, "hud")
        if changes:
            self.activity.record("system", source="hud", title=f"Changed what {channel} may do", outcome="ok",
                                 actor="you, in the HUD")
        return result

    def safety_view(self) -> dict[str, JSON]:
        """What the HUD's Safety page shows: each channel's limits, the sandbox, and what a limit can name."""
        from orion import sandbox

        cfg, safety = self.cfg, self.cfg.safety
        found = sandbox.status()
        tools = {tool.name: {"name": tool.name, "title": tool.title or tool.name, "connector": tool.server,
                             "risk": risk_of(tool.hints)} for _, _, tool in self.hub.offered()}
        return {"channels": {name: {"never": list(rule["never"]), "confirm": list(rule["confirm"])}
                             for name, rule in safety.channels.items()},
                "sandbox": {"available": profile().sandbox, "commands": safety.sandbox_commands,
                            "network": safety.sandbox_network, "network_ok": found.network, "why": found.why,
                            "hidden": list(safety.sandbox_hidden), "writable": list(safety.sandbox_writable)},
                "choices": {"tools": sorted(tools.values(), key=lambda tool: str(tool["name"])),
                            "classes": list(CLASSES)},
                "restart": list(self.settings.restart_pending()), "data": str(cfg.data_path)}

    def secrets_view(self) -> dict[str, JSON]:
        """The vault as the HUD shows it: whether it works, and each secret's name, whether it is set and what uses it
        (never a value)."""
        return {"store": self.cfg.secrets.store, "ok": self._opened is not None and not self.vault_error,
                "error": self.vault_error, "items": vaulting.listing(self.cfg, self._opened)}

    def keep_secret(self, name: str, value: str, *, who: str = "you, in the HUD") -> None:
        """Set a secret (a worker thread: the vault may be a keyring), tell the redactor, and record it (the name only).

        Raises:
            Refused: there is no vault, the name isn't one, or the vault refused.
        """
        try:
            self.vault.set(name, value)
        except VaultError as e:
            raise Refused(str(e)) from None
        redact.refresh()
        self.activity.record("secret", source="hud", title=f"Set the secret {name}", outcome="ok", actor=who)

    def forget_secret(self, name: str, *, who: str = "you, in the HUD") -> None:
        """Delete a secret (a worker thread), and record it.

        Raises:
            Refused: there is no vault, the name isn't one, or the vault refused.
            LookupError: it isn't set.
        """
        try:
            gone = self.vault.delete(name)
        except VaultError as e:
            raise Refused(str(e)) from None
        if not gone:
            raise LookupError(f"the secret {name} isn't set")
        redact.refresh()
        self.activity.record("secret", source="hud", title=f"Deleted the secret {name}", outcome="ok", actor=who)

    @property
    def vault(self) -> Vault:
        """His vault (what the connectors, the sign-in broker and the plugin library keep their secrets in).

        Raises:
            VaultError: it couldn't be opened (``vault_error`` says why).
        """
        if self._opened is None:
            raise VaultError(f"His vault isn't available: {self.vault_error}")
        return self._opened

    async def remove_calendar(self, name: str) -> dict[str, JSON]:
        """Remove a calendar source.

        Raises:
            LookupError: there is none by that name.
        """
        if name not in self.cfg.calendar.sources:
            raise LookupError(f"no calendar called {name}")
        return await self.apply_own({"calendar.sources": {key: value for key, value in
                                                          self.cfg.calendar.sources.items() if key != name}})

    async def _choices(self, actor: Actor, keys: Collection[str]) -> dict[str, list[Any]]:
        """The values some keys may take right now: installed voices, the Whisper catalogue, and for the AI the
        installed Ollama models (so a prompt injection can't start a download)."""
        choices: dict[str, list[Any]] = {"stt.model": list(STT_MODELS)}
        if (tts := self.voice.tts) is not None:
            choices["tts.voice"] = tts.voices()
        if actor != "hud" and "ollama.model" in keys:
            installed, why = await self.ollama.models()
            if not installed:
                raise Refused(f"There's no model on this PC to switch to{f': {why}' if why else ''}.")
            choices["ollama.model"] = [model["name"] for model in installed]
        if actor != "hud":  # the scripted brain is the user's tool for testing, never the AI's choice
            brains = [provider for provider in PROVIDERS if provider != SCRIPT]
            choices["brain.provider"], choices["brain.fallback"] = brains, ["none", *brains]
        return choices

    async def _commit(self, changes: Mapping[str, Any], source: str) -> dict[str, JSON]:
        """Save validated changes, apply them, and say what changed. A secret in them goes to the vault first, and the
        file keeps a reference."""
        changes = await asyncio.to_thread(self._vaulted, dict(changes))
        changed = await self.settings.commit(dict(changes)) if changes else []
        if changed:
            await self._dispatch(changed, source)
        restart: list[JSON] = list(self.settings.restart_pending())
        return {"changed": list(changed), "restart": restart}

    def _vaulted(self, changes: dict[str, Any]) -> dict[str, Any]:
        """The changes with every plaintext secret in them in the vault, a reference in its place, and the redactor told
        (a worker thread: a keyring may take a moment).

        Raises:
            Refused: there is no vault to keep a secret in, or it refused.
        """
        keys = [key for key in vaulting.KEYS if key in changes]
        if not keys:
            return changes
        try:
            secured = vaulting.secure({key: changes[key] for key in keys},
                                      {key: value_of(self.cfg, key) for key in keys}, self._opened)
        except VaultError as e:
            raise Refused(f"A secret can't be saved: {e}") from None
        redact.refresh()
        return {**changes, **secured}

    async def _dispatch(self, changed: Sequence[str], source: str) -> None:
        """Run each action once for the union of the changed keys' ``Apply`` flags, in a fixed order."""
        flags = functools.reduce(operator.or_, (meta(key).apply for key in changed), Apply.LIVE)
        spawn = self.supervisor.spawn
        appliers: tuple[tuple[Apply, Callable[[], object]], ...] = (
            (Apply.REDACT, self._apply_redaction),
            (Apply.MCP, self._apply_mcp),
            (Apply.LLM, lambda: spawn(self._switch_llm(), name="switch the brain")),
            (Apply.OLLAMA_UNIT, lambda: spawn(self._refresh_ollama_unit(), name="refresh ollama.service")),
            (Apply.STT, lambda: spawn(self._reload_stt(), name="reload speech recognition")),
            (Apply.VOCABULARY, self._apply_vocabulary_now),
            (Apply.SPEAKER, lambda: self.voice.set_output_device(self.cfg.voice.output_device)),
            (Apply.ECHO, lambda: spawn(self.voice.reload_echo(), name="echo cancellation")),
            (Apply.PLUGINS, self._apply_plugins),
            (Apply.WEATHER, self._city_changed.set), (Apply.REINDEX, self.memory.reindex),
            (Apply.DOCUMENTS, self.documents.reconfigure))
        for flag, action in appliers:
            if flag in flags:
                try:
                    action()
                except Exception:
                    log.exception("applying a %s change failed", flag.name)
        if any(key.startswith(("prompts.", "user.")) or key in CONTEXT_KEYS for key in changed):
            self.brain.invalidate_context()
        if any(key.startswith("openrouter.") for key in changed):  # a new budget or key: the HUD hears, limits are read
            self.spend.changed()
        self.bus.publish("settings", changed=list(changed), restart=list(self.settings.restart_pending()),
                         source=source)
        self._publish_capabilities()
        self.connectors_changed()

    def _apply_plugins(self) -> None:
        """The skills, plugins or marketplaces changed (here or by hand): look at the skills again, fetch what is new,
        and tell the HUDs."""
        self.skills.invalidate()
        self.supervisor.spawn(self.library.refresh(), name="refresh the marketplaces")
        self.bus.publish("plugins", items=self.library.plugins())

    def _apply_redaction(self) -> None:
        """Hide the shapes and the length the settings name now."""
        safety = self.cfg.safety
        self.redactor.configure(safety.secret_patterns, min_chars=safety.redact_min_chars)

    def _apply_mcp(self) -> None:
        """Make the hub match ``mcp_servers`` (and follow the live view of the gesture remotes there are now)."""
        self.supervisor.spawn(self.hub.configure(), name="reconfigure the servers")
        self._follow_hands()

    def _apply_vocabulary_now(self) -> None:
        """Give speech recognition the names to spell right."""
        cfg = self.cfg
        city = self.memory.home_city().split(",")[0].strip()
        self.voice.set_vocabulary([word for word in (cfg.user.name.strip(), city,
                                                     *self.memory.vocabulary(cfg.stt.vocabulary_max)) if word])

    @contextlib.asynccontextmanager
    async def _brain_loading(self) -> AsyncIterator[str]:
        """Wait for the answer under way, then hold new requests (the HUD shows the brain loading) until the block
        is over; yields the brain's label."""
        name = label(self.cfg)
        await self.turns.idle()
        self.turns.set_loading(name)
        try:
            yield name
        finally:
            self.turns.set_loading("")

    async def _switch_llm(self) -> None:
        """Hold new requests, wait for the answer under way, then build, start and warm up the new brain."""
        old = self.brain.llm
        async with self._brain_loading() as name:
            try:
                local = [llm for llm in members(old) if llm.provider == "ollama"]
                for llm in local:  # free its GPU memory, whatever comes next
                    await self.ollama.unload(llm.model)
                provider = self.cfg.brain.provider
                if provider == "ollama":
                    await self._start_ollama()
                elif local and self._owns_ollama and self.cfg.brain.fallback != "ollama":  # nothing needs it now
                    await self.ollama.stop()
                    self._owns_ollama = False
                if "claude" in (provider, self.cfg.brain.fallback):
                    from orion.llm.claude import claude_status

                    await claude_status(fresh=True)  # signed in since the last look?
                self.brain.set_llm(self._make_llm())
                await self.brain.warm_up()
                self.brain_error = ""
                self.bus.publish("toast", title="BRAIN", text=f"Ready: now thinking with {name}.")
            except Exception as e:
                self.brain_error = str(e) or type(e).__name__
                log.warning("could not switch to %s: %s", name, self.brain_error, exc_info=not isinstance(e, LLMError))
                self.bus.publish("toast", title="BRAIN", text=f"Could not load {name}: {self.brain_error}")
        self._publish_capabilities()

    async def _start_ollama(self) -> None:
        """Start Ollama (rendering its unit from the settings first) unless it answers already."""
        unit = OllamaUnit.from_config(self.cfg)
        if unit.local:
            await asyncio.to_thread(refresh_units, ROOT, unit)
        if await self.ollama.start(self.cfg.ollama.start_timeout_s):
            self._owns_ollama = True

    async def _refresh_ollama_unit(self) -> None:
        """Rewrite ollama.service; a running Ollama restarts with the new values once no answer is under way."""
        unit = OllamaUnit.from_config(self.cfg)
        if not unit.local:
            return
        changed = await asyncio.to_thread(refresh_units, ROOT, unit)
        if OLLAMA_UNIT not in changed or not await asyncio.to_thread(unit_active, OLLAMA_UNIT):
            return
        async with self._brain_loading():
            try:
                code, out = await asystemctl("try-restart", OLLAMA_UNIT)
                if code:
                    raise OllamaError(out or f"systemctl exited with {code}")
                if self.cfg.brain.provider == "ollama":
                    await self.ollama.start(self.cfg.ollama.start_timeout_s)
                    await self.brain.warm_up()
            except (OllamaError, LLMError) as e:
                self.bus.publish("toast", title="BRAIN", text=f"Ollama didn't restart cleanly: {e}")

    async def _reload_stt(self) -> None:
        """Load the speech recognition model the settings name, saying so in the HUD."""
        from orion.inference import release_memory_async

        if self.voice.stt is None:
            return
        self.bus.publish("toast", title="EARS", text="Loading the speech recognition model...")
        try:
            await self.voice.reload_ears()
        except Exception as e:
            log.exception("speech recognition did not reload")
            self.bus.publish("toast", title="EARS", text=f"Could not load that model: {e}")
        else:
            await release_memory_async()
            stt = self.voice.info["stt"]
            if stt is not None:
                where = "GPU" if stt["device"] == "cuda" else "CPU"
                self.bus.publish("toast", title="EARS", text=f"Listening with {stt['model']} on the {where}.")
        self._publish_capabilities()

    async def _config_changed(self) -> None:
        """config.yaml changed on disk: read it again and apply what changed (Orion's own writes are ignored)."""
        result = await self.settings.reload()
        if result.problems:
            self._publish_problems()
        if result.changed:
            await self._dispatch(result.changed, "file")
            now = vaulting.current(self.cfg)  # a secret typed into the file by hand: into the vault, and the file says so
            if (fixed := await asyncio.to_thread(self._vaulted, dict(now))) != now:
                await self._commit({key: value for key, value in fixed.items() if value != now[key]}, "file")

    # ------------------------------------------------------------------------------------------------ hub callbacks
    def _tools_changed(self) -> None:
        """A server connected, dropped or changed: give the brain the new toolbox, tell the HUD."""
        version = self.hub.version
        if version != self._tools_version:
            self._tools_version = version
            self.brain.set_tools(self.hub.tools(), version)
        self._publish_capabilities()
        self._hands_link()
        self.instant.tools_changed()
        self.connectors_changed()

    def connectors_changed(self) -> None:
        """Something about the connectors changed (a server connected, a setting, a sign-in): tell the HUDs, once, after
        a short pause since such changes come in bursts. The list is not sent again when it is what was sent last."""
        if self._connectors_due is None and self.hud_open.is_set():
            self._connectors_due = self._loop.call_later(
                CONNECTORS_PAUSE_S, lambda: self.supervisor.spawn(self._send_connectors(), name="connectors"))

    async def _send_connectors(self) -> None:
        """Work out the connectors now and send them to the HUDs unless they are as last sent (each HUD hears them as
        it should: whether it is on this PC is its own)."""
        self._connectors_due = None
        items: list[JSON] = list(await self.connectors.snapshot(local=True))
        if items != self._connectors_sent:
            self._connectors_sent = items
            self.bus.publish("connectors", items=items)

    def _tool_event(self, event: dict[str, JSON]) -> None:
        """An event one of his tools published (a memory saved, the weather): on to the HUD. A change of the reminders
        or the to-dos is shown from the database, once, as another app's change is."""
        kind = str(event.get("type") or "")
        if kind in ("reminders", "todos"):
            self._data_changed()
            return
        if kind == "weather" and isinstance(weather := event.get("weather"), dict):
            self.last_weather = cast("Weather", weather)
        elif kind == "news" and isinstance(items := event.get("items"), list):
            self.last_news = items
        elif kind == "skill_changed":
            self.skills.invalidate()
        if kind:
            self.bus.publish(kind, **{key: value for key, value in event.items() if key not in ("type", "ts")})

    async def _intent(self, server: str, intent: "Intent") -> None:
        """An intent a server pushed (``mcp_servers.<server>.intents``), such as a hand gesture: run it."""
        await self.intents.run(server, intent)

    def _record(self, record: StepRecord) -> None:
        """Keep the brain's, other apps' and instant commands' operations with the conversation (the HUD's are not
        kept). An instant command's are the turn's own: the table keeps "brain" and "app" only."""
        source = record["source"]
        if source in ("brain", "app", "user"):
            kept: StepRecord = {**record, "source": "app" if source == "app" else "brain"}
            store = self.memory.store
            store.submit(store.log_step, kept, self.brain.conv.id)

    async def _forecast(self, city: str) -> "Weather | None":
        """Today's weather in a city, from the weather service (instant commands say it)."""
        return await self.weather.forecast(city, 1)

    def _indexed(self, report: "SyncReport") -> None:
        """The memory index changed (on the database's thread): tell the HUD; new names for speech recognition."""
        self.bus.publish("memory_indexed", version=report.version, files=report.files, turns=report.turns)
        if report.files:
            self._loop.call_soon_threadsafe(self._apply_vocabulary_now)

    def _on_idle(self) -> None:
        """A voice conversation closed: fold the database log in, and hand freed memory back now and then."""
        from orion.inference import release_memory_async

        store = self.memory.store
        store.submit(store.checkpoint)
        now = time.monotonic()
        if now - self._released >= self.cfg.performance.release_interval_s and not self.busy:
            self._released = now
            self.supervisor.spawn(release_memory_async(), name="release memory")

    # ------------------------------------------------------------------------------------------------ loops
    async def _scheduler(self) -> None:
        """Announce timers and reminders, run jobs and make the scheduled backup as they fall due; sleep until the next
        one or until a list changes, never longer than ``SCHEDULER_MAX_SLEEP_S`` (the clock asyncio waits on stops
        while the machine sleeps, so the wall clock is looked at again regularly). A job missed by more than
        ``jobs.catch_up_s`` (Orion was off) waits for its next time; a backup missed is made at once."""
        store = self.memory.store
        while True:
            self._reminders_changed.clear()
            now = time.time()
            for item in store.due_reminders(now):
                if await store.call(store.finish_reminder, item["id"]):
                    self.turns.announce(announcement(item, now, self.cfg),
                                        "Timer" if item["kind"] == "timer" else "Reminder")
            for job in store.due_jobs(now):
                self.jobs.advance(job, now)
                late = now - (job["next_run"] or now)
                if late > self.cfg.jobs.catch_up_s:
                    log.info("job %s was missed by %.0f min: it waits for its next time", job["name"], late / 60)
                    continue
                self.run_job(job, manual=False)
            if self.backups.due(now):
                self.backups.start("system")
            pending = store.pending_reminders()
            if pending != self._reminders:
                self._reminders = pending
                self.bus.publish("reminders", items=cast(JSON, pending))
            due = [t for t in (pending[0]["due"] if pending else None, store.next_job_run(), self.backups.next_run())
                   if t is not None]
            wait = min(SCHEDULER_MAX_SLEEP_S, max(0.0, min(due) - time.time())) if due else SCHEDULER_MAX_SLEEP_S
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._reminders_changed.wait(), wait)

    def _activity_event(self, event: dict[str, JSON]) -> None:
        """Job runs and watcher alerts reach the activity log from the event bus (the hub records the tool calls)."""
        kind = event.get("type")
        if kind == "job_run" and event.get("phase") == "end":
            self.activity.record("job", source="job", title=f"Ran the job “{event.get('name')}”",
                                 outcome="ok" if event.get("ok") else "failed", detail=str(event.get("text") or ""),
                                 actor=str(event.get("name") or ""))
        elif kind == "alert" and isinstance(item := event.get("item"), dict):
            self.activity.record("alert", source="watcher", title=str(item.get("title") or "A warning"),
                                 detail=str(item.get("text") or ""), actor="a watcher")

    async def _prune_activity(self) -> None:
        """Forget what is older than ``activity.keep_days``: at start, then once a day."""
        store = self.memory.store
        while True:
            await store.call(self.activity.prune, self.cfg.activity.keep_days)
            await asyncio.sleep(86400)

    def _restic_password(self) -> str | None:
        """The password of the restic repository, from the vault (``self.vault``, the security package's)."""
        return self.vault.get("backup.restic.password")

    def _alert(self, alert: "Alert") -> None:
        """A watcher warns: show it and say it (aloud with ``watchers.speak``), and send it on Discord too
        (``watchers.discord``)."""
        self.bus.publish("alert", item=alert.as_dict())
        self.turns.announce(alert.text, alert.title, spoken=self.cfg.watchers.speak)
        if self.cfg.watchers.discord:
            self.supervisor.spawn(self.send_on_discord(alert.title, alert.text), name="alert on Discord")

    def discord_server(self) -> str:
        """The connected server that can message the user on Discord ("" when there is none)."""
        return next((server for server, connected, tool in self.hub.offered()
                     if connected and tool.remote == "discord_send"), "")

    async def send_on_discord(self, title: str, text: str) -> str:
        """Message the user on Discord (a job's result, a warning); what happened, in words."""
        server = self.discord_server()
        if not server:
            return "Error: no Discord is connected (Customize, Connectors, Discord)."
        return await self.hub.call(server, "discord_send", {"title": title, "text": text},
                                   CallContext("hud", client=server))

    async def _ask_on_discord(self, question: "Question", timeout_s: float) -> bool | None:
        """Ask the user yes or no on Discord (a request that came from there, or a job's): None when it can't."""
        server = question.device if question.source_kind == "discord" else self.discord_server()
        if not server:
            return None
        said = await self.hub.call(server, "discord_confirm", {"question": question.text, "detail": question.detail,
                                                                "timeout_s": max(5, int(timeout_s))},
                                   CallContext("hud", client=server))
        return True if said == "yes" else False if said == "no" else None

    def run_job(self, job: "JobRow", *, manual: bool = True) -> None:
        """Run a job in the background (now, or because it fell due); safe to call from any thread (the HUD's REST
        endpoints and sync tools run on worker threads).

        Args:
            job: the job.
            manual: asked for now rather than due.
        """
        def start() -> None:
            self.supervisor.spawn(self.job_runner.run(job, manual=manual), name=f"job {job['name']}")

        self._loop.call_soon_threadsafe(start)

    async def _watch_memory(self) -> None:
        """Index memory files and pick up skills as they are saved (by hand, or by his tools in their process)."""
        import watchfiles

        from orion.skills import SKILL_FILE

        cfg = self.cfg
        for folder in (cfg.vault_path, cfg.skills_path):
            folder.mkdir(parents=True, exist_ok=True)
        async for changes in watchfiles.awatch(cfg.vault_path, cfg.skills_path, debounce=WATCH_DEBOUNCE_MS,
                                               watch_filter=lambda _, path: path.endswith(".md"),
                                               stop_event=self._stopping):
            paths = [Path(path) for _, path in changes]
            if any(p.name == SKILL_FILE and p.is_relative_to(cfg.skills_path) for p in paths):
                self.skills.invalidate()
            if notes := [p for p in paths if p.is_relative_to(cfg.vault_path)]:
                self.memory.on_files_changed(notes)

    async def _watch_data(self) -> None:
        """Apply hand edits of config.yaml; notice database changes made elsewhere (a reminder another app set)."""
        import watchfiles

        config, data = self.settings.path, self.cfg.data_path
        names = {config.name, DB_WAL}
        folders = sorted({config.parent, data})
        async for changes in watchfiles.awatch(*folders, recursive=False, debounce=WATCH_DEBOUNCE_MS,
                                               watch_filter=lambda _, path: Path(path).name in names,
                                               stop_event=self._stopping):
            paths = {Path(path) for _, path in changes}
            if config in paths:
                await self._config_changed()
            if data / DB_WAL in paths:
                self._data_changed()

    def _data_changed(self) -> None:
        """Someone committed to the database (one of his tools, another app): look at the reminders again, and
        republish changed to-dos."""
        store = self.memory.store
        version = store.data_version()
        if version == self._data_version:
            return
        self._data_version = version
        self._reminders_changed.set()
        todos = store.open_todos()
        if todos != self._todos:
            self._todos = todos
            self.bus.publish("todos", items=cast(JSON, todos))

    async def _stats_loop(self) -> None:
        """The PC's vital signs: read all the time (well under a millisecond each), so a HUD that opens draws the last
        minute at once; sent only while one is open."""
        tick = 0
        while True:
            top = self.cfg.hud.vitals_programs if self.hud_open.is_set() and tick % STATS_TOP_EVERY == 0 else 0
            stats = await asyncio.to_thread(self.vitals.snapshot, slow=tick % STATS_SLOW_EVERY == 0, programs=top)
            tick += 1
            self.last_stats = stats
            self.stats_history.append(stats)
            self.watchers.check(stats)
            if self.hud_open.is_set():
                self.bus.publish("stats", stats=cast(JSON, stats))
            await asyncio.sleep(self.cfg.hud.stats_interval_s)

    async def _library_loop(self) -> None:
        """The marketplaces and skill sources Customize looks in: fetched when older than ``plugins.refresh_h`` (a
        marketplace that hasn't changed costs a few bytes), looked at again every hour."""
        await asyncio.to_thread(self.library.load)
        while True:
            await self.library.refresh()
            await asyncio.sleep(STALE_CHECK_S)

    async def _weather_loop(self) -> None:
        """The weather the HUD shows, fetched while one is open: on opening, every ``weather.refresh_s``, and when
        the home city changes."""
        import httpx

        while True:
            await self.hud_open.wait()
            self._city_changed.clear()
            wait: float = self.cfg.weather.refresh_s
            city = self.memory.home_city() or self.memory.store.get_setting("last_weather_city")
            if city:
                try:
                    weather = await self.weather.forecast(city, self.cfg.weather.widget_days)
                except httpx.HTTPError as e:
                    log.warning("weather update failed: %s", e)
                    wait = WEATHER_RETRY_S
                else:
                    if weather is not None:
                        self.last_weather = weather
                        self.bus.publish("weather", weather=cast(JSON, weather))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._city_changed.wait(), wait)

    # ------------------------------------------------------------------------------------------------ stop
    async def stop(self) -> None:
        """Stop everything, in order; a part that fails to stop is logged and the rest still stop."""
        from orion.tools import web

        self._stopping.set()
        sd_notify("STOPPING=1")
        steps: list[tuple[str, Callable[[], Awaitable[object]]]] = [
            ("the review", self.reviewer.stop), ("the backup", self.backups.stop),
            ("the documents", self.documents.stop), ("the turns", self.turns.close),
            ("the voice", self.voice.close),
            ("the sign-ins", self.broker.close), ("instant commands", self.instant.close),
            ("the servers", self.hub.stop), ("the background work", self.supervisor.aclose),
            ("the brain", self.brain.close), ("what it costs", self.spend.close), ("Ollama", self._stop_ollama),
            ("the web client", web.aclose)]
        for what, stop in steps:
            try:
                await stop()
            except Exception:
                log.exception("stopping %s failed", what)
        self.weather.cache.close()
        await asyncio.to_thread(self.memory.close)
        if self.forget_on_stop:
            from orion.memory import wipe

            try:
                await asyncio.to_thread(wipe, self.cfg)
            except Exception:
                log.exception("forgetting everything failed")

    async def _stop_ollama(self) -> None:
        """Stop the Ollama this run started: kept for the next run across a restart in a terminal; under systemd it
        goes with orion.service by itself."""
        try:
            if not self._owns_ollama or UNDER_SERVICE:
                return
            if self.exit_action == "restart":
                os.environ[OWNS_OLLAMA] = "1"  # the restarted process inherits it, and stops it in the end
            else:
                await self.ollama.stop()
        finally:
            await self.ollama.aclose()

    # ------------------------------------------------------------------------------------------------ events
    def _boot_payload(self) -> dict[str, JSON]:
        """The startup progress, as the HUD shows it."""
        return {"ready": self.ready.is_set(), "steps": [dataclasses.asdict(step) for step in self.boot.values()]}

    def _set_boot(self, name: BootName, status: BootStatus, detail: str) -> None:
        """Record a startup step's outcome and show it."""
        self.boot[name] = BootStep(name, status, detail)
        self._publish_boot()

    def _publish_boot(self) -> None:
        """Send the startup progress to the HUD."""
        self.bus.publish("boot", **self._boot_payload())

    def _publish_capabilities(self) -> None:
        """Send what he has to work with to the HUD."""
        self.bus.publish("capabilities", **self.capabilities())

    def _publish_problems(self) -> None:
        """Tell the HUD what is wrong with config.yaml."""
        broken = self.settings.broken
        self.bus.publish("settings_problem", problems=[dataclasses.asdict(p) for p in self.settings.problems],
                         broken=dataclasses.asdict(broken) if broken is not None else None)
