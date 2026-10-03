import os

# Tests never read Nicholas's private settings and always use one fixed time zone, so they behave the same
# on any machine. Set before any module under test is imported (they read these at import time).
os.environ["LIFE_MCP_PRIVATE"] = os.path.join(os.path.dirname(__file__), "no-private-repo")
os.environ["TZ"] = "America/Chicago"

import host  # noqa: E402  modules copy host.NAME at import, so set it before any of them loads

host.NAME = "TestMac"

import pytest  # noqa: E402

import usage_log


@pytest.fixture(autouse=True)
def _usage_log_in_tmp(tmp_path, monkeypatch):
    """Tests never write to the real connector usage log, the real memory folder, the real Life Home token or the real
    tool list (a server that starts writes it, and any test that opens a client on a mounted server starts one)."""
    monkeypatch.setattr(usage_log, "PATH", tmp_path / "connector-usage.jsonl")
    import memory_mcp
    monkeypatch.setattr(memory_mcp, "WORKING_COPY", tmp_path / "harness")  # memory is written inside this repo folder
    monkeypatch.setattr(memory_mcp, "MEMORY_DIR", tmp_path / "harness" / "memory")
    monkeypatch.setattr(memory_mcp, "LOCK_PATH", None)
    import home_mcp
    monkeypatch.setattr(home_mcp, "TOKEN_PATH", tmp_path / "config" / "home-token")
    import server
    monkeypatch.setattr(server, "TOOLS_FILE", tmp_path / "tools.json")
    monkeypatch.setattr(server, "RUNNING_FILE", tmp_path / "running.json")
    import private  # a clean copy on the Mac running the tests must not change what they check
    monkeypatch.setattr(private, "CLEAN", tmp_path / "no-clean-copy")


@pytest.fixture
def harness_repo():
    """The working copy memory is written into: a git repo on dev with one commit, and nothing in memory/ yet."""
    import subprocess

    import memory_mcp
    repo = memory_mcp.WORKING_COPY
    repo.mkdir(parents=True, exist_ok=True)
    run = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=repo,
                                    capture_output=True, text=True, check=True)
    run("init", "-q", "-b", "dev")
    (repo / "README.md").write_text("harness\n")
    run("add", "-A")
    run("commit", "-q", "-m", "start")
    return repo
