"""A fake Eight Sleep API behind httpx.MockTransport, plus a client wired to it."""
import json
import time

import httpx

from eightsleep_api import EightSleep


class FakeAPI:
    def __init__(self):
        self.routes: dict[tuple[str, str], tuple[int, object]] = {}
        self.calls: list[tuple[str, str, object]] = []
        self.logins = 0
        self.expire_once = False  # the next API call answers 401
        self.last_params: dict[str, dict] = {}  # "host:/path" → query params of the last call

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = ("auth" if "auth-api" in request.url.host else
                "app" if "app-api" in request.url.host else "client")
        path = request.url.path.removeprefix("/v1") if host == "client" else request.url.path
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, f"{host}:{path}", body))
        self.last_params[f"{host}:{path}"] = dict(request.url.params)
        if host == "auth":
            self.logins += 1
            return httpx.Response(200, json={"access_token": f"NEW{self.logins}", "expires_in": 72000, "userId": "U1"})
        if self.expire_once:
            self.expire_once = False
            return httpx.Response(401, json={"message": "expired"})
        status, data = self.routes.get((request.method, f"{host}:{path}"), (200, {}))
        return httpx.Response(status) if data is None else httpx.Response(status, json=data)

    def api_calls(self, method: str | None = None) -> list[tuple[str, str, object]]:
        return [c for c in self.calls if not c[1].startswith("auth:") and (method is None or c[0] == method)]


def make_api(tmp_path, fake: FakeAPI, expired: bool = False, mutations: bool = True) -> EightSleep:
    home = tmp_path / ".eight-sleep-mcp"
    home.mkdir(exist_ok=True)
    (home / "tokens.json").write_text(json.dumps(
        {"access_token": "OLD", "expires_at": int(time.time()) + (-10 if expired else 3600), "user_id": "U1"}))
    (home / "config.json").write_text(json.dumps(
        {"EIGHT_SLEEP_EMAIL": "e", "EIGHT_SLEEP_PASSWORD": "p",
         "EIGHT_SLEEP_ALLOW_MUTATIONS": "true" if mutations else "false"}))
    constants = tmp_path / "constants.js"
    constants.write_text('export const DEFAULT_CLIENT_ID = "cid";\nexport const DEFAULT_CLIENT_SECRET = "csec";\n')
    return EightSleep(home=home, constants=constants, transport=httpx.MockTransport(fake.handler))
