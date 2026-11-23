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
