"""The Life connector: Nicholas's Logseq graph (the tools below), plus every tool group in GROUPS
(shared memory, skills, health, people, Hue, Eight Sleep, Reminders, Hevy, Music, the Apple TV and
Apple Home), mounted by mount_all at the bottom.

claude.ai -> Tailscale Funnel (https://$PUBLIC_URL) -> this server on 127.0.0.1:8765
-> `logseq` CLI -> the db-worker owned by the running Logseq app.

Why the CLI and not the HTTP API: in Logseq 2.0 (DB graphs), the HTTP API's
getPageBlocksTree/getBlock hang for any page that isn't loaded in the app
window. The CLI talks to the app's own db-worker, so reads always work and
writes go through the same single writer (and sync) as typing in the app.

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
from pathlib import Path
from typing import Annotated, Any, Callable, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.github import GitHubProvider
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware, MiddlewareContext
from pydantic import Field
from host import NAME as MACHINE
import private
import usage_log

try:
    from memory_mcp import PAGE as MEMORY_PAGE
except Exception:  # the memory group logs its own failure; its page stays protected anyway
    MEMORY_PAGE = "Claude memories"

# Numeric IDs never change or get reused; logins can be renamed and re-registered.
# The one GitHub account (numeric user id) allowed in; set in ~/.zshrc.local. Empty = nobody.
ALLOWED_GITHUB_ID = os.environ.get("LIFE_MCP_GITHUB_ID", "").strip()
HOST = "127.0.0.1"  # never listen on the LAN; Funnel connects over loopback
PORT = 8765

LOGSEQ = os.path.expanduser("~/.local/bin/logseq")
GRAPH = private.get("logseq_graph", "")  # the DB graph's name, from the private config
REPO = f"logseq_db_{GRAPH}"
CLI_TIMEOUT_S = 30


# ── Logseq CLI ───────────────────────────────────────────────────────────


async def _run(*args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        LOGSEQ, *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), CLI_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        raise ToolError("Logseq CLI timed out")
    return proc.returncode, out.decode().strip()


async def _require_app_worker() -> None:
    """Refuse to run unless the Logseq app owns the graph's db-worker.

    Otherwise the CLI would start a second, CLI-owned worker on the same graph.
    """
    _, out = await _run("server", "list", "-o", "json")
    try:
        servers = json.loads(out)["data"]["servers"]
    except (ValueError, KeyError):
        servers = []
    if not any(
        s.get("repo") == REPO and s.get("status") == "ready"
        and s.get("owner-source") == "electron"
        for s in servers
    ):
        raise ToolError(f"The Logseq app isn't running on {MACHINE} (or its graph isn't open).")


async def cli(*args: str, json_out: bool = False):
    """Run `logseq <args> -g GRAPH`. Values must be passed as --opt=value."""
    await _require_app_worker()
    code, out = await _run(*args, f"--graph={GRAPH}", *(["-o", "json"] if json_out else []))
    if json_out:
        try:
            parsed = json.loads(out)
        except ValueError:
            raise ToolError(out or "Logseq CLI returned no output")
        if parsed.get("status") != "ok":
            raise ToolError(parsed.get("error", {}).get("message", out))
        return parsed["data"]
    if code != 0:
        raise ToolError(out or f"Logseq CLI exited with {code}")
    return out


async def page_exists(page: str) -> bool:
    try:
        await cli("show", f"--page={page}", "--level=1", json_out=True)
        return True
    except ToolError as e:
        if "not found" in str(e):
            return False
        raise


def edn(value) -> str:
    """JSON strings, numbers, booleans and arrays are also valid EDN."""
    if isinstance(value, dict):
        return "{" + " ".join(f"{edn(k)} {edn(v)}" for k, v in value.items()) + "}"
    if isinstance(value, Ident):
        return f":{value}"
    if isinstance(value, list):
        return "[" + " ".join(edn(v) for v in value) + "]"
    return json.dumps(value, ensure_ascii=False)


class Ident(str):
    """A :namespace/name keyword, written to EDN unquoted."""


async def resolve_tags(tags: list[str], create: bool = True) -> list:
    """Tag names -> EDN-ready tags. Built-in tags (Card, Task...) are used by ident,
    since a name lookup can hit a user tag with the same name in another case.
    Other missing tags are created (a no-op for existing ones, any case)."""
    built_in = {
        t["title"].lower(): Ident(t["ident"])
        for t in await _list("tag", "--include-built-in", "--limit=1000")
        if t.get("ident", "").startswith("logseq.class/")
    }
    out = []
    for t in tags:
        if t.lower() in built_in:
            out.append(built_in[t.lower()])
        else:
            if create:
                await cli("upsert", "tag", f"--name={t}", json_out=True)
            out.append(t)
    return out


async def ensure_properties(names: list[str]) -> None:
    existing = {(p.get("block/title") or p.get("title") or "").lower()
                for p in await _list("property", "--include-built-in")}
    for n in names:
        if n.lower() not in existing:
            await cli("upsert", "property", f"--name={n}", json_out=True)


async def _list(kind: str, *args: str) -> list[dict]:
    return (await cli("list", kind, *args, json_out=True))["items"]


def entity_args(page: str | None, block_id: int | None) -> list[str]:
    """`upsert page|block` args addressing exactly one page or block."""
    if (page is None) == (block_id is None):
        raise ToolError("Pass exactly one of page or block_id.")
    return ["page", f"--page={page}"] if page is not None else ["block", f"--id={block_id}"]


async def target_args(page: str | None, parent_block_id: int | None, create_page: bool) -> str:
    """Where to insert: under/next to a block, or on a page (default today's journal)."""
    if parent_block_id is not None:
        return f"--target-id={parent_block_id}"
    name = page or journal_name(None)
    if not await page_exists(name):
        if page is None:
            raise ToolError("Today's journal doesn't exist yet; open Logseq to create it.")
        if not create_page:
            raise ToolError(f"Page '{name}' doesn't exist. Pass create_page=true to create it.")
    return f"--target-page={name}"


def journal_name(date: str | None) -> str:
    """Logseq's journal page name, e.g. 'sep 26th, 2026'."""
    d = dt.date.fromisoformat(date) if date else dt.date.today()
    suffix = "th" if 11 <= d.day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(d.day % 10, "th")
    return f"{d.strftime('%b').lower()} {d.day}{suffix}, {d.year}"


# The memory page is written only through memory_save / memory_update, which refuse planted commands and
# secrets; the general block tools refuse it so that guard can't be walked around.
BLOCK_PAGE_QUERY = "[:find ?page-title :in $ ?b :where [?b :block/page ?p] [?p :block/title ?page-title]]"


async def refuse_memory_page(page: str | None = None, block_id: int | None = None) -> None:
    """Raise if page is the memory page or block_id is a block on it."""
    use = "use memory_save to add to it and memory_update to change or remove an entry."
    if page is not None and page.strip().lower() == MEMORY_PAGE.lower():
        raise ToolError(f"'{page}' is the shared memory page: {use}")
    if block_id is not None:
        rows = (await cli("query", f"--query={BLOCK_PAGE_QUERY}", f"--inputs=[{block_id}]",
                          json_out=True))["result"] or []
        if any(str(r[0] if isinstance(r, list) else r).strip().lower() == MEMORY_PAGE.lower() for r in rows):
            raise ToolError(f"Block {block_id} is on the {MEMORY_PAGE} page (shared memory): {use}")


# ── Tools ────────────────────────────────────────────────────────────────

mcp = FastMCP(
    "Life",
    instructions=(
        "Nicholas's Logseq graph (DB version), on his Mac. Pages and blocks are shown as "
        "trees with numeric ids; pass those ids to the block and task tools. "
        "Use tag and set_properties to tag pages and blocks or set their properties, "
        "instead of editing block text. Journal pages are named like 'sep 26th, 2026'."
    ),
)

READ = {"readOnlyHint": True}
WRITE = {"readOnlyHint": False, "destructiveHint": False}
DESTROY = {"readOnlyHint": False, "destructiveHint": True}

Position = Literal["last-child", "first-child", "sibling"]
Tags = Annotated[list[str] | None, Field(description="Tag names without '#'; missing tags are created")]
Date = Annotated[str | None, Field(description="YYYY-MM-DD")]
Depth = Annotated[int | None, Field(ge=1, le=20, description="Levels of children to show; omit for all")]


@mcp.tool(annotations=READ)
async def get_journal(
    date: Annotated[str | None, Field(description="YYYY-MM-DD; omit for today")] = None,
    depth: Depth = None,
) -> str:
    """Show a journal page (today's by default) as a block tree with ids."""
    return await get_page(journal_name(date), depth)


@mcp.tool(annotations=READ)
async def get_page(
    page: Annotated[str, Field(description="Page name (case-insensitive)")],
    depth: Depth = None,
    linked_references: Annotated[bool, Field(description="Also show blocks that link to this page")] = False,
) -> str:
    """Show a page as a block tree with ids."""
    args = ["show", f"--page={page}", f"--linked-references={str(linked_references).lower()}"]
    if depth:
        args.append(f"--level={depth + 1}")  # CLI counts the page itself as level 1
    return await cli(*args)


@mcp.tool(annotations=READ)
async def get_block(block_id: int, depth: Depth = None) -> str:
    """Show one block and its children."""
    args = ["show", f"--id={block_id}"]
    if depth:
        args.append(f"--level={depth + 1}")
    return await cli(*args)


@mcp.tool(annotations=READ)
async def search(
    query: str,
    kind: Literal["blocks", "pages", "both"] = "both",
) -> str:
    """Full-text search over block content and/or page titles."""
    parts = []
    for k in (["pages", "blocks"] if kind == "both" else [kind]):
        out = await cli("search", k[:-1], f"--content={query}")
        parts.append(f"## {k.title()}\n{out}")
    return "\n\n".join(parts)


@mcp.tool(annotations=READ)
async def list_pages(
    journal_only: bool = False,
    limit: Annotated[int, Field(ge=1, le=500)] = 50,
    sort: Literal["updated-at", "created-at", "title"] = "updated-at",
) -> str:
    """List pages, most recently updated first by default."""
    args = ["list", "page", f"--limit={limit}", f"--sort={sort}",
            f"--order={'asc' if sort == 'title' else 'desc'}"]
    if journal_only:
        args.append("--journal-only")
    return await cli(*args)


@mcp.tool(annotations=READ)
async def list_tasks(
    status: Annotated[str | None, Field(description="e.g. todo, doing, done")] = None,
    content: Annotated[str | None, Field(description="Filter by text")] = None,
    limit: Annotated[int, Field(ge=1, le=500)] = 50,
) -> str:
    """List tasks, most recently updated first."""
    args = ["list", "task", f"--limit={limit}", "--sort=updated-at", "--order=desc"]
    if status:
        args.append(f"--status={status}")
    if content:
        args.append(f"--content={content}")
    return await cli(*args)


@mcp.tool(annotations=READ)
async def list_tags(
    limit: Annotated[int, Field(ge=1, le=500)] = 100,
    include_built_in: bool = False,
) -> str:
    """List tags (classes), newest first."""
    return await cli("list", "tag", f"--limit={limit}", "--sort=created-at", "--order=desc",
                     f"--include-built-in={str(include_built_in).lower()}")


@mcp.tool(annotations=READ)
async def list_properties(
    limit: Annotated[int, Field(ge=1, le=500)] = 100,
    include_built_in: bool = False,
) -> str:
    """List properties with their types, newest first."""
    return await cli("list", "property", "--with-type", f"--limit={limit}", "--sort=created-at",
                     "--order=desc", f"--include-built-in={str(include_built_in).lower()}")


@mcp.tool(annotations=READ)
async def find_tagged(
    tags: Annotated[list[str] | None, Field(description="Match pages/blocks with any of these tags")] = None,
    properties: Annotated[list[str] | None, Field(description="Match pages/blocks that have any of these properties")] = None,
    limit: Annotated[int, Field(ge=1, le=500)] = 50,
) -> str:
    """Find pages and blocks by tag and/or property, most recently updated first."""
    if not tags and not properties:
        raise ToolError("Pass tags or properties.")
    args = ["list", "node", f"--limit={limit}", "--sort=updated-at", "--order=desc"]
    if tags:
        args.append(f"--tags={','.join(tags)}")
    if properties:
        args.append(f"--properties={','.join(properties)}")
    return await cli(*args)


@mcp.tool(annotations=READ)
async def query(
    datascript: Annotated[str, Field(description="Datascript query in EDN")],
    inputs: Annotated[str | None, Field(description="Query inputs as an EDN vector")] = None,
) -> str:
    """Run a raw Datascript query against the graph."""
    args = ["query", f"--query={datascript}"]
    if inputs:
        args.append(f"--inputs={inputs}")
    return await cli(*args)


@mcp.tool(annotations=WRITE)
async def add_block(
    content: str,
    page: Annotated[str | None, Field(description="Page to add to; omit for today's journal")] = None,
    parent_block_id: Annotated[int | None, Field(description="Nest under (or next to) this block instead of a page")] = None,
    position: Position = "last-child",
    create_page: Annotated[bool, Field(description="Create the page if it doesn't exist")] = False,
    tags: Tags = None,
) -> str:
    """Add a block to a page (default: end of today's journal) or under a block."""
    await refuse_memory_page(page, parent_block_id)
    target = await target_args(page, parent_block_id, create_page)
    args = ["upsert", "block", target, f"--pos={position}", f"--content={content}"]
    if tags:
        args.append(f"--update-tags={edn(await resolve_tags(tags))}")
    data = await cli(*args, json_out=True)
    return f"Added block {data['result'][0]}"


@mcp.tool(annotations=WRITE)
async def update_block(block_id: int, content: str) -> str:
    """Replace a block's text."""
    await refuse_memory_page(block_id=block_id)
    await cli("upsert", "block", f"--id={block_id}", f"--content={content}", json_out=True)
    return f"Updated block {block_id}"


@mcp.tool(annotations=WRITE)
async def create_page(page: str) -> str:
    """Create an empty page (no-op if it already exists)."""
    if await page_exists(page):
        return f"Page '{page}' already exists"
    data = await cli("upsert", "page", f"--page={page}", json_out=True)
    return f"Created page '{page}' ({data.get('result')})"


@mcp.tool(annotations=WRITE)
async def move_block(
    block_id: int,
    page: Annotated[str | None, Field(description="Move to this page")] = None,
    target_block_id: Annotated[int | None, Field(description="Move under (or next to) this block instead")] = None,
    position: Position = "last-child",
) -> str:
    """Move a block (with its children) to another page or block."""
    if (page is None) == (target_block_id is None):
        raise ToolError("Pass exactly one of page or target_block_id.")
    await refuse_memory_page(block_id=block_id)
    await refuse_memory_page(page, target_block_id)
    target = await target_args(page, target_block_id, create_page=False)
    await cli("upsert", "block", f"--id={block_id}", target, f"--pos={position}", json_out=True)
    return f"Moved block {block_id}"


@mcp.tool(annotations=WRITE)
async def tag(
    page: Annotated[str | None, Field(description="Page to tag")] = None,
    block_id: Annotated[int | None, Field(description="Block to tag instead of a page")] = None,
    add: Tags = None,
    remove: Annotated[list[str] | None, Field(description="Tag names to remove")] = None,
) -> str:
    """Add or remove tags on a page or block. Missing tags are created."""
    if not add and not remove:
        raise ToolError("Pass tags to add or remove.")
    args = ["upsert", *entity_args(page, block_id)]
    if add:
        args.append(f"--update-tags={edn(await resolve_tags(add))}")
    if remove:
        args.append(f"--remove-tags={edn(await resolve_tags(remove, create=False))}")
    await cli(*args, json_out=True)
    return f"Tagged {page or block_id}"


@mcp.tool(annotations=WRITE)
async def set_properties(
    page: Annotated[str | None, Field(description="Page to update")] = None,
    block_id: Annotated[int | None, Field(description="Block to update instead of a page")] = None,
    values: Annotated[dict[str, str | int | float | bool] | None, Field(
        description="Property name -> value. Missing properties are created as text.")] = None,
    remove: Annotated[list[str] | None, Field(description="Property names to remove")] = None,
) -> str:
    """Set or remove properties on a page or block."""
    if not values and not remove:
        raise ToolError("Pass properties to set or remove.")
    args = ["upsert", *entity_args(page, block_id)]
    if values:
        await ensure_properties(list(values))
        args.append(f"--update-properties={edn(values)}")
    if remove:
        args.append(f"--remove-properties={edn(remove)}")
    await cli(*args, json_out=True)
    return f"Updated properties on {page or block_id}"


@mcp.tool(annotations=WRITE)
async def add_flashcard(
    question: str,
    answer: Annotated[str | list[str], Field(description="Answer text, or several answer blocks")],
    page: Annotated[str | None, Field(description="Page to add to; omit for today's journal")] = None,
    parent_block_id: Annotated[int | None, Field(description="Nest under (or next to) this block instead of a page")] = None,
    position: Position = "last-child",
    create_page: Annotated[bool, Field(description="Create the page if it doesn't exist")] = False,
) -> str:
    """Add a flashcard: a question block tagged #Card with the answer nested under it.
    It shows up in Logseq's flashcard review. To make an existing block a card, tag it Card."""
    await refuse_memory_page(page, parent_block_id)
    target = await target_args(page, parent_block_id, create_page)
    data = await cli("upsert", "block", target, f"--pos={position}", f"--content={question}",
                     f"--update-tags={edn([Ident('logseq.class/Card')])}", json_out=True)
    card_id = data["result"][0]
    for text in [answer] if isinstance(answer, str) else answer:
        await cli("upsert", "block", f"--target-id={card_id}", "--pos=last-child",
                  f"--content={text}", json_out=True)
    return f"Added flashcard {card_id}"


TaskStatus = Literal["backlog", "todo", "doing", "in-review", "done", "canceled"]
Priority = Literal["low", "medium", "high", "urgent"]


@mcp.tool(annotations=WRITE)
async def add_task(
    content: str,
    page: Annotated[str | None, Field(description="Page to add to; omit for today's journal")] = None,
    parent_block_id: Annotated[int | None, Field(description="Nest under (or next to) this block instead of a page")] = None,
    position: Position = "last-child",
    status: TaskStatus = "todo",
    priority: Priority | None = None,
    scheduled: Date = None,
    deadline: Date = None,
) -> str:
    """Add a task (default: end of today's journal)."""
    await refuse_memory_page(page, parent_block_id)
    fields = _task_fields(priority, scheduled, deadline)
    # `upsert task` can only create at the end of a page, so add a block and convert it.
    target = await target_args(page, parent_block_id, create_page=False)
    data = await cli("upsert", "block", target, f"--pos={position}", f"--content={content}", json_out=True)
    block_id = data["result"][0]
    await cli("upsert", "task", f"--id={block_id}", f"--status={status}", *fields, json_out=True)
    return f"Added task {block_id}"


@mcp.tool(annotations=WRITE)
async def update_task(
    block_id: Annotated[int, Field(description="A task, or any block to turn into one")],
    status: TaskStatus | None = None,
    priority: Priority | None = None,
    scheduled: Date = None,
    deadline: Date = None,
    clear: Annotated[list[Literal["status", "priority", "scheduled", "deadline"]] | None,
                     Field(description="Fields to remove")] = None,
) -> str:
    """Change a task's status, priority or dates."""
    args = ["upsert", "task", f"--id={block_id}"]
    if status:
        args.append(f"--status={status}")
    args += _task_fields(priority, scheduled, deadline)
    args += [f"--no-{f}" for f in clear or []]
    if len(args) == 3:
        raise ToolError("Nothing to change.")
    await cli(*args, json_out=True)
    return f"Updated task {block_id}"


def _task_fields(priority, scheduled, deadline) -> list[str]:
    for d in (scheduled, deadline):
        try:
            d and dt.date.fromisoformat(d)
        except ValueError:
            raise ToolError(f"Dates must be YYYY-MM-DD, not '{d}'.")
    return [f"--{k}={v}" for k, v in
            (("priority", priority), ("scheduled", scheduled), ("deadline", deadline)) if v]


# Parent links and user properties of every block on the page that a block is on.
PAGE_PARENTS_QUERY = (
    '[:find ?c ?parent :in $ ?b :where [?b :block/page ?page] [?c :block/page ?page]'
    ' [?c :block/parent ?parent]]'
)
PAGE_PROPS_QUERY = (
    '[:find ?c ?a :in $ ?b :where [?b :block/page ?page] [?c :block/page ?page] [?c ?a _]'
    ' [(namespace ?a) ?ns] [(= ?ns "user.property")]]'
)


@mcp.tool(annotations=DESTROY)
async def delete_block(block_id: int) -> str:
    """Delete a block and its children."""
    await refuse_memory_page(block_id=block_id)
    # Logseq sync once rejected removing blocks that still had user properties (in the
    # subtree), and they came back (seen live on the old memory page). Clear them first to be safe.
    edges = (await cli("query", f"--query={PAGE_PARENTS_QUERY}", f"--inputs=[{block_id}]",
                       json_out=True))["result"] or []
    children: dict[int, list[int]] = {}
    for c, parent in edges:
        children.setdefault(parent, []).append(c)
    subtree, todo = set(), [block_id]
    while todo:
        b = todo.pop()
        subtree.add(b)
        todo += children.get(b, [])
    idents: dict[int, list[str]] = {}
    for b, ident in (await cli("query", f"--query={PAGE_PROPS_QUERY}", f"--inputs=[{block_id}]",
                               json_out=True))["result"] or []:
        if b in subtree:
            idents.setdefault(b, []).append(ident)
    for b, names in idents.items():
        await cli("upsert", "block", f"--id={b}",
                  f"--remove-properties=[{' '.join(':' + n for n in names)}]", json_out=True)
    await cli("remove", "block", f"--id={block_id}", json_out=True)
    return f"Deleted block {block_id}"


@mcp.tool(annotations=DESTROY)
async def delete_page(page: str) -> str:
    """Delete a page and all its blocks."""
    await refuse_memory_page(page)
    await cli("remove", "page", f"--page={page}", json_out=True)
    return f"Deleted page '{page}'"


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
    Group("health", "health_mcp", build=lambda m: m.build()),
    Group("people", "people_mcp", build=lambda m: m.build(cli)),
    Group("cards", "cards_mcp", build=lambda m: m.build(cli)),
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


def mount_all(target: FastMCP | None = None, groups: tuple[Group, ...] = GROUPS) -> list[str]:
    """Mount every group (returns the labels that loaded), then tell the memory guard every tool name
    the server exposes, so memory can't be made to hold a command for any of them."""
    target = target or mcp
    loaded = [g.label for g in groups if mount(g, target)]

    async def names():
        return [t.name for t in await target.list_tools()]
    try:
        import memory_mcp
        memory_mcp.set_tool_source(names)  # listed on first use, inside the running server
    except Exception as e:
        print(f"Memory guard not given the tool names: {e!r}", file=sys.stderr)
    return loaded


if __name__ == "__main__":
    mount_all()
    if "--stdio" in sys.argv:
        mcp.run(show_banner=False)
    else:
        mcp.auth = github_auth()
        mcp.add_middleware(OnlyMe())
        mcp.add_middleware(ChatLog())
        mcp.run(transport="http", host=HOST, port=PORT)
