import asyncio
import json
import shutil
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

import grimoire_mcp
import grimoire_people
import people_data

GRIM = shutil.which("grim") or str(Path.home() / ".local/bin/grim")


def test_property_lines_become_a_dict_without_brackets():
    props = grimoire_people.parse_props("keep in touch:: monthly\nbirthday:: [[May 5th, 1990]]\ntags:: [[person]], [[friend]]\nalias:: Andrew, Drew")
    assert props["keep in touch"] == "monthly"
    assert props["birthday"] == "May 5th, 1990"
    assert props["tags"] == "person, friend" and props["alias"] == "Andrew, Drew"


class FakeRun:
    def __init__(self, rows=None, page=None, fail=None):
        self.calls, self.rows, self.page, self.fail = [], rows or [], page, fail

    async def __call__(self, *args):
        self.calls.append(args)
        if self.fail:
            raise ToolError(self.fail)
        if args[0] == "query":
            return json.dumps(self.rows)
        if args[0] == "page":
            return json.dumps(self.page or {"blocks": []})
        return "ok"


def test_pages_come_from_the_property_block_with_aliases_and_props():
    run = FakeRun(rows=[{"id": "p1", "title": "andrew d", "props": "keep in touch:: never\nalias:: Andrew\ntags:: [[person]]"}])
    pages = asyncio.run(grimoire_people.GrimoirePages(run).pages())
    assert pages == [{"id": "p1", "title": "andrew d", "props": {"keep in touch": "never"}, "aliases": ["Andrew"]}]


def test_a_page_that_is_another_pages_alias_is_not_listed_twice():
    run = FakeRun(rows=[{"id": "p1", "title": "Andrew", "props": "tags:: [[person]]"},
                        {"id": "p2", "title": "andrew d", "props": "alias:: Andrew\ntags:: [[person]]"}])
    assert [p["title"] for p in asyncio.run(grimoire_people.GrimoirePages(run).pages())] == ["andrew d"]


def test_notes_text_skips_the_property_block_and_keeps_the_outline():
    page = {"blocks": [{"text": "tags:: [[person]]"}, {"text": "likes climbing", "children": [{"text": "bouldering"}]}]}
    text = asyncio.run(grimoire_people.GrimoirePages(FakeRun(page=page)).text("Sam"))
    assert text == "- likes climbing\n  - bouldering"


def test_missing_pages_are_reported_as_missing_other_errors_are_not():
    assert asyncio.run(grimoire_people.GrimoirePages(FakeRun(fail="page not found: Sam")).exists("Sam")) is False
    with pytest.raises(ToolError):
        asyncio.run(grimoire_people.GrimoirePages(FakeRun(fail="grim exploded")).exists("Sam"))


def test_creating_and_noting_append_to_the_page():
    run = FakeRun()
    b = grimoire_people.GrimoirePages(run)
    asyncio.run(b.create_person("Sam"))
    asyncio.run(b.add_note("Sam", "likes tea\nwith lemon"))
    assert run.calls[0] == ("append", "Sam", "- tags:: [[person]]")
    assert run.calls[1] == ("append", "Sam", "\u0001- likes tea with lemon")


@pytest.mark.skipif(not Path(GRIM).exists(), reason="grim isn't installed here")
def test_people_data_round_trip_on_a_real_graph(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIMOIRE_GRAPH", str(tmp_path / "g"))
    monkeypatch.setenv("GRIM_BIN", GRIM)
    data = people_data.PeopleData(cli=None, backend=grimoire_people.GrimoirePages(), snap_dir=str(tmp_path / "snap"), refresh=lambda: None)
    data._checked = True

    async def go():
        await data.note("Sam Rivera", "likes bouldering")
        pages = await data.backend.pages()
        assert [p["title"] for p in pages] == ["Sam Rivera"]
        text = await data.backend.text("Sam Rivera")
        assert "likes bouldering" in text and "tags::" not in text
        assert await data.backend.exists("Sam Rivera") and not await data.backend.exists("Nobody Here")
    asyncio.run(go())
