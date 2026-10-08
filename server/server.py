"""The Life connector: every tool group in GROUPS (shared memory, changes, skills, health, people, Grimoire, Hue,
Eight Sleep, Reminders, Hevy, Music, the Apple TV and Apple Home), mounted by mount_all at the bottom.

claude.ai -> Tailscale Funnel (https://$PUBLIC_URL) -> this server on 127.0.0.1:8765.

Only one GitHub account can use it. Everything else gets a 401 at token
verification, before any MCP request is handled.

`uv run server.py --stdio` serves the same tools locally with no auth.
"""
import asyncio
import dataclasses
import datetime as dt
import importlib
import json
import os
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

from fastmcp import FastMCP
from fastmcp.server.auth.providers.github import GitHubProvider
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware, MiddlewareContext
import usage_log

# Numeric IDs never change or get reused; logins can be renamed and re-registered.
# The one GitHub account (numeric user id) allowed in; set in ~/.zshrc.local. Empty = nobody.
ALLOWED_GITHUB_ID = os.environ.get("LIFE_MCP_GITHUB_ID", "").strip()
HOST = "127.0.0.1"  # never listen on the LAN; Funnel connects over loopback
PORT = 8765

mcp = FastMCP("Life")


# ── Auth (remote only) ───────────────────────────────────────────────────


def is_me(token) -> bool:
    return bool(token) and bool(ALLOWED_GITHUB_ID) and str((token.claims or {}).get("sub")) == ALLOWED_GITHUB_ID


class OnlyMe(Middleware):
    """Second layer: re-check the caller on every MCP request."""

    async def on_request(self, context: MiddlewareContext, call_next):
        if not is_me(get_access_token()):
            raise PermissionError("Not authorized")
        return await call_next(context)


class ChatLog(Middleware):
    """Log each tool call's name (usage_log.record_call) for the lessons job's count of chats that skip memory_recall."""

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        try:
            usage_log.record_call(context.message.name)
        except Exception as e:  # logging never breaks a call
            print(f"Call not logged: {e!r}", file=sys.stderr)
        return await call_next(context)


# claude.ai often finds a tool by searching and never calls memory_recall, so it never sees the user's rules and core facts.
# A tool's description is the one thing it reads before calling the tool, so every tool's description ends with this.
RECALL_REMINDER = (" If you haven't called memory_recall in this conversation, call it first: it returns the user's rules"
                   " and core memory.")
RECALL_EXEMPT = frozenset({"memory_recall", "memory_save", "memory_update"})  # the memory tools are that call or follow it


class RecallReminder(Middleware):
    """End every tool's description with RECALL_REMINDER, except the memory tools'. Each listing gets copies, so the
    registered tools stay as written and listing again never stacks the sentence."""

    async def on_list_tools(self, context: MiddlewareContext, call_next):
        tools = await call_next(context)
        return [t if t.name in RECALL_EXEMPT
                else t.model_copy(update={"description": (t.description or "").rstrip() + RECALL_REMINDER})
                for t in tools]


def github_auth() -> GitHubProvider:
    if not ALLOWED_GITHUB_ID.isdigit():
        raise RuntimeError("LIFE_MCP_GITHUB_ID (the allowed GitHub user's numeric id) isn't set")
    auth = GitHubProvider(
        client_id=os.environ["GITHUB_CLIENT_ID"],
        client_secret=os.environ["GITHUB_CLIENT_SECRET"],
        base_url=os.environ["PUBLIC_URL"],
        # Read-only profile access; the default "user" scope can also edit the profile.
        required_scopes=["read:user"],
        # Only Claude's OAuth callbacks may receive codes from this server.
        allowed_client_redirect_uris=[
            "https://claude.ai/api/mcp/auth_callback",
            "https://claude.com/api/mcp/auth_callback",
        ],
        jwt_signing_key=os.environ["LIFE_MCP_JWT_KEY"],
        cache_ttl_seconds=300,
    )
    # First layer: a GitHub token for anyone else fails verification (-> 401).
    verify = auth._token_validator.verify_token

    async def verify_only_me(token: str):
        result = await verify(token)
        return result if is_me(result) else None

    auth._token_validator.verify_token = verify_only_me
    return auth


# ── Tool groups ──────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class Group:
    """One tool group the connector serves, mounted by mount_all."""
    label: str                    # its name in the log and in smoke.py
    module: str                   # what to import
    namespace: str | None = None  # prefix FastMCP adds to tool names; None when the tools are named in full
    # Added to the connector's instructions: text, a function of the module, or None for the module's
    # INSTRUCTIONS (or the mounted server's own instructions).
    instructions: str | Callable[[Any], str] | None = None
    build: Callable[[Any], FastMCP] = lambda m: m.mcp  # the module -> its FastMCP server
    path: Path | None = None      # a folder to put first on sys.path before importing
    first: bool = False           # its instructions go at the very start (clients cut long ones)


GROUPS: tuple[Group, ...] = (
    Group("memory", "memory_mcp", build=lambda m: m.build()),
    Group("changes", "changes_mcp", build=lambda m: m.build()),
    Group("health", "health_mcp", build=lambda m: m.build()),
    Group("people", "people_mcp", build=lambda m: m.build()),
    Group("grimoire", "grimoire_mcp", "grimoire", build=lambda m: m.build()),
    # claude.ai only connects on port 443, and Funnel's 443 is this server, so Hue is mounted here too.
    Group("hue", "hue_mcp", "hue", path=Path(__file__).resolve().parent.parent / "hue"),
    Group("eight_sleep", "eightsleep_proxy",
          instructions=" The eight_sleep_* tools read sleep data from the Eight Sleep bed and control it."),
    Group("eight_sleep_extras", "eightsleep_mcp",
          instructions=" For Eight Sleep, prefer eight_sleep_list_alarms / update_alarm / skip_next_alarm for"
                       " alarms, and set_sleep_levels / shift_levels for temperature."),
    Group("reminders", "reminders_mcp", "reminders",
          instructions=" The reminders_* tools are Nicholas's Apple Reminders."),
    Group("hevy", "hevy_mcp", "hevy",
          instructions=" hevy_api calls Hevy's REST API (his workouts and routines) with the key added on"
                       " the server: never ask for or show the key."),
    Group("music", "music_mcp",
          instructions=" The music_* tools play his Apple Music library on AirPlay speakers (HomePods, the"
                       " Apple TV) and control whatever they're playing."),
    Group("music_library", "music_lib_mcp",
          instructions=" music_recent/top/taste show what he listens to (\"songs I just skipped\" = music_recent"
                       " kind=skipped, or last_skipped=N on the favorite and playlist tools); music_set_favorite and"
                       " the playlist tools change his library; for new music read music_taste, then"
                       " music_search_catalog / music_artist / music_recommendations."),
    Group("tv", "tv_mcp", instructions=" The tv_* tools control the Apple TV (power, apps, playback, remote)."),
    Group("home", "home_mcp",
          instructions=" The home_* tools control Apple Home (accessories, scenes, automations, motion)"
                       " through the Life Home app; HomePods and the Apple TV are music_* and tv_*."),
    Group("skills", "skills_mcp", build=lambda m: m.build(), first=True,
          instructions=lambda m: m.instructions(m.load_all(m.SKILLS_DIR))),
)


def group(label: str) -> Group:
    return next(g for g in GROUPS if g.label == label)


def mount(g: Group, target: FastMCP | None = None) -> bool:
    """Import a group, mount its tools on target (default: this server) and add its instructions.
    A group that fails to load is logged and skipped, so one broken integration can't take down the rest."""
    target = target or mcp
    try:
        if g.path and str(g.path) not in sys.path:
            sys.path.insert(0, str(g.path))  # first, so nothing shadows its modules
        module = importlib.import_module(g.module)
        server = g.build(module)
        if callable(g.instructions):
            text = g.instructions(module)
        elif g.instructions is not None:
            text = g.instructions
        else:
            text = getattr(module, "INSTRUCTIONS", None) or server.instructions or ""
    except Exception as e:
        print(f"{g.label} tools not loaded: {e!r}", file=sys.stderr)
        return False
    target.mount(server, namespace=g.namespace)
    text = text.strip()
    if text:
        target.instructions = (f"{text} {target.instructions or ''}" if g.first
                               else f"{target.instructions or ''} {text}").strip()
    return True


# Every tool name the server exposes, sorted, written when the server starts. The private harness's drift check reads
# it to find a rule or a skill that names a tool that isn't there.
TOOLS_FILE = Path.home() / "Library" / "Application Support" / "life-mcp" / "tools.json"


# Written when the HTTP server starts: its pid, when it started and the private folder it reads (private.DIR), so a
# check outside it (smoke.py, run by the deploy) can confirm that the running connector serves the folder it should.
RUNNING_FILE = Path.home() / "Library" / "Application Support" / "life-mcp" / "running.json"


def write_running() -> None:
    """Say in RUNNING_FILE which private folder this process reads. A failure is a log line, never a stop."""
    import private
    record = {"pid": os.getpid(), "private_dir": str(private.DIR),
              "started_at": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    try:
        _write_private(RUNNING_FILE, json.dumps(record) + "\n")
    except Exception as e:
        print(f"{RUNNING_FILE.name} not written: {e!r}", file=sys.stderr)


def _write_private(path: Path, text: str) -> None:
    """Replace a file whole (600, in a 700 folder): a reader sees the old text or the new, never half."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".", suffix=".tmp")  # created 600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


async def write_tool_list(target: FastMCP) -> list[str] | None:
    """Save the name of every tool target exposes in TOOLS_FILE, and return them. If they can't be listed or saved this
    logs a line and returns None: the connector doesn't depend on the file."""
    try:
        names = sorted({t.name for t in await target.list_tools(run_middleware=False)})
        _write_private(TOOLS_FILE, json.dumps(names, indent=2) + "\n")
    except Exception as e:
        print(f"Tool list not written: {e!r}", file=sys.stderr)
        return None
    return names


def tool_list_writer(target: FastMCP) -> FastMCP:
    """A server with no tools whose start writes TOOLS_FILE for target. FastMCP starts a mounted server's lifespan when
    the server it's mounted on starts, on that server's own event loop, and that is the one safe place to take the list:
    the Eight Sleep tools come through a proxy that keeps its connection to the npm server on the loop that opened it, so
    listing them on a loop of our own (asyncio.run inside mount_all) leaves the proxy failing with "Event loop is closed"
    for as long as the server runs, and drops its tools from every later listing (checked on FastMCP 4.0.10). The write
    is a background task, so a proxy that hangs can't hold up the server starting."""
    @asynccontextmanager
    async def lifespan(_server: FastMCP):
        writing = asyncio.create_task(write_tool_list(target))
        try:
            yield {}
        finally:
            writing.cancel()
            await asyncio.gather(writing, return_exceptions=True)
    return FastMCP("Tool list", lifespan=lifespan)


def mount_all(target: FastMCP | None = None, groups: tuple[Group, ...] = GROUPS) -> list[str]:
    """Mount every group (returns the labels that loaded), then tell the memory guard every tool name the server
    exposes, so memory can't be made to hold a command for any of them, and arrange for TOOLS_FILE to be written when
    the server starts."""
    target = target or mcp
    loaded = [g.label for g in groups if mount(g, target)]

    async def names():
        return [t.name for t in await target.list_tools()]
    try:
        import memory_mcp
        memory_mcp.set_tool_source(names)  # listed on first use, inside the running server
    except Exception as e:
        print(f"Memory guard not given the tool names: {e!r}", file=sys.stderr)
    target.mount(tool_list_writer(target))  # last, so that what it lists includes everything above
    return loaded


if __name__ == "__main__":
    mount_all()
    if "--stdio" in sys.argv:
        mcp.run(show_banner=False)
    else:
        mcp.auth = github_auth()
        mcp.add_middleware(OnlyMe())
        mcp.add_middleware(ChatLog())
        mcp.add_middleware(RecallReminder())
        write_running()
        mcp.run(transport="http", host=HOST, port=PORT)
