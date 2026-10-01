"""Apple TV tools: status, power, apps, playback, remote, typing, volume (via appletv.AppleTV), screenshots (tvscreen)."""
from typing import Annotated, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.utilities.types import Image
from pydantic import Field

import tvscreen
from appletv import AppleTV

mcp = FastMCP("Apple TV")
READ = {"readOnlyHint": True}
WRITE = {"readOnlyHint": False, "destructiveHint": False}
_tv: AppleTV | None = None


def tv() -> AppleTV:
    global _tv
    _tv = _tv or AppleTV()
    return _tv


@mcp.tool(annotations=READ)
async def tv_status() -> str:
    """Whether the Apple TV is on, what's playing, and the app it last played in (which may not be
    the app on screen)."""
    s = await tv().status()
    # pyatv's app is the one behind the last playback, not necessarily the one on screen (seen live).
    parts = [s["power"]] + ([f"last played in {s['app']}"] if s.get("app") else [])
    if s.get("title") and s["state"] in ("playing", "paused"):
        parts.append(f"{s['state']} {s['title']}" + (f" — {s['artist']}" if s.get("artist") else ""))
    return "Apple TV: " + " · ".join(parts)


@mcp.tool(annotations=WRITE)
async def tv_power(on: Annotated[bool, Field(description="true = on, false = off (sleep)")]) -> str:
    """Turn the Apple TV (and the TV with it, over HDMI-CEC) on or off."""
    await tv().power(on)
    return f"Apple TV {'on' if on else 'off'}"


@mcp.tool(annotations=READ)
async def tv_list_apps() -> str:
    """The apps installed on the Apple TV."""
    return "\n".join(sorted(a["name"].replace("\xa0", " ") for a in await tv().apps()))


@mcp.tool(annotations=WRITE)
async def tv_open_app(name: Annotated[str, Field(description="App name, e.g. 'YouTube', 'Netflix', 'Disney+'")]) -> str:
    """Open an app on the Apple TV (turning it on if it's asleep)."""
    return f"Opened {await tv().launch(name)}"


async def _control(action: str, past: str) -> str:
    await tv().control(action)
    return past


@mcp.tool(annotations=WRITE)
async def tv_play() -> str:
    """Play on the Apple TV."""
    return await _control("play", "Playing")


@mcp.tool(annotations=WRITE)
async def tv_pause() -> str:
    """Pause the Apple TV."""
    return await _control("pause", "Paused")


@mcp.tool(annotations=WRITE)
async def tv_next() -> str:
    """Skip ahead / next on the Apple TV."""
    return await _control("next", "Skipped")


@mcp.tool(annotations=WRITE)
async def tv_previous() -> str:
    """Back / previous on the Apple TV."""
    return await _control("previous", "Went back")


@mcp.tool(annotations=WRITE)
async def tv_remote(keys: Annotated[list[Literal["up", "down", "left", "right", "select", "menu", "home", "play_pause"]],
                                    Field(min_length=1, max_length=20,
                                          description="Remote buttons to press in order (1–20)")]) -> str:
    """Press Apple TV remote buttons, e.g. to move around a menu."""
    await tv().keys(list(keys))
    return f"Pressed {', '.join(keys)}"


@mcp.tool(annotations=WRITE)
async def tv_volume(
    level: Annotated[int | None, Field(ge=0, le=100, description="Volume 0–100")] = None,
    change: Annotated[int | None, Field(ge=-100, le=100, description="Relative, e.g. -10")] = None,
) -> str:
    """Set the Apple TV's volume. Its sound plays on the living-room HomePods, and this is the same
    volume as theirs: use this or music_set_volume, not both."""
    if (level is None) == (change is None):
        raise ToolError("Give a level (0–100) or a change (e.g. -10), not both.")
    return f"Apple TV volume {await tv().volume(level, change):g}"


@mcp.tool(annotations=WRITE)
async def tv_type(
    text: Annotated[str, Field(max_length=500, description="What to type")],
    mode: Annotated[Literal["replace", "append", "clear"],
                    Field(description="replace what's in the field (default), append to it, or clear it")] = "replace",
) -> str:
    """Type into the text field selected on the Apple TV (a search box, a server address). Select the field
    with tv_remote first. Never type passwords: ask Nicholas to type those himself."""
    now = await tv().type_text(text, mode)
    return "Cleared the field" if mode == "clear" else f"Typed. The field now says: {now}"


@mcp.tool(annotations=READ)
async def tv_read_text() -> str:
    """Whether a text field is selected on the Apple TV, and what it says."""
    focused, text = await tv().read_text()
    return f"A text field is selected. It says: {text}" if focused else "No text field is selected on the Apple TV"


@mcp.tool(annotations=READ)
async def tv_screenshot() -> Image:
    """See what's on the Apple TV screen right now (a 1280-wide JPEG). Use it to find your way around
    menus before and after tv_remote presses. Protected video (Netflix etc.) may show black."""
    return Image(data=await tvscreen.screenshot(), format="jpeg")
