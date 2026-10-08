"""The supported interface for other code on the server Mac (such as his private lessons job).

Import this module and nothing else from the connector: everything here keeps its signature, while the
rest of server/ can change. Run your code in this folder's environment (pyproject.toml), with the folder
on the path: `PYTHONPATH=~/Projects/life-mcp/server uv run --project ~/Projects/life-mcp/server python ...`
(add your own dependencies with --with).

memory() is files on this Mac; people_note() writes to Grimoire through its `grim` CLI and raises
fastmcp.exceptions.ToolError when that fails.

Importing api doesn't start the connector or mount any tool group.
"""
from fastmcp.exceptions import ToolError

import grimoire_people
import memory_mcp
import memory_store
import people_data


def memory() -> memory_store.MemoryStore:
    """The shared memory: Markdown topic files in the harness repo's memory/ folder, written on the dev branch (one
    commit each, pushed soon after), with the same guard the memory_* tools use.
    Methods (all plain, none async): .topics(), .entries(topic), .render(topic), .size(topic) (characters of entries),
    .search(word), .save(text, topic, source, under=None, review=None, similar=None), .update(entry_id, text=None,
    remove=False, review=None), .archive(entry_id, why), .move(entry_id, topic), .merge(keep_id, merge_id, text),
    .split(topic, groups, about), .review_due(today), .head() (the repo's HEAD sha), .last_commit(path) and
    .commit_files(files, message). Text that names a connector tool, reads like a command or a rule for Claude, or holds
    a secret is refused with a ToolError; every other refusal (an unknown topic, a full core, a bad date) is a
    memory_store.MemoryError_, a ValueError."""
    return memory_mcp.write_store()


async def people_note(person: str, text: str) -> str:
    """Add a fact to someone's person page in Grimoire (creating it if needed), as people_note does; the same
    guard as memory refuses commands and secrets. Returns the line to report."""
    return await people_data.PeopleData(backend=grimoire_people.GrimoirePages()).note(person, text)


__all__ = ["ToolError", "memory", "people_note"]
