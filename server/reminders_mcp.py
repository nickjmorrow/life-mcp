"""MCP tools for Apple Reminders on the server Mac, through reminders-cli (EventKit).

server.py mounts this server (namespace "reminders"). Reminders sync over iCloud,
so these are the same lists as on Nicholas's phone.

Items are addressed by id (reminders-cli's externalId). Ids stay put when other
items are added or completed; list positions don't.
"""
import asyncio
import json
import re
from typing import Annotated, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field
from host import NAME as HOST

REMINDERS = "/opt/homebrew/bin/reminders"  # brew: keith/formulae/reminders-cli
TIMEOUT_S = 20

mcp = FastMCP("Reminders")


async def run(*args: str) -> str:
    """Run `reminders <args>` and return stdout. Failures become ToolError."""
    try:
        proc = await asyncio.create_subprocess_exec(
            REMINDERS, *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        raise ToolError(f"reminders-cli isn't installed on {HOST} "
                        "(brew install keith/formulae/reminders-cli).")
    try:
        out, err = await asyncio.wait_for(proc.communicate(), TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        raise ToolError("Reminders didn't answer in time.")
    out, err = out.decode().strip(), err.decode().strip()
    if "grant reminders access" in out + err:
        raise ToolError(f"{HOST} doesn't have access to Reminders. On the Mac: "
                        "System Settings → Privacy & Security → Reminders.")
    if proc.returncode:
        raise ToolError(err or out or f"reminders exited with {proc.returncode}")
    return out


def _key(name: str) -> str:
    """"The Groceries list" → "groceries"."""
    words = re.sub(r"[^a-z0-9 ]+", "", name.lower()).split()
    words = [w for w in words if w not in ("the", "my", "list")]
    return "".join(words)


async def _list(name: str) -> str:
    """The real list name that `name` means: exact, then loose match."""
    lists = [l for l in (await run("show-lists")).splitlines() if l.strip()]
    if name in lists:
        return name
    found = [l for l in lists if _key(l) == _key(name)]
    if len(found) == 1:
        return found[0]
    raise ToolError(f"No Reminders list called '{name}'. Lists: {', '.join(lists)}.")


PRIORITY = {1: "high", 5: "medium", 9: "low"}  # EventKit: 1–4 high, 5 medium, 6–9 low


def _describe(item: dict, with_list: bool = False) -> str:
    extras = []
    if item.get("isCompleted"):
        extras.append("done")
    if item.get("dueDate"):
        extras.append(f"due {item['dueDate']}")
    p = item.get("priority") or 0
    if p:
        extras.append(f"{PRIORITY[1 if p < 5 else 5 if p == 5 else 9]} priority")
    if item.get("notes"):
        extras.append(f"notes: {item['notes']}")
    line = item["title"] + (f" ({', '.join(extras)})" if extras else "") + f" [id {item['externalId']}]"
    return line + (f" ({item['list']})" if with_list else "")


ListName = Annotated[str, Field(description="Reminders list, e.g. \"Groceries\" (loose names are fine)")]
ItemId = Annotated[str, Field(description="The reminder's id, as shown by show or due")]
Due = Annotated[str | None, Field(description="When it's due, e.g. 'tomorrow 9am', 'friday', '2026-10-01'")]
Priority = Annotated[Literal["none", "low", "medium", "high"] | None, Field(description="Priority")]


@mcp.tool
async def list_lists() -> str:
    """All of Nicholas's Reminders lists."""
    return await run("show-lists")


@mcp.tool
async def show(list: ListName,
               include_completed: Annotated[bool, Field(description="Also show completed items")] = False) -> str:
    """The reminders on one list, with their ids."""
    name = await _list(list)
    args = ["show", "--format", "json"] + (["--include-completed"] if include_completed else [])
    items = json.loads(await run(*args, "--", name) or "[]")
    return "\n".join(_describe(i) for i in items) or f"{name} is empty."


@mcp.tool
async def due(date: Annotated[str, Field(description="'today', 'tomorrow', 'friday', '2026-10-01'...")] = "today",
              include_overdue: Annotated[bool, Field(description="Also items due before that date")] = True) -> str:
    """Reminders due on or by a date, across every list."""
    args = ["show-all", "--format", "json", f"--due-date={date}"] + (["--include-overdue"] if include_overdue else [])
    items = json.loads(await run(*args) or "[]")
    return "\n".join(_describe(i, with_list=True) for i in items) or f"Nothing due {date}."


@mcp.tool
async def add(list: ListName,
              title: Annotated[str, Field(description="What to remember, e.g. 'oat milk'")],
              notes: Annotated[str | None, Field(description="Extra detail")] = None,
              due: Due = None, priority: Priority = None) -> str:
    """Add a reminder to a list."""
    name = await _list(list)
    args = ["add", "--format", "json"]
    if notes:
        args += [f"--notes={notes}"]
    if due:
        args += [f"--due-date={due}"]
    if priority:
        args += [f"--priority={priority}"]
    item = json.loads(await run(*args, "--", name, title))
    return f"Added '{item['title']}' to {name} [id {item['externalId']}]"


@mcp.tool
async def complete(list: ListName, id: ItemId) -> str:
    """Mark a reminder done (e.g. bought it)."""
    return await run("complete", "--", await _list(list), id)


@mcp.tool
async def uncomplete(list: ListName, id: ItemId) -> str:
    """Mark a completed reminder as not done again."""
    return await run("uncomplete", "--", await _list(list), id)


@mcp.tool
async def edit(list: ListName, id: ItemId,
               title: Annotated[str | None, Field(description="New text")] = None,
               notes: Annotated[str | None, Field(description="New notes (replaces the old ones)")] = None,
               due: Due = None,
               clear_due: Annotated[bool, Field(description="Remove the due date")] = False) -> str:
    """Change a reminder's text, notes or due date."""
    if due and clear_due:
        raise ToolError("Give a new due date or clear it, not both.")
    if not (title or notes or due or clear_due):
        raise ToolError("Nothing to change. Give title, notes, due or clear_due.")
    args = ["edit"]
    if notes:
        args += [f"--notes={notes}"]
    if due:
        args += [f"--due-date={due}"]
    if clear_due:
        args += ["--clear-due-date"]
    return await run(*args, "--", await _list(list), id, *([title] if title else []))


@mcp.tool
async def delete(list: ListName, id: ItemId) -> str:
    """Delete a reminder for good. Only after Nicholas has confirmed; to tick something off, use complete."""
    return await run("delete", "--", await _list(list), id)


@mcp.tool
async def create_list(name: Annotated[str, Field(description="Name of the new list")]) -> str:
    """Create a new Reminders list."""
    return await run("new-list", "--", name)


if __name__ == "__main__":
    mcp.run(show_banner=False)
