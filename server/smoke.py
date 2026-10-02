"""One read-only check per tool group. Exit 1 if any fail. Usage: uv run smoke.py [group ...]"""
import asyncio
import sys
import time

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


# Groups checked without a tool call: what the check calls, and the function that does it.
LOCAL_CHECKS = {
    "memory": ("store().topics()", check_memory),
}


_server = None


def load_server():
    """Import server.py and mount every tool group, as its __main__ does (without auth or HTTP)."""
    global _server
    if _server is None:
        import server  # imported late so the tests run without the Logseq app
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
