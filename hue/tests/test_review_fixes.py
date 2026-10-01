"""Findings from the full-control review, each pinned by a test."""
import asyncio

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import hue_common
import hue_mcp
from fake_bridge import FakeBridge


def call(fake, tool, **args):
    hue_common._bridge = fake.bridge()

    async def go():
        async with Client(hue_mcp.mcp) as client:
            return (await client.call_tool(tool, args)).content[0].text

    return asyncio.run(go())


def app_named(fake):
    """The Hue app shows the device's name; the light's own name can differ (16 of his 46 do)."""
    fake.data["device"][0]["metadata"]["name"] = "Reading lamp"
    fake.data["light"][0]["metadata"]["name"] = "Hue color lamp 1"
    return fake


# 1. Names come from the device (what the app shows), not the light's own name.

def test_set_light_by_app_name():
    fake = app_named(FakeBridge())
    assert call(fake, "set_light", name="reading lamp", brightness=40) == "Reading lamp: on, 40%"
    assert fake.puts[0][:2] == ("light", "l1")


def test_list_home_shows_app_names():
    out = call(app_named(FakeBridge()), "list_home")
    assert "Reading lamp (on 50%)" in out and "Hue color lamp 1" not in out


def test_old_light_name_no_longer_matches():
    with pytest.raises(ToolError, match="No light called 'hue color lamp 1'"):
        call(app_named(FakeBridge()), "set_light", name="hue color lamp 1", on=True)


# 2. A relative change is applied once, not once for the room and again per colour bulb.

def test_brightness_change_with_colour_applied_once():
    fake = FakeBridge()
    call(fake, "set_room", name="living room", brightness_change=-20, color="red")
    assert fake.puts[0] == ("grouped_light", "g1", {"dimming_delta": {"action": "down", "brightness_delta": 20.0}})
    assert all("dimming_delta" not in body for _, _, body in fake.puts[1:])


# 3. update_automation refuses changes it can't make.

def with_wake_and_coming_home(fake):
    fake.data["behavior_instance"] += [
        {"id": "a3", "type": "behavior_instance", "script_id": "sc-wake", "enabled": True, "metadata": {"name": "Wake"},
         "configuration": {"end_brightness": 100.0, "fade_in_duration": {"seconds": 1800}, "style": "sunrise",
                           "when": {"recurrence_days": ["monday"], "time_point": {"type": "time", "time": {"hour": 6, "minute": 0}}},
                           "where": [{"group": {"rid": "r2", "rtype": "room"}}]}},
        {"id": "a4", "type": "behavior_instance", "script_id": "sc-home", "enabled": True, "metadata": {"name": "Arrive"},
         "configuration": {"where": [{"group": {"rid": "r1", "rtype": "room"}}]}},
    ]
    return fake


def test_update_automation_scene_on_wake_up_refused():
    fake = with_wake_and_coming_home(FakeBridge())
    with pytest.raises(ToolError, match="'Wake' has no scene to change"):
        call(fake, "update_automation", name="wake", scene="relax")
    assert fake.puts == []


def test_update_automation_unsupported_type_refused():
    fake = with_wake_and_coming_home(FakeBridge())
    with pytest.raises(ToolError, match="'Arrive' is a Coming home automation"):
        call(fake, "update_automation", name="arrive", new_name="Home")


def test_update_wake_time_still_works():
    fake = with_wake_and_coming_home(FakeBridge())
    call(fake, "update_automation", name="wake", at="6:30")
    assert fake.puts[0][2]["configuration"]["when"]["time_point"]["time"] == {"hour": 6, "minute": 30}


# 4. Rooms: never leave a light without a room.

def test_create_room_creates_empty_then_moves():
    fake = FakeBridge()
    call(fake, "create_room", name="Den", lights=["bedside"])
    assert fake.posts == [("room", {"type": "room", "metadata": {"name": "Den", "archetype": "other"}, "children": []})]
    assert fake.puts == [("room", "r2", {"children": []}),
                         ("room", "new-room", {"children": [{"rid": "d3", "rtype": "device"}]})]


def test_move_light_puts_it_back_if_adding_fails():
    fake = FakeBridge(failing={"r2"})
    with pytest.raises(ToolError, match="Couldn't add Ceiling to Bedroom; it's back in Living room"):
        call(fake, "move_light", light="ceiling", room="bedroom")
    assert fake.puts == [("room", "r1", {"children": [{"rid": "d1", "rtype": "device"}]}),
                         ("room", "r1", {"children": [{"rid": "d1", "rtype": "device"}, {"rid": "d2", "rtype": "device"}]})]


# 5. Deleting a room or zone says which scenes go with it.

def test_delete_room_mentions_scenes():
    assert call(FakeBridge(), "delete_room", name="living room") == \
        "Deleted room 'Living room' (2 lights now have no room; its 2 scenes went with it: Read, Relax)"


def test_delete_zone_mentions_no_scenes():
    assert call(FakeBridge(), "delete_zone", name="downstairs") == "Deleted zone 'Downstairs'"


# 6. Tools say whether they read, change or destroy.

def test_tool_annotations():
    tools = {t.name: t for t in asyncio.run(hue_mcp.mcp.list_tools())}
    assert tools["list_home"].annotations.read_only_hint is True
    assert tools["set_room"].annotations.read_only_hint is False
    assert tools["set_room"].annotations.destructive_hint is False
    for name in ("delete_scene", "delete_automation", "delete_zone", "delete_room"):
        assert tools[name].annotations.destructive_hint is True, name
    assert all(t.annotations is not None for t in tools.values())


# 7. Automations: no duplicate names; ids work where names can't.

def test_create_automation_duplicate_name_refused():
    with pytest.raises(ToolError, match="There's already an automation called 'Morning'"):
        call(FakeBridge(), "create_timer", name="morning", room="bedroom", minutes=5)


def test_automation_by_id():
    fake = FakeBridge()
    call(fake, "set_automation_enabled", name="a1", enabled=False)
    assert fake.puts == [("behavior_instance", "a1", {"enabled": False})]
