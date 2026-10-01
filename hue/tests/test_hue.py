import pytest

from hue import HueError, color_to_xy, describe, light_body, match, normalize, to_mirek

ROOMS = {"r1": "Living room", "r2": "Bedroom", "r3": "Bed nook"}


def test_normalize_case_and_punctuation():
    assert normalize("Living-Room!") == "living room"


def test_normalize_drops_leading_article():
    assert normalize("the living room") == "living room"
    assert normalize("My Bedroom") == "bedroom"


def test_match_exact_beats_substring():
    assert match("bedroom", ROOMS, "room") == "r2"


def test_match_substring():
    assert match("living", ROOMS, "room") == "r1"


def test_match_ambiguous_lists_options():
    with pytest.raises(HueError, match="could mean: Bed nook, Bedroom"):
        match("bed", ROOMS, "room")


def test_match_unknown_lists_options():
    with pytest.raises(HueError, match="No room called 'garage'. Options: Bed nook, Bedroom, Living room"):
        match("garage", ROOMS, "room")


def test_match_uses_labels_in_errors():
    scenes = {"s1": "Relax", "s2": "Relax"}
    labels = {"s1": "Relax (Bedroom)", "s2": "Relax (Living room)"}
    with pytest.raises(HueError, match=r"Relax \(Bedroom\), Relax \(Living room\)"):
        match("relax", scenes, "scene", labels)


def test_match_nothing_to_choose():
    with pytest.raises(HueError, match="no scene is set up"):
        match("relax", {}, "scene")


def test_color_named_red():
    assert color_to_xy("red") == pytest.approx({"x": 0.64, "y": 0.33}, abs=0.001)


def test_color_hex_white():
    assert color_to_xy("#FFFFFF") == pytest.approx({"x": 0.3127, "y": 0.329}, abs=0.001)


def test_color_unknown():
    with pytest.raises(HueError, match="Unknown colour 'mauve-ish'"):
        color_to_xy("mauve-ish")


def test_color_black():
    with pytest.raises(HueError, match="Turn it off instead"):
        color_to_xy("#000000")


@pytest.mark.parametrize("temp,mirek", [("warm", 370), ("neutral", 250), ("cool", 154),
                                         ("2000", 500), ("3000K", 333), ("3000 k", 333)])
def test_to_mirek(temp, mirek):
    assert to_mirek(temp) == mirek


@pytest.mark.parametrize("temp", ["1500", "9000", "toasty"])
def test_to_mirek_rejects(temp):
    with pytest.raises(HueError, match="warm, neutral, cool, or 2000–6500 K"):
        to_mirek(temp)


def test_light_body_brightness_turns_on():
    assert light_body(brightness=30) == {"dimming": {"brightness": 30.0}, "on": {"on": True}}


def test_light_body_off_wins():
    assert light_body(on=False, brightness=50, color="red") == {"on": {"on": False}}
    assert light_body(brightness=0, color_temp="warm") == {"on": {"on": False}}


def test_light_body_on_only():
    assert light_body(on=True) == {"on": {"on": True}}


def test_light_body_colour_and_temp_conflict():
    with pytest.raises(HueError, match="not both"):
        light_body(color="red", color_temp="warm")


def test_light_body_brightness_range():
    with pytest.raises(HueError, match="0 to 100"):
        light_body(brightness=150)


def test_light_body_nothing():
    with pytest.raises(HueError, match="Nothing to change"):
        light_body()


def test_describe():
    assert describe("Living room", {"on": {"on": False}}) == "Living room: off"
    assert describe("Living room", light_body(brightness=30, color_temp="warm")) == "Living room: on, 30%, 2700 K"
    assert describe("Lamp", light_body(color="blue")) == "Lamp: on, colour set"


import asyncio

import httpx

from fake_bridge import FakeBridge
from hue import build_home


def test_build_home_resolves_room_devices_and_zone_lights():
    home = asyncio.run(FakeBridge().bridge().home())
    assert home.groups["r1"].lights == ["l1", "l2"]
    assert home.groups["r1"].grouped_light == "g1"
    assert home.groups["z1"].kind == "zone" and home.groups["z1"].lights == ["l1", "l2"]
    assert home.groups["r2"].lights == ["l3"]
    assert home.everything == "g0"
    assert set(home.scenes) == {"s1", "s2", "s3"}


def test_bridge_put_sends_body_and_key():
    fake = FakeBridge()
    asyncio.run(fake.bridge().put("grouped_light", "g1", {"on": {"on": False}}))
    assert fake.puts == [("grouped_light", "g1", {"on": {"on": False}})]


def test_bridge_unreachable():
    fake = FakeBridge(error=httpx.ConnectError("no route to host"))
    with pytest.raises(HueError, match="Can't reach the Hue Bridge"):
        asyncio.run(fake.bridge().get("room"))


def test_bridge_key_rejected():
    fake = FakeBridge(status=403, reply={"errors": [{"description": "unauthorized user"}], "data": []})
    with pytest.raises(HueError, match="Re-run pair.py"):
        asyncio.run(fake.bridge().get("room"))


def test_bridge_207_errors():
    fake = FakeBridge(status=207, reply={"errors": [{"description": "device (light) is \"soft off\""}],
                                         "data": []})
    with pytest.raises(HueError, match='Hue Bridge: device \\(light\\) is "soft off"'):
        asyncio.run(fake.bridge().put("light", "l1", {"on": {"on": True}}))


def test_bridge_warning_with_data_is_success():
    # The bridge flags flaky bulbs with "may not have effect" but still applies the change.
    fake = FakeBridge(status=200, reply={
        "errors": [{"description": "device (light) l1 has communication issues, command (.on.on) may not have effect"}],
        "data": [{"rid": "l1", "rtype": "light"}]})
    asyncio.run(fake.bridge().put("light", "l1", {"on": {"on": True}}))


from hue import clamp_mirek


def test_normalize_drops_trailing_lights():
    assert normalize("the living room lights") == "living room"


def test_to_mirek_kelvin_word():
    assert to_mirek("2700 kelvin") == 370


def test_clamp_mirek_to_light_range():
    light = {"color_temperature": {"mirek_schema": {"mirek_minimum": 153, "mirek_maximum": 454}}}
    assert clamp_mirek(500, light) == 454
    assert clamp_mirek(100, light) == 153
    assert clamp_mirek(300, {}) == 300


def test_light_body_brightness_change_up_turns_on():
    assert light_body(brightness_change=20) == {
        "dimming_delta": {"action": "up", "brightness_delta": 20.0}, "on": {"on": True}}


def test_light_body_brightness_change_down_leaves_on_alone():
    assert light_body(brightness_change=-15) == {"dimming_delta": {"action": "down", "brightness_delta": 15.0}}


def test_light_body_brightness_and_change_conflict():
    with pytest.raises(HueError, match="not both"):
        light_body(brightness=50, brightness_change=10)


def test_light_body_fade():
    assert light_body(brightness=10, fade_seconds=1200)["dynamics"] == {"duration": 1200000}


def test_describe_change_and_fade():
    assert describe("Office", light_body(brightness_change=-15, fade_seconds=30)) == "Office: 15% dimmer, over 30 s"
    assert describe("Office", light_body(brightness_change=20)) == "Office: on, 20% brighter"


def test_build_home_extras():
    home = asyncio.run(FakeBridge().bridge().home())
    assert home.groups["r1"].archetype == "living_room"
    assert home.groups["r1"].devices == ["d1", "d2"]
    assert home.room_of_device == {"d1": "r1", "d2": "r1", "d3": "r2"}
    assert home.connectivity == {"d1": "connected", "d3": "connectivity_issue"}
    assert home.updates == {"d2": "update_pending"}
    assert set(home.scripts) >= {"sc-schedule", "sc-wake"} and set(home.automations) == {"a1", "a2"}
    assert (home.time_zone, home.sunset) == ("America/Chicago", "18:41:00")


def test_bridge_post_and_delete():
    fake = FakeBridge()
    b = fake.bridge()
    assert asyncio.run(b.post("scene", {"type": "scene"})) == "new-scene"
    asyncio.run(b.delete("scene", "s1"))
    assert fake.posts == [("scene", {"type": "scene"})] and fake.deletes == [("scene", "s1")]


def test_bridge_non_dict_json():
    fake = FakeBridge(status=200, reply=[1, 2])
    with pytest.raises(HueError, match="didn't look like a Hue Bridge"):
        asyncio.run(fake.bridge().get("room"))


def test_bridge_other_http_errors():
    fake = FakeBridge(error=httpx.TooManyRedirects("loop"))
    with pytest.raises(HueError, match="Can't reach the Hue Bridge"):
        asyncio.run(fake.bridge().get("room"))
