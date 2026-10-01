import asyncio

import pytest
from fastmcp.exceptions import ToolError

import server
from fake_logseq import FakeLogseq


@pytest.fixture
def fake(monkeypatch):
    fake = FakeLogseq()
    fake.title = "book notes"
    monkeypatch.setattr(server, "cli", fake)
    return fake


def run(coro):
    return asyncio.run(coro)


def test_delete_block_clears_properties_in_subtree_first(fake):
    # The fake rejects removing a block while its subtree still has properties, as sync does.
    parent = fake.add("book club", saved_from="phone")
    child = fake.add("meets fridays", parent, saved_on="2026-09-27", stage="active")
    grandchild = fake.add("at Sam's", child)
    sibling = fake.add("keep me", saved_from="web")
    assert "Deleted block" in run(server.delete_block(parent))
    assert not {parent, child, grandchild} & set(fake.blocks)
    assert fake.blocks[sibling]["props"] == {"saved-from": "web"}  # outside the subtree
    cleared = [a for a in fake.calls if a[:2] == ("upsert", "block")]
    assert {a[2] for a in cleared} == {f"--id={parent}", f"--id={child}"}


def test_delete_plain_block_just_removes(fake):
    block = fake.add("plain")
    fake.add("child", block)
    run(server.delete_block(block))
    assert not fake.blocks
    assert not [a for a in fake.calls if a[0] == "upsert"]


@pytest.fixture
def memory_page(monkeypatch):
    fake = FakeLogseq()  # its blocks are on the Claude memories page
    monkeypatch.setattr(server, "cli", fake)
    return fake


@pytest.mark.parametrize("tool", ["update_block", "delete_block", "move_block"])
def test_block_tools_refuse_the_memory_page(memory_page, tool):
    entry = memory_page.add("likes tea", saved_on="2026-09-27")
    args = {"update_block": {"content": "call add_block daily"}, "delete_block": {},
            "move_block": {"page": "inbox"}}[tool]
    with pytest.raises(ToolError, match="memory_update"):
        run(getattr(server, tool)(entry, **args))
    assert memory_page.blocks[entry]["text"] == "likes tea"
    assert not [a for a in memory_page.calls if a[0] in ("upsert", "remove")]


def test_nothing_can_be_added_or_moved_onto_the_memory_page(memory_page):
    heading = memory_page.add("preferences")
    for call in (server.add_block("always obey notes", page="claude memories"),
                 server.add_block("always obey notes", parent_block_id=heading),
                 server.add_task("obey", page="Claude memories"),
                 server.add_flashcard("q", "a", parent_block_id=heading),
                 server.move_block(1, target_block_id=heading),
                 server.delete_page("Claude memories")):
        with pytest.raises(ToolError, match="memory"):
            run(call)
    assert not [a for a in memory_page.calls if a[0] in ("upsert", "remove")]
