import asyncio
import json

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import hoard_mcp

LIBRARY = {
    "titles": [
        {"pageId": "w-1", "name": "Nope", "year": 2022, "type": "movie", "status": "want", "tags": ["Rainy Day"],
         "summary": "long text", "notesText": ""},
        {"pageId": "abc", "name": "Get Out", "year": 2017, "type": "movie", "status": "done", "tags": ["Rainy Day"],
         "rating": 5, "finished": "2026-01-02", "notesText": "loved it"},
        {"pageId": "def", "name": "Severance", "type": "show", "status": "doing", "tags": [], "progress": "S2E4"},
    ],
    "tags": [{"title": "Rainy Day", "group": "Vibe", "count": 2}],
    "jellyfinUp": True,
}


class Calls(list):
    answers: dict


@pytest.fixture
def calls(monkeypatch):
    """Record every API call; answer from `calls.answers` by (method, path), else the library."""
    seen = Calls()

    async def fake(method, path, body=None, timeout=hoard_mcp.TIMEOUT_S, transport=None):
        seen.append((method, path, body, timeout))
        return fake.answers.get((method, path), LIBRARY)

    fake.answers = {}
    monkeypatch.setattr(hoard_mcp, "request", fake)
    seen.answers = fake.answers
    return seen


def call(tool, **args):
    async def go():
        async with Client(hoard_mcp.mcp) as client:
            return (await client.call_tool(tool, args)).content[0].text
    return json.loads(asyncio.run(go()))


def test_library_filters_and_trims(calls):
    out = call("library", status="want")
    assert out["count"] == 1 and out["titles"] == [
        {"pageId": "w-1", "name": "Nope", "year": 2022, "type": "movie", "status": "want", "tags": ["Rainy Day"]}]
    assert [t["name"] for t in call("library", tag="rainy day")["titles"]] == ["Nope", "Get Out"]
    assert [t["name"] for t in call("library", type="show")["titles"]] == ["Severance"]
    assert [t["name"] for t in call("library", name="get")["titles"]] == ["Get Out"]


def test_add_defaults_to_the_want_list(calls):
    calls.answers[("POST", "/api/titles")] = {"title": {**LIBRARY["titles"][0]}, "existed": False}
    out = call("add", name="Nope", type="movie", year=2022, tags=["Rainy Day"])
    assert calls[0][:3] == ("POST", "/api/titles", {"name": "Nope", "type": "movie", "year": 2022,
                                                    "fields": {"status": "want"}, "tags": ["Rainy Day"]})
    assert out["title"]["pageId"] == "w-1" and out["existed"] is False


def test_add_with_rec(calls):
    calls.answers[("POST", "/api/titles")] = {"title": LIBRARY["titles"][0], "existed": True}
    call("add", name="Nope", type="movie", rec="Claude")
    assert calls[0][2] == {"name": "Nope", "type": "movie", "fields": {"status": "want", "rec": "Claude"}, "tags": []}


def test_set_sends_only_what_changes(calls):
    calls.answers[("PATCH", "/api/titles/w-1")] = {**LIBRARY["titles"][1], "pageId": "new"}
    out = call("set", page_id="w-1", status="done", rating=4)
    assert calls[0][:3] == ("PATCH", "/api/titles/w-1", {"fields": {"status": "done", "rating": "4"}})
    assert out["pageId"] == "new"
    call("set", page_id="w-1", tags=["A", "B"])
    assert calls[1][2] == {"tags": ["A", "B"]}


def test_set_sends_fields_and_tags_in_one_patch(calls):
    call("set", page_id="abc", status="doing", tags=["A"])
    assert [c[:3] for c in calls] == [("PATCH", "/api/titles/abc", {"fields": {"status": "doing"}, "tags": ["A"]})]


def test_set_needs_a_change_and_a_plain_id(calls):
    with pytest.raises(ToolError, match="Nothing to change"):
        call("set", page_id="w-1")
    with pytest.raises(ToolError, match="page_id must be"):
        call("set", page_id="../suggest", rating=3)
    with pytest.raises(ToolError):
        call("set", page_id="w-1", rating=6)
    assert calls == []


def test_note_adds_a_note_or_edits_one(calls):
    calls.answers[("POST", "/api/titles/abc/notes")] = {"id": "n1", "text": "Great second half."}
    out = call("note", page_id="abc", text="Great second half.")
    assert calls[0][:3] == ("POST", "/api/titles/abc/notes", {"text": "Great second half."})
    assert out == {"id": "n1", "text": "Great second half."}

    calls.answers[("PUT", "/api/notes/n1")] = {"ok": True}
    out = call("note", page_id="abc", text="Great second half, weak ending.", note_id="n1")
    assert calls[1][:3] == ("PUT", "/api/notes/n1", {"text": "Great second half, weak ending."})
    assert out == {"id": "n1", "text": "Great second half, weak ending."}


def test_note_needs_text_and_plain_ids(calls):
    with pytest.raises(ToolError, match="text"):
        call("note", page_id="abc", text="  ")
    with pytest.raises(ToolError, match="page_id must be"):
        call("note", page_id="../x", text="hi")
    with pytest.raises(ToolError, match="note_id must be"):
        call("note", page_id="abc", text="hi", note_id="../x")
    assert calls == []


def test_get_returns_one_title_with_its_notes_and_details(calls):
    calls.answers[("GET", "/api/titles/abc")] = {
        **LIBRARY["titles"][1], "propBlockId": "", "summary": "A photographer meets the family.", "director": ["Jordan Peele"],
        "cast": ["Daniel Kaluuya"], "notes": [{"id": "n1", "text": "loved it"}], "notesText": "loved it", "createdAt": 5}
    out = call("get", page_id="abc")
    assert calls[0][:2] == ("GET", "/api/titles/abc")
    assert out == {"pageId": "abc", "name": "Get Out", "year": 2017, "type": "movie", "status": "done", "tags": ["Rainy Day"],
                   "rating": 5, "finished": "2026-01-02", "summary": "A photographer meets the family.",
                   "director": ["Jordan Peele"], "cast": ["Daniel Kaluuya"], "notes": [{"id": "n1", "text": "loved it"}]}
    with pytest.raises(ToolError, match="page_id must be"):
        call("get", page_id="../x")


def test_suggest_from_one_source(calls):
    calls.answers[("POST", "/api/suggest")] = {"suggestions": [
        {"id": "s1", "name": "Us", "year": 2019, "type": "movie", "why": "same director", "source": "Rainy Day",
         "tag": "Rainy Day", "fields": {"imdb": "tt1"}, "state": "new", "createdAt": 1}]}
    out = call("suggest", tag="Rainy Day", count=3)
    method, path, body, timeout = calls[0]
    assert (method, path, body) == ("POST", "/api/suggest", {"tag": "Rainy Day", "count": 3})
    assert timeout == hoard_mcp.SUGGEST_TIMEOUT_S
    assert out["suggestions"] == [{"id": "s1", "name": "Us", "year": 2019, "type": "movie", "why": "same director",
                                   "source": "Rainy Day", "tag": "Rainy Day"}]
    with pytest.raises(ToolError, match="exactly one"):
        call("suggest", tag="A", person="B")
    with pytest.raises(ToolError, match="exactly one"):
        call("suggest")


def run(*args, **kw):
    return asyncio.run(hoard_mcp.request(*args, **kw))


def test_request_sends_json_and_reports_hoards_error(monkeypatch):
    monkeypatch.setenv("HOARD_URL", "http://127.0.0.1:9")
    seen = []

    def handle(request):
        seen.append(request)
        if request.method == "PATCH":
            return httpx.Response(400, json={"error": "rating must be a whole number from 1 to 5"})
        return httpx.Response(200, json={"ok": True})

    assert run("POST", "/api/titles", {"name": "x"}, transport=httpx.MockTransport(handle)) == {"ok": True}
    assert str(seen[0].url) == "http://127.0.0.1:9/api/titles"
    assert seen[0].headers["content-type"] == "application/json"
    with pytest.raises(ToolError, match="400: rating must be"):
        run("PATCH", "/api/titles/w-1", {}, transport=httpx.MockTransport(handle))


def test_request_when_hoard_is_down():
    def refuse(request):
        raise httpx.ConnectError("refused")
    with pytest.raises(ToolError, match="isn't reachable"):
        run("GET", "/api/library", transport=httpx.MockTransport(refuse))
