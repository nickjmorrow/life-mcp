"""rule_edit, change_preview, ship_it and drop_change: change how Claude behaves for this user, through his agent harness.

How Claude behaves (his rules, skills and the harness's code) lives in his private harness repo, and only reaches the
main branch (what every Claude loads) with his approval. These tools don't touch the repo themselves: each runs the
harness's own command (the private config's `harness.command`, a list like ["/path/to/harness-command"]) with one
sub-command and the arguments as JSON on stdin, and returns the `text` the command prints as JSON.

    rule_edit        sub-command `edit`: commit the edit on the dev branch; it joins the open changes he merges
    change_preview   sub-command `preview`: show the diff of an edit and its diff_hash (changes nothing)
    ship_it          sub-command `ship`: open a one-change pull request from main and merge it, only for the exact
                     change just previewed, and only after he said "ship it" and allowed the tool in the app's prompt
    drop_change      sub-command `drop`: take a change off dev and record why (a `Declined:` commit)

The command answers with exit 0 and {"text": "..."}, or exit 2 and {"error": "..."} for a refusal (a ToolError with that
reason). Anything else, a missing command or a timeout is a ToolError that names this Mac and never repeats what the
command printed.
"""
import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

import host
import private

TIMEOUT_S = 30  # a local command answers at once; a stuck one must not hold a chat
SHIP_TIMEOUT_S = 660  # ship waits for the pull request's checks (up to 10 minutes) before it can merge

Surface = Literal["phone", "laptop", "hob", "job"]


def command() -> list[str]:
    """The harness command from the private config, or a ToolError when this Mac has none that can run."""
    named = private.get("harness.command")
    if not isinstance(named, list) or not named or not all(isinstance(p, str) and p for p in named):
        raise ToolError(f"Changes aren't set up on {host.NAME}.")
    first = Path(named[0]).expanduser()
    found = str(first) if "/" in named[0] else shutil.which(named[0])
    if not found or not os.access(found, os.X_OK):
        raise ToolError(f"Changes aren't set up on {host.NAME}.")
    return [found, *(str(Path(p).expanduser()) if p.startswith("~/") else p for p in named[1:])]   # no shell: expand ~ here


def _run(sub: str, args: dict[str, Any], timeout: int) -> str:
    argv = [*command(), sub]
    try:
        done = subprocess.run(argv, input=json.dumps(args), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ToolError(f"The change command on {host.NAME} didn't answer in {timeout} seconds, so {sub} may or may not "
                        "have happened: look at the repo before trying again.") from None
    except OSError:
        raise ToolError(f"The change command on {host.NAME} couldn't be started.") from None
    try:
        reply = json.loads(done.stdout)
    except ValueError:
        reply = None
    if done.returncode == 2 and isinstance(reply, dict) and isinstance(reply.get("error"), str) and reply["error"].strip():
        raise ToolError(reply["error"].strip()[:1000])
    text = reply.get("text") if isinstance(reply, dict) else None
    if done.returncode != 0 or not isinstance(text, str) or not text.strip():
        raise ToolError(f"The change command on {host.NAME} failed (exit {done.returncode}) without a usable answer.")
    return text.strip()


async def _call(sub: str, args: dict[str, Any], timeout: int | None = None) -> str:
    return await asyncio.to_thread(_run, sub, {k: v for k, v in args.items() if v is not None}, timeout or TIMEOUT_S)


Path_ = Annotated[str, Field(description="The file to change, relative to his agent's setup, like RULES.md or skills/<name>/SKILL.md")]
New = Annotated[str, Field(description="The text to put in")]
Old = Annotated[str | None, Field(description="Text to replace with new; it must appear exactly once in the file")]
After = Annotated[str | None, Field(description="The whole line to insert new after, instead of replacing")]


def build() -> FastMCP:
    mcp = FastMCP("Changes")

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False})
    async def rule_edit(
        path: Path_,
        new: New,
        why: Annotated[str, Field(description="One plain sentence on why, shown to him beside the change")],
        surface: Annotated[Surface, Field(description="Where you are: phone (the Claude app or the web), laptop (Claude Code), hob (voice) or job (a scheduled job)")],
        old: Old = None,
        after: After = None,
        quote: Annotated[str | None, Field(description="His exact words that led to this, if he said them in this conversation; otherwise leave out")] = None,
        untrusted_content_seen: Annotated[bool, Field(description="True if this conversation has read web pages, email, other people's messages or other outside text (his review looks closer at such changes)")] = False,
    ) -> str:
        """Edit how Claude behaves for this user: his rules or one of his skills (a Markdown file in his agent's setup).
        The edit is saved on the dev branch and joins the open changes he reviews and merges; nothing changes for other
        chats until he does. Use it when he states a lasting rule ("from now on...") or agrees that a correction should
        become one; facts about him go to memory_save, not here. Give the file's path, the new text and why. To replace
        text, pass old (it must appear exactly once in the file); to insert after a line, pass after (the whole line);
        with neither, new is added at the end of the file. Changes he is looking at with you right now go through
        change_preview and ship_it instead."""
        return await _call("edit", {"path": path, "new": new, "why": why, "surface": surface, "old": old, "after": after,
                                    "quote": quote, "untrusted": untrusted_content_seen})

    @mcp.tool(annotations={"readOnlyHint": True})
    async def change_preview(path: Path_, new: New, old: Old = None, after: After = None) -> str:
        """Show the exact change an edit would make (the diff against main) and its diff_hash, without changing anything.
        Use it to show him a change you are about to ship, before he says "ship it"."""
        return await _call("preview", {"path": path, "new": new, "old": old, "after": after})

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True})
    async def ship_it(
        path: Path_,
        new: New,
        title: Annotated[str, Field(description="A short plain title for the change")],
        diff_hash: Annotated[str, Field(description="The diff_hash change_preview returned for exactly this change")],
        old: Old = None,
        after: After = None,
    ) -> str:
        """Put a change on main now: it opens a one-change pull request and merges it with his approval, so every Claude has
        it within a minute. Call it only after he said "ship it" about the change you just showed him with
        change_preview, passing that preview's diff_hash. It refuses if the change differs from the previewed one, or
        doesn't apply to main. His app asks him to allow this call and shows the change first."""
        return await _call("ship", {"path": path, "new": new, "title": title, "diff_hash": diff_hash, "old": old,
                                    "after": after}, SHIP_TIMEOUT_S)

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False})
    async def drop_change(
        commit: Annotated[str, Field(description="The commit on dev to take off (a sha from the open changes)")],
        change: Annotated[str, Field(description="The change, in a few plain words, as it should read in the record")],
        reason: Annotated[str, Field(description="Why he doesn't want it, in his words if he gave them")],
    ) -> str:
        """Take a change off the open changes because he doesn't want it, and record why (a "Declined:" commit) so the
        nightly and weekly jobs don't suggest it again."""
        return await _call("drop", {"commit": commit, "change": change, "reason": reason})

    return mcp
