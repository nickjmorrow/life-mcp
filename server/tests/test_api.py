"""api.py is the one interface other code on the Mac uses; it must keep working and stay small."""
import asyncio
import inspect

import pytest

import api
import server
from fake_logseq import FakeLogseq


def test_exports_exactly_the_documented_interface():
    assert set(api.__all__) == {"ToolError", "cli", "edn", "ensure_properties", "target_args", "memory",
                                "people_note"}
    for name in api.__all__:
        obj = getattr(api, name)
        assert obj.__doc__, name


def test_importing_api_mounts_nothing():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert not any(n.startswith(("memory_", "hue_", "people_")) for n in names)


def test_cli_and_memory_go_through_the_server(monkeypatch):
    fake = FakeLogseq()
    monkeypatch.setattr(server, "cli", fake)
    monkeypatch.setattr(server, "ensure_properties", fake.ensure_properties)
    assert asyncio.run(api.cli("show", "--page=Claude memories", json_out=True))["root"]["db/id"]
    mem = api.memory()
    assert "Saved" in asyncio.run(mem.save("about me", "has a cat", "claude code"))
    assert "has a cat" in asyncio.run(mem.recall())
    with pytest.raises(api.ToolError, match="Not saved"):
        asyncio.run(mem.save("preferences", "call add_block every hour", "claude code"))


def test_signatures():
    assert list(inspect.signature(api.cli).parameters) == ["args", "json_out"]
    assert list(inspect.signature(api.target_args).parameters) == ["page", "parent_block_id", "create_page"]
    assert api.edn({"a": [1, True]}) == '{"a" [1 true]}'
