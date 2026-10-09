import asyncio
import json
import shutil
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import grimoire_mcp


class FakeGrim:
    def __init__(self):
        self.calls: list[tuple[str, ...]] = []
        self.rows: list[dict] = []

    async def __call__(self, *args: str) -> str:
        self.calls.append(args)
        return json.dumps(self.rows) if args[0] == "query" else json.dumps({"ok": args[0]})


@pytest.fixture
def grim(monkeypatch):
    fake = FakeGrim()
    monkeypatch.setattr(grimoire_mcp, "run", fake)
    return fake


def call(name, **kw):
    async def go():
        async with Client(grimoire_mcp.mcp) as client:
            r = await client.call_tool(name, kw)
            return r.content[0].text if r.content else ""
    return asyncio.run(go())


def refused(name, **kw):
    with pytest.raises(ToolError) as e:
        call(name, **kw)
    return str(e.value)


def test_reads_map_to_grim_commands(grim):
    call("get_page", title="Focaccia")
    call("search", query="water", limit=5)
    call("get_today")
    assert grim.calls == [("page", "Focaccia"), ("search", "water", "--limit", "5"), ("today",)]


def test_text_that_looks_like_an_option_is_protected(grim):
    call("append", target="today", markdown="- -5 degrees")
    call("append", target="today", markdown="--note")
    assert grim.calls[0] == ("append", "today", "\u0001- -5 degrees")
    assert grim.calls[1] == ("append", "today", "\u0001--note")


def test_edits_are_refused_on_journal_pages(grim):
    grim.rows = [{"kind": "journal"}]
    for name, kw in (("edit_block", {"block_id": "b1", "text": "x"}), ("delete_block", {"block_id": "b1"}),
                     ("move_block", {"block_id": "b1", "page": "Other"})):
        assert "journal" in refused(name, **kw)
    assert all(c[0] == "query" for c in grim.calls)               # nothing was written


def test_edits_go_through_on_ordinary_pages(grim):
    grim.rows = [{"kind": "page"}]
    call("edit_block", block_id="b1", text="new text")
    call("delete_block", block_id="b1")
    assert ("edit", "b1", "new text") in grim.calls and ("delete", "b1") in grim.calls


def test_a_quote_in_a_block_id_cannot_break_the_guard_query(grim):
    grim.rows = [{"kind": "page"}]
    call("edit_block", block_id="x' OR '1'='1", text="t")
    assert "x' OR" not in grim.calls[0][1]


def test_insert_and_move_need_exactly_one_target(grim):
    assert "exactly one" in refused("insert", markdown="- a", after="x", page="P")
    assert "exactly one" in refused("insert", markdown="- a")
    grim.rows = [{"kind": "page"}]
    assert "exactly one" in refused("move_block", block_id="b1")


def test_card_tools(grim):
    call("cards_next", chapter="ch 5", limit=2)
    call("cards_review", card_id="c1", rating="good")
    assert grim.calls[0] == ("cards", "next", "--limit", "2", "--chapter", "ch 5")
    assert grim.calls[1] == ("cards", "review", "c1", "good")


def test_the_flashcard_review_skill_rides_in_cards_next(tmp_path):
    # The description is built at import from the private folder's skills, so import it fresh with a skill in place.
    import os
    import subprocess
    import sys
    skill = tmp_path / "skills" / "flashcard-review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: flashcard-review\ndescription: d\n---\n\n# Flashcard review\n\nSay Yep.\n")
    code = ("import asyncio, grimoire_mcp; tools = asyncio.run(grimoire_mcp.mcp.list_tools());"
            " print(next(t.description for t in tools if t.name == 'cards_next'))")
    out = subprocess.run([sys.executable, "-c", code], cwd=Path(grimoire_mcp.__file__).parent, capture_output=True,
                         text=True, check=True, env={**os.environ, "LIFE_MCP_PRIVATE": str(tmp_path)}).stdout.strip()
    assert out.startswith(grimoire_mcp.NEXT_DOC) and out.endswith("Say Yep.")
    assert "Follow his flashcard-review skill" in out and "no need to skill_load" in out


def test_cards_next_logs_the_skill_once_per_chat(grim, monkeypatch):
    import skill_tools
    import usage_log
    monkeypatch.setattr(skill_tools, "_logged", {})
    call("cards_next")
    call("cards_next")
    rows = [json.loads(line) for line in usage_log.PATH.read_text().splitlines()]
    assert [(r["tool"], r["skill"]) for r in rows] == [("skill_load", "flashcard-review")]


def test_delete_page_is_refused_on_journals_and_goes_through_otherwise(grim):
    grim.rows = [{"kind": "journal"}]
    assert "journal" in refused("delete_page", title="Oct 5th, 2026")
    assert "journal" in refused("delete_page", title="today")
    assert all(c[0] == "query" for c in grim.calls)               # nothing was deleted
    grim.rows = [{"kind": "page"}]
    call("delete_page", title="Old notes")
    assert ("delete-page", "Old notes") in grim.calls


def test_the_server_registers_the_expected_tools():
    names = {t.name for t in asyncio.run(grimoire_mcp.mcp.list_tools())}
    assert {"get_page", "append", "edit_block", "delete_block", "delete_page", "undo_claude", "cards_next", "cards_review", "query"} <= names


GRIM = shutil.which("grim") or str(Path.home() / ".local/bin/grim")


@pytest.mark.skipif(not Path(GRIM).exists(), reason="grim isn't installed here")
def test_round_trip_with_the_real_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIMOIRE_GRAPH", str(tmp_path / "g"))
    monkeypatch.setenv("GRIM_BIN", GRIM)
    call("append", target="Recipes", markdown="- focaccia #recipe\n  - 80% water")
    assert "80% water" in call("get_page", title="Recipes")
    assert json.loads(call("search", query="focaccia"))
    block = json.loads(call("append", target="Recipes", markdown="- to edit"))["blockIds"][0]
    call("edit_block", block_id=block, text="edited")
    assert "edited" in call("get_page", title="Recipes")
    call("undo_claude", last=1)
    assert "edited" not in call("get_page", title="Recipes")
    jid = json.loads(call("append", target="today", markdown="- my thought"))["blockIds"][0]
    assert "journal" in refused("edit_block", block_id=jid, text="changed")
    call("append", target="Scratch", markdown="- x")
    call("delete_page", title="Scratch")
    assert "not found" in refused("get_page", title="Scratch")
