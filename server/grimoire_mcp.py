"""MCP tools for Grimoire, Nicholas's notes app (journals, pages, blocks, flashcards), through its `grim` command-line tool.

server.py mounts this server (namespace "grimoire"), so the tools are grimoire_get_page, grimoire_append and so on.
They work on the hub graph on the server Mac; the app on his Mac and phone syncs with it, so what Claude writes here shows up
there within seconds. Everything Claude writes is logged as author `claude` and can be undone (grimoire_undo_claude).

Blocks are addressed by their UUID (shown in square brackets in get_page output). Text goes in as Markdown bullets: two spaces
of indent per level, `[[Page]]` links, `#tags`, `TODO ` tasks.
"""
import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Annotated, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

READ = {"readOnlyHint": True}
WRITE = {"readOnlyHint": False, "destructiveHint": False}
DESTROY = {"readOnlyHint": False, "destructiveHint": True}

TIMEOUT_S = 60
MARKER = "\u0001"  # grim strips this; it keeps text that starts with "-" from being read as an option

INSTRUCTIONS = (
    " The grimoire_* tools are Nicholas's notes in Grimoire (journals, pages, flashcards). Read with grimoire_get_page /"
    " grimoire_search; add with grimoire_append (a bullet outline; 'today' is today's journal). Never change a journal's"
    " existing blocks: add new ones. Everything Claude writes can be undone with grimoire_undo_claude."
)

mcp = FastMCP("Grimoire", instructions=INSTRUCTIONS)


def graph_folder() -> str:
    return os.path.expanduser(os.environ.get("GRIMOIRE_GRAPH") or "~/Grimoire-hub")


def grim_bin() -> str:
    return os.environ.get("GRIM_BIN") or shutil.which("grim") or str(Path.home() / ".local" / "bin" / "grim")


def text_arg(value: str) -> str:
    return MARKER + value if value.startswith("-") else value


async def run(*args: str) -> str:
    """Run `grim <args> --graph … --json --author claude`; failures become ToolError."""
    argv = [grim_bin(), *args, "--graph", graph_folder(), "--json", "--author", "claude"]
    try:
        proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError:
        raise ToolError("grim isn't installed on this Mac (build it from the grimoire repo).")
    try:
        out, err = await asyncio.wait_for(proc.communicate(), TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        raise ToolError("grim didn't answer in time.")
    out, err = out.decode().strip(), err.decode().strip()
    if proc.returncode:
        raise ToolError(err or out or f"grim exited with {proc.returncode}")
    return out or "ok"


async def refuse_journal_block(block_id: str) -> None:
    """A journal's existing blocks are his raw thoughts: Claude adds new blocks, it doesn't change those."""
    safe = block_id.replace("'", "")
    out = await run("query", f"SELECT p.kind AS kind FROM blocks b JOIN pages p ON p.id = b.page_id WHERE b.id = '{safe}'")
    rows = json.loads(out)
    if rows and rows[0].get("kind") == "journal":
        raise ToolError("That block is on a journal page. Nicholas's journal entries stay as he wrote them: add a new block "
                        "with grimoire_append instead.")


# ── reads ────────────────────────────────────────────────────────────────

@mcp.tool(annotations=READ)
async def get_page(title: Annotated[str, Field(description="Page title (any case). A journal date like 2026-10-05 or 'today' also works.")]) -> str:
    """A page and its block tree. Block ids are UUIDs, used by the edit tools."""
    return await run("page", text_arg(title))


@mcp.tool(annotations=READ)
async def get_journal(date: Annotated[str, Field(description="2026-10-05, 'yesterday', 'oct 5th, 2026'…")]) -> str:
    """A journal page by date."""
    return await run("journal", date)


@mcp.tool(annotations=READ)
async def get_today() -> str:
    """Today's journal (page: null when nothing has been written today yet)."""
    return await run("today")


@mcp.tool(annotations=READ)
async def search(query: Annotated[str, Field(description="Words to find in page titles and block text; the last word matches as a prefix")],
                 limit: Annotated[int, Field(ge=1, le=200)] = 30) -> str:
    """Full-text search over page titles and blocks."""
    return await run("search", text_arg(query), "--limit", str(limit))


@mcp.tool(annotations=READ)
async def backlinks(title: str) -> str:
    """Blocks that link to a page, grouped by the page they're on."""
    return await run("backlinks", text_arg(title))


@mcp.tool(annotations=READ)
async def recent_pages(limit: Annotated[int, Field(ge=1, le=100)] = 20) -> str:
    """Recently changed pages."""
    return await run("recent", "--limit", str(limit))


@mcp.tool(annotations=READ)
async def favorites() -> str:
    """His favorite pages, in order."""
    return await run("favorites")


@mcp.tool(annotations=READ)
async def blocks_with_tag(tag: str) -> str:
    """Blocks tagged with #tag (or `tags:: [[tag]]`)."""
    return await run("tag", text_arg(tag))


@mcp.tool(annotations=READ)
async def blocks_with_property(key: str, value: str | None = None) -> str:
    """Blocks that have a `key:: value` property, optionally with that value."""
    return await run("prop", key, *([text_arg(value)] if value else []))


@mcp.tool(annotations=READ)
async def query(sql: Annotated[str, Field(description="A read-only SELECT. Tables: pages, blocks, links, tags, block_tags, properties, block_props, cards, reviews, ops")]) -> str:
    """Read-only SQL over the graph database, for questions the other tools can't answer."""
    return await run("query", text_arg(sql))


@mcp.tool(annotations=READ)
async def changes(since: Annotated[str, Field(description="ISO 8601 time or a duration like 2h, 30m, 1d")] = "1d") -> str:
    """What changed since a time, and who changed it (me, claude, import, sync)."""
    return await run("changes", "--since", since)


# ── writes ───────────────────────────────────────────────────────────────

@mcp.tool(annotations=WRITE)
async def append(target: Annotated[str, Field(description="Page title, a journal date, or 'today'. The page is created if it doesn't exist.")],
                 markdown: Annotated[str, Field(description="Markdown bullets; indent two spaces per level")]) -> str:
    """Add blocks to the end of a page or journal. Returns the new block ids."""
    return await run("append", text_arg(target), text_arg(markdown))


@mcp.tool(annotations=WRITE)
async def insert(markdown: str,
                 after: Annotated[str | None, Field(description="Block id to insert after (same level)")] = None,
                 parent: Annotated[str | None, Field(description="Block id to insert under (as its last child)")] = None,
                 page: Annotated[str | None, Field(description="Page title to add to the end of")] = None) -> str:
    """Insert blocks at a precise place. Give exactly one of after, parent, page."""
    if sum(x is not None for x in (after, parent, page)) != 1:
        raise ToolError("Give exactly one of after, parent or page.")
    args = ["insert"]
    for flag, value in (("--after", after), ("--parent", parent), ("--page", page)):
        if value is not None:
            args += [flag, value]
    return await run(*args, text_arg(markdown))


@mcp.tool(annotations=WRITE)
async def edit_block(block_id: str, text: Annotated[str, Field(description="The block's new full text (Markdown, one block)")]) -> str:
    """Replace one block's text. Refused on journal pages (add a new block instead)."""
    await refuse_journal_block(block_id)
    return await run("edit", block_id, text_arg(text))


@mcp.tool(annotations=WRITE)
async def move_block(block_id: str,
                     after: str | None = None, parent: str | None = None, page: str | None = None) -> str:
    """Move a block with its children. Give exactly one of after (a block id), parent (a block id) or page (a title). Refused on journals."""
    if sum(x is not None for x in (after, parent, page)) != 1:
        raise ToolError("Give exactly one of after, parent or page.")
    await refuse_journal_block(block_id)
    args = ["move", block_id]
    for flag, value in (("--after", after), ("--parent", parent), ("--page", page)):
        if value is not None:
            args += [flag, value]
    return await run(*args)


@mcp.tool(annotations=WRITE)
async def create_page(title: str) -> str:
    """Create an empty page."""
    return await run("create-page", text_arg(title))


@mcp.tool(annotations=WRITE)
async def rename_page(old: str, new: str) -> str:
    """Rename a page; every link and tag that points to it is rewritten."""
    return await run("rename-page", text_arg(old), text_arg(new))


@mcp.tool(annotations=WRITE)
async def set_favorite(page: str, favorite: bool = True) -> str:
    """Favorite or unfavorite a page."""
    return await run("favorite", text_arg(page), *([] if favorite else ["--off"]))


@mcp.tool(annotations=DESTROY)
async def delete_block(block_id: str) -> str:
    """Delete a block and its children (undoable with grimoire_undo_claude). Refused on journal pages."""
    await refuse_journal_block(block_id)
    return await run("delete", block_id)


@mcp.tool(annotations=DESTROY)
async def undo_claude(last: Annotated[int, Field(ge=1, le=50)] = 1) -> str:
    """Undo Claude's own last change(s) in Grimoire."""
    return await run("undo", "--last", str(last))


# ── flashcards ───────────────────────────────────────────────────────────

@mcp.tool(annotations=READ)
async def cards_status(page: str | None = None, chapter: Annotated[str | None, Field(description="Text of a parent heading, e.g. 'ch 5'")] = None) -> str:
    """Flashcards due now, new, and total, optionally for one page or chapter."""
    args = ["cards", "status"]
    if page: args += ["--page", page]
    if chapter: args += ["--chapter", chapter]
    return await run(*args)


@mcp.tool(annotations=READ)
async def cards_next(page: str | None = None, chapter: str | None = None,
                     limit: Annotated[int, Field(ge=1, le=20)] = 1) -> str:
    """The next flashcards to study (due first, then new): id, front, back. Read the front; when he's answered, call cards_review."""
    args = ["cards", "next", "--limit", str(limit)]
    if page: args += ["--page", page]
    if chapter: args += ["--chapter", chapter]
    return await run(*args)


@mcp.tool(annotations=WRITE)
async def cards_review(card_id: str, rating: Literal["again", "hard", "good", "easy"]) -> str:
    """Record how well he remembered a card; FSRS schedules the next review. Returns the new due time."""
    return await run("cards", "review", card_id, rating)


def build() -> FastMCP:
    return mcp
