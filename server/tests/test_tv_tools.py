import asyncio

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import tv_mcp
from appletv import TVError


class FakeTV:
    def __init__(self, power="off"):
        self.calls, self._power = [], power

    async def status(self):
        return {"power": self._power, "app": "YouTube", "state": "playing", "title": "Lo-fi", "artist": None}

    async def power(self, on):
        self.calls.append(("power", on))

    async def apps(self):
        return [{"name": "YouTube", "id": "y"}, {"name": "Netflix", "id": "n"}]

    async def launch(self, name):
        self.calls.append(("launch", name))
        return "YouTube"

    async def control(self, action):
        self.calls.append(("control", action))

    async def keys(self, keys):
        if "jump" in keys:
            raise TVError("Unknown remote key(s): jump.")
        self.calls.append(("keys", keys))

    async def volume(self, level, change):
        self.calls.append(("volume", level, change))
        return 30.0


def call(tool, **args):
    async def go():
        async with Client(tv_mcp.mcp) as c:
            return (await c.call_tool(tool, args)).content[0].text
    return asyncio.run(go())


@pytest.fixture
def tv():
    tv_mcp._tv = FakeTV()
    return tv_mcp._tv


def test_status(tv):
    # The app pyatv reports is the one behind the last thing played (live: said YouTube with Netflix on screen).
    assert call("tv_status") == "Apple TV: off · last played in YouTube · playing Lo-fi"


def test_power_and_open_app(tv):
    assert call("tv_power", on=True) == "Apple TV on"
    assert call("tv_open_app", name="youtube") == "Opened YouTube"
    assert tv.calls == [("power", True), ("launch", "youtube")]


def test_open_app_wakes_the_tv(tv):
    # launch() itself wakes the TV (AppleTV.launch turns it on first); the tool just asks for it.
    call("tv_open_app", name="netflix")
    assert tv.calls == [("launch", "netflix")]


def test_list_apps(tv):
    assert call("tv_list_apps") == "Netflix\nYouTube"


def test_playback(tv):
    for tool, action in (("tv_play", "play"), ("tv_pause", "pause"), ("tv_next", "next"), ("tv_previous", "previous")):
        call(tool)
    assert [c[1] for c in tv.calls] == ["play", "pause", "next", "previous"]


def test_remote(tv):
    assert call("tv_remote", keys=["down", "down", "select"]) == "Pressed down, down, select"


def test_remote_rejects_unknown_key(tv):
    with pytest.raises(ToolError, match="Unknown remote key|Input should be"):  # rejected before anything is sent
        call("tv_remote", keys=["jump"])


def test_volume_needs_one(tv):
    with pytest.raises(ToolError, match="not both"):
        call("tv_volume")
    assert call("tv_volume", change=-10) == "Apple TV volume 30"


class KeyboardTV(FakeTV):
    def __init__(self, focused=True):
        super().__init__()
        self.focused = focused

    async def type_text(self, text, mode):
        if not self.focused:
            raise TVError("No text field is selected on the Apple TV.")
        self.calls.append(("type", text, mode))
        return "" if mode == "clear" else text

    async def read_text(self):
        return (True, "infuse") if self.focused else (False, None)


def test_tv_type():
    tv_mcp._tv = KeyboardTV()
    assert call("tv_type", text="infuse") == "Typed. The field now says: infuse"
    assert tv_mcp._tv.calls == [("type", "infuse", "replace")]


def test_tv_type_clear():
    tv_mcp._tv = KeyboardTV()
    assert call("tv_type", text="", mode="clear") == "Cleared the field"


def test_tv_type_unfocused():
    tv_mcp._tv = KeyboardTV(focused=False)
    with pytest.raises(ToolError, match="No text field"):
        call("tv_type", text="x")


def test_tv_read_text():
    tv_mcp._tv = KeyboardTV()
    assert call("tv_read_text") == "A text field is selected. It says: infuse"
    tv_mcp._tv = KeyboardTV(focused=False)
    assert call("tv_read_text") == "No text field is selected on the Apple TV"


def test_tv_screenshot(monkeypatch):
    async def shot():
        return b"\xff\xd8jpeg"
    monkeypatch.setattr(tv_mcp.tvscreen, "screenshot", shot)

    async def go():
        async with Client(tv_mcp.mcp) as c:
            return (await c.call_tool("tv_screenshot", {})).content[0]
    img = asyncio.run(go())
    assert img.type == "image" and img.mimeType == "image/jpeg"
