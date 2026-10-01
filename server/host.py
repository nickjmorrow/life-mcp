"""The Mac this server runs on: its name for messages (its computer name) and its time zone.

The private config's `timezone` (or the TZ variable) overrides the zone, which otherwise comes from the
Mac's own setting (/etc/localtime).
"""
import os
import platform
from pathlib import Path

import private

NAME = platform.node().split(".")[0] or "this Mac"


def _system_zone() -> str:
    try:
        return str(Path("/etc/localtime").resolve()).split("zoneinfo/", 1)[1]
    except (OSError, IndexError):
        return "UTC"


TIMEZONE: str = private.get("timezone") or os.environ.get("TZ") or _system_zone()
