"""Philips Hue Bridge client (CLIP API v2) and the pure helpers the tools use."""
import re
from collections import defaultdict
from dataclasses import dataclass, field

import httpx
from fastmcp.exceptions import ToolError


class HueError(ToolError):
    """A problem to tell Claude about in plain words."""


# ── Names ────────────────────────────────────────────────────────────────


def normalize(name: str) -> str:
    """'The Living-Room!' → 'living room'. Voice transcripts vary; names shouldn't."""
    words = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
    words = re.sub(r"^(the|my) ", "", words)
    return re.sub(r" lights$", "", words)


def match(name: str, items: dict[str, str], kind: str,
          labels: dict[str, str] | None = None) -> str:
    """The id in `items` (id → name) that `name` means: exact match first, then substring.

    `labels` (id → text) gives clearer names in errors, e.g. a scene with its room.
    """
    if not items:
        raise HueError(f"Nothing to choose from: no {kind} is set up.")
    labels = labels or items
    found = _candidates(name, items)
    if len(found) == 1:
        return found[0]
    if found:
        options = ", ".join(sorted(labels[i] for i in found))
        raise HueError(f"'{name}' could mean: {options}. Say which.")
    options = ", ".join(sorted(set(labels.values())))
    raise HueError(f"No {kind} called '{name}'. Options: {options}.")


def match_all(name: str, items: dict[str, str], kind: str) -> list[str]:
    """Like match, but when the matches all have the same name, every one of them.

    Two bulbs both called "Office Ceiling" is normal, and there's no way to say which.
    """
    found = _candidates(name, items)
    if len(found) > 1 and len({normalize(items[i]) for i in found}) == 1:
        return found
    return [match(name, items, kind)]


def _candidates(name: str, items: dict[str, str]) -> list[str]:
    target = normalize(name)
    found = [i for i, n in items.items() if normalize(n) == target]
    if not found and target:
        found = [i for i, n in items.items() if target in normalize(n)]
    return found


# ── Colour ───────────────────────────────────────────────────────────────

COLORS = {
    "red": "ff0000", "orange": "ff8000", "yellow": "ffd200", "green": "00ff00",
    "teal": "00c8a0", "cyan": "00ffff", "blue": "0000ff", "purple": "8000ff",
    "pink": "ff60a0", "magenta": "ff00ff", "white": "ffffff",
}
TEMPS = {"warm": 2700, "neutral": 4000, "cool": 6500}


def color_to_xy(color: str) -> dict[str, float]:
    """A colour name or hex (#ff8800) → CIE 1931 xy, via sRGB → XYZ (D65)."""
    hexv = COLORS.get(normalize(color), color.strip().lstrip("#"))
    if not re.fullmatch(r"[0-9a-fA-F]{6}", hexv):
        raise HueError(f"Unknown colour '{color}'. Use a hex code like #ff8800 "
                       f"or one of: {', '.join(COLORS)}.")

    def linear(c: int) -> float:
        c /= 255
        return ((c + 0.055) / 1.055) ** 2.4 if c > 0.04045 else c / 12.92

    r, g, b = (linear(int(hexv[i:i + 2], 16)) for i in (0, 2, 4))
    x = r * 0.4124 + g * 0.3576 + b * 0.1805
    y = r * 0.2126 + g * 0.7152 + b * 0.0722
    z = r * 0.0193 + g * 0.1192 + b * 0.9505
    total = x + y + z
    if total == 0:
        raise HueError("Black isn't a colour a light can show. Turn it off instead.")
    return {"x": round(x / total, 4), "y": round(y / total, 4)}


def to_mirek(temp: str) -> int:
    """'warm' | 'neutral' | 'cool' | kelvin ('3000', '3000K') → mirek (153–500)."""
    t = normalize(str(temp))
    digits = t.removesuffix("kelvin").rstrip("k").strip()
    kelvin = TEMPS.get(t) or (int(digits) if digits.isdigit() else None)
    if kelvin is None or not 2000 <= kelvin <= 6500:
        raise HueError(f"Colour temperature '{temp}' should be warm, neutral, cool, or 2000–6500 K.")
    return min(500, max(153, round(1_000_000 / kelvin)))


def clamp_mirek(mirek: int, light: dict) -> int:
    """Keep a colour temperature inside what this bulb can do."""
    schema = light.get("color_temperature", {}).get("mirek_schema")
    if not schema:
        return mirek
    return min(schema["mirek_maximum"], max(schema["mirek_minimum"], mirek))


# ── Request bodies ───────────────────────────────────────────────────────


def light_body(on: bool | None = None, brightness: int | None = None,
               color_temp: str | None = None, color: str | None = None,
               brightness_change: int | None = None, fade_seconds: float | None = None) -> dict:
    """The CLIP v2 PUT body for a light or grouped_light. Unset fields stay as they are."""
    if color and color_temp:
        raise HueError("Give a colour or a colour temperature, not both.")
    if brightness is not None and brightness_change:
        raise HueError("Give a brightness or a brightness change, not both.")
    if brightness is not None and not 0 <= brightness <= 100:
        raise HueError("Brightness goes from 0 to 100.")
    fade = {"dynamics": {"duration": int(fade_seconds * 1000)}} if fade_seconds else {}
    if on is False or brightness == 0:
        return {"on": {"on": False}, **fade}
    body: dict = {}
    if brightness is not None:
        body["dimming"] = {"brightness": float(brightness)}
    if brightness_change:
        body["dimming_delta"] = {"action": "up" if brightness_change > 0 else "down",
                                 "brightness_delta": float(abs(brightness_change))}
    if color_temp:
        body["color_temperature"] = {"mirek": to_mirek(color_temp)}
    if color:
        body["color"] = {"xy": color_to_xy(color)}
    if not body and on is None:
        raise HueError("Nothing to change. Give on, brightness, brightness_change, color_temp or color.")
    if not (brightness_change and brightness_change < 0 and on is None):
        body["on"] = {"on": True}  # dimming down shouldn't switch lights on
    return {**body, **fade}


def describe(name: str, body: dict) -> str:
    """One line for Claude to relay, e.g. 'Living room: on, 30%, 2700 K'."""
    parts = []
    if "on" in body:
        parts.append("on" if body["on"]["on"] else "off")
    if "dimming" in body:
        parts.append(f"{body['dimming']['brightness']:g}%")
    if "dimming_delta" in body:
        d = body["dimming_delta"]
        parts.append(f"{d['brightness_delta']:g}% {'brighter' if d['action'] == 'up' else 'dimmer'}")
    if "color_temperature" in body:
        parts.append(f"{round(1_000_000 / body['color_temperature']['mirek'], -2):g} K")
    if "color" in body:
        parts.append("colour set")
    if "dynamics" in body:
        parts.append(f"over {body['dynamics']['duration'] / 1000:g} s")
    return f"{name}: " + ", ".join(parts)


# ── Home model ───────────────────────────────────────────────────────────


@dataclass
class Group:
    id: str
    name: str
    kind: str  # "room" or "zone"
    grouped_light: str | None
    lights: list[str] = field(default_factory=list)
    archetype: str = "other"
    devices: list[str] = field(default_factory=list)  # rooms: child device ids


@dataclass
class Home:
    groups: dict[str, Group]  # room/zone id → Group
    lights: dict[str, dict]  # light id → CLIP light
    grouped: dict[str, dict]  # grouped_light id → CLIP grouped_light
    scenes: dict[str, dict]  # scene id → CLIP scene
    everything: str | None  # the bridge_home grouped_light: every light at once
    devices: dict[str, dict] = field(default_factory=dict)  # device id → CLIP device
    room_of_device: dict[str, str] = field(default_factory=dict)  # device id → room id
    connectivity: dict[str, str] = field(default_factory=dict)  # device id → zigbee status
    updates: dict[str, str] = field(default_factory=dict)  # device id → software update state
    scripts: dict[str, dict] = field(default_factory=dict)  # behavior_script id → script
    automations: dict[str, dict] = field(default_factory=dict)  # behavior_instance id → instance
    time_zone: str | None = None
    sunset: str | None = None


def build_home(resources: list[dict]) -> Home:
    """The home from GET /clip/v2/resource (every resource, one list)."""
    by = defaultdict(list)
    for r in resources:
        by[r["type"]].append(r)
    # Rooms list devices; zones list lights. Map devices to their lights.
    lights_of_device = defaultdict(list)
    for light in by["light"]:
        lights_of_device[light["owner"]["rid"]].append(light["id"])
    groups = {}
    for kind in ("room", "zone"):
        for g in by[kind]:
            grouped_light = next(
                (s["rid"] for s in g.get("services", []) if s["rtype"] == "grouped_light"), None)
            ids, devices = [], []
            for child in g.get("children", []):
                if child["rtype"] == "light":
                    ids.append(child["rid"])
                else:
                    devices.append(child["rid"])
                    ids += lights_of_device[child["rid"]]
            groups[g["id"]] = Group(g["id"], g["metadata"]["name"], kind, grouped_light, ids,
                                    g["metadata"].get("archetype", "other"), devices)
    everything = next(
        (g["id"] for g in by["grouped_light"] if g.get("owner", {}).get("rtype") == "bridge_home"), None)
    bridge_info = (by["bridge"] or [{}])[0]
    geo = (by["geolocation"] or [{}])[0]
    return Home(
        groups=groups,
        lights={l["id"]: l for l in by["light"]},
        grouped={g["id"]: g for g in by["grouped_light"]},
        scenes={s["id"]: s for s in by["scene"]},
        everything=everything,
        devices={d["id"]: d for d in by["device"]},
        room_of_device={d: g.id for g in groups.values() if g.kind == "room" for d in g.devices},
        connectivity={z["owner"]["rid"]: z["status"] for z in by["zigbee_connectivity"]},
        updates={u["owner"]["rid"]: u.get("state") for u in by["device_software_update"]},
        scripts={s["id"]: s for s in by["behavior_script"]},
        automations={a["id"]: a for a in by["behavior_instance"]},
        time_zone=bridge_info.get("time_zone", {}).get("time_zone"),
        sunset=geo.get("sun_today", {}).get("sunset_time"),
    )


# ── Bridge ───────────────────────────────────────────────────────────────

TIMEOUT_S = 5


class Bridge:
    """Async client for https://<ip>/clip/v2/resource. Failures become HueError."""

    def __init__(self, ip: str, key: str, transport: httpx.AsyncBaseTransport | None = None):
        self._http = httpx.AsyncClient(
            base_url=f"https://{ip}/clip/v2",
            headers={"hue-application-key": key},
            verify=False,  # the bridge's cert is self-signed; we only reach it on the LAN
            timeout=TIMEOUT_S,
            transport=transport,
        )

    async def _call(self, method: str, path: str, body: dict | None = None) -> list[dict]:
        try:
            r = await self._http.request(method, path, json=body)
        except httpx.HTTPError:
            raise HueError("Can't reach the Hue Bridge from the server Mac. Is it powered and on the network?")
        if r.status_code in (401, 403):
            raise HueError("The Hue Bridge rejected the app key. Re-run pair.py on the server Mac.")
        try:
            payload = r.json()
        except ValueError:
            raise HueError(f"The Hue Bridge answered HTTP {r.status_code} without JSON.")
        if not isinstance(payload, dict):
            raise HueError("That answer didn't look like a Hue Bridge. Is HUE_BRIDGE_IP right?")
        errors = [e.get("description", "unknown error") for e in payload.get("errors", [])]
        # Errors alongside data are warnings: the bridge applied the change but flags
        # flaky bulbs with "may not have effect".
        if r.status_code >= 400 or (errors and not payload.get("data")):
            raise HueError("Hue Bridge: " + ("; ".join(errors) or f"HTTP {r.status_code}"))
        return payload.get("data", [])

    async def get(self, rtype: str) -> list[dict]:
        return await self._call("GET", f"/resource/{rtype}" if rtype else "/resource")

    async def put(self, rtype: str, rid: str, body: dict) -> None:
        await self._call("PUT", f"/resource/{rtype}/{rid}", body)

    async def post(self, rtype: str, body: dict) -> str:
        """Create a resource; returns its new id."""
        data = await self._call("POST", f"/resource/{rtype}", body)
        return data[0]["rid"]

    async def delete(self, rtype: str, rid: str) -> None:
        await self._call("DELETE", f"/resource/{rtype}/{rid}")

    async def home(self) -> Home:
        return build_home(await self.get(""))
