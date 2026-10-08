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
    assert set(smoke.CHECKS) | set(smoke.LOCAL_CHECKS) == {"private"} | {g.label for g in server.GROUPS}
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
    assert smoke.LOCAL_CHECKS["memory"][0] == "read_store().topics()"
    assert "memory" not in smoke.CHECKS


@pytest.fixture
def clean_memory(tmp_path, monkeypatch):
    """The clean copy's memory folder, which the check reads (private.DIR/memory)."""
    clean = tmp_path / "clean"
    monkeypatch.setattr(private, "DIR", clean)
    return clean / "memory"


def test_memory_check_reads_the_store_and_writes_nothing(no_server, capsys, clean_memory):
    folder = clean_memory
    (folder / "topics").mkdir(parents=True)
    (folder / "core.md").write_text("# core (c): facts that change most answers\n\n- [c1] likes oolong tea (2026-10-05, phone)\n")
    (folder / "topics" / "home.md").write_text("# home (o): home, pets, household\n")
    before = listing(folder)
    assert smoke.run(["memory"]) == 0
    assert "ok    memory       read_store().topics()" in capsys.readouterr().out
    assert listing(folder) == before  # no new file, no commit, no change
    assert not usage_log.PATH.exists()  # a smoke run is not a chat that called memory_recall


def test_memory_check_passes_before_anything_is_saved(no_server, capsys, clean_memory):
    assert not clean_memory.exists()
    assert smoke.run(["memory"]) == 0
    assert not clean_memory.exists()  # not even the folder is made


def test_memory_check_fails_when_a_memory_file_is_broken(no_server, capsys, clean_memory):
    (clean_memory / "topics").mkdir(parents=True)
    (clean_memory / "topics" / "home.md").write_text("no header here\n")
    assert smoke.run(["memory"]) == 1
    out = capsys.readouterr().out
    assert "FAIL  memory" in out and "home" in out


@pytest.fixture
def harness_command(tmp_path, monkeypatch):
    """A made-up harness command whose check-setup prints ok (or whatever the test writes into it)."""
    script = tmp_path / "harness-command"
    script.write_text('#!/bin/sh\n[ "$1" = check-setup ] && echo ok\n')
    script.chmod(0o755)
    monkeypatch.setitem(private.CONFIG, "harness", {"command": [str(script)]})
    return script


def test_changes_check_runs_check_setup_and_nothing_else(no_server, harness_command, capsys, tmp_path):
    harness_command.write_text(f'#!/bin/sh\necho "$@" >> {tmp_path / "argv"}\necho ok\n')
    assert smoke.run(["changes"]) == 0
    assert "ok    changes      check-setup" in capsys.readouterr().out
    assert (tmp_path / "argv").read_text().split() == ["check-setup"]  # no edit, no ship, no push


def test_changes_check_fails_when_check_setup_does(no_server, harness_command, capsys):
    harness_command.write_text("#!/bin/sh\necho 'no bot token' >&2\nexit 1\n")
    assert smoke.run(["changes"]) == 1
    out = capsys.readouterr().out
    assert "FAIL  changes" in out and "no bot token" in out


def test_changes_check_fails_without_a_command(no_server, monkeypatch, capsys):
    monkeypatch.delitem(private.CONFIG, "harness", raising=False)
    assert smoke.run(["changes"]) == 1
    out = capsys.readouterr().out
    assert "FAIL  changes" in out and "aren't set up" in out


def test_memory_check_fails_when_saves_would_go_to_the_wrong_branch(no_server, harness_repo, capsys, clean_memory):
    import subprocess
    subprocess.run(["git", "checkout", "-q", "-b", "main"], cwd=harness_repo, check=True)
    assert smoke.run(["memory"]) == 1
    assert "dev" in capsys.readouterr().out


def test_memory_check_passes_with_the_working_copy_on_dev(no_server, harness_repo, capsys, clean_memory):
    assert smoke.run(["memory"]) == 0


def test_every_group_is_run_when_none_is_named(no_server, harness_command, monkeypatch, capsys):
    ran = []

    async def call(client, tool, args):
        ran.append(tool)
    monkeypatch.setattr(smoke, "call", call)
    assert smoke.run([]) == 0
    assert len(ran) == len(smoke.CHECKS) and "ok    memory" in capsys.readouterr().out


# --- the private folder: the clean copy, here and in the running connector -----------------------------------------


@pytest.fixture
def clean_copy(tmp_path, monkeypatch):
    """A made-up clean copy, this process reading it, and the running connector's record saying it does too."""
    import server
    live = tmp_path / "live"
    live.mkdir()
    monkeypatch.setattr(private, "CLEAN", live)
    monkeypatch.setattr(private, "DIR", live)
    record = tmp_path / "running.json"
    record.write_text(json.dumps({"pid": os.getpid(), "private_dir": str(live), "started_at": "2026-10-05T07:00:00"}))
    monkeypatch.setattr(server, "RUNNING_FILE", record)
    return SimpleNamespace(live=live, record=record)


def test_private_check_passes_when_both_read_the_clean_copy(no_server, clean_copy, capsys):
    assert smoke.run(["private"]) == 0
    assert "ok    private      clean copy" in capsys.readouterr().out


def test_private_check_has_nothing_to_check_without_a_clean_copy(no_server, tmp_path, monkeypatch):
    monkeypatch.setattr(private, "CLEAN", tmp_path / "none")
    monkeypatch.setattr(private, "DIR", tmp_path / "checkout")
    assert smoke.run(["private"]) == 0


def test_private_check_fails_when_this_process_reads_the_checkout(no_server, clean_copy, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(private, "DIR", tmp_path / "checkout")
    assert smoke.run(["private"]) == 1
    assert "FAIL  private" in capsys.readouterr().out


def test_private_check_fails_when_the_connector_reads_the_checkout(no_server, clean_copy, tmp_path, capsys):
    clean_copy.record.write_text(json.dumps({"pid": os.getpid(), "private_dir": str(tmp_path / "checkout")}))
    assert smoke.run(["private"]) == 1
    out = capsys.readouterr().out
    assert "FAIL  private" in out and "running connector reads" in out


def test_private_check_skips_the_connector_on_a_mac_without_one(no_server, clean_copy, capsys):
    """A Mac with a clean copy but no connector (no record): only this process is checked, and that's ok."""
    clean_copy.record.unlink()
    assert smoke.run(["private"]) == 0
    out = capsys.readouterr().out
    assert "ok    private" in out and "no connector here" in out


def test_private_check_skips_a_record_from_a_connector_that_stopped(no_server, clean_copy, capsys):
    proc = subprocess.Popen(["/usr/bin/true"])
    proc.wait()  # a pid that has exited (our own child's, reaped)
    clean_copy.record.write_text(json.dumps({"pid": proc.pid, "private_dir": str(tmp_checkout := clean_copy.live.parent / "checkout")}))
    assert smoke.run(["private"]) == 0
    out = capsys.readouterr().out
    assert "ok    private" in out and "no connector here" in out and str(tmp_checkout) not in out


def test_expect_connector_fails_when_no_connector_runs(no_server, clean_copy, capsys):
    """Edgar's deploy passes --expect-connector: a crashed connector there must not pass as "no connector here"."""
    clean_copy.record.unlink()
    assert smoke.main(["--expect-connector", "private"]) == 1
    out = capsys.readouterr().out
    assert "FAIL  private" in out and "no connector is running" in out


def test_expect_connector_fails_when_the_records_pid_has_exited(no_server, clean_copy, capsys):
    proc = subprocess.Popen(["/usr/bin/true"])
    proc.wait()
    clean_copy.record.write_text(json.dumps({"pid": proc.pid, "private_dir": str(clean_copy.live)}))
    assert smoke.main(["--expect-connector", "private"]) == 1
    assert "no connector is running" in capsys.readouterr().out


def test_expect_connector_fails_without_a_clean_copy_too(no_server, tmp_path, monkeypatch, capsys):
    import server
    monkeypatch.setattr(private, "CLEAN", tmp_path / "none")
    monkeypatch.setattr(private, "DIR", tmp_path / "checkout")
    monkeypatch.setattr(server, "RUNNING_FILE", tmp_path / "running.json")
    assert smoke.main(["--expect-connector", "private"]) == 1
    assert smoke.main(["private"]) == 0


def test_expect_connector_passes_with_a_running_connector_on_the_clean_copy(no_server, clean_copy, capsys):
    assert smoke.main(["--expect-connector", "private"]) == 0
    out = capsys.readouterr().out
    assert "ok    private" in out and "no connector here" not in out


def test_without_the_flag_no_connector_stays_ok(no_server, clean_copy, capsys):
    clean_copy.record.unlink()
    assert smoke.main(["private"]) == 0
    assert "no connector here" in capsys.readouterr().out


def test_private_check_still_checks_this_process_without_a_connector(no_server, clean_copy, tmp_path, monkeypatch, capsys):
    clean_copy.record.unlink()
    monkeypatch.setattr(private, "DIR", tmp_path / "checkout")
    assert smoke.run(["private"]) == 1
    assert "FAIL  private" in capsys.readouterr().out


def test_private_check_fails_on_an_unreadable_record_from_a_live_connector(no_server, clean_copy, capsys):
    clean_copy.record.write_text(json.dumps({"pid": os.getpid()}))
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
