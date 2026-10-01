import asyncio

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import home_mcp
from fake_home import FakeHome


def setup(**kw):
    fake = FakeHome(**kw)
    home_mcp._transport = fake.transport()
    return fake


def call(tool, **args):
    async def go():
        async with Client(home_mcp.mcp) as c:
            return (await c.call_tool(tool, args)).content[0].text
    return asyncio.run(go())


def test_list():
    setup()
    out = call("home_list")
    assert "Living Room: Front Camera (IP Camera): motion detected, light 42.5 lux" in out
    assert "Bedroom: Lamp (Lightbulb): off, brightness 60%" in out
    assert "Living Room: Lamp (Lightbulb): NOT REACHABLE" in out
    assert "Scenes: Good Night, Movie Time" in out
    assert "Automations: Porch at sunset (on), Morning lights (off)" in out


def test_get():
    setup()
    out = call("home_get", accessory="front camera")
    assert "Motion Detected: yes (read-only)" in out and "Current Light Level: 42.5 lux (read-only)" in out


@pytest.mark.parametrize("setting,value,expected", [("power", "on", True), ("on", "off", False),
                                                    ("brightness", "35", 35), ("Brightness", 35, 35)])
def test_set_friendly_names(setting, value, expected):
    fake = setup()
    call("home_set", accessory="bedroom lamp", setting=setting, value=value)
    assert fake.posts[-1][0] == "/write" and fake.posts[-1][1]["value"] == expected


def test_set_value_checks():
    setup()
    with pytest.raises(ToolError, match="Brightness goes from 0 to 100"):
        call("home_set", accessory="bedroom lamp", setting="brightness", value="150")
    with pytest.raises(ToolError, match="Power State takes on/off"):
        call("home_set", accessory="bedroom lamp", setting="power", value="maybe")
    with pytest.raises(ToolError, match="Motion Detected can't be changed"):
        call("home_set", accessory="front camera", setting="motion detected", value="on")


def test_duplicate_names_by_room():
    setup()
    with pytest.raises(ToolError, match="'lamp' could mean: Bedroom Lamp, Living Room Lamp"):
        call("home_set", accessory="lamp", setting="power", value="on")


def test_unreachable_accessory():
    setup()
    with pytest.raises(ToolError, match="Living Room Lamp isn't reachable"):
        call("home_set", accessory="living room lamp", setting="power", value="off")


def test_app_not_running():
    setup(down=True)
    with pytest.raises(ToolError, match="Life Home isn.t running on this Mac"):
        call("home_list")


def test_scene_and_automations():
    fake = setup()
    assert call("home_run_scene", name="good night") == "Ran Good Night"
    assert fake.posts[-1] == ("/scene", {"id": "SC1"})
    assert call("home_set_automation", name="morning lights", enabled=True) == "Morning lights is now on"
    assert fake.posts[-1] == ("/automation", {"id": "T2", "enabled": True})
    out = call("home_list_automations")
    assert "Porch at sunset: on — runs Movie Time; Lamp: Power State → 1" in out
    assert "Morning lights: off — its actions aren't visible" in out


def test_motion():
    setup()
    assert call("home_motion") == "Living Room — Front Camera: motion detected"


def test_repeated_setting_names_use_the_service():
    fake = setup()
    with pytest.raises(ToolError, match="'volume' could mean: Microphone Volume, Speaker Volume"):
        call("home_set", accessory="front camera", setting="volume", value=50)
    call("home_set", accessory="front camera", setting="speaker volume", value=50)
    assert fake.posts[-1] == ("/write", {"characteristic": "C7", "value": 50})
    out = call("home_get", accessory="front camera")
    assert "Speaker Volume: 88%" in out and "Custom" not in out


def test_token_is_made_once_private_and_sent(tmp_path):
    import stat
    path = home_mcp.TOKEN_PATH
    assert not path.exists()
    setup()
    call("home_list")  # the fake refuses requests without the right header
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    first = path.read_text().strip()
    assert len(first) >= 40 and home_mcp.token() == first


def test_wrong_token_is_refused():
    home_mcp.token()
    home_mcp._transport = FakeHome(token="someone-else").transport()
    with pytest.raises(ToolError, match="forbidden"):
        call("home_list")


def test_empty_token_file_is_replaced(tmp_path):
    p = tmp_path / "t" / "home-token"
    p.parent.mkdir()
    p.write_text("")
    assert len(home_mcp.token(p)) >= 40
