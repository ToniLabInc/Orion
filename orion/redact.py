"""The redactor: every secret a process knows, cut out of every text a model, the HUD, a channel, a log or the
activity log receives (G1), and looked for in every tool call's arguments, which are refused when they carry one
(G2, in ``orion.mcp``).

What it knows:
- values: the vault's secrets, the secret-looking variables of the environment (``CLAUDE_CODE_OAUTH_TOKEN``...) and
  the tokens inside a secret that is JSON (an OAuth sign-in's access and refresh tokens). ``orion.secrets.Known``
  gathers them (a ``Source``) and says when they changed. A value shorter than ``safety.redact_min_chars`` is left
  alone: it would match ordinary words.
- each value's encodings (``encodings``), so it can't slip out disguised: base64, standard and URL-safe, at each of
  the three alignments it can have inside a longer encoded text; URL-encoding; hex; JSON escaping.
- patterns (``safety.secret_patterns``): the shapes of well-known tokens, whoever's they are: private-key blocks,
  ``sk-…``, ``ghp_…``, AWS keys, JWTs, Discord bot tokens...

All of it is one RE2 automaton, built again whenever what it knows changes (8 ms for 50 secrets), so a text is read
once, in linear time: 0.14 ms for 100 KB when nothing matches and 0.34 ms with a secret in it, against 50 secrets with
their 370 forms and the 30 patterns of the factory settings; 1.3 us for a short line (Python's ``re`` would try each
alternative at every position: 68 ms). Every match becomes ``[secret]``. A redactor is safe to use from any thread:
a build swaps one immutable matcher in.

Each process installs its own (``install``): Orion, his tools process, the Discord bridge. Until one does, the active
redactor knows nothing. ``guard_logs`` makes every log line of the process pass through it, tracebacks included.

A reply that streams (``Stream``) is let out a word at a time, so a secret within a word (every token, every key) is
always seen whole before any of it shows; a known secret that has spaces in it, and a private-key block that has
begun, wait until they can be judged (at most ``MAX_HOLD`` characters). What comes out is redacted again as a whole
when it is stored.
"""

import base64
import contextlib
import json
import logging
import re
import threading
import urllib.parse
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

import re2

REDACTED: Final = "[secret]"  # what a secret becomes
BLOCK_START: Final = "-----BEGIN"  # a key block: while a reply streams, held back from here until its end
MAX_HOLD: Final = 16_384  # a stream holds back at most this many characters (a key block is about 3,000)
KEEP_HOLD: Final = 4_096  # ...and when more is waiting, lets out all but the last this many
MEMORY_MIN: Final = 64 << 20  # RE2's memory budget for one matcher: its lazily built DFA needs room for about twenty
MEMORY_PER_CHAR: Final = 1024  # states of the size of the whole program, which grows with the values' characters

_B64_SKIP: Final = (0, 2, 3)  # characters of base64 that still mix in the bytes before the secret, by alignment
_BLOCK_END: Final = re.compile(r"-----END[^\n]*?-----")
_SURROGATES: Final = re.compile("[\ud800-\udfff]")  # what RE2 can't read: UTF-16 halves left alone by a broken escape
_SPACES: Final = " \t\n\r\x0b\x0c"  # what ends a word of a stream (a secret has none of these, unless it is known)
_PROBE: Final = "a b\n-\n"  # where a pattern that can match nothing at all shows it (words, spaces, line ends, the end)

log = logging.getLogger("orion.redact")


class Source(Protocol):
    """Where a redactor learns its values (``orion.secrets.Known``)."""

    def revision(self) -> object:
        """Something that differs whenever the values do, cheap to ask (one ``stat``)."""

    def values(self) -> Iterable[str]:
        """Every value that is secret now."""


def encodings(value: str) -> set[str]:
    """A secret as it can appear in a text: itself, URL-encoded, JSON-escaped, as hex, and in base64.

    Base64 turns each 3 bytes into 4 characters, so a secret inside a longer encoded text shows one of three
    different strings, depending on how many bytes come before it; each is cut to the characters made from the
    secret's own bits alone (those mixed with the bytes around it can be anything). The secret encoded on its own,
    with or without its padding, is a form too.

    Args:
        value: the secret.

    Returns:
        Its forms (some may coincide with the value itself).
    """
    raw = value.encode()
    found = {value, urllib.parse.quote(value, safe=""), urllib.parse.quote_plus(value), json.dumps(value)[1:-1],
             json.dumps(value, ensure_ascii=False)[1:-1], raw.hex(), raw.hex().upper()}
    for encode in (base64.b64encode, base64.urlsafe_b64encode):
        alone = encode(raw).decode("ascii")
        found |= {alone, alone.rstrip("=")}
        for before in range(3):
            end = (4 * (before + len(raw))) // 3  # the characters made from whole bytes of the secret (no padding)
            found.add(encode(b"\0" * before + raw).decode("ascii")[_B64_SKIP[before]:end])
    return found


def check_patterns(patterns: Iterable[str]) -> None:
    """Refuse a secret pattern RE2 can't run (``safety.secret_patterns``: no lookaround, no backreference), or one that
    can match nothing at all (it would put ``[secret]`` between every two characters).

    Raises:
        ValueError: names the pattern and what is wrong with it.
    """
    for pattern in patterns:
        try:
            regex = re2.compile(f"(?:{pattern})", _options(MEMORY_MIN))
        except re2.error as e:
            reason = e.args[0].decode(errors="replace") if e.args and isinstance(e.args[0], bytes) else str(e)
            raise ValueError(f"the secret pattern {pattern!r} isn't one RE2 can run: {reason}") from None
        if any(match.start() == match.end() for match in regex.finditer(_PROBE)):
            raise ValueError(f"the secret pattern {pattern!r} can match nothing at all: give it at least one character "
                             "it always needs")


@dataclass(frozen=True, slots=True)
class _Matcher:
    """One build of a redactor: what it finds, and what a stream holds back.

    Attributes:
        regex: the RE2 automaton; None when there is nothing to find.
        spaced: the beginnings, each up to one of its spaces, of the known values (and forms) that have spaces in them.
        values: how many values it knows.
    """

    regex: Any
    spaced: frozenset[str]
    values: int


def _options(memory: int) -> Any:
    """RE2's options for a matcher: its memory budget, no capture groups (only where matches are), errors raised
    rather than logged."""
    options = re2.Options()
    options.max_mem = memory
    options.never_capture = True
    options.log_errors = False
    return options


def _build(values: frozenset[str], patterns: tuple[str, ...], min_chars: int) -> _Matcher:
    """The matcher of these values (with their forms) and patterns (already checked)."""
    needles = sorted({form for value in values for form in encodings(value) if len(form) >= min_chars},
                     key=lambda form: (-len(form), form))  # leftmost-first: at one place, the longest form wins
    parts = [re2.escape(needle) for needle in needles] + [f"(?:{pattern})" for pattern in patterns]
    if not parts:
        return _Matcher(None, frozenset(), 0)
    memory = max(MEMORY_MIN, MEMORY_PER_CHAR * sum(map(len, needles)))
    regex = re2.compile("|".join(parts), _options(memory))
    spaced = frozenset(needle[:i + 1] for needle in needles for i, char in enumerate(needle[:-1]) if char.isspace())
    return _Matcher(regex, spaced, len(values))


def _valid(text: str) -> str:
    """A text RE2 can read: each lone surrogate (from a broken JSON escape) becomes U+FFFD."""
    return _SURROGATES.sub("\ufffd", text)


class Redactor:
    """Everything secret one process knows, as one automaton (see the module docstring).

    Args:
        patterns: ``safety.secret_patterns`` (RE2 syntax).
        min_chars: ``safety.redact_min_chars``: shorter values, and shorter forms of a value, are left alone.
        source: where it learns its values (``refresh`` asks it); None: only what ``learn`` is given.

    Raises:
        ValueError: a pattern RE2 can't run.
    """

    def __init__(self, patterns: Sequence[str] = (), *, min_chars: int, source: Source | None = None) -> None:
        check_patterns(patterns)
        self._lock = threading.Lock()  # one build at a time; readers never wait (they take the matcher of the moment)
        self._patterns = tuple(patterns)
        self._min = min_chars
        self._source = source
        self._revision: object = object()  # never equal to a source's: the first refresh always learns
        self._failed = ""  # why the source couldn't be read last, said once
        self._values: frozenset[str] = frozenset()
        self._matcher = _build(self._values, self._patterns, min_chars)

    @property
    def known(self) -> int:
        """How many values it knows (their forms not counted)."""
        return self._matcher.values

    @property
    def idle(self) -> bool:
        """Whether it has nothing to look for (no value, no pattern): any text it is given comes back as it is."""
        return self._matcher.regex is None

    def stale(self) -> bool:
        """Whether the source's values changed since they were learned (one cheap look: a ``stat``); reading them may
        take longer (a keyring), so a caller on the event loop asks this first and refreshes elsewhere."""
        source = self._source
        if source is None:
            return False
        try:
            return source.revision() != self._revision
        except Exception:  # noqa: BLE001 (``refresh`` says why it can't be read)
            return True

    def refresh(self) -> bool:
        """Learn the source's values again when they changed (one cheap look otherwise); never raises.

        A source that can't be read (the keyring doesn't answer) is logged once, and what was known stays known.

        Returns:
            Whether it learned something new.
        """
        source = self._source
        if source is None:
            return False
        try:
            revision = source.revision()
            if revision == self._revision:
                return False
            values = list(source.values())
        except Exception as e:  # noqa: BLE001 (a source that fails never stops a call: it keeps the values it has)
            if str(e) != self._failed:
                self._failed = str(e)
                log.warning("the secrets couldn't be read, so new ones are not hidden yet: %s", e)
            return False
        self._failed = ""
        self._revision = revision
        return self.learn(values)

    def learn(self, values: Iterable[str]) -> bool:
        """Know exactly these values from now on (surrounding spaces ignored; shorter than ``min_chars``: left alone).

        Args:
            values: every value to look for.

        Returns:
            Whether what it knows changed (it was built again).
        """
        clean = frozenset(stripped for value in values if len(stripped := value.strip()) >= self._min)
        with self._lock:
            if clean == self._values:
                return False
            self._matcher = _build(clean, self._patterns, self._min)
            self._values = clean
        return True

    def configure(self, patterns: Sequence[str], *, min_chars: int) -> bool:
        """Look for other patterns, or values of another length, from now on (``safety`` changed).

        Returns:
            Whether anything changed (it was built again).

        Raises:
            ValueError: a pattern RE2 can't run (the redactor stays as it was).
        """
        with self._lock:
            if tuple(patterns) == self._patterns and min_chars == self._min:
                return False
            check_patterns(patterns)
            values = frozenset(value for value in self._values if len(value) >= min_chars)
            self._matcher = _build(values, tuple(patterns), min_chars)
            self._patterns, self._min, self._values = tuple(patterns), min_chars, values
            self._revision = object()  # what the source gave under the old length is asked for again
        return True

    def redact(self, text: str) -> str:
        """The text with every secret it holds replaced by ``[secret]`` (the very same object when it holds none).

        Text that isn't valid Unicode (a lone surrogate, from a broken JSON escape) is first made valid, each bad
        character becoming U+FFFD.
        """
        regex = self._matcher.regex
        if regex is None or not text:
            return text
        try:
            redacted, _ = regex.subn(REDACTED, text)
        except UnicodeEncodeError:
            redacted, _ = regex.subn(REDACTED, _valid(text))
        return redacted

    def holds(self, value: object) -> bool:
        """Whether any text in a JSON value (keys included, at any depth) carries a secret (G2: a tool call's
        arguments)."""
        regex = self._matcher.regex
        if regex is None:
            return False
        stack: list[object] = [value]
        while stack:
            item = stack.pop()
            if isinstance(item, str):
                try:
                    found = regex.search(item)
                except UnicodeEncodeError:
                    found = regex.search(_valid(item))
                if found is not None:
                    return True
            elif isinstance(item, dict):
                stack.extend(item.keys())
                stack.extend(item.values())
            elif isinstance(item, list | tuple):
                stack.extend(item)
        return False

    def stream(self) -> "Stream":
        """A redaction for one reply as it streams (see ``Stream``)."""
        return Stream(self)

    def releasable(self, text: str) -> int:
        """How much of a streamed text can be let out now: up to its last space (the word it ends in may not be over),
        less a key block that has begun but not ended, less the beginning of a known value with spaces in it; and
        whatever is more than ``MAX_HOLD`` characters back, all but the last ``KEEP_HOLD``.

        Args:
            text: what is held so far.

        Returns:
            How many of its characters can go.
        """
        end = max(map(text.rfind, _SPACES)) + 1
        head = text[:end]
        if BLOCK_START in head:  # a block is open from its BEGIN until an END line follows it, inside what goes out
            closed = max((match.start() for match in _BLOCK_END.finditer(head)), default=-1)
            if (opened := head.find(BLOCK_START, closed + 1)) >= 0:
                end, head = opened, head[:opened]
        if self._matcher.spaced:
            end -= max((len(start) for start in self._matcher.spaced if head.endswith(start)), default=0)
        return max(end, len(text) - KEEP_HOLD) if len(text) > MAX_HOLD else end


class Stream:
    """One reply's redaction while it streams: pieces go in as they come, the text that is safe to show comes out.

    Args:
        redactor: what it looks for.
    """

    def __init__(self, redactor: Redactor) -> None:
        self._redactor = redactor
        self._held = ""

    def feed(self, piece: str) -> str:
        """Take the next piece of the reply.

        Returns:
            The text that can be shown now, redacted ("" while everything new waits; with nothing to look for, the piece
            as it is).
        """
        if self._redactor.idle and not self._held:
            return piece
        if not any(space in piece for space in _SPACES) and len(self._held) < MAX_HOLD:
            self._held += piece  # still inside the same word: nothing new can come out
            return ""
        text = self._held + piece
        end = self._redactor.releasable(text)
        self._held = text[end:]
        return self._redactor.redact(text[:end]) if end > 0 else ""

    def flush(self) -> str:
        """The reply ended (or was cut short): the rest, redacted."""
        text, self._held = self._held, ""
        return self._redactor.redact(text)


# ---------------------------------------------------------------------------------------------------- the process's
_active = Redactor(min_chars=len(REDACTED))  # knows nothing until the process installs its own


def active() -> Redactor:
    """The redactor this process installed (one that knows nothing until it does)."""
    return _active


def install(redactor: Redactor) -> None:
    """Make a redactor the one this process uses everywhere (``redacted``, ``holds_secret``, the log lines)."""
    global _active
    _active = redactor


@contextlib.contextmanager
def installed(redactor: Redactor) -> Iterator[Redactor]:
    """Use a redactor for the length of a block, then the one before it again (tests)."""
    before = _active
    install(redactor)
    try:
        yield redactor
    finally:
        install(before)


def redacted(text: str) -> str:
    """A text with every secret this process knows replaced by ``[secret]`` (``Redactor.redact``)."""
    return _active.redact(text)


def holds_secret(value: object) -> bool:
    """Whether a JSON value (a tool call's arguments) carries a secret this process knows (``Redactor.holds``)."""
    return _active.holds(value)


def stale() -> bool:
    """Whether secrets changed since this process's redactor learned them (``Redactor.stale``)."""
    return _active.stale()


def refresh() -> bool:
    """Learn the secrets that changed since the last look (``Redactor.refresh``): one ``stat`` when none did."""
    return _active.refresh()


def stream() -> Stream:
    """A redaction for one reply as it streams, by this process's redactor."""
    return _active.stream()


def scrubbed(value: Any, redact: Callable[[str], str]) -> Any:
    """A JSON value with each of its texts redacted: the same object when none changed, else a copy of what changed.

    Args:
        value: a JSON value (an event, its fields).
        redact: what redacts one text (``redacted``).

    Returns:
        The value, redacted.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        changed = {key: new for key, item in value.items() if (new := scrubbed(item, redact)) is not item}
        return {**value, **changed} if changed else value
    if isinstance(value, list):
        items = [scrubbed(item, redact) for item in value]
        return items if any(new is not old for new, old in zip(items, value, strict=True)) else value
    return value


# ---------------------------------------------------------------------------------------------------- the logs
class _Record(logging.LogRecord):
    """A log record whose message and traceback are redacted when they are formatted."""

    def getMessage(self) -> str:
        """The message, its arguments filled in, then redacted."""
        return _active.redact(super().getMessage())

    @property
    def exc_text(self) -> str | None:  # type: ignore[override]  # LogRecord's plain attribute: here it redacts
        """The traceback as formatted (the formatter keeps it here), redacted."""
        return self._exc_text

    @exc_text.setter
    def exc_text(self, value: str | None) -> None:
        self._exc_text = _active.redact(value) if value else value


def guard_logs() -> None:
    """Make every log record of this process redact its message and traceback when formatted (the records of every
    library too; a record that isn't formatted, below the level, costs nothing)."""
    if logging.getLogRecordFactory() is not _Record:
        logging.setLogRecordFactory(_Record)
