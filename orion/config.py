"""Orion's settings: the values live in one YAML file, the policy lives here.

- ``orion/default_config.yaml`` is the factory copy and the ONLY place a default is written; ``data/config.yaml``
  is the user's copy of it, complete, with their values and the same explanations.
- This module holds what a YAML file must never decide: every option's type and range, what has to happen when it
  changes (``Apply``), whether the HUD shows it (``Hud``), what Orion's own tools may do with it (``AI``), which
  values are secret, and which ``{placeholders}`` each prompt may use. The section models have no defaults: every value
  comes from the template, so it is written once.
- ``Settings`` is the one object per process. It loads the file (merging it over the factory copy, checking it
  leniently so one bad value never costs the others), saves changes atomically off the event loop, follows hand
  edits, and validates every change the HUD or the AI asks for before anything is saved.

PyYAML's C loader reads; ruamel.yaml, imported only when a file is written, writes and keeps the template's comments.
"""

import asyncio
import copy
import enum
import hashlib
import io
import json
import logging
import os
import re
import string
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal, Required, Self, TypedDict, get_args, get_origin

import yaml
from annotated_types import Ge, Gt, Le, Lt
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    ValidationInfo,
    field_validator,
)
from pydantic.fields import FieldInfo

from orion.base import MARKET_NAME, PLUGIN_NAME, SKILL_REF, Actor, file_stamp, repo_url, write_atomic

if TYPE_CHECKING:
    from ruamel.yaml import YAML
    from ruamel.yaml.comments import CommentedMap

log = logging.getLogger("orion.config")

ROOT: Final = Path(__file__).resolve().parent.parent
CONFIG_PATH: Final = Path(os.environ.get("ORION_CONFIG") or ROOT / "data" / "config.yaml")  # cannot live in itself
TEMPLATE_PATH: Final = Path(__file__).with_name("default_config.yaml")
BASE_NAME: Final = ".config-base.json"  # beside config.yaml: fingerprints of the factory values it reflects
BACKUP_SUFFIX: Final = ".bak"  # config.yaml.bak keeps the file as it was before Orion dropped or changed options
VERSION: Final = 2  # the settings layout; equal to `version:` in the template (template_values checks it)
USER_HEADER: Final = (
    "# ORION settings: every option, with your values. The HUD's Settings pages save this file as you change\n"
    "# things, and Orion picks up your own edits the moment you save; lines marked [restart] apply after a\n"
    "# restart. orion/default_config.yaml holds the factory values: an option you never changed follows it\n"
    "# when an update improves it. Types, ranges and who may change what are enforced in orion/config.py.\n"
)
TOP_LEVEL: Final = ("data_dir", "mcp_servers")  # the two options that sit outside any section

_LOADER: Final = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
_COMMENT: Final = " # "  # separates a template value from its explanation (so no template value may contain it)
_URL: Final = r"^https?://\S+$"
_FILE_NAME: Final = r"^[\w.-]+$"
_MODEL_NAME: Final = r"^[\w.-]+(/[\w.-]+)?$"  # a Whisper catalogue name or a Hugging Face repo id

type Role = Literal["main", "tools"]  # "main" may write config.yaml; the tools process and mcp-serve never do
type Flat = dict[str, Any]  # "section.option" -> value; "data_dir" and "mcp_servers" stay bare
type Stamp = tuple[int, int, int]  # (st_mtime_ns, st_size, st_ino) of config.yaml when last read or written


# ---------------------------------------------------------------------------------------------------- policy
class Apply(enum.Flag):
    """What must happen when a setting changes. LIVE (no flag): consumers read it at use time, so nothing."""

    LIVE = 0
    LLM = enum.auto()  # swap the language-model client (after the answer under way)
    STT = enum.auto()  # reload speech recognition in the background
    VOCABULARY = enum.auto()  # recompute the speech-recognition hint words
    SPEAKER = enum.auto()  # move playback to voice.output_device
    ECHO = enum.auto()  # start, change or stop the echo canceller
    MCP = enum.auto()  # reconfigure the MCP hub
    REINDEX = enum.auto()  # rebuild the memory index (in the main process, which owns it)
    WEATHER = enum.auto()  # fetch the weather again
    REDACT = enum.auto()  # build the redactor again (new secret patterns or length)
    OLLAMA_UNIT = enum.auto()  # rewrite ollama.service and restart Ollama if it runs (after the answer)
    PLUGINS = enum.auto()  # look at the skills, the plugins and the marketplaces again
    DOCUMENTS = enum.auto()  # look at the documents' folders again (what is indexed, and how)
    RESTART = enum.auto()  # applies after Orion restarts


class AI(enum.StrEnum):
    """What Orion's own settings tools may do with a key (decision 5: the allow-list lives in code)."""

    NONE = "none"  # invisible to settings_get: prompts, secrets, tuning (it also saves tokens)
    READ = "read"  # shown by settings_get, refused by settings_set
    WRITE = "write"  # the allow-list


type Control = Literal["switch", "slider", "number", "select", "text", "secret", "list", "abilities"]
type ChoicesFrom = Literal["", "voices", "devices.input", "devices.output", "stt_models", "claude_models",
                           "ollama_models", "openrouter_models"]


@dataclass(frozen=True, slots=True)
class Hud:
    """How the HUD edits a key; only keys with a Hud are on the Settings pages (and writable by the "hud" actor).

    Attributes:
        control: the kind of control.
        label: the label shown beside it ("" = the option's name, humanised).
        step: a slider's step.
        unit: the unit shown after the value.
        options: fixed choices for a field that is not a Literal (ollama.num_ctx).
        choices_from: where the server finds dynamic choices to send beside the schema.
    """

    control: Control
    label: str = ""
    step: float | None = None
    unit: str = ""
    options: tuple[int | str, ...] = ()
    choices_from: ChoicesFrom = ""


@dataclass(frozen=True, slots=True)
class Prompt:
    """The {placeholders} a prompt may use and those it must keep. A prompt that allows none is used as written.

    Attributes:
        allowed: the names it may use.
        required: the names it must keep.
    """

    allowed: frozenset[str] = frozenset()
    required: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class Meta:
    """The policy of one key. A field without a Meta is live, file only, invisible to the AI and not secret.

    Attributes:
        apply: what must happen when it changes.
        hud: how the HUD edits it, None when it is in the file only.
        ai: what Orion's own tools may do with it.
        secret: never shown: not to the HUD, the AI or the logs.
        prompt: the placeholders of a prompt, None for anything else.
    """

    apply: Apply = Apply.LIVE
    hud: Hud | None = None
    ai: AI = AI.NONE
    secret: bool = False
    prompt: Prompt | None = None


def everyday(apply: Apply = Apply.LIVE, *, hud: Hud, ai: AI = AI.WRITE, secret: bool = False) -> Meta:
    """The policy of a key on the Settings pages (writable by the AI unless `ai` says otherwise).

    Args:
        apply: what must happen when it changes.
        hud: how the HUD edits it.
        ai: what the AI may do with it.
        secret: whether it is never shown.

    Returns:
        The Meta.
    """
    return Meta(apply=apply, hud=hud, ai=ai, secret=secret)


def restart() -> Meta:
    """The policy of a file-only key that applies after a restart."""
    return Meta(apply=Apply.RESTART)


def prompt(*allowed: str, required: tuple[str, ...] = ()) -> Meta:
    """The policy of a prompt: file only, invisible to the AI, with the placeholders it may and must use.

    Args:
        allowed: the placeholder names it may use; none means the text is used exactly as written.
        required: the names it must keep (a subset of `allowed`).

    Returns:
        The Meta.
    """
    return Meta(prompt=Prompt(frozenset(allowed), frozenset(required)))


def check_placeholders(text: str, spec: Prompt) -> None:
    """Refuse a prompt whose {placeholders} its code could not fill.

    A prompt that allows no placeholders is used as written, so any text is fine. Otherwise every field must be one
    of the allowed names, with no conversion, format spec, attribute or index and no positional field; braces must
    be balanced (a literal brace is written twice); and every required name must be present.

    Args:
        text: the prompt.
        spec: what it may and must use.

    Raises:
        ValueError: what is wrong, naming the placeholder.
    """
    if not spec.allowed:
        return
    seen: set[str] = set()
    try:
        parsed = list(string.Formatter().parse(text))
    except ValueError as e:
        raise ValueError(f"unbalanced braces ({e}); write a literal brace twice: {{{{ or }}}}") from None
    for _, name, format_spec, conversion in parsed:
        if name is None:
            continue
        if name == "" or name.isdigit():
            raise ValueError("a positional {} placeholder; name it, e.g. {" + min(spec.allowed) + "}")
        if any(c in name for c in ".["):
            raise ValueError(f"{{{name}}} reaches inside a value, which is not allowed")
        if conversion or format_spec:
            raise ValueError(f"{{{name}}} has a conversion or format spec, which is not allowed")
        if name not in spec.allowed:
            raise ValueError(f"unknown placeholder {{{name}}}; it may use {_names(spec.allowed)}")
        seen.add(name)
    if missing := spec.required - seen:
        raise ValueError(f"it must keep {_names(missing)}")


def _names(names: Collection[str]) -> str:
    """The placeholders as they are written in a prompt: "{a}, {b}"."""
    return ", ".join(f"{{{n}}}" for n in sorted(names))


# ---------------------------------------------------------------------------------------------------- the sections
class Section(BaseModel):
    """Base of every settings section: every option required, unknown options refused, JSON-strict types."""

    model_config = ConfigDict(extra="forbid", strict=True)


class UserConfig(Section):
    """Who the user is."""

    name: Annotated[str, Field(max_length=60), everyday(Apply.VOCABULARY, hud=Hud("text", "Your name"))]
    honorific: Annotated[str, Field(min_length=1, max_length=30), everyday(hud=Hud("text", "How he addresses you"))]
    city: Annotated[str, Field(max_length=80),
                    everyday(Apply.WEATHER | Apply.VOCABULARY, hud=Hud("text", "Home city"))]


class BrainConfig(Section):
    """How he thinks, whatever the model."""

    provider: Annotated[Literal["claude", "ollama", "openrouter", "script"],
                        everyday(Apply.LLM, hud=Hud("select", "Brain"))]
    fallback: Annotated[Literal["none", "claude", "ollama", "openrouter", "script"],
                        everyday(Apply.LLM, hud=Hud("select", "When the brain can't answer"))]
    think: Annotated[bool, everyday(hud=Hud("switch", "Hidden reasoning"))]
    temperature: Annotated[float, Field(ge=0, le=1.5), everyday(hud=Hud("slider", "Temperature", step=0.05))]
    max_tool_rounds: Annotated[int, Field(ge=1, le=12), everyday(hud=Hud("slider", "Tool rounds", step=1))]
    history_chars: Annotated[int, Field(ge=2000, le=60000),
                             everyday(hud=Hud("slider", "Conversation kept in mind", step=1000, unit="chars"))]
    session_gap_min: Annotated[int, Field(ge=0, le=1440)]
    snapshot_journal_chars: Annotated[int, Field(ge=0, le=4000)]
    tool_result_chars: Annotated[int, Field(ge=500, le=50000)]
    history_tool_result_chars: Annotated[int, Field(ge=200, le=20000)]
    first_chunk_timeout_s: Annotated[float, Field(ge=10, le=600)]
    chunk_timeout_s: Annotated[float, Field(ge=5, le=300)]
    fallback_retry_min: Annotated[int, Field(ge=1, le=1440)]
    fallback_on: Annotated[list[Literal["limit", "billing", "auth", "unavailable", "timeout", "model", "other"]],
                           Field(max_length=7)]


class ClaudeConfig(Section):
    """Your Claude plan, through Claude Code."""

    model: Annotated[Literal["haiku", "sonnet", "opus"],
                     everyday(Apply.LLM, hud=Hud("select", "Claude model", choices_from="claude_models"))]
    review_model: Literal["haiku", "sonnet", "opus"]
    all_tools: bool
    session_idle_s: Annotated[int, Field(ge=60, le=7200)]
    tool_timeout_s: Annotated[int, Field(ge=30, le=3600)]


class OllamaConfig(Section):
    """The local brain (opt-in): Ollama on this PC."""

    model: Annotated[str, Field(pattern=r"^[\w./-]+(:[\w.-]+)?$", max_length=100),
                     everyday(Apply.LLM, hud=Hud("select", "Model on this PC", choices_from="ollama_models"))]
    num_ctx: Annotated[int, Field(ge=2048, le=131072),
                       everyday(Apply.LLM, hud=Hud("select", "Context window", unit="tokens",
                                                    options=(4096, 8192, 16384, 32768)))]
    host: Annotated[str, Field(pattern=r"^https?://[^/\s]+$"), restart()]
    keep_alive: int | Annotated[str, Field(pattern=r"^\d+(\.\d+)?(ms|s|m|h)$")]
    top_p: Annotated[float, Field(ge=0, le=1)]
    top_k: Annotated[int, Field(ge=1, le=200)]
    presence_penalty: Annotated[float, Field(ge=0, le=2)]
    start_timeout_s: Annotated[float, Field(ge=2, le=120)]
    api_timeout_s: Annotated[float, Field(ge=1, le=120)]
    flash_attention: Annotated[bool, Meta(Apply.OLLAMA_UNIT)]
    kv_cache_type: Annotated[Literal["f16", "q8_0", "q4_0"], Meta(Apply.OLLAMA_UNIT)]
    num_parallel: Annotated[int, Field(ge=1, le=8), Meta(Apply.OLLAMA_UNIT)]
    max_loaded_models: Annotated[int, Field(ge=1, le=4), Meta(Apply.OLLAMA_UNIT)]


class OpenRouterConfig(Section):
    """A cloud brain: the conversation goes to openrouter.ai."""

    model: Annotated[str, Field(min_length=1, max_length=200),
                     everyday(Apply.LLM, hud=Hud("select", "Cloud model", choices_from="openrouter_models"))]
    api_key: Annotated[str, Field(max_length=500),
                       everyday(Apply.LLM, hud=Hud("secret", "OpenRouter key"), ai=AI.NONE, secret=True)]
    daily_budget_usd: Annotated[float, Field(ge=0, le=1000),
                                everyday(hud=Hud("number", "Daily budget", step=0.1, unit="$"), ai=AI.READ)]
    base_url: Annotated[str, Field(pattern=_URL), Meta(Apply.LLM)]
    models_url: Annotated[str, Field(pattern=_URL)]
    top_p: Annotated[float, Field(ge=0, le=1)]
    timeout_s: Annotated[float, Field(ge=10, le=600), Meta(Apply.LLM)]

    @property
    def config_key(self) -> str:
        """api_key with ${secret:NAME} and ${VAR} filled in; "" when it is empty or names something that isn't set
        (a secret the vault doesn't hold or can't read, a variable that isn't set)."""
        from orion.secrets import VaultError, expand

        try:
            expanded = expand(self.api_key).strip()
        except VaultError as e:
            log.warning("the OpenRouter key can't be read: %s", e)
            return ""
        return "" if "$" in expanded else expanded  # a reference to an unset variable is left as written: no key

    @property
    def key(self) -> str:
        """The key to use: `config_key`, else $OPENROUTER_API_KEY; "" when neither is set."""
        return self.config_key or os.environ.get("OPENROUTER_API_KEY", "").strip()


class VoiceConfig(Section):
    """Listening and the conversation."""

    enabled: Annotated[bool, everyday(Apply.RESTART, hud=Hud("switch", "Microphone"), ai=AI.READ)]
    name_wake: Annotated[bool, everyday(hud=Hud("switch", "Wake on his name"))]
    barge_in: Annotated[bool, everyday(hud=Hud("switch", "Interrupt with his name"))]
    echo_cancellation: Annotated[bool, everyday(Apply.ECHO, hud=Hud("switch", "Echo cancellation"), ai=AI.READ)]
    noise_suppression: Annotated[bool, everyday(Apply.ECHO, hud=Hud("switch", "Noise suppression"), ai=AI.READ)]
    wake_model: Annotated[str, Field(pattern=r"^[\w.-]*$", max_length=100),
                          everyday(Apply.RESTART, hud=Hud("text", "Wake-word model"), ai=AI.READ)]
    wake_threshold: Annotated[float, Field(ge=0.05, le=0.95),
                              everyday(hud=Hud("slider", "Wake-word sensitivity", step=0.05))]
    follow_up_seconds: Annotated[float, Field(ge=0, le=60),
                                 everyday(hud=Hud("slider", "Conversation window", step=1, unit="s"))]
    end_silence_ms: Annotated[int, Field(ge=300, le=2000),
                              everyday(hud=Hud("slider", "Pause that ends a sentence", step=50, unit="ms"))]
    max_utterance_s: Annotated[float, Field(ge=5, le=60),
                               everyday(hud=Hud("slider", "Longest request", step=5, unit="s"))]
    chime: Annotated[bool, everyday(hud=Hud("switch", "Chimes"))]
    input_device: Annotated[str | int | None, everyday(Apply.RESTART, ai=AI.READ,
                                                       hud=Hud("select", "Microphone device",
                                                               choices_from="devices.input"))]
    output_device: Annotated[str | int | None, everyday(Apply.SPEAKER, ai=AI.READ,
                                                        hud=Hud("select", "Speaker device",
                                                                choices_from="devices.output"))]
    end_silence_short_ms: Annotated[int, Field(ge=200, le=2000)]
    end_silence_long_ms: Annotated[int, Field(ge=300, le=3000)]
    command_wait_s: Annotated[float, Field(ge=1, le=20)]
    vad_threshold: Annotated[float, Field(ge=0.05, le=0.95)]
    vad_silence_threshold: Annotated[float, Field(ge=0.05, le=0.95)]
    min_speech_ms: Annotated[int, Field(ge=32, le=1000)]
    speculative: Literal["auto", "on", "off"]


class InstantConfig(Section):
    """Instant commands: whole sentences answered without the model (``orion.instant``)."""

    enabled: Annotated[bool, everyday(hud=Hud("switch", "Instant commands"), ai=AI.READ)]
    intents: Annotated[list[str], Field(max_length=64)]
    home_assistant: Annotated[str, Field(pattern=r"^[\w.-]*$", max_length=100)]
    area: Annotated[str, Field(max_length=100)]
    device_domains: Annotated[list[Annotated[str, Field(pattern=r"^[a-z_]+$")]], Field(max_length=32)]
    context_s: Annotated[int, Field(ge=30, le=86400)]
    time_format: Annotated[str, Field(min_length=1, max_length=40)]
    date_format: Annotated[str, Field(min_length=1, max_length=40)]
    replies: dict[str, str]

    @field_validator("intents")
    @classmethod
    def _handled(cls, value: list[str]) -> list[str]:
        """Each is an intent Orion knows how to do without the model."""
        from orion.instant import HANDLED

        if unknown := sorted(set(value) - HANDLED):
            raise ValueError(f"Orion can't do {', '.join(unknown)} at once; he can: {', '.join(sorted(HANDLED))}")
        return value

    @field_validator("replies")
    @classmethod
    def _replies(cls, value: dict[str, str]) -> dict[str, str]:
        """Every reply is there, and uses only its own placeholders (``orion.instant.REPLIES``)."""
        from orion.instant import REPLIES

        if missing := sorted(REPLIES.keys() - value.keys()):
            raise ValueError(f"replies lacks {', '.join(missing)}")
        if extra := sorted(value.keys() - REPLIES.keys()):
            raise ValueError(f"there is no reply called {', '.join(extra)}; they are {', '.join(REPLIES)}")
        for name, text in value.items():
            try:
                check_placeholders(text, Prompt(REPLIES[name] | {"honorific", "Honorific"}))
            except ValueError as e:
                raise ValueError(f"replies.{name}: {e}") from None
        return value


class TTSConfig(Section):
    """His voice."""

    voice: Annotated[str, Field(pattern=r"^[ab][fm]_\w+$"),
                     everyday(hud=Hud("select", "Voice", choices_from="voices"))]
    speed: Annotated[float, Field(ge=0.5, le=2.0),
                     everyday(hud=Hud("slider", "Speed", step=0.05, unit="\u00d7"))]  # the times sign
    min_sentence_chars: Annotated[int, Field(ge=1, le=200)]
    first_chunk_min_chars: Annotated[int, Field(ge=5, le=200)]


class STTConfig(Section):
    """Speech recognition (faster-whisper)."""

    model: Annotated[str, Field(pattern=_MODEL_NAME, max_length=100),
                     everyday(Apply.STT, hud=Hud("select", "Speech recognition model", choices_from="stt_models"))]
    device: Annotated[Literal["auto", "cuda", "cpu"], everyday(Apply.STT, hud=Hud("select", "Runs on"))]
    compute_type: Annotated[Literal["auto", "int8", "int8_float16", "int8_bfloat16", "int16", "float16", "bfloat16",
                                    "float32"], Meta(Apply.STT)]
    beam_size: Annotated[int, Field(ge=1, le=10)]
    auto_gpu_model: Annotated[str, Field(pattern=_MODEL_NAME, max_length=100), Meta(Apply.STT)]
    auto_cpu_model: Annotated[str, Field(pattern=_MODEL_NAME, max_length=100), Meta(Apply.STT)]
    extra_vocabulary: Annotated[list[str], Field(max_length=100), Meta(Apply.VOCABULARY)]
    hallucinations: list[str]
    vocabulary_max: Annotated[int, Field(ge=0, le=200), Meta(Apply.VOCABULARY)]
    no_speech_threshold: Annotated[float, Field(ge=0, le=1)]
    logprob_threshold: Annotated[float, Field(ge=-5, le=0)]
    fallback_temperatures: Annotated[list[Annotated[float, Field(ge=0, le=1)]], Field(min_length=1, max_length=6)]


class MemoryConfig(Section):
    """What he remembers."""

    review: Annotated[bool, everyday(hud=Hud("switch", "Review conversations"))]
    review_after_idle_s: Annotated[int, Field(ge=30, le=600),
                                   everyday(hud=Hud("slider", "Review after", step=30, unit="s"))]
    recall: Annotated[bool, everyday(hud=Hud("switch", "Recall on his own"))]
    user_chars: Annotated[int, Field(ge=500, le=4000),
                          everyday(hud=Hud("slider", "About you (USER.md)", step=100, unit="chars"))]
    self_chars: Annotated[int, Field(ge=500, le=4000),
                          everyday(hud=Hud("slider", "His notes (MEMORY.md)", step=100, unit="chars"))]
    log_conversations: Annotated[bool, everyday(hud=Hud("switch", "Conversations in the logs"), ai=AI.NONE)]
    same_fact_similarity: Annotated[float, Field(ge=0.5, le=1)]
    similar_fact_similarity: Annotated[float, Field(ge=0.5, le=1)]
    review_window_chars: Annotated[int, Field(ge=1000, le=50000)]
    review_temperature: Annotated[float, Field(ge=0, le=1.5)]
    review_max_user_facts: Annotated[int, Field(ge=0, le=20)]
    review_max_notes: Annotated[int, Field(ge=0, le=20)]
    review_max_updates: Annotated[int, Field(ge=0, le=20)]
    review_max_concepts: Annotated[int, Field(ge=0, le=30)]
    db_timeout_s: Annotated[float, Field(ge=1, le=120), restart()]


class BackupConfig(Section):
    """Backups of his memory and settings (``orion/backup.py``): an archive on a schedule, and optionally a restic
    copy off this machine. The AI may read when and where, never change them; the restic repository may hold a
    login, so it isn't shown to the AI at all."""

    folder: Annotated[str, Field(min_length=1, max_length=500),
                      Meta(hud=Hud("text", "Backup folder"), ai=AI.READ)]
    schedule: Annotated[str, Field(max_length=100), Meta(hud=Hud("text", "When (cron)"), ai=AI.READ)]
    keep: Annotated[int, Field(ge=1, le=365), Meta(hud=Hud("slider", "Backups kept", step=1), ai=AI.READ)]
    restic_repository: Annotated[str, Field(max_length=500), Meta(hud=Hud("text", "restic repository"))]
    restic_keep_daily: Annotated[int, Field(ge=0, le=3650)]
    restic_keep_weekly: Annotated[int, Field(ge=0, le=520)]
    restic_keep_monthly: Annotated[int, Field(ge=0, le=1200)]
    restic_timeout_s: Annotated[int, Field(ge=60, le=86400)]
    compress_level: Annotated[int, Field(ge=1, le=9)]

    @field_validator("schedule")
    @classmethod
    def _schedule(cls, value: str) -> str:
        """Empty (no schedule), or a cron schedule cronsim reads (``orion.jobs.parse_schedule`` says what's wrong)."""
        from orion.jobs import parse_schedule

        return parse_schedule(value) if value.strip() else ""


class ToolsConfig(Section):
    """Which tools a model sees."""

    max_per_turn: Annotated[int, Field(ge=5, le=64), everyday(hud=Hud("slider", "Tools per message", step=1))]
    always: Annotated[list[str], Field(max_length=64)]
    min_relevant: Annotated[int, Field(ge=0, le=20)]
    recent_kept: Annotated[int, Field(ge=0, le=20)]
    find_results: Annotated[int, Field(ge=1, le=20)]
    find_min_score: Annotated[float, Field(ge=0, le=1)]
    warm_up: Annotated[bool, restart()]


class CalendarConfig(Section):
    """The user's calendars (``tools/calendar.py``). Their sources hold passwords and secret links: never shown to the
    HUD or the AI, and the AI never changes them."""

    sources: Annotated[dict[str, dict[str, Any]], Meta(secret=True)]
    write_to: str
    cache_s: Annotated[int, Field(ge=0, le=86400)]
    timeout_s: Annotated[float, Field(ge=2, le=120)]


class DocumentsConfig(Section):
    """Your documents (``orion/documents.py``): the folders he indexes, what he reads in them and how. Which folders
    he may read is the user's alone: the AI may see them, never change them (it could point him at ~/.ssh)."""

    folders: Annotated[list[Annotated[str, Field(min_length=1, max_length=500)]], Field(max_length=32),
                       Meta(Apply.DOCUMENTS, hud=Hud("list", "Folders"), ai=AI.READ)]
    extensions: Annotated[list[Annotated[str, Field(pattern=r"^[a-z0-9]{1,10}$")]], Field(min_length=1, max_length=40),
                          Meta(Apply.DOCUMENTS, hud=Hud("list", "File types"), ai=AI.READ)]
    excludes: Annotated[list[Annotated[str, Field(min_length=1, max_length=200)]], Field(max_length=64),
                        Meta(Apply.DOCUMENTS, hud=Hud("list", "Skip"), ai=AI.READ)]
    max_file_mb: Annotated[int, Field(ge=1, le=1000),
                           Meta(Apply.DOCUMENTS, hud=Hud("number", "Largest file", unit="MB"), ai=AI.READ)]
    max_chars: Annotated[int, Field(ge=10_000, le=50_000_000), Meta(Apply.DOCUMENTS)]
    chunk_chars: Annotated[int, Field(ge=200, le=8000), Meta(Apply.DOCUMENTS)]
    chunk_overlap: Annotated[int, Field(ge=0, le=2000), Meta(Apply.DOCUMENTS)]
    pdf_tables: Annotated[bool, Meta(Apply.DOCUMENTS)]
    workers: Annotated[int, Field(ge=1, le=16)]
    worker_memory_mb: Annotated[int, Field(ge=512, le=16384),  # below ~400 MB the readers cannot even start
                                 Meta(Apply.DOCUMENTS)]  # a document that did not fit is read again with more
    convert_timeout_s: Annotated[float, Field(ge=5, le=3600), Meta(Apply.DOCUMENTS)]  # ...or more time
    search_results: Annotated[int, Field(ge=1, le=20)]
    search_candidates: Annotated[int, Field(ge=5, le=200)]
    min_similarity: Annotated[float, Field(ge=0, le=1)]
    result_chars: Annotated[int, Field(ge=100, le=5000)]
    read_chars: Annotated[int, Field(ge=500, le=100_000)]


class JobsConfig(Section):
    """Scheduled jobs: requests he runs on his own."""

    provider: Annotated[Literal["brain", "claude", "ollama", "openrouter"],
                        everyday(hud=Hud("select", "Jobs think with"))]
    max_tool_rounds: Annotated[int, Field(ge=1, le=20)]
    run_timeout_s: Annotated[float, Field(ge=10, le=3600)]
    catch_up_s: Annotated[int, Field(ge=0, le=86400)]
    deliver: Annotated[list[Literal["speak", "hud", "discord"]], Field(max_length=3)]


class WatchersConfig(Section):
    """He speaks up when the machine needs you (0 switches a watcher off)."""

    battery_low: Annotated[int, Field(ge=0, le=60), everyday(hud=Hud("slider", "Battery low", step=1, unit="%"))]
    battery_critical: Annotated[int, Field(ge=0, le=30),
                                everyday(hud=Hud("slider", "Battery critical", step=1, unit="%"))]
    cpu_hot: Annotated[int, Field(ge=0, le=110), everyday(hud=Hud("slider", "Processor hot", step=1, unit="°C"))]
    gpu_hot: Annotated[int, Field(ge=0, le=110), everyday(hud=Hud("slider", "Graphics card hot", step=1, unit="°C"))]
    disk_full: Annotated[int, Field(ge=0, le=100), everyday(hud=Hud("slider", "Disk nearly full", step=1, unit="%"))]
    memory_full: Annotated[int, Field(ge=0, le=100),
                           everyday(hud=Hud("slider", "Memory nearly full", step=1, unit="%"))]
    speak: Annotated[bool, everyday(hud=Hud("switch", "Say warnings aloud"))]
    discord: Annotated[bool, everyday(hud=Hud("switch", "Send warnings on Discord too"))]
    hold_s: Annotated[int, Field(ge=0, le=600)]
    repeat_min: Annotated[int, Field(ge=1, le=1440)]


class EyesConfig(Section):
    """What he sees: pictures from the screen, a camera, a browser or an attachment, as the models get them."""

    image_max_side: Annotated[int, Field(ge=256, le=2000)]
    image_quality: Annotated[int, Field(ge=30, le=95)]
    capture_timeout_s: Annotated[float, Field(ge=2, le=120)]
    frame_timeout_s: Annotated[float, Field(ge=1, le=60)]
    share_wait_s: Annotated[float, Field(ge=5, le=300)]


class SafetyConfig(Section):
    """What keeps a model from abusing the PC: questions before risky actions, limits per channel, the sandbox
    commands run in, what the environment passes on, and the secrets he hides. The AI may read some of these, never
    change any: a request steered by a web page must not be able to switch off what guards against it."""

    confirm_after_content: Annotated[bool, everyday(hud=Hud("switch", "Ask before risky actions after web content"),
                                                    ai=AI.READ)]
    channels: dict[str, dict[str, list[str]]]
    always_confirm: Annotated[list[str], Field(max_length=64)]
    never_confirm: Annotated[list[str], Field(max_length=64)]
    confirm_timeout_s: Annotated[int, Field(ge=10, le=600)]
    sandbox_commands: Annotated[bool, everyday(hud=Hud("switch", "Run commands in a sandbox"), ai=AI.NONE)]
    sandbox_network: Annotated[bool, everyday(hud=Hud("switch", "Network for commands"), ai=AI.NONE)]
    sandbox_memory_mb: Annotated[int, Field(ge=128, le=65536)]
    sandbox_hidden: Annotated[list[str], Field(max_length=200),
                              everyday(hud=Hud("list", "Hidden from commands"), ai=AI.NONE)]
    sandbox_writable: Annotated[list[str], Field(max_length=100),
                                everyday(hud=Hud("list", "Folders commands may write in"), ai=AI.NONE)]
    env_allow: Annotated[list[str], Field(max_length=200)]
    env_desktop: Annotated[list[str], Field(max_length=100)]
    secret_patterns: Annotated[list[str], Field(max_length=100), Meta(Apply.REDACT)]
    redact_min_chars: Annotated[int, Field(ge=4, le=64), Meta(Apply.REDACT)]

    @field_validator("channels")
    @classmethod
    def _channels(cls, value: dict[str, dict[str, list[str]]]) -> dict[str, dict[str, list[str]]]:
        """Every channel is there with its two lists, each entry naming a tool, a connector or a class."""
        from orion.tools.policy import check_channels

        check_channels(value)
        return value

    @field_validator("sandbox_hidden", "sandbox_writable")
    @classmethod
    def _paths(cls, value: list[str]) -> list[str]:
        """Each is an absolute path or one in the home folder (~)."""
        if bad := [path for path in value if not re.fullmatch(r"(?:~|/)[^\0]*", path)]:
            raise ValueError(f"{bad[0]!r} isn't an absolute path or one starting with ~")
        return value

    @field_validator("sandbox_writable")
    @classmethod
    def _no_startup_files(cls, value: list[str]) -> list[str]:
        """None is, holds or lies in a place whose files run later outside the sandbox (the home folder itself, or
        ~/.config/autostart): a command could plant something there that runs at your next login."""
        from orion.sandbox import AUTORUN

        startup = [Path(path).expanduser() for path in AUTORUN]
        for path in value:
            folder = Path(path).expanduser()
            if folder == Path("/") or any(folder.is_relative_to(each) or each.is_relative_to(folder) for each in startup):
                raise ValueError(f"commands may not write in {path}: it holds startup files (such as ~/.bashrc or "
                                 "~/.config/autostart) that a command could use to run something outside the "
                                 "sandbox at your next login; name the folders in it that hold none")
        return value

    @field_validator("env_allow", "env_desktop")
    @classmethod
    def _variables(cls, value: list[str]) -> list[str]:
        """Each is a variable's name, or a prefix ending in * (LC_*)."""
        if bad := [name for name in value if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\*?", name)]:
            raise ValueError(f"{bad[0]!r} isn't an environment variable's name (LC_* stands for LC_ and more)")
        return value

    @field_validator("secret_patterns")
    @classmethod
    def _patterns(cls, value: list[str]) -> list[str]:
        """Each is a pattern RE2 can run (no lookaround or backreference) that can't match nothing."""
        from orion.redact import check_patterns

        check_patterns(value)
        return value


class HudConfig(Section):
    """The HUD (the web app reads these too)."""

    quick_timers: Annotated[list[Annotated[int, Field(ge=1, le=1440)]], Field(min_length=1, max_length=8),
                            everyday(hud=Hud("list", "Quick timers", unit="min"))]
    sounds: Annotated[bool, everyday(hud=Hud("switch", "Sounds"))]
    history_messages: Annotated[int, Field(ge=0, le=500)]
    stats_interval_s: Annotated[float, Field(ge=0.5, le=60)]
    vitals_programs: Annotated[int, Field(ge=1, le=12)]
    max_message_chars: Annotated[int, Field(ge=100, le=100000)]
    max_pictures: Annotated[int, Field(ge=0, le=10)]
    tool_result_chars: Annotated[int, Field(ge=50, le=5000)]


class ServerConfig(Section):
    """The HUD's web server: on this PC alone, or on your network too, where a device is paired first
    (``orion.pairing``). The AI may read these, never change them."""

    port: Annotated[int, Field(ge=1024, le=65535), everyday(Apply.RESTART, hud=Hud("number", "Port"), ai=AI.READ)]
    host: Annotated[str, everyday(Apply.RESTART, hud=Hud("select", "Who can open the HUD",
                                                         options=("127.0.0.1", "0.0.0.0")), ai=AI.READ)]
    names: list[Annotated[str, Field(pattern=r"^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*$", max_length=253)]]
    lan_only: bool
    session_days: Annotated[int, Field(ge=1, le=365)]
    pair_code_s: Annotated[int, Field(ge=30, le=900)]
    pair_tries: Annotated[int, Field(ge=1, le=10)]
    tls_cert: Annotated[str, restart()]
    tls_key: Annotated[str, restart()]

    @field_validator("host")
    @classmethod
    def _address(cls, value: str) -> str:
        """An address of this PC: 127.0.0.1 for this PC alone, 0.0.0.0 (or ::) for every address it has."""
        import ipaddress

        ipaddress.ip_address(value)  # a ValueError says what is wrong
        return value


def _repositories(urls: list[str]) -> list[str]:
    """Repositories as the settings keep them (``orion.base.repo_url``): "owner/repo" is written out."""
    return [repo_url(url) for url in urls]


class SkillsConfig(Section):
    """Skills he runs up front, the ones switched off, and where Customize looks for more."""

    auto_score: Annotated[float, Field(ge=0, le=1)]
    auto_margin: Annotated[float, Field(ge=0, le=1)]
    context_max: Annotated[int, Field(ge=0, le=50)]
    disabled: Annotated[list[Annotated[str, Field(pattern=f"^{SKILL_REF}$")]], Field(max_length=500),
                        Meta(Apply.PLUGINS)]
    sources: Annotated[list[str], Field(max_length=20), AfterValidator(_repositories), Meta(Apply.PLUGINS)]


class InstalledPlugin(TypedDict):
    """One installed plugin (``plugins.installed.<name>``), as ``orion.plugins`` records it.

    Keys:
        source: the marketplace it came from, or "upload".
        version: the version its manifest gives ("" when it gives none).
        sha: the commit it is pinned to, or a hash of its files when it comes from a marketplace's own folders or an
            upload.
        enabled: whether its skills, commands and connectors are switched on.
        installed_at: when it was first installed (epoch seconds).
    """

    source: Annotated[str, Field(pattern=f"^{MARKET_NAME}$")]
    version: Annotated[str, Field(max_length=100)]
    sha: Annotated[str, Field(pattern=r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")]
    enabled: bool
    installed_at: Annotated[float, Field(ge=0)]


class PluginsConfig(Section):
    """Claude plugins (their skills, commands and MCP servers) and the marketplaces they come from."""

    marketplaces: Annotated[dict[Annotated[str, Field(pattern=f"^{MARKET_NAME}$")], str],
                            Field(max_length=20), Meta(Apply.PLUGINS)]
    installed: Annotated[dict[Annotated[str, Field(pattern=f"^{PLUGIN_NAME}$")], InstalledPlugin],
                         Field(max_length=500), Meta(Apply.MCP | Apply.PLUGINS)]
    refresh_h: Annotated[int, Field(ge=1, le=720)]
    max_download_mb: Annotated[int, Field(ge=1, le=1024)]
    max_unpacked_mb: Annotated[int, Field(ge=1, le=4096)]
    max_files: Annotated[int, Field(ge=100, le=1_000_000)]
    timeout_s: Annotated[float, Field(ge=10, le=600)]

    @field_validator("marketplaces")
    @classmethod
    def _named(cls, value: dict[str, str]) -> dict[str, str]:
        """Each marketplace is a repository, and none is called "upload" (what an uploaded plugin's source says)."""
        if "upload" in value:
            raise ValueError("a marketplace can't be called upload")
        return dict(zip(value, _repositories(list(value.values())), strict=True))


class RetrievalConfig(Section):
    """Memory search and proactive recall."""

    recall_max_items: Annotated[int, Field(ge=0, le=3)]
    recall_chars: Annotated[int, Field(ge=100, le=2000)]
    recall_rank_window: Annotated[int, Field(ge=1, le=50)]
    recall_min_cosine: Annotated[float, Field(ge=0, le=1)]
    recall_solo_cosine: Annotated[float, Field(ge=0, le=1)]
    search_results: Annotated[int, Field(ge=1, le=20)]
    search_candidates: Annotated[int, Field(ge=5, le=200)]
    search_min_similarity: Annotated[float, Field(ge=0, le=1)]
    recency_half_life_days: Annotated[float, Field(ge=1, le=3650)]
    recency_weight: Annotated[float, Field(ge=0, le=2)]
    usage_weight: Annotated[float, Field(ge=0, le=2)]
    not_concepts: list[str]
    unit_max_chars: Annotated[int, Field(ge=200, le=5000), Meta(Apply.REINDEX)]
    turn_vector_days: Annotated[int, Field(ge=0, le=3650), Meta(Apply.REINDEX)]
    result_chars: Annotated[int, Field(ge=100, le=5000)]
    recent_notes: Annotated[int, Field(ge=1, le=50)]


class WakeConfig(Section):
    """Spotting his name in speech."""

    segment_end_ms: Annotated[int, Field(ge=200, le=3000)]
    check_pause_ms: Annotated[int, Field(ge=50, le=2000)]
    check_every_s: Annotated[float, Field(ge=0.5, le=10)]
    check_window_s: Annotated[float, Field(ge=1, le=15)]
    check_min_speech_s: Annotated[float, Field(ge=0.05, le=5)]
    greetings: list[str]
    mentions: list[str]
    lead_words: Annotated[int, Field(ge=1, le=10)]
    name_max_edits: Annotated[int, Field(ge=0, le=2)]
    candidate_model: Annotated[str, Field(pattern=r"^([\w.-]+(/[\w.-]+)?)?$", max_length=100), Meta(Apply.STT)]
    long_talk_s: Annotated[float, Field(ge=3, le=60)]


class PhrasesConfig(Section):
    """Short replies he understands without the model, the words that shape how a sentence ends, and how the HUD's
    panels are asked for."""

    stop: list[str]
    dismiss: list[str]
    affirm: list[str]
    deny: list[str]
    fillers: list[str]
    trailing: list[str]
    apologies: list[str]
    show: list[str]
    hide: list[str]
    everything: list[str]
    panels: dict[str, list[str]]

    @field_validator("panels")
    @classmethod
    def _parts(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        """Each names a part of the HUD: a panel, a realm, or a settings tab ("settings/abilities")."""
        from orion.voice.wake import HUD_PARTS

        if unknown := sorted(value.keys() - set(HUD_PARTS)):
            raise ValueError(f"the HUD has no {', '.join(unknown)}; it has {', '.join(HUD_PARTS)}")
        return value


class RemindersConfig(Section):
    """Timers and reminders."""

    late_after_s: Annotated[int, Field(ge=0, le=86400)]
    default_hour: Annotated[int, Field(ge=0, le=23), restart()]


class WeatherConfig(Section):
    """The weather, from Open-Meteo."""

    refresh_s: Annotated[int, Field(ge=300, le=86400)]
    widget_days: Annotated[int, Field(ge=3, le=7), Meta(Apply.WEATHER)]
    default_days: Annotated[int, Field(ge=1, le=7)]
    forecast_cache_s: Annotated[int, Field(ge=0, le=7200)]
    timeout_s: Annotated[float, Field(ge=2, le=60)]
    geocode_url: Annotated[str, Field(pattern=_URL)]
    forecast_url: Annotated[str, Field(pattern=_URL)]


class WebConfig(Section):
    """Searching and reading pages."""

    user_agent: Annotated[str, Field(min_length=1, max_length=500)]
    search_backends: Annotated[list[str], Field(min_length=1, max_length=10)]
    search_results: Annotated[int, Field(ge=1, le=10)]
    search_timeout_s: Annotated[float, Field(ge=1, le=30)]
    snippet_chars: Annotated[int, Field(ge=50, le=2000)]
    news_results: Annotated[int, Field(ge=1, le=20)]
    news_backends: Annotated[list[str], Field(min_length=1, max_length=10)]
    news_default_query: Annotated[str, Field(min_length=1, max_length=200)]
    page_chars: Annotated[int, Field(ge=500, le=100000)]
    page_max_bytes: Annotated[int, Field(ge=100_000, le=50_000_000)]
    page_timeout_s: Annotated[float, Field(ge=2, le=120)]
    cache_s: Annotated[int, Field(ge=0, le=86400)]


class ScriptConfig(Section):
    """The scripted brain: replies and tool calls read from a file instead of a model, for tests."""

    path: Annotated[str, Field(min_length=1, max_length=500)]


class BenchConfig(Section):
    """Benchmarks of the brains."""

    suite: Literal["builtin", "history"]
    history_requests: Annotated[int, Field(ge=1, le=500)]
    history_min_words: Annotated[int, Field(ge=1, le=20)]
    stop_after_errors: Annotated[int, Field(ge=1, le=100)]
    keep_runs: Annotated[int, Field(ge=1, le=200)]


class SystemConfig(Section):
    """His tools on this PC."""

    command_timeout_s: Annotated[int, Field(ge=1, le=600)]
    command_max_timeout_s: Annotated[int, Field(ge=10, le=3600), restart()]
    command_output_chars: Annotated[int, Field(ge=500, le=100000), restart()]
    read_file_chars: Annotated[int, Field(ge=500, le=200000)]
    find_timeout_s: Annotated[float, Field(ge=1, le=60)]
    find_max_results: Annotated[int, Field(ge=1, le=200)]
    find_skip_dirs: list[str]
    app_match_min_score: Annotated[float, Field(ge=0, le=1)]
    volume_step: Annotated[int, Field(ge=1, le=50)]
    power_delay_s: Annotated[float, Field(ge=0, le=60)]
    disk_path: Annotated[str, Field(pattern=r"^/"), restart()]


class McpConfig(Section):
    """MCP servers and the tools process."""

    startup_wait_s: Annotated[float, Field(ge=0, le=300)]
    heartbeat_s: Annotated[float, Field(ge=5, le=600)]
    backoff_max_s: Annotated[float, Field(ge=10, le=3600)]
    default_timeout_s: Annotated[float, Field(ge=5, le=3600)]
    description_chars: Annotated[int, Field(ge=100, le=10000)]
    recent_calls: Annotated[int, Field(ge=10, le=5000), restart()]
    signin_wait_s: Annotated[float, Field(ge=30, le=3600)]


class GesturesConfig(Section):
    """A gesture remote's gestures (``orion.gestures``): what they do until the user chooses, and what the HUD offers
    to choose from."""

    defaults: dict[str, dict[str, Any] | None]
    starts: dict[str, dict[str, Any]]
    timer_minutes: Annotated[list[Annotated[int, Field(ge=1, le=1440)]], Field(min_length=1, max_length=24)]
    awake_seconds: Annotated[list[Annotated[int, Field(ge=2, le=120)]], Field(min_length=1, max_length=24)]
    pause_minutes: Annotated[int, Field(ge=1, le=1440)]

    @field_validator("defaults")
    @classmethod
    def _bindings(cls, value: dict[str, dict[str, Any] | None]) -> dict[str, dict[str, Any] | None]:
        """Every default is a binding Orion can run (the message names the gesture)."""
        from orion.gestures import check_bindings

        check_bindings(value)
        return value

    @field_validator("starts")
    @classmethod
    def _starts(cls, value: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Each start names an action and makes a whole binding of it, without a hold or repeat."""
        from orion.gestures import ACTIONS, BindingError, parse_binding

        for do, start in value.items():
            if do not in ACTIONS:
                raise BindingError(f"{do} is not an action; they are {', '.join(ACTIONS)}")
            if extra := sorted(start.keys() & {"do", "hold", "repeat"}):
                raise BindingError(f"{do} starts with its options only, not {', '.join(extra)}")
            try:
                parse_binding({"do": do, **start})
            except BindingError as e:
                raise BindingError(f"{do}: {e}") from None
        return value


class PerformanceConfig(Section):
    """CPU and memory use."""

    cpu_threads: Annotated[int, Field(ge=0, le=64), restart()]
    release_interval_s: Annotated[int, Field(ge=60, le=86400)]


class ModelsConfig(Section):
    """What `orion setup` downloads."""

    embedding_repo: Annotated[str, Field(pattern=r"^[\w.-]+/[\w.-]+$"), restart()]
    kokoro_url: Annotated[str, Field(pattern=_URL), restart()]
    kokoro_model: Annotated[str, Field(pattern=_FILE_NAME), restart()]
    kokoro_voices: Annotated[str, Field(pattern=_FILE_NAME), restart()]
    openwakeword_url: Annotated[str, Field(pattern=_URL), restart()]


class LoggingConfig(Section):
    """The logs in data/logs."""

    quiet: Annotated[list[str], restart()]
    file_max_bytes: Annotated[int, Field(ge=100_000, le=100_000_000), restart()]
    file_backups: Annotated[int, Field(ge=0, le=20), restart()]


class SecretsConfig(Section):
    """Where his keys, tokens and passwords are kept, apart from this file (``orion.secrets``). Nothing about it is
    shown to the AI or changed by it."""

    store: Annotated[Literal["keyring", "files"], restart()]
    folder: Annotated[str, Field(min_length=1, max_length=300), restart()]


class ActivityConfig(Section):
    """The log of what he did and who allowed it (``orion.activity``)."""

    keep_days: Annotated[int, Field(ge=1, le=3650),
                         everyday(hud=Hud("number", "Keep the activity log for", unit="days"), ai=AI.NONE)]


class PromptsConfig(Section):
    """What he is told and what he says (decision 6: edited in config.yaml only, never by Orion himself)."""

    persona: Annotated[str, Field(min_length=1), prompt("who", "who_pos", "honorific")]
    voice_style: Annotated[str, prompt()]
    text_style: Annotated[str, prompt()]
    unnamed_user: Annotated[str, Field(min_length=1), prompt()]
    unnamed_possessive: Annotated[str, Field(min_length=1), prompt()]
    time_format: Annotated[str, Field(min_length=1), prompt()]
    context_time: Annotated[str, prompt("time", "tz")]
    context_home_city: Annotated[str, prompt("city")]
    context_user: Annotated[str, prompt()]
    context_user_empty: Annotated[str, prompt()]
    context_notes: Annotated[str, prompt()]
    context_skills: Annotated[str, prompt()]
    context_active_skill: Annotated[str, prompt("name")]
    context_recalled: Annotated[str, prompt()]
    context_changes: Annotated[str, prompt()]
    context_journal: Annotated[str, prompt()]
    context_reply: Annotated[str, prompt("channel", required=("channel",))]
    memory_review: Annotated[str, prompt("user", "orion", "facts", "fill", "transcript", "max_concepts",
                                         required=("transcript",))]
    json_system: Annotated[str, Field(min_length=1), prompt()]
    json_instruction: Annotated[str, Field(min_length=1), prompt()]
    error_reply: Annotated[str, prompt("honorific", "error")]
    tool_limit_reply: Annotated[str, prompt("honorific")]
    fallback_switched: Annotated[str, prompt("honorific", "Honorific", "primary", "fallback", "reason")]
    confirm_question: Annotated[str, prompt("honorific", "Honorific", "action", "source", required=("action",))]
    confirm_always: Annotated[str, prompt("honorific", "Honorific", "action", required=("action",))]
    confirm_outside: Annotated[str, prompt("honorific", "Honorific", "action", required=("action",))]
    job_request: Annotated[str, prompt("name", "prompt", "when", required=("prompt",))]
    watch_battery_low: Annotated[str, prompt("honorific", "Honorific", "value")]
    watch_battery_critical: Annotated[str, prompt("honorific", "Honorific", "value")]
    watch_cpu_hot: Annotated[str, prompt("honorific", "Honorific", "value")]
    watch_gpu_hot: Annotated[str, prompt("honorific", "Honorific", "value")]
    watch_disk_full: Annotated[str, prompt("honorific", "Honorific", "value")]
    watch_memory_full: Annotated[str, prompt("honorific", "Honorific", "value")]
    timer_done: Annotated[str, prompt("Honorific", "timer")]
    reminder_due: Annotated[str, prompt("Honorific", "text")]
    reminder_late: Annotated[str, prompt("Honorific", "time", "text")]
    voice_preview: Annotated[str, prompt("honorific")]
    code_note: Annotated[str, prompt()]

    @field_validator("*", mode="after")
    @classmethod
    def _placeholders(cls, value: str, info: ValidationInfo) -> str:
        """Check a prompt's placeholders against its spec (see `check_placeholders`).

        Args:
            value: the prompt text.
            info: which field it is.

        Returns:
            The text, unchanged.

        Raises:
            ValueError: a placeholder its code could not fill.
        """
        name = info.field_name
        spec = _meta_of(cls.model_fields[name]).prompt if name else None
        if spec is not None:
            check_placeholders(value, spec)
        return value


class Config(BaseModel):
    """All settings: one Config per process, changed in place so whoever holds a section sees every change."""

    model_config = ConfigDict(extra="forbid", strict=True)

    user: UserConfig
    brain: BrainConfig
    claude: ClaudeConfig
    ollama: OllamaConfig
    openrouter: OpenRouterConfig
    voice: VoiceConfig
    instant: InstantConfig
    tts: TTSConfig
    stt: STTConfig
    memory: MemoryConfig
    backup: BackupConfig
    tools: ToolsConfig
    jobs: JobsConfig
    watchers: WatchersConfig
    calendar: CalendarConfig
    documents: DocumentsConfig
    eyes: EyesConfig
    safety: SafetyConfig
    hud: HudConfig
    server: ServerConfig
    skills: SkillsConfig
    plugins: PluginsConfig
    retrieval: RetrievalConfig
    wake: WakeConfig
    phrases: PhrasesConfig
    reminders: RemindersConfig
    weather: WeatherConfig
    web: WebConfig
    script: ScriptConfig
    bench: BenchConfig
    system: SystemConfig
    mcp: McpConfig
    gestures: GesturesConfig
    performance: PerformanceConfig
    models: ModelsConfig
    logging: LoggingConfig
    prompts: PromptsConfig
    secrets: SecretsConfig
    activity: ActivityConfig
    data_dir: Annotated[str, Field(min_length=1), restart()]
    mcp_servers: Annotated[dict[str, dict[str, Any]],
                           everyday(Apply.MCP, hud=Hud("abilities", "Abilities"), ai=AI.NONE)]

    @property
    def data_path(self) -> Path:
        """Where his data lives: data_dir, a relative one counted from the repository folder (never created here)."""
        path = Path(self.data_dir).expanduser()
        return path if path.is_absolute() else ROOT / path

    @property
    def script_path(self) -> Path:
        """The scripted brain's file: script.path, a relative one counted from the repository folder."""
        path = Path(self.script.path).expanduser()
        return path if path.is_absolute() else ROOT / path

    @property
    def models_dir(self) -> Path:
        """The models folder: <data_path>/models."""
        return self.data_path / "models"

    @property
    def vault_path(self) -> Path:
        """The memory folder: <data_path>/memory."""
        return self.data_path / "memory"

    @property
    def secrets_path(self) -> Path:
        """The vault's folder: ``secrets.folder``, a relative one counted from the data folder."""
        path = Path(self.secrets.folder).expanduser()
        return path if path.is_absolute() else self.data_path / path

    @property
    def skills_path(self) -> Path:
        """The skills folder: <data_path>/skills."""
        return self.data_path / "skills"

    @property
    def plugins_path(self) -> Path:
        """The plugins folder: <data_path>/plugins (each installed plugin's folder, and the cache of what was fetched)."""
        return self.data_path / "plugins"

    @property
    def logs_path(self) -> Path:
        """The logs folder: <data_path>/logs."""
        return self.data_path / "logs"


# ---------------------------------------------------------------------------------------------------- keys and policy
def _meta_of(info: FieldInfo) -> Meta:
    """The Meta in a field's metadata, or the default Meta() when it has none."""
    return next((item for item in info.metadata if isinstance(item, Meta)), Meta())


def _section_model(name: str) -> type[Section] | None:
    """The model of a section, None for a top-level option or an unknown name."""
    info = Config.model_fields.get(name)
    kind = info.annotation if info is not None else None
    return kind if isinstance(kind, type) and issubclass(kind, Section) else None


def _field(key: str) -> FieldInfo:
    """The pydantic field of a flat key.

    Raises:
        KeyError: no such setting.
    """
    section, _, option = key.partition(".")
    if not option:
        if section not in TOP_LEVEL:
            raise KeyError(key)
        return Config.model_fields[section]
    model = _section_model(section)
    if model is None or option not in model.model_fields:
        raise KeyError(key)
    return model.model_fields[option]


@cache
def keys() -> tuple[str, ...]:
    """Every flat key, in the template's order ("user.name", ..., "data_dir", "mcp_servers")."""
    out: list[str] = []
    for name in Config.model_fields:
        model = _section_model(name)
        out += [f"{name}.{option}" for option in model.model_fields] if model else [name]
    return tuple(out)


@cache
def meta(key: str) -> Meta:
    """The policy of a key.

    Args:
        key: a flat key, e.g. "voice.end_silence_ms".

    Returns:
        Its Meta (Meta() for a plain file-only key).

    Raises:
        KeyError: no such setting.
    """
    return _meta_of(_field(key))


@cache
def ai_writable() -> frozenset[str]:
    """The keys Orion's settings_set may change: the allow-list (pinned by a test)."""
    return frozenset(key for key in keys() if meta(key).ai is AI.WRITE)


def _ai_reads(key: str) -> bool:
    """Whether Orion's settings_get may show a key: one the AI may read or write that is no secret."""
    policy = meta(key)
    return policy.ai is not AI.NONE and not policy.secret


@cache
def ai_sections() -> tuple[str, ...]:
    """The sections Orion's settings_get may show (those with a key it may show), sorted."""
    return tuple(sorted({key.partition(".")[0] for key in keys() if _ai_reads(key)}))


def value_of(cfg: Config, key: str) -> Any:
    """The current value of a flat key in a Config."""
    section, _, option = key.partition(".")
    return getattr(getattr(cfg, section), option) if option else getattr(cfg, section)


def _set_value(cfg: Config, key: str, value: Any) -> None:
    """Change one flat key in place (the section object keeps its identity)."""
    section, _, option = key.partition(".")
    if option:
        setattr(getattr(cfg, section), option, value)
    else:
        setattr(cfg, section, value)


def flat_values(cfg: Config) -> Flat:
    """Every flat key with its value, in template order."""
    return {key: value_of(cfg, key) for key in keys()}


# Rules between two keys. They are not model validators: a model-level error has no field location, so lenient
# loading could not give the one key involved its factory value.
CROSS_RULES: Final[tuple[tuple[str, Callable[[Config], bool], str], ...]] = (
    ("server.tls_key", lambda c: bool(c.server.tls_cert) == bool(c.server.tls_key),
     "goes with server.tls_cert: both, or neither"),
    ("claude.tool_timeout_s", lambda c: c.claude.tool_timeout_s >= c.system.command_max_timeout_s,
     "must be at least system.command_max_timeout_s"),
    ("system.command_timeout_s", lambda c: c.system.command_timeout_s <= c.system.command_max_timeout_s,
     "must be at most system.command_max_timeout_s"),
    ("voice.end_silence_short_ms", lambda c: c.voice.end_silence_short_ms <= c.voice.end_silence_ms,
     "must be at most voice.end_silence_ms"),
    ("voice.end_silence_long_ms", lambda c: c.voice.end_silence_long_ms >= c.voice.end_silence_ms,
     "must be at least voice.end_silence_ms"),
    ("voice.vad_silence_threshold", lambda c: c.voice.vad_silence_threshold < c.voice.vad_threshold,
     "must be below voice.vad_threshold"),
    ("memory.similar_fact_similarity", lambda c: c.memory.similar_fact_similarity < c.memory.same_fact_similarity,
     "must be below memory.same_fact_similarity"),
    ("retrieval.recall_solo_cosine", lambda c: c.retrieval.recall_solo_cosine >= c.retrieval.recall_min_cosine,
     "must be at least retrieval.recall_min_cosine"),
    ("wake.check_window_s", lambda c: c.wake.check_window_s >= c.wake.check_every_s,
     "must be at least wake.check_every_s"),
    ("documents.chunk_overlap", lambda c: 2 * c.documents.chunk_overlap <= c.documents.chunk_chars,
     "must be at most half of documents.chunk_chars"),
)


def cross_check(cfg: Config) -> list[tuple[str, str]]:
    """The rules between keys that `cfg` breaks.

    Returns:
        (key, why) for each broken rule, in rule order; empty when all hold.
    """
    return [(key, why) for key, holds, why in CROSS_RULES if not holds(cfg)]


# ---------------------------------------------------------------------------------------------------- the template
@cache
def template_text() -> str:
    """The template file's text, read once."""
    return TEMPLATE_PATH.read_text(encoding="utf-8")


@cache
def template_values() -> dict[str, Any]:
    """The template's values without `version` (treat as read-only; callers copy before changing).

    Raises:
        RuntimeError: the template's version is not VERSION (a packaging mistake).
    """
    data = yaml.load(template_text(), Loader=_LOADER)
    version = data.pop("version", None)
    if version != VERSION:
        raise RuntimeError(f"{TEMPLATE_PATH.name} has layout {version}, this code expects {VERSION}")
    return data


@cache
def _default() -> Config:
    """The factory settings, validated once (shared: never change it)."""
    return Config.model_validate(template_values(), strict=True)


def default_config() -> Config:
    """A fresh, changeable copy of the factory settings."""
    return _default().model_copy(deep=True)


@cache
def template_help() -> dict[str, str]:
    """Each flat key's explanation: its end-of-line comment in the template.

    Returns:
        flat key -> comment text; block-scalar lines and comment-only lines are skipped.
    """
    helps: dict[str, str] = {}
    section = ""
    for line in template_text().splitlines():
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)
        if not stripped or stripped.startswith("#") or indent >= 4 or ":" not in stripped:
            continue
        body, _, comment = line.partition(_COMMENT)
        name = body.strip().split(":", 1)[0]
        if indent == 0:
            section = "" if name in TOP_LEVEL or name == "version" else name
            if name in TOP_LEVEL:
                helps[name] = comment.strip()
        elif indent == 2 and section:
            helps[f"{section}.{name}"] = comment.strip()
    return helps


# ---------------------------------------------------------------------------------------------------- problems
type ProblemKind = Literal["syntax", "unreadable", "newer", "unknown", "invalid", "updated_default", "external"]


@dataclass(frozen=True, slots=True)
class Problem:
    """Something about config.yaml the user should know, shown as a HUD notice and logged.

    Attributes:
        kind: what kind of problem.
        key: the flat key it is about, "" for the whole file.
        message: what to tell the user.
    """

    kind: ProblemKind
    key: str
    message: str


@dataclass(frozen=True, slots=True)
class Reload:
    """The result of reading config.yaml again after it changed.

    Attributes:
        changed: the keys whose value changed, in template order.
        problems: what is wrong with the file now (with an "external" notice listing the changed keys).
    """

    changed: list[str]
    problems: list[Problem]


def parse_yaml(text: str) -> dict[str, Any]:
    """The settings in a config.yaml text.

    Args:
        text: the file's text.

    Returns:
        Its mapping; {} for an empty file.

    Raises:
        ValueError: not valid YAML, or not a mapping: "line L, column C: what is wrong".
    """
    try:
        data = yaml.load(text, Loader=_LOADER)
    except yaml.MarkedYAMLError as e:
        mark = e.problem_mark or e.context_mark
        where = f"line {mark.line + 1}, column {mark.column + 1}: " if mark else ""
        raise ValueError(f"{where}{e.problem or e.context or 'invalid YAML'}") from None
    except yaml.YAMLError as e:
        raise ValueError(str(e)) from None
    if data is None:
        return {}
    if isinstance(data, dict):
        return data
    raise ValueError("line 1, column 1: the file must be a mapping of sections")


# ---------------------------------------------------------------------------------------------------- merging
def _put_dotted(raw: dict[str, Any], key: str, value: Any) -> None:
    """Set a dotted key in a raw two-level mapping, creating its section."""
    section, _, option = key.partition(".")
    if not option:
        raw[section] = value
        return
    holder = raw.setdefault(section, {})
    if isinstance(holder, dict):
        holder[option] = value


def merge(template: Mapping[str, Any], raw: Mapping[str, Any]) -> tuple[dict[str, Any], list[Problem]]:
    """The template's values with the user's laid over them, two levels deep.

    User options win; lists and mappings are replaced whole. Unknown sections and options, a section that is not a
    mapping and MCP servers that are not mappings are left out, each with a problem.

    Args:
        template: the factory values.
        raw: the user's values.

    Returns:
        The merged values and the problems found.
    """
    merged = copy.deepcopy(dict(template))
    problems: list[Problem] = []
    for name, value in raw.items():
        if name == "mcp_servers":
            merged[name] = _clean_servers(value, problems)
        elif name in TOP_LEVEL:
            merged[name] = value
        elif not isinstance(merged.get(name), dict):
            problems.append(Problem("unknown", str(name), f"there is no {name} section (any more): ignored"))
        elif not isinstance(value, dict):
            problems.append(Problem("invalid", str(name), f"{name} must be a section of options: factory values used"))
        else:
            for option, item in value.items():
                if option in merged[name]:
                    merged[name][option] = item
                else:
                    key = f"{name}.{option}"
                    problems.append(Problem("unknown", key, f"there is no {key} option (any more): ignored"))
    return merged, problems


def _clean_servers(value: Any, problems: list[Problem]) -> dict[str, dict[str, Any]]:
    """The MCP servers that are mappings; anything else is dropped with a problem, so one typo costs one server."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        problems.append(Problem("invalid", "mcp_servers", "mcp_servers must map server names to their settings"))
        return {}
    servers: dict[str, dict[str, Any]] = {}
    for name, settings in value.items():
        if isinstance(settings, dict):
            servers[str(name)] = settings
        else:
            problems.append(Problem("invalid", "mcp_servers", f"MCP server {name!r} has no settings: left out"))
    return servers


_BOUNDS: Final = {"greater_than_equal": "ge", "greater_than": "gt", "less_than_equal": "le", "less_than": "lt"}


def _error_key(loc: tuple[int | str, ...]) -> str:
    """The flat key a validation error is about ("" for the whole config)."""
    if not loc:
        return ""
    head = str(loc[0])
    if head in TOP_LEVEL or len(loc) < 2:
        return head
    return f"{head}.{loc[1]}"


def validate_lenient(merged: dict[str, Any]) -> tuple[Config, dict[str, Any], list[Problem]]:
    """Validate merged settings so that one bad value never costs the others.

    A number outside its range is clamped to the bound; any other bad value takes its factory value, and the raw
    value is kept so the next save writes the user's text back unchanged (a stray brace in a custom persona is never
    lost). A broken rule between two keys gives the named key its factory value.

    Args:
        merged: the template's values with the user's laid over them (changed in place).

    Returns:
        The Config, the raw values that were replaced (flat key -> raw value), and the problems.
    """
    template = template_values()
    invalid: dict[str, Any] = {}
    problems: list[Problem] = []
    for attempt in range(3):
        try:
            cfg = Config.model_validate(merged, strict=True)
        except ValidationError as e:
            if attempt == 2:
                raise  # the template is valid (guard test), so a third failure is a bug
            handled: set[str] = set()
            for err in e.errors():
                key = _error_key(err["loc"])
                if not key or key in handled:
                    continue
                handled.add(key)
                section, _, option = key.partition(".")
                raw_value = merged[section][option] if option else merged.get(section)
                bound = next((v for k, v in (err.get("ctx") or {}).items() if k in _BOUNDS.values()), None)
                numeric = isinstance(raw_value, int | float) and not isinstance(raw_value, bool)
                if err["type"] in _BOUNDS and numeric and len(err["loc"]) == (2 if option else 1):
                    _put_dotted(merged, key, bound)
                    problems.append(Problem("invalid", key, f"{key} was {raw_value}: clamped to {bound}"))
                    continue
                invalid[key] = raw_value
                factory = template[section][option] if option else template[section]
                _put_dotted(merged, key, copy.deepcopy(factory))
                problems.append(Problem("invalid", key,
                                        f"{key}: {err['msg']}; the factory value is used until you fix it"))
            continue
        broken = cross_check(cfg)
        if not broken:
            return cfg, invalid, problems
        for key, why in broken:
            section, _, option = key.partition(".")
            _put_dotted(merged, key, copy.deepcopy(template[section][option]))
            problems.append(Problem("invalid", key, f"{key} {why}: the factory value is used"))
    return Config.model_validate(merged, strict=True), invalid, problems


def fingerprint(value: Any) -> str:
    """A short, stable digest of a validated value (15 and 15.0 differ only where the field type does)."""
    canonical = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.blake2b(canonical.encode(), digest_size=8).hexdigest()


def factory_fingerprints() -> dict[str, str]:
    """Each key's factory-value fingerprint: the baseline saved beside config.yaml."""
    return {key: fingerprint(value) for key, value in flat_values(_default()).items()}


def read_baseline(path: Path) -> dict[str, str]:
    """The fingerprints of the factory values a config.yaml was last written from.

    Args:
        path: the config.yaml.

    Returns:
        The baseline of the current layout; {} when there is none to trust.
    """
    try:
        data = json.loads(path.with_name(BASE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if isinstance(data, dict) and data.get("version") == VERSION and isinstance(data.get("keys"), dict):
        return {str(k): str(v) for k, v in data["keys"].items()}
    return {}


def adopt_defaults(cfg: Config, base: Mapping[str, str], invalid: Mapping[str, Any]) -> list[str]:
    """Give options the user never changed the factory's improved value (decision 4), in place.

    An option is taken when its value still fingerprints as the baseline (never changed) and the factory value no
    longer does (the factory improved it).

    Args:
        cfg: the user's validated settings.
        base: the baseline fingerprints.
        invalid: keys whose raw value was unusable: left alone.

    Returns:
        The keys that took the factory value, in template order.
    """
    factory = _default()
    adopted: list[str] = []
    for key in keys():
        if key in invalid or key not in base:
            continue
        mine, new = value_of(cfg, key), value_of(factory, key)
        if fingerprint(mine) == base[key] and fingerprint(new) != base[key]:
            _set_value(cfg, key, copy.deepcopy(new))
            adopted.append(key)
    return adopted


# ---------------------------------------------------------------------------------------------------- loading
def _stamp(path: Path) -> Stamp | None:
    """(mtime_ns, size, inode) of a file, None when it does not exist."""
    try:
        st = path.stat()
    except OSError:
        return None
    return file_stamp(st)


@dataclass(slots=True)
class _Loaded:
    """What one read of config.yaml produced."""

    cfg: Config
    invalid: dict[str, Any]
    problems: list[Problem]
    broken: Problem | None
    newer: bool
    text: str | None
    stamp: Stamp | None
    rewrite: bool  # unknown options dropped or defaults adopted: back the old file up before writing


def _load(path: Path, *, adopt: bool) -> _Loaded:
    """Read config.yaml and turn it into settings: read, merge, validate leniently, adopt new defaults.

    Args:
        path: the config.yaml (missing means factory settings).
        adopt: give untouched options improved factory values (the main process at startup).

    Returns:
        The result; a file that cannot be read or parsed gives factory settings and a `broken` problem.
    """
    stamp = _stamp(path)
    try:
        text: str | None = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = None
    except OSError as e:
        problem = Problem("unreadable", "", f"{path.name} can't be read ({e.strerror}): running on factory settings")
        return _Loaded(default_config(), {}, [problem], problem, False, None, stamp, False)
    try:
        raw = parse_yaml(text) if text is not None else {}
    except ValueError as e:
        problem = Problem("syntax", "", f"{path.name}, {e}. Running on factory settings until you fix it (the file is "
                                        "left as it is)")
        return _Loaded(default_config(), {}, [problem], problem, False, text, stamp, False)
    version = raw.pop("version", VERSION)  # none means this layout
    newer = isinstance(version, int) and version > VERSION
    problems = [Problem("newer", "", f"{path.name} was written by a newer ORION (layout {version}): it is read but "
                                     "never rewritten")] if newer else []
    merged, merge_problems = merge(template_values(), raw)
    cfg, invalid, check_problems = validate_lenient(merged)
    problems += merge_problems + check_problems
    adopted = adopt_defaults(cfg, read_baseline(path), invalid) if adopt and not newer else []
    problems += [Problem("updated_default", key, f"{key} follows the new factory value") for key in adopted]
    rewrite = bool(adopted or any(p.kind == "unknown" for p in merge_problems))
    return _Loaded(cfg, invalid, problems, None, newer, text, stamp, rewrite)


def load_config(path: Path | None = None) -> Config:
    """The settings in a config.yaml (CONFIG_PATH by default), read-only (the CLI, install.sh); it never writes."""
    return Settings.open(path, role="tools").cfg


# ---------------------------------------------------------------------------------------------------- saving
@cache
def _template_doc() -> "CommentedMap":
    """The template as ruamel's round-trip document, parsed once (copy it before changing it)."""
    return _yaml().load(template_text())


def _yaml() -> "YAML":
    """A ruamel round-trip YAML that keeps the template's comments, order and quotes."""
    from ruamel.yaml import YAML

    y = YAML()
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    y.representer.add_representer(type(None), lambda r, _: r.represent_scalar("tag:yaml.org,2002:null", "null"))
    return y


_RESOLVER: Final = yaml.resolver.Resolver()


def _yaml_safe(value: Any) -> Any:
    """A value ready for ruamel to write so that PyYAML reads it back exactly.

    Multi-line text becomes a literal block; text PyYAML would read as something else ("no", "on", "12:30"), the
    empty string and text with outer spaces get double quotes; lists and mappings are converted item by item.
    """
    from ruamel.yaml.scalarstring import DoubleQuotedScalarString, LiteralScalarString

    if isinstance(value, str):
        if "\n" in value:
            return LiteralScalarString(value)
        plain = _RESOLVER.resolve(yaml.ScalarNode, value, (True, False)) == "tag:yaml.org,2002:str"
        if not value or value != value.strip() or not plain:
            return DoubleQuotedScalarString(value)
        return value
    if isinstance(value, list):
        return [_yaml_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _yaml_safe(v) for k, v in value.items()}
    return value


def _template_body() -> str:
    """The template without its header paragraph."""
    return template_text().split("\n\n", 1)[1]


def render(cfg: Config, invalid: Mapping[str, Any] | None = None) -> str:
    """The text of data/config.yaml for these settings: the template, comments and all, with these values.

    Args:
        cfg: the settings.
        invalid: unusable raw values to write back unchanged (the user's text survives until they fix it).

    Returns:
        The file's text.
    """
    invalid = dict(invalid or {})
    if not invalid and cfg == _default():
        return USER_HEADER + "\n" + _template_body()
    factory = flat_values(_default())
    values = flat_values(cfg) | invalid
    doc = copy.deepcopy(_template_doc())
    doc["version"] = VERSION
    for key, value in values.items():
        if key not in invalid and value == factory[key]:
            continue  # untouched: the template's own text, quotes and comment stay
        section, _, option = key.partition(".")
        holder, name = (doc[section], option) if option else (doc, section)
        if key == "mcp_servers" and isinstance(value, dict):
            node = holder[name]
            node.clear()
            node.update(_yaml_safe(value))
            if value:
                node.fa.set_block_style()
        elif isinstance(value, list) and isinstance(holder[name], list):
            holder[name][:] = _yaml_safe(value)
        else:
            plain = int(value) if isinstance(value, float) and value.is_integer() else value
            holder[name] = _yaml_safe(plain)
    buffer = io.StringIO()
    _yaml().dump(doc, buffer)
    return USER_HEADER + "\n" + buffer.getvalue().split("\n\n", 1)[1]


@cache
def _baseline_text() -> str:
    """The baseline file's text: the layout and the factory fingerprints the written config.yaml reflects."""
    return json.dumps({"version": VERSION, "keys": factory_fingerprints()}, indent=0, sort_keys=True) + "\n"


def _write_baseline(path: Path) -> None:
    """Write the baseline beside a config.yaml unless it already holds these factory fingerprints."""
    base = path.with_name(BASE_NAME)
    if not base.is_file() or base.read_text(encoding="utf-8") != _baseline_text():
        write_atomic(base, _baseline_text(), private=True)


# ---------------------------------------------------------------------------------------------------- changes
class Refused(ValueError):
    """A settings change Orion won't make; the message is the plain reason, naming the key."""


def assign(cfg: Config, new: Config) -> list[str]:
    """Copy every changed value of `new` into `cfg`, keeping each section object (its holders see the change).

    Attribute writes are atomic in Python: a thread reading a key sees the old or the new value, never a torn one.

    Args:
        cfg: the live settings.
        new: the settings to adopt.

    Returns:
        The keys that changed, in template order.
    """
    changed: list[str] = []
    for key in keys():
        value = value_of(new, key)
        if value_of(cfg, key) != value:
            _set_value(cfg, key, value)
            changed.append(key)
    return changed


def normalize_patch(patch: Mapping[str, Any]) -> Flat:
    """A settings change as flat keys: {"tts": {"speed": 1.2}} and {"tts.speed": 1.2} both give {"tts.speed": 1.2}.

    Raises:
        Refused: it is not a mapping of sections or flat keys.
    """
    if not isinstance(patch, Mapping):
        raise Refused("Settings are sent as sections of options.")
    flat: Flat = {}
    for name, value in patch.items():
        if not isinstance(name, str):
            raise Refused("Settings are sent as sections of options.")
        if name in TOP_LEVEL or "." in name:
            flat[name] = value
        elif isinstance(value, Mapping):
            flat.update({f"{name}.{option}": item for option, item in value.items()})
        else:
            raise Refused(f"{name} is a section: send its options, e.g. {name}.<option>.")
    return flat


def _explain(e: ValidationError, *within: str) -> str:
    """One plain sentence for the first problem in a validation error, naming the key (inside `within`, if given)."""
    err = e.errors()[0]
    key = ".".join(map(str, (*within, *err["loc"]))) if within else _error_key(err["loc"]) or "the settings"
    if err["type"] == "literal_error":
        return f"{key} can't be {err['input']!r}; choose one of: {err['ctx']['expected']}."
    message = err["msg"]
    return f"Invalid value for {key}: {message[:1].lower()}{message[1:]}."


def _apply(cfg: Config, flat: Mapping[str, Any]) -> Config:
    """A new Config: `cfg` with these changes, validated strictly with the rules between keys.

    Raises:
        Refused: a value Orion can't use, or a broken rule, naming the key.
    """
    data = cfg.model_dump()
    for key, value in flat.items():
        _put_dotted(data, key, value)
    try:
        new = Config.model_validate(data, strict=True)
    except ValidationError as e:
        raise Refused(_explain(e)) from None
    if broken := cross_check(new):
        key, why = broken[0]
        raise Refused(f"{key} {why}.")
    return new


class _Server(TypedDict, total=False):
    """The settings of an MCP server that Orion reads himself, checked in a patch (the others are the server's own)."""

    enabled: bool
    intents: bool
    lend: bool
    sandbox: bool
    disabled_tools: list[str]
    timeout: Annotated[float, Field(gt=0)] | None
    gestures: dict[str, Any] | None
    plugin: str
    values: dict[str, Any]
    oauth: dict[str, Any]


_SERVERS: Final = TypeAdapter(dict[str, _Server])


def validate_patch(cfg: Config, patch: Mapping[str, Any], *, actor: Actor,
                   choices: Mapping[str, Collection[Any]] | None = None, own: bool = False) -> Flat:
    """Refuse a settings change Orion can't work with, or that its actor may not make, before anything is saved.

    The only place the HUD's and the AI's rights are enforced: the HUD may change the keys on its pages, the AI the
    allow-list (any actor that is not "hud" counts as the AI). Types are strict: the HUD sends JSON, so "8192" for
    a number is a mistake, not a number.

    Args:
        cfg: the live settings.
        patch: the change, as sections of options or flat keys.
        actor: who asks.
        choices: allowed values of some keys right now (installed voices, the STT catalogue; for the AI also the
            installed Ollama models, so a prompt injection can't start a download).
        own: a change Orion's own code makes for the user's action on a page of its own (the integrations catalog
            adding a calendar source): checked like any other, without the key rights of the HUD's settings pages.

    Returns:
        The keys whose value really changes, with their validated values.

    Raises:
        Refused: why it is refused, naming the key.
    """
    flat = normalize_patch(patch)
    known = set(keys())
    for key in flat:
        if key not in known:
            raise Refused(f"There is no setting {key}.")
        policy = meta(key)
        if own:
            continue
        if actor == "hud":
            if policy.hud is None:
                raise Refused(f"{key} is changed in data/config.yaml.")
        elif policy.ai is not AI.WRITE:
            where = "in Settings" if policy.hud is not None else "in data/config.yaml"
            raise Refused(f"Only the user can change {key} ({where}).")
    for key, allowed in (choices or {}).items():
        if key in flat and flat[key] is not None and flat[key] not in allowed:
            raise Refused(f"{key} can't be {flat[key]!r}; choose one of: {', '.join(map(str, allowed))}.")
    if "mcp_servers" in flat:
        try:
            _SERVERS.validate_python(flat["mcp_servers"], strict=True)
        except ValidationError as e:
            raise Refused(_explain(e, "mcp_servers")) from None
    new = _apply(cfg, flat)
    return {key: value_of(new, key) for key in flat if value_of(new, key) != value_of(cfg, key)}


# ---------------------------------------------------------------------------------------------------- views
def public_view(cfg: Config) -> dict[str, Any]:
    """The settings as the HUD sees them: no prompts (the HUD never edits them), secrets as ""."""
    data = cfg.model_dump(exclude={"prompts"})
    for key in keys():
        if meta(key).secret:
            _put_dotted(data, key, "")
    return data


def secret_sources(cfg: Config) -> dict[str, Literal["config", "env", ""]]:
    """Where each secret comes from, without its value: "config", "env" or "" (none)."""
    openrouter = cfg.openrouter
    return {"openrouter.api_key": "config" if openrouter.config_key else "env" if openrouter.key else ""}


def ai_view(cfg: Config, section: str = "") -> dict[str, Any]:
    """The settings Orion's settings_get shows: only keys the AI may read or write, never secrets or prompts.

    Args:
        cfg: the settings.
        section: one section, or "" for all.

    Returns:
        section -> option -> value.

    Raises:
        ValueError: `section` has nothing the AI may read.
    """
    out: dict[str, Any] = {}
    for key in keys():
        name, _, option = key.partition(".")
        if not _ai_reads(key) or (section and name != section):
            continue
        if option:
            out.setdefault(name, {})[option] = value_of(cfg, key)
        else:
            out[name] = value_of(cfg, key)
    if section and section not in out:
        raise ValueError(f"There's no {section!r} section Orion may read; choose one of: "
                         f"{', '.join(ai_sections())}.")
    return out


class Choice(TypedDict):
    """One choice of a select control."""

    value: str | int
    label: str


class FieldSpec(TypedDict, total=False):
    """How the HUD renders one key (the web app's FieldSpec)."""

    kind: Required[Literal["bool", "int", "float", "text", "secret", "choice", "list", "device"]]
    label: Required[str]
    help: Required[str]
    min: float
    max: float
    step: float
    unit: str
    choices: list[Choice]
    restart: Required[bool]
    default: Required[Any]


def _bounds(info: FieldInfo) -> tuple[float | None, float | None]:
    """A field's inclusive range from its constraints (a list's from its items'); None where there is none."""
    sources: list[Any] = list(info.metadata)
    if get_origin(info.annotation) is list:
        for item in get_args(info.annotation):
            for extra in getattr(item, "__metadata__", ()):
                sources += extra.metadata if isinstance(extra, FieldInfo) else [extra]
    lo = next((float(s.ge if isinstance(s, Ge) else s.gt) for s in sources if isinstance(s, Ge | Gt)), None)
    hi = next((float(s.le if isinstance(s, Le) else s.lt) for s in sources if isinstance(s, Le | Lt)), None)
    return lo, hi


def _kind(key: str, hud: Hud, info: FieldInfo) -> Literal["bool", "int", "float", "text", "secret", "choice", "list",
                                                         "device"]:
    """The FieldSpec kind of a HUD key, from its control and annotation."""
    match hud.control:
        case "switch":
            return "bool"
        case "slider" | "number":
            return "int" if info.annotation is int else "float"
        case "select":
            return "device" if hud.choices_from.startswith("devices.") else "choice"
        case "text" | "secret" | "list":
            return hud.control
    raise ValueError(f"{key}: the {hud.control!r} control has no field kind")


def _static_choices(info: FieldInfo, hud: Hud) -> list[Choice] | None:
    """A select's fixed choices (Hud.options, or a Literal's values); [] for dynamic ones; None for no select."""
    if hud.options:
        return [{"value": option, "label": str(option)} for option in hud.options]
    if get_origin(info.annotation) is Literal:
        return [{"value": value, "label": str(value)} for value in get_args(info.annotation)]
    if hud.choices_from:
        return []  # filled by the server for each request (voices, devices, models)
    return None


@cache
def _hud_schema() -> dict[str, FieldSpec]:
    """The schema of every key on the Settings pages, built once (see `hud_schema`)."""
    factory = _default()
    helps = template_help()
    schema: dict[str, FieldSpec] = {}
    for key in keys():
        policy = meta(key)
        hud = policy.hud
        if hud is None or hud.control == "abilities":
            continue  # the Abilities page has its own API
        info = _field(key)
        spec: FieldSpec = {"kind": _kind(key, hud, info),
                           "label": hud.label or key.rsplit(".", 1)[-1].replace("_", " ").capitalize(),
                           "help": helps.get(key, ""), "restart": Apply.RESTART in policy.apply,
                           "default": value_of(factory, key)}
        lo, hi = _bounds(info)
        if lo is not None:
            spec["min"] = lo
        if hi is not None:
            spec["max"] = hi
        if hud.step is not None:
            spec["step"] = hud.step
        if hud.unit:
            spec["unit"] = hud.unit
        if (choices := _static_choices(info, hud)) is not None:
            spec["choices"] = choices
        schema[key] = spec
    return schema


def hud_schema() -> dict[str, FieldSpec]:
    """How the HUD renders each key of its Settings pages (GET /api/settings), in template order.

    Returns:
        A fresh copy keyed by flat key; selects whose choices are dynamic have an empty `choices` list that the
        server fills (voices, audio devices, models) before sending it.
    """
    return copy.deepcopy(_hud_schema())


UNIT_WORDS: Final[Mapping[str, str]] = {"s": "s", "ms": "ms", "min": "min", "h": "h", "days": "days", "mb": "MB",
                                        "gb": "GB", "pct": "%"}  # a name's last word that is a unit


def option_label(name: str) -> str:
    """An option's name in words, its unit after it: "cache_s" -> "Cache (s)", "keep_days" -> "Keep (days)".

    Args:
        name: the last part of its flat key.
    """
    words = [word for word in name.split("_") if word]
    unit = UNIT_WORDS.get(words[-1]) if len(words) > 1 else None
    text = " ".join(words[:-1] if unit else words).capitalize()
    return f"{text} ({unit})" if unit else text


@dataclass(frozen=True, slots=True)
class Option:
    """One simple option of a section, as a connector's page edits it.

    Attributes:
        key: its flat key ("watchers.battery_low").
        kind: "switch", "number", "select", "text" or "list".
        label: for people (the HUD's label for it, else its name in words; the unit after it).
        help: its explanation in the template ("" when it has none).
        options: a select's choices, or what a list may hold when it holds a fixed set.
    """

    key: str
    kind: Literal["switch", "number", "select", "text", "list"]
    label: str
    help: str
    options: tuple[str, ...] = ()


def _simple(info: FieldInfo) -> tuple[Literal["switch", "number", "select", "text", "list"], tuple[str, ...]] | None:
    """How a field is edited as a form control: (kind, choices); None for what a form can't hold (mappings,
    unions, records)."""

    def plain(annotation: Any) -> Any:
        """A type without its Annotated wrapper."""
        return get_args(annotation)[0] if get_origin(annotation) is Annotated else annotation

    kind = plain(info.annotation)
    if kind is bool:
        return "switch", ()
    if kind in (int, float):
        return "number", ()
    if kind is str:
        return "text", ()
    if get_origin(kind) is Literal:
        return "select", tuple(str(value) for value in get_args(kind))
    if get_origin(kind) is list:
        item = plain(get_args(kind)[0])
        if item in (str, int, float):
            return "list", ()
        if get_origin(item) is Literal:
            return "list", tuple(str(value) for value in get_args(item))
    return None


def options_of(sections: Collection[str], *, skip: Collection[str] = ()) -> list[Option]:
    """The options of some sections that a form can edit, in template order: no secret, no prompt, no mapping.

    Args:
        sections: the sections ("web", "weather").
        skip: flat keys to leave out (an option with a page of its own).

    Returns:
        The options.
    """
    helps = template_help()
    found: list[Option] = []
    for key in keys():
        section = key.partition(".")[0]
        policy = meta(key)
        if section not in sections or key in skip or policy.secret or policy.prompt is not None:
            continue
        spec = _simple(_field(key))
        if spec is None:
            continue
        hud = policy.hud
        label = hud.label if hud is not None and hud.label else option_label(key.rpartition(".")[2])
        found.append(Option(key, spec[0], f"{label} ({hud.unit})" if hud is not None and hud.unit else label,
                            helps.get(key, ""), spec[1]))
    return found


# ---------------------------------------------------------------------------------------------------- Settings
class Settings:
    """The live settings of one process: the Config every module holds, the file behind it, and its problems."""

    __slots__ = ("_lock", "_newer", "_rewrite", "_stamp", "_text", "broken", "cfg", "invalid", "path", "problems",
                 "role", "started_with")

    def __init__(self, path: Path, role: Role, loaded: _Loaded) -> None:
        """Hold what a load produced (use `Settings.open`).

        Args:
            path: the config.yaml.
            role: "main" may write it; "tools" never does.
            loaded: the result of reading it.
        """
        self.path = path
        self.role = role
        self.cfg = loaded.cfg
        self.invalid = loaded.invalid
        self.problems = loaded.problems
        self.broken = loaded.broken
        self.started_with: Flat = {key: copy.deepcopy(value_of(self.cfg, key)) for key in keys()
                                   if Apply.RESTART in meta(key).apply}
        self._newer = loaded.newer
        self._rewrite = loaded.rewrite
        self._stamp = loaded.stamp
        self._text = loaded.text  # the text last read or written: a reload of it changes nothing
        self._lock = asyncio.Lock()
        for problem in self.problems:
            log.warning("config: %s", problem.message)

    @classmethod
    def open(cls, path: Path | None = None, *, role: Role = "main") -> Self:
        """Read the settings (a few milliseconds; it never writes: the main process calls `sync_file` after).

        Args:
            path: the config.yaml; None = CONFIG_PATH, looked up now (so tests and the tools process can point it
                elsewhere).
            role: "main" (Orion, which adopts improved factory values) or "tools" (the tools process, mcp-serve,
                the CLI), which only reads.

        Returns:
            The settings.
        """
        path = path or CONFIG_PATH
        return cls(path, role, _load(path, adopt=role == "main"))

    @property
    def writable(self) -> bool:
        """Whether this process may write config.yaml: the main role, and the file is neither broken nor newer."""
        return self.role == "main" and self.broken is None and not self._newer

    def sync_file(self) -> bool:
        """Write config.yaml when it does not list exactly these settings (first start, an update).

        Runs once at startup in a worker thread, before the tools process starts. After dropped options or adopted
        defaults, the old file is kept as config.yaml.bak first. The baseline is written too.

        Returns:
            True when the file was (re)written.
        """
        if not self.writable:
            return False
        text = render(self.cfg, self.invalid)
        if self._text is not None and text == self._text:
            _write_baseline(self.path)
            return False
        if self._text is not None and self._rewrite:
            write_atomic(self.path.with_name(self.path.name + BACKUP_SUFFIX), self._text, private=True)
        self._write(text)
        self._rewrite = False
        return True

    def validate(self, patch: Mapping[str, Any], *, actor: Actor,
                 choices: Mapping[str, Collection[Any]] | None = None, own: bool = False) -> Flat:
        """`validate_patch` on the live settings (see there)."""
        return validate_patch(self.cfg, patch, actor=actor, choices=choices, own=own)

    async def commit(self, changes: Flat) -> list[str]:
        """Save validated changes, then apply them in place.

        Saves never interleave. A hand edit the watcher has not processed yet is read first, so it is not
        overwritten. The file is rendered and written in a worker thread; only once it is on disk do the live
        settings change, so a failed write changes nothing.

        Args:
            changes: flat key -> value, as `validate` returned them.

        Returns:
            The keys that changed, in template order.

        Raises:
            Refused: the file is broken, newer or not this process's to write, or the changes are invalid.
            OSError: the file could not be written.
        """
        async with self._lock:
            if not self.writable:
                raise Refused(self._unwritable())
            if _stamp(self.path) != self._stamp:
                await self.reload()
                if not self.writable:
                    raise Refused(self._unwritable())
            new = _apply(self.cfg, changes)
            invalid = {key: raw for key, raw in self.invalid.items() if key not in changes}
            text = await asyncio.to_thread(render, new, invalid)
            await asyncio.to_thread(self._write, text)
            self.invalid = invalid
            return assign(self.cfg, new)

    async def reload(self) -> Reload:
        """Read config.yaml again after it changed (the watcher, in the main process).

        The text Orion last read or wrote (his own save, say) is ignored. A file that breaks keeps the current
        values; the problem is returned so the HUD can say which line.

        Returns:
            What changed, and the problems (an "external" one listing the changed keys).
        """
        loaded = await asyncio.to_thread(_load, self.path, adopt=False)
        if loaded.text == self._text:
            self._stamp = loaded.stamp
            return Reload([], [])
        return self._take(loaded, external=True)

    def refresh_if_changed(self) -> list[str]:
        """Re-read config.yaml if it changed since the last read (the tools process, before every call).

        One stat when nothing changed. A broken file keeps the last good values and is logged once.

        Returns:
            The keys that changed.
        """
        stamp = _stamp(self.path)
        if stamp == self._stamp:
            return []
        loaded = _load(self.path, adopt=False)
        if loaded.broken is not None:
            if self.broken is None:
                log.warning("config: %s (keeping the last good settings)", loaded.broken.message)
            self.broken = loaded.broken
            self._stamp = loaded.stamp
            return []
        return self._take(loaded, external=False).changed

    def restart_pending(self) -> list[str]:
        """The keys that need a restart and changed since this process started, in template order."""
        return [key for key, value in self.started_with.items() if value_of(self.cfg, key) != value]

    def _take(self, loaded: _Loaded, *, external: bool) -> Reload:
        """Adopt a fresh load in place (a broken one only records the problem) and say what changed."""
        if loaded.broken is not None:
            self.broken = loaded.broken
            self._stamp, self._text = loaded.stamp, loaded.text
            self.problems = [loaded.broken]
            return Reload([], [loaded.broken])
        self.broken = None
        self._newer = loaded.newer
        changed = assign(self.cfg, loaded.cfg)
        self.invalid = loaded.invalid
        self._stamp, self._text = loaded.stamp, loaded.text
        problems = list(loaded.problems)
        if external and changed:
            problems.append(Problem("external", "",
                                    f"{self.path.name} changed outside Orion: {', '.join(changed)}"))
        self.problems = problems
        return Reload(changed, problems)

    def _write(self, text: str) -> None:
        """Write config.yaml (and its baseline, when it differs) atomically and remember the text."""
        write_atomic(self.path, text, private=True)
        _write_baseline(self.path)
        self._text = text
        self._stamp = _stamp(self.path)

    def _unwritable(self) -> str:
        """Why config.yaml can't be written now, in words for the HUD."""
        if self.broken is not None:
            return f"{self.path.name} has a problem to fix first: {self.broken.message}"
        if self._newer:
            return f"{self.path.name} was written by a newer ORION: edit it by hand or update Orion."
        return "Only Orion himself writes his settings file."
