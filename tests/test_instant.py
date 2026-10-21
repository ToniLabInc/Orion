"""instant: Home Assistant's English sentences are matched whole (near-misses are not matched), their lists come from
Home Assistant's live context, a match becomes the tools to call or is refused, the calls run as the user's own
command and are recorded, and the turn runner answers with them (or, when they fail, with the model)."""

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from orion import instant as instant_module
from orion.activity import Activity
from orion.base import CallContext, EventBus, Tool
from orion.brain import Brain, TurnRunner
from orion.brain.context import Context
from orion.config import Config, default_config
from orion.instant import (
    Call,
    Home,
    Instant,
    Match,
    Matcher,
    Outcome,
    Plan,
    Refused,
    home_args,
    home_failed,
    home_tool,
    joined,
    parse_live_context,
    pick_timers,
    say_duration,
)
from orion.llm import LLM, Piece, Prompt
from orion.memory import MemorySystem
from orion.memory.store import Store
from orion.skills import Skills
from orion.timing import Heard

ATHENS = ZoneInfo("Europe/Athens")
HA = "home-assistant"


@pytest.fixture(scope="module")
def matcher() -> Matcher:
    """The templates of the factory intents, loaded once for every test here."""
    return Matcher.english(default_config().instant.intents)


def seen(matcher: Matcher, text: str, home: Home | None = None) -> Match | None:
    """What the matcher makes of a request said to "sir"."""
    return matcher.match(text, home, ("sir",))


# ---------------------------------------------------------------------------------------------------- matching
@pytest.mark.parametrize(("text", "intent", "slots"), [
    ("set a timer for 5 minutes", "HassStartTimer", {"minutes": 5}),
    ("Set a timer for five minutes.", "HassStartTimer", {"minutes": 5}),
    ("please set a timer for 5 minutes", "HassStartTimer", {"minutes": 5}),
    ("I'd like to set a timer for 5 minutes", "HassStartTimer", {"minutes": 5}),
    ("set a 10 minute timer called pasta", "HassStartTimer", {"minutes": 10, "name": "pasta"}),
    ("set a five-minute timer", "HassStartTimer", {"minutes": 5}),
    ("cancel the timer", "HassCancelTimer", {}),
    ("stop the timer", "HassCancelTimer", {}),
    ("cancel the pasta timer", "HassCancelTimer", {"name": "pasta"}),
    ("cancel all timers", "HassCancelAllTimers", {}),
    ("how much time is left on my timer", "HassTimerStatus", {}),
    ("add 5 minutes to the timer", "HassIncreaseTimer", {"minutes": 5}),
    ("take 1 minute off the timer", "HassDecreaseTimer", {"minutes": 1}),
    ("remove 30 seconds from the pasta timer", "HassDecreaseTimer", {"seconds": 30, "name": "pasta"}),
    ("what time is it", "HassGetCurrentTime", {}),
    ("What's the time?", "HassGetCurrentTime", {}),
    ("tell me the time", "HassGetCurrentTime", {}),
    ("what's the date", "HassGetCurrentDate", {}),
    ("what's the weather like", "HassGetWeather", {}),
    ("pause", "HassMediaPause", {}),
    ("pause the music", "HassMediaPause", {}),
    ("could you pause the music please", "HassMediaPause", {}),
    ("stop the music", "HassMediaPause", {}),
    ("resume", "HassMediaUnpause", {}),
    ("unpause the music", "HassMediaUnpause", {}),
    ("next song", "HassMediaNext", {}),
    ("skip this track", "HassMediaNext", {}),
    ("previous song", "HassMediaPrevious", {}),
    ("go back", "HassMediaPrevious", {}),
    ("turn the volume up", "HassSetVolumeRelative", {"volume_step": "up"}),
    ("volume down", "HassSetVolumeRelative", {"volume_step": "down"}),
    ("turn up the volume by 20 percent", "HassSetVolumeRelative", {"volume_step": 20}),
    ("turn down the volume by 20 percent", "HassSetVolumeRelative", {"volume_step": -20}),
    ("set the volume to 40 percent", "HassSetVolume", {"volume_level": 40}),
    ("set the volume to forty", "HassSetVolume", {"volume_level": 40}),
    ("mute", "HassMediaPlayerMute", {}),
    ("unmute", "HassMediaPlayerUnmute", {}),
    ("add milk to my shopping list", "HassShoppingListAddItem", {"item": "milk"}),
    ("put eggs on the shopping list", "HassShoppingListAddItem", {"item": "eggs"}),
    ("turn on the lights", "HassTurnOn", {"domain": "light"}),
    ("turn off all the lights", "HassTurnOff", {"domain": "light"}),
    ("set the brightness to 50 percent", "HassLightSet", {"brightness": 50}),
])
def test_a_request_is_one_of_the_sentences(matcher: Matcher, text: str, intent: str, slots: dict[str, Any]) -> None:
    """The whole request fits a template: its intent, and the slots it fills."""
    found = seen(matcher, text)
    assert found is not None and found.intent == intent
    assert {name: found.slots[name].value for name in slots} == slots


@pytest.mark.parametrize("text", [
    # a template's words with more around them, or fewer
    "set a timer", "set a timer for the pasta", "set a timer for 0 minutes", "set a timer for 150 minutes",
    "set a reminder for 5 minutes", "how do I set a timer in python", "tell me a joke about timers",
    "pause and think about it", "pause the video call", "pause the timer", "resume the timer", "skip the intro",
    "skip the intro and tell me about the weather", "next week", "cancel", "nevermind", "play", "play some music",
    # the time, the date, the weather somewhere or somewhen else
    "what time is it in Tokyo", "what time does the store close", "what time is the meeting",
    "what is the time complexity of quicksort", "what's the weather in Paris", "what's the weather tomorrow",
    "what is the weather like today",
    # volume and sound that are not this PC's speakers
    "mute the microphone", "mute the sound", "turn the volume of the tv up", "what is the volume of a cube",
    "I need to turn up the volume of my voice", "louder", "set the volume to 150",
    # lists, lights and devices that are not in the home
    "what's on my shopping list", "add an event to my calendar", "turn on the oven", "turn the lights on in the garage",
    "set the brightness of my screen to 50 percent", "lock the front door", "open the blinds in the bedroom",
    ("write a list of everything I want to buy and then add the whole thing to a very long story about my shopping "
     "trip and the people I met there and what they said"),
])
def test_a_near_miss_is_left_to_the_brain(matcher: Matcher, text: str) -> None:
    """Sentences that share words with a template, or a template's shape with other words, are not matched: a wrong
    action is worse than the brain's answer."""
    assert seen(matcher, text) is None


@pytest.mark.parametrize(("text", "seconds"), [
    ("set a timer for 5 minutes", 300), ("set a timer for 1 hour and 30 minutes", 5400),
    ("set a timer for one hour and thirty minutes", 5400), ("start a timer for 90 seconds", 90),
    ("set a timer for half an hour", 1800), ("set a timer for half a minute", 30),
    ("set a timer for 2 and a half minutes", 150), ("set a timer for 5 minutes and 30 seconds", 330),
    ("set a timer for twenty five minutes", 1500),
])
def test_a_timers_length_adds_up(matcher: Matcher, text: str, seconds: float) -> None:
    """Hours, minutes, seconds and "half" are one length, in digits or words."""
    found = seen(matcher, text)
    assert found is not None and found.seconds() == seconds


def test_a_name_two_kinds_of_device_share_has_no_kind(matcher: Matcher) -> None:
    """A device name that devices of two kinds share has no kind, so a sentence naming it can't match by name; the
    kind of device and this room remain."""
    context = {"result": "Live Context: x\n- names: Fan\n  domain: fan\n  areas: Office\n- names: Fan\n"
                         "  domain: switch\n  areas: Kitchen\n- names: Lamp\n  domain: light\n"}
    home = parse_live_context(json.dumps(context))
    assert [v.context for v in home.names.values] == [None, {"domain": "light"}]
    found = seen(matcher, "turn on the lamp", home)
    assert found is not None and found.domain == "light" and found.slots["name"].value == "Lamp"
    found = seen(matcher, "turn on the fan", home)
    assert found is not None and found.slots["domain"].value == "fan" and found.slots["area"].value == "<here>"
    assert "name" not in found.slots


def sentences(*pairs: tuple[str, str]) -> dict[str, Any]:
    """A tiny sentence file: (intent, template) pairs."""
    intents: dict[str, Any] = {}
    for intent, template in pairs:
        intents.setdefault(intent, {"data": []})["data"].append({"sentences": [template]})
    return {"language": "en", "intents": intents, "lists": {}, "expansion_rules": {}, "skip_words": []}


def test_two_equally_good_readings_that_differ_are_no_match() -> None:
    """Two intents fitting a request equally well are a guess, so there is no match; the same reading twice (two
    sentences of one intent) is one; a better fit wins."""
    both = ["HassTurnOn", "HassTurnOff"]
    ties = Matcher(sentences(("HassTurnOn", "do the thing"), ("HassTurnOff", "do the thing")), both)
    assert ties.match("do the thing", None) is None
    same = Matcher(sentences(("HassTurnOn", "do the thing"), ("HassTurnOn", "do [the] thing")), ["HassTurnOn"])
    found = same.match("do the thing", None)
    assert found is not None and found.intent == "HassTurnOn"
    better = Matcher(sentences(("HassTurnOn", "do the thing"), ("HassTurnOff", "do the thing now")), both)
    assert better.match("do the thing now", None).intent == "HassTurnOff"  # type: ignore[union-attr]
    assert better.match("do the thing", None).intent == "HassTurnOn"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------------------------------- Home Assistant
CONTEXT = json.dumps({"result": """Live Context: An overview of the areas and the devices in this smart home:
- names: Kitchen Light, Counter Light
  domain: light
  state: 'off'
  areas: Kitchen
- names: Desk Lamp
  domain: light
  state: 'on'
  areas: Office, Study
  attributes:
    brightness: '255'
- names: Living Room TV, Television
  domain: media_player
  state: playing
  areas: Living Room
- names: Coffee Machine
  domain: switch
  state: 'off'
  areas: Kitchen
- names: Front Door
  domain: lock
  state: locked
  areas: Hallway
- names: Garage Door
  domain: cover
  state: closed
- names: Home
  domain: weather
  state: sunny
- names: Shopping List
  domain: todo
  state: '3'
"""})


def test_home_assistants_names_and_areas_become_lists() -> None:
    """Each device's names and aliases give its kind; each area's first name is its own, the rest are aliases of it."""
    home = parse_live_context(CONTEXT)
    names = {v.value_out: v.context for v in home.names.values}
    assert names["Counter Light"] == {"domain": "light"} and names["Television"] == {"domain": "media_player"}
    assert names["Shopping List"] == {"domain": "todo"} and len(names) == 10
    assert {v.text_in.text.lower(): v.value_out for v in home.areas.values} == {  # type: ignore[attr-defined]
        "kitchen": "Kitchen", "office": "Office", "study": "Office", "living room": "Living Room",
        "hallway": "Hallway"}
    assert parse_live_context("Live Context: x\n- names: A\n  domain: light\n").names.values[0].value_out == "A"
    for bad in ("nothing here", "Live Context: x\n- [broken", "Live Context: x\njust text"):
        with pytest.raises(ValueError):
            parse_live_context(bad)


@pytest.mark.parametrize(("text", "intent", "slots", "domain"), [
    ("turn on the kitchen light", "HassTurnOn", {"name": "Kitchen Light"}, "light"),  # the device, not every light there
    ("turn on the counter light", "HassTurnOn", {"name": "Counter Light"}, "light"),
    ("turn off the lights in the office", "HassTurnOff", {"area": "Office", "domain": "light"}, "light"),
    ("turn off the study lamp", "HassTurnOff", {"area": "Office", "domain": "light"}, "light"),
    ("pause the living room tv", "HassMediaPause", {"name": "Living Room TV"}, "media_player"),
    ("turn off the television", "HassTurnOff", {"name": "Television"}, "media_player"),
    ("turn on the coffee machine", "HassTurnOn", {"name": "Coffee Machine"}, "switch"),
    ("set the desk lamp brightness to 40 percent", "HassLightSet", {"name": "Desk Lamp", "brightness": 40}, "light"),
    ("mute the living room tv", "HassMediaPlayerMute", {"name": "Living Room TV"}, "media_player"),
    ("set the living room tv volume to 30", "HassSetVolume", {"name": "Living Room TV", "volume_level": 30},
     "media_player"),
    ("pause the music in the living room", "HassMediaPause", {"area": "Living Room"}, ""),
    ("lock the front door", "HassTurnOn", {"name": "Front Door"}, "lock"),
    ("add milk to the shopping list", "HassListAddItem", {"item": "milk", "name": "Shopping List"}, "todo"),
])
def test_a_device_or_an_area_of_the_home_is_found(matcher: Matcher, text: str, intent: str, slots: dict[str, Any],
                                                  domain: str) -> None:
    """Names and areas come from Home Assistant, in any case; a device named beats an area and a kind."""
    found = seen(matcher, text, parse_live_context(CONTEXT))
    assert found is not None and found.intent == intent and found.domain == domain
    assert {name: found.slots[name].value for name in slots} == slots


@pytest.mark.parametrize("text", [
    "pause the tv", "turn on the kitchen", "turn off the kitchen light and the desk lamp", "turn on the ktichen light",
    "turn on the sofa lamp", "turn the lights on upstairs", "turn on the bedroom light", "turn on the home",
])
def test_what_the_home_does_not_have_is_no_match(matcher: Matcher, text: str) -> None:
    """A name that is not exactly a device's, a room it has not, or two things at once: the brain answers."""
    assert seen(matcher, text, parse_live_context(CONTEXT)) is None


# ---------------------------------------------------------------------------------------------------- pure helpers
def test_lengths_and_lists_are_said_as_a_person_would() -> None:
    """Hours and minutes (seconds only below an hour), singular and plural; "a, b and c"."""
    assert [say_duration(s) for s in (0, 1, 30, 60, 250, 3600, 3900, 7325, 5400.4)] == [
        "no time", "1 second", "30 seconds", "1 minute", "4 minutes and 10 seconds", "1 hour", "1 hour and 5 minutes",
        "2 hours and 2 minutes", "1 hour and 30 minutes"]
    assert [joined(["a"]), joined(["a", "b"]), joined(["a", "b", "c"])] == ["a", "a and b", "a, b and c"]


def timers(*items: tuple[int, str, float, float]) -> list[Any]:
    """Running timers (id, name, set, due)."""
    return [{"id": i, "text": name, "kind": "timer", "ts": ts, "due": due} for i, name, ts, due in items]


def test_the_timers_a_request_names() -> None:
    """By name (exactly), by length (within five seconds), or all of them when it names neither."""
    running = timers((1, "pasta", 0, 600), (2, "", 0, 300), (3, "Tea", 0, 180))
    assert [t["id"] for t in pick_timers(running, name="", length=0)] == [1, 2, 3]
    assert [t["id"] for t in pick_timers(running, name="pasta", length=0)] == [1]
    assert [t["id"] for t in pick_timers(running, name="tea", length=0)] == [3]
    assert [t["id"] for t in pick_timers(running, name="", length=302)] == [2]
    assert pick_timers(running, name="pas", length=0) == [] and pick_timers(running, name="pasta", length=300) == []


def tool(remote: str, properties: dict[str, Any], server: str = HA) -> Tool:
    """A tool of a connector, named as the hub names it."""
    return Tool(f"{server}__{remote}", remote, {"type": "object", "properties": properties}, server, remote)


def test_home_assistants_tool_is_found_by_the_intent_it_does() -> None:
    """Named after the intent, alone or after its integration's name; only on the connector asked for."""
    tools = [tool("intent__HassTurnOn", {}), tool("HassTurnOff", {}), tool("light__HassLightSet", {}),
             tool("intent__HassTurnOn", {}, server="other")]
    found = {name: home_tool(tools, HA, name) for name in ("HassTurnOn", "HassTurnOff", "HassLightSet", "HassSetVolume")}
    assert found["HassTurnOn"] is tools[0] and found["HassTurnOff"] is tools[1] and found["HassLightSet"] is tools[2]
    assert found["HassSetVolume"] is None and home_tool(tools, "", "HassTurnOn") is None


def match_of(intent: str, slots: dict[str, Any], kind: str = "") -> Match:
    """A match with these slot values (said, so with their words; the room of this PC is not said) of a device kind."""
    return Match(intent, {name: instant_module.Slot(value, "" if value == "<here>" else str(value))
                          for name, value in slots.items()}, kind)


def test_a_match_becomes_the_arguments_of_homes_tool() -> None:
    """Every slot must be an argument of the tool; the room of this PC and a device's kind are filled in; whole numbers
    are integers; an array argument gets a list."""
    turn_on = tool("intent__HassTurnOn", {"name": {"type": "string"}, "area": {"type": "string"},
                                          "domain": {"type": "array", "items": {"type": "string"}}})
    assert home_args(match_of("HassTurnOn", {"name": "Kitchen Light"}, "light"), turn_on, "") == {
        "name": "Kitchen Light", "domain": ["light"]}
    here = match_of("HassTurnOn", {"domain": "light", "area": "<here>"}, "light")
    assert home_args(here, turn_on, "Office") == {"domain": ["light"], "area": "Office"}
    light = tool("light__HassLightSet", {"name": {"type": "string"}, "brightness": {"type": "integer"}})
    assert home_args(match_of("HassLightSet", {"name": "Desk Lamp", "brightness": 40.0}, "light"), light, "") == {
        "name": "Desk Lamp", "brightness": 40}
    with pytest.raises(Refused, match="instant.area"):
        home_args(here, turn_on, "")
    with pytest.raises(Refused, match="takes no floor"):
        home_args(match_of("HassTurnOn", {"domain": "light", "floor": "Upstairs"}, "light"), turn_on, "")


def test_home_assistants_answer_says_whether_it_worked() -> None:
    """An error response, or targets it could not change, is a failure; anything else (even not JSON) is not."""
    assert home_failed(json.dumps({"response_type": "error", "data": {"code": "no_valid_targets"}})) == (
        "no_valid_targets")
    assert "2 of them" in home_failed(json.dumps({"response_type": "action_done",
                                                  "data": {"success": [], "failed": [{}, {}]}}))
    assert home_failed(json.dumps({"response_type": "action_done", "data": {"success": [{}], "failed": []}})) == ""
    assert home_failed("Turned on.") == "" and home_failed("[1]") == ""


# ---------------------------------------------------------------------------------------------------- the engine
class Hub:
    """A stand-in hub: it lists the tools given, and answers calls from a table (anything else: "Done.")."""

    def __init__(self, tools: Sequence[Tool] = ()) -> None:
        """Start with these tools and no calls."""
        self._tools = {t.name: t for t in tools}
        self.calls: list[tuple[str, str, dict[str, Any], CallContext]] = []
        self.answers: dict[str, str] = {}

    def tools(self) -> dict[str, Tool]:
        """The tools advertised."""
        return self._tools

    async def call(self, server: str, tool: str, args: dict[str, Any], ctx: CallContext) -> str:
        """Note the call and answer it."""
        self.calls.append((server, tool, args, ctx))
        return self.answers.get(tool, "Done.")


class Clock:
    """The module's time, fixed (wall time, and the rest as it is)."""

    def __init__(self, now: float) -> None:
        """The wall clock reads `now`."""
        self.now = now

    def time(self) -> float:
        """The fixed wall time."""
        return self.now

    monotonic = staticmethod(__import__("time").monotonic)
    perf_counter = staticmethod(__import__("time").perf_counter)


FORECAST = {"place": "Athens, Greece", "temp": 21, "condition": "partly cloudy", "days": [
    {"max": 24, "min": 15, "condition": "clear sky", "rain_chance": 0, "date": "", "code": 0, "sunrise": "",
     "sunset": ""}]}


class Rig:
    """Instant commands over a stand-in hub, a real store and activity log, and the factory settings."""

    def __init__(self, tmp_path: Path, matcher: Matcher, monkeypatch: pytest.MonkeyPatch, *,
                 tools: Sequence[Tool] = (), area: str = "", city: str = "Athens", forecast: Any = FORECAST) -> None:
        """Make it; the templates are the shared matcher (loading them again for every test would waste seconds)."""
        self.cfg: Config = default_config()
        self.cfg.instant.area = area
        self.store = Store(tmp_path / "orion.db", self.cfg.memory, role="tools")
        self.store.ensure_schema()
        self.events: list[dict[str, Any]] = []
        self.hub = Hub(tools)
        self.clock = Clock(__import__("time").time())  # the store stamps a timer when it is set, by the real clock
        self.spawned: list[str] = []
        self.forecast = forecast
        monkeypatch.setattr(instant_module, "time", self.clock)
        monkeypatch.setattr(instant_module, "local_now", lambda: datetime(2026, 10, 11, 14, 5, tzinfo=ATHENS))
        self.instant = Instant(self.cfg, self.hub, self.store, weather=self._weather, home_city=lambda: city,
                               activity=Activity(self.store, lambda kind, **data: self.events.append(data),
                                                 redact=lambda text: text),
                               spawn=self._spawn)
        self.instant._matcher, self.instant._loaded = matcher, tuple(self.cfg.instant.intents)

    async def _weather(self, city: str) -> Any:
        """The forecast (or what goes wrong with it)."""
        if isinstance(self.forecast, Exception):
            raise self.forecast
        return self.forecast

    def _spawn(self, job: Any, *, name: str) -> "asyncio.Task[Any]":
        """Run background work as the runtime does, noting it."""
        self.spawned.append(name)
        return asyncio.get_running_loop().create_task(job, name=name)

    def timer(self, name: str, seconds: float) -> int:
        """Start a timer in the store that was set a moment ago."""
        return self.store.add_reminder(name, self.clock.now + seconds, "timer")

    async def plan(self, text: str) -> Plan | None:
        """What a request does, as the turn runner asks (nothing is run)."""
        found = await self.instant.match(text)
        return await self.instant.plan(found, text) if found is not None else None

    def close(self) -> None:
        """Close the store."""
        self.store.close()


@pytest.fixture
def rig(tmp_path: Path, matcher: Matcher, monkeypatch: pytest.MonkeyPatch) -> Rig:
    """Instant commands with nothing connected, and no timers running."""
    made = Rig(tmp_path, matcher, monkeypatch)
    yield made  # type: ignore[misc]
    made.close()


def test_timers_are_started_with_their_length_and_name(rig: Rig) -> None:
    """Orion's own set_timer, a name without its article, and his reply naming the timer."""

    async def go() -> None:
        """Three requests."""
        plan = await rig.plan("set a timer for 5 minutes")
        assert plan is not None and plan.calls == (Call("organizer", "set_timer", {"duration": "300 seconds",
                                                                                    "label": ""}),)
        assert plan.reply == "Your 5-minute timer is running, sir." and plan.intent == "HassStartTimer"
        named = await rig.plan("set a 10 minute timer for the pasta")
        assert named is not None and named.calls[0].args == {"duration": "600 seconds", "label": "pasta"}
        assert named.reply == "Your pasta timer is running, sir."
        long = await rig.plan("set a timer for one hour and thirty minutes")
        assert long is not None and long.calls[0].args["duration"] == "5400 seconds"
        assert long.reply == "Your 1 hour 30-minute timer is running, sir."

    asyncio.run(go())


def test_timers_are_cancelled_listed_and_changed(rig: Rig) -> None:
    """From the running timers in the database: the one named or the only one, all of them, how long each has left,
    time added or taken off; no timer running is an answer; two that fit are the brain's question."""
    pasta, tea = rig.timer("pasta", 600), rig.timer("", 300)

    async def go() -> None:
        """Every request on two timers."""
        cancel = await rig.plan("cancel the pasta timer")
        assert cancel is not None and cancel.calls == (Call("organizer", "cancel_timer_or_reminder",
                                                            {"which": f"#{pasta}"}),)
        assert cancel.reply == "Your pasta timer is cancelled, sir."
        assert await rig.plan("cancel the timer") is None  # two fit: he asks which
        assert await rig.plan("cancel the laundry timer") is None  # none is called that
        every = await rig.plan("cancel all timers")  # in the order they end
        assert every is not None and [c.args for c in every.calls] == [{"which": f"#{tea}"}, {"which": f"#{pasta}"}]
        assert every.reply == "Cancelled your 5-minute timer and your pasta timer, sir."
        status = await rig.plan("how much time is left on my timer")
        assert status is not None and status.calls == () and status.reply == (
            "Your 5-minute timer has 5 minutes left. Your pasta timer has 10 minutes left.")
        one = await rig.plan("how long is left on the pasta timer")
        assert one is not None and one.reply == "Your pasta timer has 10 minutes left."
        more = await rig.plan("add 2 minutes to the pasta timer")
        assert more is not None and more.calls == (Call("organizer", "change_timer", {"which": f"#{pasta}",
                                                                                     "by": "+120 seconds"}),)
        assert more.reply == "Your pasta timer now has 12 minutes left, sir."
        less = await rig.plan("take 30 seconds off the pasta timer")
        assert less is not None and less.calls[0].args["by"] == "-30 seconds"
        assert less.reply == "Your pasta timer now has 9 minutes and 30 seconds left, sir."
        assert await rig.plan("add 2 minutes to the timer") is None  # which one?

    asyncio.run(go())


def test_with_no_timer_running_that_is_the_answer(rig: Rig) -> None:
    """Cancelling, asking or changing a timer when none runs is answered at once, calling nothing."""

    async def go() -> None:
        """Four requests."""
        for text in ("cancel the timer", "cancel all timers", "how much time is left on my timer",
                     "add 5 minutes to the timer"):
            plan = await rig.plan(text)
            assert plan is not None and plan.calls == () and plan.reply == "There's no timer running, sir.", text

    asyncio.run(go())


def test_a_timer_that_runs_a_command_is_the_brains(rig: Rig) -> None:
    """"Remind me to call mom in 10 minutes" fits the timer template with a command to run: it is a reminder, which
    the model sets; so are the other requests that would carry a command out later."""

    async def go() -> None:
        """Three requests that match, and are refused."""
        for text in ("remind me to call mom in 10 minutes", "turn off the lights in 5 minutes",
                     "wake me up in 5 minutes"):
            found = await rig.instant.match(text)
            assert found is not None and found.intent == "HassStartTimer", text
            assert await rig.instant.plan(found, text) is None, text

    asyncio.run(go())


def test_the_time_the_date_and_the_weather_are_said(rig: Rig) -> None:
    """From the clock and the weather service; the weather of the home city needs the city, the service and the place."""

    async def go() -> None:
        """Three answers, then what stops the weather."""
        assert (await rig.plan("what time is it")).reply == "It's 14:05, sir."  # type: ignore[union-attr]
        assert (await rig.plan("what's the date")).reply == "It's Sunday 11 October, sir."  # type: ignore[union-attr]
        weather = await rig.plan("what's the weather like")
        assert weather is not None and weather.calls == ()
        assert weather.reply == "It's 21 degrees and partly cloudy in Athens, sir, with a high of 24."
        for forecast in (httpx.ConnectError("no network"), None):
            rig.forecast = forecast
            assert await rig.plan("what's the weather like") is None

    asyncio.run(go())


def test_the_weather_needs_a_home_city(tmp_path: Path, matcher: Matcher, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without one the brain asks where he is."""
    nowhere = Rig(tmp_path, matcher, monkeypatch, city="")
    assert asyncio.run(nowhere.plan("what's the weather like")) is None
    nowhere.close()


def test_media_and_sound_are_this_pcs(rig: Rig) -> None:
    """The system's media and volume tools, whatever is named by default; a number or the usual step."""

    async def go() -> None:
        """Every sound request."""
        expected = {
            "pause the music": ("media", {"action": "pause"}, "Paused, sir."),
            "resume": ("media", {"action": "play"}, "Playing, sir."),
            "next song": ("media", {"action": "next"}, "Next track."),
            "previous song": ("media", {"action": "previous"}, "Back a track."),
            "set the volume to 40 percent": ("volume", {"action": "set", "level": 40}, "Volume at 40 percent, sir."),
            "set the volume to 0": ("volume", {"action": "set", "level": 0}, "Volume at 0 percent, sir."),
            "turn the volume up": ("volume", {"action": "up"}, "Louder."),
            "turn down the volume": ("volume", {"action": "down"}, "Quieter."),
            "turn up the volume by 20 percent": ("volume", {"action": "up", "level": 20}, "Louder."),
            "turn down the volume by 5 percent": ("volume", {"action": "down", "level": 5}, "Quieter."),
            "mute": ("volume", {"action": "mute"}, "Muted, sir."),
            "unmute": ("volume", {"action": "unmute"}, "Sound's back, sir."),
        }
        for text, (name, args, reply) in expected.items():
            plan = await rig.plan(text)
            assert plan is not None and plan.calls == (Call("system", name, args),) and plan.reply == reply, text

    asyncio.run(go())


def test_the_shopping_list_is_the_to_do_list(rig: Rig) -> None:
    """Orion's own to-do list, the item as said."""
    plan = asyncio.run(rig.plan("add milk to my shopping list"))
    assert plan is not None and plan.calls == (Call("organizer", "todo", {"action": "add", "item": "milk"}),)
    assert plan.reply == "Milk is on your list, sir."


HOME_TOOLS = [
    tool("intent__HassTurnOn", {"name": {"type": "string"}, "area": {"type": "string"}, "floor": {"type": "string"},
                                "domain": {"type": "array", "items": {"type": "string"}}}),
    tool("intent__HassTurnOff", {"name": {"type": "string"}, "area": {"type": "string"},
                                 "domain": {"type": "array", "items": {"type": "string"}}}),
    tool("light__HassLightSet", {"name": {"type": "string"}, "area": {"type": "string"},
                                 "brightness": {"type": "integer"}, "domain": {"type": "array"}}),
    tool("media_player__HassMediaPause", {"name": {"type": "string"}, "area": {"type": "string"}}),
    tool("media_player__HassSetVolume", {"name": {"type": "string"}, "volume_level": {"type": "integer"}}),
    tool("GetLiveContext", {}),
]


def test_lights_and_devices_are_home_assistants(tmp_path: Path, matcher: Matcher,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """The connector's tool of the same intent with the slots as its arguments, the room of this PC when none is said
    (instant.area), and a short reply; each thing it can't do exactly is refused."""
    rig = Rig(tmp_path, matcher, monkeypatch, tools=HOME_TOOLS, area="Office")
    rig.instant._home = parse_live_context(CONTEXT)
    rig.instant._home_at = rig.clock.monotonic()

    async def go() -> None:
        """Requests the home does, and ones it must not."""
        light = await rig.plan("turn on the kitchen light")
        assert light is not None and light.reply == "On, sir." and light.calls == (Call(
            HA, "intent__HassTurnOn", {"name": "Kitchen Light", "domain": ["light"]}, home=True),)
        room = await rig.plan("turn off the lights")  # this PC's room
        assert room is not None and room.calls[0].args == {"domain": ["light"], "area": "Office"}
        assert room.reply == "Off, sir."
        dimmed = await rig.plan("set the desk lamp brightness to 40 percent")
        assert dimmed is not None and dimmed.calls[0].args == {"name": "Desk Lamp", "brightness": 40,
                                                               "domain": ["light"]} and dimmed.reply == "Done, sir."
        tv = await rig.plan("pause the living room tv")
        assert tv is not None and tv.calls == (Call(HA, "media_player__HassMediaPause", {"name": "Living Room TV"},
                                                    home=True),)
        loud = await rig.plan("set the living room tv volume to 30")
        assert loud is not None and loud.calls[0].args == {"name": "Living Room TV", "volume_level": 30}
        music = await rig.plan("pause the music in the living room")  # a room named: not this PC's media
        assert music is not None and music.calls == (Call(HA, "media_player__HassMediaPause",
                                                          {"area": "Living Room"}, home=True),)
        # not instant: a lock and a cover are not on instant.device_domains; a tool Home Assistant lacks; a slot its
        # tool has no argument for; the weather of its own entity
        assert await rig.plan("lock the front door") is None
        assert await rig.plan("unmute the living room tv") is None
        assert await rig.plan("turn on the lights upstairs") is None
        assert await rig.plan("what's the weather for home") is None

    asyncio.run(go())
    rig.close()


def test_home_assistant_needs_its_connector_and_the_room(tmp_path: Path, matcher: Matcher,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """Not connected, or no room set for this PC: "turn on the lights" is the brain's, who can ask which."""
    unplugged = Rig(tmp_path / "a", matcher, monkeypatch, area="Office")
    assert asyncio.run(unplugged.plan("turn on the lights")) is None
    unplugged.close()
    no_room = Rig(tmp_path / "b", matcher, monkeypatch, tools=HOME_TOOLS)
    assert asyncio.run(no_room.plan("turn on the lights")) is None
    no_room.close()


# ---------------------------------------------------------------------------------------------------- running them
def test_a_plan_runs_as_the_users_own_command_and_is_recorded(rig: Rig) -> None:
    """The calls go through the hub with origin "user" for this turn and source; the reply comes back; the activity log
    says what was heard and done."""

    async def go() -> None:
        """Run one."""
        plan = await rig.plan("set a timer for 5 minutes")
        assert plan is not None
        outcome = await rig.instant.execute(plan, CallContext("user", "t1", source="voice"))
        assert outcome.reply == plan.reply and outcome.why == "" and outcome.ms >= 0
        [(server, name, args, ctx)] = rig.hub.calls
        assert (server, name, ctx) == ("organizer", "set_timer", CallContext("user", "t1", source="voice"))
        assert args == {"duration": "300 seconds", "label": ""}

    asyncio.run(go())
    rig.store.submit(lambda: None).result()
    [row] = rig.store.activity()
    assert (row["kind"], row["source"], row["title"], row["outcome"], row["turn"]) == (
        "instant", "voice", "Started a timer", "ok", "t1")
    assert (row["connector"], row["tool"], row["actor"]) == ("organizer", "set_timer", "you, without the model")
    assert "heard: set a timer for 5 minutes" in row["detail"] and "HassStartTimer" in row["detail"]


def test_a_call_that_fails_hands_the_request_to_the_brain(rig: Rig) -> None:
    """An "Error" answer stops the plan where it is and gives no reply; the log says why."""
    rig.timer("pasta", 600)
    rig.timer("", 300)
    rig.hub.answers["cancel_timer_or_reminder"] = "Error: There's no timer or reminder #7 running."

    async def go() -> Outcome:
        """Cancel both; the first call fails."""
        plan = await rig.plan("cancel all timers")
        assert plan is not None and len(plan.calls) == 2
        return await rig.instant.execute(plan, CallContext("user", "t2", source="hud"))

    outcome = asyncio.run(go())
    assert outcome.reply == "" and outcome.why == "There's no timer or reminder #7 running."
    assert len(rig.hub.calls) == 1  # the second wasn't tried
    rig.store.submit(lambda: None).result()
    [row] = rig.store.activity()
    assert row["outcome"] == "failed" and "#7 running. (the brain took it)" in row["detail"]


def test_home_assistant_saying_it_failed_is_a_failure(tmp_path: Path, matcher: Matcher,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """Its answer is JSON: an error response, or targets it could not change, hands the request on."""
    rig = Rig(tmp_path, matcher, monkeypatch, tools=HOME_TOOLS)
    rig.instant._home = parse_live_context(CONTEXT)
    rig.instant._home_at = rig.clock.monotonic()

    async def go() -> list[Outcome]:
        """The same command, answered three ways."""
        plan = await rig.plan("turn on the kitchen light")
        assert plan is not None
        outcomes = []
        for answer in ('{"response_type": "error", "data": {"code": "no_valid_targets"}}',
                       '{"response_type": "action_done", "data": {"success": [], "failed": [{"name": "x"}]}}',
                       '{"response_type": "action_done", "data": {"success": [{"name": "x"}], "failed": []}}'):
            rig.hub.answers["intent__HassTurnOn"] = answer
            outcomes.append(await rig.instant.execute(plan, CallContext("user", "t3", source="voice")))
        return outcomes

    first, second, third = asyncio.run(go())
    assert first.why == "no_valid_targets" and "1 of them" in second.why and third.why == "" and third.reply
    rig.close()


# ---------------------------------------------------------------------------------------------------- guard rails
def test_what_is_left_to_the_brain_without_a_match(rig: Rig) -> None:
    """Switched off, templates not loaded, a stop phrase, the wake name or his honorific around a request."""

    async def go() -> None:
        """The guards, one at a time."""
        assert (await rig.instant.match("what time is it")) is not None
        assert (await rig.instant.match("Orion, what time is it?")).intent == "HassGetCurrentTime"  # type: ignore[union-attr]
        assert (await rig.instant.match("hey Orion set a timer for 5 minutes")).intent == "HassStartTimer"  # type: ignore[union-attr]
        assert (await rig.instant.match("what time is it, sir?")).intent == "HassGetCurrentTime"  # type: ignore[union-attr]
        assert await rig.instant.match("stop") is None  # his own stop, not "stop the music"
        assert await rig.instant.match("Orion") is None and await rig.instant.match("thank you") is None
        rig.cfg.instant.enabled = False
        assert await rig.instant.match("what time is it") is None
        rig.cfg.instant.enabled = True
        rig.instant._matcher = None
        assert await rig.instant.match("what time is it") is None

    asyncio.run(go())


def test_changed_intents_are_loaded_again(rig: Rig) -> None:
    """Editing instant.intents applies at the next request: the templates load in the background, and until they do
    the brain answers."""

    async def go() -> None:
        """Take one intent away."""
        rig.cfg.instant.intents = ["HassGetCurrentTime"]
        assert await rig.instant.match("what time is it") is None  # not loaded yet
        assert rig.spawned == ["load instant commands"]
        await rig.instant._loading  # type: ignore[misc]
        assert (await rig.instant.match("what time is it")) is not None
        assert await rig.instant.match("pause the music") is None and await rig.instant.match("cancel the timer") is None

    asyncio.run(go())


def test_templates_that_cannot_load_leave_everything_to_the_brain(rig: Rig, monkeypatch: pytest.MonkeyPatch,
                                                                  caplog: pytest.LogCaptureFixture) -> None:
    """A failed load is logged once, and no request is matched (or retried until the intents change)."""

    def broken(intents: Any) -> Matcher:
        """No templates."""
        raise RuntimeError("no sentences")

    monkeypatch.setattr(Matcher, "english", broken)
    rig.instant._matcher, rig.instant._loaded = None, None

    async def go() -> None:
        """Load, then ask."""
        await rig.instant.load()
        assert await rig.instant.match("what time is it") is None
        assert rig.spawned == []

    asyncio.run(go())
    assert "instant commands couldn't load" in caplog.text


def test_homes_names_are_read_when_it_connects_and_dropped_when_it_goes(tmp_path: Path, matcher: Matcher,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """The toolbox changing is when to ask for its live context (once at a time); a failed read keeps what was known;
    losing the connector forgets it."""
    rig = Rig(tmp_path, matcher, monkeypatch, tools=HOME_TOOLS)
    rig.hub.answers["GetLiveContext"] = CONTEXT

    async def go() -> None:
        """Connect, fail to refresh, disconnect."""
        rig.instant.tools_changed()
        rig.instant.tools_changed()  # a second change while the first read is under way adds none
        await rig.instant._home_task  # type: ignore[misc]
        assert [call[1] for call in rig.hub.calls] == ["GetLiveContext"] and rig.hub.calls[0][3].origin == "hud"
        assert (await rig.instant.match("turn on the kitchen light")).slots["name"].value == "Kitchen Light"  # type: ignore[union-attr]
        rig.hub.answers["GetLiveContext"] = "Error: unreachable"
        rig.clock.now += 10_000
        rig.instant._home_at = 0.0
        rig.instant._read_home()
        await rig.instant._home_task  # type: ignore[misc]
        assert rig.instant._home is not None
        rig.hub._tools = {}
        rig.instant.tools_changed()
        assert rig.instant._home is None and await rig.instant.match("turn on the kitchen light") is None

    asyncio.run(go())
    rig.close()


# ---------------------------------------------------------------------------------------------------- the turn runner
class Model(LLM):
    """A stand-in brain that must not be asked unless the test says so."""

    def __init__(self, rounds: list[list[Piece]] | None = None) -> None:
        """Answer these rounds in order."""
        super().__init__(default_config())
        self.rounds = rounds or []
        self.asked = 0

    @property
    def model(self) -> str:
        """Its name."""
        return "stand-in"

    async def stream(self, prompt: Prompt) -> AsyncIterator[Piece]:
        """The next round's pieces."""
        self.asked += 1
        for piece in self.rounds.pop(0):
            yield piece

    async def json(self, prompt: str, schema: Any, *, temperature: float) -> dict[str, Any]:
        """Not used."""
        return {}


class NoModel:
    """An embedder without a model."""

    id = ""
    loaded = False

    def load(self) -> bool:
        """Nothing to load."""
        return False

    def encode(self, texts: object) -> None:
        """No vectors."""

    def encode_query(self, text: str) -> None:
        """No vector."""


class Voice:
    """A stand-in voice: what it is given is kept, and its first piece is ready and sounds at once."""

    speaking = False

    def __init__(self) -> None:
        """Nothing said yet."""
        self.said: list[str] = []
        self.clock: Any = None

    def reply_stream(self, turn: str, *, spoken: bool, clock: Any = None) -> "Voice":
        """Itself, told the clock."""
        self.clock = clock
        return self

    def feed(self, delta: str) -> None:
        """A piece of the reply is ready, and sounds."""
        self.said.append(delta)
        self.clock.ready()
        self.clock.sound(__import__("time").monotonic())

    def boundary(self) -> None:
        """A round ends."""

    def close(self) -> None:
        """The reply is complete."""

    async def reply_done(self, turn: str, source: str) -> None:
        """After the reply."""

    def announce(self, text: str) -> None:
        """Not used."""

    def chime(self, kind: str) -> None:
        """Not used."""

    def stop_speaking(self) -> None:
        """Silence."""


class Quick:
    """A stand-in for instant commands: one sentence, answered or failing."""

    def __init__(self, *, fails: bool = False, breaks: bool = False) -> None:
        """Answer "set a timer for 5 minutes"; `fails`: its call fails; `breaks`: it raises."""
        self.fails, self.breaks = fails, breaks
        self.contexts: list[CallContext] = []

    async def match(self, text: str) -> Match | None:
        """The one sentence."""
        if self.breaks:
            raise RuntimeError("boom")
        return Match("HassStartTimer", {}) if text == "set a timer for 5 minutes" else None

    async def plan(self, match: Match, said: str) -> Plan:
        """Start the timer."""
        return Plan(match.intent, said, (Call("organizer", "set_timer", {}),), "Your 5-minute timer is running, sir.")

    async def execute(self, plan: Plan, ctx: CallContext) -> Outcome:
        """Run it, taking 12 ms."""
        self.contexts.append(ctx)
        await asyncio.sleep(0.012)
        return Outcome("", "boom", 12.0) if self.fails else Outcome(plan.reply, "", 12.0)


def turn(tmp_path: Path, request: str, quick: Quick | None, rounds: list[list[Piece]] | None = None,
         heard: Heard | None = None) -> tuple[list[dict[str, Any]], Voice, Brain, Model, Any]:
    """One request through a real turn runner and brain over stand-ins."""
    cfg = default_config()
    memory = MemorySystem.open(cfg, NoModel(), role="main")  # type: ignore[arg-type]
    skills = Skills(tmp_path / "skills")
    events: list[dict[str, Any]] = []
    bus = EventBus()
    bus.subscribe(events.append)
    voice, model = Voice(), Model(rounds)
    hub = SimpleNamespace(ops=SimpleNamespace(record=lambda **step: None, turn_steps=lambda t: []),
                          end_turn=lambda t: None)

    def spawn(job: Any, *, name: str) -> Any:
        """Run background work."""
        return asyncio.get_running_loop().create_task(job, name=name)

    brain = Brain(cfg, memory, skills, NoModel(), Context(cfg, memory, skills, NoModel()), hub,  # type: ignore[arg-type]
                  model, spawn)

    async def go() -> Any:
        """Run the turn, then the speech."""
        runner = TurnRunner(cfg, bus, memory, brain, hub, voice, None, spawn, instant=quick)  # type: ignore[arg-type]
        result = await runner.handle(request, "voice" if heard else "hud", heard=heard)
        await runner.idle()
        return result

    result = asyncio.run(go())
    memory.close()
    return events, voice, brain, model, result


def test_an_instant_command_answers_without_the_model(tmp_path: Path) -> None:
    """The reply is spoken and shown like any other, its events marked instant, the exchange is in the history the
    model reads next, and the turn is timed as instant."""
    quick = Quick()
    events, voice, brain, model, result = turn(tmp_path, "set a timer for 5 minutes", quick)
    reply = "Your 5-minute timer is running, sir."
    assert model.asked == 0 and voice.said == [reply] and result.text == reply and not result.interrupted
    kinds = [e["type"] for e in events]
    assert kinds[:3] == ["user_message", "state", "assistant_start"] and "timing" in kinds
    [start] = [e for e in events if e["type"] == "assistant_start"]
    [delta] = [e for e in events if e["type"] == "assistant_delta"]
    [done] = [e for e in events if e["type"] == "assistant_done"]
    assert start["instant"] is True and (delta["text"], delta["instant"]) == (reply, True)
    assert (done["text"], done["instant"], done["rounds"]) == (reply, True, 0)
    assert [(m["role"], m["content"]) for m in brain.history.messages()] == [
        ("user", "set a timer for 5 minutes"), ("assistant", reply)]
    assert quick.contexts == [CallContext("user", result.turn, source="hud")]
    [row] = [e for e in events if e["type"] == "timing"]
    assert (row["turn"], row["brain"], row["instant"], row["tools_ms"]) == (result.turn, "instant", True, 12)
    assert row["heard_ms"] == 0 and row["total_ms"] >= row["think_ms"] + row["tools_ms"]


def test_a_spoken_instant_command_counts_from_the_end_of_speech(tmp_path: Path) -> None:
    """The hearing time is from the last speech to the words, on the clock the voice reports."""
    now = __import__("time").monotonic()
    events, *_ = turn(tmp_path, "set a timer for 5 minutes", Quick(), heard=Heard(said=now - 0.8, heard=now - 0.3))
    [row] = [e for e in events if e["type"] == "timing"]
    assert row["source"] == "voice" and row["heard_ms"] == 500 and row["total_ms"] >= 800


def test_a_command_that_fails_is_answered_by_the_model(tmp_path: Path) -> None:
    """The call failed: no instant reply, no mark, the model answers the request as if instant commands were off."""
    events, voice, _, model, _ = turn(tmp_path, "set a timer for 5 minutes", Quick(fails=True),
                                      [["Done, sir, five minutes."]])
    assert model.asked == 1 and voice.said == ["Done, sir, five minutes."]
    [start] = [e for e in events if e["type"] == "assistant_start"]
    assert "instant" not in start and not any(e.get("instant") for e in events)
    [row] = [e for e in events if e["type"] == "timing"]
    assert row["instant"] is False and row["brain"] != "instant"


@pytest.mark.parametrize(("quick", "request_text"), [
    (Quick(breaks=True), "set a timer for 5 minutes"), (Quick(), "what is the capital of France"), (None, "hello"),
])
def test_anything_else_or_any_trouble_goes_to_the_model(tmp_path: Path, quick: Quick | None, request_text: str) -> None:
    """Not a command, no instant commands, or a bug in them: the model answers and the turn does not fail."""
    events, voice, _, model, result = turn(tmp_path, request_text, quick, [["Hello."]])
    assert model.asked == 1 and voice.said == ["Hello."] and not any(e.get("error") for e in events)
    assert result.text == "Hello."


def test_a_request_with_a_picture_is_never_instant(tmp_path: Path) -> None:
    """A picture is part of the request, which only the model can read."""
    cfg = default_config()
    memory = MemorySystem.open(cfg, NoModel(), role="main")  # type: ignore[arg-type]
    quick = Quick()
    runner = TurnRunner(cfg, EventBus(), memory, SimpleNamespace(), SimpleNamespace(), Voice(), None,  # type: ignore[arg-type]
                        lambda job, *, name: job, instant=quick)  # type: ignore[arg-type]
    picture = {"mime": "image/png", "data": "", "label": "x"}

    async def go() -> str | None:
        """Ask for the instant answer with a picture."""
        return await runner._instantly("set a timer for 5 minutes", "hud", "", [picture], "t", SimpleNamespace())  # type: ignore[arg-type]

    assert asyncio.run(go()) is None and quick.contexts == []
    memory.close()


def test_an_instant_commands_steps_are_kept_with_the_conversation(tmp_path: Path) -> None:
    """The steps table keeps "brain" and "app" only: the tool calls of an instant command (origin "user") are kept as
    the turn's own, so a reloaded conversation still shows what was done; the HUD's own calls are not kept."""
    from orion.base import Conv, StepRecord
    from orion.runtime import Runtime

    store = Store(tmp_path / "orion.db", default_config().memory, role="tools")
    store.ensure_schema()
    me = SimpleNamespace(memory=SimpleNamespace(store=store), brain=SimpleNamespace(conv=Conv(1, 0.0)))

    def record(step: str, source: str) -> StepRecord:
        """A finished tool call of this origin."""
        return StepRecord(id=step, turn="t1", kind="tool", name="set_timer", title="Start a timer", server="organizer",
                          args={}, source=source, client="", at=1.0, ms=3, ok=True, result="Timer #1 started.",  # type: ignore[typeddict-item]
                          taints=False, memory=None)

    for step, source in (("a", "user"), ("b", "brain"), ("c", "app"), ("d", "hud")):
        Runtime._record(me, record(step, source))  # type: ignore[arg-type]
    store.submit(lambda: None).result()
    with store.reading() as db:
        kept = {row["id"]: row["source"] for row in db.execute("SELECT id, source FROM steps")}
    assert kept == {"a": "brain", "b": "brain", "c": "app"}
    store.close()
