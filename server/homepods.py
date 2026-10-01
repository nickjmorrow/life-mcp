"""The HomePods (and Apple TV) themselves, via pyatv on the LAN. No pairing needed for
HomePods (AirPlay pairing: NotNeeded; checked 2026-09-27). Devices are found by one
scan, then each call connects by host (fast: ~1–3 s) and closes again."""
import asyncio
import os
import sys
import threading
import time

import pyatv
from fastmcp.exceptions import ToolError

SCAN_S = 5
DISCOVER_TTL_S = 300  # reuse a scan this long: saves ~5 s per voice request
CONNECT_S = 6
CLOSE_S = 2  # don't wait longer than this for a connection to close
MAX_STUCK_THREADS = 20  # more abandoned pyatv threads than this: restart (launchd brings it back)
OP_S = 20  # a whole HomePod operation (scan + connect + call), run in its own thread
CALL_S = 6  # every HomePod call; one hung forever live (the stereo pair's second speaker)


class HomePodError(ToolError):
    """A HomePod problem to tell Claude about in plain words."""


def kind_of(model: str | None) -> str:
    m = (model or "").lower()
    if "homepod" in m:
        return "homepod"
    if "apple tv" in m:
        return "tv"
    if m.startswith("mac"):
        return "mac"
    return "other"


async def _close(atv) -> None:
    """pyatv's close() returns cleanup tasks; wait for them so no sessions are left open."""
    tasks = atv.close()
    if tasks:
        await asyncio.wait(tasks, timeout=CLOSE_S)  # a stuck close shouldn't hang the answer


async def _timed(name: str, awaitable, limit: float | None = None):
    """Wait up to limit. On timeout, cancel and move on without waiting for the call to finish:
    pyatv keeps retrying after cancellation, and asyncio.wait_for would wait for that (hung live)."""
    task = asyncio.ensure_future(awaitable)
    done, _ = await asyncio.wait({task}, timeout=limit or CALL_S)
    if task in done:
        return task.result()
    task.cancel()
    task.add_done_callback(lambda t: t.cancelled() or t.exception())  # never "exception never retrieved"
    raise HomePodError(f"'{name}' didn't answer in time.")


async def _isolated(label: str, make, limit: float | None = None):
    """Run make() — a pyatv coroutine — on its own thread and event loop, and give up after the limit.
    Live, pyatv blocked the event loop in a synchronous socket read, so no asyncio time limit could
    fire and the whole server froze. A stuck call now leaves a daemon thread behind instead; too many
    of those and the service restarts itself."""
    stuck = sum(1 for t in threading.enumerate() if t.name.startswith("pyatv ") and t.is_alive())
    if stuck >= MAX_STUCK_THREADS:
        print(f"{stuck} HomePod calls are stuck; restarting the connector service.", file=sys.stderr, flush=True)
        os._exit(1)
    loop = asyncio.get_running_loop()
    done = loop.create_future()
    done.add_done_callback(lambda f: f.cancelled() or f.exception())  # late results: nobody waits, no warning

    def settle(result=None, error=None):
        if not done.done():
            done.set_exception(error) if error else done.set_result(result)

    def run():
        async def main():
            # Hand the answer over the moment it's ready; asyncio.run's clean-up of leftovers
            # (stuck closes, abandoned timeouts) can take much longer.
            try:
                result = await make()
            except Exception as e:  # noqa: BLE001 — handed to the caller as-is
                loop.call_soon_threadsafe(settle, None, e)
            else:
                loop.call_soon_threadsafe(settle, result)
        try:
            asyncio.run(main())
        except BaseException:  # noqa: BLE001 — the caller already has its answer or timed out
            loop.call_soon_threadsafe(settle, None, HomePodError(f"'{label}' stopped unexpectedly."))

    threading.Thread(target=run, daemon=True, name=f"pyatv {label}").start()
    try:
        return await asyncio.wait_for(asyncio.shield(done), limit or OP_S)
    except TimeoutError:
        raise HomePodError(f"'{label}' didn't answer in time.")


class HomePods:
    def __init__(self, addresses: dict[str, str] | None = None):
        self._addresses = dict(addresses or {})  # name → ip, refreshed by discover()
        self._known: dict[str, dict] = {}         # last successful scan
        self._scanned_at = 0.0

    async def discover(self) -> dict[str, dict]:
        """Every AirPlay device on the network (scan runs in its own thread; see _isolated)."""
        try:
            return await _isolated("the network scan", self._discover, SCAN_S * 2 + 1)
        except HomePodError:
            return dict(self._known)

    async def _discover(self, force: bool = False) -> dict[str, dict]:
        """Every AirPlay device on the network. Reuses a recent scan unless force; a scan that hangs
        (seen live) falls back to the last one instead of hanging the tool."""
        if not force and self._known and time.monotonic() - self._scanned_at < DISCOVER_TTL_S:
            return dict(self._known)
        loop = asyncio.get_running_loop()
        try:
            confs = await _timed("the network scan", pyatv.scan(loop, timeout=SCAN_S), SCAN_S * 2)
        except HomePodError:
            return dict(self._known)
        found = {}
        for conf in confs or []:
            model = conf.device_info.model_str
            found[conf.name] = {"ip": str(conf.address), "model": model, "kind": kind_of(model)}
            self._addresses[conf.name] = str(conf.address)
        if found:
            self._known, self._scanned_at = found, time.monotonic()
        return found or dict(self._known)

    async def _connect(self, name: str):
        """Connect to this speaker — checking it really is this speaker, since DHCP can hand its
        old address to another one. On a miss, scan afresh (not the cache) and try once more."""
        loop = asyncio.get_running_loop()
        conf = None
        for fresh in (False, True):
            if fresh or name not in self._addresses:
                await self._discover(force=fresh)
            ip = self._addresses.get(name)
            if not ip:
                continue
            confs = await _timed(name, pyatv.scan(loop, hosts=[ip], timeout=SCAN_S), SCAN_S + 2)
            conf = next((c for c in confs or [] if c.name == name), None)
            if conf:
                break
            self._addresses.pop(name, None)
        if conf is None:
            raise HomePodError(f"Can't find '{name}' on the network. Is it plugged in?")
        try:
            return await _timed(name, pyatv.connect(conf, loop), CONNECT_S)
        except (OSError, pyatv.exceptions.ConnectionFailedError):
            raise HomePodError(f"Couldn't connect to '{name}'.")

    async def _now_playing(self, name: str) -> dict:
        atv = await self._connect(name)
        try:
            p = await _timed(name, atv.metadata.playing())
            app = None
            try:
                app = atv.metadata.app.name if atv.metadata.app else None
            except Exception:
                pass
            return {"state": p.device_state.name.lower(), "title": p.title, "artist": p.artist,
                    "album": p.album, "app": app}
        finally:
            await _close(atv)

    async def _volume(self, name: str) -> float:
        atv = await self._connect(name)
        try:
            return atv.audio.volume
        finally:
            await _close(atv)

    async def _set_volume(self, name: str, level: float) -> None:
        atv = await self._connect(name)
        try:
            await _timed(name, atv.audio.set_volume(max(0.0, min(100.0, float(level)))))
        finally:
            await _close(atv)

    async def _control(self, name: str, action: str) -> None:
        atv = await self._connect(name)
        try:
            rc = atv.remote_control
            await _timed(name, {"pause": rc.pause, "resume": rc.play, "next": rc.next, "previous": rc.previous}[action]())
        except pyatv.exceptions.NotSupportedError:
            raise HomePodError(f"'{name}' can't {action} right now (nothing playing?).")
        finally:
            await _close(atv)

    # Public calls: each HomePod operation runs isolated (own thread and loop), under one limit.
    async def now_playing(self, name: str) -> dict:
        return await _isolated(name, lambda: self._now_playing(name))

    async def volume(self, name: str) -> float:
        return await _isolated(name, lambda: self._volume(name))

    async def set_volume(self, name: str, level: float) -> None:
        return await _isolated(name, lambda: self._set_volume(name, level))

    async def control(self, name: str, action: str) -> None:
        return await _isolated(name, lambda: self._control(name, action))
