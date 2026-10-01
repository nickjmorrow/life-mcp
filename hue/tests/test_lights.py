import asyncio

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import fake_bridge
import hue_common
import hue_mcp
from fake_bridge import FakeBridge


def call(fake: FakeBridge, tool: str, **args) -> str:
    hue_common._bridge = fake.bridge()

    async def go():
        async with Client(hue_mcp.mcp) as client:
            return (await client.call_tool(tool, args)).content[0].text

    return asyncio.run(go())


def with_effects(fake):
    fake.data["light"][0]["effects_v2"] = {"action": {"effect_values": ["no_effect", "candle", "fire"]}}
    fake.data["light"][0]["timed_effects"] = {"effect_values": ["no_effect", "sunrise", "sunset"]}
    fake.data["light"][0]["signaling"] = {"signal_values": ["no_signal", "on_off", "on_off_color", "alternating"]}
    fake.data["light"][0]["powerup"] = {"preset": "safety"}
    fake.data["light"][1]["powerup"] = {"preset": "safety"}
    return fake


def test_list_home_zone_shows_count():
    out = call(FakeBridge(), "list_home")
    assert "Downstairs [zone, on 65%] lights: 2 lights; scenes: none" in out
    assert "Floor lamp (on 50%), Ceiling (on 80%)" in out


def test_set_room_brightness_change():
    fake = FakeBridge()
    assert call(fake, "set_room", name="living room", brightness_change=-20) == "Living room: 20% dimmer"
    assert fake.puts == [("grouped_light", "g1", {"dimming_delta": {"action": "down", "brightness_delta": 20.0}})]


def test_set_room_fade():
    fake = FakeBridge()
    call(fake, "set_room", name="living room", brightness=10, fade_seconds=600)
    assert fake.puts[0][2]["dynamics"] == {"duration": 600000}


def test_set_room_colour_and_brightness_mixed():
    fake = FakeBridge()
    out = call(fake, "set_room", name="living room", color="red", brightness=40)
    assert out == "Living room: on, 40%, colour set (1 light can't do colour, brightness set)"
    assert fake.puts[0] == ("grouped_light", "g1", {"dimming": {"brightness": 40.0}, "on": {"on": True}})
    assert [(t, i) for t, i, _ in fake.puts[1:]] == [("light", "l1")]


def test_set_room_turn_on_dim_note():
    fake = FakeBridge()
    fake.data["grouped_light"][0]["dimming"]["brightness"] = 3.0
    assert call(fake, "set_room", name="living room", on=True) == \
        "Living room: on (it came back at 3%; say a brightness to change it)"


def test_set_room_effect():
    fake = with_effects(FakeBridge())
    out = call(fake, "set_room", name="living room", effect="candle")
    assert out == "Living room: candle effect (1 light can't)"
    assert fake.puts == [("light", "l1", {"effects_v2": {"action": {"effect": "candle"}}, "on": {"on": True}})]


def test_set_room_effect_none_capable():
    with pytest.raises(ToolError, match="None of the lights in Bedroom can do candle"):
        call(with_effects(FakeBridge()), "set_room", name="bedroom", effect="candle")


def test_set_room_warm_clamped_per_light():
    fake = FakeBridge()
    fake.data["light"][1]["color_temperature"]["mirek_schema"]["mirek_maximum"] = 454
    call(fake, "set_room", name="living room", color_temp="2000")
    assert [p[2]["color_temperature"]["mirek"] for p in fake.puts] == [500, 454]


def test_set_light_gradient():
    fake = FakeBridge()
    fake.data["light"][0]["gradient"] = {"points_capable": 5, "points": []}
    out = call(fake, "set_light", name="floor lamp", gradient=["red", "blue", "#00ff00"])
    assert out == "Floor lamp: gradient of 3 colours"
    body = fake.puts[0][2]
    assert len(body["gradient"]["points"]) == 3 and body["on"] == {"on": True}


def test_set_light_gradient_not_capable():
    with pytest.raises(ToolError, match="Ceiling can't do gradients"):
        call(FakeBridge(), "set_light", name="ceiling", gradient=["red", "blue"])


def test_identify_light():
    fake = FakeBridge()
    assert call(fake, "identify_light", name="bedside") == "Bedside is breathing so you can spot it"
    assert fake.puts == [("light", "l3", {"alert": {"action": "breathe"}})]


def test_timed_effect_room():
    fake = with_effects(FakeBridge())
    out = call(fake, "timed_effect", name="living room", effect="sunrise", minutes=20)
    assert out == "Living room: sunrise over 20 min (1 light can't)"
    assert fake.puts == [("light", "l1", {"timed_effects": {"effect": "sunrise", "duration": 1200000}})]


def test_signal_single_light():
    fake = with_effects(FakeBridge())
    out = call(fake, "signal", name="floor lamp", kind="on_off_color", seconds=5, colors=["red"])
    assert out == "Floor lamp: on_off_color signal for 5 s"
    body = fake.puts[0][2]["signaling"]
    assert body["signal"] == "on_off_color" and body["duration"] == 5000 and len(body["colors"]) == 1


def test_signal_colour_count():
    with pytest.raises(ToolError, match="alternating needs 2 colours"):
        call(with_effects(FakeBridge()), "signal", name="floor lamp", kind="alternating", seconds=5, colors=["red"])


def test_set_power_on():
    fake = with_effects(FakeBridge())
    assert call(fake, "set_power_on", name="living room", preset="last_on_state") == \
        "Living room: after a power cut, last_on_state (2 lights)"
    assert fake.puts == [("light", "l1", {"powerup": {"preset": "last_on_state"}}),
                         ("light", "l2", {"powerup": {"preset": "last_on_state"}})]
