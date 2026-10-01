"""Findings from the Eight Sleep review, each pinned by a test."""
import asyncio
import json
import os
import stat
import threading
import time

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import eightsleep_mcp
from fake_eightsleep import FakeAPI, make_api
from test_eightsleep_alarms import DAILY
from test_eightsleep_levels import TEMP


def run(coro):
    return asyncio.run(coro)


def call(tool, **args):
    async def go():
        async with Client(eightsleep_mcp.mcp) as client:
            return (await client.call_tool(tool, args)).content[0].text
    return asyncio.run(go())


def tokens_path(tmp_path):
    return tmp_path / ".eight-sleep-mcp" / "tokens.json"


# 1. Shared token file: locked, atomic, 0600; reuse a token the other server just saved.

def test_login_writes_atomically_with_0600_and_releases_lock(tmp_path):
    api = make_api(tmp_path, FakeAPI(), expired=True)
    run(api.request("GET", "app", "/v1/users/{uid}/temperature"))
    path = tokens_path(tmp_path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert not path.with_name("tokens.json.lock").exists()
    assert [p.name for p in path.parent.iterdir() if ".tmp" in p.name] == []


def test_login_waits_for_the_npm_servers_lock(tmp_path):
    fake = FakeAPI()
    api = make_api(tmp_path, fake, expired=True)
    lock = tokens_path(tmp_path).with_name("tokens.json.lock")
    lock.write_text("123\n")

    def npm_server_finishes():  # the other server logs in, saves a fresh token, releases the lock
        time.sleep(0.3)
        tokens_path(tmp_path).write_text(json.dumps(
            {"access_token": "FROM_NPM", "expires_at": int(time.time()) + 3600, "user_id": "U1"}))
        lock.unlink()

    threading.Thread(target=npm_server_finishes).start()
    run(api.request("GET", "app", "/v1/users/{uid}/temperature"))
    assert fake.logins == 0  # used the npm server's fresh token instead of logging in again


# 2. A 401: first try the token the other server may have saved since.

def test_401_uses_newer_saved_token_before_logging_in(tmp_path):
    fake = FakeAPI()
    api = make_api(tmp_path, fake)
    run(api.request("GET", "app", "/v1/users/{uid}/temperature"))  # caches "OLD"
    tokens_path(tmp_path).write_text(json.dumps(
        {"access_token": "FROM_NPM", "expires_at": int(time.time()) + 3600, "user_id": "U1"}))
    fake.expire_once = True
    run(api.request("GET", "app", "/v1/users/{uid}/temperature"))
    assert fake.logins == 0


# 3. Editing a skipped alarm keeps the skip (the API clears it unless skipNext stays true).

SKIPPED = {**DAILY, "skipNext": False, "skippedUntil": "2999-01-01T12:20:00Z"}


@pytest.fixture
def skipped(tmp_path):
    f = FakeAPI()
    f.routes[("GET", "app:/v2/users/U1/alarms")] = (200, {"alarms": [SKIPPED]})
    eightsleep_mcp._api = make_api(tmp_path, f)
    return f


def test_update_keeps_skip(skipped):
    call("eight_sleep_update_alarm", alarm="7:20", vibration_strength=70)
    assert skipped.api_calls("PUT")[0][2]["skipNext"] is True


def test_enable_keeps_skip(skipped):
    call("eight_sleep_set_alarm_enabled", alarm="7:20", enabled=True)
    assert skipped.api_calls("PUT")[0][2]["skipNext"] is True


def test_unskip_clears_it(skipped):
    assert call("eight_sleep_skip_next_alarm", alarm="7:20", skip=False) == "The next 07:20 alarm will ring"
    assert skipped.api_calls("PUT")[0][2]["skipNext"] is False


# 4. set_bedtime keeps every other schedule.

def test_set_bedtime_keeps_other_schedules(tmp_path):
    f = FakeAPI()
    weekend = {"id": "S2", "enabled": True, "time": "23:30:00", "days": ["saturday", "sunday"], "tags": [],
               "startSettings": {"bedtime": -80}}
    f.routes[("GET", "app:/v1/users/U1/temperature")] = (200, TEMP)
    f.routes[("GET", "app:/v1/users/U1/temperature/all")] = (200, {"schedules": [TEMP["currentSchedule"], weekend]})
    eightsleep_mcp._api = make_api(tmp_path, f)
    call("eight_sleep_set_bedtime", time="22:30")
    schedules = f.api_calls("PUT")[0][2]["schedules"]
    assert [(s["id"], s["time"]) for s in schedules] == [("S1", "22:30:00"), ("S2", "23:30:00")]


# 5. Shifting the levels says it's every night, and refuses when there are none.

def test_shift_levels_is_honest(tmp_path):
    f = FakeAPI()
    f.routes[("GET", "app:/v1/users/U1/temperature")] = (200, TEMP)
    eightsleep_mcp._api = make_api(tmp_path, f)
    out = call("eight_sleep_shift_levels", change=-1)
    assert "saved for every night" in out
    tools = {t.name: t for t in asyncio.run(eightsleep_mcp.mcp.list_tools())}
    assert "eight_sleep_adjust_tonight" not in tools
    assert "every night" in tools["eight_sleep_shift_levels"].description


def test_shift_levels_without_levels(tmp_path):
    f = FakeAPI()
    f.routes[("GET", "app:/v1/users/U1/temperature")] = (200, {"scheduleType": "timeBased"})
    eightsleep_mcp._api = make_api(tmp_path, f)
    with pytest.raises(ToolError, match="no smart sleep levels"):
        call("eight_sleep_shift_levels", change=-1)
    assert f.api_calls("PUT") == []


# 6. In bed: tomorrow's date included, the device's timezone, "None" means no end.

def pod(tmp_path, days, tz="America/New_York"):
    f = FakeAPI()
    f.routes[("GET", "client:/users/U1/current-device")] = (200, {"id": "D1", "side": "solo", "timeZone": tz})
    f.routes[("GET", "client:/devices/D1")] = (200, {"result": {"online": True, "hasWater": True}})
    f.routes[("GET", "client:/users/U1/trends")] = (200, {"days": days})
    eightsleep_mcp._api = make_api(tmp_path, f)
    return f


def test_in_bed_none_string_means_no_end(tmp_path):
    pod(tmp_path, [{"day": "x", "presenceStart": "2026-09-26T03:00:00Z", "presenceEnd": "None"}])
    assert "In bed: probably yes (since 2026-09-26T03:00:00Z)" in call("eight_sleep_pod_status")


def test_in_bed_query_uses_device_tz_and_includes_tomorrow(tmp_path):
    import datetime as dt
    f = pod(tmp_path, [])
    call("eight_sleep_pod_status")
    trends = [c for c in f.calls if c[1] == "client:/users/U1/trends"]
    assert trends  # parameters are checked through the recorded request below
    req = f.last_params["client:/users/U1/trends"]
    assert req["tz"] == "America/New_York"
    assert req["to"] > req["from"] and req["to"] >= str(dt.date.today() + dt.timedelta(days=1))
