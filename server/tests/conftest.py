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
    """Tests never write to the real connector usage log or the real Life Home token."""
    monkeypatch.setattr(usage_log, "PATH", tmp_path / "connector-usage.jsonl")
    import home_mcp
    monkeypatch.setattr(home_mcp, "TOKEN_PATH", tmp_path / "config" / "home-token")
