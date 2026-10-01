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


def test_rename_light_goes_to_device():
    fake = FakeBridge()
    assert call(fake, "rename", kind="light", name="floor lamp", new_name="Reading lamp") == \
        "Renamed light 'Floor lamp' to 'Reading lamp'"
    # The name the app (and list_home) shows is the light's own; the device name is separate. Change both.
    assert fake.puts == [("device", "d1", {"metadata": {"name": "Reading lamp", "archetype": "floor_shade"}}),
                         ("light", "l1", {"metadata": {"name": "Reading lamp"}})]


def test_rename_room_keeps_archetype():
    fake = FakeBridge()
    call(fake, "rename", kind="room", name="living room", new_name="Lounge")
    assert fake.puts == [("room", "r1", {"metadata": {"name": "Lounge", "archetype": "living_room"}})]


def test_rename_scene():
    fake = FakeBridge()
    call(fake, "rename", kind="scene", name="read", new_name="Study", room="living room")
    assert fake.puts == [("scene", "s2", {"metadata": {"name": "Study"}})]


def test_rename_shared_light_name_refused():
    fake = FakeBridge()
    fake.data["light"].append({**fake.data["light"][0], "id": "l4", "owner": {"rid": "d4", "rtype": "device"}})
    with pytest.raises(ToolError, match="2 lights are called 'Floor lamp'"):
        call(fake, "rename", kind="light", name="floor lamp", new_name="X")


def test_move_light_order():
    fake = FakeBridge()
    assert call(fake, "move_light", light="ceiling", room="bedroom") == "Moved Ceiling from Living room to Bedroom"
    assert fake.puts == [("room", "r1", {"children": [{"rid": "d1", "rtype": "device"}]}),
                         ("room", "r2", {"children": [{"rid": "d3", "rtype": "device"}, {"rid": "d2", "rtype": "device"}]})]


def test_move_light_to_zone_refused():
    with pytest.raises(ToolError, match="Downstairs is a zone"):
        call(FakeBridge(), "move_light", light="ceiling", room="downstairs")


def test_create_zone():
    fake = FakeBridge()
    assert call(fake, "create_zone", name="Reading nook", lights=["floor lamp", "bedside"]) == \
        "Created zone 'Reading nook' with Floor lamp, Bedside"
    assert fake.posts == [("zone", {"type": "zone", "metadata": {"name": "Reading nook", "archetype": "other"},
                                    "children": [{"rid": "l1", "rtype": "light"}, {"rid": "l3", "rtype": "light"}]})]


def test_update_zone_add_remove():
    fake = FakeBridge()
    assert call(fake, "update_zone", name="downstairs", add=["bedside"], remove=["ceiling"]) == \
        "Downstairs now has Floor lamp, Bedside"
    assert fake.puts == [("zone", "z1", {"children": [{"rid": "l1", "rtype": "light"}, {"rid": "l3", "rtype": "light"}]})]


def test_delete_zone_and_room():
    fake = FakeBridge()
    call(fake, "delete_zone", name="downstairs")
    call(fake, "delete_room", name="bedroom")
    assert fake.deletes == [("zone", "z1"), ("room", "r2")]


def test_delete_zone_refuses_room():
    with pytest.raises(ToolError, match="Bedroom is a room"):
        call(FakeBridge(), "delete_zone", name="bedroom")


def test_status():
    out = call(FakeBridge(), "status")
    assert "Not responding: Bedside (Bedroom)" in out
    assert "Firmware update waiting: Ceiling (Living room)" in out
    assert "Bridge time zone: America/Chicago; sunset today 18:41" in out
