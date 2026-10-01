"""Scene tools: turn on, create, change and delete Hue scenes."""
from typing import Annotated

from pydantic import BaseModel, Field

from hue import HueError, clamp_mirek, color_to_xy, match, match_all, normalize, to_mirek
from hue_common import DESTROY, EFFECTS, WRITE, Effect, bridge, group_of, light_name, mcp
from typing import Literal

SCENE_EFFECT_KEY = "effects_v2"  # confirmed on the real bridge (docs/bridge-formats.md)


class LightSetting(BaseModel):
    light: str = Field(description="Light name in that room")
    on: bool = True
    brightness: int | None = Field(None, ge=1, le=100)
    color: str | None = None
    color_temp: str | None = Field(None, description="'warm', 'neutral', 'cool' or kelvin")
    effect: Literal[EFFECTS] | None = None


def _check_name(name: str) -> str:
    name = name.strip()
    if not 1 <= len(name) <= 32:
        raise HueError("Scene names are 1–32 characters.")
    return name


def snapshot_action(light: dict) -> dict:
    """A scene action that reproduces the light as it is now."""
    if not light.get("on", {}).get("on"):
        return {"on": {"on": False}}
    action: dict = {"on": {"on": True}}
    if "dimming" in light:
        action["dimming"] = {"brightness": light["dimming"]["brightness"]}
    ct = light.get("color_temperature") or {}
    if ct.get("mirek_valid") and ct.get("mirek"):
        action["color_temperature"] = {"mirek": ct["mirek"]}
    elif "color" in light:
        action["color"] = {"xy": light["color"]["xy"]}
    return action


def scene_action(light: dict, s: LightSetting | None) -> dict:
    """A scene action from a described setting; lights not described are off."""
    name = light.get("metadata", {}).get("name", "That light")
    if s is None or not s.on:
        return {"on": {"on": False}}
    if s.color and s.color_temp:
        raise HueError(f"{name}: give a colour or a colour temperature, not both.")
    action: dict = {"on": {"on": True}}
    if s.brightness is not None:
        action["dimming"] = {"brightness": float(s.brightness)}
    if s.color:
        if "color" not in light:
            raise HueError(f"{name} can't do colour.")
        action["color"] = {"xy": color_to_xy(s.color)}
    if s.color_temp:
        if "color_temperature" not in light:
            raise HueError(f"{name} can't change white temperature.")
        action["color_temperature"] = {"mirek": clamp_mirek(to_mirek(s.color_temp), light)}
    if s.effect and s.effect != "none":
        action[SCENE_EFFECT_KEY] = {"action": {"effect": s.effect}} if SCENE_EFFECT_KEY == "effects_v2" \
            else {"effect": s.effect}
    return action


def build_actions(home, group, lights: list[LightSetting] | None) -> list[dict]:
    settings: dict[str, LightSetting] = {}
    if lights is not None:
        in_room = {i: light_name(home, i) for i in group.lights if i in home.lights}
        for s in lights:
            for light_id in match_all(s.light, in_room, f"light in {group.name}"):
                settings[light_id] = s
    actions = []
    for light_id in group.lights:
        light = home.lights[light_id]
        action = snapshot_action(light) if lights is None else scene_action(light, settings.get(light_id))
        actions.append({"target": {"rid": light_id, "rtype": "light"}, "action": action})
    return actions


def _where(home, scene: dict) -> str:
    g = home.groups.get(scene["group"]["rid"])
    return g.name if g else "unknown room"


def find_scene(home, name: str, room: str | None) -> str:
    scenes = home.scenes
    if room:
        group_id = group_of(home, room).id
        scenes = {i: s for i, s in scenes.items() if s["group"]["rid"] == group_id}
    return match(name, {i: s["metadata"]["name"] for i, s in scenes.items()}, "scene",
                 labels={i: f"{s['metadata']['name']} ({_where(home, s)})" for i, s in scenes.items()})


SceneName = Annotated[str, Field(description="Scene name, e.g. 'Relax'")]
Room = Annotated[str | None, Field(description="Room or zone the scene belongs to; needed when rooms share a scene name")]


@mcp.tool(annotations=WRITE)
async def activate_scene(
    name: SceneName, room: Room = None,
    dynamic: Annotated[bool, Field(description="Play the scene's colours slowly shifting")] = False,
    speed: Annotated[float | None, Field(ge=0, le=1, description="Dynamic speed, 0 slow – 1 fast")] = None,
    brightness: Annotated[int | None, Field(ge=1, le=100, description="Override the scene's brightness")] = None,
    fade_seconds: Annotated[float | None, Field(gt=0, le=3600, description="Fade into the scene over this long")] = None,
) -> str:
    """Turn on a Hue scene, optionally dynamic, dimmer/brighter, or fading in."""
    home = await bridge().home()
    scene_id = find_scene(home, name, room)
    scene = home.scenes[scene_id]
    if speed is not None:
        await bridge().put("scene", scene_id, {"speed": speed})
    recall: dict = {"action": "dynamic_palette" if dynamic else "active"}
    if brightness is not None:
        recall["dimming"] = {"brightness": float(brightness)}
    if fade_seconds:
        recall["duration"] = int(fade_seconds * 1000)
    await bridge().put("scene", scene_id, {"recall": recall})
    extras = [x for x in ("dynamic" if dynamic else None,
                          f"{brightness}%" if brightness is not None else None,
                          f"over {fade_seconds:g} s" if fade_seconds else None) if x]
    return f"{scene['metadata']['name']} is on in {_where(home, scene)}" + (f" ({', '.join(extras)})" if extras else "")


@mcp.tool(annotations=WRITE)
async def create_scene(
    name: SceneName,
    room: Annotated[str, Field(description="Room or zone the scene is for")],
    lights: Annotated[list[LightSetting] | None, Field(description="How each light should look. Omit to save the "
                                                                   "room exactly as it is now. Lights not listed are off.")] = None,
) -> str:
    """Create a new scene for a room: from how the lights are right now, or described light by light."""
    name = _check_name(name)
    home = await bridge().home()
    group = group_of(home, room)
    for s in home.scenes.values():
        if s["group"]["rid"] == group.id and normalize(s["metadata"]["name"]) == normalize(name):
            raise HueError(f"{group.name} already has a scene called '{s['metadata']['name']}'. Use update_scene.")
    actions = build_actions(home, group, lights)
    await bridge().post("scene", {"type": "scene", "metadata": {"name": name},
                                  "group": {"rid": group.id, "rtype": group.kind}, "actions": actions})
    how = "saved as they are now" if lights is None else "as described"
    return f"Created scene '{name}' in {group.name} ({len(actions)} lights, {how})"


@mcp.tool(annotations=WRITE)
async def update_scene(
    name: SceneName, room: Room = None,
    new_name: Annotated[str | None, Field(description="Rename the scene")] = None,
    lights: Annotated[list[LightSetting] | None, Field(description="Replace its light settings; lights not listed are off")] = None,
    save_current: Annotated[bool, Field(description="Replace its light settings with how the room looks now")] = False,
) -> str:
    """Rename a scene or change how it looks."""
    if lights is not None and save_current:
        raise HueError("Give lights or save_current, not both.")
    home = await bridge().home()
    scene_id = find_scene(home, name, room)
    scene = home.scenes[scene_id]
    group = home.groups.get(scene["group"]["rid"])
    body: dict = {}
    done = []
    if new_name:
        body["metadata"] = {"name": _check_name(new_name)}
        done.append(f"renamed to '{body['metadata']['name']}'")
    if lights is not None or save_current:
        if group is None:
            raise HueError("That scene's room is gone, so its lights can't be changed.")
        body["actions"] = build_actions(home, group, None if save_current else lights)
        done.append("lights replaced")
    if not body:
        raise HueError("Nothing to change. Give new_name, lights or save_current.")
    await bridge().put("scene", scene_id, body)
    return f"Updated scene '{scene['metadata']['name']}' in {_where(home, scene)} ({', '.join(done)})"


@mcp.tool(annotations=DESTROY)
async def delete_scene(name: SceneName, room: Room = None) -> str:
    """Delete a scene for good. Only after Nicholas has confirmed."""
    home = await bridge().home()
    scene_id = find_scene(home, name, room)
    scene = home.scenes[scene_id]
    await bridge().delete("scene", scene_id)
    return f"Deleted scene '{scene['metadata']['name']}' from {_where(home, scene)}"
