"""Automation tools: the Hue app's schedules, wake-ups, go-to-sleep fades and timers."""
import copy
import re
from typing import Annotated

from pydantic import Field

from hue import Group, HueError, match, normalize
from hue_common import DESTROY, READ, WRITE, bridge, group_of, mcp
from hue_scenes import find_scene

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
DAY_SETS = {"weekdays": DAYS[:5], "weekends": DAYS[5:], "every day": DAYS, "everyday": DAYS,
            "daily": DAYS, "all": DAYS}
SCRIPTS = {"schedule": "Schedule", "wake_up": "Basic wake up routine",
           "go_to_sleep": "Go to sleep routines", "timer": "Timers"}


def parse_days(days: str | list[str]) -> list[str]:
    if isinstance(days, str):
        if normalize(days) in DAY_SETS:
            return DAY_SETS[normalize(days)]
        days = re.split(r"[,\s]+(?:and\s+)?", days)
    out = []
    for d in days:
        key = normalize(d)
        if not key:
            continue
        full = next((x for x in DAYS if x.startswith(key[:3])), None) if len(key) >= 3 else None
        if full is None:
            raise HueError(f"Unknown day '{d}'. Use day names, 'weekdays', 'weekends' or 'every day'.")
        if full not in out:
            out.append(full)
    return sorted(out, key=DAYS.index)


def parse_at(at: str) -> dict:
    """'7am', '07:30', '10:15 pm', 'sunset', 'sunrise' → a Hue time_point."""
    text = at.strip().lower()
    if text in ("sunset", "sunrise"):
        return {"type": text}
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text)
    if m:
        hour, minute, half = int(m[1]), int(m[2] or 0), m[3]
        if half:
            hour = hour % 12 + (12 if half == "pm" else 0)
        if 0 <= hour <= 23 and 0 <= minute <= 59 and not (half and not 1 <= int(m[1]) <= 12):
            return {"type": "time", "time": {"hour": hour, "minute": minute}}
    raise HueError(f"Times look like '7am', '19:30' or 'sunset', not '{at}'.")


def _where(group: Group) -> list[dict]:
    return [{"group": {"rid": group.id, "rtype": group.kind}}]


def schedule_config(group: Group, recall: dict, time_point: dict, days: list[str], fade_minutes: int | None) -> dict:
    start: dict = {"time_point": time_point}
    if fade_minutes:
        start["transition"] = {"minutes": fade_minutes}
    return {"what": [{"group": {"rid": group.id, "rtype": group.kind}, "recall": recall}],
            "when_extended": {"recurrence_days": days, "start_at": start},
            "where": _where(group)}


def wake_up_config(group: Group, time_point: dict, days: list[str], fade_minutes: int, end_brightness: int) -> dict:
    return {"end_brightness": float(end_brightness), "fade_in_duration": {"seconds": fade_minutes * 60},
            "style": "sunrise", "when": {"recurrence_days": days, "time_point": time_point},
            "where": _where(group)}


def go_to_sleep_config(group: Group, time_point: dict, days: list[str], fade_minutes: int) -> dict:
    return {"end_state": "turn_off", "fade_out_duration": {"seconds": fade_minutes * 60}, "style": "basic",
            "when": {"recurrence_days": days, "time_point": time_point}, "where": _where(group)}


def timer_config(group: Group, minutes: int, then_recall: dict) -> dict:
    """then_recall is a scene or the off recipe; the bridge requires `what` (docs/bridge-formats.md)."""
    return {"duration": {"seconds": minutes * 60},
            "what": [{"group": {"rid": group.id, "rtype": group.kind}, "recall": then_recall}],
            "where": _where(group)}


# ── Reading automations back ──────────────────────────────────────────────


def _days_text(days: list[str]) -> str:
    days = sorted(set(days), key=lambda d: DAYS.index(d) if d in DAYS else 7)  # the bridge often starts on Sunday
    if days == DAYS:
        return "every day"
    if days == DAYS[:5]:
        return "on weekdays"
    if days == DAYS[5:]:
        return "on weekends"
    return "on " + ", ".join(d.capitalize() for d in days)


def _when(cfg: dict) -> tuple[dict | None, list[str]]:
    if "when_extended" in cfg:
        return cfg["when_extended"].get("start_at", {}).get("time_point"), cfg["when_extended"].get("recurrence_days", [])
    if "when" in cfg:
        return cfg["when"].get("time_point"), cfg["when"].get("recurrence_days", [])
    return None, []


def _at_text(tp: dict | None) -> str:
    if not tp:
        return ""
    if tp.get("type") == "time":
        return f"at {tp['time']['hour']:02d}:{tp['time'].get('minute', 0):02d}"
    return f"at {tp.get('type')}"


def _group_name(home, rid: str) -> str:
    g = home.groups.get(rid)
    return g.name if g else "everywhere"


def describe_automation(home, inst: dict) -> str:
    cfg = inst.get("configuration", {})
    parts = []
    for what in cfg.get("what", []):
        recall = what.get("recall", {})
        target = home.scenes.get(recall.get("rid"), {}).get("metadata", {}).get("name") \
            if recall.get("rtype") == "scene" else "off"
        parts.append(f"{_group_name(home, what['group']['rid'])} → {target}")
    if not parts:
        parts = [_group_name(home, w["group"]["rid"]) for w in cfg.get("where", [])]
    tp, days = _when(cfg)
    when = " ".join(x for x in (_at_text(tp), _days_text(days) if days else "") if x)
    return ", ".join(parts) + (f" {when}" if when else "")


def _script_name(home, inst: dict) -> str:
    return home.scripts.get(inst.get("script_id"), {}).get("metadata", {}).get("name", "unknown")


def _find(home, name: str) -> str:
    if name in home.automations:  # an id, from list_automations
        return name
    return match(name, {i: a["metadata"]["name"] for i, a in home.automations.items()}, "automation")


def _script_id(home, key: str) -> str:
    wanted = SCRIPTS[key]
    for sid, s in home.scripts.items():
        if s["metadata"]["name"] == wanted:
            return sid
    raise HueError(f"The Hue Bridge has no '{wanted}' automation type.")


def _off_recall(home) -> dict:
    """The bridge's built-in 'lights off' recipe, as the Hue app uses it."""
    for inst in home.automations.values():
        for what in inst.get("configuration", {}).get("what", []):
            if what.get("recall", {}).get("rtype") == "recipe":
                return {"rid": what["recall"]["rid"], "rtype": "recipe"}
    raise HueError("Can't find the bridge's 'lights off' recipe to schedule 'off'.")


def _recall(home, group: Group, scene: str) -> dict:
    if normalize(scene) == "off":
        return _off_recall(home)
    return {"rid": find_scene(home, scene, group.name), "rtype": "scene"}


def _name(name: str) -> str:
    name = name.strip()
    if not 1 <= len(name) <= 32:
        raise HueError("Automation names are 1–32 characters.")
    return name


async def _create(home, key: str, name: str, cfg: dict) -> None:
    for a in home.automations.values():
        if normalize(a["metadata"]["name"]) == normalize(name):
            raise HueError(f"There's already an automation called '{a['metadata']['name'].strip()}'. "
                           "Use update_automation, or pick another name.")
    await bridge().post("behavior_instance", {"type": "behavior_instance", "script_id": _script_id(home, key),
                                              "enabled": True, "metadata": {"name": _name(name)},
                                              "configuration": cfg})


AutoName = Annotated[str, Field(description="Automation name")]
RoomName = Annotated[str, Field(description="Room or zone")]
At = Annotated[str, Field(description="'7am', '19:30', 'sunset' or 'sunrise'")]
Days = Annotated[str | list[str], Field(description="'weekdays', 'weekends', 'every day', or day names")]


@mcp.tool(annotations=READ)
async def list_automations() -> str:
    """Every Hue automation: what it does, when, and whether it's on."""
    home = await bridge().home()
    lines = [f"{a['metadata']['name'].strip()} [{_script_name(home, a)}, {'on' if a.get('enabled') else 'off'}]: "
             f"{describe_automation(home, a)} [id {i}]"
             for i, a in sorted(home.automations.items(), key=lambda x: x[1]["metadata"]["name"])]
    return "\n".join(lines) or "No automations are set up."


@mcp.tool(annotations=WRITE)
async def set_automation_enabled(name: AutoName, enabled: Annotated[bool, Field(description="true = on")]) -> str:
    """Switch an automation on or off without changing it."""
    home = await bridge().home()
    auto_id = _find(home, name)
    await bridge().put("behavior_instance", auto_id, {"enabled": enabled})
    return f"{home.automations[auto_id]['metadata']['name'].strip()} is now {'on' if enabled else 'off'}"


@mcp.tool(annotations=WRITE)
async def create_schedule(
    name: AutoName, room: RoomName,
    scene: Annotated[str, Field(description="Scene in that room to turn on, or 'off'")],
    at: At, days: Days,
    fade_minutes: Annotated[int | None, Field(ge=1, le=60, description="Fade in over this many minutes")] = None,
) -> str:
    """Create a schedule: at a time (or sunset/sunrise) on chosen days, set a room to a scene or off."""
    home = await bridge().home()
    group = group_of(home, room)
    recall = _recall(home, group, scene)
    cfg = schedule_config(group, recall, parse_at(at), parse_days(days), fade_minutes)
    await _create(home, "schedule", name, cfg)
    target = "off" if recall["rtype"] == "recipe" else home.scenes[recall["rid"]]["metadata"]["name"]
    tp, d = _when(cfg)
    return f"Created '{name.strip()}': {group.name} → {target} {_at_text(tp)} {_days_text(d)}"


@mcp.tool(annotations=WRITE)
async def create_wake_up(
    name: AutoName, room: RoomName, at: At, days: Days,
    fade_minutes: Annotated[int, Field(ge=1, le=60, description="Sunrise fade length")] = 30,
    end_brightness: Annotated[int, Field(ge=1, le=100, description="Brightness at the end")] = 100,
) -> str:
    """Create a wake-up: a sunrise fade in a room at a time on chosen days."""
    home = await bridge().home()
    group = group_of(home, room)
    tp = parse_at(at)
    await _create(home, "wake_up", name, wake_up_config(group, tp, parse_days(days), fade_minutes, end_brightness))
    return f"Created wake-up '{name.strip()}': {group.name} {_at_text(tp)} {_days_text(parse_days(days))}, " \
           f"{fade_minutes} min sunrise"


@mcp.tool(annotations=WRITE)
async def create_go_to_sleep(
    name: AutoName, room: RoomName, at: At, days: Days,
    fade_minutes: Annotated[int, Field(ge=1, le=60, description="Fade-out length")] = 30,
) -> str:
    """Create a go-to-sleep: lights in a room fade out at a time on chosen days."""
    home = await bridge().home()
    group = group_of(home, room)
    tp = parse_at(at)
    await _create(home, "go_to_sleep", name, go_to_sleep_config(group, tp, parse_days(days), fade_minutes))
    return f"Created go-to-sleep '{name.strip()}': {group.name} {_at_text(tp)} {_days_text(parse_days(days))}, " \
           f"{fade_minutes} min fade"


@mcp.tool(annotations=WRITE)
async def create_timer(
    name: AutoName, room: RoomName,
    minutes: Annotated[int, Field(ge=1, le=1440, description="Minutes from now")],
    then: Annotated[str, Field(description="'off', or a scene in that room")] = "off",
) -> str:
    """Create a timer: after some minutes, turn a room off (or to a scene)."""
    home = await bridge().home()
    group = group_of(home, room)
    await _create(home, "timer", name, timer_config(group, minutes, _recall(home, group, then)))
    return f"Created timer '{name.strip()}': {group.name} → {then} in {minutes} min"


@mcp.tool(annotations=WRITE)
async def update_automation(
    name: AutoName,
    new_name: Annotated[str | None, Field(description="Rename it")] = None,
    at: Annotated[str | None, Field(description="New time: '7am', '19:30', 'sunset'")] = None,
    days: Annotated[str | list[str] | None, Field(description="New days")] = None,
    scene: Annotated[str | None, Field(description="New scene (in the same room), or 'off'")] = None,
    fade_minutes: Annotated[int | None, Field(ge=1, le=60, description="New fade length")] = None,
) -> str:
    """Change an automation's name, time, days, scene or fade."""
    if all(v is None for v in (new_name, at, days, scene, fade_minutes)):
        raise HueError("Nothing to change. Give new_name, at, days, scene or fade_minutes.")
    home = await bridge().home()
    auto_id = _find(home, name)
    inst = home.automations[auto_id]
    label = inst["metadata"]["name"].strip()
    script = _script_name(home, inst)
    if script not in SCRIPTS.values():
        raise HueError(f"'{label}' is a {script} automation; change it in the Hue app "
                       "(set_automation_enabled still switches it on or off).")
    cfg = copy.deepcopy(inst.get("configuration", {}))
    if scene and not cfg.get("what"):
        raise HueError(f"'{label}' has no scene to change.")
    when = cfg.get("when_extended") or cfg.get("when")
    if (at or days) and when is None:
        raise HueError(f"'{inst['metadata']['name'].strip()}' doesn't run at a set time.")
    if at:
        tp = parse_at(at)
        if "when_extended" in cfg:
            cfg["when_extended"].setdefault("start_at", {})["time_point"] = tp
        else:
            cfg["when"]["time_point"] = tp
    if days:
        when["recurrence_days"] = parse_days(days)
    if scene:
        for what in cfg.get("what", []):
            group = home.groups.get(what["group"]["rid"])
            if group is None:
                raise HueError("That automation isn't tied to one room, so its scene can't change.")
            what["recall"] = _recall(home, group, scene)
    if fade_minutes:
        if "when_extended" in cfg:
            cfg["when_extended"].setdefault("start_at", {})["transition"] = {"minutes": fade_minutes}
        elif "fade_in_duration" in cfg:
            cfg["fade_in_duration"] = {"seconds": fade_minutes * 60}
        elif "fade_out_duration" in cfg:
            cfg["fade_out_duration"] = {"seconds": fade_minutes * 60}
        else:
            raise HueError("That automation has no fade to change.")
    body: dict = {"configuration": cfg}
    if new_name:
        body["metadata"] = {"name": _name(new_name)}
    await bridge().put("behavior_instance", auto_id, body)
    shown = {**inst, "configuration": cfg}
    return f"Updated '{(new_name or inst['metadata']['name']).strip()}': {describe_automation(home, shown)}"


@mcp.tool(annotations=DESTROY)
async def delete_automation(name: AutoName) -> str:
    """Delete an automation for good. Only after Nicholas has confirmed; to pause it, use set_automation_enabled."""
    home = await bridge().home()
    auto_id = _find(home, name)
    await bridge().delete("behavior_instance", auto_id)
    return f"Deleted automation '{home.automations[auto_id]['metadata']['name'].strip()}'"
