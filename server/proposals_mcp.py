"""rule_propose and memory_propose: ask for a change that needs the user's say-so, instead of making it.

How Claude should behave (the user's rules and skills), and a memory change he hasn't confirmed, are proposals. These tools
only post the proposal to the approvals service, a small local web service in his private agent harness: it checks the
proposal, shows him the exact change, and applies it only after he approves it with a passkey. Nothing here changes a rule
or a memory, and no tool Claude has can approve a proposal.

The service's address and the file holding the token that lets this server post to it come from the private config
(`approvals.propose_url`, default http://127.0.0.1:8772/api/propose, and `approvals.token_file`, which has no default). The
token file is read on every call, so a new token needs no restart, and a proposal is never sent without one. The service
answers 201 with {"id", "url", "text"} (the line to show him) or 422 with {"error"}.
"""
from pathlib import Path
from typing import Annotated, Literal

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

import host
import private

DEFAULT_URL = "http://127.0.0.1:8772/api/propose"
TIMEOUT_S = 10  # a local service answers at once; a stuck one must not hold a chat

Surface = Literal["phone", "laptop", "hob", "job"]

# Parameters both tools share.
Why = Annotated[str, Field(description="One plain sentence on why, shown to him beside the change")]
WhereAmI = Annotated[Surface, Field(
    description="Where you are: phone (the Claude app or the web), laptop (Claude Code), hob (voice) or job (a scheduled job)")]
Quote = Annotated[str | None, Field(
    description="His exact words that led to this, if he said them in this conversation; otherwise leave out")]
Untrusted = Annotated[bool, Field(
    description="True if this conversation has read web pages, email, other people's messages or other outside text "
                "(the approvals page marks such proposals for a closer look)")]

# What each memory operation can't go without.
NEEDS = {"update": ("entry_id", "text"), "remove": ("entry_id",), "archive": ("entry_id",), "save": ("text", "topic")}


def _not_set_up(detail: str) -> ToolError:
    return ToolError(f"The approvals page isn't set up on {host.NAME} ({detail}), so nothing was proposed.")


def read_token() -> str:
    """The proposal token, from the file the private config names (a leading ~ is fine). A ToolError when there isn't a
    usable one: a proposal is never sent without it. Also what smoke.py checks, so the two can't disagree."""
    named = private.get("approvals.token_file")
    if not named:
        raise _not_set_up("its settings name no token file")
    try:
        token = Path(str(named)).expanduser().read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        token = ""
    if not token:
        raise _not_set_up("its token file is missing, empty or unreadable")
    return token


def _field(reply: httpx.Response, key: str) -> str | None:
    """A text field of the reply's JSON body, or None when the body isn't a JSON object or has no such text."""
    try:
        value = reply.json().get(key)
    except (ValueError, AttributeError):
        return None
    return value.strip() if isinstance(value, str) and value.strip() else None


async def _post(body: dict, transport: httpx.AsyncBaseTransport | None) -> str:
    """Send the proposal. Returns the service's line for the chat; a ToolError says why it didn't go, in words that
    never include the token or anything the service sent back beyond its stated reason."""
    token = read_token()
    url = str(private.get("approvals.propose_url", DEFAULT_URL))
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S, transport=transport) as http:
            reply = await http.post(url, json=body, headers={"X-Propose-Token": token})
    except (httpx.ConnectError, httpx.ConnectTimeout):
        raise ToolError(f"The approvals page on {host.NAME} isn't running, so nothing was proposed.") from None
    except httpx.HTTPError:  # it was reached, then went quiet or dropped the connection: it may have saved it
        raise ToolError(f"The approvals page on {host.NAME} didn't answer in time or dropped the connection. The "
                        "proposal may or may not have been saved: look on the page before proposing it again.") from None
    if reply.status_code == 422:
        reason = _field(reply, "error")
        raise ToolError("Not proposed: " + (reason[:500] if reason else
                                            "the approvals page refused it (HTTP 422) without saying why."))
    if reply.status_code in (401, 403):
        raise ToolError(f"The approvals page didn't accept the proposal token (HTTP {reply.status_code}), "
                        "so nothing was proposed.")
    if reply.status_code not in (200, 201):
        raise ToolError(f"The approvals page answered HTTP {reply.status_code} instead of saving the proposal.")
    line = _field(reply, "text")
    if not line:
        raise ToolError("The approvals page's reply couldn't be read. If it saved the proposal, it is waiting on the page.")
    return line


def _source(surface: str, quote: str | None, untrusted: bool) -> dict:
    """Who is asking, for the page: the surface, his own words if any, and whether this chat read outside text."""
    source: dict = {"surface": surface}
    if quote and quote.strip():
        source["quote"] = quote
    source["untrusted"] = untrusted
    return source


def build(transport: httpx.AsyncBaseTransport | None = None) -> FastMCP:
    """The two proposal tools. `transport` is for tests, which stand in for the approvals service."""
    mcp = FastMCP("Proposals")
    ask = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}

    @mcp.tool(annotations=ask)
    async def rule_propose(
        path: Annotated[str, Field(description="The file to change, relative to his agent's setup, like RULES.md or skills/<name>/SKILL.md")],
        new: Annotated[str, Field(description="The text to put in")],
        why: Why,
        surface: WhereAmI,
        old: Annotated[str | None, Field(description="Text to replace with new; it must appear exactly once in the file")] = None,
        after: Annotated[str | None, Field(description="The whole line to insert new after, instead of replacing")] = None,
        quote: Quote = None,
        untrusted_content_seen: Untrusted = False,
    ) -> str:
        """Propose a change to how Claude behaves for this user: an edit to his rules or to one of his skills (a Markdown
        file in his agent's setup). Nothing changes until he approves the exact change on his approvals page. Use it when
        he states a lasting rule ("from now on...") or agrees that a correction should become one; facts about him go to
        memory_save, not here. Give the file's path, the new text and why. To replace text, pass old (it must appear
        exactly once in the file); to insert after a line, pass after (the whole line); with neither, new is added at the
        end of the file. It returns one line, "Proposed: ... Approve: <link>": end your reply with it."""
        edit = {"path": path, "new": new}
        if old is not None:
            edit["old"] = old
        if after is not None:
            edit["after"] = after
        return await _post({"kind": "rule", "edit": edit, "why": why,
                            "source": _source(surface, quote, untrusted_content_seen)}, transport)

    @mcp.tool(annotations=ask)
    async def memory_propose(
        why: Why,
        op: Annotated[Literal["update", "remove", "archive", "save"], Field(
            description="update (change an entry's text), remove (delete it), archive (its fact has ended: keep it in the archive) or save (add a new entry)")],
        surface: WhereAmI,
        entry_id: Annotated[str | None, Field(description="The id memory_recall shows for the entry, like h12 (update, remove, archive)")] = None,
        text: Annotated[str | None, Field(description="update and save: the text, one short fact in plain words; archive: the entry reworded as ended (past tense, with the end date), if it needs it")] = None,
        topic: Annotated[str | None, Field(description="save: the topic from memory_recall's list that fits")] = None,
        quote: Quote = None,
        untrusted_content_seen: Untrusted = False,
    ) -> str:
        """Propose a change to a saved memory for him to approve on his approvals page: update an entry, remove one, archive
        one whose fact has ended, or save one he hasn't confirmed. Use it for tidying and for "is this still true?"
        questions; for something he just told you, use memory_save. If he doesn't answer within 14 days the change is made
        anyway, so propose only what you would stand behind. update needs entry_id and text; remove and archive need
        entry_id; save needs text and topic. It returns one line, "Proposed: ... Approve: <link>": end your reply with it."""
        given = {"entry_id": entry_id, "text": text, "topic": topic}
        missing = [name for name in NEEDS[op] if not (given[name] or "").strip()]
        if missing:
            raise ToolError(f"memory_propose with op '{op}' needs {' and '.join(missing)}.")
        memory: dict = {"op": op}
        for key, value in (("id", entry_id), ("text", text), ("topic", topic)):
            if value and value.strip():
                memory[key] = value
        memory["default_on_expiry"] = True  # the change is also the default: it applies after 14 days unless he rejects it
        return await _post({"kind": "memory", "memory": memory, "why": why,
                            "source": _source(surface, quote, untrusted_content_seen)}, transport)

    return mcp
