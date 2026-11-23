import asyncio
import json

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import cauldron_mcp

OAT_MILK = {"id": 7, "name": "Oat milk", "qty": "2", "aisle": "Dairy & eggs", "done": False, "note": "", "photo": "",
            "aisleFrom": "guess", "looking": False, "meals": ["Breakfast"]}
FOIL = {"id": 3, "name": "Foil", "qty": "", "aisle": "3", "done": False, "note": "heavy-duty", "photo": "",
        "aisleFrom": "you", "looking": False, "meals": []}
RICE = {**FOIL, "id": 2, "name": "Rice", "note": "", "done": True}
LIST = {
    "groups": [{"title": "Aisle 3", "holds": "Foil and bags", "items": [FOIL]},
               {"title": "Dairy & eggs", "holds": "", "items": [OAT_MILK]}],
    "done": [RICE], "reasons": ["Breakfast"], "hidden": ["Later"],
}


class Calls(list):
    answers: dict


@pytest.fixture
def calls(monkeypatch):
    """Record every API call; answer from `calls.answers` by (method, path), else the list."""
    seen = Calls()
    seen.answers = {}

    async def fake(method, path, body=None, transport=None):
        seen.append((method, path, body))
        return seen.answers.get((method, path), LIST)

    monkeypatch.setattr(cauldron_mcp, "request", fake)
    return seen


def call(tool, **args):
    async def go():
        async with Client(cauldron_mcp.mcp) as client:
            return (await client.call_tool(tool, args)).content[0].text
    return json.loads(asyncio.run(go()))


def test_list_is_grouped_by_aisle_and_trimmed(calls):
    assert call("shopping_list") == {
        "groups": [{"aisle": "Aisle 3", "items": [{"id": 3, "name": "Foil", "note": "heavy-duty"}]},
                   {"aisle": "Dairy & eggs", "items": [{"id": 7, "name": "Oat milk", "qty": "2", "reasons": ["Breakfast"]}]}],
        "done": [{"id": 2, "name": "Rice"}], "hidden": ["Later"]}
    assert calls == [("GET", "/api/shopping", None)]


def test_add_sends_only_what_was_given_and_no_aisle(calls):
    calls.answers[("POST", "/api/shopping")] = {**FOIL, "id": 9, "name": "paper towels", "note": ""}
    out = call("shopping_add", name=" paper towels ")
    assert calls[-1] == ("POST", "/api/shopping", {"name": "paper towels"})
    assert out == {"item": {"id": 9, "name": "paper towels"}, "existed": False}
    call("shopping_add", name="limes", qty="3", note="ripe", reasons=["Taco night"])
    assert calls[-1][2] == {"name": "limes", "qty": "3", "note": "ripe", "meals": ["Taco night"]}


def test_add_returns_the_item_already_waiting(calls):
    out = call("shopping_add", name="oat MILK")
    assert out == {"item": {"id": 7, "name": "Oat milk", "qty": "2", "reasons": ["Breakfast"]}, "existed": True}
    assert [c[0] for c in calls] == ["GET"]


def test_add_again_after_it_was_checked_off(calls):
    calls.answers[("POST", "/api/shopping")] = {**RICE, "id": 10, "done": False}
    assert call("shopping_add", name="Rice")["existed"] is False


def test_add_needs_a_name(calls):
    with pytest.raises(ToolError, match="name"):
        call("shopping_add", name="  ")
    assert calls == []


def test_set_sends_only_what_changes(calls):
    calls.answers[("PATCH", "/api/shopping/7")] = {**OAT_MILK, "done": True}
    out = call("shopping_set", id=7, done=True)
    assert calls == [("PATCH", "/api/shopping/7", {"done": True})]
    assert out == {"id": 7, "name": "Oat milk", "qty": "2", "reasons": ["Breakfast"], "done": True}
    call("shopping_set", id=7, qty="", reasons=[])
    assert calls[-1][2] == {"qty": "", "meals": []}


def test_set_needs_a_change_and_a_whole_number_id(calls):
    with pytest.raises(ToolError, match="Nothing to change"):
        call("shopping_set", id=7)
    with pytest.raises(ToolError):
        call("shopping_set", id=0, done=True)
    assert calls == []


def test_remove_deletes_the_item(calls):
    calls.answers[("DELETE", "/api/shopping/3")] = None
    assert call("shopping_remove", id=3) == {"removed": 3}
    assert calls == [("DELETE", "/api/shopping/3", None)]


SOUP = {"id": 4, "name": "Lentil soup", "summary": "Red lentils, one pot.", "serves": "4", "activeTime": "~15 min",
        "totalTime": "40 min", "tags": ["dinner", "soup"], "meals": ["Soup night"], "typical": True,
        "ingredients": [{"group": "", "text": "1 cup red lentils", "buy": "red lentils"},
                        {"group": "", "text": "4 cups water", "buy": ""}],
        "sections": [{"title": "instructions", "body": "- **1. Simmer.** **25 min**"},
                     {"title": "log", "body": "- 2026-01-05: a little thin"},
                     {"title": "notes", "body": ""}]}
SOUP_ROW = {"id": 4, "name": "Lentil soup", "summary": "Red lentils, one pot.", "tags": ["dinner", "soup"],
            "typical": True, "meals": ["Soup night"]}
TOAST_ROW = {"id": 6, "name": "Toast", "summary": "", "tags": [], "typical": False, "meals": []}
RECIPES = {"recipes": [SOUP_ROW, TOAST_ROW]}
SOUP_TITLES = ["instructions", "log", "notes"]
NEW_LINES = [{"text": "2 slices bread", "buy": "bread"}]
NEW_SECTIONS = [{"title": "instructions", "body": "- Toast it."}]


def test_recipe_list_is_trimmed(calls):
    calls.answers[("GET", "/api/recipes")] = RECIPES
    assert call("recipe_list") == {"recipes": [
        {"id": 4, "name": "Lentil soup", "summary": "Red lentils, one pot.", "tags": ["dinner", "soup"],
         "meals": ["Soup night"]},
        {"id": 6, "name": "Toast"}]}
    assert calls == [("GET", "/api/recipes", None)]


def test_recipe_get_returns_it_whole(calls):
    calls.answers[("GET", "/api/recipes/4")] = SOUP
    assert call("recipe_get", id=4) == SOUP
    assert calls == [("GET", "/api/recipes/4", None)]
    with pytest.raises(ToolError):
        call("recipe_get", id=0)


def test_recipe_save_adds_a_new_recipe(calls):
    calls.answers[("GET", "/api/recipes")] = RECIPES
    calls.answers[("POST", "/api/recipes")] = {
        "recipe": {**SOUP, "id": 9, "name": "Garlic bread", "sections": NEW_SECTIONS}, "unknownMeals": ["Brunch"]}
    out = call("recipe_save", name=" Garlic bread ", ingredients=NEW_LINES, sections=NEW_SECTIONS, serves="2",
               active_time="5 min", total_time="10 min", tags=["side"], meals=["Brunch"])
    assert calls == [("GET", "/api/recipes", None), ("POST", "/api/recipes", {
        "name": "Garlic bread", "ingredients": NEW_LINES, "sections": NEW_SECTIONS, "serves": "2",
        "activeTime": "5 min", "totalTime": "10 min", "tags": ["side"], "meals": ["Brunch"]})]
    assert out == {"id": 9, "name": "Garlic bread", "sections": ["instructions"], "unknown_meals": ["Brunch"]}


def test_recipe_save_without_an_id_never_replaces(calls):
    calls.answers[("GET", "/api/recipes")] = RECIPES
    with pytest.raises(ToolError, match=r"already a recipe called Lentil soup \(id 4\)"):
        call("recipe_save", name="lentil SOUP", ingredients=NEW_LINES, sections=NEW_SECTIONS)
    assert [c[0] for c in calls] == ["GET"]


def test_recipe_save_with_an_id_replaces_that_recipe(calls):
    calls.answers[("PUT", "/api/recipes/4")] = {"recipe": SOUP, "unknownMeals": []}
    out = call("recipe_save", id=4, name="Lentil soup", ingredients=SOUP["ingredients"], sections=SOUP["sections"])
    assert calls == [("PUT", "/api/recipes/4", {"name": "Lentil soup", "ingredients": SOUP["ingredients"],
                                                "sections": SOUP["sections"]})]
    assert out == {"id": 4, "name": "Lentil soup", "sections": SOUP_TITLES}


def test_recipe_save_needs_a_name_and_a_buy_on_every_line(calls):
    with pytest.raises(ToolError, match="name"):
        call("recipe_save", name=" ", ingredients=NEW_LINES, sections=NEW_SECTIONS)
    with pytest.raises(ToolError, match="ingredient 2 needs buy"):
        call("recipe_save", name="Tea", ingredients=[{"text": "1 tea bag", "buy": "tea"}, {"text": "water"}],
             sections=[])
    assert calls == []


def test_recipe_append_adds_to_the_section_and_keeps_the_rest(calls):
    calls.answers[("GET", "/api/recipes/4")] = SOUP
    appended = "- 2026-02-01: 30 min, thicker\n  - used less water"
    sections = [SOUP["sections"][0], {"title": "log", "body": "- 2026-01-05: a little thin\n" + appended},
                SOUP["sections"][2]]
    calls.answers[("PUT", "/api/recipes/4")] = {"recipe": {**SOUP, "sections": sections}, "unknownMeals": []}
    out = call("recipe_append", id=4, section=" Log ", text=appended + "\n")
    sent = {k: v for k, v in SOUP.items() if k not in ("id", "typical")} | {"sections": sections}
    assert calls == [("GET", "/api/recipes/4", None), ("PUT", "/api/recipes/4", sent)]
    assert out == {"id": 4, "name": "Lentil soup", "sections": SOUP_TITLES, "section": "log", "created": False}


def test_recipe_append_fills_an_empty_section_without_a_blank_line(calls):
    calls.answers[("GET", "/api/recipes/4")] = SOUP
    calls.answers[("PUT", "/api/recipes/4")] = {"recipe": SOUP, "unknownMeals": []}
    call("recipe_append", id=4, section="notes", text="- note A")
    assert calls[-1][2]["sections"][2] == {"title": "notes", "body": "- note A"}


def test_recipe_append_adds_a_section_the_recipe_lacks(calls):
    calls.answers[("GET", "/api/recipes/4")] = SOUP
    sections = SOUP["sections"] + [{"title": "troubleshooting", "body": "- thin: simmer longer"}]
    calls.answers[("PUT", "/api/recipes/4")] = {"recipe": {**SOUP, "sections": sections}, "unknownMeals": []}
    out = call("recipe_append", id=4, section="troubleshooting", text="- thin: simmer longer")
    assert calls[-1][2]["sections"] == sections
    assert out["created"] is True and out["sections"] == SOUP_TITLES + ["troubleshooting"]


def test_recipe_append_needs_a_section_and_text(calls):
    with pytest.raises(ToolError, match="section"):
        call("recipe_append", id=4, section=" ", text="- x")
    with pytest.raises(ToolError, match="text"):
        call("recipe_append", id=4, section="log", text=" \n")
    assert calls == []


def test_there_is_no_tool_that_deletes_a_recipe():
    async def names():
        async with Client(cauldron_mcp.mcp) as client:
            return {t.name for t in await client.list_tools()}
    assert {n for n in asyncio.run(names()) if n.startswith("recipe_")} == {
        "recipe_list", "recipe_get", "recipe_save", "recipe_append"}


def answering(status, **kwargs):
    return httpx.MockTransport(lambda request: httpx.Response(status, **kwargs))


def test_request_passes_on_cauldrons_error_and_reads_an_empty_answer():
    with pytest.raises(ToolError, match="404: No item with that id."):
        asyncio.run(cauldron_mcp.request("DELETE", "/api/shopping/99",
                                         transport=answering(404, json={"error": "No item with that id."})))
    assert asyncio.run(cauldron_mcp.request("DELETE", "/api/shopping/3", transport=answering(204))) is None


def test_request_says_when_cauldron_is_down():
    def refuse(request):
        raise httpx.ConnectError("refused")
    with pytest.raises(ToolError, match="isn't reachable"):
        asyncio.run(cauldron_mcp.request("GET", "/api/shopping", transport=httpx.MockTransport(refuse)))
