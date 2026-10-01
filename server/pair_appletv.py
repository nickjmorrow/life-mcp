"""One-time Apple TV pairing for the tv_* tools.

    uv run pair_appletv.py <pin-file-prefix>

For each protocol the TV needs (Companion: apps/power/remote; AirPlay: playback) it starts
pairing, the TV shows a 4-digit PIN, and this waits for <prefix>.companion / <prefix>.airplay to
be written with it (a file older than the pairing is ignored). Credentials go to
~/.config/life-mcp/appletv.json (mode 600), saved after each protocol so a later failure
doesn't lose an earlier pairing.
"""
import asyncio
import json
import os
import sys
import time

import pyatv
from pyatv.const import Protocol
import private

TV: str | None = private.get("apple_tv_ip")  # asked for at run time if the private config has none
OUT = os.path.expanduser("~/.config/life-mcp/appletv.json")
WAIT_S = 900


def _fresh_pin(path: str, since: float, wait_s: float | None = None) -> str | None:
    """The PIN written to path after `since`, or None if none arrives in time."""
    deadline = time.monotonic() + (WAIT_S if wait_s is None else wait_s)
    while time.monotonic() < deadline:
        try:
            if os.path.getmtime(path) >= since:
                text = open(path).read().strip()
                if text:
                    return text
        except OSError:
            pass
        time.sleep(1)
    return None


def _load() -> dict:
    if not os.path.exists(OUT):
        return {}
    try:
        with open(OUT) as f:
            return json.load(f)
    except ValueError:
        sys.exit(f"{OUT} isn't valid JSON; move it aside and pair again.")


def _save(creds: dict) -> None:
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    fd = os.open(OUT, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)  # the mode above only applies when the file is new
    with os.fdopen(fd, "w") as f:
        json.dump(creds, f)


async def main(prefix: str) -> None:
    loop = asyncio.get_running_loop()
    creds = _load()
    host = TV or input("Apple TV IP address: ").strip()
    confs = await pyatv.scan(loop, hosts=[host], timeout=5)
    if not confs:
        sys.exit("The Apple TV isn't answering. Is it on?")
    conf = confs[0]
    creds["identifier"], creds["all_identifiers"] = conf.identifier, list(conf.all_identifiers)
    _save(creds)
    for proto in (Protocol.Companion, Protocol.AirPlay):
        if proto.name in creds:
            print(f"{proto.name}: already paired", flush=True)
            continue
        pairing = await pyatv.pair(conf, proto, loop, name="Life")
        try:
            started = time.time()
            await pairing.begin()
            if pairing.device_provides_pin:
                path = f"{prefix}.{proto.name.lower()}"
                print(f"{proto.name}: a PIN is on the TV; waiting for {path}", flush=True)
                pin = await asyncio.to_thread(_fresh_pin, path, started)
                if pin is None:
                    sys.exit(f"No PIN arrived for {proto.name}; run this again.")
                if not (pin.isdigit() and len(pin) == 4):
                    sys.exit(f"'{pin}' isn't a 4-digit PIN; run this again.")
                pairing.pin(int(pin))
            else:
                pairing.pin(1111)  # we choose the PIN; pyatv sends it and the TV accepts it
            try:
                await pairing.finish()
            except Exception as e:
                sys.exit(f"{proto.name} pairing failed ({e}); earlier pairings are saved. Run this again.")
            if not pairing.has_paired:
                sys.exit(f"{proto.name} pairing failed; earlier pairings are saved. Run this again.")
            creds[proto.name] = pairing.service.credentials
            _save(creds)
            print(f"{proto.name}: paired and saved", flush=True)
        finally:
            await pairing.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
