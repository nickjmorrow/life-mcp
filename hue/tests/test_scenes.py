import asyncio

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import hue_common
import hue_mcp
from fake_bridge import FakeBridge
from hue_scenes import LightSetting, scene_action, snapshot_action


def call(fake, tool, **args):
    hue_common._bridge = fake.bridge()

    async def go():
        async with Client(hue_mcp.mcp) as client:
            return (await client.call_tool(tool, args)).content[0].text

    return asyncio.run(go())


def test_snapshot_action_uses_ct_when_valid():
    light = {"on": {"on": True}, "dimming": {"brightness": 40.0},
             "color_temperature": {"mirek": 366, "mirek_valid": True}, "color": {"xy": {"x": 0.4, "y": 0.4}}}
    assert snapshot_action(light) == {"on": {"on": True}, "dimming": {"brightness": 40.0},
                                      "color_temperature": {"mirek": 366}}


def test_snapshot_action_off():
    assert snapshot_action({"on": {"on": False}}) == {"on": {"on": False}}


def test_scene_action_colour_needs_capable_light():
    with pytest.raises(ToolError, match="Bedside can't do colour"):
        scene_action({"metadata": {"name": "Bedside"}, "on": {"on": False}}, LightSetting(light="bedside", color="red"))


def test_activate_scene_dynamic_with_speed_and_fade():
    fake = FakeBridge()
    out = call(fake, "activate_scene", name="read", dynamic=True, speed=0.3, brightness=60, fade_seconds=10)
    assert out == "Read is on in Living room (dynamic, 60%, over 10 s)"
    assert fake.puts == [("scene", "s2", {"speed": 0.3}),
                         ("scene", "s2", {"recall": {"action": "dynamic_palette", "dimming": {"brightness": 60.0},
                                                     "duration": 10000}})]


def test_create_scene_snapshot():
    fake = FakeBridge()
    assert call(fake, "create_scene", name="Cozy", room="living room") == \
        "Created scene 'Cozy' in Living room (2 lights, saved as they are now)"
    rtype, body = fake.posts[0]
    assert rtype == "scene" and body["metadata"] == {"name": "Cozy"}
    assert body["group"] == {"rid": "r1", "rtype": "room"}
    assert [a["target"]["rid"] for a in body["actions"]] == ["l1", "l2"]


def test_create_scene_described_unlisted_lights_off():
    fake = FakeBridge()
    call(fake, "create_scene", name="Movie", room="living room",
         lights=[{"light": "floor lamp", "brightness": 20, "color": "purple"}])
    actions = {a["target"]["rid"]: a["action"] for a in fake.posts[0][1]["actions"]}
    assert actions["l1"]["dimming"] == {"brightness": 20.0} and "color" in actions["l1"]
    assert actions["l2"] == {"on": {"on": False}}


def test_create_scene_duplicate():
    with pytest.raises(ToolError, match="already has a scene called 'Relax'. Use update_scene"):
        call(FakeBridge(), "create_scene", name="relax", room="living room")


def test_create_scene_name_length():
    with pytest.raises(ToolError, match="1–32 characters"):
        call(FakeBridge(), "create_scene", name="x" * 33, room="living room")


def test_update_scene_rename_and_save_current():
    fake = FakeBridge()
    assert call(fake, "update_scene", name="read", room="living room", new_name="Reading", save_current=True) == \
        "Updated scene 'Read' in Living room (renamed to 'Reading', lights replaced)"
    rtype, rid, body = fake.puts[0]
    assert (rtype, rid) == ("scene", "s2") and body["metadata"] == {"name": "Reading"} and len(body["actions"]) == 2


def test_update_scene_nothing():
    with pytest.raises(ToolError, match="Nothing to change"):
        call(FakeBridge(), "update_scene", name="read", room="living room")


def test_delete_scene():
    fake = FakeBridge()
    assert call(fake, "delete_scene", name="relax", room="bedroom") == "Deleted scene 'Relax' from Bedroom"
    assert fake.deletes == [("scene", "s3")]
