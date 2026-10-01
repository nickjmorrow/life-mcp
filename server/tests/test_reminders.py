import asyncio
import json
import os
import stat

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import reminders_mcp

LISTS = "Kim's\nSoon\nWork"
MILK = {"externalId": "A1", "title": "milk", "isCompleted": False, "priority": 0, "list": "Kim's"}
EGGS = {"externalId": "B2", "title": "eggs", "isCompleted": False, "priority": 1, "list": "Kim's",
        "notes": "the big carton", "dueDate": "2026-09-27T09:00:00-05:00"}


class FakeCLI:
    """Stands in for `reminders`: records each call's args, answers by subcommand."""

    def __init__(self, items=(MILK, EGGS)):
        self.calls: list[tuple[str, ...]] = []
        self.items = list(items)

    async def __call__(self, *args: str) -> str:
        self.calls.append(args)
        if args[0] == "show-lists":
            return LISTS
        if args[0] in ("show", "show-all"):
            return json.dumps(self.items)
        if args[0] == "add":
            return json.dumps({**MILK, "externalId": "NEW", "title": args[-1]})
        return f"ok {args[0]}"


@pytest.fixture
def cli(monkeypatch):
    fake = FakeCLI()
    monkeypatch.setattr(reminders_mcp, "run", fake)
    return fake


def call(tool: str, **args) -> str:
    async def go():
        async with Client(reminders_mcp.mcp) as client:
            return (await client.call_tool(tool, args)).content[0].text

    return asyncio.run(go())


def test_list_lists(cli):
    assert call("list_lists") == "Kim's\nSoon\nWork"


def test_show_formats_items_with_ids(cli):
    out = call("show", list="kims")
    assert "milk [id A1]" in out
    assert "eggs (due 2026-09-27T09:00:00-05:00, high priority, notes: the big carton) [id B2]" in out
    assert cli.calls[-1] == ("show", "--format", "json", "--", "Kim's")


def test_show_include_completed(cli):
    call("show", list="Soon", include_completed=True)
    assert cli.calls[-1] == ("show", "--format", "json", "--include-completed", "--", "Soon")


def test_show_empty_list(cli):
    cli.items = []
    assert call("show", list="Work") == "Work is empty."


def test_list_names_are_loose(cli):
    call("add", list="the kims list", title="milk")
    assert cli.calls[-1][-2] == "Kim's"


def test_unknown_list_lists_options(cli):
    with pytest.raises(ToolError, match="No Reminders list called 'Costco'. Lists: Kim's, Soon, Work"):
        call("add", list="Costco", title="milk")


def test_add_plain(cli):
    assert call("add", list="Kim's", title="oat milk") == "Added 'oat milk' to Kim's [id NEW]"
    assert cli.calls[-1] == ("add", "--format", "json", "--", "Kim's", "oat milk")


def test_add_with_options(cli):
    call("add", list="Soon", title="-call mom", notes="about the trip", due="tomorrow 9am", priority="high")
    assert cli.calls[-1] == ("add", "--format", "json", "--notes=about the trip", "--due-date=tomorrow 9am",
                             "--priority=high", "--", "Soon", "-call mom")


def test_due(cli):
    out = call("due", date="today")
    assert "milk [id A1] (Kim's)" in out
    assert cli.calls[-1] == ("show-all", "--format", "json", "--due-date=today", "--include-overdue")


def test_complete_uncomplete_delete(cli):
    assert call("complete", list="kims", id="A1") == "ok complete"
    assert cli.calls[-1] == ("complete", "--", "Kim's", "A1")
    call("uncomplete", list="kims", id="A1")
    assert cli.calls[-1] == ("uncomplete", "--", "Kim's", "A1")
    call("delete", list="kims", id="A1")
    assert cli.calls[-1] == ("delete", "--", "Kim's", "A1")


def test_edit(cli):
    call("edit", list="Soon", id="B2", title="call mom tonight", clear_due=True)
    assert cli.calls[-1] == ("edit", "--clear-due-date", "--", "Soon", "B2", "call mom tonight")


def test_edit_needs_a_change(cli):
    with pytest.raises(ToolError, match="Nothing to change"):
        call("edit", list="Soon", id="B2")


def test_edit_due_and_clear_conflict(cli):
    with pytest.raises(ToolError, match="not both"):
        call("edit", list="Soon", id="B2", due="friday", clear_due=True)


def test_create_list(cli):
    call("create_list", name="Costco")
    assert cli.calls[-1] == ("new-list", "--", "Costco")


# ── run(): the real subprocess wrapper, against a stand-in script ──────────


def fake_binary(tmp_path, monkeypatch, body: str):
    script = tmp_path / "reminders"
    script.write_text("#!/bin/sh\n" + body + "\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(reminders_mcp, "REMINDERS", str(script))


def test_run_returns_stdout(tmp_path, monkeypatch):
    fake_binary(tmp_path, monkeypatch, 'echo "hello $1"')
    assert asyncio.run(reminders_mcp.run("show-lists")) == "hello show-lists"


def test_run_no_access_says_how_to_fix(tmp_path, monkeypatch):
    fake_binary(tmp_path, monkeypatch, 'echo "error: you need to grant reminders access" >&2; exit 1')
    with pytest.raises(ToolError, match="Privacy & Security → Reminders"):
        asyncio.run(reminders_mcp.run("show-lists"))


def test_run_error_passes_message(tmp_path, monkeypatch):
    fake_binary(tmp_path, monkeypatch, 'echo "No reminder at index 7 on Soon" >&2; exit 1')
    with pytest.raises(ToolError, match="No reminder at index 7 on Soon"):
        asyncio.run(reminders_mcp.run("complete", "Soon", "7"))


def test_run_missing_binary(monkeypatch):
    monkeypatch.setattr(reminders_mcp, "REMINDERS", "/nonexistent/reminders")
    with pytest.raises(ToolError, match="reminders-cli isn't installed"):
        asyncio.run(reminders_mcp.run("show-lists"))
