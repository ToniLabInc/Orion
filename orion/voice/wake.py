"""What heard words mean, as pure rules.

- Whether Orion is addressed, and what was asked of him (`address`).
- The short phrases that stop him, end the exchange or only fill a pause.
- Whether a request only asks to show or hide parts of the HUD, which needs no model (`summon`).
- Transcript sanity: Whisper's stock inventions on noise, and its repetition loops.
- Whether an utterance sounds finished, and the end-of-speech pause that implies.
- Whether a name-check transcript can stand in for the request, which spares a second Whisper pass.
- What an utterance in a recording, or speech heard while he replies, should make him do.

Standard library only and free of side effects: the voice session sequences these decisions and tests/test_wake.py
pins them. Words are compared folded (lower case, no accents; see `fold`), because speech recognition is consistent
about neither. Every phrase set comes from the settings (phrases.*, wake.greetings, wake.mentions,
stt.hallucinations) through `Phrases.from_config`.
"""

from __future__ import annotations

import re
import unicodedata
import zlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from itertools import pairwise
from typing import TYPE_CHECKING, Final, Literal

from orion.voice import MAX_COMPRESSION_RATIO

if TYPE_CHECKING:
    from orion.config import Config

type Purpose = Literal["command", "follow_up", "answer"]  # why the listener was recording ("answer": to a yes/no)
type UtteranceAction = Literal["command", "keep_listening", "end", "filler", "wake_again", "stop"]
type BargeAction = Literal["ignore", "stop", "stop_and_listen"]
type SummonAction = Literal["show", "hide"]

NAME: Final = "orion"
CANDIDATE_MAX_EDITS: Final = 2  # spelling slips the stage-1 name check allows (a five-letter name): it favours recall
NOT_NAMES: Final = frozenset({"onion", "onions", "orin"})  # everyday words and another name, one slip away
MAX_REPEAT: Final = 3  # a word repeated this many times in a row (said 4 times) is a Whisper loop

_QUOTES: Final = str.maketrans({"\u2018": "'", "\u2019": "'", "\u02bc": "'"})  # typographic apostrophes
_NOT_LETTER: Final = re.compile(r"[^a-z]")
_NOT_PHRASE: Final = re.compile(r"[^\w\s'-]")
_NOT_FILLER: Final = re.compile(r"[^\w\s']")
_NOT_PLAIN: Final = re.compile(r"[^\w\s]|_")
_ECHO_WORD: Final = re.compile(r"[a-z']+")
_TOKEN: Final = re.compile(r"[\w']+")
_WORD: Final = re.compile(r"\w+")
_ACRONYM_DOT: Final = re.compile(r"(?<=\b\w)\.(?=\w\b)")  # "O.R.I.O.N." -> "ORION."
# How speech recognition sometimes splits his name in two: put back together before matching.
_SPLIT_NAME: Final = re.compile(r"\b(?:o|oh)['\u2019 -]?ry[ae]n\b", re.IGNORECASE)  # "O'Ryan", "Oh Ryan", "O Ryen"
_GLUED_GREETING: Final = re.compile(r"\b(hey|hi|hello|ok|okay|yo)orion\b", re.IGNORECASE)  # "HiOrion"
_SENTENCE_BREAK: Final = re.compile(r"(?<=[.!?…])\s+")
_AFTER_NAME: Final = " ,.;:!?…-—"  # stripped from what follows his name
_CLOSE: Final = ",.!?;…"  # punctuation that closes what was said to him
_UNFINISHED: Final = (",", "…", "...", "-", "\u2013", "—")  # speech trailing off (\u2013: en dash)
PANELS: Final = ("comms", "vitals", "weather", "timers", "tasks", "news")  # what "show everything" puts on the screen
VIEWS: Final = ("hands",)  # panels shown only when asked for by name: the live view of the hands
REALMS: Final = ("neural", "settings")  # the places that take the whole screen
SETTINGS_TABS: Final = ("general", "voice", "brain", "memory", "skills", "abilities")  # "voice settings"
EVERYTHING: Final = (*PANELS, *VIEWS, *REALMS)  # what hiding nothing named takes away
HUD_PARTS: Final = (*EVERYTHING, *(f"settings/{tab}" for tab in SETTINGS_TABS))  # what can be shown or hidden
_SUMMON_LEAD: Final = frozenset({"please", "can", "could", "would", "will", "you", "orion", "hey", "hi", "ok",
                                 "okay", "now", "just", "quickly"})  # politeness before the verb
_SUMMON_GLUE: Final = frozenset({"the", "my", "your", "me", "and", "a", "an", "both", "please", "now", "for", "us",
                                 "of", "just", "again", "back", "up", "on", "also", "too", "then"})  # between names


@dataclass(frozen=True, slots=True)
class Phrases:
    """The phrase sets the rules match against, folded once: build them with `of` or `from_config`.

    Attributes:
        stop: said alone, these silence him at once and he stands by (setting `phrases.stop`).
        dismissals: these end the exchange after an answer, without a reply (`phrases.dismiss`).
        fillers: sounds that only fill a pause while the user thinks (`phrases.fillers`).
        greetings: words allowed before his name, as in "hey Orion" (`wake.greetings`).
        mentions: words before his name that make it a mention, as in "talk to Orion, ..." (`wake.mentions`).
        hallucinations: Whisper's inventions on noise, held without punctuation (`stt.hallucinations`).
        trailing: words after which an utterance cannot be finished (`phrases.trailing`).
        apologies: words of a slip said just before his name, as in "Jarvis? Ah sorry, Orion?"
        (`phrases.apologies`).
        yes: answers that say yes when he asks before a risky action (`phrases.affirm`).
        no: answers that say no (`phrases.deny`).
    """

    stop: frozenset[str]
    dismissals: frozenset[str]
    fillers: frozenset[str]
    greetings: frozenset[str]
    mentions: frozenset[str]
    hallucinations: frozenset[str]
    trailing: frozenset[str] = frozenset()
    apologies: frozenset[str] = frozenset()
    yes: frozenset[str] = frozenset()
    no: frozenset[str] = frozenset()

    @classmethod
    def of(cls, *, stop: Iterable[str], dismissals: Iterable[str], fillers: Iterable[str], greetings: Iterable[str],
           mentions: Iterable[str], hallucinations: Iterable[str], trailing: Iterable[str] = (),
           apologies: Iterable[str] = (), yes: Iterable[str] = (), no: Iterable[str] = ()) -> Phrases:
        """Phrase sets from plain lists, folded the way the rules compare them.

        Args:
            stop: phrases that silence him.
            dismissals: phrases that end the exchange.
            fillers: single words that only fill a pause.
            greetings: single words allowed before his name.
            mentions: single words that make his name a mention.
            hallucinations: Whisper's inventions; only their letters, digits and spaces count.
            trailing: words after which an utterance cannot be finished.
            apologies: words of a slip said before his name.
            yes: answers that say yes.
            no: answers that say no.

        Returns:
            The sets. Every phrase may be in any case, with accents and punctuation: an apostrophe counts, a hyphen
            counts as a space. Empty phrases are dropped, except from `hallucinations`.
        """
        return cls(stop=_phrase_set(stop), dismissals=_phrase_set(dismissals), fillers=_phrase_set(fillers),
                   greetings=_phrase_set(greetings), mentions=_phrase_set(mentions),
                   hallucinations=frozenset(map(_plain, hallucinations)), trailing=_phrase_set(trailing),
                   apologies=_phrase_set(apologies), yes=_phrase_set(yes), no=_phrase_set(no))

    @classmethod
    def from_config(cls, cfg: Config) -> Phrases:
        """The phrase sets of these settings, cached on the lists' contents.

        It reads `phrases.*`, `wake.greetings`, `wake.mentions` and `stt.hallucinations` on every call, so an edited
        list applies on the next call with nothing to invalidate.

        Args:
            cfg: the settings.

        Returns:
            The folded sets, shared by every call with the same lists.
        """
        phrases = cfg.phrases
        return _phrases(tuple(phrases.stop), tuple(phrases.dismiss), tuple(phrases.fillers), tuple(cfg.wake.greetings),
                        tuple(cfg.wake.mentions), tuple(cfg.stt.hallucinations), tuple(phrases.trailing),
                        tuple(phrases.apologies), tuple(phrases.affirm), tuple(phrases.deny))


@dataclass(frozen=True, slots=True)
class Summon:
    """A request that only shows or hides parts of the HUD.

    Attributes:
        action: "show" or "hide".
        panels: what, in order: panels ("comms", "vitals", "weather", "timers", "tasks", "news"), the realms ("neural",
            "settings"), or a settings tab ("settings/voice").
    """

    action: SummonAction
    panels: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Summons:
    """How the HUD's parts are asked for, folded once (`phrases.show`, `hide`, `everything` and `panels`).

    Attributes:
        verbs: each verb phrase and its action.
        names: each name and what it stands for ("*" for everything).
    """

    verbs: Mapping[str, SummonAction]
    names: Mapping[str, str]

    @classmethod
    def from_config(cls, cfg: Config) -> Summons:
        """The summon phrases of these settings, cached on their contents.

        Args:
            cfg: the settings.

        Returns:
            The folded phrases.
        """
        p = cfg.phrases
        panels = tuple((panel, tuple(words)) for panel, words in p.panels.items())
        return _summons(tuple(p.show), tuple(p.hide), tuple(p.everything), panels)


@lru_cache(maxsize=4)
def _summons(show: tuple[str, ...], hide: tuple[str, ...], everything: tuple[str, ...],
             panels: tuple[tuple[str, tuple[str, ...]], ...]) -> Summons:
    """The folded summon phrases, cached on the lists' contents (hide wins a phrase in both lists)."""
    verbs: dict[str, SummonAction] = {phrase: "show" for phrase in _phrase_set(show)}
    verbs |= {phrase: "hide" for phrase in _phrase_set(hide)}
    names = {phrase: panel for panel, words in panels for phrase in _phrase_set(words)}
    names |= {phrase: "*" for phrase in _phrase_set(everything)}
    return Summons(verbs=verbs, names=names)


def summon(text: str, sm: Summons) -> Summon | None:
    """Whether a request only asks to show or hide parts of the HUD, and which.

    It must be a verb ("show me", "open", "hide", "clear") and nothing but names of the HUD's parts after it, joined
    by small words ("the", "my", "and"): "Show me the weather and my timers", "Open voice settings", "Hide everything".
    A verb of hiding said alone hides everything. Anything else ("Show me what you know about Paris", "Open YouTube")
    is a request for the model.

    Args:
        text: what was said or typed.
        sm: the summon phrases.

    Returns:
        The summon, or None when the request asks for more than that.
    """
    words = _phrase(text).split()
    i = 0
    while i < len(words) and words[i] in _SUMMON_LEAD:
        i += 1
    verb = _longest(words, i, sm.verbs)
    if verb is None:
        return None
    action, i = sm.verbs[verb], i + len(verb.split())
    found: list[str] = []
    while i < len(words):
        if words[i] in SETTINGS_TABS and words[i + 1:i + 2] == ["settings"]:
            found.append(f"settings/{words[i]}")
            i += 2
            continue
        if (name := _longest(words, i, sm.names)) is not None:
            found.append(sm.names[name])
            i += len(name.split())
            continue
        if words[i] not in _SUMMON_GLUE:
            return None
        i += 1
    if not found:
        return Summon("hide", EVERYTHING) if action == "hide" else None
    expanded = [p for name in found for p in ((PANELS if action == "show" else EVERYTHING)
                                                 if name == "*" else (name,))]
    return Summon(action, tuple(dict.fromkeys(expanded)))


def _longest(words: Sequence[str], at: int, phrases: Mapping[str, object]) -> str | None:
    """The longest phrase of `phrases` that the words starting at `at` begin with (up to four words)."""
    for n in range(min(4, len(words) - at), 0, -1):
        phrase = " ".join(words[at:at + n])
        if phrase in phrases:
            return phrase
    return None


@lru_cache(maxsize=4)
def _phrases(stop: tuple[str, ...], dismiss: tuple[str, ...], fillers: tuple[str, ...], greetings: tuple[str, ...],
             mentions: tuple[str, ...], hallucinations: tuple[str, ...], trailing: tuple[str, ...],
             apologies: tuple[str, ...], yes: tuple[str, ...], no: tuple[str, ...]) -> Phrases:
    """`Phrases.of` on the settings' lists, returning the folded sets cached on the lists' contents."""
    return Phrases.of(stop=stop, dismissals=dismiss, fillers=fillers, greetings=greetings, mentions=mentions,
                      hallucinations=hallucinations, trailing=trailing, apologies=apologies, yes=yes, no=no)


@dataclass(frozen=True, slots=True)
class Address:
    """Whether he was addressed, and what was asked.

    Attributes:
        addressed: his name was said to him, not only mentioned.
        request: what was asked of him; "" when only his name was said. When he was not addressed, the whole text.
        name_only: addressed with nothing asked yet (his name alone, a slip before it, or a filler after it): he
            should wait for the request.
    """

    addressed: bool
    request: str
    name_only: bool


@dataclass(frozen=True, slots=True)
class OwnWords:
    """What he said last and what he is saying now, to recognise his own voice coming back through the microphone.

    Attributes:
        last_reply: his last complete reply.
        saying: what he has said so far in the reply under way.
    """

    last_reply: str = ""
    saying: str = ""

    def covers(self, words: Sequence[str]) -> bool:
        """Whether every heard word is one of his.

        Args:
            words: the heard words, in any case and with any punctuation.

        Returns:
            True when at least one word was heard and each is a word of what he is saying or said last.
        """
        heard = _words(" ".join(words))
        return bool(heard) and set(_words(f"{self.saying} {self.last_reply}")).issuperset(heard)


@dataclass(frozen=True, slots=True)
class CheckInfo:
    """A name check (stage 2) that heard him addressed, kept so its transcript may stand in for the request.

    Attributes:
        seg_id: the listener's segment the checked window came from.
        end_frame: the listener's frame counter at the window's end.
        text: what the check heard.
        truncated: the segment began before the window, so its head was cut off.
        hit_token_cap: the transcript stopped at the decoding limit, so its tail may be missing.
    """

    seg_id: int
    end_frame: int
    text: str
    truncated: bool
    hit_token_cap: bool


@dataclass(frozen=True, slots=True)
class RecordInfo:
    """The recording that followed a wake, as far as transcript reuse cares.

    Attributes:
        seg_id: the standby segment the recording adopted, or None if it adopted none.
        last_speech_frame: the listener's frame counter at the recording's last speech.
    """

    seg_id: int | None
    last_speech_frame: int


def fold(text: str) -> str:
    """Lower case without accents ("Café" -> "cafe").

    Args:
        text: any text.

    Returns:
        The folded text.
    """
    if text.isascii():
        return text.lower()
    return "".join(c for c in unicodedata.normalize("NFD", text.lower()) if not unicodedata.combining(c))


def is_name(word: str, max_edits: int) -> bool:
    """Whether a word is his name as speech recognition may write it: "Orion", "Orion's", "Orian", "Arion", but
    not "onion" or "Orin" (an everyday word and another name, `NOT_NAMES`) nor "union".

    Args:
        word: one heard word; case, accents and anything but letters are ignored.
        max_edits: spelling slips allowed; a swap of two neighbouring letters counts as one.

    Returns:
        True when the word's letters are within `max_edits` of "orion".
    """
    return _near_name(_NOT_LETTER.sub("", fold(word)), max_edits)


def _near_name(letters: str, max_edits: int) -> bool:
    """Whether `letters` (a word reduced to a-z) are within `max_edits` slips of his name (length test first), or are
    his name with at most two letters run on ("Orions": no English word starts with "orion"); never another
    person's name (`NOT_NAMES`)."""
    if letters in NOT_NAMES:
        return False
    if letters.startswith(NAME) and len(letters) <= len(NAME) + 2:
        return True
    return abs(len(letters) - len(NAME)) <= max_edits and _within(letters, max_edits)


@lru_cache(maxsize=2048)
def _within(letters: str, max_edits: int) -> bool:
    """Whether `letters` are at most `max_edits` edits from his name; cached, as the same few words keep coming back."""
    return _distance(letters, NAME) <= max_edits


def _distance(a: str, b: str) -> int:
    """The fewest insertions, deletions, substitutions and swaps of neighbouring letters that turn `a` into `b`
    (optimal string alignment distance)."""
    before: list[int] = []
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i, *([0] * len(b))]
        for j, cb in enumerate(b, 1):
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb))
            if i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                current[j] = min(current[j], before[j - 2] + 1)
        before, previous = previous, current
    return previous[-1]


def _joined(text: str) -> str:
    """`text` with his name put back together where speech recognition broke it up ("O.R.I.O.N." becomes "ORION.",
    and "O'Ryan" or "Oh Ryan" becomes "Orion") or ran a greeting into it ("HiOrion" becomes "Hi Orion")."""
    text = _SPLIT_NAME.sub("Orion", _ACRONYM_DOT.sub("", text))
    return _GLUED_GREETING.sub(r"\1 Orion", text)


def candidate_hit(text: str, max_edits: int = CANDIDATE_MAX_EDITS) -> bool:
    """Whether a cheap stage-1 transcript may hold his name, so the main model should check the audio.

    It favours recall: up to `max_edits` slips, and two neighbouring words are also tried as one ("or ion").

    Args:
        text: the candidate model's transcript.
        max_edits: spelling slips allowed.

    Returns:
        True when a word, or two neighbouring words joined, come close to "orion".
    """
    previous = ""
    for word in fold(_joined(text)).split():
        letters = _NOT_LETTER.sub("", word)
        if _near_name(letters, max_edits) or (previous and letters and _near_name(previous + letters, max_edits)):
            return True
        previous = letters
    return False


def address(text: str, ph: Phrases, lead_words: int, *, max_edits: int) -> Address:
    """Whether he is addressed, and what is asked, in one pass over the sentences.

    In a sentence his name must open it (after greetings at most: "Hey Orion, ...", "Good morning Orion,
    ..."), be followed by a comma early on ("Buddy Orion, ...") unless a mention word comes first ("talk to
    Orion, ..."), be set off by a comma before it and end what is said ("What's the weather, Orion?"), or end
    a sentence within `lead_words` words ("Thanks Orion"). So "I was talking to Orion" does not wake him. Each
    sentence is looked at on its own, so what a TV said just before does not hide "... Orion, what time is it?". A
    repeated name ("Orion? Orion, ...") is skipped, and a slip of at most two words before a closing name
    ("Jarvis? Ah sorry, Orion?") asks nothing.

    Args:
        text: a transcript.
        ph: the phrase sets (greetings, mentions, fillers and apologies are used).
        lead_words: how early in a sentence his name must come.
        max_edits: spelling slips allowed in his name.

    Returns:
        The verdict, the request and whether only his name was said.
    """
    text = _joined(text)
    start = 0
    for brk in (*_SENTENCE_BREAK.finditer(text), None):
        end = brk.start() if brk else len(text)
        if (request := _request(text, start, end, ph, lead_words, max_edits)) is not None:
            while ((again := _request(request, 0, len(request), ph, lead_words, max_edits)) is not None
                   and len(again) < len(request)):
                request = again  # "Orion? Orion, ..."
            return Address(True, request, not request or is_filler(request, ph, max_edits=max_edits))
        start = brk.end() if brk else end
    return Address(False, text.strip(), False)


def _request(text: str, start: int, end: int, ph: Phrases, lead_words: int, max_edits: int) -> str | None:
    """What is asked of him if the sentence `text[start:end]` speaks to him (see `address`).

    Args:
        text: the whole transcript; what follows his name may run past the sentence.
        start: where the sentence begins.
        end: where it ends.
        ph: the phrase sets.
        lead_words: how early in the sentence his name must come.
        max_edits: spelling slips allowed in his name.

    Returns:
        The request ("" for his name alone), or None when the sentence does not speak to him.
    """
    words = list(_WORD.finditer(text, start, end))
    for w in words[:lead_words]:
        if is_name(w.group(), max_edits):
            return text[w.end():].lstrip(_AFTER_NAME).strip()
        if fold(w.group()) not in ph.greetings:
            break
    for i, w in enumerate(words):
        if not is_name(w.group(), max_edits):
            continue
        before, after = text[start:w.start()].rstrip(), text[w.end():].lstrip()
        if before.endswith(",") and (not after or after[0] in _CLOSE):
            if _slip(words[:i], ph):
                return after.lstrip(" " + _CLOSE).strip()
            return f"{before.rstrip(' ,')} {after.lstrip(' ' + _CLOSE)}".strip()
        if after.startswith(",") and i < lead_words and not any(fold(x.group()) in ph.mentions for x in words[:i]):
            return after.lstrip(" ,").strip()
        if i == len(words) - 1 and len(words) <= lead_words:
            return after.lstrip(" " + _CLOSE).strip()
    return None


def _slip(words: Sequence[re.Match[str]], ph: Phrases) -> bool:
    """Whether `words`, a sentence's words before his name, are only a slip: one or two words that `ph` holds as
    apologies, greetings or fillers ("Ah sorry", "Oh", "Hmm")."""
    if not 0 < len(words) <= 2:
        return False
    allowed = ph.apologies | ph.greetings | ph.fillers
    return all(fold(w.group()) in allowed for w in words)


def without_name(text: str, *, max_edits: int) -> str:
    """What was said with his name taken out: "Thanks, Orion." -> "Thanks,".

    Args:
        text: a transcript.
        max_edits: spelling slips allowed in his name.

    Returns:
        The other words, joined by single spaces.
    """
    return " ".join(w for w in _joined(text).split() if not is_name(w, max_edits))


def is_stop(text: str, ph: Phrases, *, max_edits: int) -> bool:
    """Whether this, his name aside, is a stop phrase ("Stop.", "never mind"): silence and stand by.

    Args:
        text: a transcript.
        ph: the phrase sets.
        max_edits: spelling slips allowed in his name.

    Returns:
        True when the whole utterance is one of `ph.stop`.
    """
    return _phrase(without_name(text, max_edits=max_edits)) in ph.stop


def is_dismissal(text: str, ph: Phrases, *, max_edits: int) -> bool:
    """Whether this, his name aside, ends the exchange ("That's all.", "Thanks, Orion."): stand by without a reply.

    Args:
        text: a transcript.
        ph: the phrase sets.
        max_edits: spelling slips allowed in his name.

    Returns:
        True when the whole utterance is one of `ph.dismissals`.
    """
    return _phrase(without_name(text, max_edits=max_edits)) in ph.dismissals


def answer_of(text: str, ph: Phrases, *, max_edits: int) -> Literal["yes", "no"] | None:
    """The answer in a reply to a yes/no question ("Yes, go ahead." -> "yes", "No, leave it." -> "no").

    The whole reply, his name aside, may be an answer; otherwise its first one to three words must be one (a "no"
    wins over a "yes" of the same length, so "no, don't do it" is no). Anything else is no answer.

    Args:
        text: a transcript.
        ph: the phrase sets (`ph.yes`, `ph.no`).
        max_edits: spelling slips allowed in his name.

    Returns:
        "yes", "no", or None when it isn't clearly either.
    """
    phrase = _phrase(without_name(text, max_edits=max_edits))
    words = phrase.split()
    for head in (phrase, *(" ".join(words[:n]) for n in (3, 2, 1) if len(words) > n)):
        if head in ph.no:
            return "no"
        if head in ph.yes:
            return "yes"
    return None


def is_filler(text: str, ph: Phrases, *, max_edits: int) -> bool:
    """Whether this, his name aside, only fills a pause ("Umm...", "uh, so..."): the user is still thinking.

    Args:
        text: a transcript.
        ph: the phrase sets.
        max_edits: spelling slips allowed in his name.

    Returns:
        True when there are words and every one is in `ph.fillers`.
    """
    words = _NOT_FILLER.sub(" ", fold(without_name(text, max_edits=max_edits)).translate(_QUOTES)).split()
    return bool(words) and all(w in ph.fillers for w in words)


def clean_transcript(text: str, ph: Phrases) -> str:
    """A transcript, or "" when it is one of Whisper's stock inventions on noise ("Thanks for watching!").

    Fillers such as "um" are kept: they mean the user is still thinking.

    Args:
        text: a transcript.
        ph: the phrase sets (`ph.hallucinations`).

    Returns:
        The stripped text, or "".
    """
    return "" if _plain(text) in ph.hallucinations else text.strip()


def is_repetitive(text: str, *, max_repeat: int = MAX_REPEAT, max_ratio: float = MAX_COMPRESSION_RATIO) -> bool:
    """Whether a transcript is a Whisper loop rather than speech ("Orion, YouTube, YouTube, YouTube, YouTube").

    Args:
        text: a transcript.
        max_repeat: a word repeated this many times in a row is a loop (with 3: a word said four times running).
        max_ratio: the highest zlib compression ratio (bytes / compressed bytes) speech has.

    Returns:
        True for a loop.
    """
    run = 0
    for previous, word in pairwise(_WORD.findall(fold(text))):
        run = run + 1 if word == previous else 0
        if run >= max_repeat:
            return True
    data = text.encode()
    return bool(data) and len(data) / len(zlib.compress(data)) > max_ratio


def utterance_complete(text: str, ph: Phrases) -> bool | None:
    """Whether an utterance, as transcribed so far, sounds finished.

    Args:
        text: a transcript of the speech up to a pause.
        ph: the phrase sets (`ph.trailing` and `ph.fillers`).

    Returns:
        False when it trails off (a comma, an ellipsis or a dash at the end, or a last word that is trailing or a
        filler), True when it ends in ".", "?" or "!" otherwise, and None when there is no telling.
    """
    said = text.rstrip()
    if not said:
        return None
    if said.endswith(_UNFINISHED):
        return False
    tokens = _TOKEN.findall(fold(said).translate(_QUOTES))
    if tokens and (tokens[-1] in ph.trailing or tokens[-1] in ph.fillers):
        return False
    return True if said[-1] in ".?!" else None


def can_reuse(check: CheckInfo, rec: RecordInfo) -> bool:
    """Whether the name check's transcript can stand in for the recording's, sparing a second Whisper pass.

    Args:
        check: the name check that woke him.
        rec: the recording that followed.

    Returns:
        True when the recording adopted the checked segment, nobody spoke after the checked audio, and the check's
        transcript is whole (not cut at either end).
    """
    return (rec.seg_id == check.seg_id and rec.last_speech_frame <= check.end_frame and not check.truncated
            and not check.hit_token_cap)


def classify_utterance(heard: str, purpose: Purpose, own: OwnWords, ph: Phrases, echo_min_words: int, *,
                       lead_words: int, max_edits: int) -> tuple[UtteranceAction, str]:
    """What a recorded utterance should make him do.

    Args:
        heard: its transcript (or the request a name check already took from it).
        purpose: why he was recording: a command after his name, or the follow-up window after a reply.
        own: his own words, to tell his voice coming back from a request.
        ph: the phrase sets.
        echo_min_words: the fewest words that count as his own voice coming back.
        lead_words: how early in a sentence his name must come.
        max_edits: spelling slips allowed in his name.

    Returns:
        The action and its text:
        - ("stop", said): a stop phrase; stop everything and stand by.
        - ("end", said): a dismissal; stand by without a reply.
        - ("filler", said): only a pause filler; keep listening.
        - ("wake_again", ""): only his name again; listen for the request.
        - ("keep_listening", ""): in the follow-up window, nothing or his own words coming back.
        - ("end", ""): nothing was asked after his name; stand by.
        - ("command", request): a request for the brain.
    """
    verdict = address(heard, ph, lead_words, max_edits=max_edits)
    text = verdict.request
    said = text or heard.strip()
    if is_stop(said, ph, max_edits=max_edits):
        return "stop", said
    if is_dismissal(said, ph, max_edits=max_edits):
        return "end", said
    if is_filler(text, ph, max_edits=max_edits):
        return "filler", text
    if verdict.addressed and not text:
        return "wake_again", ""
    if purpose == "follow_up" and (not text or _echo(text, own, echo_min_words)):
        return "keep_listening", ""
    if not text:
        return "end", ""
    return "command", text


def barge_action(heard: str, own: OwnWords, *, loading: bool, ph: Phrases, lead_words: int,
                 max_edits: int) -> BargeAction:
    """What speech heard while he thinks or speaks should do (barge-in, opt-in).

    His own voice is in the microphone too, so words that are all his own (his name in a sentence of his) never count.

    Args:
        heard: the transcript.
        own: his own words.
        loading: a brain is loading; nothing interrupts that.
        ph: the phrase sets.
        lead_words: how early in a sentence his name must come.
        max_edits: spelling slips allowed in his name.

    Returns:
        "stop" for his name with a stop phrase or dismissal ("Orion, stop."), "stop_and_listen" for his name with
        anything else (the request follows), and "ignore" otherwise.
    """
    if loading or not heard.strip():
        return "ignore"
    verdict = address(heard, ph, lead_words, max_edits=max_edits)
    if not verdict.addressed or own.covers(heard.split()):
        return "ignore"
    said = verdict.request or heard
    stop = is_stop(said, ph, max_edits=max_edits) or is_dismissal(said, ph, max_edits=max_edits)
    return "stop" if stop else "stop_and_listen"


def _echo(text: str, own: OwnWords, min_words: int) -> bool:
    """Whether `text`, heard in the follow-up window, is only his own voice coming back through the speakers: at least
    `min_words` words, and every one of them in `own`."""
    words = _words(text)
    return len(words) >= min_words and own.covers(words)


def _words(text: str) -> list[str]:
    """The folded words of `text`, in order, as the echo checks compare them: letters and apostrophes only."""
    return _ECHO_WORD.findall(fold(text).translate(_QUOTES))


def _phrase(text: str) -> str:
    """`text` as the phrase sets hold it: folded, punctuation removed (apostrophes kept), and hyphens and runs of
    space made one space."""
    return " ".join(_NOT_PHRASE.sub("", fold(text).translate(_QUOTES)).replace("-", " ").split())


def _phrase_set(items: Iterable[str]) -> frozenset[str]:
    """The phrases in `items` normalised by `_phrase`, without empty ones."""
    return frozenset(filter(None, map(_phrase, items)))


def _plain(text: str) -> str:
    """`text` as the hallucination list holds it: folded letters and digits only, one space between words
    ("I'll see you next time." -> "ill see you next time")."""
    return " ".join(_NOT_PLAIN.sub("", fold(text)).split())
