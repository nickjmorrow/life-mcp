"""One read-only check per tool group. Exit 1 if any fail. Usage: uv run smoke.py [group ...]"""
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from fastmcp import Client

# One per tool group in server.GROUPS (tests check none is missing), plus the Logseq tools themselves.
CHECKS = {
    "logseq": ("list_pages", {}),
    "health": ("health_summary", {}),
    "people": ("people_keep_in_touch", {}),
    "cards": ("cards_status", {}),
    "hue": ("hue_status", {}),
    "eight_sleep": ("eight_sleep_connection_status", {}),
    "eight_sleep_extras": ("eight_sleep_pod_status", {}),
    "reminders": ("reminders_list_lists", {}),
    "hevy": ("hevy_api", {"method": "GET", "path": "/v1/workouts/count"}),
    "music": ("music_list_speakers", {}),
    "music_library": ("music_status", {}),
    "tv": ("tv_status", {}),
    "home": ("home_list", {}),
    "skills": ("skill_list", {}),
}


def check_memory():
    """Memory is files on this Mac, so read the topic list straight from the store. A smoke run never calls
    memory_recall: that would count as a chat in the usage log."""
    import memory_mcp
    memory_mcp.store().topics()


def check_proposals():
    """The proposal tools can't send without their token, so check that its file is there and has something in it.
    Nothing is posted: a health check must not leave a proposal on the approvals page."""
    import proposals_mcp
    proposals_mcp.read_token()


def check_private():
    """On a Mac with the harness's live tree (private.LIVE), the private folder must be that tree, both in this process
    (run as the deploy runs it, after private-env.sh) and in the running connector, which says so in
    server.RUNNING_FILE when it starts: a connector reading the checkout would serve skills, rules and settings nobody
    approved. A Mac without a live tree has nothing to check."""
    import private
    import server
    if not private.LIVE.is_dir():
        return
    live = private.LIVE.resolve()
    if private.DIR.resolve() != live:
        raise RuntimeError(f"this check reads {private.DIR}, not the live tree {live} (source private-env.sh)")
    try:
        running = json.loads(server.RUNNING_FILE.read_text(encoding="utf-8"))
        pid, folder = running["pid"], Path(running["private_dir"])
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise RuntimeError(f"the running connector hasn't said which private folder it reads ({e!r})") from None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        raise RuntimeError(f"the connector that wrote {server.RUNNING_FILE.name} (pid {pid}) isn't running") from None
    except PermissionError:
        pass
    if folder.resolve() != live:
        raise RuntimeError(f"the running connector reads {folder}, not the live tree {live}")


# Groups checked without a tool call: what the check calls, and the function that does it.
LOCAL_CHECKS = {
    "memory": ("store().topics()", check_memory),
    "proposals": ("token file", check_proposals),
    "private": ("live tree", check_private),
}


_server = None


def load_server():
    """Import server.py and mount every tool group, as its __main__ does (without auth or HTTP). Like the real server,
    the client's start writes server.TOOLS_FILE, which is pointed at a temp folder here: the running server's own list
    is the one the harness reads."""
    global _server
    if _server is None:
        import server  # imported late so the tests run without the Logseq app
        server.TOOLS_FILE = Path(tempfile.mkdtemp(prefix="smoke-")) / "tools.json"
        server.mount_all()
        _server = server
    return _server


async def call(client, tool, args):
    return await client.call_tool(tool, args)


async def run_checks(groups):
    """Every check in one event loop: proxied servers keep their session on the loop that opened it."""
    failed = 0
    async with Client(load_server().mcp) as client:
        for group in groups or [*CHECKS, *LOCAL_CHECKS]:
            check, args = None, None
            if group in LOCAL_CHECKS:
                tool, check = LOCAL_CHECKS[group]
            else:
                tool, args = CHECKS[group]
            start = time.time()
            try:
                if check:
                    check()
                else:
                    await call(client, tool, args)
                print(f"ok    {group:12} {tool} ({time.time() - start:.1f} s)")
            except Exception as e:  # report and keep going
                failed = 1
                print(f"FAIL  {group:12} {tool}: {e}")
    return failed


def run(groups):
    return asyncio.run(run_checks(groups))


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
