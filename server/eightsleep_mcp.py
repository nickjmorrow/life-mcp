"""Eight Sleep tools the npm server doesn't have: alarms, sleep levels, bedtime, naps,
sounds, and the Pod's status and priming.

server.py mounts this (no namespace; names start with eight_sleep_). Levels use the
app's scale, -10 (cool) … +10 (warm); the API's is -100…100.
"""
import copy
import datetime as dt
import re
from typing import Annotated, Literal

from pydantic import Field

from eightsleep_api import EightSleep, EightSleepError
from fastmcp import FastMCP
import host

mcp = FastMCP("Eight Sleep extras")
READ = {"readOnlyHint": True}
WRITE = {"readOnlyHint": False, "destructiveHint": False}
DESTROY = {"readOnlyHint": False, "destructiveHint": True}

_api: EightSleep | None = None


def api() -> EightSleep:
    global _api
    if _api is None:
        _api = EightSleep()
    return _api


# ── Helpers ──────────────────────────────────────────────────────────────

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
DAY_SETS = {"weekdays": DAYS[:5], "weekends": DAYS[5:], "every day": DAYS, "everyday": DAYS, "daily": DAYS}
COMPUTED = ("nextTimestamp", "startTimestamp", "endTimestamp", "dismissedUntil", "snoozedUntil", "skippedUntil")


def to_api_level(level: float) -> int:
    if not -10 <= level <= 10:
        raise EightSleepError("Levels go between -10 and +10 (cool to warm), like the app.")
    return int(round(level * 10))


def app_level(value: int) -> str:
    return f"{value / 10:+g}"


def parse_time(text: str) -> str:
    """'7:20', '7:20am', '6:45 pm', '07:20:00', '7am' → 'HH:MM:SS'."""
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(?::(\d{2}))?\s*(am|pm)?", text.strip().lower())
    if m:
        hour, minute, half = int(m[1]), int(m[2] or 0), m[4]
        if half:
            if not 1 <= hour <= 12:
                m = None
            else:
                hour = hour % 12 + (12 if half == "pm" else 0)
        if m and 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}:{int(m[3] or 0):02d}"
    raise EightSleepError(f"Times look like '7:20', '6:45pm' or '07:20', not '{text}'.")


def parse_days(days: str | list[str]) -> list[str]:
    if isinstance(days, str):
        key = days.strip().lower()
        if key in DAY_SETS:
            return DAY_SETS[key]
        days = re.split(r"[,\s]+(?:and\s+)?", key)
    out = []
    for d in days:
        d = d.strip().lower()
        if not d:
            continue
        full = next((x for x in DAYS if len(d) >= 3 and x.startswith(d[:3])), None)
        if full is None:
            raise EightSleepError(f"Unknown day '{d}'. Use day names, 'weekdays', 'weekends' or 'every day'.")
        if full not in out:
            out.append(full)
    return sorted(out, key=DAYS.index)


def hhmmss(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}:00"


def _days_text(week_days: dict) -> str:
    on = [d for d in DAYS if week_days.get(d)]
    if on == DAYS:
        return "every day"
    if on == DAYS[:5]:
        return "on weekdays"
    if on == DAYS[5:]:
        return "on weekends"
    return "on " + ", ".join(d.capitalize() for d in on)


def _is_skipped(a: dict) -> bool:
    """A skip comes back as skippedUntil (a future time) with skipNext reset to false."""
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    until = a.get("skippedUntil") or ""
    return bool(a.get("skipNext")) or (until[:4].isdigit() and until > now)


def describe_alarm(a: dict) -> str:
    repeat = a.get("repeat") or {}
    when = _days_text(repeat.get("weekDays", {})) if repeat.get("enabled") else \
        "once (nap)" if "oneOff-napMode" in a.get("tags", []) else "once"
    parts = []
    vib = a.get("vibration") or {}
    if vib.get("enabled"):
        parts.append(f"vibration {str(vib.get('pattern', '')).lower()} {vib.get('powerLevel')}")
    if (a.get("thermal") or {}).get("enabled"):
        parts.append(f"heat {a['thermal'].get('level')}")
    if (a.get("audio") or {}).get("enabled"):
        parts.append(f"sound {a['audio'].get('level')}")
    if (a.get("smart") or {}).get("lightSleepEnabled"):
        parts.append("smart wake")
    if _is_skipped(a):
        parts.append("next one skipped")
    state = "on" if a.get("enabled") else "off"
    return f"{a['time'][:5]} {when}, {state}" + (f" — {', '.join(parts)}" if parts else "") + f" [id {a['id']}]"


def find_alarm(alarms: list[dict], key: str) -> dict:
    for a in alarms:
        if a["id"] == key:
            return a
    try:
        wanted = parse_time(key)[:5]
    except EightSleepError:
        raise EightSleepError(f"Say which alarm by its time or id. Alarms: {', '.join(a['time'][:5] for a in alarms)}.")
    found = [a for a in alarms if a["time"][:5] == wanted]
    if len(found) == 1:
        return found[0]
    if found:
        raise EightSleepError(f"Several alarms at {wanted}; use the id: {', '.join(a['id'] for a in found)}.")
    raise EightSleepError(f"No alarm at {wanted}. Alarms: {', '.join(a['time'][:5] for a in alarms) or 'none'}.")


def alarm_update_body(alarm: dict) -> dict:
    return {k: v for k, v in copy.deepcopy(alarm).items() if k not in COMPUTED}


async def _alarms() -> list[dict]:
    return (await api().request("GET", "app", "/v2/users/{uid}/alarms")).get("alarms", [])


Pattern = Literal["RISE", "INTENSE"]
AlarmKey = Annotated[str, Field(description="The alarm's time ('7:20') or id")]
Strength = Annotated[int | None, Field(ge=1, le=100, description="Vibration strength 1–100")]
HeatLevel = Annotated[int | None, Field(ge=0, le=100, description="Wake-up warmth 0–100")]
SoundLevel = Annotated[int | None, Field(ge=0, le=100, description="Alarm sound volume 0–100")]


def _apply_alarm_changes(a: dict, time=None, days=None, vibration=None, vibration_strength=None,
                         vibration_pattern=None, heat=None, heat_level=None, sound=None, sound_level=None,
                         smart_wake=None, sleep_cap_minutes=None) -> dict:
    if time:
        a["time"] = parse_time(time)
    if days is not None:
        chosen = parse_days(days)
        a["repeat"] = {"enabled": True, "weekDays": {d: d in chosen for d in DAYS}}
    vib, thermal = a.setdefault("vibration", {}), a.setdefault("thermal", {})
    audio, smart = a.setdefault("audio", {"enabled": False, "level": 30}), a.setdefault("smart", {})
    if vibration is not None:
        vib["enabled"] = vibration
    if vibration_strength is not None:
        vib.update(enabled=True, powerLevel=vibration_strength)
    if vibration_pattern:
        vib["pattern"] = vibration_pattern
    if heat is not None:
        thermal["enabled"] = heat
    if heat_level is not None:
        thermal.update(enabled=True, level=heat_level)
    if sound is not None:
        audio["enabled"] = sound
    if sound_level is not None:
        audio.update(enabled=True, level=sound_level)
    if smart_wake is not None:
        smart["lightSleepEnabled"] = smart_wake
    if sleep_cap_minutes is not None:
        smart.update(sleepCapEnabled=sleep_cap_minutes > 0, sleepCapMinutes=sleep_cap_minutes or 480)
    return a


async def _put_alarm(a: dict, keep_skip: bool = True) -> dict:
    body = alarm_update_body(a)
    if keep_skip and _is_skipped(a):
        body["skipNext"] = True  # any update with skipNext false clears a pending skip (seen live)
    await api().request("PUT", "app", f"/v1/users/{{uid}}/alarms/{a['id']}", body)
    return a


# ── Alarms ───────────────────────────────────────────────────────────────


@mcp.tool(annotations=READ)
async def eight_sleep_list_alarms() -> str:
    """Every Eight Sleep alarm: time, days, on/off, vibration/heat/sound, smart wake, and id."""
    alarms = await _alarms()
    return "\n".join(describe_alarm(a) for a in alarms) or "No alarms set."


@mcp.tool(annotations=WRITE)
async def eight_sleep_create_alarm(
    time: Annotated[str, Field(description="When, e.g. '6:45' or '6:45am'")],
    days: Annotated[str | list[str] | None, Field(description="'weekdays', 'every day', day names; omit for one time")] = None,
    vibration: bool | None = None, vibration_strength: Strength = None, vibration_pattern: Pattern | None = None,
    heat: bool | None = None, heat_level: HeatLevel = None, sound: bool | None = None, sound_level: SoundLevel = None,
    smart_wake: Annotated[bool | None, Field(description="Wake during light sleep near the time")] = None,
    sleep_cap_minutes: Annotated[int | None, Field(ge=0, le=720, description="Wake after this much sleep; 0 = off")] = None,
) -> str:
    """Create an alarm. Anything not given copies his daily alarm's settings."""
    api().require_mutations()
    alarms = await _alarms()
    template = next((a for a in alarms if (a.get("repeat") or {}).get("enabled")), None) or {
        "vibration": {"enabled": True, "powerLevel": 50, "pattern": "RISE"}, "thermal": {"enabled": True, "level": 0},
        "audio": {"enabled": False, "level": 30},
        "smart": {"lightSleepEnabled": False, "sleepCapEnabled": False, "sleepCapMinutes": 480}}
    alarm = {"enabled": True, "repeat": {"enabled": False, "weekDays": {}}, "skipNext": False, "tags": [],
             **{k: copy.deepcopy(template[k]) for k in ("vibration", "thermal", "audio", "smart") if k in template}}
    alarm = _apply_alarm_changes(alarm, time, days, vibration, vibration_strength, vibration_pattern, heat,
                                 heat_level, sound, sound_level, smart_wake, sleep_cap_minutes)
    await api().request("POST", "app", "/v1/users/{uid}/alarms", alarm)
    return "Created alarm " + describe_alarm({**alarm, "id": "new"}).removesuffix(" [id new]")


@mcp.tool(annotations=WRITE)
async def eight_sleep_update_alarm(
    alarm: AlarmKey,
    time: Annotated[str | None, Field(description="New time")] = None,
    days: Annotated[str | list[str] | None, Field(description="New days")] = None,
    vibration: bool | None = None, vibration_strength: Strength = None, vibration_pattern: Pattern | None = None,
    heat: bool | None = None, heat_level: HeatLevel = None, sound: bool | None = None, sound_level: SoundLevel = None,
    smart_wake: bool | None = None,
    sleep_cap_minutes: Annotated[int | None, Field(ge=0, le=720)] = None,
) -> str:
    """Change an alarm's time, days, vibration, heat, sound or smart wake."""
    api().require_mutations()
    a = copy.deepcopy(find_alarm(await _alarms(), alarm))
    _apply_alarm_changes(a, time, days, vibration, vibration_strength, vibration_pattern, heat, heat_level,
                         sound, sound_level, smart_wake, sleep_cap_minutes)
    return "Updated alarm: " + describe_alarm(await _put_alarm(a))


@mcp.tool(annotations=WRITE)
async def eight_sleep_set_alarm_enabled(alarm: AlarmKey, enabled: bool) -> str:
    """Switch an alarm on or off without changing it."""
    api().require_mutations()
    a = copy.deepcopy(find_alarm(await _alarms(), alarm))
    a["enabled"] = enabled
    await _put_alarm(a)
    return f"The {a['time'][:5]} alarm is now {'on' if enabled else 'off'}"


@mcp.tool(annotations=WRITE)
async def eight_sleep_skip_next_alarm(alarm: AlarmKey,
                                      skip: Annotated[bool, Field(description="false to undo")] = True) -> str:
    """Skip the next time an alarm rings (e.g. 'skip tomorrow's alarm'), or undo that."""
    api().require_mutations()
    a = copy.deepcopy(find_alarm(await _alarms(), alarm))
    a["skipNext"] = skip
    await _put_alarm(a, keep_skip=False)
    return f"The next {a['time'][:5]} alarm will {'be skipped' if skip else 'ring'}"


@mcp.tool(annotations=DESTROY)
async def eight_sleep_delete_alarm(alarm: AlarmKey) -> str:
    """Delete an alarm for good. Only after Nicholas has confirmed; to pause it, use set_alarm_enabled."""
    api().require_mutations()
    a = find_alarm(await _alarms(), alarm)
    await api().request("DELETE", "app", f"/v1/users/{{uid}}/alarms/{a['id']}")
    return f"Deleted the {a['time'][:5]} alarm"


# ── Sleep levels and bedtime ─────────────────────────────────────────────

STAGES = (("bedTimeLevel", "bedtime"), ("initialSleepLevel", "early night"), ("finalSleepLevel", "late night"))
STOP_TEXT = {"UntilFallAsleep": "until you fall asleep", "ManualStop": "until stopped"}


def _levels_text(smart: dict) -> str:
    return ", ".join(f"{label} {app_level(smart[key])}" for key, label in STAGES if key in smart)


async def _temperature() -> dict:
    return await api().request("GET", "app", "/v1/users/{uid}/temperature")


@mcp.tool(annotations=READ)
async def eight_sleep_get_bedtime() -> str:
    """The bedtime schedule (time, days), the three sleep levels, the bedtime sound, and what the bed is doing now."""
    t = await _temperature()
    lines = []
    s = t.get("currentSchedule")
    if s:
        days = _days_text({d: True for d in s.get("days", [])})
        lines.append(f"Bedtime {s['time'][:5]} {days} ({'on' if s.get('enabled') else 'off'})")
        audio = (s.get("startSettings") or {}).get("audioSettings")
        if audio:
            lines.append(f"Sound: {audio.get('trackId')} at {audio.get('level')} "
                         f"{STOP_TEXT.get(audio.get('stopCriteria'), '')}".rstrip())
    if t.get("smart"):
        lines.append("Levels: " + _levels_text(t["smart"]))
    if t.get("currentState"):
        lines.append(f"Right now: {t['currentState'].get('type')}")
    return "\n".join(lines) or "No bedtime schedule set."


Level = Annotated[float | None, Field(ge=-10, le=10, description="-10 coolest … +10 warmest")]


@mcp.tool(annotations=WRITE)
async def eight_sleep_set_sleep_levels(bedtime: Level = None, early_night: Level = None, late_night: Level = None) -> str:
    """Set the smart temperature for each stage of the night (app scale -10…+10)."""
    api().require_mutations()
    if bedtime is None and early_night is None and late_night is None:
        raise EightSleepError("Nothing to change. Give bedtime, early_night or late_night.")
    smart = dict((await _temperature()).get("smart") or {})
    for (key, _), value in zip(STAGES, (bedtime, early_night, late_night)):
        if value is not None:
            smart[key] = to_api_level(value)
    await api().request("PUT", "app", "/v1/users/{uid}/temperature", {"smart": smart})
    return "Levels now: " + _levels_text(smart)


@mcp.tool(annotations=WRITE)
async def eight_sleep_shift_levels(change: Annotated[float, Field(ge=-10, le=10, description="e.g. -1 'a bit cooler', +2 'warmer'")]) -> str:
    """Make every stage of the night cooler or warmer by the same amount. This changes the saved
    levels for every night, not just tonight; say so when he asks about 'tonight'."""
    api().require_mutations()
    smart = dict((await _temperature()).get("smart") or {})
    if not smart:
        raise EightSleepError("There are no smart sleep levels to shift (the bed isn't on a smart schedule).")
    step, clamped = to_api_level(change), []
    for key, label in STAGES:
        if key in smart:
            value = smart[key] + step
            if not -100 <= value <= 100:
                clamped.append(label)
            smart[key] = max(-100, min(100, value))
    await api().request("PUT", "app", "/v1/users/{uid}/temperature", {"smart": smart})
    notes = []
    if clamped:
        edge = "coolest" if step < 0 else "warmest"
        notes.append(f"{' and '.join(clamped)} was already near the {edge}")
    notes.append("saved for every night")
    return "Levels now: " + _levels_text(smart) + f" ({'; '.join(notes)})"


@mcp.tool(annotations=WRITE)
async def eight_sleep_set_bedtime(
    time: Annotated[str | None, Field(description="New bedtime, e.g. '22:30'")] = None,
    days: Annotated[str | list[str] | None, Field(description="'weekdays', 'every day', day names")] = None,
    enabled: bool | None = None,
) -> str:
    """Change when the bed starts its bedtime routine, and on which days."""
    api().require_mutations()
    t = await _temperature()
    s = copy.deepcopy(t.get("currentSchedule"))
    if not s:
        raise EightSleepError("There's no bedtime schedule to change; set one up in the Eight Sleep app.")
    if time:
        s["time"] = parse_time(time)
    if days is not None:
        s["days"] = parse_days(days)
    if enabled is not None:
        s["enabled"] = enabled
    # Send every schedule back, with this one changed, so other schedules (e.g. weekends) survive.
    all_ = await api().request("GET", "app", "/v1/users/{uid}/temperature/all", allow_404=True) or {}
    schedules = [s if x.get("id") == s.get("id") else x for x in all_.get("schedules") or []] or [s]
    if s.get("id") not in {x.get("id") for x in schedules}:
        schedules.append(s)
    await api().request("PUT", "app", "/v1/users/{uid}/bedtime",
                        {"scheduleType": t.get("scheduleType", "smart"), "smart": t.get("smart", {}),
                         "schedules": schedules})
    return f"Bedtime now {s['time'][:5]} {_days_text({d: True for d in s['days']})} ({'on' if s['enabled'] else 'off'})"


# ── Naps ─────────────────────────────────────────────────────────────────

NAP_PATH = "/v1/users/{uid}/temperature/nap-mode"


@mcp.tool(annotations=WRITE)
async def eight_sleep_start_nap(
    minutes: Annotated[int, Field(ge=5, le=240, description="Nap length")] = 20,
    level: Annotated[float | None, Field(ge=-10, le=10, description="Temperature -10…+10; omit for his nap default")] = None,
    alarm: Annotated[bool | None, Field(description="Wake him with the Pod's alarm at the end; omit for his nap default")] = None,
) -> str:
    """Start a nap now: the bed goes to a nap temperature for a while, optionally with an alarm. Only if Nicholas asked."""
    api().require_mutations()
    defaults = await api().request("GET", "app", NAP_PATH) or {}
    levels = dict(defaults.get("defaultLevels") or {"pod": -30})
    if level is not None:
        levels["pod"] = to_api_level(level)
    if alarm is None:
        alarm = bool(defaults.get("alarmRequested"))
    await api().request("POST", "app", f"{NAP_PATH}/activate",
                        {"duration": hhmmss(minutes), "levels": levels, "alarmRequested": alarm})
    return f"Nap started: {minutes} min at {app_level(levels['pod'])}, {'alarm at the end' if alarm else 'no alarm'}"


@mcp.tool(annotations=WRITE)
async def eight_sleep_extend_nap(minutes: Annotated[int, Field(ge=5, le=120)] = 15) -> str:
    """Make the current nap longer."""
    api().require_mutations()
    await api().request("POST", "app", f"{NAP_PATH}/extend", {"additionalDuration": hhmmss(minutes)})
    return f"Nap extended by {minutes} min"


@mcp.tool(annotations=WRITE)
async def eight_sleep_end_nap() -> str:
    """End the current nap now."""
    api().require_mutations()
    try:
        await api().request("PUT", "app", f"{NAP_PATH}/deactivate", {})
    except EightSleepError as e:  # older app versions used POST
        if "HTTP 404" not in str(e) and "HTTP 405" not in str(e):
            raise
        await api().request("POST", "app", f"{NAP_PATH}/deactivate", {})
    return "Nap ended"


@mcp.tool(annotations=READ)
async def eight_sleep_nap_status() -> str:
    """Whether a nap is running, and until when."""
    s = await api().request("GET", "app", f"{NAP_PATH}/status", allow_404=True)
    if not s:
        return "No nap running."
    pod = (s.get("levels") or {}).get("pod")
    return f"Nap running until {s.get('endTime')}" + (f" at {app_level(pod)}" if pod is not None else "") + \
        (", alarm at the end" if s.get("alarmRequested") else "")


# ── Sounds ───────────────────────────────────────────────────────────────

AUDIO_PATH = "/v1/users/{uid}/audio"
NO_SPEAKER = "This Pod has no speaker paired, so it can't play sounds."


async def _tracks() -> list[dict] | None:
    body = await api().request("GET", "app", f"{AUDIO_PATH}/tracks", allow_404=True)
    return None if body is None else body.get("tracks", [])


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


@mcp.tool(annotations=READ)
async def eight_sleep_list_sounds() -> str:
    """The sounds the Pod can play."""
    tracks = await _tracks()
    if tracks is None:
        return NO_SPEAKER
    return "\n".join(f"{t['name']} [{t['id']}]" for t in sorted(tracks, key=lambda t: t["name"])) or "No sounds."


@mcp.tool(annotations=WRITE)
async def eight_sleep_play_sound(
    track: Annotated[str, Field(description="Sound name or id, e.g. 'pink noise', 'rain'")],
    volume: Annotated[int | None, Field(ge=0, le=100)] = None,
) -> str:
    """Play a sound on the Pod's speaker until stopped. Only if Nicholas asked."""
    api().require_mutations()
    if await api().request("GET", "app", f"{AUDIO_PATH}/player", allow_404=True) is None:
        raise EightSleepError(NO_SPEAKER)  # tracks list fine without a speaker; playing doesn't
    tracks = await _tracks() or []
    want = _key(track)
    found = [t for t in tracks if _key(t["id"]) == want or _key(t["name"]) == want] or \
            [t for t in tracks if want and (want in _key(t["name"]) or want in _key(t["id"]))]
    if len(found) != 1:
        names = ", ".join(sorted(t["name"] for t in (found or tracks)))
        lead = "Several sounds match" if found else f"No sound called '{track}'"
        raise EightSleepError(f"{lead}. Sounds: {names}.")
    t = found[0]
    await api().request("PUT", "app", f"{AUDIO_PATH}/player/currentTrack", {"id": t["id"], "stopCriteria": "ManualStop"})
    if volume is not None:
        await api().request("PUT", "app", f"{AUDIO_PATH}/player/volume", {"volume": volume})
    await api().request("PUT", "app", f"{AUDIO_PATH}/player/state", {"state": "Playing"})
    return f"Playing {t['name']}" + (f" at {volume}" if volume is not None else "") + " (until stopped)"


@mcp.tool(annotations=WRITE)
async def eight_sleep_stop_sound() -> str:
    """Stop the sound playing on the Pod."""
    api().require_mutations()
    await api().request("PUT", "app", f"{AUDIO_PATH}/player/state", {"state": "Paused"})
    return "Sound stopped"


@mcp.tool(annotations=WRITE)
async def eight_sleep_set_sound_volume(volume: Annotated[int, Field(ge=0, le=100)]) -> str:
    """Change the Pod speaker's volume."""
    api().require_mutations()
    await api().request("PUT", "app", f"{AUDIO_PATH}/player/volume", {"volume": volume})
    return f"Volume {volume}"


@mcp.tool(annotations=READ)
async def eight_sleep_sound_status() -> str:
    """What the Pod's speaker is doing."""
    p = await api().request("GET", "app", f"{AUDIO_PATH}/player", allow_404=True)
    if p is None:
        return NO_SPEAKER
    track = (p.get("currentTrack") or {}).get("name")
    return f"{p.get('state', 'Unknown')}" + (f" {track}" if track else "") + \
        (f" at volume {p['volume']}" if p.get("volume") is not None else "")


# ── The Pod: status, priming, away ───────────────────────────────────────

async def _device() -> dict:
    return await api().request("GET", "client", "/users/{uid}/current-device")


async def _in_bed(device: dict) -> str:
    today = dt.date.today()
    # to = tomorrow, in case a night is filed under the date he wakes up.
    trends = await api().request("GET", "client", "/users/{uid}/trends", params={
        "tz": device.get("timeZone") or host.TIMEZONE, "from": str(today - dt.timedelta(days=1)),
        "to": str(today + dt.timedelta(days=1)),
        "include-main": "false", "include-all-sessions": "true", "model-version": "v2"}, allow_404=True) or {}
    days = trends.get("days") or []
    if not days:
        return "unknown"
    last = days[-1]
    start, end = (v if v not in (None, "", "None") else None
                  for v in (last.get("presenceStart"), last.get("presenceEnd")))
    if start and (not end or end < start):
        return f"probably yes (since {start})"
    return "probably not"


@mcp.tool(annotations=READ)
async def eight_sleep_pod_status() -> str:
    """The Pod's health: online, firmware, water and priming, current and target temperature, and whether he's probably in bed."""
    device = await _device()
    r = (await api().request("GET", "client", f"/devices/{device['id']}")).get("result", {})
    side = "right" if device.get("side") == "right" else "left"
    lines = [("Online" if r.get("online") else "OFFLINE") +
             (f", firmware {r['firmwareVersion']}" if r.get("firmwareVersion") else "") +
             (" (updating)" if r.get("firmwareUpdating") else "")]
    if r.get("priming"):
        lines.append("Water: priming now")
    elif r.get("hasWater") is False or r.get("needsPriming"):
        lines.append("Water: LOW — add water, then prime" if r.get("hasWater") is False else "Water: needs priming")
    else:
        last = (r.get("lastPrime") or "")[:10]
        lines.append("Water: OK; doesn't need priming" + (f" (last primed {last})" if last else ""))
    now, target = r.get(f"{side}HeatingLevel"), r.get(f"{side}TargetHeatingLevel")
    if now is not None:
        lines.append(f"Bed now {app_level(now)}" + (f", heading to {app_level(target)}" if target is not None else ""))
    lines.append(f"In bed: {await _in_bed(device)}")
    return "\n".join(lines)


@mcp.tool(annotations=WRITE)
async def eight_sleep_prime() -> str:
    """Start priming the Pod (refills its water lines; noisy, takes a while). Only after Nicholas has confirmed."""
    api().require_mutations()
    device, uid = await _device(), await api().user_id()
    try:
        await api().request("POST", "app", f"/v1/devices/{device['id']}/priming/tasks",
                            {"notifications": {"users": [uid], "meta": "fill_pod"}})
    except EightSleepError as e:
        if "HTTP 409" in str(e):
            return "The Pod is already priming"
        raise
    return "Priming started; it takes a while and is noisy"


@mcp.tool(annotations=WRITE)
async def eight_sleep_cancel_priming() -> str:
    """Stop a priming run."""
    api().require_mutations()
    device = await _device()
    await api().request("DELETE", "app", f"/v1/devices/{device['id']}/priming/tasks")
    return "Priming cancelled"
