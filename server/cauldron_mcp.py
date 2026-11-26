"""cauldron_*: the shopping list and the recipes in Cauldron (the diet planner), through its JSON API on the same Mac.

Cauldron places each new item in a store aisle by itself, so these tools never set up aisle sections. A recipe is
how one of Cauldron's meals is cooked: its id and name are the meal's. A recipe is
always saved whole (Cauldron has no partial update), so recipe_append reads it, adds to one section and saves it
back. There is no tool that deletes a recipe.
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
# What a recipe list row keeps: enough to pick a recipe; cauldron_recipe_get has the rest.
RECIPE_ROW_KEYS = ("id", "name", "summary", "tags", "typical")
# What Cauldron takes when a recipe is saved (what it returns, without id and typical).
RECIPE_KEYS = ("name", "summary", "serves", "activeTime", "totalTime", "tags", "ingredients", "sections")

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


def _recipe_row(recipe: dict) -> dict:
    return {k: recipe[k] for k in RECIPE_ROW_KEYS if recipe.get(k) not in (None, "", [])}


def _saved(answer: dict) -> dict:
    """What a save tool returns: Cauldron's {recipe}, cut to the recipe's id, name and section titles."""
    recipe = answer["recipe"]
    return {"id": recipe["id"], "name": recipe["name"], "sections": [s["title"] for s in recipe["sections"]]}


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
async def recipe_list() -> str:
    """His recipes, by name: Cauldron holds every recipe he cooks from, each as part of a meal (the id and name are
    the meal's). Returns {recipes: [{id, name, summary, tags, typical}]}; typical: the meal is in his typical day.
    Read one whole with cauldron_recipe_get."""
    data = await request("GET", "/api/recipes")
    return _dump({"recipes": [_recipe_row(r) for r in data["recipes"]]})


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
async def recipe_get(
    id: Annotated[int, Field(ge=1, description="The recipe's id from cauldron_recipe_list")],
) -> str:
    """One recipe, whole: {id, name, summary, serves, activeTime, totalTime, tags, ingredients: [{group, text, buy}],
    sections: [{title, body}], typical}. An ingredient's text is the line as written, buy is what goes on a
    shopping list for it ("" when nothing is bought, several names split by ";"), group is its sub-list heading.
    A section's body is Markdown bullets, nested by two spaces. typical: the meal is in his typical day."""
    return _dump(await request("GET", f"/api/recipes/{id}"))


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
async def recipe_save(
    name: Annotated[str, Field(description="The dish, e.g. Chicken tikka masala")],
    ingredients: Annotated[list[dict[str, str]], Field(description=(
        "Every ingredient line in order, each {text, buy, group}: text is the line as written (2 Tbsp olive oil), "
        "buy is what to shop for (olive oil; \"\" for water and the like; several names split by \";\"), group is "
        "an optional sub-list heading (sauce)"))],
    sections: Annotated[list[dict[str, str]], Field(description=(
        "Every section in order, each {title, body}: title like instructions or log, body Markdown bullets "
        "nested by two spaces"))],
    id: Annotated[int | None, Field(ge=1, description="The recipe to replace (from cauldron_recipe_list); leave out "
                                                      "for a new recipe")] = None,
    summary: Annotated[str | None, Field(description="One line: what it is and what makes it good")] = None,
    serves: Annotated[str | None, Field(description="Free text, e.g. 6")] = None,
    active_time: Annotated[str | None, Field(description="Free text, e.g. ~25 min")] = None,
    total_time: Annotated[str | None, Field(description="Free text, e.g. 2 hr + overnight")] = None,
    tags: Annotated[list[str] | None, Field(description="e.g. [\"dinner\", \"chicken\"]")] = None,
) -> str:
    """Save a whole recipe in Cauldron. Without id it adds a new recipe (onto the meal of that name when there is
    one, else as a new meal he cooks now and then), and refuses when a recipe of that name exists. With id it replaces that meal's recipe entirely (and may rename the
    meal): anything you leave out is cleared, so read it with cauldron_recipe_get first and send everything back,
    changed where needed. To add to one section (a log entry after cooking), use cauldron_recipe_append instead.
    Returns {id, name, sections: [titles]}."""
    name = name.strip()
    if not name:
        raise ToolError("name must not be empty.")
    for i, line in enumerate(ingredients, 1):
        if "buy" not in line:
            raise ToolError(f"ingredient {i} needs buy: what to shop for, or \"\" when nothing is bought for it.")
    if id is None:
        for recipe in (await request("GET", "/api/recipes"))["recipes"]:
            if _same_name(recipe["name"], name):
                raise ToolError(f"There is already a recipe called {recipe['name']} (id {recipe['id']}): pass its id "
                                "to replace it, or use another name.")
    body: dict[str, Any] = {"name": name, "ingredients": ingredients, "sections": sections}
    for key, value in (("summary", summary), ("serves", serves), ("activeTime", active_time),
                       ("totalTime", total_time), ("tags", tags)):
        if value is not None:
            body[key] = value
    if id is None:
        return _dump(_saved(await request("POST", "/api/recipes", body)))
    return _dump(_saved(await request("PUT", f"/api/recipes/{id}", body)))


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False})
async def recipe_append(
    id: Annotated[int, Field(ge=1, description="The recipe's id from cauldron_recipe_list")],
    section: Annotated[str, Field(description="The section's title, e.g. log (upper or lower case is the same)")],
    text: Annotated[str, Field(description="Markdown bullets to add at the section's end, nested by two spaces, "
                                           "e.g. - 2026-03-01: 24 min at 350°F, fluffy")],
) -> str:
    """Add lines to the end of one section of a recipe, keeping everything else as it is: a dated log entry after
    he cooks it, a troubleshooting line. A section the recipe doesn't have yet is added after its last one, so
    check the titles with cauldron_recipe_get first. Returns {id, name, section, created, sections: [titles]}."""
    title, text = section.strip(), text.strip("\n").rstrip()
    if not title:
        raise ToolError("section must not be empty.")
    if not text.strip():
        raise ToolError("text must not be empty.")
    recipe = await request("GET", f"/api/recipes/{id}")
    existing = next((s for s in recipe["sections"] if _same_name(s["title"], title)), None)
    if existing:
        title = existing["title"]
        existing["body"] = f"{existing['body']}\n{text}" if existing["body"] else text
    else:
        recipe["sections"].append({"title": title, "body": text})
    saved = await request("PUT", f"/api/recipes/{id}", {k: recipe[k] for k in RECIPE_KEYS})
    return _dump({**_saved(saved), "section": title, "created": existing is None})
