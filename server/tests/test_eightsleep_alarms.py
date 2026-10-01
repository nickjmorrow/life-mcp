import asyncio

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import eightsleep_mcp
from eightsleep_mcp import alarm_update_body, app_level, find_alarm, hhmmss, parse_days, parse_time, to_api_level
from fake_eightsleep import FakeAPI, make_api

DAILY = {"id": "A1", "enabled": True, "time": "07:20:00",
         "repeat": {"enabled": True, "weekDays": {d: True for d in ("monday", "tuesday", "wednesday", "thursday",
                                                                    "friday", "saturday", "sunday")}},
         "vibration": {"enabled": True, "powerLevel": 50, "pattern": "INTENSE"},
         "thermal": {"enabled": True, "level": 60}, "audio": {"enabled": False, "level": 30},
         "smart": {"lightSleepEnabled": True, "sleepCapEnabled": False, "sleepCapMinutes": 480},
         "skipNext": False, "tags": [], "nextTimestamp": "x", "startTimestamp": "x", "endTimestamp": "x",
         "dismissedUntil": "x", "skippedUntil": "x", "snoozedUntil": "x", "snoozing": False}
NAP = {**DAILY, "id": "A2", "time": "18:26:56", "repeat": {"enabled": False, "weekDays": {}},
       "tags": ["temporary-mode", "oneOff-napMode"]}


@pytest.fixture
def fake(tmp_path):
    f = FakeAPI()
    f.routes[("GET", "app:/v2/users/U1/alarms")] = (200, {"alarms": [DAILY, NAP], "recommendedAlarm": DAILY})
    eightsleep_mcp._api = make_api(tmp_path, f)
    return f


def call(tool, **args):
    async def go():
        async with Client(eightsleep_mcp.mcp) as client:
            return (await client.call_tool(tool, args)).content[0].text
    return asyncio.run(go())


@pytest.mark.parametrize("text,expected", [("7:20", "07:20:00"), ("7:20am", "07:20:00"), ("6:45 pm", "18:45:00"),
                                           ("07:20:00", "07:20:00"), ("7am", "07:00:00"), ("12am", "00:00:00")])
def test_parse_time(text, expected):
    assert parse_time(text) == expected


def test_parse_time_bad():
    with pytest.raises(ToolError, match="Times look like"):
        parse_time("half past")


def test_levels():
    assert to_api_level(-3) == -30 and to_api_level(2.5) == 25
    assert app_level(-95) == "-9.5" and app_level(40) == "+4"
    with pytest.raises(ToolError, match="between -10 and \\+10"):
        to_api_level(11)


def test_parse_days_and_hhmmss():
    assert parse_days("weekdays") == ["monday", "tuesday", "wednesday", "thursday", "friday"]
    assert hhmmss(20) == "00:20:00" and hhmmss(95) == "01:35:00"


def test_find_alarm_by_spoken_time():
    for key in ("7:20", "7:20am", "07:20", "A1"):
        assert find_alarm([DAILY, NAP], key)["id"] == "A1"
    with pytest.raises(ToolError, match="No alarm at 09:00. Alarms: 07:20, 18:26"):
        find_alarm([DAILY, NAP], "9am")


def test_alarm_update_body_drops_computed():
    body = alarm_update_body(DAILY)
    for k in ("nextTimestamp", "startTimestamp", "endTimestamp", "dismissedUntil", "snoozedUntil", "skippedUntil"):
        assert k not in body
    assert body["id"] == "A1" and body["time"] == "07:20:00"


def test_list_alarms(fake):
    out = call("eight_sleep_list_alarms")
    assert "07:20 every day, on — vibration intense 50, heat 60, smart wake [id A1]" in out
    assert "18:26 once (nap), on" in out


def test_create_alarm_copies_daily_settings(fake):
    out = call("eight_sleep_create_alarm", time="6:45", days="weekdays")
    assert out.startswith("Created alarm 06:45 on weekdays")
    method, where, body = fake.api_calls("POST")[0]
    assert where == "app:/v1/users/U1/alarms"
    assert body["time"] == "06:45:00" and body["enabled"] is True
    assert body["repeat"]["weekDays"]["monday"] is True and body["repeat"]["weekDays"]["sunday"] is False
    assert body["vibration"] == {"enabled": True, "powerLevel": 50, "pattern": "INTENSE"}
    assert body["thermal"] == {"enabled": True, "level": 60} and body["smart"]["lightSleepEnabled"] is True


def test_create_one_off_alarm(fake):
    call("eight_sleep_create_alarm", time="5am", heat=False)
    body = fake.api_calls("POST")[0][2]
    assert body["repeat"] == {"enabled": False, "weekDays": {}} and body["thermal"]["enabled"] is False


def test_update_alarm(fake):
    assert call("eight_sleep_update_alarm", alarm="7:20", time="7:05", vibration_strength=80) == \
        "Updated alarm: 07:05 every day, on — vibration intense 80, heat 60, smart wake [id A1]"
    method, where, body = fake.api_calls("PUT")[0]
    assert where == "app:/v1/users/U1/alarms/A1" and body["time"] == "07:05:00"
    assert body["vibration"]["powerLevel"] == 80 and "nextTimestamp" not in body


def test_set_enabled_and_skip(fake):
    call("eight_sleep_set_alarm_enabled", alarm="7:20", enabled=False)
    assert fake.api_calls("PUT")[-1][2]["enabled"] is False
    assert call("eight_sleep_skip_next_alarm", alarm="7:20") == "The next 07:20 alarm will be skipped"
    assert fake.api_calls("PUT")[-1][2]["skipNext"] is True
    assert call("eight_sleep_skip_next_alarm", alarm="7:20", skip=False) == "The next 07:20 alarm will ring"


def test_delete_alarm(fake):
    assert call("eight_sleep_delete_alarm", alarm="7:20") == "Deleted the 07:20 alarm"
    assert fake.api_calls("DELETE") == [("DELETE", "app:/v1/users/U1/alarms/A1", None)]


def test_writes_respect_gate(tmp_path):
    f = FakeAPI()
    f.routes[("GET", "app:/v2/users/U1/alarms")] = (200, {"alarms": [DAILY]})
    eightsleep_mcp._api = make_api(tmp_path, f, mutations=False)
    with pytest.raises(ToolError, match="Changing the bed is switched off"):
        call("eight_sleep_skip_next_alarm", alarm="7:20")
    assert f.api_calls("PUT") == []


def test_describe_shows_skip_from_skipped_until():
    # Eight Sleep stores a skip as skippedUntil (and resets skipNext to false), per the live check.
    from eightsleep_mcp import describe_alarm
    skipped = {**DAILY, "skipNext": False, "skippedUntil": "2999-01-01T12:20:00Z"}
    assert "next one skipped" in describe_alarm(skipped)
    assert "next one skipped" not in describe_alarm({**DAILY, "skippedUntil": "1970-01-01T00:00:00Z"})
