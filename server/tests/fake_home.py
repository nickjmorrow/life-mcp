"""A fake Life Home API (httpx MockTransport) with a camera and a lamp."""
import json

import httpx

HOME = {"homes": [{
    "id": "H1", "name": "Home", "primary": True,
    "rooms": [{"id": "R1", "name": "Living Room"}, {"id": "R2", "name": "Bedroom"}],
    "accessories": [
        {"id": "A1", "name": "Front Camera", "room": "Living Room", "category": "IP Camera", "reachable": True,
         "services": [
             {"id": "S1", "name": "Motion", "type": "Motion Sensor", "primary": False, "characteristics": [
                 {"id": "C1", "name": "Motion Detected", "type": "x", "value": 1, "readable": True, "writable": False,
                  "format": "bool"}]},
             {"id": "S2", "name": "Light", "type": "Light Sensor", "primary": False, "characteristics": [
                 {"id": "C2", "name": "Current Light Level", "type": "x", "value": 42.5, "readable": True,
                  "writable": False, "format": "float", "unit": "lux"}]},
             {"id": "S5", "name": "", "type": "Speaker", "primary": False, "characteristics": [
                 {"id": "C7", "name": "Volume", "type": "x", "value": 88, "readable": True, "writable": True,
                  "format": "uint8", "min": 0, "max": 100, "unit": "percentage"}]},
             {"id": "S6", "name": "", "type": "Microphone", "primary": False, "characteristics": [
                 {"id": "C8", "name": "Volume", "type": "x", "value": 79, "readable": True, "writable": True,
                  "format": "uint8", "min": 0, "max": 100, "unit": "percentage"},
                 {"id": "C9", "name": "Custom", "type": "x", "value": 3, "readable": True, "writable": True,
                  "format": "int"}]}]},
        {"id": "A2", "name": "Lamp", "room": "Bedroom", "category": "Lightbulb", "reachable": True,
         "services": [{"id": "S3", "name": "Lamp", "type": "Lightbulb", "primary": True, "characteristics": [
             {"id": "C3", "name": "Power State", "type": "x", "value": 0, "readable": True, "writable": True,
              "format": "bool"},
             {"id": "C4", "name": "Brightness", "type": "x", "value": 60, "readable": True, "writable": True,
              "format": "int", "min": 0, "max": 100, "step": 1, "unit": "percentage"},
             {"id": "C5", "name": "Name", "type": "x", "value": "Lamp", "readable": True, "writable": False,
              "format": "string"}]}]},
        {"id": "A3", "name": "Lamp", "room": "Living Room", "category": "Lightbulb", "reachable": False,
         "services": [{"id": "S4", "name": "Lamp", "type": "Lightbulb", "primary": True, "characteristics": [
             {"id": "C6", "name": "Power State", "type": "x", "value": 1, "readable": True, "writable": True,
              "format": "bool"}]}]},
    ],
    "scenes": [{"id": "SC1", "name": "Good Night", "type": "HMActionSetTypeSleep"},
               {"id": "SC2", "name": "Movie Time", "type": "HMActionSetTypeUserDefined"}],
    "automations": [{"id": "T1", "name": "Porch at sunset", "enabled": True, "scenes": ["Movie Time"],
                     "actions": ["Lamp: Power State → 1"]},
                    {"id": "T2", "name": "Morning lights", "enabled": False}],
}]}


class FakeHome:
    def __init__(self, down=False, write_error=None, token=None):
        self.posts: list[tuple[str, dict]] = []
        self.down, self.write_error = down, write_error
        self.token = token  # the secret Life Home expects; None = read it from home_mcp.TOKEN_PATH

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("refused")
        import home_mcp
        expected = self.token or home_mcp.TOKEN_PATH.read_text().strip()
        if request.headers.get(home_mcp.TOKEN_HEADER) != expected:
            return httpx.Response(403, json={"error": "forbidden"})
        if request.method == "GET" and request.url.path == "/home":
            return httpx.Response(200, json=HOME)
        body = json.loads(request.content or b"{}")
        self.posts.append((request.url.path, body))
        if request.url.path == "/write":
            if self.write_error:
                return httpx.Response(500, json={"error": self.write_error})
            return httpx.Response(200, json={"value": body["value"]})
        if request.url.path == "/read":
            return httpx.Response(200, json={"value": 1})
        if request.url.path == "/scene":
            return httpx.Response(200, json={"ran": "Good Night"})
        if request.url.path == "/automation":
            return httpx.Response(200, json={"name": "x", "enabled": body["enabled"]})
        return httpx.Response(404, json={"error": "no such endpoint"})

    def transport(self):
        return httpx.MockTransport(self.handler)
