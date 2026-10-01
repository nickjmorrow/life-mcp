"""Screenshots of the Apple TV through Xcode's devicectl (CoreDevice), no root needed.

Needs the server Mac paired with the TV once (`xcrun devicectl manage pair`, PIN shown on the TV) and
Developer Mode on there. The TV's CoreDevice identifier can change after pairing, so it's looked
up by type every time rather than stored.
"""
import asyncio
import json
import os
import tempfile

from fastmcp.exceptions import ToolError
from host import NAME as HOST

TIMEOUT_S = 60
WIDTH = 1280  # 4K PNGs are ~28 MB; a 1280-wide JPEG is ~80 KB and still readable


class ScreenError(ToolError):
    """A screenshot problem to tell Claude about in plain words."""


async def _run(*args: str, timeout: float = TIMEOUT_S) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise ScreenError("The Apple TV didn't answer in time. Is it on and on the network?")
    return proc.returncode, out.decode(errors="replace")


def pick_tv(listing: dict) -> str:
    """The paired, physical Apple TV's identifier from `devicectl list devices` JSON."""
    tvs = [d for d in listing.get("result", {}).get("devices", [])
           if d.get("hardwareProperties", {}).get("deviceType") == "appleTV"
           # "physical" once connected; missing on a freshly paired TV. Simulators say "simulated".
           and (d.get("hardwareProperties", {}).get("reality") or "physical") == "physical"]
    if not tvs:
        raise ScreenError(f"{HOST} isn't paired with the Apple TV for screenshots: run "
                          f"`xcrun devicectl manage pair` on {HOST} (a PIN shows on the TV).")
    return tvs[0]["identifier"]


async def screenshot(run=_run) -> bytes:
    """A JPEG of what the Apple TV shows, 1280 wide."""
    with tempfile.TemporaryDirectory() as tmp:
        listing_path = os.path.join(tmp, "devices.json")
        code, out = await run("xcrun", "devicectl", "list", "devices", "--quiet", "--json-output", listing_path)
        try:
            with open(listing_path) as f:
                listing = json.load(f)
        except (OSError, ValueError):
            raise ScreenError(f"Couldn't list Xcode devices: {out.strip()[-300:]}")
        tv = pick_tv(listing)
        png, jpg = os.path.join(tmp, "tv.png"), os.path.join(tmp, "tv.jpg")
        code, out = await run("xcrun", "devicectl", "device", "capture", "screenshot", "--device", tv,
                              "--destination", png)
        if code != 0 or not os.path.exists(png):
            if "tunnel" in out or "could not be established" in out:
                raise ScreenError("Couldn't connect to the Apple TV for a screenshot. Is it awake? "
                                  "(Developer Mode must be on: Settings → Privacy & Security.)")
            raise ScreenError(f"The screenshot failed: {out.strip()[-300:]}")
        code, out = await run("sips", "-Z", str(WIDTH), "-s", "format", "jpeg", png, "--out", jpg)
        if code != 0 or not os.path.exists(jpg):
            raise ScreenError(f"Couldn't shrink the screenshot: {out.strip()[-300:]}")
        with open(jpg, "rb") as f:
            return f.read()
