# tests/test_smoke.py
from types import SimpleNamespace

import httpx
import pytest
from fastmcp import FastMCP

import memory_mcp
import private
import smoke
import usage_log


def test_checks_cover_every_group():
    import server
    assert set(smoke.CHECKS) | set(smoke.LOCAL_CHECKS) == {"logseq"} | {g.label for g in server.GROUPS}
    assert not set(smoke.CHECKS) & set(smoke.LOCAL_CHECKS)

def test_every_check_is_read_only():
    for group, (tool, args) in smoke.CHECKS.items():
        assert not any(w in tool for w in ("add", "set", "delete", "create", "update", "play", "power", "note", "save")), tool

def test_failure_sets_exit_code(monkeypatch):
    async def boom(client, tool, args):
        raise RuntimeError("down")
    monkeypatch.setattr(smoke, "load_server", lambda: SimpleNamespace(mcp=FastMCP("empty")))
    monkeypatch.setattr(smoke, "call", boom)
    assert smoke.run(["hue"]) == 1


@pytest.fixture
def no_server(monkeypatch):
    """Local checks need no connector: a tool call here would fail the test."""
    async def boom(client, tool, args):
        raise AssertionError(f"called the tool {tool}")
    monkeypatch.setattr(smoke, "load_server", lambda: SimpleNamespace(mcp=FastMCP("empty")))
    monkeypatch.setattr(smoke, "call", boom)


def listing(folder):
    return sorted((str(p.relative_to(folder)), p.stat().st_mtime_ns) for p in folder.rglob("*"))


def test_memory_is_checked_by_reading_the_store_not_by_calling_a_tool():
    assert smoke.LOCAL_CHECKS["memory"][0] == "store().topics()"
    assert "memory" not in smoke.CHECKS


def test_memory_check_reads_the_store_and_writes_nothing(no_server, capsys):
    folder = memory_mcp.MEMORY_DIR
    (folder / "topics").mkdir(parents=True)
    (folder / "core.md").write_text("# core (c): facts that change most answers\n\n- [c1] likes oolong tea (2026-10-05, phone)\n")
    (folder / "topics" / "home.md").write_text("# home (o): home, pets, household\n")
    before = listing(folder)
    assert smoke.run(["memory"]) == 0
    assert "ok    memory       store().topics()" in capsys.readouterr().out
    assert listing(folder) == before  # no new file, no commit, no change
    assert not usage_log.PATH.exists()  # a smoke run is not a chat that called memory_recall


def test_memory_check_passes_before_anything_is_saved(no_server, capsys):
    assert not memory_mcp.MEMORY_DIR.exists()
    assert smoke.run(["memory"]) == 0
    assert not memory_mcp.MEMORY_DIR.exists()  # not even the folder is made


def test_memory_check_fails_when_a_memory_file_is_broken(no_server, capsys):
    (memory_mcp.MEMORY_DIR / "topics").mkdir(parents=True)
    (memory_mcp.MEMORY_DIR / "topics" / "home.md").write_text("no header here\n")
    assert smoke.run(["memory"]) == 1
    out = capsys.readouterr().out
    assert "FAIL  memory" in out and "home" in out


@pytest.fixture
def approvals_token(tmp_path, monkeypatch):
    """The approvals service's token, as the private config names it (a temp file with a made-up token)."""
    path = tmp_path / "propose-token"
    path.write_text("tok-4f9a1c\n")
    monkeypatch.setitem(private.CONFIG, "approvals", {"token_file": str(path)})
    return path


def test_proposals_check_passes_with_a_token_file_and_posts_nothing(no_server, approvals_token, monkeypatch, capsys):
    def posted(*args, **kwargs):
        raise AssertionError("the smoke check sent something to the approvals service")
    monkeypatch.setattr(httpx, "AsyncClient", posted)  # a proposal left on the page by a health check would be noise
    assert smoke.run(["proposals"]) == 0
    out = capsys.readouterr().out
    assert "ok    proposals    token file" in out
    assert "tok-4f9a1c" not in out  # it says the file is there, never what's in it


def test_proposals_check_fails_without_a_token_file(no_server, approvals_token, capsys):
    approvals_token.unlink()
    assert smoke.run(["proposals"]) == 1
    out = capsys.readouterr().out
    assert "FAIL  proposals" in out and "isn't set up" in out


def test_every_group_is_run_when_none_is_named(no_server, approvals_token, monkeypatch, capsys):
    ran = []

    async def call(client, tool, args):
        ran.append(tool)
    monkeypatch.setattr(smoke, "call", call)
    assert smoke.run([]) == 0
    assert len(ran) == len(smoke.CHECKS) and "ok    memory" in capsys.readouterr().out
