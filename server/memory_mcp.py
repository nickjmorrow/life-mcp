"""Shared memory for every Claude (phone, web, Claude Code): Markdown topic files in the harness repo's memory/ folder.

memory_store.py holds the files and every rule about them (ids, review dates, near-duplicates, the core budget, one
commit per change). This module is what sits on top: the three tools, and the guard in front of the store, which every
text entering memory passes.

Reads come from the clean copy of the repo's main branch (private.DIR), so what Claude knows is what Nicholas merged.
Writes go to the working copy on dev (WORKING_COPY/memory), one commit each, then a background push; a saved fact
reaches other chats once he merges the open changes. A recall with no topic returns CONTEXT.md (context.py).
"""
import asyncio
import re
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Any, Callable, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

import context
import memory_store
import private
import usage_log
from context import DATA_HEADER  # topic reads open with it too; CONTEXT.md (context.py) has it above the core facts
from memory_store import MemoryError_, MemoryStore

# Memory comes back at the start of every chat, so text planted there (say, from a message someone sent him) would
# steer every later Claude. Facts and preferences are fine; tool commands, instruction overrides and secrets are not,
# and neither are rules for Claude (below), which go in as a change he merges.
_TOOLS = r"\b(?:memory|people|home|hue|tv|music|eight_sleep|reminders|skill|health|hevy|grimoire)_[a-z_]+\b"
_CALL = r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\("  # any snake_case_name( : a function call
_OVERRIDE = r"\b(?:ignore|disregard|forget|override)\b[^.]{0,40}\b(?:instructions?|rules?|prompt|previous|above)\b"
_SECRETS = (r"\b(?:api[ _-]?key|password|passcode|secret|token)s?\b\s*[:=]\s*\S{8,}"  # a secret value
            r"|\b(?:reveal|share|send|include|print|paste|post|email)\b[^.]{0,40}"
            r"\b(?:api[ _-]?keys?|passwords?|secrets?|tokens?|credentials?)\b")  # or asking to hand one over

# Every tool the connector exposes. hevy_api is known up front; server.mount_all adds the rest (set_tool_source),
# read the first time the guard runs.
TOOL_NAMES: set[str] = {"hevy_api"}
_tool_source = None


def set_tool_source(source) -> None:
    """source: an async function returning every tool name the server exposes."""
    global _tool_source
    _tool_source = source


def register_tools(names) -> None:
    TOOL_NAMES.update(names)


async def tool_names() -> set[str]:
    """TOOL_NAMES, with the server's full list added the first time (kept on failure, retried next time)."""
    global _tool_source
    if _tool_source is not None:
        try:
            register_tools(await _tool_source())
            _tool_source = None
        except Exception as e:
            print(f"Memory guard couldn't list the tools: {e!r}", file=sys.stderr)
    return TOOL_NAMES


def _names_a_tool(text: str) -> bool:
    if re.search(_TOOLS, text, re.IGNORECASE) or re.search(_CALL, text, re.IGNORECASE):
        return True
    for name in tuple(TOOL_NAMES):  # a copy: the guard runs in a worker thread while the loop may add names
        n = re.escape(name)
        if "_" in name:  # hevy_api, people_note: never plain words
            pattern = rf"\b{n}\b"
        else:  # a one-word name is also a word: only as a tool ("the lamp tool", "call lamp")
            pattern = rf"\b{n}\s+tool\b|\b(?:call|run|invoke)\s+(?:the\s+)?{n}\b"
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


def check_safe(text: str) -> None:
    """Refuse memory text that reads like instructions to Claude rather than a fact about Nicholas.
    Async callers await tool_names() first, so every tool the server exposes is known."""
    for test, why in ((_names_a_tool, "names a connector tool"),
                      (lambda t: re.search(_OVERRIDE, t, re.IGNORECASE), "tries to override instructions"),
                      (lambda t: re.search(_SECRETS, t, re.IGNORECASE), "mentions a secret")):
        if test(text):
            raise ToolError(f"Not saved: the text {why}. Memory and person notes hold facts and preferences in plain words, "
                            "not commands for Claude; if Nicholas really wants this, he can add it himself.")

# How Claude should behave is a rule, not a fact about him. Rules go through rule_edit (his merge or his ship-it), so a "fact"
# that is really an instruction (the usual way an assistant with memory gets steered) can't be saved. A preference
# that follows from a fact ("dairy-free") is saved as the fact. These catch the usual shapes: text that starts with
# a command word, or that tells Claude, "you" or "the assistant" what to do.
RULE_REFUSAL = ("Not saved: this reads like a rule for Claude, not a fact about him. "
                "Put it in with rule_edit so it lands in the changes he merges.")
_RULE_START = re.compile(r"^\W*(?:always|never|don'?t|do not|make sure|from now on|remember to)\b", re.IGNORECASE)
_RULE_ADDRESSED = re.compile(r"\b(?:claude|you|the assistant)\b.{0,30}\b(?:should|must|always|never|don'?t)\b",
                             re.IGNORECASE)


def looks_like_rule(text: str) -> bool:
    """True if the text reads like a rule for Claude: it starts with always, never, don't, do not, make sure, from now
    on or remember to, or it tells Claude, "you" or "the assistant" what to do."""
    text = text.replace("\u2019", "'").replace("\u2018", "'")  # a phone turns the apostrophe in don't into U+2019
    return bool(_RULE_START.search(text) or _RULE_ADDRESSED.search(text))


def memory_guard(text: str) -> None:
    """The store's guard, run on every text that enters memory (an entry, a group name, a reason): it must be a fact
    about him, not a command for Claude (check_safe), and not a rule for Claude (looks_like_rule)."""
    check_safe(text)
    if looks_like_rule(text):
        raise ToolError(RULE_REFUSAL)


# Where memory lives: a folder inside the harness working copy (on dev), written under the repo's shared lock and pushed
# soon after each commit. Reads come from the clean copy of main (private.DIR).
_DEFAULT_WORKING_COPY = "~/Projects/personal-agent-harness"
MERGE_NOTE = " It reaches other chats once Nicholas merges the open changes."


def _setting(key: str, default: str = "") -> str:
    return str(private.get(key) or "").strip() or default


def _working_copy() -> Path:
    return Path(_setting("harness.working_copy", _DEFAULT_WORKING_COPY)).expanduser()


def _memory_dir() -> Path:
    configured = _setting("memory_dir")
    return Path(configured).expanduser() if configured else _working_copy() / "memory"


def _lock_path() -> Path | None:
    configured = _setting("harness.lock")
    return Path(configured).expanduser() if configured else None


WORKING_COPY = _working_copy()
MEMORY_DIR = _memory_dir()
LOCK_PATH = _lock_path()


def _push_soon(sha: str) -> None:
    """After a commit, start the harness command's push in the background and don't wait for it. No command, no push."""
    command = private.get("harness.command")
    if not command or not isinstance(command, list):
        return
    subprocess.Popen([*map(str, command), "push"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def read_store() -> MemoryStore:
    """Memory as merged: the clean copy of main. Reads only; never runs git."""
    return MemoryStore(private.DIR / "memory", commit=False)


def write_store() -> MemoryStore:
    """Memory for writing: the working copy's memory/ folder, on dev only, with the guard in front of every text that
    enters it. Each commit is pushed soon after (see _push_soon)."""
    return MemoryStore(MEMORY_DIR, guard=memory_guard, repo=WORKING_COPY, branch="dev", lock_path=LOCK_PATH,
                       after_commit=_push_soon)


Source = Literal["phone", "web", "claude code", "other"]

INSTRUCTIONS = (
    "Nicholas's shared memory is the same for every Claude (phone, web, Claude Code). At the start of any conversation"
    " about his life, plans, preferences or setup, call memory_recall once. When you learn something lasting (a fact"
    " about him, a preference, a decision and its reason, a project change; facts about a person go to people_note),"
    " call memory_save (pick a topic) and end your reply with its one line. Don't save one-off details, anything"
    " already there, or passwords, keys and account numbers. Rules about how Claude should behave go to"
    " rule_edit, not memory; \"write down / note...\" about his day means his journal."
)

SAVE_DESCRIPTION = (
    "Lasting facts about him (never one-off details or secrets). Core is only for facts that change most answers; "
    "everything else goes to a topic. For a passing state set review about four weeks out; for a plan, its date. "
    "If the save is held for similar entries, call again with similar='add' or similar='replace:<id>'. "
    "How Claude should behave goes to rule_edit.")


def _recall(s: MemoryStore, topic: str | None) -> str:
    """With no topic: what every chat starts with, CONTEXT.md (his rules, core facts, the topic list and the skill list).
    With one: that topic's file, or the archive when asked for it by name, or else the entries across topics that
    mention it (never the archive's), under the data header."""
    if topic is None:
        return _context_text()
    name = " ".join(topic.split()).lower()
    if name == memory_store.ARCHIVE or any(t.name == name for t in s.topics()):
        return f"{DATA_HEADER}\n{s.render(name)}"
    return f"{DATA_HEADER}\n{s.search(topic)}"


def _context_text() -> str:
    """CONTEXT.md from the clean copy if it's there, else rendered from the same folder."""
    try:
        return (private.DIR / context.FILE).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return context.render(private.DIR)


async def _run(fn: Callable[..., Any], *args: Any) -> Any:
    """A store call off the event loop (a write waits for the store's lock, which another process may hold), with its
    refusals as ToolErrors."""
    try:
        return await asyncio.to_thread(fn, *args)
    except MemoryError_ as e:
        raise ToolError(str(e)) from None


def _with_note(reply: str) -> str:
    """After a change, the note that it waits for the merge goes before the store's "End your reply with: ..."
    instruction, so the line Claude ends its reply with stays the store's own."""
    marker = " End your reply with:"
    return reply.replace(marker, MERGE_NOTE + marker, 1)  # a reply without it changed nothing (a repeat, a hold)


def _save(*args: Any) -> str:
    return _with_note(write_store().save(*args))


def _update(*args: Any) -> str:
    return _with_note(write_store().update(*args))


def build() -> FastMCP:
    mcp = FastMCP("Memory")

    @mcp.tool(annotations={"readOnlyHint": True})
    async def memory_recall(
        topic: Annotated[str | None, Field(description="A topic from the list, 'archive' for facts that have ended, or a word to look for across topics; leave out at the start of a conversation")] = None,
    ) -> str:
        """Read Nicholas's shared memory: with no topic, his rules, core facts and the list of topics; with a topic,
        what's saved there. Call once at the start of a conversation about his life, plans, preferences or setup."""
        usage_log.record("memory_recall")
        return await _run(_recall, read_store(), (topic or "").strip() or None)

    @mcp.tool(description=SAVE_DESCRIPTION, annotations={"readOnlyHint": False, "destructiveHint": False})
    async def memory_save(
        text: Annotated[str, Field(description="One short, lasting fact in plain words")],
        topic: Annotated[str, Field(description="'core' only for facts that change most answers; otherwise the topic from memory_recall's list that fits")],
        source: Annotated[Source, Field(description="Where you are: phone, web, claude code or other")],
        under: Annotated[str | None, Field(description="A group inside the topic, such as a person's or an app's name; optional")] = None,
        review: Annotated[str | None, Field(description="A date (like 2026-11-05) to check this again, or 'never' for a lasting fact")] = None,
        similar: Annotated[str | None, Field(description="Only after a save was held for similar entries: 'add' to keep both, or 'replace:<id>' to close the old one")] = None,
    ) -> str:
        await tool_names()  # the guard needs every tool the server exposes
        return await _run(_save, text, topic, source, under, review, similar)

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
    async def memory_update(
        entry_id: Annotated[str, Field(description="The id memory_recall shows for the entry, like h12")],
        text: Annotated[str | None, Field(description="New text; leave out to confirm the entry is still true (its date becomes today)")] = None,
        remove: Annotated[bool, Field(description="Delete the entry (and anything nested under it). Only when Nicholas asks you to forget it, or a correction makes it wrong.")] = False,
    ) -> str:
        """Change a memory, confirm it's still true (leave the text out), or remove it. Afterwards, end your reply with
        the line it gives."""
        await tool_names()
        return await _run(_update, entry_id, text, remove)

    return mcp
