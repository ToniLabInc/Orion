"""The pure safety policy for everything the AI can reach: plain data in, decisions out, nothing run.

ORION's language model acts through tools, and tools act on his settings, his MCP servers, his memory and the PC.
This module decides, for the AI ("ai") and for the user at the HUD ("hud"):

- which settings the AI may write: the allow-list in config's metadata AND ``NEVER_AI`` (defence in depth, D5);
- that the AI switches tools and servers off, never back on (D12);
- which MCP-server changes the AI may make: Orion's own groups only switch off or restart, a server run by a
  command needs run_command switched on, a server's tokens stay where it points and are the user's alone to set, and
  nothing the AI supplies may read the environment (``$NAME`` or ``${NAME}``);
- how secrets are masked before settings are shown;
- provenance (D3): which sources reach the core memory, which tools taint a turn, and which provenance a call into
  the tools process may claim;
- which processes close_app may end;
- which of the model's calls the user is asked about first (``confirm_reason``);
- what each channel (voice, hud, discord, job, app) may never do and must always ask about (``channel_verdict``), and the
  actions that widen his reach (running outside the sandbox, installing a server), which need a yes every time and are
  impossible from Discord and jobs (``widening``).

Callers apply the decisions and pass a refusal's reason on to whoever asked. Only ``orion.base`` and the standard
library are imported.
"""

import re
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Final, Literal, cast

from orion.base import CORE_SERVER, JSON, TRUSTED, WIRE_PROVENANCE, Actor, Provenance, switched_off

type ServerAction = Literal["add", "update", "remove", "restart", "enable", "disable"]
type Risk = Literal["read", "change", "destroy"]
type Verdict = Literal["never", "confirm", ""]

CHANNELS: Final = ("voice", "hud", "discord", "job", "app")  # where a request can come from (safety.channels)
CLASSES: Final = ("@read", "@change", "@destroy", "@open_world")  # what a channel's rule may name besides tools

# What the AI never writes, whatever the allow-list says. "section." entries cover a whole section; the rest are
# single keys (data_dir, mcp_servers and version are bare keys: version drives the settings file's migrations).
NEVER_AI: Final[tuple[str, ...]] = (
    "prompts.", "server.", "models.", "logging.", "performance.", "mcp.", "system.", "web.", "weather.", "wake.",
    "phrases.", "retrieval.", "reminders.", "skills.",
    "ollama.host", "ollama.keep_alive", "ollama.flash_attention", "ollama.kv_cache_type", "ollama.num_parallel",
    "ollama.max_loaded_models", "ollama.start_timeout_s", "openrouter.api_key", "openrouter.base_url", "openrouter.daily_budget_usd",
    "voice.enabled", "voice.input_device", "voice.output_device", "voice.wake_model", "data_dir", "mcp_servers",
    "version", "tools.always", "claude.tool_timeout_s", "safety.", "calendar.", "secrets.", "activity.", "plugins.", "script.",
    "bench.", "backup.", "documents.", "instant.",
)
# Processes close_app never ends, matched by name (lower case, a version tail such as "3.12" ignored).
PROTECTED_PROCESSES: Final[frozenset[str]] = frozenset({
    # the desktop session and display
    "gnome-shell", "gnome-session", "gnome-session-binary", "gdm", "mutter", "kwin_wayland", "plasmashell",
    "xwayland", "xorg", "systemd", "dbus-daemon", "dbus-broker", "gnome-keyring-daemon",
    # sound
    "pipewire", "pipewire-pulse", "wireplumber",
    # shells and remote logins: ending one closes the terminal or session it serves
    "bash", "zsh", "sh", "fish", "sshd",
    # Orion himself and his models
    "orion", "python", "python3", "uv", "ollama",
})
# A reference to an environment variable: $NAME or ${NAME}.
ENV_REF: Final[re.Pattern[str]] = re.compile(r"\$(?:\w+|\{[^}]*\})")

_NEVER_SECTIONS: Final[frozenset[str]] = frozenset(e.removesuffix(".") for e in NEVER_AI if e.endswith("."))
_NEVER_KEYS: Final[frozenset[str]] = frozenset(e for e in NEVER_AI if not e.endswith("."))
_PLAIN_REF: Final[re.Pattern[str]] = re.compile(r"\$\{\w+\}")  # a value that is only "${NAME}" holds no secret
HIDDEN: Final = "(hidden)"  # a secret as it is shown
_CONNECTORS_PAGE: Final = "in the HUD, under Customize, Connectors"
_TOKEN_FIELDS: Final[tuple[str, ...]] = ("env", "headers")  # where a server's tokens are kept
_COMMAND_FIELDS: Final[frozenset[str]] = frozenset({"command", "args", "cwd"})  # what a command server runs
_TARGET_FIELDS: Final[frozenset[str]] = _COMMAND_FIELDS | {"url"}  # where a server's tokens would be sent
_VERSION_TAIL: Final = "0123456789."
_SELECTOR: Final = re.compile(r"@(?:read|change|destroy|open_world)|[A-Za-z0-9._-]+(?::\*)?")  # a tool, server:*, a class
# What widens his reach, so it needs the user's yes every time and never comes from Discord or a job: a tool, the
# argument that says so, its values, and why it asks ("outside_sandbox" has a question of its own). Changing his
# connectors and plugins is asked about too, but inside the tools themselves (``Controls.ask_first``), not here.
_WIDENING: Final[tuple[tuple[str, str, frozenset[object], str], ...]] = (
    ("run_command", "outside_sandbox", frozenset({True}), "outside_sandbox"),
)
UNATTENDED: Final = ("discord", "job")  # channels with no one at the screen: what widens his reach never comes from them


@dataclass(frozen=True, slots=True)
class Refusal:
    """A decision against a request, with the reason to pass on (the AI reads it as the tool's error).

    Attributes:
        reason: one or two plain sentences saying why, and who can do it instead.
    """

    reason: str


@dataclass(frozen=True, slots=True)
class ProcRow:
    """One running process, as close_app sees it.

    Attributes:
        pid: the process id.
        name: the process name.
        exe_name: the base name of its executable ("" when unknown).
        username: the user it runs as.
    """

    pid: int
    name: str
    exe_name: str
    username: str


def settings_refusal(keys: Iterable[str], actor: Actor, ai_writable: Callable[[str], bool]) -> Refusal | None:
    """Decide whether a settings change may go ahead.

    The user (``"hud"``) passes here; config validation applies the HUD's own limits. The AI, and any actor other
    than ``"hud"``, is refused a key when either the allow-list says no or the key matches ``NEVER_AI``, so a slip in
    config's metadata can never open prompts, hosts, keys, devices, paths or tuning to the AI.

    Args:
        keys: the flat keys the change touches, e.g. "tts.speed", "data_dir".
        actor: who asks.
        ai_writable: the allow-list from config's metadata (True for a key the AI may write).

    Returns:
        None when the change may go ahead, else a Refusal naming the first key refused.
    """
    if actor == "hud":
        return None
    for key in keys:
        if _never_ai(key) or not ai_writable(key):
            return Refusal(f"Only the user can change {key} (in Settings or data/config.yaml).")
    return None


def tool_switch_refusal(*, enabled: bool, actor: Actor, server_enabled: bool, tool_disabled: bool) -> Refusal | None:
    """Decide whether a tool may be switched on or off: the AI switches tools off, never back on (D12).

    Args:
        enabled: the state asked for (True: on).
        actor: who asks; any actor other than "hud" is treated as the AI.
        server_enabled: whether the tool's server is switched on now.
        tool_disabled: whether the tool itself is switched off now.

    Returns:
        None when the switch may go ahead (switching off, the user, or a tool that is already on), else a Refusal.
    """
    if actor == "hud" or not enabled or (server_enabled and not tool_disabled):
        return None
    return Refusal(f"Only the user can switch a tool back on, {_CONNECTORS_PAGE}.")


def server_change_refusal(*, action: ServerAction, actor: Actor, builtin: bool, old: Mapping[str, JSON] | None,
                          new: Mapping[str, JSON] | None, run_command_on: bool, name: str = "") -> Refusal | None:
    """Decide whether a change to an MCP server may go ahead.

    The user may change anything except restarting the in-process ``orion`` link. For the AI, and any actor other
    than "hud": restarting and switching off are always allowed; switching on never is (an ``enabled: false`` dropped
    or a name taken out of ``disabled_tools`` counts as switching on); Orion's own groups are never added, changed
    or removed; command, args and cwd change only while run_command is switched on; env and headers (the tokens) are
    never set or changed; a server holding tokens keeps its url, command, args and cwd; no value supplied may
    reference the environment; a server is never allowed to push intents (``intents: true``) or lent to other apps
    (``lend: true``), only stopped; what its gestures do (``gestures``) is never changed: a gesture bound to a tool
    runs it with no questions asked; and neither is whether it runs in the sandbox (``sandbox``).

    Args:
        action: what is asked.
        actor: who asks.
        builtin: whether the server is one of Orion's own groups (system, web, organizer, memory, skills,
        orion). old: the server's settings now; None when it has none (a new server, or a group left at its
        defaults). new: the server's complete settings after an add or update; keys left out are removed. Ignored
        otherwise. run_command_on: whether the run_command tool is switched on (a server run by a command can run
        anything). name: the server's name, used in the reasons; "orion" names the in-process link, whose restart
            would cut short the very call that asked for it.

    Returns:
        None when the change may go ahead, else a Refusal saying why.
    """
    server = f"the {name} server" if name else "that server"
    if action == "restart" and name == CORE_SERVER:
        return Refusal("The orion server runs inside Orion himself: restarting it would cut short the very "
                       "call that asked for it.")
    if actor == "hud" or action in ("restart", "disable"):
        return None
    before: Mapping[str, JSON] = old or {}
    if action == "enable":
        if before.get("enabled") is False:
            return Refusal(f"Only the user can switch {server} back on, {_CONNECTORS_PAGE}.")
        return None
    if builtin:
        return Refusal(f"Orion's own tool groups can only be switched off or restarted, and {server} is one of "
                       "them.")
    if action == "remove":
        return None
    after: Mapping[str, JSON] = new or {}
    changed = {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
    if before.get("enabled") is False and after.get("enabled") is not False:
        return Refusal(f"Only the user can switch {server} back on, {_CONNECTORS_PAGE}.")
    if switched_off(before) - switched_off(after):
        return Refusal(f"Only the user can switch a tool back on, {_CONNECTORS_PAGE}.")
    if after.get("intents") is True and before.get("intents") is not True:
        return Refusal(f"Only the user can let {server} push intents (intents: true), in data/config.yaml.")
    if after.get("lend") is True and before.get("lend") is not True:
        return Refusal(f"Only the user can lend {server}'s tools to other apps (lend: true), in data/config.yaml.")
    if "gestures" in changed:
        return Refusal("Only the user can choose what gestures do, in the HUD, on the remote's page in Customize, "
                       "Connectors.")
    if "sandbox" in changed:
        return Refusal(f"Only the user can choose whether {server} runs in the sandbox, {_CONNECTORS_PAGE}.")
    if changed.intersection(_TOKEN_FIELDS):
        return Refusal(f"Only the user can set a server's tokens (its env and headers), {_CONNECTORS_PAGE}.")
    if changed & _COMMAND_FIELDS and not run_command_on:
        return Refusal("A server run by a command can run anything, and run_command is switched off: only the user "
                       f"can add or change one now, {_CONNECTORS_PAGE}.")
    if changed & _TARGET_FIELDS and any(before.get(part) for part in _TOKEN_FIELDS):
        return Refusal(f"Only the user can re-point {server}: it holds tokens meant for where it points now, "
                       f"{_CONNECTORS_PAGE}.")
    if any(_reads_env(after.get(key)) for key in changed):
        return Refusal(f"Only the user can fill a server's settings in from the environment ($NAME), {_CONNECTORS_PAGE}.")
    return None


def mask_secrets(config: Mapping[str, JSON], *, openrouter_key_set: bool) -> dict[str, JSON]:
    """Return the settings with their secrets hidden, for showing them (the input is left unchanged).

    The OpenRouter key becomes "(hidden)" when one is set, else "". Every value under an MCP server's env or headers
    becomes "(hidden)", except a value that is exactly ``${NAME}``: it only names a variable of Orion's
    environment.

    Args:
        config: the settings as nested sections, e.g. ``{"openrouter": {...}, "mcp_servers": {...}}``.
        openrouter_key_set: whether an OpenRouter key is set (in the file or the environment).

    Returns:
        A new top-level dict; the openrouter section and each server's settings are new dicts too, and every other
        value is shared with the input.
    """
    masked: dict[str, JSON] = dict(config)
    openrouter = config.get("openrouter")
    if isinstance(openrouter, dict):
        masked["openrouter"] = {**openrouter, "api_key": HIDDEN if openrouter_key_set else ""}
    servers = config.get("mcp_servers")
    if isinstance(servers, dict):
        masked["mcp_servers"] = {name: masked_server(settings) for name, settings in servers.items()}
    return masked


def masked_server(settings: JSON) -> JSON:
    """Return one MCP server's settings with its env and headers values hidden, except plain ``${NAME}`` values.

    Args:
        settings: the server's settings as configured.

    Returns:
        A new dict when ``settings`` is one (anything else is returned as it is); a malformed, non-empty env or
        headers value is hidden whole.
    """
    if not isinstance(settings, dict):
        return settings
    masked = dict(settings)
    for part in _TOKEN_FIELDS:
        values = settings.get(part)
        if isinstance(values, dict):
            masked[part] = {key: value if isinstance(value, str) and _PLAIN_REF.fullmatch(value) else HIDDEN
                            for key, value in values.items()}
        elif values:
            masked[part] = HIDDEN
    return masked


def core_memory_allowed(p: Provenance) -> bool:
    """Whether a memory write from this source may reach the core memory (USER.md, MEMORY.md), per D3.

    Args:
        p: where the write comes from.

    Returns:
        True for brain, hud, review and hand; False for tainted and app (their writes go to unverified.md).
    """
    return p in TRUSTED


def taints(*, open_world: bool | None, external: bool, override: bool | None) -> bool:
    """Whether a successful call of this tool makes the rest of its turn untrusted, per D3.

    Args:
        open_world: the tool's MCP open_world hint (None when unsaid).
        external: whether the tool comes from a server other than Orion's own.
        override: the tool's own declaration (True or False), None when it declares nothing.

    Returns:
        True for every external tool, whatever it declares; for Orion's own tools the override when given, else
        whether the tool is open-world.
    """
    if external:
        return True
    if override is not None:
        return override
    return open_world is True


def confirm_reason(*, origin: str, tainted: bool, name: str, hints: Mapping[str, bool | None],
                   after_content: bool, always: Collection[str], never: Collection[str], widens: str = "",
                   channel_asks: bool = False) -> str:
    """Why a call needs the user's yes before it runs: "outside_sandbox" (it would run without the sandbox),
    "channel" (the place it came from says to ask about it), "always" (the user listed the tool, or it widens his
    reach), "content" (the request has read web or app content, and the tool changes or sends something), or "" when
    it may simply run.

    Only the model's calls are asked about: the user's own (the HUD, a gesture) and another app's are theirs. A tool
    is risky unless it declares it only reads, or declares it destroys nothing: MCP's defaults make an external tool
    that says nothing about itself destructive, so it is asked about. Widening his reach and a channel's own rule are
    asked about whatever ``never_confirm`` says (that list is about what web content may steer).

    Args:
        origin: who asks ("brain" for the model, also in a scheduled job's run).
        tainted: whether the turn has read web or app content.
        name: the tool's model-visible name.
        hints: its MCP behaviour hints (read_only, destructive, ...; None or absent when unsaid).
        after_content: ``safety.confirm_after_content``.
        always: ``safety.always_confirm``.
        never: ``safety.never_confirm``.
        widens: what the call widens (``widening``), "" for nothing.
        channel_asks: whether the channel's rule says to ask about it (``channel_verdict`` is "confirm").

    Returns:
        The reason, or "".
    """
    if origin != "brain":
        return ""
    if widens:
        return widens
    if channel_asks:
        return "channel"
    if name in never:
        return ""
    if name in always:
        return "always"
    risky = hints.get("read_only") is not True and hints.get("destructive") is not False
    return "content" if tainted and after_content and risky else ""


def check_channels(channels: Mapping[str, Mapping[str, Collection[str]]]) -> None:
    """Check ``safety.channels``: every channel is there with a ``never`` and a ``confirm`` list, and each entry names
    a tool, ``<connector>:*`` or a class (``@read``, ``@change``, ``@destroy``, ``@open_world``).

    Raises:
        ValueError: what is wrong, naming the channel.
    """
    missing = [name for name in CHANNELS if name not in channels]
    extra = sorted(channels.keys() - set(CHANNELS))
    if missing or extra:
        raise ValueError(f"the channels are {', '.join(CHANNELS)}: "
                         + (f"{missing[0]} is missing" if missing else f"{extra[0]} isn't one"))
    for name, rule in channels.items():
        if rule.keys() != {"never", "confirm"}:
            raise ValueError(f"{name} holds a never list and a confirm list, nothing else")
        for kind, entries in rule.items():
            if bad := [entry for entry in entries if not _SELECTOR.fullmatch(entry)]:
                raise ValueError(f"{name}.{kind}: {bad[0]!r} isn't a tool, a connector:* or one of {', '.join(CLASSES)}")


def risk_of(hints: Mapping[str, bool | None]) -> Risk:
    """How much harm a tool may do, from its MCP hints: "read" (it only reads), "change" (it changes things but says
    it destroys nothing) or "destroy" (it may destroy something, or says nothing: MCP assumes the worst)."""
    if hints.get("read_only") is True:
        return "read"
    return "change" if hints.get("destructive") is False else "destroy"


def channel_of(origin: str, source: str) -> str:
    """The channel a call counts as: another app's is "app"; the rest by where the request came from (voice, hud,
    discord, job); a call outside any request (the HUD's own tool use) is the HUD's.

    Args:
        origin: who asks ("brain", "hud", "app").
        source: where the turn's request came from ("" outside a turn).

    Returns:
        One of ``CHANNELS``.
    """
    if origin == "app":
        return "app"
    return source if source in CHANNELS else "hud"


def selects(selector: str, *, name: str, server: str, hints: Mapping[str, bool | None]) -> bool:
    """Whether a channel rule's entry names a tool: by its name, by ``<connector>:*`` (its server), or by class
    (``@read``, ``@change``, ``@destroy``, ``@open_world``).

    Args:
        selector: the entry.
        name: the tool's model-visible name.
        server: the connector that provides it.
        hints: its MCP hints.
    """
    if selector.startswith("@"):
        return (hints.get("open_world") is True) if selector == "@open_world" else risk_of(hints) == selector[1:]
    if selector.endswith(":*"):
        return server == selector[:-2]
    return selector == name


def channel_verdict(channels: Mapping[str, Mapping[str, Collection[str]]], channel: str, *, name: str, server: str,
                    hints: Mapping[str, bool | None]) -> Verdict:
    """What a channel's rule says of a tool: "never" (it can't be used from there, whoever says yes), "confirm" (the
    user is asked first, however safe the request looks) or "".

    Args:
        channels: ``safety.channels``.
        channel: where the call comes from (``channel_of``).
        name: the tool's model-visible name.
        server: the connector that provides it.
        hints: its MCP hints.
    """
    rule = channels.get(channel)
    if rule is None:
        return ""
    for verdict in ("never", "confirm"):
        if any(selects(entry, name=name, server=server, hints=hints) for entry in rule[verdict]):
            return verdict
    return ""


def widening(name: str, args: Mapping[str, JSON]) -> str:
    """Whether a call widens his reach: running a command outside the sandbox.

    Such a call needs the user's yes every time, and never comes from an unattended channel (``UNATTENDED``).

    Args:
        name: the tool's model-visible name.
        args: its arguments.

    Returns:
        The reason it asks ("outside_sandbox") or "" for an ordinary call.
    """
    for tool, argument, values, reason in _WIDENING:
        value = args.get(argument)
        if name == tool and isinstance(value, str | bool) and value in values:  # (a list or a dict can't be one)
            return reason
    return ""


def trusted_provenance(value: object, *, lend: bool) -> Provenance:
    """The provenance a call into the tools process may claim, from its request ``_meta``.

    Args:
        value: the request's ``_meta["orion/provenance"]``, whatever was sent (or None).
        lend: whether the tools process is lending its tools to another app (``orion mcp-serve``).

    Returns:
        "app" in lend mode; otherwise the value when it is one of brain, tainted, hud and app, and "app" for anything
        missing or unknown (the safe default).
    """
    if lend or not isinstance(value, str) or value not in WIRE_PROVENANCE:
        return "app"
    return cast(Provenance, value)


def close_victims(rows: Iterable[ProcRow], name: str, *, me: int, ancestors: frozenset[int], user: str,
                  exec_names: frozenset[str]) -> list[int] | Refusal:
    """Choose the processes close_app ends for the application the user named.

    A process is chosen when it runs as ``user``, is neither this process nor one of its ancestors, is not protected,
    and its name starts with the name asked for or equals one of the names (the name asked for and the application's
    executables) — or its executable does. Protected names never count as names to match, so an application started
    through an interpreter or a shell does not take every process of that interpreter with it.

    Args:
        rows: the running processes.
        name: what the user called the application, e.g. "Firefox" or "Visual Studio Code".
        me: this process's id.
        ancestors: the ids of this process's ancestors (Orion himself among them).
        user: the user whose processes may be ended.
        exec_names: base names of the matching installed application's executables, e.g. {"code"}.

    Returns:
        The chosen process ids in the order of ``rows`` (possibly none), or a Refusal when the name is too short to
        mean one application.
    """
    wanted = name.strip().lower()
    if len(wanted) < 3:
        return Refusal("Tell me which application to close.")
    executables = (e.rsplit("/", 1)[-1].strip().lower() for e in exec_names)
    names = {n for n in (wanted, *executables) if n and not _protected(n)}
    spared = ancestors | {me}
    chosen: list[int] = []
    for row in rows:
        process = row.name.lower()
        if row.username != user or row.pid in spared or row.pid in chosen or _protected(process):
            continue
        if process.startswith(wanted) or process in names or row.exe_name.lower() in names:
            chosen.append(row.pid)
    return chosen


def _never_ai(key: str) -> bool:
    """Whether a flat key matches ``NEVER_AI`` (case and surrounding spaces ignored; sub-keys of bare keys too)."""
    flat = key.strip().lower()
    section = flat.partition(".")[0]
    return section in _NEVER_SECTIONS or section in _NEVER_KEYS or flat in _NEVER_KEYS


def _reads_env(value: JSON) -> bool:
    """Whether any string in a JSON value (keys included, at any depth) references an environment variable."""
    stack: list[JSON] = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if ENV_REF.search(item):
                return True
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
    return False


def _protected(name: str) -> bool:
    """Whether close_app must spare a process or name: protected as it is, or once a version tail is cut off."""
    lowered = name.lower()
    return lowered in PROTECTED_PROCESSES or lowered.rstrip(_VERSION_TAIL) in PROTECTED_PROCESSES
