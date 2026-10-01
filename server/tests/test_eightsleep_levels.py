import asyncio

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import eightsleep_mcp
from fake_eightsleep import FakeAPI, make_api

TEMP = {"currentLevel": -100, "currentState": {"type": "smart:bedtime"}, "scheduleType": "smart",
        "smart": {"bedTimeLevel": -95, "initialSleepLevel": -41, "finalSleepLevel": -44},
        "currentSchedule": {"id": "S1", "enabled": True, "time": "22:00:00", "days": ["monday", "tuesday", "wednesday",
                            "thursday", "friday", "saturday", "sunday"], "tags": [],
                            "startSettings": {"bedtime": -95, "elevationPreset": "sleep", "pillowBedtime": -95,
                                              "audioSettings": {"trackId": "pink-noise", "level": 30,
                                                                "stopCriteria": "UntilFallAsleep"}}}}


@pytest.fixture
def fake(tmp_path):
    f = FakeAPI()
    f.routes[("GET", "app:/v1/users/U1/temperature")] = (200, TEMP)
    eightsleep_mcp._api = make_api(tmp_path, f)
    return f


def call(tool, **args):
    async def go():
        async with Client(eightsleep_mcp.mcp) as client:
            return (await client.call_tool(tool, args)).content[0].text
    return asyncio.run(go())


def test_get_bedtime(fake):
    out = call("eight_sleep_get_bedtime")
    assert "Bedtime 22:00 every day (on)" in out
    assert "Levels: bedtime -9.5, early night -4.1, late night -4.4" in out
    assert "Sound: pink-noise at 30 until you fall asleep" in out
    assert "Right now: smart:bedtime" in out


def test_set_sleep_levels_merges(fake):
    assert call("eight_sleep_set_sleep_levels", late_night=-6) == \
        "Levels now: bedtime -9.5, early night -4.1, late night -6"
    assert fake.api_calls("PUT") == [("PUT", "app:/v1/users/U1/temperature",
                                      {"smart": {"bedTimeLevel": -95, "initialSleepLevel": -41, "finalSleepLevel": -60}})]


def test_set_sleep_levels_nothing(fake):
    with pytest.raises(ToolError, match="Nothing to change"):
        call("eight_sleep_set_sleep_levels")


def test_adjust_tonight_clamps(fake):
    out = call("eight_sleep_shift_levels", change=-1)
    assert out == ("Levels now: bedtime -10, early night -5.1, late night -5.4 "
                   "(bedtime was already near the coolest; saved for every night)")
    assert fake.api_calls("PUT")[0][2]["smart"] == {"bedTimeLevel": -100, "initialSleepLevel": -51, "finalSleepLevel": -54}


def test_levels_are_ints(fake):
    call("eight_sleep_set_sleep_levels", bedtime=-9.5)
    assert isinstance(fake.api_calls("PUT")[0][2]["smart"]["bedTimeLevel"], int)


def test_set_bedtime_time_and_days(fake):
    assert call("eight_sleep_set_bedtime", time="22:30", days="weekdays") == "Bedtime now 22:30 on weekdays (on)"
    method, where, body = fake.api_calls("PUT")[0]
    assert where == "app:/v1/users/U1/bedtime" and body["scheduleType"] == "smart"
    sched = body["schedules"][0]
    assert sched["id"] == "S1" and sched["time"] == "22:30:00" and sched["days"] == [
        "monday", "tuesday", "wednesday", "thursday", "friday"]
    assert sched["startSettings"]["audioSettings"]["trackId"] == "pink-noise"
