import asyncio

import pytest
from fastmcp import Client

import eightsleep_mcp
from fake_eightsleep import FakeAPI, make_api


@pytest.fixture
def fake(tmp_path):
    f = FakeAPI()
    f.routes[("GET", "client:/users/U1/current-device")] = (200, {"id": "D1", "side": "solo", "specialization": "pod"})
    f.routes[("GET", "client:/devices/D1")] = (200, {"result": {
        "online": True, "hasWater": True, "needsPriming": False, "priming": False, "lastPrime": "2026-09-20T10:00:00Z",
        "firmwareVersion": "2.5.1", "firmwareUpdating": False, "leftHeatingLevel": -70, "leftTargetHeatingLevel": -73}})
    f.routes[("GET", "client:/users/U1/trends")] = (200, {"days": [
        {"day": "2026-09-26", "presenceStart": "2026-09-26T22:30:00Z"}]})
    eightsleep_mcp._api = make_api(tmp_path, f)
    return f


def call(tool, **args):
    async def go():
        async with Client(eightsleep_mcp.mcp) as client:
            return (await client.call_tool(tool, args)).content[0].text
    return asyncio.run(go())


def test_pod_status(fake):
    out = call("eight_sleep_pod_status")
    assert "Online, firmware 2.5.1" in out
    assert "Water: OK; doesn't need priming (last primed 2026-09-20)" in out
    assert "Bed now -7, heading to -7.3" in out
    assert "In bed: probably yes (since 2026-09-26T22:30:00Z)" in out


def test_pod_status_needs_water(fake):
    fake.routes[("GET", "client:/devices/D1")] = (200, {"result": {"online": True, "hasWater": False,
                                                                     "needsPriming": True, "priming": False}})
    assert "Water: LOW — add water, then prime" in call("eight_sleep_pod_status")


def test_prime(fake):
    assert call("eight_sleep_prime") == "Priming started; it takes a while and is noisy"
    assert fake.api_calls("POST") == [("POST", "app:/v1/devices/D1/priming/tasks",
                                       {"notifications": {"users": ["U1"], "meta": "fill_pod"}})]


def test_prime_already_running(fake):
    fake.routes[("POST", "app:/v1/devices/D1/priming/tasks")] = (409, {"message": "conflict"})
    assert call("eight_sleep_prime") == "The Pod is already priming"


def test_cancel_priming(fake):
    assert call("eight_sleep_cancel_priming") == "Priming cancelled"
    assert fake.api_calls("DELETE") == [("DELETE", "app:/v1/devices/D1/priming/tasks", None)]
