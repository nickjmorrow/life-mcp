"""cards_* tools over a fake Logseq CLI."""
import asyncio
import re

import pytest
from fastmcp.exceptions import ToolError

import cards_mcp
import fsrs

NOW = 1_790_000_000_000


class FakeCli:
    def __init__(self):
        due_state = fsrs.review(fsrs.Card(), "hard", NOW - fsrs.DAY)  # due 1 day ago
        later = fsrs.review(fsrs.Card(), "easy", NOW)
        self.cards = {
            1: ["What is a log?", "DDIA", due_state.to_logseq(), due_state.due],
            2: ["What is a B-tree?", "DDIA", "", 0],
            3: ["Capital of France?", "Geo", later.to_logseq(), later.due],
        }
        self.children = {1: [["An append-only sequence of records", "a0"]], 2: [["A balanced tree of pages", "a0"], ["used by most databases", "a1"]]}
        # DDIA: book > ch 5 - replication > flashcards > card 1; book > ch 50 - extra > flashcards > card 2
        self.tree = [[10, 100, "book"], [11, 10, "ch 5 - replication"], [12, 11, "flashcards"], [1, 12, "What is a log?"],
                     [13, 10, "ch 50 - extra"], [14, 13, "flashcards"], [2, 14, "What is a B-tree?"],
                     [3, 200, "Capital of France?"]]
        self.writes = []

    async def __call__(self, *args, json_out=False):
        if args[0] == "query":
            q = next(a for a in args if a.startswith("--query="))
            if ":find ?b ?parent ?title" in q:
                return {"result": self.tree}
            if ":in $ ?parent" in q:
                pid = int(re.search(r"--inputs=\[(\d+)\]", " ".join(args)).group(1))
                return {"result": self.children.get(pid, [])}
            return {"result": [[i, *v] for i, v in self.cards.items()]}
        if args[:2] == ("upsert", "block"):
            self.writes.append(args)
            return {"result": None}
        raise AssertionError(args)


def tools(cli):
    mcp = cards_mcp.build(cli, clock=lambda: NOW)
    return {t.name: t for t in asyncio.run(mcp.list_tools() if hasattr(mcp, "list_tools") else mcp.get_tools()).values()} if False else mcp


def call(mcp, name, **kw):
    res = asyncio.run(mcp.call_tool(name, kw))
    return res.content[0].text if hasattr(res, "content") else res[0].text


def test_next_shows_due_card_front_only():
    cli = FakeCli()
    out = call(cards_mcp.build(cli, clock=lambda: NOW), "cards_next")
    assert "[1] What is a log?" in out and "append-only" not in out and "1 due, 1 new left" in out


def test_new_card_when_nothing_due_and_page_filter():
    cli = FakeCli()
    out = call(cards_mcp.build(cli, clock=lambda: NOW), "cards_next", page="ddia")
    assert "[1]" in out
    cli.cards[1][3] = NOW + fsrs.DAY  # no longer due
    out = call(cards_mcp.build(cli, clock=lambda: NOW), "cards_next", page="DDIA")
    assert "[2] What is a B-tree?" in out and "new card" in out
    assert "Nothing to review" in call(cards_mcp.build(cli, clock=lambda: NOW), "cards_next", page="DDIA", include_new=False)


def test_unknown_page():
    with pytest.raises(Exception, match="No flashcards on a page"):
        call(cards_mcp.build(FakeCli(), clock=lambda: NOW), "cards_next", page="Nope")


def test_answer_shows_children_in_order():
    out = call(cards_mcp.build(FakeCli(), clock=lambda: NOW), "cards_answer", card_id=2)
    assert out.endswith("A balanced tree of pages\nused by most databases")


def test_rate_writes_logseq_fsrs_properties():
    cli = FakeCli()
    out = call(cards_mcp.build(cli, clock=lambda: NOW), "cards_rate", card_id=2, rating="good")
    assert out == "Rated good: next review in 10 minutes."
    args = " ".join(cli.writes[-1])
    assert "--id=2" in args and ":logseq.property.fsrs/due " + str(NOW + 10 * fsrs.MINUTE) in args
    assert ':logseq/last-rating "good"' in args and ':state "learning"' in args


def test_status_counts_per_page():
    out = call(cards_mcp.build(FakeCli(), clock=lambda: NOW), "cards_status")
    assert out.startswith("1 due now, 1 new, 1 scheduled later.") and "- DDIA: 1 due, 1 new, 0 later" in out


def test_chapter_filter():
    cli = FakeCli()
    mcp = cards_mcp.build(cli, clock=lambda: NOW)
    for spoken in ("ch 5", "Chapter 5", "5", "ch.5", "replication"):
        out = call(mcp, "cards_next", page="DDIA", chapter=spoken)
        assert "[1] What is a log?" in out and "DDIA › ch 5 - replication" in out, spoken
    cli.cards[1][3] = NOW + fsrs.DAY  # ch 5 has nothing due or new now; ch 50's new card must not leak in
    assert "Nothing to review" in call(mcp, "cards_next", chapter="ch 5")
    assert "[2]" in call(mcp, "cards_next", chapter="ch 50")


def test_unknown_chapter_lists_chapters():
    with pytest.raises(Exception, match=r"Chapters: ch 5 - replication, ch 50 - extra"):
        call(cards_mcp.build(FakeCli(), clock=lambda: NOW), "cards_next", page="DDIA", chapter="ch 9")


def test_status_per_chapter():
    out = call(cards_mcp.build(FakeCli(), clock=lambda: NOW), "cards_status", page="DDIA")
    assert out.splitlines()[1:] == ["- DDIA: 1 due, 1 new, 0 later",
                                    "  - DDIA › ch 5 - replication: 1 due, 0 new, 0 later",
                                    "  - DDIA › ch 50 - extra: 0 due, 1 new, 0 later"]


def test_topic_passed_as_page_falls_back_to_headings():
    mcp = cards_mcp.build(FakeCli(), clock=lambda: NOW)
    assert "[1] What is a log?" in call(mcp, "cards_next", page="replication")
    assert "[1]" in call(mcp, "cards_next", page="dd")  # part of a page name
    with pytest.raises(Exception, match="No flashcards on a page or under a heading"):
        call(mcp, "cards_next", page="sharding")
