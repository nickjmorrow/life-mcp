"""The Apple TV via pyatv, with the credentials saved by pair_appletv.py.

Every call runs through homepods._isolated (own thread and loop, one time limit): pyatv can block
an event loop outright (seen live with the HomePods).
"""
import asyncio
import json
import os
import re

import pyatv
from fastmcp.exceptions import ToolError
from pyatv.const import KeyboardFocusState, Protocol

from homepods import CONNECT_S, SCAN_S, HomePodError, _close, _isolated, _timed
from host import NAME as HOST
import private

CREDS = os.path.expanduser("~/.config/life-mcp/appletv.json")
TV_IP: str | None = private.get("apple_tv_ip")  # a hint for discovery; None scans the LAN
KEYS = ("up", "down", "left", "right", "select", "menu", "home", "play_pause")
LABEL = "the Apple TV"


class TVError(ToolError):
    """An Apple TV problem to tell Claude about in plain words."""


def _norm(text: str) -> str:
    words = re.sub(r"[^a-z0-9+]+", " ", text.lower().replace("\xa0", " ")).replace("+", " plus").split()
    return " ".join(w for w in words if w not in ("the", "app", "my"))


def match_app(name: str, apps: list[dict]) -> dict:
    """The installed app that name means: exact, then prefix, then contains."""
    key = _norm(name)
    for rule in (lambda n: n == key, lambda n: n.startswith(key), lambda n: key in n):
        found = [a for a in apps if key and rule(_norm(a["name"]))]
        if len(found) == 1:
            return found[0]
        if found:
            raise TVError(f"'{name}' could mean: {', '.join(a['name'] for a in found)}.")
    names = ", ".join(sorted(a["name"].replace("\xa0", " ") for a in apps))
    raise TVError(f"No app called '{name}'. Apps: {names}.")


def _is_tv(conf, creds: dict) -> bool:
    """pyatv's main identifier is whichever service a scan saw first, so match any of them."""
    ours = {creds.get("identifier"), *creds.get("all_identifiers", [])} - {None}
    return bool(ours & set(conf.all_identifiers))


def _maybe(read):
    try:
        return read()
    except pyatv.exceptions.NotSupportedError:
        return None


class AppleTV:
    def __init__(self, ip: str = TV_IP, creds_path: str = CREDS):
        self.ip, self.creds_path = ip, creds_path

    def _creds(self) -> dict:
        try:
            with open(self.creds_path) as f:
                creds = json.load(f)
        except (OSError, ValueError):
            creds = {}
        if not creds.get("Companion") or not creds.get("identifier"):
            raise TVError(f"The Apple TV isn't paired yet: run pair_appletv.py on {HOST} once (a PIN shows on the TV).")
        return creds

    async def _connect(self):
        creds = self._creds()
        loop = asyncio.get_running_loop()
        confs = await _timed(LABEL, pyatv.scan(loop, hosts=[self.ip], timeout=SCAN_S), SCAN_S + 2)
        conf = next((c for c in confs or [] if _is_tv(c, creds)), None)
        if conf is None:  # its address changed: find it by identifier
            everyone = await _timed(LABEL, pyatv.scan(loop, timeout=SCAN_S), SCAN_S * 2)
            conf = next((c for c in everyone or [] if _is_tv(c, creds)), None)
            if conf is None:
                raise TVError("Can't find the Apple TV on the network. Is it plugged in?")
            self.ip = str(conf.address)
        conf.set_credentials(Protocol.Companion, creds["Companion"])
        if creds.get("AirPlay"):
            conf.set_credentials(Protocol.AirPlay, creds["AirPlay"])
        return await _timed(LABEL, pyatv.connect(conf, loop), CONNECT_S)

    async def _run(self, fn):
        async def work():
            atv = await self._connect()
            try:
                return await fn(atv)
            finally:
                await _close(atv)
        try:
            return await _isolated(LABEL, work)
        except HomePodError as e:
            raise TVError(str(e).replace("'the Apple TV'", "The Apple TV"))
        except pyatv.exceptions.AuthenticationError:
            raise TVError(f"The Apple TV no longer accepts {HOST}'s pairing: run pair_appletv.py again.")

    async def status(self) -> dict:
        async def fn(atv):
            playing = await _timed(LABEL, atv.metadata.playing())
            app = _maybe(lambda: atv.metadata.app)  # pyatv: unavailable when nothing has played
            power = _maybe(lambda: atv.power.power_state.name.lower())
            return {"power": power or "unknown", "app": app.name if app else None,
                    "state": playing.device_state.name.lower(), "title": playing.title, "artist": playing.artist}
        return await self._run(fn)

    async def power(self, on: bool) -> None:
        async def fn(atv):
            await _timed(LABEL, atv.power.turn_on() if on else atv.power.turn_off())
        await self._run(fn)

    async def apps(self) -> list[dict]:
        async def fn(atv):
            return [{"name": a.name, "id": a.identifier} for a in await _timed(LABEL, atv.apps.app_list())]
        return await self._run(fn)

    async def launch(self, name: str) -> str:
        async def fn(atv):
            app = match_app(name, [{"name": a.name, "id": a.identifier} for a in await _timed(LABEL, atv.apps.app_list())])
            if atv.power.power_state.name.lower() != "on":
                await _timed(LABEL, atv.power.turn_on())
            await _timed(LABEL, atv.apps.launch_app(app["id"]))
            return app["name"].replace("\xa0", " ")
        return await self._run(fn)

    async def control(self, action: str) -> None:
        async def fn(atv):
            rc = atv.remote_control
            try:
                await _timed(LABEL, {"play": rc.play, "pause": rc.pause, "next": rc.next,
                                     "previous": rc.previous}[action]())
            except pyatv.exceptions.NotSupportedError:
                raise TVError(f"The Apple TV can't {action} right now (nothing playing?).")
        await self._run(fn)

    async def keys(self, keys: list[str]) -> None:
        bad = [k for k in keys if k not in KEYS]
        if bad:
            raise TVError(f"Unknown remote key(s): {', '.join(bad)}. Keys: {', '.join(KEYS)}.")

        async def fn(atv):
            rc = atv.remote_control
            for i, k in enumerate(keys):
                if i:
                    await asyncio.sleep(0.3)
                await _timed(LABEL, getattr(rc, k)())
        await self._run(fn)

    async def volume(self, level: float | None, change: float | None) -> float:
        async def fn(atv):
            try:
                target = level if level is not None else atv.audio.volume + change
                target = max(0.0, min(100.0, float(target)))
                await _timed(LABEL, atv.audio.set_volume(target))
                return target
            except pyatv.exceptions.NotSupportedError:
                raise TVError("The Apple TV can't change volume right now (nothing playing?).")
        return await self._run(fn)

    async def type_text(self, text: str, mode: str) -> str:
        """Type into the text field selected on screen: replace what's there, append, or clear. Returns the field."""
        async def fn(atv):
            kb = atv.keyboard
            if kb.text_focus_state != KeyboardFocusState.Focused:
                raise TVError("No text field is selected on the Apple TV. Move to a search box or text field first.")
            if mode == "clear":
                await _timed(LABEL, kb.text_clear())
            elif mode == "append":
                await _timed(LABEL, kb.text_append(text))
            else:
                await _timed(LABEL, kb.text_set(text))
            return await _timed(LABEL, kb.text_get()) or ""
        return await self._run(fn)

    async def read_text(self) -> tuple[bool, str | None]:
        """Whether a text field is selected on screen, and what it says."""
        async def fn(atv):
            kb = atv.keyboard
            if kb.text_focus_state != KeyboardFocusState.Focused:
                return False, None
            return True, await _timed(LABEL, kb.text_get()) or ""
        return await self._run(fn)
