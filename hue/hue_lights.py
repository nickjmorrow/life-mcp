"""Light tools: see and change rooms, zones and lights."""
from typing import Annotated, Literal

from pydantic import Field

from hue import HueError, clamp_mirek, color_to_xy, describe, light_body, match_all
from hue_common import (READ, WRITE, Brightness, BrightnessChange, Color, ColorTemp, Effect, FadeSeconds, On,
                        bridge, capable, group_of, light_name, light_names, mcp, put_lights, state, targets)

DIM_NOTE_BELOW = 20  # "turn on the living room" at 3% should say so


def _clamped(home, body: dict):
    """body, with its colour temperature clamped to each light's range."""
    def for_light(light_id: str) -> dict:
        if "color_temperature" not in body:
            return body
        mirek = clamp_mirek(body["color_temperature"]["mirek"], home.lights[light_id])
        return {**body, "color_temperature": {"mirek": mirek}}
    return for_light


def _effect_body(effect: str) -> dict:
    value = "no_effect" if effect == "none" else effect
    body: dict = {"effects_v2": {"action": {"effect": value}}}
    if value != "no_effect":
        body["on"] = {"on": True}
    return body


def _skipped(total: int, done: int) -> str:
    n = total - done
    return f" ({n} light{'' if n == 1 else 's'} can't)" if n else ""


@mcp.tool(annotations=READ)
async def list_home() -> str:
    """Every room and zone, its lights and scenes, and what's on right now."""
    home = await bridge().home()
    lines = []
    for g in sorted(home.groups.values(), key=lambda g: (g.kind, g.name)):
        names = [f"{light_name(home, l)} ({state(home.lights[l])})"
                 for l in g.lights if l in home.lights]
        lights = ", ".join(names) if g.kind == "room" else f"{len(names)} lights"
        scenes = ", ".join(sorted(s["metadata"]["name"] for s in home.scenes.values()
                                  if s["group"]["rid"] == g.id))
        lines.append(f"{g.name} [{g.kind}, {state(home.grouped.get(g.grouped_light, {}))}] "
                     f"lights: {lights or 'none'}; scenes: {scenes or 'none'}")
    return "\n".join(lines) or "No rooms are set up on the Hue Bridge."


@mcp.tool(annotations=WRITE)
async def set_room(
    name: Annotated[str, Field(description="Room or zone, e.g. 'living room'")],
    on: On = None, brightness: Brightness = None, brightness_change: BrightnessChange = None,
    color_temp: ColorTemp = None, color: Color = None, effect: Effect = None,
    fade_seconds: FadeSeconds = None,
) -> str:
    """Change a whole room or zone: on/off, brightness (absolute or relative), white
    temperature, colour, or an effect, optionally fading over time. Anything not given
    stays as it is."""
    home = await bridge().home()
    group = group_of(home, name)
    note = ""
    if effect:
        ids = capable(home, group.lights, "effect", "no_effect" if effect == "none" else effect)
        if not ids:
            raise HueError(f"None of the lights in {group.name} can do {effect}.")
        failed = await put_lights(home, ids, _effect_body(effect))
        if all(v is None for v in (on, brightness, brightness_change, color_temp, color)):
            return f"{group.name}: {effect} effect" + _skipped(len(group.lights), len(ids)) + failed
        note = failed
    body = light_body(on, brightness, color_temp, color, brightness_change, fade_seconds)
    feature = next((f for f in ("color", "color_temperature") if f in body), None)
    if not feature:
        if not group.grouped_light:
            raise HueError(f"{group.name} has no lights.")
        await bridge().put("grouped_light", group.grouped_light, body)
        return describe(group.name, body) + _dim_note(home, group, body) + note
    ids = capable(home, group.lights, feature)
    if not ids:
        raise HueError(f"None of the lights in {group.name} can do that.")
    # Brightness goes to the whole room first, so white-only bulbs follow it too.
    group_part = {k: body[k] for k in ("dimming", "dimming_delta", "on", "dynamics") if k in body}
    brightness_asked = "dimming" in body or "dimming_delta" in body
    per_light = body
    if brightness_asked and group.grouped_light:
        await bridge().put("grouped_light", group.grouped_light, group_part)
        # Brightness is done room-wide; sending a relative change again would apply it twice.
        per_light = {k: v for k, v in body.items() if k not in ("dimming", "dimming_delta")}
    failed = await put_lights(home, ids, _clamped(home, per_light))
    skipped = len(group.lights) - len(ids)
    if skipped:
        what = "colour" if feature == "color" else "white temperature"
        rest = "brightness set" if brightness_asked else "left as is"
        note = f" ({skipped} light{'' if skipped == 1 else 's'} can't do {what}, {rest})" + note
    return describe(group.name, body) + note + failed


def _dim_note(home, group, body: dict) -> str:
    if body != {"on": {"on": True}}:
        return ""
    level = home.grouped.get(group.grouped_light, {}).get("dimming", {}).get("brightness")
    if level is not None and level < DIM_NOTE_BELOW:
        return f" (it came back at {level:.0f}%; say a brightness to change it)"
    return ""


@mcp.tool(annotations=WRITE)
async def set_light(
    name: Annotated[str, Field(description="One light, e.g. 'floor lamp'")],
    on: On = None, brightness: Brightness = None, brightness_change: BrightnessChange = None,
    color_temp: ColorTemp = None, color: Color = None, effect: Effect = None,
    gradient: Annotated[list[str] | None, Field(description="Gradient lightstrips only: 2–5 colours along the strip")] = None,
    fade_seconds: FadeSeconds = None,
) -> str:
    """Change a single light (every bulb sharing that name): on/off, brightness, white
    temperature, colour, effect or gradient. Anything not given stays as it is."""
    home = await bridge().home()
    ids = match_all(name, light_names(home), "light")
    label = light_name(home, ids[0])
    parts, note = [], ""
    if gradient:
        ids_g = capable(home, ids, "gradient")
        if not ids_g:
            raise HueError(f"{label} can't do gradients.")
        most = min(home.lights[i]["gradient"].get("points_capable", 5) for i in ids_g)
        if not 2 <= len(gradient) <= most:
            raise HueError(f"{label} takes 2–{most} gradient colours.")
        points = [{"color": {"xy": color_to_xy(c)}} for c in gradient]
        note += await put_lights(home, ids_g, {"gradient": {"points": points}, "on": {"on": True}})
        parts.append(f"gradient of {len(gradient)} colours")
    if effect:
        ids_e = capable(home, ids, "effect", "no_effect" if effect == "none" else effect)
        if not ids_e:
            raise HueError(f"{label} can't do {effect}.")
        note += await put_lights(home, ids_e, _effect_body(effect))
        parts.append(f"{effect} effect")
    if all(v is None for v in (on, brightness, brightness_change, color_temp, color)):
        if not parts:
            raise HueError("Nothing to change.")
        return f"{label}: " + ", ".join(parts) + note
    body = light_body(on, brightness, color_temp, color, brightness_change, fade_seconds)
    for feature, words in (("color", "colour"), ("color_temperature", "white temperature")):
        if feature in body:
            ids = capable(home, ids, feature)
            if not ids:
                raise HueError(f"{label} can't change {words}.")
    if len(ids) > 1:
        label += f" ({len(ids)} lights)"
    return describe(label, body) + (", " + ", ".join(parts) if parts else "") + note + \
        await put_lights(home, ids, _clamped(home, body))


@mcp.tool(annotations=WRITE)
async def all_off() -> str:
    """Turn off every light in the home."""
    home = await bridge().home()
    if not home.everything:
        raise HueError("The Hue Bridge has no all-lights group.")
    await bridge().put("grouped_light", home.everything, {"on": {"on": False}})
    return "Everything is off"


@mcp.tool(annotations=WRITE)
async def identify_light(name: Annotated[str, Field(description="Light name")]) -> str:
    """Make a light 'breathe' (pulse) so Nicholas can see which bulb it is."""
    home = await bridge().home()
    label, ids = targets(home, name)
    return f"{label} is breathing so you can spot it" + await put_lights(home, ids, {"alert": {"action": "breathe"}})


@mcp.tool(annotations=WRITE)
async def timed_effect(
    name: Annotated[str, Field(description="Room, zone or light")],
    effect: Annotated[Literal["sunrise", "sunset", "none"], Field(description="sunrise starts from off and "
                                                                            "slowly brightens; sunset slowly dims to off")],
    minutes: Annotated[int, Field(ge=1, le=360, description="How long it takes")] = 30,
) -> str:
    """A slow sunrise or sunset on a room or light, e.g. a gentle wake-up right now."""
    home = await bridge().home()
    label, ids = targets(home, name)
    value = "no_effect" if effect == "none" else effect
    ok = capable(home, ids, "timed", value)
    if not ok:
        raise HueError(f"None of the lights in {label} can do a {effect}.")
    body = {"timed_effects": {"effect": value, "duration": minutes * 60_000}}
    return f"{label}: {effect} over {minutes} min" + _skipped(len(ids), len(ok)) + await put_lights(home, ok, body)


@mcp.tool(annotations=WRITE)
async def signal(
    name: Annotated[str, Field(description="Room, zone or light")],
    kind: Annotated[Literal["on_off", "on_off_color", "alternating"],
                    Field(description="on_off blinks; on_off_color blinks one colour; alternating swaps two colours")],
    seconds: Annotated[int, Field(ge=1, le=3600, description="How long to keep signalling")] = 10,
    colors: Annotated[list[str] | None, Field(description="1 colour for on_off_color, 2 for alternating")] = None,
) -> str:
    """Flash a room or light to get attention, e.g. 'flash the office red'."""
    need = {"on_off": 0, "on_off_color": 1, "alternating": 2}[kind]
    if len(colors or []) != need:
        raise HueError(f"{kind} needs {need} colour{'' if need == 1 else 's'}.")
    home = await bridge().home()
    label, ids = targets(home, name)
    ok = capable(home, ids, "signal", kind)
    if not ok:
        raise HueError(f"None of the lights in {label} can signal {kind}.")
    body: dict = {"signaling": {"signal": kind, "duration": seconds * 1000}}
    if colors:
        body["signaling"]["colors"] = [{"xy": color_to_xy(c)} for c in colors]
    return f"{label}: {kind} signal for {seconds} s" + _skipped(len(ids), len(ok)) + await put_lights(home, ok, body)


@mcp.tool(annotations=WRITE)
async def set_power_on(
    name: Annotated[str, Field(description="Room, zone or light")],
    preset: Annotated[Literal["safety", "powerfail", "last_on_state"],
                      Field(description="safety: bright warm white; powerfail: stay off if it was off; "
                                        "last_on_state: come back as it was")],
) -> str:
    """What lights do when their power comes back (after a power cut or a wall switch)."""
    home = await bridge().home()
    label, ids = targets(home, name)
    ok = capable(home, ids, "powerup")
    if not ok:
        raise HueError(f"None of the lights in {label} support power-on settings.")
    failed = await put_lights(home, ok, {"powerup": {"preset": preset}})
    return f"{label}: after a power cut, {preset} ({len(ok)} light{'' if len(ok) == 1 else 's'})" + failed
