"""cards_*: spaced-repetition review of the Logseq graph's flashcards, so Claude can quiz him.

A card is a block tagged with Logseq's built-in Card class: the block's text is the front, its child
blocks are the back. Review state lives on the card as Logseq's own FSRS properties (see fsrs.py), so
reviews here and in Logseq's own review screen share one schedule.

The review loop: cards_next shows only the front (he answers in his head), cards_answer shows the back,
cards_rate records again / hard / good / easy and reschedules the card.
"""
from __future__ import annotations

import json
import re
import time
from typing import Annotated, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

import fsrs

CARDS_QUERY = """[:find ?b ?title ?page ?state ?due
 :where [?t :db/ident :logseq.class/Card] [?b :block/tags ?t] [?b :block/title ?title]
        [?b :block/page ?p] [?p :block/title ?page]
        [(missing? $ ?b :logseq.property/deleted-at)] [(missing? $ ?p :logseq.property/deleted-at)]
        [(get-else $ ?b :logseq.property.fsrs/state "") ?state]
        [(get-else $ ?b :logseq.property.fsrs/due 0) ?due]]"""
# Every block on a page that has cards, with its parent: enough to walk a card up to its chapter heading.
TREE_QUERY = """[:find ?b ?parent ?title
 :where [?t :db/ident :logseq.class/Card] [?c :block/tags ?t] [?c :block/page ?p]
        [?b :block/page ?p] [?b :block/parent ?parent] [?b :block/title ?title]]"""
CHILDREN_QUERY = """[:find ?title ?order :in $ ?parent
 :where [?c :block/parent ?parent] [?c :block/title ?title] [?c :block/order ?order]]"""
INSTRUCTIONS = (
    " The cards_* tools quiz Nicholas on his Logseq flashcards with spaced repetition: cards_next gives a"
    " front (read only that, then wait), cards_answer the back, cards_rate takes again/hard/good/easy."
)
READ = {"readOnlyHint": True}
WRITE = {"readOnlyHint": False, "destructiveHint": False}
Rating = Literal["again", "hard", "good", "easy"]
PageArg = Annotated[str | None, Field(description="Only this page's cards (e.g. a book)")]
ChapterArg = Annotated[str | None, Field(
    description="Only cards under this chapter or heading: 'ch 5' (or 'chapter 5', '5') matches the heading 'ch 5 - …';"
                " other text matches any heading containing it, e.g. 'replication'")]


def now_ms() -> int:
    return int(time.time() * 1000)


def _norm(text: str) -> str:
    """'Chapter 5', 'ch.5', 'CH 5 - Replication' → 'ch 5…', so spoken and written chapter names compare equal."""
    text = re.sub(r"\bchapter\b", "ch", text.strip().lower())
    text = re.sub(r"\bch\.?\s*(\d+)", r"ch \1", text)
    return re.sub(r"\s+", " ", text)


def _is_chapter(title: str) -> bool:
    return re.match(r"ch \d+(?!\d)", _norm(title)) is not None


def _matches(title: str, want: str) -> bool:
    """'ch 5' (or '5') matches a heading starting 'ch 5' but not 'ch 50'; any other text matches a heading containing it."""
    t, w = _norm(title), _norm(want)
    if w.isdigit():
        w = f"ch {w}"
    if re.fullmatch(r"ch \d+", w):
        return re.match(re.escape(w) + r"(?!\d)", t) is not None
    return w in t


def _edn(value) -> str:
    """EDN for Logseq's FSRS properties: keyword keys (including :logseq/last-rating), JSON-style values."""
    if isinstance(value, dict):
        return "{" + " ".join(f":{k} {_edn(v)}" for k, v in value.items()) + "}"
    return json.dumps(value)


class Deck:
    def __init__(self, cli, clock=now_ms):
        self.cli, self.clock = cli, clock

    async def cards(self, page: str | None = None, chapter: str | None = None, with_chapters: bool = False) -> list[dict]:
        rows = (await self.cli("query", f"--query={CARDS_QUERY}", json_out=True))["result"]
        cards = [{"id": b, "front": title, "page": pg, "chapter": None, "card": fsrs.Card.from_logseq(st or None, due)}
                 for b, title, pg, st, due in rows]
        if page:
            cards = [c for c in cards if c["page"].lower() == page.strip().lower()]
            if not cards:
                raise ToolError(f"No flashcards on a page called {page!r}. Use cards_status to see the pages.")
        if chapter or with_chapters:
            await self._add_chapters(cards)
        if chapter:
            matched = [c for c in cards if any(_matches(h, chapter) for h in c["headings"])]
            if not matched:
                names = sorted({c["chapter"] for c in cards if c["chapter"]}, key=_chapter_key)
                hint = f" Chapters: {', '.join(names)}." if names else ""
                raise ToolError(f"No flashcards under a chapter or heading matching {chapter!r}.{hint}")
            cards = matched
        return cards

    async def _add_chapters(self, cards: list[dict]) -> None:
        """Give each card its headings (every ancestor block's text, nearest first) and its chapter: the
        nearest heading that starts 'ch N'."""
        rows = (await self.cli("query", f"--query={TREE_QUERY}", json_out=True))["result"]
        parent = {b: p for b, p, _ in rows}
        title = {b: t for b, _, t in rows}
        for c in cards:
            heads, b = [], parent.get(c["id"])
            while b in title and len(heads) < 50:
                heads.append(title[b])
                b = parent.get(b)
            c["headings"] = heads
            c["chapter"] = next((_short(h) for h in heads if _is_chapter(h)), None)

    def split(self, cards: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
        """(due now, new, later) — due ordered most overdue first, new in page order."""
        now = self.clock()
        due = sorted((c for c in cards if c["card"].state != "new" and c["card"].due <= now), key=lambda c: c["card"].due)
        new = sorted((c for c in cards if c["card"].state == "new"), key=lambda c: (c["page"], c["id"]))
        later = sorted((c for c in cards if c["card"].state != "new" and c["card"].due > now), key=lambda c: c["card"].due)
        return due, new, later

    async def back(self, card_id: int) -> str:
        rows = (await self.cli("query", f"--query={CHILDREN_QUERY}", f"--inputs=[{int(card_id)}]", json_out=True))["result"]
        return "\n".join(title for title, _ in sorted(rows, key=lambda r: str(r[1]))) or "(this card has no answer blocks)"

    async def find(self, card_id: int) -> dict:
        for c in await self.cards():
            if c["id"] == int(card_id):
                return c
        raise ToolError(f"No flashcard with id {card_id}.")

    async def rate(self, card_id: int, rating: str) -> tuple[dict, fsrs.Card]:
        c = await self.find(card_id)
        now = self.clock()
        new = fsrs.review(c["card"], rating, now)
        props = {"logseq.property.fsrs/state": new.to_logseq(), "logseq.property.fsrs/due": new.due}
        await self.cli("upsert", "block", f"--id={int(card_id)}", f"--update-properties={_edn(props)}", json_out=True)
        return c, new


def _short(heading: str) -> str:
    """A chapter heading's name without the notes after it: 'ch 5 - replication', not a paragraph."""
    return heading.split("\n")[0][:60].strip()


def _chapter_key(name: str):
    m = re.match(r"ch (\d+)", _norm(name))
    return (int(m.group(1)) if m else 10**6, name)


def build(cli, clock=now_ms) -> FastMCP:
    mcp = FastMCP("Cards")
    deck = Deck(cli, clock)

    def card_line(c: dict, left: str) -> str:
        where = f"{c['page']} › {c['chapter']}" if c.get("chapter") else c["page"]
        return f"[{c['id']}] {c['front']}\n(from {where}; {left})"

    @mcp.tool(annotations=READ)
    async def cards_status(page: PageArg = None, chapter: ChapterArg = None) -> str:
        """How many flashcards are due now and new, per page (and per chapter when a page or chapter is
        given), and when the next one comes due."""
        due, new, later = deck.split(await deck.cards(page, chapter, with_chapters=bool(page)))
        groups: dict[str, list[int]] = {}
        for bucket, i in ((due, 0), (new, 1), (later, 2)):
            for c in bucket:
                groups.setdefault(c["page"], [0, 0, 0])[i] += 1
                if c["chapter"]:
                    groups.setdefault(f"{c['page']} › {c['chapter']}", [0, 0, 0])[i] += 1
        lines = [f"{len(due)} due now, {len(new)} new, {len(later)} scheduled later."]
        if later and not due:
            lines.append(f"Next due {fsrs.describe_wait(later[0]['card'].due, deck.clock())}.")
        order = sorted(groups, key=lambda g: (g.split(" › ")[0], _chapter_key(g.split(" › ")[1]) if " › " in g else (-1, "")))
        lines += [f"{'  ' if ' › ' in g else ''}- {g}: {d} due, {n} new, {l} later" for g in order for d, n, l in [groups[g]]]
        return "\n".join(lines)

    @mcp.tool(annotations=READ)
    async def cards_next(
        page: PageArg = None,
        chapter: ChapterArg = None,
        include_new: Annotated[bool, Field(description="Show a new card when nothing is due")] = True,
    ) -> str:
        """The next flashcard to review: ONLY its front and id. Read the front to Nicholas and wait for him to
        answer before calling cards_answer. Due cards come first, then new ones."""
        due, new, later = deck.split(await deck.cards(page, chapter))
        queue = due + (new if include_new else [])
        if not queue:
            nxt = f" Next due {fsrs.describe_wait(later[0]['card'].due, deck.clock())}." if later else ""
            return "Nothing to review right now." + nxt
        c = queue[0]
        left = f"{len(due)} due, {len(new)} new left" if c in due else f"new card; {len(new)} new left"
        return card_line(c, left)

    @mcp.tool(annotations=READ)
    async def cards_answer(card_id: Annotated[int, Field(description="The id from cards_next")]) -> str:
        """The back of a flashcard (its answer). Call after Nicholas has answered in his head."""
        c = await deck.find(card_id)
        return f"{c['front']}\n— answer —\n{await deck.back(card_id)}"

    @mcp.tool(annotations=WRITE)
    async def cards_rate(card_id: Annotated[int, Field(description="The id from cards_next")], rating: Rating) -> str:
        """Record how well Nicholas remembered the card (again = forgot, hard, good, easy) and reschedule it
        with FSRS. Then call cards_next for the next card."""
        c, new = await deck.rate(card_id, rating)
        return f"Rated {rating}: next review {fsrs.describe_wait(new.due, deck.clock())}."

    return mcp
