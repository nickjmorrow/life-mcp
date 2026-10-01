"""A fake Hue Bridge serving CLIP v2-shaped data through httpx.MockTransport.

Living room: Floor lamp (colour + white temp), Ceiling (white temp only).
Bedroom: Bedside (dimmable only, off). Zone Downstairs: Floor lamp + Ceiling.
Scenes: Relax + Read in Living room, Relax in Bedroom.
"""
import json

import httpx

from hue import Bridge


def light(id, device, name, on=True, brightness=50.0, color=False, ct=False):
    r = {"id": id, "type": "light", "owner": {"rid": device, "rtype": "device"},
         "metadata": {"name": name}, "on": {"on": on}, "dimming": {"brightness": brightness}}
    if ct:
        r["color_temperature"] = {"mirek": 366, "mirek_valid": True,
                                  "mirek_schema": {"mirek_minimum": 153, "mirek_maximum": 500}}
    if color:
        r["color"] = {"xy": {"x": 0.45, "y": 0.41}}
    return r


def group(id, kind, name, children, grouped_light):
    return {"id": id, "type": kind, "metadata": {"name": name}, "children": children,
            "services": [{"rid": grouped_light, "rtype": "grouped_light"}]}


def grouped(id, owner, rtype, on, brightness=None):
    r = {"id": id, "type": "grouped_light", "owner": {"rid": owner, "rtype": rtype}, "on": {"on": on}}
    if brightness is not None:
        r["dimming"] = {"brightness": brightness}
    return r


def scene(id, name, room):
    return {"id": id, "type": "scene", "metadata": {"name": name},
            "group": {"rid": room, "rtype": "room"}}


def device(id, name, light_id, archetype="classic_bulb"):
    return {"id": id, "type": "device", "metadata": {"name": name, "archetype": archetype},
            "services": [{"rid": light_id, "rtype": "light"}]}


def home_data() -> dict[str, list[dict]]:
    dev = lambda d: {"rid": d, "rtype": "device"}
    lig = lambda l: {"rid": l, "rtype": "light"}
    room = lambda *a: {**group(*a), "metadata": {"name": a[2], "archetype": "living_room"}}
    return {
        "light": [light("l1", "d1", "Floor lamp", color=True, ct=True),
                  light("l2", "d2", "Ceiling", brightness=80.0, ct=True),
                  light("l3", "d3", "Bedside", on=False)],
        "device": [device("d1", "Floor lamp", "l1", "floor_shade"), device("d2", "Ceiling", "l2"),
                   device("d3", "Bedside", "l3")],
        "room": [room("r1", "room", "Living room", [dev("d1"), dev("d2")], "g1"),
                 room("r2", "room", "Bedroom", [dev("d3")], "g2")],
        "zone": [group("z1", "zone", "Downstairs", [lig("l1"), lig("l2")], "g3")],
        "grouped_light": [grouped("g1", "r1", "room", True, 65.0),
                          grouped("g2", "r2", "room", False, 0.0),
                          grouped("g3", "z1", "zone", True, 65.0),
                          grouped("g0", "b0", "bridge_home", True)],
        "scene": [scene("s1", "Relax", "r1"), scene("s2", "Read", "r1"), scene("s3", "Relax", "r2")],
        "zigbee_connectivity": [{"id": "c1", "type": "zigbee_connectivity", "owner": {"rid": "d1", "rtype": "device"}, "status": "connected"},
                                {"id": "c3", "type": "zigbee_connectivity", "owner": {"rid": "d3", "rtype": "device"}, "status": "connectivity_issue"}],
        "device_software_update": [{"id": "u2", "type": "device_software_update", "owner": {"rid": "d2", "rtype": "device"}, "state": "update_pending"}],
        "behavior_script": [{"id": f"sc-{k}", "type": "behavior_script", "metadata": {"name": n, "category": "automation"}}
                            for k, n in (("schedule", "Schedule"), ("wake", "Basic wake up routine"),
                                         ("sleep", "Go to sleep routines"), ("timer", "Timers"),
                                         ("home", "Coming home"))],
        "behavior_instance": [
            {"id": "a1", "type": "behavior_instance", "script_id": "sc-schedule", "enabled": True,
             "metadata": {"name": "Morning"},
             "configuration": {"what": [{"group": {"rid": "r1", "rtype": "room"}, "recall": {"rid": "s2", "rtype": "scene"}}],
                               "when_extended": {"recurrence_days": ["monday", "tuesday", "wednesday", "thursday", "friday"],
                                                 "start_at": {"time_point": {"type": "time", "time": {"hour": 7, "minute": 0}}}},
                               "where": [{"group": {"rid": "r1", "rtype": "room"}}]}},
            {"id": "a2", "type": "behavior_instance", "script_id": "sc-schedule", "enabled": True,
             "metadata": {"name": "Turn off everything "},
             "configuration": {"what": [{"group": {"rid": "b0", "rtype": "bridge_home"}, "recall": {"rid": "rc-off", "rtype": "recipe"}}],
                               "when_extended": {"recurrence_days": ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"],
                                                 "start_at": {"time_point": {"type": "time", "time": {"hour": 0, "minute": 0}}}},
                               "where": [{"group": {"rid": "b0", "rtype": "bridge_home"}}]}},
        ],
        "bridge": [{"id": "br", "type": "bridge", "time_zone": {"time_zone": "America/Chicago"}}],
        "geolocation": [{"id": "geo", "type": "geolocation", "sun_today": {"sunset_time": "18:41:00"}}],
    }


class FakeBridge:
    def __init__(self, status: int = 200, error: Exception | None = None, reply: dict | list | None = None,
                 failing: set[str] = frozenset()):
        self.data = home_data()
        self.puts: list[tuple[str, str, dict]] = []
        self.posts: list[tuple[str, dict]] = []
        self.deletes: list[tuple[str, str]] = []
        self.status, self.error, self.reply = status, error, reply
        self.failing = failing  # resource ids whose PUTs the bridge rejects (429)

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.error:
            raise self.error
        if self.reply is not None:
            return httpx.Response(self.status, json=self.reply)
        rtype, *rid = [p for p in request.url.path.split("/")[4:] if p] or [""]  # /clip/v2/resource[/<type>[/<id>]]
        ok = lambda data: httpx.Response(200, json={"errors": [], "data": data})
        if request.method == "GET":
            return ok([r for rs in self.data.values() for r in rs] if not rtype else self.data[rtype])
        if request.method == "POST":
            self.posts.append((rtype, json.loads(request.content)))
            return ok([{"rid": f"new-{rtype}", "rtype": rtype}])
        if request.method == "DELETE":
            self.deletes.append((rtype, rid[0]))
            return ok([{"rid": rid[0], "rtype": rtype}])
        if rid[0] in self.failing:
            return httpx.Response(429, json={"errors": [{"description": "rate limit exceeded"}], "data": []})
        self.puts.append((rtype, rid[0], json.loads(request.content)))
        return ok([{"rid": rid[0], "rtype": rtype}])

    def bridge(self) -> Bridge:
        return Bridge("bridge.test", "test-key", transport=httpx.MockTransport(self.handler))
