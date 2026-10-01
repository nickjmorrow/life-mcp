"""Apple Home tools, through the Life Home helper app's local API (127.0.0.1:8767).

Life Home (../home-app) is a signed Mac Catalyst app with the HomeKit entitlement;
third-party HomeKit can't be reached any other way. Accessories are generic: a new device works
without new code.

Every request carries a shared secret (TOKEN_HEADER), so other programs on the Mac can't drive Apple
Home through Life Home. The connector makes the secret on first use in ~/.config/life-mcp/home-token
(600); Life Home, though sandboxed, may read exactly that file (a read-only sandbox exception in its
entitlements) and refuses requests without it.
"""
import os
import re
import secrets
from pathlib import Path
from typing import Annotated

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

API = "http://127.0.0.1:8767"
TIMEOUT_S = 15
mcp = FastMCP("Apple Home")
READ = {"readOnlyHint": True}
WRITE = {"readOnlyHint": False, "destructiveHint": False}
_transport: httpx.AsyncBaseTransport | None = None  # tests put a fake here
TOKEN_PATH = Path.home() / ".config" / "life-mcp" / "home-token"  # Life Home's entitlement names this path
TOKEN_HEADER = "X-Life-Home-Token"

ALIASES = {"power": "power state", "on": "power state", "off": "power state", "temperature": "target temperature"}
TRUE, FALSE = {"on", "true", "yes", "1"}, {"off", "false", "no", "0"}


def _norm(text: str) -> str:
    return " ".join(w for w in re.sub(r"[^a-z0-9]+", " ", str(text).lower()).split() if w not in ("the", "my"))


def token(path: Path | None = None) -> str:
    """The secret shared with Life Home, made (mode 600, folder 700) the first time."""
    path = path or TOKEN_PATH
    try:
        if text := path.read_text().strip():
            return text
    except FileNotFoundError:
        pass
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    new = secrets.token_urlsafe(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:  # made by a parallel call (or left empty): use what's there, else replace it
        if text := path.read_text().strip():
            return text
        path.unlink()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(new + "\n")
    return new


async def _call(method: str, path: str, body: dict | None = None) -> dict:
    headers = {TOKEN_HEADER: token()}
    try:
        async with httpx.AsyncClient(base_url=API, timeout=TIMEOUT_S, transport=_transport) as http:
            r = await http.request(method, path, json=body, headers=headers)
    except httpx.HTTPError:
        raise ToolError("Life Home isn't running on this Mac. Open ~/Applications/Life Home.app (it starts at login).")
    data = r.json() if r.content else {}
    if r.status_code >= 400:
        raise ToolError(f"Apple Home: {data.get('error', f'HTTP {r.status_code}')}")
    return data


async def _home() -> dict:
    homes = (await _call("GET", "/home")).get("homes", [])
    if not homes:
        raise ToolError("Life Home sees no Apple Home. Is it allowed in System Settings → Privacy & Security → Home?")
    return next((h for h in homes if h.get("primary")), homes[0])


def _label(a: dict) -> str:
    return f"{a['room']} {a['name']}".strip()


def _find(items: list[dict], name: str, kind: str, label=lambda x: x["name"]) -> dict:
    key = _norm(name)
    for rule in (lambda x: _norm(label(x)) == key, lambda x: _norm(x["name"]) == key,
                 lambda x: key in _norm(label(x))):
        found = [x for x in items if rule(x)]
        if len(found) == 1:
            return found[0]
        if found:
            raise ToolError(f"'{name}' could mean: {', '.join(sorted(label(x) for x in found))}. Say which.")
    raise ToolError(f"No {kind} called '{name}'. Options: {', '.join(sorted(label(x) for x in items)) or 'none'}.")


HIDDEN = {"custom", "identify", "name", "serial number", "hardware version"}


def _chars(accessory: dict) -> list[dict]:
    """The accessory's settings and readings, each with a `label`: its name, or, when two services
    share a name (the camera's speaker and microphone "Volume"), the service type plus the name."""
    found = [(s, c) for s in accessory["services"] for c in s["characteristics"]
             if _norm(c["name"]) not in HIDDEN]
    counts: dict[str, int] = {}
    for _, c in found:
        counts[_norm(c["name"])] = counts.get(_norm(c["name"]), 0) + 1
    return [{**c, "label": f"{s['type']} {c['name']}" if counts[_norm(c["name"])] > 1 else c["name"]}
            for s, c in found]


def _show(c: dict) -> str:
    v = c.get("value")
    if c.get("format") == "bool":
        return "yes" if v else "no"
    unit = {"percentage": "%", "celsius": "°C", "lux": " lux"}.get(c.get("unit", ""), "")
    return f"{v:g}{unit}" if isinstance(v, (int, float)) else str(v)


def _summary(a: dict) -> str:
    if not a["reachable"]:
        return "NOT REACHABLE"
    by = {_norm(c["name"]): c for c in _chars(a)}
    parts = []
    if "power state" in by:
        parts.append("on" if by["power state"]["value"] else "off")
    if "brightness" in by:
        parts.append(f"brightness {_show(by['brightness'])}")
    if "motion detected" in by:
        parts.append("motion detected" if by["motion detected"]["value"] else "no motion")
    if "current light level" in by:
        parts.append(f"light {_show(by['current light level'])}")
    if "current temperature" in by:
        parts.append(f"{_show(by['current temperature'])}")
    return ", ".join(parts) or "ok"


def _coerce(c: dict, value):
    fmt, name = c.get("format", ""), c["name"]
    if fmt == "bool":
        v = str(value).strip().lower()
        if v in TRUE:
            return True
        if v in FALSE:
            return False
        raise ToolError(f"{name} takes on/off.")
    if fmt in ("int", "uint8", "uint16", "uint32", "uint64", "float"):
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ToolError(f"{name} takes a number.")
        low, high = c.get("min"), c.get("max")
        if (low is not None and number < low) or (high is not None and number > high):
            raise ToolError(f"{name} goes from {low:g} to {high:g}.")
        if c.get("valid") and number not in c["valid"]:
            raise ToolError(f"{name} takes one of {', '.join(f'{v:g}' for v in c['valid'])}.")
        return number if fmt == "float" else int(round(number))
    return str(value)


@mcp.tool(annotations=READ)
async def home_list() -> str:
    """Everything in Apple Home: accessories by room with their state, scenes, and automations."""
    home = await _home()
    lines = [f"{a['room']}: {a['name']} ({a['category']}): {_summary(a)}"
             for a in sorted(home["accessories"], key=lambda a: (a["room"], a["name"]))]
    lines.append("Scenes: " + (", ".join(s["name"] for s in home["scenes"]) or "none"))
    lines.append("Automations: " + (", ".join(f"{t['name']} ({'on' if t['enabled'] else 'off'})"
                                              for t in home["automations"]) or "none"))
    return "\n".join(lines)


@mcp.tool(annotations=READ)
async def home_get(accessory: Annotated[str, Field(description="Accessory name, optionally with its room")]) -> str:
    """Every setting and reading of one accessory."""
    a = _find((await _home())["accessories"], accessory, "accessory", _label)
    lines = [f"{_label(a)} ({a['category']}){'' if a['reachable'] else ' — NOT REACHABLE'}"]
    for c in _chars(a):
        if c.get("readable") and c.get("value") is not None:
            lines.append(f"  {c['label']}: {_show(c)}" + ("" if c.get("writable") else " (read-only)"))
    return "\n".join(lines)


@mcp.tool(annotations=WRITE)
async def home_set(
    accessory: Annotated[str, Field(description="Accessory name, optionally with its room")],
    setting: Annotated[str, Field(description="What to change: 'power', 'brightness', 'target temperature', …")],
    value: Annotated[str | int | float | bool, Field(description="New value: on/off, a number, …")],
) -> str:
    """Change a setting on any Apple Home accessory."""
    a = _find((await _home())["accessories"], accessory, "accessory", _label)
    if not a["reachable"]:
        raise ToolError(f"{_label(a)} isn't reachable right now.")
    wanted = ALIASES.get(_norm(setting), _norm(setting))
    c = _find(_chars(a), wanted, "setting", lambda x: x["label"])
    if not c.get("writable"):
        raise ToolError(f"{c['name']} can't be changed.")
    result = await _call("POST", "/write", {"characteristic": c["id"], "value": _coerce(c, value)})
    return f"{_label(a)}: {c['label']} → {_show({**c, 'value': result.get('value')})}"


@mcp.tool(annotations=WRITE)
async def home_run_scene(name: Annotated[str, Field(description="Scene name")]) -> str:
    """Run an Apple Home scene."""
    scene = _find((await _home())["scenes"], name, "scene")
    await _call("POST", "/scene", {"id": scene["id"]})
    return f"Ran {scene['name']}"


@mcp.tool(annotations=READ)
async def home_list_automations() -> str:
    """Apple Home automations and whether each is on."""
    lines = []
    for t in (await _home())["automations"]:
        does = t.get("scenes", []) + t.get("actions", [])
        lines.append(f"{t['name']}: {'on' if t['enabled'] else 'off'}"
                     + (f" — runs {'; '.join(does)}" if does else " — its actions aren't visible to Life Home"
                        " (e.g. HomePod media; edit it in the Home app)"))
    return "\n".join(lines) or "No automations."


@mcp.tool(annotations=WRITE)
async def home_set_automation(name: Annotated[str, Field(description="Automation name")], enabled: bool) -> str:
    """Switch an Apple Home automation on or off."""
    t = _find((await _home())["automations"], name, "automation")
    await _call("POST", "/automation", {"id": t["id"], "enabled": enabled})
    return f"{t['name']} is now {'on' if enabled else 'off'}"


@mcp.tool(annotations=READ)
async def home_motion(room: Annotated[str | None, Field(description="Room, or omit for all")] = None) -> str:
    """Motion and occupancy sensors: is anyone moving in a room right now?"""
    home = await _home()
    lines = []
    for a in home["accessories"]:
        if room and _norm(room) not in _norm(a["room"]):
            continue
        for c in _chars(a):
            if _norm(c["name"]) in ("motion detected", "occupancy detected"):
                state = "not reachable" if not a["reachable"] else \
                    ("motion detected" if c["value"] else "no motion")
                lines.append(f"{a['room']} — {a['name']}: {state}")
    return "\n".join(lines) or "No motion sensors" + (f" in {room}." if room else ".")
