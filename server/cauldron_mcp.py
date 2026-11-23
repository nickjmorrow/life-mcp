"""cauldron_*: the shopping list in Cauldron (the diet planner), through its JSON API on the same Mac.

Cauldron places each new item in a store aisle by itself, so these tools never set up aisle sections.
CAULDRON_URL sets where it listens (default http://127.0.0.1:3100). server.py mounts this server (namespace "cauldron").
"""
import json
import os
from typing import Annotated, Any

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

DEFAULT_URL = "http://127.0.0.1:3100"
TIMEOUT_S = 30
# What a list row keeps: enough to read the list back and change an item (no photo or lookup state).
ITEM_KEYS = ("id", "name", "qty", "note")

mcp = FastMCP("Cauldron")


async def request(method: str, path: str, body: dict | None = None, transport: Any = None) -> Any:
    """Call Cauldron's API; returns the parsed JSON (None when it answers with no body). Cauldron's own error
    message becomes the ToolError."""
    base = os.environ.get("CAULDRON_URL", DEFAULT_URL)
    try:
        async with httpx.AsyncClient(base_url=base, timeout=TIMEOUT_S, transport=transport) as http:
            r = await http.request(method, path, json=body)
    except httpx.TimeoutException:
        raise ToolError(f"Cauldron didn't answer {method} {path} within {TIMEOUT_S} seconds.")
    except httpx.TransportError as e:
        raise ToolError(f"Cauldron isn't reachable at {base} ({e.__class__.__name__}): is its server running?")
    if r.status_code >= 400:
        try:
            message = r.json().get("error") or r.text
        except ValueError:
            message = r.text
        raise ToolError(f"Cauldron answered {r.status_code}: {str(message)[:500]}")
    return r.json() if r.content else None


def _row(item: dict) -> dict:
    row = {k: item[k] for k in ITEM_KEYS if item.get(k) not in (None, "")}
    if item.get("meals"):
        row["reasons"] = item["meals"]
    return row


def _dump(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _same_name(a: str, b: str) -> bool:
    return a.strip().casefold() == b.strip().casefold()


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
async def shopping_list() -> str:
    """His grocery list: every item still to buy, grouped by store aisle in walking order. Returns
    {groups: [{aisle, items: [{id, name, qty, note, reasons}]}], done: [...], hidden: [...]}. reasons are what an item
    is for (meals, an occasion); hidden lists the reasons he has switched off for this trip ("" stands for items
    with no reason), so an item whose reasons are all hidden isn't on this trip. done are checked off, not yet
    cleared."""
    data = await request("GET", "/api/shopping")
    groups = [{"aisle": g["title"], "items": [_row(i) for i in g["items"]]} for g in data["groups"]]
    return _dump({"groups": groups, "done": [_row(i) for i in data["done"]], "hidden": data["hidden"]})


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
async def shopping_add(
    name: Annotated[str, Field(description="The item the way he said it, e.g. oat milk")],
    qty: Annotated[str | None, Field(description="How much, only if he said: free text like 2 or 1 bunch")] = None,
    note: Annotated[str | None, Field(description="A note, e.g. a brand he asked for")] = None,
    reasons: Annotated[list[str] | None, Field(description="What it's for, when he says (a meal, an occasion); "
                                                           "left out, Cauldron fills in the meals that use it")] = None,
) -> str:
    """Add one item to his grocery list. Cauldron finds its aisle itself: never pass or look up an aisle. If the
    same name is already waiting to be bought, nothing is added and that item comes back with existed: true.
    Returns {item, existed}."""
    name = name.strip()
    if not name:
        raise ToolError("name must not be empty.")
    current = await request("GET", "/api/shopping")
    for group in current["groups"]:
        for item in group["items"]:
            if _same_name(item["name"], name):
                return _dump({"item": _row(item), "existed": True})
    body: dict[str, Any] = {"name": name}
    for key, value in (("qty", qty), ("note", note), ("meals", reasons)):
        if value:
            body[key] = value
    return _dump({"item": _row(await request("POST", "/api/shopping", body)), "existed": False})


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
async def shopping_set(
    id: Annotated[int, Field(ge=1, description="The item's id from cauldron_shopping_list")],
    done: Annotated[bool | None, Field(description="true checks it off (bought); false puts it back")] = None,
    name: Annotated[str | None, Field(description="New name")] = None,
    qty: Annotated[str | None, Field(description="New quantity; \"\" clears it")] = None,
    note: Annotated[str | None, Field(description="New note; \"\" clears it")] = None,
    reasons: Annotated[list[str] | None, Field(description="The full new list of what it's for (replaces the old "
                                                           "one); [] clears it")] = None,
) -> str:
    """Change one item on his grocery list: check it off or put it back, rename it, or change its quantity, note or
    reasons. Only what you pass changes. Returns the updated item."""
    body = {key: value for key, value in (("done", done), ("name", name), ("qty", qty), ("note", note),
                                          ("meals", reasons)) if value is not None}
    if not body:
        raise ToolError("Nothing to change: pass done, name, qty, note or reasons.")
    item = await request("PATCH", f"/api/shopping/{id}", body)
    return _dump({**_row(item), "done": item["done"]})


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
async def shopping_remove(
    id: Annotated[int, Field(ge=1, description="The item's id from cauldron_shopping_list")],
) -> str:
    """Take an item off his grocery list for good (added by mistake, no longer wanted). For something he bought,
    use cauldron_shopping_set with done true instead. Returns {removed: id}."""
    await request("DELETE", f"/api/shopping/{id}")
    return _dump({"removed": id})
