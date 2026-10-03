# tests/test_smoke.py
import json
import os
import subprocess
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
    assert set(smoke.CHECKS) | set(smoke.LOCAL_CHECKS) == {"logseq", "private"} | {g.label for g in server.GROUPS}
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


# --- the private folder: the live tree, here and in the running connector -----------------------------------------


@pytest.fixture
def live_tree(tmp_path, monkeypatch):
    """A made-up live tree, this process reading it, and the running connector's record saying it does too."""
    import server
    live = tmp_path / "live"
    live.mkdir()
    monkeypatch.setattr(private, "LIVE", live)
    monkeypatch.setattr(private, "DIR", live)
    record = tmp_path / "running.json"
    record.write_text(json.dumps({"pid": os.getpid(), "private_dir": str(live), "started_at": "2026-10-05T07:00:00"}))
    monkeypatch.setattr(server, "RUNNING_FILE", record)
    return SimpleNamespace(live=live, record=record)


def test_private_check_passes_when_both_read_the_live_tree(no_server, live_tree, capsys):
    assert smoke.run(["private"]) == 0
    assert "ok    private      live tree" in capsys.readouterr().out


def test_private_check_has_nothing_to_check_without_a_live_tree(no_server, tmp_path, monkeypatch):
    monkeypatch.setattr(private, "LIVE", tmp_path / "none")
    monkeypatch.setattr(private, "DIR", tmp_path / "checkout")
    assert smoke.run(["private"]) == 0


def test_private_check_fails_when_this_process_reads_the_checkout(no_server, live_tree, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(private, "DIR", tmp_path / "checkout")
    assert smoke.run(["private"]) == 1
    assert "FAIL  private" in capsys.readouterr().out


def test_private_check_fails_when_the_connector_reads_the_checkout(no_server, live_tree, tmp_path, capsys):
    live_tree.record.write_text(json.dumps({"pid": os.getpid(), "private_dir": str(tmp_path / "checkout")}))
    assert smoke.run(["private"]) == 1
    out = capsys.readouterr().out
    assert "FAIL  private" in out and "running connector reads" in out


def test_private_check_skips_the_connector_on_a_mac_without_one(no_server, live_tree, capsys):
    """A Mac with a live tree but no connector (no record): only this process is checked, and that's ok."""
    live_tree.record.unlink()
    assert smoke.run(["private"]) == 0
    out = capsys.readouterr().out
    assert "ok    private" in out and "no connector here" in out


def test_private_check_skips_a_record_from_a_connector_that_stopped(no_server, live_tree, capsys):
    proc = subprocess.Popen(["/usr/bin/true"])
    proc.wait()  # a pid that has exited (our own child's, reaped)
    live_tree.record.write_text(json.dumps({"pid": proc.pid, "private_dir": str(tmp_checkout := live_tree.live.parent / "checkout")}))
    assert smoke.run(["private"]) == 0
    out = capsys.readouterr().out
    assert "ok    private" in out and "no connector here" in out and str(tmp_checkout) not in out


def test_expect_connector_fails_when_no_connector_runs(no_server, live_tree, capsys):
    """Edgar's deploy passes --expect-connector: a crashed connector there must not pass as "no connector here"."""
    live_tree.record.unlink()
    assert smoke.main(["--expect-connector", "private"]) == 1
    out = capsys.readouterr().out
    assert "FAIL  private" in out and "no connector is running" in out


def test_expect_connector_fails_when_the_records_pid_has_exited(no_server, live_tree, capsys):
    proc = subprocess.Popen(["/usr/bin/true"])
    proc.wait()
    live_tree.record.write_text(json.dumps({"pid": proc.pid, "private_dir": str(live_tree.live)}))
    assert smoke.main(["--expect-connector", "private"]) == 1
    assert "no connector is running" in capsys.readouterr().out


def test_expect_connector_fails_without_a_live_tree_too(no_server, tmp_path, monkeypatch, capsys):
    import server
    monkeypatch.setattr(private, "LIVE", tmp_path / "none")
    monkeypatch.setattr(private, "DIR", tmp_path / "checkout")
    monkeypatch.setattr(server, "RUNNING_FILE", tmp_path / "running.json")
    assert smoke.main(["--expect-connector", "private"]) == 1
    assert smoke.main(["private"]) == 0


def test_expect_connector_passes_with_a_running_connector_on_the_live_tree(no_server, live_tree, capsys):
    assert smoke.main(["--expect-connector", "private"]) == 0
    out = capsys.readouterr().out
    assert "ok    private" in out and "no connector here" not in out


def test_without_the_flag_no_connector_stays_ok(no_server, live_tree, capsys):
    live_tree.record.unlink()
    assert smoke.main(["private"]) == 0
    assert "no connector here" in capsys.readouterr().out


def test_private_check_still_checks_this_process_without_a_connector(no_server, live_tree, tmp_path, monkeypatch, capsys):
    live_tree.record.unlink()
    monkeypatch.setattr(private, "DIR", tmp_path / "checkout")
    assert smoke.run(["private"]) == 1
    assert "FAIL  private" in capsys.readouterr().out


def test_private_check_fails_on_an_unreadable_record_from_a_live_connector(no_server, live_tree, capsys):
    live_tree.record.write_text(json.dumps({"pid": os.getpid()}))
    assert smoke.run(["private"]) == 1
    assert "hasn't said" in capsys.readouterr().out


def test_smoke_never_writes_the_running_servers_tool_list(monkeypatch):
    import server
    real = server.TOOLS_FILE  # the conftest's temp path stands in for the real one
    monkeypatch.setattr(smoke, "_server", None)
    monkeypatch.setattr(server, "mount_all", lambda *a, **k: [])
    assert smoke.load_server() is server
    assert server.TOOLS_FILE != real and server.TOOLS_FILE.parent.name.startswith("smoke-")
    server.TOOLS_FILE.parent.rmdir()


def test_the_server_says_which_private_folder_it_reads(tmp_path, monkeypatch):
    import server
    monkeypatch.setattr(server, "RUNNING_FILE", tmp_path / "life-mcp" / "running.json")
    server.write_running()
    record = json.loads(server.RUNNING_FILE.read_text())
    assert record["pid"] == os.getpid() and record["private_dir"] == str(private.DIR)
    assert oct(server.RUNNING_FILE.stat().st_mode & 0o777) == "0o600"
