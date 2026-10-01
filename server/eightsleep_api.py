"""A small Eight Sleep API client for the tools the npm server doesn't have.

Shares the npm server's login: ~/.eight-sleep-mcp/tokens.json (the access token, which
both servers renew) and config.json (email/password and the mutation gate). The app's
client id/secret are read at runtime from the npm package, so no credentials live here.
Endpoints (the Eight Sleep app's own API, found by watching the app; no public docs): app-api.8slp.net
for alarms, temperature/levels, bedtime, naps, sounds and priming, client-api.8slp.net/v1 for the device
and trends, auth-api.8slp.net/v1/tokens for the password login. {uid} in a path is the signed-in user.
"""
import asyncio
import json
import os
import re
import time
from pathlib import Path

import httpx
from fastmcp.exceptions import ToolError

from host import NAME as MACHINE

HOME = Path.home() / ".eight-sleep-mcp"
NPM_CONSTANTS = Path("/opt/homebrew/lib/node_modules/eight-sleep-mcp-unofficial/dist/constants.js")
HOSTS = {"app": "https://app-api.8slp.net", "client": "https://client-api.8slp.net/v1"}
AUTH_URL = "https://auth-api.8slp.net/v1/tokens"
TIMEOUT_S = 15
EARLY_S = 60  # log in again this long before the token runs out
LOCK_WAIT_S = 10  # the npm server holds tokens.json.lock while it logs in
STALE_LOCK_S = 60


def _fresh(token: dict | None) -> bool:
    return bool(token and token.get("access_token") and token.get("expires_at", 0) > time.time() + EARLY_S)


class EightSleepError(ToolError):
    """A problem to tell Claude about in plain words."""


class EightSleep:
    def __init__(self, home: Path = HOME, constants: Path = NPM_CONSTANTS,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.home, self.constants = home, constants
        self._http = httpx.AsyncClient(timeout=TIMEOUT_S, transport=transport)
        self._token: dict | None = None

    def _config(self) -> dict:
        try:
            return json.loads((self.home / "config.json").read_text())
        except (OSError, ValueError):
            return {}

    @property
    def mutations_allowed(self) -> bool:
        return str(self._config().get("EIGHT_SLEEP_ALLOW_MUTATIONS", "")).lower() == "true"

    def require_mutations(self) -> None:
        if not self.mutations_allowed:
            raise EightSleepError("Changing the bed is switched off "
                                  "(EIGHT_SLEEP_ALLOW_MUTATIONS in ~/.eight-sleep-mcp/config.json).")

    def _saved_token(self) -> dict | None:
        try:
            return json.loads((self.home / "tokens.json").read_text())
        except (OSError, ValueError):
            return None

    def _client_credentials(self) -> tuple[str, str]:
        try:
            text = self.constants.read_text()
            return (re.search(r'DEFAULT_CLIENT_ID = "([^"]+)"', text)[1],
                    re.search(r'DEFAULT_CLIENT_SECRET = "([^"]+)"', text)[1])
        except (OSError, TypeError):
            raise EightSleepError("Can't find the Eight Sleep app credentials in the npm package "
                                  "(eight-sleep-mcp-unofficial). Is it still installed?")

    async def _login(self) -> dict:
        cfg = self._config()
        email, password = cfg.get("EIGHT_SLEEP_EMAIL"), cfg.get("EIGHT_SLEEP_PASSWORD")
        if not (email and password):
            raise EightSleepError("The Eight Sleep login isn't set up: ~/.eight-sleep-mcp/config.json "
                                  "needs EIGHT_SLEEP_EMAIL and EIGHT_SLEEP_PASSWORD.")
        client_id, client_secret = self._client_credentials()
        try:
            r = await self._http.post(AUTH_URL, json={"client_id": client_id, "client_secret": client_secret,
                                                      "grant_type": "password", "username": email,
                                                      "password": password})
        except httpx.HTTPError:
            raise EightSleepError(f"{MACHINE} can't reach Eight Sleep.")
        if r.status_code != 200:
            raise EightSleepError("The Eight Sleep login failed; check ~/.eight-sleep-mcp/config.json.")
        body = r.json()
        previous = self._saved_token() or {}
        token = {"access_token": body["access_token"],
                 "expires_at": int(time.time()) + int(body.get("expires_in") or 3600),
                 "user_id": body.get("userId") or previous.get("user_id")}
        # Same as the npm server: write a 0600 temp file, then rename over tokens.json.
        path = self.home / "tokens.json"
        tmp = path.with_name(f"tokens.json.tmp-{os.getpid()}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(token, f)
        os.replace(tmp, path)
        return token

    async def _with_lock(self, fn):
        """Hold tokens.json.lock (the npm server's lock) while renewing the token."""
        lock = self.home / "tokens.json.lock"
        start = time.monotonic()
        while True:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
                break
            except FileExistsError:
                try:
                    if time.time() - lock.stat().st_mtime > STALE_LOCK_S:
                        lock.unlink(missing_ok=True)  # left behind by a crashed process
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() - start > LOCK_WAIT_S:
                    raise EightSleepError("The Eight Sleep login is busy; try again in a moment.")
                await asyncio.sleep(0.1)
        try:
            os.write(fd, f"{os.getpid()}\n".encode())
            return await fn()
        finally:
            os.close(fd)
            lock.unlink(missing_ok=True)

    async def _renew(self, failed_access_token: str | None = None) -> dict:
        """A usable token: the one the other server just saved if it's fresh, else a new login."""
        async def inside():
            saved = self._saved_token()
            if _fresh(saved) and saved["access_token"] != failed_access_token:
                return saved
            return await self._login()
        return await self._with_lock(inside)

    async def _current_token(self) -> dict:
        token = self._token or self._saved_token()
        if not _fresh(token):
            token = await self._renew()
        self._token = token
        return token

    async def user_id(self) -> str:
        return (await self._current_token())["user_id"]

    async def request(self, method: str, host: str, path: str, json_body=None, params=None,
                      headers: dict | None = None, allow_404: bool = False):
        token = await self._current_token()
        for attempt in (1, 2):
            url = HOSTS[host] + path.replace("{uid}", token["user_id"])
            try:
                r = await self._http.request(
                    method, url, json=json_body, params=params,
                    headers={"Authorization": f"Bearer {token['access_token']}",
                             "Accept": "application/json", **(headers or {})})
            except httpx.HTTPError:
                raise EightSleepError(f"{MACHINE} can't reach Eight Sleep.")
            if r.status_code == 401 and attempt == 1:
                token = self._token = await self._renew(failed_access_token=token["access_token"])
                continue
            break
        if r.status_code == 404 and allow_404:
            return None
        if r.status_code >= 400:
            try:
                message = r.json().get("message") or r.text
            except (ValueError, AttributeError):
                message = r.text
            if r.status_code == 403 and "subscription" in message.lower():
                raise EightSleepError("Eight Sleep says that needs an Autopilot subscription.")
            raise EightSleepError(f"Eight Sleep didn't accept that (HTTP {r.status_code}): {message[:200]}")
        if not r.content:
            return {}
        try:
            return r.json()
        except ValueError:
            return {}
