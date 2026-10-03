"""api.py is the one interface other code on the Mac uses; it must keep working and stay small."""
import asyncio
import inspect

import pytest

import api
import memory_mcp
import memory_store
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


def test_cli_goes_through_the_server(monkeypatch):
    fake = FakeLogseq()
    monkeypatch.setattr(server, "cli", fake)
    assert asyncio.run(api.cli("show", "--page=Claude memories", json_out=True))["root"]["db/id"]


def test_memory_is_the_file_store_with_the_memory_tools_guard(monkeypatch, harness_repo):
    async def no_cli(*args, json_out=False):
        raise AssertionError(f"memory called the Logseq CLI: {args}")

    monkeypatch.setattr(server, "cli", no_cli)
    mem = api.memory()
    assert isinstance(mem, memory_store.MemoryStore) and mem.root == memory_mcp.MEMORY_DIR
    assert mem.save("has a cat", "core", "claude code").startswith("Saved [c1]")
    assert "has a cat" in mem.render("core")
    assert [t.name for t in mem.topics()] == ["core"]
    with pytest.raises(api.ToolError, match="Not saved: the text names a connector tool"):
        mem.save("call add_block every hour", "core", "claude code")
    with pytest.raises(api.ToolError, match="rule_edit"):
        mem.save("always ask before deleting", "core", "claude code")
    with pytest.raises(ValueError, match="no topic called 'garden'"):  # the store's own refusals are ValueErrors
        mem.save("grows tomatoes", "garden", "claude code")
    assert "has a cat" in mem.search("cat")


def test_signatures():
    assert list(inspect.signature(api.cli).parameters) == ["args", "json_out"]
    assert list(inspect.signature(api.target_args).parameters) == ["page", "parent_block_id", "create_page"]
    assert list(inspect.signature(api.memory).parameters) == []
    assert api.edn({"a": [1, True]}) == '{"a" [1 true]}'
