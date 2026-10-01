# tests/test_smoke.py
import smoke

def test_checks_cover_every_group():
    import server
    assert set(smoke.CHECKS) == {"logseq"} | {g.label for g in server.GROUPS}

def test_every_check_is_read_only():
    for group, (tool, args) in smoke.CHECKS.items():
        assert not any(w in tool for w in ("add", "set", "delete", "create", "update", "play", "power", "note", "save")), tool

def test_failure_sets_exit_code(monkeypatch):
    from types import SimpleNamespace
    from fastmcp import FastMCP
    async def boom(client, tool, args):
        raise RuntimeError("down")
    monkeypatch.setattr(smoke, "load_server", lambda: SimpleNamespace(mcp=FastMCP("empty")))
    monkeypatch.setattr(smoke, "call", boom)
    assert smoke.run(["hue"]) == 1
