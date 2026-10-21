"""What gestures do: the actions and their options, the defaults, the bindings a server's settings give, and when a
held hand shape fires (its hold, its repeats, gestures asleep until woken)."""

import pytest
from pydantic import ValidationError

from orion import gestures as g
from orion.config import default_config
from orion.gestures import Binding, Firing, bindings_of, check_bindings, describe, parse_binding
from orion.voice.wake import HUD_PARTS

DEFAULTS = default_config().gestures.defaults  # config.yaml's gestures.defaults, as shipped
DEFAULT_BINDINGS = g.default_bindings(DEFAULTS)


def test_the_defaults_use_orions_features_but_not_listening_or_stopping() -> None:
    assert {"listen", "stop"}.isdisjoint(g.ACTIONS)
    assert {b.do for b in DEFAULT_BINDINGS.values()} >= {"media", "volume", "toggle", "show", "hide", "mic",
                                                         "new_chat", "skill", "dismiss"}
    assert all(parse_binding(b.as_dict()) == b for b in DEFAULT_BINDINGS.values())  # they round-trip
    assert DEFAULT_BINDINGS["thumb_up"] == Binding("volume", {"action": "up"}, repeat=True)
    assert DEFAULT_BINDINGS["closed_fist"].hold == 1.0
    assert set(DEFAULT_BINDINGS["hands_apart"].params["panels"]) == set(g.PANELS)  # everything but the hands view
    assert "hands" in HUD_PARTS and "settings/abilities" in HUD_PARTS and "settings/gestures" not in HUD_PARTS


def test_the_defaults_and_what_actions_start_with_are_checked_when_the_settings_load() -> None:
    cfg = default_config()
    starts = cfg.gestures.starts
    assert all(parse_binding({"do": do, **start}) for do, start in starts.items())
    for wrong, message in [({"defaults": {"victory": {"do": "fly"}}}, "victory: unknown action"),
                           ({"starts": {"fly": {}}}, "fly is not an action"),
                           ({"starts": {"say": {"text": "hi", "hold": 1}}}, "its options only"),
                           ({"starts": {"timer": {"minutes": 0}}}, "timer: timer.minutes"),
                           ({"awake_seconds": [1]}, "greater than or equal to 2")]:
        with pytest.raises(ValidationError, match=message):
            type(cfg.gestures).model_validate({**cfg.gestures.model_dump(), **wrong})


def test_a_new_binding_starts_from_the_settings_or_its_first_choice() -> None:
    starts = default_config().gestures.starts
    tools = [("system", "open"), ("home", "lights")]
    assert g.fresh("toggle", starts, skills=[], tools=tools) == {"do": "toggle", "panel": "comms"}
    assert g.fresh("media", starts, skills=[], tools=tools) == {"do": "media", "action": "play_pause"}
    assert g.fresh("hide", starts, skills=[], tools=tools) == {"do": "hide"}  # its panels are optional: everything
    assert g.fresh("skill", starts, skills=["morning-briefing"], tools=tools) == {"do": "skill",
                                                                                   "name": "morning-briefing"}
    assert g.fresh("tool", starts, skills=[], tools=tools) == {"do": "tool", "server": "system", "tool": "open"}
    assert g.fresh("skill", starts, skills=[], tools=tools) is None  # nothing to run yet
    assert g.fresh("tool", starts, skills=[], tools=[]) is None
    assert g.fresh("say", {}, skills=[], tools=tools) is None  # a text nobody gave
    assert all(parse_binding(b) for do in g.ACTIONS
               if (b := g.fresh(do, starts, skills=["x"], tools=tools)) is not None)
    assert g.choice_words("mic", "toggle") == "Microphone off / on" and g.choice_words("media", "next") == "Next track"


@pytest.mark.parametrize(("raw", "expected"), [
    ({"do": "media", "action": "next"}, Binding("media", {"action": "next"})),
    ({"do": "show", "panels": ["Weather", "weather", "news"]}, Binding("show", {"panels": ("weather", "news")})),
    ({"do": "hide"}, Binding("hide", {"panels": ()})),
    ({"do": "toggle", "panel": "settings/voice", "hold": 0.6}, Binding("toggle", {"panel": "settings/voice"}, 0.6)),
    ({"do": "say", "text": "  what's   the time  "}, Binding("say", {"text": "what's the time"})),
    ({"do": "timer", "minutes": 5}, Binding("timer", {"minutes": 5})),
    ({"do": "arm", "seconds": 10}, Binding("arm", {"seconds": 10})),
    ({"do": "tool", "server": "system", "tool": "open", "args": {"target": "spotify"}},
     Binding("tool", {"server": "system", "tool": "open", "args": {"target": "spotify"}})),
    ({"do": "tool", "server": "home", "tool": "lights_off"}, Binding("tool", {"server": "home", "tool": "lights_off",
                                                                             "args": {}})),
    ({"do": "volume", "action": "down", "repeat": True}, Binding("volume", {"action": "down"}, repeat=True)),
])
def test_bindings_are_checked_and_made_plain(raw: dict, expected: Binding) -> None:
    assert parse_binding(raw) == expected
    assert parse_binding(expected.as_dict()) == expected


@pytest.mark.parametrize(("raw", "message"), [
    ("media", "a mapping"),
    ({"do": "listen"}, "unknown action"),
    ({"do": "media"}, "media needs action"),
    ({"do": "media", "action": "eject"}, "media.action is one of"),
    ({"do": "media", "action": "next", "volume": 3}, "not volume"),
    ({"do": "show", "panels": []}, "at least one"),
    ({"do": "show", "panels": ["desktop"]}, "no desktop"),
    ({"do": "toggle", "panel": "garage"}, "toggle.panel is one of"),
    ({"do": "say", "text": ""}, "a text of"),
    ({"do": "timer", "minutes": 0}, "minutes from 1"),
    ({"do": "arm"}, "arm needs seconds"),
    ({"do": "skill", "name": "Morning Briefing!"}, "a skill's name"),
    ({"do": "tool", "server": "system"}, "tool needs tool"),
    ({"do": "tool", "server": "sys tem", "tool": "open"}, "a name of"),
    ({"do": "tool", "server": "system", "tool": "open", "args": ["spotify"]}, "a mapping of the tool's arguments"),
    ({"do": "tool", "server": "system", "tool": "open", "args": {"target": "x" * 3000}}, "longer than"),
    ({"do": "media", "action": "next", "hold": 9}, "hold is a number"),
    ({"do": "media", "action": "next", "repeat": "yes"}, "repeat is true or false"),
])
def test_wrong_bindings_say_why(raw: object, message: str) -> None:
    with pytest.raises(g.BindingError, match=message):
        parse_binding(raw)


def test_a_server_without_gestures_gets_the_defaults_and_one_with_them_only_its_own() -> None:
    assert bindings_of({"command": "uv"}, DEFAULTS) == (DEFAULT_BINDINGS, {})
    bindings, problems = bindings_of({"gestures": {"victory": {"do": "new_chat"}, "thumb_up": None, "wave": {},
                                                   "pinch": {"do": "fly"}}}, DEFAULTS)
    assert bindings == {"victory": Binding("new_chat")}  # nothing else: not even the defaults
    assert list(problems) == ["pinch"] and "unknown action" in problems["pinch"]
    assert bindings_of({"gestures": "everything"}, DEFAULTS)[0] == {}


def test_a_whole_set_is_checked_strictly_as_the_hud_saves_it() -> None:
    saved = check_bindings({"victory": {"do": "media", "action": "play_pause"}, "wave": None,
                            "circle_cw": {"do": "tool", "server": "system", "tool": "open",
                                          "args": {"target": "firefox"}}})
    assert saved == {"victory": {"do": "media", "action": "play_pause"},
                     "circle_cw": {"do": "tool", "server": "system", "tool": "open", "args": {"target": "firefox"}}}
    with pytest.raises(ValueError, match="^pinch: unknown action"):
        check_bindings({"victory": {"do": "new_chat"}, "pinch": {"do": "fly"}})
    with pytest.raises(ValueError, match="mapping"):
        check_bindings(["victory"])


def test_bindings_in_words() -> None:
    words = {gesture: describe(b) for gesture, b in DEFAULT_BINDINGS.items()}
    assert words["swipe_right"] == "next track" and words["closed_fist"] == "sound off / on"
    assert words["pointing_up"] == "COMMS on / off" and words["i_love_you"] == "the neural map on / off"
    assert words["hands_apart"] == "show everything" and words["hands_together"] == "clear the screen"
    assert words["wave"] == "run morning-briefing" and words["push"] == "dismiss the newest panel"
    tool = parse_binding({"do": "tool", "server": "system", "tool": "open", "args": {"target": "spotify"}})
    assert describe(tool) == "system.open (target: spotify)"
    assert describe(parse_binding({"do": "toggle", "panel": "settings/voice"})) == "voice settings on / off"
    assert g.hold_words(DEFAULT_BINDINGS["thumb_up"]) == "repeats while held"
    assert g.hold_words(DEFAULT_BINDINGS["closed_fist"]) == "held 1 s"


class Clock:
    """A monotonic clock the test moves."""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def steps(firing: Firing, binding: Binding, helds: list[float], showing: int = 7, armed: Binding | None = None,
          clock: Clock | None = None) -> list[str]:
    """What a shape's start and hold steps decide, in order."""
    out = []
    for i, held in enumerate(helds):
        if clock is not None:
            clock.now += 0.4
        out.append(firing.decide(binding, phase="start" if i == 0 else "hold", held=held, showing=showing,
                                 armed=armed))
    return out


def test_a_shape_fires_once_held_as_long_as_its_binding_asks() -> None:
    plain, held = Binding("new_chat"), Binding("new_chat", hold=1.0)
    assert steps(Firing(), plain, [0.3, 0.6, 1.0]) == ["run", "done", "done"]
    assert steps(Firing(), held, [0.3, 0.6, 1.0, 1.5]) == ["wait", "wait", "run", "done"]
    assert steps(Firing(), Binding("volume", {"action": "up"}, repeat=True), [0.3, 0.6, 1.0]) == ["run", "again",
                                                                                                    "again"]
    assert steps(Firing(), Binding("volume", {"action": "up"}, hold=0.6, repeat=True),
                 [0.3, 0.6, 1.0]) == ["wait", "run", "again"]


def test_a_shape_seen_only_halfway_through_its_hold_doesnt_fire_at_once() -> None:
    firing = Firing()
    assert firing.decide(Binding("new_chat"), phase="hold", held=1.5, showing=9, armed=None) == "done"
    assert firing.decide(Binding("new_chat", hold=1.0), phase="hold", held=1.5, showing=10, armed=None) == "run"


def test_motions_fire_at_once() -> None:
    firing = Firing()
    swipe = Binding("media", {"action": "next"})
    assert [firing.decide(swipe, phase="start", held=0, showing=None, armed=None) for _ in range(3)] == ["run"] * 3


def test_while_a_gesture_wakes_the_others_they_sleep_until_it_is_made() -> None:
    clock = Clock()
    firing = Firing(clock)
    arm = Binding("arm", {"seconds": 8})
    swipe = Binding("media", {"action": "next"})
    assert firing.decide(swipe, phase="start", held=0, showing=None, armed=arm) == "asleep"
    assert steps(firing, Binding("new_chat"), [0.3, 0.6], showing=1, armed=arm) == ["asleep", "done"]  # said once
    assert steps(firing, Binding("volume", {"action": "up"}, repeat=True), [0.3, 0.6, 1.0], showing=3,
                 armed=arm) == ["asleep", "done", "done"]  # a repeating one too, not at every step
    assert firing.decide(arm, phase="start", held=0.3, showing=2, armed=arm) == "run"
    clock.now += 5
    assert firing.decide(swipe, phase="start", held=0, showing=None, armed=arm) == "run"  # and it keeps them awake
    clock.now += 7
    assert firing.decide(swipe, phase="start", held=0, showing=None, armed=arm) == "run"
    clock.now += 9
    assert firing.decide(swipe, phase="start", held=0, showing=None, armed=arm) == "asleep"


def test_old_showings_are_forgotten() -> None:
    clock = Clock()
    firing = Firing(clock)
    firing.decide(Binding("new_chat"), phase="start", held=0.3, showing=1, armed=None)
    clock.now += 60
    firing.decide(Binding("new_chat"), phase="start", held=0.3, showing=2, armed=None)
    assert list(firing._showings) == [2]
