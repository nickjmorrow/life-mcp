import asyncio
import json

import pytest

import tvscreen
from tvscreen import ScreenError, pick_tv

TV = {"identifier": "D1D1", "hardwareProperties": {"deviceType": "appleTV", "reality": "physical"}}
SIM = {"identifier": "E05E", "hardwareProperties": {"deviceType": "iPhone", "reality": "simulated"}}


def test_pick_tv():
    assert pick_tv({"result": {"devices": [SIM, TV]}}) == "D1D1"


def test_pick_tv_before_first_connection():
    # A freshly paired TV that hasn't connected yet has no "reality" field (seen live, 2026-10-01).
    fresh = {"identifier": "F1F1", "hardwareProperties": {"deviceType": "appleTV"}}
    assert pick_tv({"result": {"devices": [fresh]}}) == "F1F1"


def test_pick_tv_skips_simulated_tv():
    sim_tv = {"identifier": "S1S1", "hardwareProperties": {"deviceType": "appleTV", "reality": "simulated"}}
    with pytest.raises(ScreenError, match="isn't paired"):
        pick_tv({"result": {"devices": [sim_tv]}})


def test_pick_tv_not_paired():
    with pytest.raises(ScreenError, match="isn't paired"):
        pick_tv({"result": {"devices": [SIM]}})


def fake_run(capture_ok=True, capture_out=""):
    calls = []

    async def run(*args, timeout=None):
        calls.append(args)
        if args[2] == "list":
            open(args[-1], "w").write(json.dumps({"result": {"devices": [TV]}}))
        elif args[2] == "device":
            if not capture_ok:
                return 1, capture_out
            open(args[-1], "wb").write(b"png")
        elif args[0] == "sips":
            open(args[-1], "wb").write(b"jpeg")
        return 0, ""
    return run, calls


def test_screenshot():
    run, calls = fake_run()
    assert asyncio.run(tvscreen.screenshot(run)) == b"jpeg"
    assert calls[1][:7] == ("xcrun", "devicectl", "device", "capture", "screenshot", "--device", "D1D1")


def test_screenshot_tv_unreachable():
    run, _ = fake_run(capture_ok=False, capture_out="This device does not support tunnel connections.")
    with pytest.raises(ScreenError, match="Is it awake"):
        asyncio.run(tvscreen.screenshot(run))
