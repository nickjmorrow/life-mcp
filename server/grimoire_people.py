"""Person pages in Grimoire, for people_data.PeopleData.

A person page is a page whose property block (`key:: value` lines, usually the first block) carries `tags:: [[person]]`;
birthday, phone, email, `keep in touch` and `alias` live on the same block. Notes are ordinary blocks on the page.
"""
import json
import re

from fastmcp.exceptions import ToolError

import grimoire_mcp

PAGES_SQL = (
    "SELECT p.id AS id, p.title AS title, b.text AS props "
    "FROM pages p JOIN blocks b ON b.page_id = p.id "
    "JOIN block_tags bt ON bt.block_id = b.id JOIN tags t ON t.id = bt.tag_id AND t.name_lower = 'person' "
    "WHERE b.parent_id IS NULL AND b.text LIKE '%tags::%' "
    "ORDER BY p.title_lower, b.order_key"
)


def parse_props(text: str) -> dict[str, str]:
    """`key:: value` lines → {key (lowercase): value}; [[links]] lose their brackets."""
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        if "::" not in line:
            continue
        key, _, value = line.partition("::")
        key, value = key.strip().lower(), value.strip()
        if key and key not in out:
            out[key] = re.sub(r"\[\[([^\]]*)\]\]", r"\1", value)
    return out


def outline(nodes: list[dict], depth: int = 0) -> list[str]:
    lines: list[str] = []
    for n in nodes:
        text = n.get("text", "").replace("\n", " ")
        if not re.fullmatch(r"(\s*[\w ]+::[^\n]*\s*)+", n.get("text", "") or "x") or depth > 0:
            lines.append("  " * depth + "- " + text)
        lines += outline(n.get("children", []), depth + 1)
    return lines


class GrimoirePages:
    """The same five calls as people_data.LogseqPages, over the hub graph's `grim`."""
    name = "Grimoire"

    def __init__(self, run=None):
        self.run = run or grimoire_mcp.run

    async def pages(self) -> list[dict]:
        rows = json.loads(await self.run("query", PAGES_SQL))
        out: dict[str, dict] = {}
        for r in rows:
            if r["id"] in out:
                continue
            props = parse_props(r.get("props") or "")
            aliases = [a.strip() for a in re.split(r",", props.get("alias", "")) if a.strip()]
            out[r["id"]] = {"id": r["id"], "title": r["title"], "props": {k: v for k, v in props.items() if k not in ("tags", "alias")},
                            "aliases": aliases}
        # Logseq listed an alias's target only; a page that is another page's alias is the same person
        alias_names = {a.lower() for p in out.values() for a in p["aliases"]}
        return [p for p in out.values() if p["title"].lower() not in alias_names]

    async def text(self, title: str) -> str:
        page = json.loads(await self.run("page", grimoire_mcp.text_arg(title)))
        return "\n".join(outline(page.get("blocks", []))) or "(no notes yet)"

    async def exists(self, title: str) -> bool:
        try:
            await self.run("page", grimoire_mcp.text_arg(title))
            return True
        except ToolError as e:
            if "not found" in str(e).lower():
                return False
            raise

    async def create_person(self, title: str) -> None:
        await self.run("append", grimoire_mcp.text_arg(title), "- tags:: [[person]]")

    async def add_note(self, title: str, text: str) -> None:
        await self.run("append", grimoire_mcp.text_arg(title), grimoire_mcp.text_arg("- " + text.replace("\n", " ")))
