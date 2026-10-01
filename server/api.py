"""The supported interface for other code on the server Mac (such as his private lessons job).

Import this module and nothing else from the connector: everything here keeps its signature, while the
rest of server/ can change. Run your code in this folder's environment (pyproject.toml), with the folder
on the path: `PYTHONPATH=~/Projects/life-mcp/server uv run --project ~/Projects/life-mcp/server python ...`
(add your own dependencies with --with).

Everything talks to the running Logseq app through its `logseq` CLI, so it raises
fastmcp.exceptions.ToolError when Logseq isn't running or its graph isn't open.

Importing api doesn't start the connector or mount any tool group.
"""
from typing import Any

from fastmcp.exceptions import ToolError

import memory_mcp
import people_data
import server


async def cli(*args: str, json_out: bool = False) -> Any:
    """Run `logseq <args> --graph=<his graph>` and return its output.

    Values must be passed as --opt=value. With json_out=True the CLI's JSON reply is parsed and its
    "data" returned (a ToolError if the CLI reports an error); otherwise the text output is returned."""
    return await server.cli(*args, json_out=json_out)


def edn(value: Any) -> str:
    """A Python value (dict, list, str, number, bool) written as EDN, for --update-properties and the like."""
    return server.edn(value)


async def ensure_properties(names: list[str]) -> None:
    """Create any of these Logseq properties that don't exist yet (as text properties)."""
    await server.ensure_properties(names)


async def target_args(page: str | None, parent_block_id: int | None, create_page: bool = False) -> str:
    """The `upsert block` target option for inserting a block: under parent_block_id if given, else on page,
    else on today's journal. Raises ToolError if the page is missing and create_page is False."""
    return await server.target_args(page, parent_block_id, create_page)


def memory() -> memory_mcp.Memory:
    """A handle on the shared memory page ("Claude memories"), with the same rules the memory_* tools use:
    await .recall(topic=None), .save(section, text, source, under=None, stage=None) and
    .update(entry_id, text=None, stage=None, remove=False). Text that reads like a command or holds a
    secret is refused with a ToolError."""
    return memory_mcp.Memory(server.cli, server.ensure_properties, server.edn)


async def people_note(person: str, text: str) -> str:
    """Add a fact to someone's Logseq person page (creating it if needed), as people_note does; the same
    guard as memory refuses commands and secrets. Returns the line to report."""
    return await people_data.PeopleData(cli=server.cli).note(person, text)


__all__ = ["ToolError", "cli", "edn", "ensure_properties", "target_args", "memory", "people_note"]
