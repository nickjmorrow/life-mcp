"""The single-user lock: only the configured GitHub account, and nobody when none is configured."""
from types import SimpleNamespace

import pytest

import server


def tok(sub):
    return SimpleNamespace(claims={"sub": sub})


def test_only_the_configured_account(monkeypatch):
    monkeypatch.setattr(server, "ALLOWED_GITHUB_ID", "123")
    assert server.is_me(tok("123")) and server.is_me(tok(123))
    assert not server.is_me(tok("124")) and not server.is_me(None)


def test_nobody_when_unset(monkeypatch):
    monkeypatch.setattr(server, "ALLOWED_GITHUB_ID", "")
    assert not server.is_me(tok("")) and not server.is_me(tok(None))


def test_auth_refuses_to_start_without_an_account(monkeypatch):
    monkeypatch.setattr(server, "ALLOWED_GITHUB_ID", "")
    with pytest.raises(RuntimeError, match="LIFE_MCP_GITHUB_ID"):
        server.github_auth()
