"""rule_propose and memory_propose only ask: they post a proposal to the approvals service and hand back its link. The
service is stood in for by an httpx.MockTransport; every address, name and token here is made up (192.0.2.x is the
documentation range, and the files are temp files)."""
import asyncio
import json

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import private
import proposals_mcp

URL = "http://192.0.2.7:8772/api/propose"
TOKEN = "tok-4f9a1c"
REPLY = {"id": "P-0007", "url": "https://approvals.example/p/P-0007",
         "text": "Proposed: keep replies short. Approve: https://approvals.example/p/P-0007"}

RULE = dict(path="RULES.md", new="- Keep replies short.", why="He asked for shorter replies.", surface="phone")
MEMORY = dict(why="He moved.", op="update", entry_id="h12", text="lives in the north end", surface="phone")
BOTH = [("rule_propose", RULE), ("memory_propose", MEMORY)]


class Service:
    """Stands in for the approvals service: answers every request the same way, and keeps what it was sent."""

    def __init__(self, status=201, payload=REPLY, content=None, error=None):
        self.requests = []
        self.status, self.payload, self.content, self.error = status, payload, content, error

    def __call__(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        if self.content is not None:
            return httpx.Response(self.status, content=self.content)
        return httpx.Response(self.status, json=self.payload)

    @property
    def bodies(self):
        return [json.loads(r.content) for r in self.requests]


@pytest.fixture
def token_file(tmp_path):
    path = tmp_path / "propose-token"
    path.write_text(TOKEN + "\n")
    return path


@pytest.fixture(autouse=True)
def settings(monkeypatch, token_file):
    monkeypatch.setitem(private.CONFIG, "approvals", {"propose_url": URL, "token_file": str(token_file)})


def call(service, tool, **args):
    server = proposals_mcp.build(transport=httpx.MockTransport(service))

    async def go():
        async with Client(server) as c:
            return (await c.call_tool(tool, args)).content[0].text
    return asyncio.run(go())


def refused(service, tool, args, **changes):
    """The ToolError text of a call that is turned away."""
    with pytest.raises(ToolError) as e:
        call(service, tool, **{**args, **changes})
    return str(e.value)


# --- rule_propose ---------------------------------------------------------------------------------------------------


def test_rule_propose_posts_the_edit_and_returns_the_services_line():
    service = Service()
    assert call(service, "rule_propose", **RULE, old="- Be wordy.") == REPLY["text"]
    [request] = service.requests
    assert (request.method, str(request.url)) == ("POST", URL)
    assert service.bodies == [{
        "kind": "rule",
        "edit": {"path": "RULES.md", "new": "- Keep replies short.", "old": "- Be wordy."},
        "why": "He asked for shorter replies.",
        "source": {"surface": "phone", "untrusted": False},
    }]


@pytest.mark.parametrize("extra, edit", [
    ({"old": "- Be wordy."}, {"path": "RULES.md", "new": "- Keep replies short.", "old": "- Be wordy."}),
    ({"after": "## Style"}, {"path": "RULES.md", "new": "- Keep replies short.", "after": "## Style"}),
    ({}, {"path": "RULES.md", "new": "- Keep replies short."}),
], ids=["replace", "insert after a line", "append"])
def test_rule_propose_sends_only_the_edit_fields_it_was_given(extra, edit):
    service = Service()
    call(service, "rule_propose", **RULE, **extra)
    assert service.bodies[0]["edit"] == edit  # no "old": null or "after": null for the service to trip over


# --- memory_propose -------------------------------------------------------------------------------------------------


def test_memory_propose_sends_default_on_expiry_and_the_entry_as_id():
    service = Service()
    assert call(service, "memory_propose", **MEMORY) == REPLY["text"]
    assert service.bodies == [{
        "kind": "memory",
        "memory": {"op": "update", "id": "h12", "text": "lives in the north end", "default_on_expiry": True},
        "why": "He moved.",
        "source": {"surface": "phone", "untrusted": False},
    }]


@pytest.mark.parametrize("args, memory", [
    (dict(op="save", text="keeps bees", topic="home"),
     {"op": "save", "text": "keeps bees", "topic": "home", "default_on_expiry": True}),
    (dict(op="remove", entry_id="h12"), {"op": "remove", "id": "h12", "default_on_expiry": True}),
    (dict(op="archive", entry_id="h12"), {"op": "archive", "id": "h12", "default_on_expiry": True}),
    (dict(op="archive", entry_id="h12", text="lived in the south end until 2026"),
     {"op": "archive", "id": "h12", "text": "lived in the south end until 2026", "default_on_expiry": True}),
], ids=["save", "remove", "archive", "archive with the closed wording"])
def test_memory_propose_sends_each_operation_with_just_its_fields(args, memory):
    service = Service()
    call(service, "memory_propose", why="Tidying.", surface="job", **args)
    assert service.bodies[0]["memory"] == memory


@pytest.mark.parametrize("changes, names, leaves_out", [
    (dict(entry_id=None), "entry_id", "text"),
    (dict(text=None), "text", "entry_id"),
    (dict(entry_id=None, text=None), "entry_id and text", None),
    (dict(op="remove", entry_id=None, text=None), "entry_id", "text"),
    (dict(op="archive", entry_id=None, text=None), "entry_id", "text"),
    (dict(op="save", entry_id=None, topic="home", text=None), "text", "topic"),
    (dict(op="save", entry_id=None, text="keeps bees", topic=None), "topic", "text"),
    (dict(op="save", entry_id=None, text=None, topic=None), "text and topic", None),
], ids=["update without an entry", "update without text", "update with neither", "remove without an entry",
        "archive without an entry", "save without text", "save without a topic", "save with neither"])
def test_memory_propose_needs_the_entry_or_the_text_its_operation_works_on(changes, names, leaves_out):
    service = Service()
    args = {k: v for k, v in {**MEMORY, **changes}.items() if v is not None}
    message = refused(service, "memory_propose", args)
    assert f"needs {names}" in message  # it says exactly what is missing, so the model can fix the call
    assert leaves_out is None or leaves_out not in message
    assert service.requests == []  # nothing goes to the service half-asked


# --- who is asking --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("tool, args", BOTH)
def test_the_source_carries_the_surface_his_words_and_whether_the_chat_read_outside_text(tool, args):
    service = Service()
    call(service, tool, **{**args, "surface": "laptop", "quote": "from now on, call me Sam",
                           "untrusted_content_seen": True})
    assert service.bodies[0]["source"] == {"surface": "laptop", "quote": "from now on, call me Sam", "untrusted": True}


@pytest.mark.parametrize("tool, args", BOTH)
@pytest.mark.parametrize("surface", [None, "", "watch"], ids=["none", "empty", "unknown"])
def test_a_proposal_must_say_which_surface_it_came_from(tool, args, surface):
    service = Service()
    args = {k: v for k, v in {**args, "surface": surface}.items() if v is not None}
    with pytest.raises(ToolError, match="surface"):  # the error names what to fix (not just "no such tool")
        call(service, tool, **args)
    assert service.requests == []


# --- the token ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("tool, args", BOTH)
def test_the_token_goes_in_a_header_without_its_newline(tool, args):
    service = Service()
    call(service, tool, **args)
    assert service.requests[0].headers["X-Propose-Token"] == TOKEN


def test_a_changed_token_file_is_used_by_the_next_proposal(token_file):
    service = Service()
    call(service, "rule_propose", **RULE)
    token_file.write_text("tok-rotated\n")
    call(service, "rule_propose", **RULE)
    assert [r.headers["X-Propose-Token"] for r in service.requests] == [TOKEN, "tok-rotated"]


def test_the_token_path_may_start_with_a_tilde(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "token").write_text("tok-home\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setitem(private.CONFIG, "approvals", {"propose_url": URL, "token_file": "~/token"})
    service = Service()
    call(service, "rule_propose", **RULE)
    assert service.requests[0].headers["X-Propose-Token"] == "tok-home"


@pytest.mark.parametrize("breakage", ["no file", "empty file", "a folder, not a file", "not in the settings"])
@pytest.mark.parametrize("tool, args", BOTH)
def test_without_a_usable_token_nothing_is_sent(tool, args, breakage, token_file, monkeypatch):
    if breakage == "no file":
        token_file.unlink()
    elif breakage == "empty file":
        token_file.write_text("\n")
    elif breakage == "a folder, not a file":
        token_file.unlink()
        token_file.mkdir()
    else:
        monkeypatch.setitem(private.CONFIG, "approvals", {"propose_url": URL})
    service = Service()
    message = refused(service, tool, args)
    assert "isn't set up on TestMac" in message and "nothing was proposed" in message
    assert service.requests == []


# --- the service's answers ------------------------------------------------------------------------------------------


def test_a_refusal_becomes_a_tool_error_with_the_reason():
    service = Service(status=422, payload={"error": "not an allowed file"})
    assert refused(service, "rule_propose", {**RULE, "path": "server/server.py"}) == "Not proposed: not an allowed file"


def test_a_refusal_without_a_reason_is_still_a_refusal():
    service = Service(status=422, payload={"detail": "unprocessable"})
    message = refused(service, "rule_propose", RULE)
    assert message.startswith("Not proposed:") and "422" in message and "unprocessable" not in message


@pytest.mark.parametrize("error", [httpx.ConnectError("refused"), httpx.ConnectTimeout("no route")],
                         ids=["refused", "no route"])
def test_a_service_that_is_down_is_named_by_its_mac(error):
    service = Service(error=error)
    assert refused(service, "rule_propose", RULE) == \
        "The approvals page on TestMac isn't running, so nothing was proposed."


def test_a_service_that_goes_quiet_may_still_have_saved_the_proposal():
    service = Service(error=httpx.ReadTimeout("slow"))
    message = refused(service, "rule_propose", RULE)
    assert "TestMac" in message and "didn't answer in time" in message
    assert "nothing was proposed" not in message  # it may have been, so don't say so


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_token_says_so(status):
    service = Service(status=status, payload={"detail": f"bad token {TOKEN}"})
    message = refused(service, "rule_propose", RULE)
    assert str(status) in message and "token" in message and "nothing was proposed" in message
    assert TOKEN not in message  # whatever the service says back, the token never reaches the chat


def test_another_failure_says_the_status_and_nothing_the_service_sent_back():
    service = Service(status=500, payload={"trace": f"boom with {TOKEN}"})
    message = refused(service, "rule_propose", RULE)
    assert "500" in message and TOKEN not in message and "boom" not in message


@pytest.mark.parametrize("answer", [
    dict(payload={"id": "P-0008", "url": "https://approvals.example/p/P-0008"}),  # no text
    dict(content=b"<html>not json</html>"),
], ids=["no text", "not JSON"])
def test_a_reply_it_cannot_read_points_at_the_page(answer):
    message = refused(Service(**answer), "rule_propose", RULE)
    assert "couldn't be read" in message and "approvals page" in message


def test_the_default_address_is_used_when_the_settings_have_none(token_file, monkeypatch):
    monkeypatch.setitem(private.CONFIG, "approvals", {"token_file": str(token_file)})
    service = Service()
    call(service, "rule_propose", **RULE)
    assert str(service.requests[0].url) == "http://127.0.0.1:8772/api/propose"


def test_the_call_gives_up_after_a_short_wait():
    service = Service()
    call(service, "rule_propose", **RULE)
    timeouts = service.requests[0].extensions["timeout"]
    assert all(t is not None and t <= 10 for t in timeouts.values())  # none of them waits forever
