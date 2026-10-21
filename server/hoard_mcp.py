"""hoard_*: the Hoard media app's API (movies, shows and games), on the same Mac.

Hoard keeps everything about a title (status, rating, tags, notes, looked-up details) in its own store; Grimoire holds
none of it. These tools go through Hoard's API, so they never touch Grimoire pages.
HOARD_URL sets where it listens (default http://127.0.0.1:8781). server.py mounts this server (namespace "hoard").
"""
import json
import os
import re
from typing import Annotated, Any, Literal

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

DEFAULT_URL = "http://127.0.0.1:8781"
PAGE_ID = re.compile(r"^[A-Za-z0-9_\-]+$")  # one path segment: no slashes, query string or ".."
TIMEOUT_S = 30
SUGGEST_TIMEOUT_S = 300  # Hoard runs Claude for suggestions and gives it up to 4 minutes
# What a library row keeps: enough to pick and change a title, without the long fields (summary, cast, notes).
ROW_KEYS = ("pageId", "name", "year", "type", "status", "tags", "rating", "rec", "finished", "progress")
# What hoard_get adds for one title: the details Hoard looked up (the notes come separately).
DETAIL_KEYS = ("imdb", "runtime", "genre", "rt", "summary", "director", "writer", "cast")

Status = Literal["want", "doing", "done", "dropped"]
MediaType = Literal["movie", "show", "game"]

mcp = FastMCP("Hoard")


async def request(method: str, path: str, body: dict | None = None, timeout: float = TIMEOUT_S,
                  transport: Any = None) -> Any:
    """Call Hoard's API; returns the parsed JSON. Hoard's own error message becomes the ToolError."""
    base = os.environ.get("HOARD_URL", DEFAULT_URL)
    try:
        async with httpx.AsyncClient(base_url=base, timeout=timeout, transport=transport) as http:
            r = await http.request(method, path, json=body)
    except httpx.TimeoutException:
        raise ToolError(f"Hoard didn't answer {method} {path} within {timeout:.0f} seconds.")
    except httpx.TransportError as e:
        raise ToolError(f"Hoard isn't reachable at {base} ({e.__class__.__name__}): is its server running?")
    if r.status_code >= 400:
        try:
            message = r.json().get("error") or r.text
        except ValueError:
            message = r.text
        raise ToolError(f"Hoard answered {r.status_code}: {str(message)[:500]}")
    return r.json()


def _page_id(page_id: str) -> str:
    page_id = page_id.strip()
    if not PAGE_ID.match(page_id):
        raise ToolError("page_id must be a title's pageId from hoard_library.")
    return page_id


def _row(title: dict) -> dict:
    return {k: title[k] for k in ROW_KEYS if title.get(k) not in (None, [], "")}


def _dump(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
async def library(
    status: Annotated[Status | None, Field(description="Only titles with this status; want is the want list")] = None,
    type: Annotated[MediaType | None, Field(description="Only movies, shows or games")] = None,
    tag: Annotated[str | None, Field(description="Only titles with this tag (a queue), e.g. Rainy Day")] = None,
    name: Annotated[str | None, Field(description="Only titles whose name contains this text")] = None,
) -> str:
    """List his movies, shows and games: the want list and everything he's started or watched. Filters combine; text
    matches ignore case. Returns {titles: [...], tags: [...]}, each title with pageId, name, year, type, status, tags,
    rating, rec, finished, progress. Use it to find a title's pageId and to check what he already has before
    suggesting or adding anything; hoard_get shows one title's notes and details."""
    data = await request("GET", "/api/library")
    titles = data["titles"]
    if status:
        titles = [t for t in titles if t.get("status") == status]
    if type:
        titles = [t for t in titles if t.get("type") == type]
    if tag:
        titles = [t for t in titles if tag.strip().lower() in (x.lower() for x in t.get("tags", []))]
    if name:
        titles = [t for t in titles if name.strip().lower() in t.get("name", "").lower()]
    tags = [{"title": t["title"], "group": t.get("group"), "count": t.get("count")} for t in data.get("tags", [])]
    return _dump({"count": len(titles), "titles": [_row(t) for t in titles], "tags": tags})


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
async def add(
    name: Annotated[str, Field(description="The title's name without the year, e.g. Nope")],
    type: Annotated[MediaType, Field(description="movie, show or game")],
    year: Annotated[int | None, Field(description="Release year, when known; tells same-name works apart")] = None,
    tags: Annotated[list[str] | None, Field(description="Tags (queues) by full name, e.g. [\"Rainy Day\"]")] = None,
    rec: Annotated[str | None, Field(description="Who recommended it, e.g. a friend's name; Claude if you did")] = None,
    status: Annotated[Status, Field(description="want (default) puts it on the want list; doing, done or dropped "
                                                "if he's already started, watched or dropped it")] = "want",
) -> str:
    """Add a movie, show or game to Hoard. If he already has it (same IMDb id or name and year), nothing is created
    and the existing title comes back with existed: true. Hoard fills in IMDb, runtime, genre and the rest within a
    few minutes. Returns {title, existed}."""
    fields: dict[str, str] = {"status": status}
    if rec:
        fields["rec"] = rec
    body: dict[str, Any] = {"name": name, "type": type, "fields": fields, "tags": tags or []}
    if year is not None:
        body["year"] = year
    out = await request("POST", "/api/titles", body)
    return _dump({"title": _row(out["title"]), "existed": out["existed"]})


@mcp.tool(name="set", annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True,
                                  "openWorldHint": False})
async def set_title(
    page_id: Annotated[str, Field(description="The title's pageId from hoard_library")],
    status: Annotated[Status | None, Field(description="New status; done also sets today's finished date")] = None,
    rating: Annotated[int | None, Field(ge=1, le=5, description="His rating, a whole number 1 to 5")] = None,
    tags: Annotated[list[str] | None, Field(description="The full new tag list (replaces the old one: include the "
                                                        "tags it already has)")] = None,
    rec: Annotated[str | None, Field(description="Who recommended it")] = None,
    finished: Annotated[str | None, Field(description="Date finished, YYYY-MM-DD; status done sets today's date "
                                                      "when this is left out")] = None,
    progress: Annotated[str | None, Field(description="Where he is, free text like S2E4")] = None,
) -> str:
    """Change a title's status, rating, tags, recommender, finished date or progress. Only what you pass changes.
    Returns the updated title."""
    fields: dict[str, str] = {}
    for key, value in (("status", status), ("rec", rec), ("finished", finished), ("progress", progress)):
        if value is not None:
            fields[key] = value
    if rating is not None:
        fields["rating"] = str(rating)
    if not fields and tags is None:
        raise ToolError("Nothing to change: pass status, rating, tags, rec, finished or progress.")
    body: dict[str, Any] = {}
    if fields:
        body["fields"] = fields
    if tags is not None:
        body["tags"] = tags
    title = await request("PATCH", f"/api/titles/{_page_id(page_id)}", body)
    return _dump(_row(title))


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
async def get(
    page_id: Annotated[str, Field(description="The title's pageId from hoard_library")],
) -> str:
    """One title in full: everything hoard_library shows plus the details Hoard looked up (imdb, runtime, genre, rt,
    summary, director, writer, cast) and his notes and reviews, each with an id (for hoard_note to edit)."""
    title = await request("GET", f"/api/titles/{_page_id(page_id)}")
    detail = {k: title[k] for k in DETAIL_KEYS if title.get(k) not in (None, [], "")}
    return _dump({**_row(title), **detail, "notes": [{"id": n["id"], "text": n["text"]} for n in title.get("notes", [])]})


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False})
async def note(
    page_id: Annotated[str, Field(description="The title's pageId from hoard_library")],
    text: Annotated[str, Field(description="The note or review, in his words")],
    note_id: Annotated[str | None, Field(description="To replace an existing note, its id from hoard_get; leave out "
                                                     "to add a new note")] = None,
) -> str:
    """Add a note or review to a title, or with note_id replace that note's text. Write what he says in his words;
    never summarize or improve it. Returns {id, text}."""
    if not text.strip():
        raise ToolError("text must not be empty.")
    title_id = _page_id(page_id)
    if note_id is None:
        return _dump(await request("POST", f"/api/titles/{title_id}/notes", {"text": text}))
    note_id = note_id.strip()
    if not PAGE_ID.match(note_id):
        raise ToolError("note_id must be a note's id from hoard_get.")
    await request("PUT", f"/api/notes/{note_id}", {"text": text})
    return _dump({"id": note_id, "text": text})


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True})
async def suggest(
    tag: Annotated[str | None, Field(description="Suggest more like the titles with this tag (a queue)")] = None,
    person: Annotated[str | None, Field(description="Suggest more by this director or writer, as Hoard lists them")] = None,
    page_ids: Annotated[list[str] | None, Field(description="Suggest more like these titles (pageIds)")] = None,
    prompt: Annotated[str | None, Field(description="Extra direction in his words, e.g. something short and funny")] = None,
    type: Annotated[Literal["movie", "show"] | None, Field(description="Only movies or only shows")] = None,
    count: Annotated[int, Field(ge=1, le=25, description="How many to ask for")] = 10,
) -> str:
    """Ask Hoard for suggestions from one source: a tag, a person or some titles (pass exactly one). Hoard asks
    Claude with his ratings and notes, checks each pick is a real title, drops anything he already has or passed on,
    and adds the rest to the Suggestions inbox in his Hoard app. Takes up to a few minutes. Returns
    {suggestions: [{id, name, year, type, why, ...}]}; to put one on the want list, hoard_add it with rec Claude."""
    sources = {"tag": tag, "person": person, "pageIds": page_ids}
    given = {k: v for k, v in sources.items() if v}
    if len(given) != 1:
        raise ToolError("Pass exactly one of tag, person or page_ids.")
    body: dict[str, Any] = {**given, "count": count}
    if prompt:
        body["prompt"] = prompt
    if type:
        body["type"] = type
    out = await request("POST", "/api/suggest", body, timeout=SUGGEST_TIMEOUT_S)
    keep = ("id", "name", "year", "type", "why", "source", "tag")
    return _dump({"suggestions": [{k: s[k] for k in keep if s.get(k) is not None} for s in out["suggestions"]]})
