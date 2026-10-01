import asyncio

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import hue_common
import hue_mcp  # registers the tools
import fake_bridge
from fake_bridge import FakeBridge


def call(fake: FakeBridge, tool: str, **args) -> str:
    hue_common._bridge = fake.bridge()

    async def go():
        async with Client(hue_mcp.mcp) as client:
            return (await client.call_tool(tool, args)).content[0].text

    return asyncio.run(go())


def test_list_home():
    out = call(FakeBridge(), "list_home")
    assert "Living room [room, on 65%] lights: Floor lamp (on 50%), Ceiling (on 80%); scenes: Read, Relax" in out
    assert "Bedroom [room, off] lights: Bedside (off); scenes: Relax" in out
    assert "Downstairs [zone, on 65%]" in out
    assert out.index("Bedroom") < out.index("Downstairs")  # rooms before zones


def test_set_room_brightness():
    fake = FakeBridge()
    assert call(fake, "set_room", name="living room", brightness=30) == "Living room: on, 30%"
    assert fake.puts == [("grouped_light", "g1", {"dimming": {"brightness": 30.0}, "on": {"on": True}})]


@pytest.mark.parametrize("name", ["Living-Room", "the living room", "living"])
def test_set_room_spoken_names(name):
    fake = FakeBridge()
    call(fake, "set_room", name=name, on=True)
    assert fake.puts == [("grouped_light", "g1", {"on": {"on": True}})]


def test_set_room_zero_is_off():
    fake = FakeBridge()
    assert call(fake, "set_room", name="living room", brightness=0, color="red") == "Living room: off"
    assert fake.puts == [("grouped_light", "g1", {"on": {"on": False}})]


def test_set_room_zone():
    fake = FakeBridge()
    call(fake, "set_room", name="downstairs", on=False)
    assert fake.puts == [("grouped_light", "g3", {"on": {"on": False}})]


def test_set_room_warm_goes_light_by_light():
    fake = FakeBridge()
    assert call(fake, "set_room", name="living room", color_temp="warm") == "Living room: on, 2700 K"
    body = {"color_temperature": {"mirek": 370}, "on": {"on": True}}
    assert fake.puts == [("light", "l1", body), ("light", "l2", body)]


def test_set_room_colour_skips_white_only():
    fake = FakeBridge()
    out = call(fake, "set_room", name="living room", color="red")
    assert out == "Living room: on, colour set (1 light can't do colour, left as is)"
    assert [(t, i) for t, i, _ in fake.puts] == [("light", "l1")]


def test_set_room_colour_none_capable():
    fake = FakeBridge()
    with pytest.raises(ToolError, match="None of the lights in Bedroom can do that"):
        call(fake, "set_room", name="bedroom", color="blue")
    assert fake.puts == []


def test_set_room_unknown():
    with pytest.raises(ToolError, match="No room or zone called 'garage'. Options: Bedroom, Downstairs, Living room"):
        call(FakeBridge(), "set_room", name="garage", on=True)


def test_set_light():
    fake = FakeBridge()
    assert call(fake, "set_light", name="floor lamp", color="blue") == "Floor lamp: on, colour set"
    assert fake.puts[0][:2] == ("light", "l1")


def test_set_light_cant_do_colour():
    fake = FakeBridge()
    with pytest.raises(ToolError, match="Bedside can't change colour"):
        call(fake, "set_light", name="bedside", color="blue")
    assert fake.puts == []


def test_scene_ambiguous_then_room():
    fake = FakeBridge()
    with pytest.raises(ToolError, match=r"could mean: Relax \(Bedroom\), Relax \(Living room\)"):
        call(fake, "activate_scene", name="relax")
    assert call(fake, "activate_scene", name="relax", room="bedroom") == "Relax is on in Bedroom"
    assert fake.puts == [("scene", "s3", {"recall": {"action": "active"}})]


def test_scene_unique_name_needs_no_room():
    fake = FakeBridge()
    assert call(fake, "activate_scene", name="read") == "Read is on in Living room"


def test_all_off():
    fake = FakeBridge()
    assert call(fake, "all_off") == "Everything is off"
    assert fake.puts == [("grouped_light", "g0", {"on": {"on": False}})]


def test_bridge_down_reaches_claude_in_plain_words():
    import httpx
    with pytest.raises(ToolError, match="Can't reach the Hue Bridge"):
        call(FakeBridge(error=httpx.ConnectError("down")), "list_home")


def test_set_light_same_name_changes_all():
    # Two bulbs called "Office Ceiling" is normal; there's no way for Claude to pick one.
    fake = FakeBridge()
    fake.data["light"].append(fake_bridge.light("l4", "d4", "Floor lamp", color=True))
    assert call(fake, "set_light", name="floor lamp", color="blue") == "Floor lamp (2 lights): on, colour set"
    assert [(t, i) for t, i, _ in fake.puts] == [("light", "l1"), ("light", "l4")]


def test_set_room_keeps_going_when_a_light_fails():
    fake = FakeBridge(failing={"l1"})
    out = call(fake, "set_room", name="living room", color_temp="warm")
    assert out == "Living room: on, 2700 K (Floor lamp didn't respond)"
    assert [(t, i) for t, i, _ in fake.puts] == [("light", "l2")]


def test_set_room_all_lights_fail():
    fake = FakeBridge(failing={"l1", "l2"})
    with pytest.raises(ToolError, match="rate limit exceeded"):
        call(fake, "set_room", name="living room", color_temp="warm")


def test_missing_setup_says_so(monkeypatch):
    monkeypatch.delenv("HUE_BRIDGE_IP", raising=False)
    monkeypatch.delenv("HUE_APP_KEY", raising=False)
    hue_common._bridge = None

    async def go():
        async with Client(hue_mcp.mcp) as client:
            await client.call_tool("list_home", {})

    with pytest.raises(ToolError, match="Hue isn't set up on this Mac"):
        asyncio.run(go())
