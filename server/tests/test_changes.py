"""The change tools: each runs the harness command with a sub-command and JSON on stdin. A fake command script stands in
for it (it records its argv and stdin and prints canned JSON); nothing here touches a repo, git or the network."""
import asyncio
import json
import os
import stat
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import changes_mcp
import host
import private


@pytest.fixture
def fake_command(tmp_path, monkeypatch):
    """A harness command that logs `argv` and stdin to calls.jsonl and answers from reply.json (exit code in
    reply.exit, default 0)."""
    calls = tmp_path / "calls.jsonl"
    reply = tmp_path / "reply.json"
    code = tmp_path / "reply.exit"
    reply.write_text(json.dumps({"text": "Edited: made up"}))
    script = tmp_path / "harness-command"
    script.write_text("#!/bin/sh\n"
                      f"python3 -c 'import json,sys; print(json.dumps({{\"argv\": sys.argv[1:], \"stdin\": sys.stdin.read()}}))' \"$@\" >> {calls}\n"
                      f"cat {reply}\n"
                      f"if [ -f {code} ]; then exit $(cat {code}); fi\n")
    script.chmod(0o755)
    monkeypatch.setitem(private.CONFIG, "harness", {"command": [str(script), "--tree", "main"]})

    class Fake:
        path = script

        def sent(self):
            return [json.loads(line) for line in calls.read_text().splitlines()]

        def answer(self, body, exit_code=0):
            reply.write_text(body if isinstance(body, str) else json.dumps(body))
            code.write_text(str(exit_code))
    return Fake()


@pytest.fixture
def client():
    return changes_mcp.build()


def call(client, tool, **args):
    async def go():
        async with Client(client) as c:
            return (await c.call_tool(tool, args)).content[0].text
    return asyncio.run(go())


def test_rule_edit_sends_json_and_returns_text(fake_command, client):
    out = call(client, "rule_edit", path="RULES.md", new="- [R9] Be kind.", why="he asked", surface="phone",
               quote="from now on, be kind", untrusted_content_seen=True)
    assert out == "Edited: made up"
    (sent,) = fake_command.sent()
    assert sent["argv"] == ["--tree", "main", "edit"]  # the configured arguments, then the sub-command
    assert json.loads(sent["stdin"]) == {"path": "RULES.md", "new": "- [R9] Be kind.", "why": "he asked",
                                         "surface": "phone", "quote": "from now on, be kind", "untrusted": True}


def test_omitted_options_are_left_out_of_the_json(fake_command, client):
    call(client, "rule_edit", path="RULES.md", new="x", why="w", surface="laptop", old="y")
    assert json.loads(fake_command.sent()[0]["stdin"]) == {"path": "RULES.md", "new": "x", "why": "w",
                                                            "surface": "laptop", "old": "y", "untrusted": False}


def test_each_tool_runs_its_own_sub_command(fake_command, client):
    call(client, "change_preview", path="RULES.md", new="x")
    call(client, "ship_it", path="RULES.md", new="x", title="T", diff_hash="abc")
    call(client, "drop_change", commit="1234abc", change="a rule", reason="too long")
    assert [s["argv"][-1] for s in fake_command.sent()] == ["preview", "ship", "drop"]
    assert json.loads(fake_command.sent()[2]["stdin"]) == {"commit": "1234abc", "change": "a rule", "reason": "too long"}


def test_command_refusal_becomes_tool_error(fake_command, client):
    fake_command.answer({"error": "not an allowed file"}, exit_code=2)
    with pytest.raises(ToolError, match="not an allowed file"):
        call(client, "rule_edit", path="x.py", new="x", why="w", surface="phone")


def test_a_crash_never_repeats_what_the_command_printed(fake_command, client):
    fake_command.answer("Traceback ... secret-token-123", exit_code=1)
    with pytest.raises(ToolError) as e:
        call(client, "change_preview", path="RULES.md", new="x")
    assert "failed (exit 1)" in str(e.value) and "secret-token-123" not in str(e.value) and host.NAME in str(e.value)


def test_an_empty_or_unreadable_answer_is_an_error(fake_command, client):
    fake_command.answer({"text": "  "})
    with pytest.raises(ToolError, match="usable answer"):
        call(client, "change_preview", path="RULES.md", new="x")
    fake_command.answer("not json")
    with pytest.raises(ToolError, match="usable answer"):
        call(client, "change_preview", path="RULES.md", new="x")


def test_not_set_up_names_host(client, monkeypatch):
    monkeypatch.delitem(private.CONFIG, "harness", raising=False)
    with pytest.raises(ToolError, match=f"Changes aren't set up on {host.NAME}"):
        call(client, "change_preview", path="RULES.md", new="x")


def test_a_command_that_cannot_run_is_not_set_up(client, tmp_path, monkeypatch):
    plain = tmp_path / "not-executable"
    plain.write_text("#!/bin/sh\n")
    for named in ([str(plain)], [str(tmp_path / "missing")], [], "a string", [""]):
        monkeypatch.setitem(private.CONFIG, "harness", {"command": named})
        with pytest.raises(ToolError, match="aren't set up"):
            call(client, "change_preview", path="RULES.md", new="x")


def test_a_timeout_says_it_may_have_happened(fake_command, client, monkeypatch):
    monkeypatch.setattr(changes_mcp, "TIMEOUT_S", 1)
    fake_command.path.write_text("#!/bin/sh\nsleep 5\n")
    with pytest.raises(ToolError, match="may or may not"):
        call(client, "drop_change", commit="1234abc", change="c", reason="r")


def test_ship_waits_longer_than_the_others():
    assert changes_mcp.SHIP_TIMEOUT_S >= 660 > changes_mcp.TIMEOUT_S


def test_ship_it_annotations(client):
    async def go():
        async with Client(client) as c:
            return {t.name: t for t in await c.list_tools()}
    tools = asyncio.run(go())
    ship = tools["ship_it"].annotations
    assert ship.destructiveHint is True and ship.openWorldHint is True and ship.readOnlyHint is False
    assert set(tools) == {"rule_edit", "change_preview", "ship_it", "drop_change"}
    assert tools["change_preview"].annotations.readOnlyHint is True
    assert "ship it" in tools["ship_it"].description and "change_preview" in tools["ship_it"].description


def test_no_memory_propose_or_rule_propose(client):
    async def go():
        async with Client(client) as c:
            return {t.name for t in await c.list_tools()}
    assert not {"memory_propose", "rule_propose"} & asyncio.run(go())


def test_the_old_module_is_gone():
    with pytest.raises(ImportError):
        __import__("proposals_mcp")


def test_a_tilde_in_any_part_of_the_command_is_expanded(tmp_path, monkeypatch):
    script = tmp_path / "cmd"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    monkeypatch.setitem(private.CONFIG, "harness",
                        {"command": [str(script), "--project", "~/Projects/x", "~/Library/y.py", "plain"]})
    assert changes_mcp.command() == [str(script), "--project", str(Path("~/Projects/x").expanduser()),
                                     str(Path("~/Library/y.py").expanduser()), "plain"]
