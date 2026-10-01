import asyncio

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import hue_common
import hue_mcp
from fake_bridge import FakeBridge
from hue import Group
from hue_automations import (go_to_sleep_config, parse_at, parse_days, schedule_config, timer_config,
                             wake_up_config)

ROOM = Group("r1", "Living room", "room", "g1", ["l1", "l2"])
WHERE = [{"group": {"rid": "r1", "rtype": "room"}}]


def call(fake, tool, **args):
    hue_common._bridge = fake.bridge()

    async def go():
        async with Client(hue_mcp.mcp) as client:
            return (await client.call_tool(tool, args)).content[0].text

    return asyncio.run(go())


@pytest.mark.parametrize("days,expected", [
    ("weekdays", ["monday", "tuesday", "wednesday", "thursday", "friday"]),
    ("weekends", ["saturday", "sunday"]),
    ("every day", ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]),
    (["Mon", "wed", "Friday"], ["monday", "wednesday", "friday"]),
])
def test_parse_days(days, expected):
    assert parse_days(days) == expected


def test_parse_days_bad():
    with pytest.raises(ToolError, match="Unknown day 'funday'"):
        parse_days(["funday"])


@pytest.mark.parametrize("at,expected", [
    ("07:30", {"type": "time", "time": {"hour": 7, "minute": 30}}),
    ("7am", {"type": "time", "time": {"hour": 7, "minute": 0}}),
    ("10:15 pm", {"type": "time", "time": {"hour": 22, "minute": 15}}),
    ("12am", {"type": "time", "time": {"hour": 0, "minute": 0}}),
    ("sunset", {"type": "sunset"}),
    ("Sunrise", {"type": "sunrise"}),
])
def test_parse_at(at, expected):
    assert parse_at(at) == expected


def test_parse_at_bad():
    with pytest.raises(ToolError, match="Times look like"):
        parse_at("25:00")


def test_schedule_config():
    cfg = schedule_config(ROOM, {"rid": "s2", "rtype": "scene"}, parse_at("7am"), ["monday"], 10)
    assert cfg == {"what": [{"group": {"rid": "r1", "rtype": "room"}, "recall": {"rid": "s2", "rtype": "scene"}}],
                   "when_extended": {"recurrence_days": ["monday"],
                                     "start_at": {"time_point": {"type": "time", "time": {"hour": 7, "minute": 0}},
                                                  "transition": {"minutes": 10}}},
                   "where": WHERE}


def test_wake_sleep_timer_configs():
    tp = parse_at("6:45")
    assert wake_up_config(ROOM, tp, ["monday"], 30, 80) == {
        "end_brightness": 80.0, "fade_in_duration": {"seconds": 1800}, "style": "sunrise",
        "when": {"recurrence_days": ["monday"], "time_point": tp}, "where": WHERE}
    assert go_to_sleep_config(ROOM, tp, ["monday"], 20) == {
        "end_state": "turn_off", "fade_out_duration": {"seconds": 1200}, "style": "basic",
        "when": {"recurrence_days": ["monday"], "time_point": tp}, "where": WHERE}
    off = {"rid": "rc-off", "rtype": "recipe"}
    assert timer_config(ROOM, 10, off) == {"duration": {"seconds": 600},
                                           "what": [{"group": {"rid": "r1", "rtype": "room"}, "recall": off}],
                                           "where": WHERE}


def test_list_automations():
    out = call(FakeBridge(), "list_automations")
    assert "Morning [Schedule, on]: Living room → Read at 07:00 on weekdays [id a1]" in out
    assert "Turn off everything [Schedule, on]: everywhere → off at 00:00 every day [id a2]" in out


def test_set_automation_enabled_trailing_space():
    fake = FakeBridge()
    assert call(fake, "set_automation_enabled", name="turn off everything", enabled=False) == \
        "Turn off everything is now off"
    assert fake.puts == [("behavior_instance", "a2", {"enabled": False})]


def test_create_schedule_scene():
    fake = FakeBridge()
    out = call(fake, "create_schedule", name="Evening", room="living room", scene="relax", at="sunset",
               days="every day")
    assert out == "Created 'Evening': Living room → Relax at sunset every day"
    rtype, body = fake.posts[0]
    assert rtype == "behavior_instance" and body["script_id"] == "sc-schedule" and body["enabled"] is True
    assert body["configuration"]["what"][0]["recall"] == {"rid": "s1", "rtype": "scene"}


def test_create_schedule_off_uses_off_recipe():
    fake = FakeBridge()
    call(fake, "create_schedule", name="Night", room="bedroom", scene="off", at="23:30", days="weekdays")
    assert fake.posts[0][1]["configuration"]["what"][0]["recall"] == {"rid": "rc-off", "rtype": "recipe"}


def test_create_wake_up_and_sleep_and_timer():
    fake = FakeBridge()
    call(fake, "create_wake_up", name="Wake", room="bedroom", at="6:30", days="weekdays", fade_minutes=20)
    call(fake, "create_go_to_sleep", name="Bed", room="bedroom", at="22:30", days="every day")
    call(fake, "create_timer", name="Nap", room="bedroom", minutes=45)
    assert [b["script_id"] for _, b in fake.posts] == ["sc-wake", "sc-sleep", "sc-timer"]


def test_update_automation_time_and_days():
    fake = FakeBridge()
    out = call(fake, "update_automation", name="morning", at="6:45", days="every day")
    assert out == "Updated 'Morning': Living room → Read at 06:45 every day"
    body = fake.puts[0][2]
    start = body["configuration"]["when_extended"]
    assert start["start_at"]["time_point"]["time"] == {"hour": 6, "minute": 45}
    assert len(start["recurrence_days"]) == 7


def test_update_automation_nothing():
    with pytest.raises(ToolError, match="Nothing to change"):
        call(FakeBridge(), "update_automation", name="morning")


def test_delete_automation():
    fake = FakeBridge()
    assert call(fake, "delete_automation", name="morning") == "Deleted automation 'Morning'"
    assert fake.deletes == [("behavior_instance", "a1")]
