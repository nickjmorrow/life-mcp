"""One JSON line per connector call that shows a skill being used, for the daily lessons job's skill usage review
(the lessons job in his agent harness). Only the tool, the skill and the file path are kept, never the chat.

memory_recall is logged too: claude.ai calls it first in every chat, so it counts the chats a skill could have
been used in. Logging never breaks a tool call.
"""
import datetime as dt
import json
import os
from pathlib import Path

PATH = Path.home() / "Library" / "Application Support" / "life-mcp" / "usage" / "connector.jsonl"


def record(tool: str, skill: str | None = None, path: str | None = None) -> None:
    entry = {"ts": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "tool": tool}
    if skill:
        entry["skill"] = skill
    if path:
        entry["path"] = path
    try:
        PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(PATH, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass
