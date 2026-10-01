"""Shared pieces for the Hue tool modules: the FastMCP server, the bridge, helpers."""
import asyncio
import os
from typing import Annotated, Callable, Literal

from fastmcp import FastMCP
from pydantic import Field

from hue import Bridge, Group, Home, HueError, _candidates, match, match_all

LIGHT_GAP_S = 0.1  # the bridge takes about 10 light commands a second

mcp = FastMCP(
    "Hue",
    instructions="Controls the Philips Hue lights in Nicholas's home. "
                 "If you don't know the room, light or scene names, call hue_list_home first. "
                 "Tools that delete say so: confirm with Nicholas before calling them.",
)

_bridge: Bridge | None = None


def bridge() -> Bridge:
    global _bridge
    if _bridge is None:
        ip, key = os.environ.get("HUE_BRIDGE_IP"), os.environ.get("HUE_APP_KEY")
        if not (ip and key):
            raise HueError("Hue isn't set up on this Mac: HUE_BRIDGE_IP / HUE_APP_KEY are missing "
                           "from ~/.zshrc.local. Run pair.py in life-mcp/hue (see its README).")
        _bridge = Bridge(ip, key)
    return _bridge


async def put_lights(home: Home, light_ids: list[str], body: dict | Callable[[str], dict]) -> str:
    """Send body (or body(light_id)) to each light, paced for the bridge. One bulb
    failing doesn't stop the rest; the returned note names the ones that didn't respond."""
    failed, error = [], None
    for n, light_id in enumerate(light_ids):
        if n:
            await asyncio.sleep(LIGHT_GAP_S)
        try:
            await bridge().put("light", light_id, body(light_id) if callable(body) else body)
        except HueError as e:
            failed.append(light_name(home, light_id))
            error = e
    if error and len(failed) == len(light_ids):
        raise error
    return f" ({', '.join(failed)} didn't respond)" if failed else ""


def group_of(home: Home, name: str) -> Group:
    return home.groups[match(name, {g.id: g.name for g in home.groups.values()}, "room or zone")]


def light_name(home: Home, light_id: str) -> str:
    """The name the Hue app shows: the device's (the light's own name can be stale),
    unless one device has several lights."""
    light = home.lights[light_id]
    device = home.devices.get(light.get("owner", {}).get("rid"), {})
    if sum(s.get("rtype") == "light" for s in device.get("services", [])) == 1:
        return device["metadata"]["name"]
    return light["metadata"]["name"]


def light_names(home: Home) -> dict[str, str]:
    return {i: light_name(home, i) for i in home.lights}


def targets(home: Home, name: str) -> tuple[str, list[str]]:
    """A room or zone's lights, or else the light(s) with that name. Returns (label, ids)."""
    if _candidates(name, {g.id: g.name for g in home.groups.values()}):
        group = group_of(home, name)
        return group.name, group.lights
    ids = match_all(name, light_names(home), "room, zone or light")
    label = light_name(home, ids[0])
    return label + (f" ({len(ids)} lights)" if len(ids) > 1 else ""), ids


_FEATURE = {  # feature → (light key, path to the list of allowed values or None)
    "color": ("color", None),
    "color_temperature": ("color_temperature", None),
    "gradient": ("gradient", None),
    "powerup": ("powerup", None),
    "effect": ("effects_v2", ("action", "effect_values")),
    "timed": ("timed_effects", ("effect_values",)),
    "signal": ("signaling", ("signal_values",)),
}


def capable(home: Home, light_ids: list[str], feature: str, value: str | None = None) -> list[str]:
    """The lights among light_ids that support feature (and value, if given)."""
    key, path = _FEATURE[feature]
    out = []
    for light_id in light_ids:
        section = home.lights.get(light_id, {}).get(key)
        if section is None:
            continue
        if value is not None and path:
            allowed = section
            for p in path:
                allowed = allowed.get(p, {})
            if value not in (allowed or []):
                continue
        out.append(light_id)
    return out


def state(resource: dict) -> str:
    if not resource.get("on", {}).get("on"):
        return "off"
    brightness = resource.get("dimming", {}).get("brightness")
    return "on" if brightness is None else f"on {brightness:.0f}%"


READ = {"readOnlyHint": True}
WRITE = {"readOnlyHint": False, "destructiveHint": False}
DESTROY = {"readOnlyHint": False, "destructiveHint": True}

EFFECTS = ("candle", "fire", "prism", "sparkle", "opal", "glisten", "underwater", "cosmos", "sunbeam",
           "enchant", "none")
On = Annotated[bool | None, Field(description="true turns it on, false turns it off")]
Brightness = Annotated[int | None, Field(ge=0, le=100, description="Percent, 0–100. 0 turns it off.")]
BrightnessChange = Annotated[int | None, Field(ge=-100, le=100, description="Relative change in percentage points: "
                                                                            "+20 is 'a bit brighter', -20 'a bit dimmer'")]
ColorTemp = Annotated[str | None, Field(description="White temperature: 'warm', 'neutral', 'cool', or kelvin 2000–6500")]
Color = Annotated[str | None, Field(description="red, orange, yellow, green, teal, cyan, blue, purple, "
                                                "pink, magenta, white, or a hex code like #ff8800")]
Effect = Annotated[Literal[EFFECTS] | None, Field(description="A light effect, or 'none' to stop one")]
FadeSeconds = Annotated[float | None, Field(gt=0, le=21600, description="Make the change over this many seconds")]
