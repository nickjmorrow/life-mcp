"""The forge_* tools: Forge's stdio MCP server proxied. Skipped where Forge isn't installed."""
import asyncio
import importlib
import json
import os
from pathlib import Path

import pytest
from fastmcp import Client, FastMCP

import server

SCRIPT = Path(os.environ.get("FORGE_MCP_SCRIPT") or Path.home() / "Projects" / "forge" / "mcp" / "forge_mcp.py")
FORGE = Path(os.environ.get("FORGE_BIN") or Path.home() / ".local" / "bin" / "forge")

pytestmark = pytest.mark.skipif(not (SCRIPT.exists() and FORGE.exists()), reason="Forge isn't installed here")


@pytest.fixture()
def mounted(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_DB", str(tmp_path / "forge.sqlite"))
    import forge_proxy
    importlib.reload(forge_proxy)
    target = FastMCP("t")
    assert server.mount(server.group("forge"), target)
    return target


def run_in_one_loop(target, scenario):
    """The proxy keeps its connection to the event loop that opened it, so a scenario makes all its calls inside one."""
    async def go():
        async with Client(target) as c:
            async def call(tool, **args):
                r = await c.call_tool(tool, args)
                return json.loads(r.content[0].text)
            return await scenario(call)
    return asyncio.run(go())


def test_the_tools_are_served_under_the_forge_prefix(mounted):
    async def names():
        async with Client(mounted) as c:
            return {t.name for t in await c.list_tools()}
    got = asyncio.run(names())
    assert {"forge_list_exercises", "forge_put_routine", "forge_delete_exercise", "forge_sql", "forge_undo_claude", "forge_put_recovery"} <= got
    assert not any(n in got for n in ("list_exercises", "sql"))


def test_a_routine_round_trips_and_undoes(mounted):
    async def scenario(call):
        await call("forge_put_exercise", exercise={"name": "Bench Press (Barbell)", "primaryMuscle": "chest"})
        made = await call("forge_put_routine", routine={"title": "Push", "exercises": [{"exercise": "Bench Press (Barbell)", "sets": [{"weightLb": 135, "reps": 8}]}]})
        titles = [r["title"] for r in await call("forge_list_routines")]
        await call("forge_undo_claude")
        return made, titles, await call("forge_list_routines")
    made, titles, after_undo = run_in_one_loop(mounted, scenario)
    assert titles == ["Push"] and made["exercises"][0]["sets"][0]["reps"] == 8
    assert after_undo == []


def test_a_missing_install_means_the_group_is_skipped(monkeypatch, tmp_path):
    monkeypatch.setenv("FORGE_MCP_SCRIPT", str(tmp_path / "nope.py"))
    import forge_proxy
    with pytest.raises(ImportError):
        importlib.reload(forge_proxy)
    monkeypatch.delenv("FORGE_MCP_SCRIPT")
    importlib.reload(forge_proxy)
