"""voice/tts: the reply splitter speaks the first clause early and flushes before tools; text is cleaned for speech."""

from orion.voice.tts import ChunkParams, SentenceSplitter, clean_for_speech

PARAMS = ChunkParams(first_min_chars=20, min_chars=12, code_note="I've put the code on screen.")


def spoken(text: str, step: int = 5) -> list[str]:
    """The pieces a reply is spoken in, streamed `step` characters at a time."""
    splitter = SentenceSplitter(PARAMS)
    pieces = [piece for i in range(0, len(text), step) for piece in splitter.feed(text[i:i + step])]
    return pieces + splitter.flush()


def test_the_first_clause_is_spoken_early_then_sentences() -> None:
    """A first clause of four words goes alone; a short first one waits for its sentence; "No." is a sentence."""
    assert spoken("It is nine o'clock, and the weather is clear. Anything else?") == [
        "It is nine o'clock,", "and the weather is clear.", "Anything else?"]
    assert spoken("Yes, sir. Done.") == ["Yes, sir.", "Done."]
    assert spoken("No. That is not how it works, sir. ") == ["No.", "That is not how it works, sir."]


def test_speech_before_a_tool_round_is_never_glued_to_what_follows() -> None:
    """boundary() speaks "Let me check." before the tool runs; the next round starts a new, separate piece."""
    splitter = SentenceSplitter(PARAMS)
    assert splitter.feed("Let me check.") == [] and splitter.boundary() == ["Let me check."]
    after = splitter.feed("Done, sir.") + splitter.flush()
    assert after == ["Done, sir."] and not any("check.Done" in piece for piece in after)


def test_long_sentences_and_code() -> None:
    """A long sentence is cut at a comma near 150 characters; code is replaced by a short note."""
    pieces = spoken("Okay. The forecast says " + ", ".join(f"item {i} is fine" for i in range(30)) + ".")
    assert pieces[0] == "Okay." and all(len(p) <= 150 for p in pieces) and pieces[1].endswith(",")
    assert spoken("Here it is: ```python\nprint(1)\n``` Done now, sir.", step=7) == [
        "Here it is:", "I've put the code on screen.", "Done now, sir."]


def test_clean_for_speech() -> None:
    """Markdown goes, links are named, symbols are read out."""
    assert clean_for_speech("**Bold** and _it_ [site](https://x.org) — 21°C, 50% at https://y.org") == (
        "Bold and it site, 21 degrees Celsius, 50 percent at the link on screen")
    assert clean_for_speech("- First item\n- `code_name` in ~/docs") == (
        "First item\ncode name in your home folder, docs")
