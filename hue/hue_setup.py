"""Home setup tools (rename, move, zones, rooms) and a health check."""
from typing import Annotated, Literal

from pydantic import Field

from hue import HueError, match, match_all
from hue_common import DESTROY, READ, WRITE, bridge, group_of, light_name, light_names, mcp
from hue_scenes import find_scene

ARCHETYPES = ("living_room", "kitchen", "dining", "bedroom", "kids_bedroom", "bathroom", "nursery", "recreation",
              "office", "gym", "hallway", "toilet", "front_door", "garage", "terrace", "garden", "driveway",
              "carport", "home", "downstairs", "upstairs", "top_floor", "attic", "guest_room", "staircase",
              "lounge", "man_cave", "computer", "studio", "music", "tv", "reading", "closet", "storage",
              "laundry_room", "balcony", "porch", "barbecue", "pool", "other")


def _check(name: str, what: str) -> str:
    name = name.strip()
    if not 1 <= len(name) <= 32:
        raise HueError(f"{what} names are 1–32 characters.")
    return name


def _one_light(home, name: str) -> str:
    ids = match_all(name, light_names(home), "light")
    if len(ids) > 1:
        raise HueError(f"{len(ids)} lights are called '{light_name(home, ids[0])}'. "
                       "Rename one in the Hue app first, or use identify_light to find them.")
    return ids[0]


def _lights(home, names: list[str]) -> list[str]:
    out = []
    for n in names:
        for light_id in match_all(n, light_names(home), "light"):
            if light_id not in out:
                out.append(light_id)
    return out


def _named(home, light_ids) -> str:
    return ", ".join(light_name(home, i) for i in light_ids if i in home.lights) or "no lights"


def _device_children(devices: list[str]) -> list[dict]:
    return [{"rid": d, "rtype": "device"} for d in devices]


def _light_children(lights: list[str]) -> list[dict]:
    return [{"rid": l, "rtype": "light"} for l in lights]


@mcp.tool(annotations=WRITE)
async def rename(
    kind: Annotated[Literal["light", "room", "zone", "scene"], Field(description="What to rename")],
    name: Annotated[str, Field(description="Current name")],
    new_name: Annotated[str, Field(description="New name")],
    room: Annotated[str | None, Field(description="Scenes only: the room, if the scene name repeats")] = None,
) -> str:
    """Rename a light, room, zone or scene."""
    new_name = _check(new_name, kind.capitalize())
    home = await bridge().home()
    if kind == "light":
        light_id = _one_light(home, name)
        device_id = home.lights[light_id]["owner"]["rid"]
        meta = home.devices.get(device_id, {}).get("metadata", {})
        await bridge().put("device", device_id, {"metadata": {"name": new_name,
                                                              "archetype": meta.get("archetype", "classic_bulb")}})
        # The light keeps its own name (what the app and list_home show); the bridge doesn't sync it.
        await bridge().put("light", light_id, {"metadata": {"name": new_name}})
        old = light_name(home, light_id)
    elif kind == "scene":
        scene_id = find_scene(home, name, room)
        await bridge().put("scene", scene_id, {"metadata": {"name": new_name}})
        old = home.scenes[scene_id]["metadata"]["name"]
    else:
        group = group_of(home, name)
        if group.kind != kind:
            raise HueError(f"{group.name} is a {group.kind}, not a {kind}.")
        await bridge().put(kind, group.id, {"metadata": {"name": new_name, "archetype": group.archetype}})
        old = group.name
    return f"Renamed {kind} '{old}' to '{new_name}'"


async def _move_devices(home, devices: list[str], target_id: str, target_devices: list[str]) -> None:
    """Move devices into a room: out of their old rooms first (a device is in exactly
    one room), then in. If adding fails, put them back where they were."""
    olds: dict[str, list[str]] = {}
    for d in devices:
        old_id = home.room_of_device.get(d)
        if old_id and old_id != target_id:
            olds.setdefault(old_id, []).append(d)
    for old_id, moving in olds.items():
        old = home.groups[old_id]
        await bridge().put("room", old_id, {"children": _device_children([d for d in old.devices if d not in moving])})
    try:
        await bridge().put("room", target_id, {"children": _device_children(target_devices + devices)})
    except HueError:
        for old_id in olds:
            await bridge().put("room", old_id, {"children": _device_children(home.groups[old_id].devices)})
        raise


@mcp.tool(annotations=WRITE)
async def move_light(light: Annotated[str, Field(description="Light name")],
                     room: Annotated[str, Field(description="Room to move it to")]) -> str:
    """Move a light to another room (a light is in exactly one room; zones are separate).
    It drops out of the old room's scenes."""
    home = await bridge().home()
    light_id = _one_light(home, light)
    label = light_name(home, light_id)
    device_id = home.lights[light_id]["owner"]["rid"]
    target = group_of(home, room)
    if target.kind != "room":
        raise HueError(f"{target.name} is a zone. Use update_zone to add lights to zones.")
    old_id = home.room_of_device.get(device_id)
    if old_id == target.id:
        return f"{label} is already in {target.name}"
    try:
        await _move_devices(home, [device_id], target.id, target.devices)
    except HueError:
        back = f"; it's back in {home.groups[old_id].name}" if old_id else ""
        raise HueError(f"Couldn't add {label} to {target.name}{back}.")
    origin = f" from {home.groups[old_id].name}" if old_id else ""
    return f"Moved {label}{origin} to {target.name}"


@mcp.tool(annotations=WRITE)
async def create_zone(name: Annotated[str, Field(description="Zone name")],
                      lights: Annotated[list[str], Field(description="Lights in the zone (from any rooms)")],
                      archetype: Annotated[str, Field(description="Icon type; 'other' is fine")] = "other") -> str:
    """Create a zone: a group of lights across rooms, e.g. 'Reading nook'."""
    name = _check(name, "Zone")
    if archetype not in ARCHETYPES:
        raise HueError(f"Unknown archetype '{archetype}'. Try 'other'.")
    home = await bridge().home()
    ids = _lights(home, lights)
    await bridge().post("zone", {"type": "zone", "metadata": {"name": name, "archetype": archetype},
                                 "children": _light_children(ids)})
    return f"Created zone '{name}' with {_named(home, ids)}"


@mcp.tool(annotations=WRITE)
async def update_zone(name: Annotated[str, Field(description="Zone name")],
                      add: Annotated[list[str] | None, Field(description="Lights to add")] = None,
                      remove: Annotated[list[str] | None, Field(description="Lights to remove")] = None) -> str:
    """Add lights to or remove lights from a zone."""
    if not (add or remove):
        raise HueError("Nothing to change. Give add or remove.")
    home = await bridge().home()
    zone = group_of(home, name)
    if zone.kind != "zone":
        raise HueError(f"{zone.name} is a room. Use move_light to change rooms.")
    gone = set(_lights(home, remove or []))
    ids = [l for l in zone.lights if l not in gone]
    ids += [l for l in _lights(home, add or []) if l not in ids]
    await bridge().put("zone", zone.id, {"children": _light_children(ids)})
    return f"{zone.name} now has {_named(home, ids)}"


def _scenes_gone(home, group) -> str:
    names = sorted(sc["metadata"]["name"] for sc in home.scenes.values() if sc["group"]["rid"] == group.id)
    if not names:
        return ""
    return f"its {len(names)} scene{'' if len(names) == 1 else 's'} went with it: {', '.join(names)}"


@mcp.tool(annotations=DESTROY)
async def delete_zone(name: Annotated[str, Field(description="Zone name")]) -> str:
    """Delete a zone (its lights stay in their rooms); the zone's scenes are deleted too.
    Only after Nicholas has confirmed."""
    home = await bridge().home()
    zone = group_of(home, name)
    if zone.kind != "zone":
        raise HueError(f"{zone.name} is a room, not a zone.")
    gone = _scenes_gone(home, zone)
    await bridge().delete("zone", zone.id)
    return f"Deleted zone '{zone.name}'" + (f" ({gone})" if gone else "")


@mcp.tool(annotations=WRITE)
async def create_room(name: Annotated[str, Field(description="Room name")],
                      archetype: Annotated[str, Field(description="Icon type, e.g. 'bedroom', 'office', 'other'")] = "other",
                      lights: Annotated[list[str] | None, Field(description="Lights to move into it")] = None) -> str:
    """Create a room, optionally moving lights into it from their current rooms."""
    name = _check(name, "Room")
    if archetype not in ARCHETYPES:
        raise HueError(f"Unknown archetype '{archetype}'. Try 'other'.")
    home = await bridge().home()
    ids = _lights(home, lights or [])
    devices = []
    for light_id in ids:
        device_id = home.lights[light_id]["owner"]["rid"]
        if device_id not in devices:
            devices.append(device_id)
    # Create it empty first, so a failure can't leave lights without a room.
    room_id = await bridge().post("room", {"type": "room", "metadata": {"name": name, "archetype": archetype},
                                           "children": []})
    if devices:
        try:
            await _move_devices(home, devices, room_id, [])
        except HueError:
            raise HueError(f"Created room '{name}', but couldn't move the lights in; they're where they were.")
    return f"Created room '{name}'" + (f" with {_named(home, ids)}" if ids else "")


@mcp.tool(annotations=DESTROY)
async def delete_room(name: Annotated[str, Field(description="Room name")]) -> str:
    """Delete a room; its lights are left without a room and its scenes are deleted too.
    Only after Nicholas has confirmed."""
    home = await bridge().home()
    room = group_of(home, name)
    if room.kind != "room":
        raise HueError(f"{room.name} is a zone. Use delete_zone.")
    gone = _scenes_gone(home, room)
    await bridge().delete("room", room.id)
    n = len(room.lights)
    return f"Deleted room '{room.name}' ({n} light{'' if n == 1 else 's'} now {'has' if n == 1 else 'have'} no room" + \
        (f"; {gone}" if gone else "") + ")"


@mcp.tool(annotations=READ)
async def status() -> str:
    """Health check: which lights aren't responding, which have firmware updates waiting,
    and the bridge's time zone and today's sunset."""
    home = await bridge().home()

    def where(device_id: str) -> str:
        name = home.devices.get(device_id, {}).get("metadata", {}).get("name", device_id)
        room = home.groups.get(home.room_of_device.get(device_id, ""))
        return f"{name} ({room.name})" if room else name

    lines = []
    down = sorted(where(d) for d, s in home.connectivity.items() if s != "connected")
    lines.append("Not responding: " + (", ".join(down) if down else "none"))
    waiting = sorted(where(d) for d, s in home.updates.items() if s not in (None, "no_update"))
    lines.append("Firmware update waiting: " + (", ".join(waiting) if waiting else "none"))
    sunset = (home.sunset or "")[:5] or "unknown"
    lines.append(f"Bridge time zone: {home.time_zone or 'unknown'}; sunset today {sunset}")
    return "\n".join(lines)
