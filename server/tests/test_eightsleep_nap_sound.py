import asyncio

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import eightsleep_mcp
from fake_eightsleep import FakeAPI, make_api

NAP = "app:/v1/users/U1/temperature/nap-mode"
AUDIO = "app:/v1/users/U1/audio"


@pytest.fixture
def fake(tmp_path):
    f = FakeAPI()
    f.routes[("GET", NAP)] = (200, {"defaultDuration": "00:20:00", "defaultLevels": {"pod": -30, "pillow": -30},
                                    "alarmRequested": False})
    f.routes[("GET", f"{NAP}/status")] = (404, {"message": "No active nap"})
    f.routes[("GET", f"{AUDIO}/tracks")] = (200, {"tracks": [{"id": "pink-noise", "name": "Pink Noise"},
                                                             {"id": "rain", "name": "Gentle Rain"}]})
    f.routes[("GET", f"{AUDIO}/player")] = (200, {"state": "Playing", "volume": 30,
                                                  "currentTrack": {"id": "rain", "name": "Gentle Rain"}})
    eightsleep_mcp._api = make_api(tmp_path, f)
    return f


def call(tool, **args):
    async def go():
        async with Client(eightsleep_mcp.mcp) as client:
            return (await client.call_tool(tool, args)).content[0].text
    return asyncio.run(go())


def test_start_nap_defaults(fake):
    assert call("eight_sleep_start_nap") == "Nap started: 20 min at -3, no alarm"
    assert fake.api_calls("POST") == [("POST", f"{NAP}/activate", {"duration": "00:20:00",
                                                                    "levels": {"pod": -30, "pillow": -30},
                                                                    "alarmRequested": False})]


def test_start_nap_custom(fake):
    call("eight_sleep_start_nap", minutes=45, level=-5, alarm=True)
    body = fake.api_calls("POST")[0][2]
    assert body == {"duration": "00:45:00", "levels": {"pod": -50, "pillow": -30}, "alarmRequested": True}


def test_extend_and_status_idle(fake):
    assert call("eight_sleep_extend_nap", minutes=15) == "Nap extended by 15 min"
    assert fake.api_calls("POST")[0] == ("POST", f"{NAP}/extend", {"additionalDuration": "00:15:00"})
    assert call("eight_sleep_nap_status") == "No nap running."


def test_end_nap_falls_back_to_post(fake):
    fake.routes[("PUT", f"{NAP}/deactivate")] = (405, {"message": "Method Not Allowed"})
    assert call("eight_sleep_end_nap") == "Nap ended"
    assert [c[0] for c in fake.api_calls() if c[1] == f"{NAP}/deactivate"] == ["PUT", "POST"]


def test_list_sounds(fake):
    assert call("eight_sleep_list_sounds") == "Gentle Rain [rain]\nPink Noise [pink-noise]"


def test_list_sounds_no_speaker(fake):
    fake.routes[("GET", f"{AUDIO}/tracks")] = (404, {"message": "No Associated Speaker"})
    assert call("eight_sleep_list_sounds") == "This Pod has no speaker paired, so it can't play sounds."


def test_play_sound_by_loose_name(fake):
    assert call("eight_sleep_play_sound", track="rain", volume=25) == "Playing Gentle Rain at 25 (until stopped)"
    puts = fake.api_calls("PUT")
    assert puts == [("PUT", f"{AUDIO}/player/currentTrack", {"id": "rain", "stopCriteria": "ManualStop"}),
                    ("PUT", f"{AUDIO}/player/volume", {"volume": 25}),
                    ("PUT", f"{AUDIO}/player/state", {"state": "Playing"})]


def test_play_sound_unknown(fake):
    with pytest.raises(ToolError, match="No sound called 'whale song'. Sounds: Gentle Rain, Pink Noise"):
        call("eight_sleep_play_sound", track="whale song")


def test_stop_volume_status(fake):
    assert call("eight_sleep_stop_sound") == "Sound stopped"
    assert fake.api_calls("PUT")[-1] == ("PUT", f"{AUDIO}/player/state", {"state": "Paused"})
    assert call("eight_sleep_set_sound_volume", volume=10) == "Volume 10"
    assert call("eight_sleep_sound_status") == "Playing Gentle Rain at volume 30"


def test_play_sound_no_speaker(fake):
    # His Pod lists tracks but has no speaker paired: the player answers 404.
    fake.routes[("GET", f"{AUDIO}/player")] = (404, {"message": "No Associated Speaker"})
    with pytest.raises(ToolError, match="no speaker paired"):
        call("eight_sleep_play_sound", track="rain")
    assert fake.api_calls("PUT") == []


def test_start_nap_uses_his_alarm_default(fake):
    fake.routes[("GET", NAP)] = (200, {"defaultDuration": "00:20:00", "defaultLevels": {"pod": -100, "pillow": -100},
                                       "alarmRequested": True})
    assert call("eight_sleep_start_nap") == "Nap started: 20 min at -10, alarm at the end"
    assert fake.api_calls("POST")[0][2]["alarmRequested"] is True
