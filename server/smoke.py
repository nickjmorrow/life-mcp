"""One read-only check per tool group. Exit 1 if any fail. Usage: uv run smoke.py [--expect-connector] [group ...]

--expect-connector (the server Mac's deploy): the private check fails when no connector is running on this Mac."""
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


EXPECT_CONNECTOR = False   # --expect-connector: this Mac must be running the connector (the server Mac's deploy)


def _unsaid(e: Exception) -> RuntimeError:
    return RuntimeError(f"the running connector hasn't said which private folder it reads ({e!r})")


def _running_record():
    """The running connector's record (server.RUNNING_FILE) when its pid is alive, else None. A record that can't be
    read is an error: a connector may be running and not saying what it reads."""
    import server
    try:
        text = server.RUNNING_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as e:
        raise _unsaid(e) from None
    try:
        running = json.loads(text)
        pid = running["pid"]
        if type(pid) is not int or pid <= 0:
            raise ValueError(f"pid {pid!r}")
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass
    except (ValueError, KeyError, TypeError, OverflowError) as e:
        raise _unsaid(e) from None
    return running


def check_private():
    """On a Mac with the harness's live tree (private.LIVE), the private folder must be that tree: always in this
    process (run as the deploy runs it, after private-env.sh), and in the running connector when this Mac runs one
    (it says which folder it reads in server.RUNNING_FILE when it starts): a connector reading the checkout would serve
    skills, rules and settings nobody approved. A Mac with a live tree but no running connector (no record, or the
    record's pid has exited) checks only this process and says "no connector here". With --expect-connector (the
    server Mac's deploy) no running connector is a failure, live tree or not, so a crashed connector can't pass."""
    import private
    live = private.LIVE.resolve() if private.LIVE.is_dir() else None
    if live is None and not EXPECT_CONNECTOR:
        return None
    if live is not None and private.DIR.resolve() != live:
        raise RuntimeError(f"this check reads {private.DIR}, not the live tree {live} (source private-env.sh)")
    running = _running_record()
    if running is None:
        if EXPECT_CONNECTOR:
            raise RuntimeError("no connector is running on this Mac, and --expect-connector says one should be "
                               "(no running.json, or its pid has exited)")
        return "no connector here"
    if live is None:
        return None
    try:
        folder = Path(running["private_dir"])
    except (KeyError, TypeError) as e:
        raise _unsaid(e) from None
    if folder.resolve() != live:
        raise RuntimeError(f"the running connector reads {folder}, not the live tree {live}")
    return None


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
                note = None
                if check:
                    note = check()
                else:
                    await call(client, tool, args)
                note = f"; {note}" if isinstance(note, str) else ""
                print(f"ok    {group:12} {tool} ({time.time() - start:.1f} s{note})")
            except Exception as e:  # report and keep going
                failed = 1
                print(f"FAIL  {group:12} {tool}: {e}")
    return failed


def run(groups):
    return asyncio.run(run_checks(groups))


def main(argv):
    """smoke.py [--expect-connector] [group ...]"""
    global EXPECT_CONNECTOR
    args = list(argv)
    expect = "--expect-connector" in args
    groups = [a for a in args if a != "--expect-connector"]
    before, EXPECT_CONNECTOR = EXPECT_CONNECTOR, expect
    try:
        return run(groups)
    finally:
        EXPECT_CONNECTOR = before


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
