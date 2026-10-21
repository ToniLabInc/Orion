"""The pure rules of orion.voice.wake, over the factory phrase lists."""

import pytest

from orion.config import default_config
from orion.voice import wake

PH = wake.Phrases.from_config(default_config())  # the factory phrase lists, from default_config.yaml
EDITS = default_config().wake.name_max_edits
NAME = {"lead_words": default_config().wake.lead_words, "max_edits": EDITS}  # where his name may come, its slips
SM = wake.Summons.from_config(default_config())
OWN = wake.OwnWords(last_reply="It is sunny and warm in Paris today.", saying="I am Orion, and I am online.")


def test_his_name_and_the_short_phrases() -> None:
    """His name as Whisper writes it (not near-misses), the stage-1 hits, and stop, dismissal and filler phrases."""
    assert all(wake.is_name(w, EDITS) for w in ("Orion", "ORION", "orion.", "Orion's", "Orian", "Arion", "Orions"))
    assert not any(wake.is_name(w, EDITS) for w in ("onion", "Onions", "Orin", "union", "Ori", "Orionson"))
    assert all(map(wake.candidate_hit, ("oh ryan", "or ion", "oreon")))
    assert not any(map(wake.candidate_hit, ("the electron microscope", "good morning everyone")))
    kinds = {"Stop.": "stop", "never mind": "stop", "That's all.": "end", "Thanks!": "end",
             "thank you very much": "end", "Thanks, Orion.": "end", "stop the music": "",
             "thanks, what about tomorrow?": "", "Umm...": "filler", "uh, so...": "filler", "Hmm.": "filler",
             "So what now": "", "": "", "OK": ""}
    rules = {"stop": wake.is_stop, "end": wake.is_dismissal, "filler": wake.is_filler}
    assert {s: "".join(k for k, rule in rules.items() if rule(s, PH, max_edits=EDITS)) for s in kinds} == kinds
    assert wake.is_stop("Orian, stop.", PH, max_edits=1) and not wake.is_stop("Orian, stop.", PH, max_edits=0)

@pytest.mark.parametrize(("text", "asked"), [  # asked None: not addressed, and the text is kept whole
    ("Orion.", ""), ("Hey Orion?", ""), ("Thanks Orion.", ""),
    ("Hey, Orion, what time is it?", "what time is it?"),
    ("Orion what's the weather", "what's the weather"), ("ORION open Firefox", "open Firefox"),
    ("Davis, come here", None), ("What's the weather like tomorrow, Orion?", "What's the weather like tomorrow"),
    ("Tell Orion a joke", None), ("Okay Orion, set a timer.", "set a timer."),
    ("Orionson is here", None),
    ("Orian, open Firefox.", "open Firefox."), ("O'Ryan, open Firefox.", "open Firefox."),
    ("O.R.I.O.N. what's up", "what's up"), ("I was talking to Orion.", None),
    ("HiOrion, open YouTube.", "open YouTube."), ("Ryan is coming at five.", None), ("Chop the onion.", None),
    ("Oh Ryan, what time is it?", "what time is it?"), ("Orion? Orion, set a timer.", "set a timer."),
    ("Good evening and welcome to the news. Orion, what time is it?", "what time is it?"),  # over a TV
    ("Jarvis? Jarvis? Ah sorry, Orion?", ""), ("Talk to Orion, he knows.", None),
    ("Turn it all on, please", None), ("The Orion nebula is beautiful tonight", None),
    ("Travis is coming over later.", None), ("The electron microscope is ready", None),
    ("I think we should order pizza", None),
])
def test_address(text: str, asked: str | None) -> None:
    """Addressed only when his name is said to him; the request is what follows (or precedes a closing name)."""
    assert wake.address(text, PH, NAME["lead_words"], max_edits=EDITS) == wake.Address(
        asked is not None, text if asked is None else asked, asked == "")


def test_sanity_completeness_reuse_and_what_he_does() -> None:
    """Loops and stock inventions are caught; completeness is judged; reuse needs a whole, uninterrupted check;
    the follow-up window and barge-in decide what heard words do."""
    assert wake.is_repetitive("Orion, YouTube, YouTube, YouTube, YouTube")
    assert not wake.is_repetitive("What time is it?")
    assert (wake.clean_transcript("Thanks for watching!", PH), wake.clean_transcript("um", PH)) == ("", "um")
    complete = {"What time is it?": True, "Set a timer for ten minutes and": False, "Open Firefox": None}
    assert {t: wake.utterance_complete(t, PH) for t in complete} == complete
    check, rec = wake.CheckInfo(1, 100, "what time is it?", False, False), wake.RecordInfo(1, 100)
    assert wake.can_reuse(check, rec) and not wake.can_reuse(check, wake.RecordInfo(1, 101))  # speech after the check
    cut_checks = [wake.CheckInfo(1, 100, "", *cut) for cut in ((True, False), (False, True))]
    assert not any(wake.can_reuse(cut, rec) for cut in cut_checks)  # head or tail cut off
    acts = {"": "keep_listening", "It is sunny and warm in Paris": "keep_listening", "And tomorrow?": "command",
            "Thanks, Orion.": "end", "Umm...": "filler", "Orion": "wake_again", "Stop.": "stop"}
    assert {h: wake.classify_utterance(h, "follow_up", OWN, PH, 4, **NAME)[0] for h in acts} == acts  # after a reply
    barge = {"Orion, stop.": "stop", "I am Orion, and I am online.": "ignore",
             "Orion, what's the time": "stop_and_listen"}
    assert {t: wake.barge_action(t, OWN, loading=False, ph=PH, **NAME) for t in barge} == barge
    assert wake.barge_action("Orion, what's the time", OWN, loading=True, ph=PH, **NAME) == "ignore"


@pytest.mark.parametrize(("text", "summoned"), [  # summoned None: a request for the model
    ("Show me the weather and my timers.", ("show", ("weather", "timers"))),
    ("Open voice settings", ("show", ("settings/voice",))),
    ("Can you show me my tasks please?", ("show", ("tasks",))),
    ("Close the chat", ("hide", ("comms",))),
    ("clear", ("hide", (*wake.PANELS, *wake.VIEWS, *wake.REALMS))),
    ("Show me my hands", ("show", ("hands",))),  # the live view of the hands: only when asked for by name
    ("Show me everything", ("show", wake.PANELS)),
    ("Open gestures settings", ("show", ("settings/abilities",))),  # they live under their server there
    ("Open the abilities settings", ("show", ("settings/abilities",))),
    ("Take me to your memory", ("show", ("neural",))),
    ("Show me what you know about Paris", None),
    ("Open YouTube", None),
    ("Give me the weather", None),
    ("show", None),
])
def test_summon(text: str, summoned: tuple[str, tuple[str, ...]] | None) -> None:
    """Only a verb and names of the HUD's parts make a summon; anything more goes to the model."""
    got = wake.summon(text, SM)
    assert (None if got is None else (got.action, got.panels)) == summoned
