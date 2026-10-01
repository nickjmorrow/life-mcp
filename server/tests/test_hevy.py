import asyncio
import json

import httpx
import pytest
from fastmcp.exceptions import ToolError

import hevy_mcp


def run(*args, **kw):
    return asyncio.run(hevy_mcp.request(*args, **kw))


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("HEVY_API_KEY", "k-123")


def transport(seen, status=200, payload=None):
    def handle(request):
        seen.append(request)
        return httpx.Response(status, json=payload if payload is not None else {"ok": True})
    return httpx.MockTransport(handle)


def test_adds_the_key_server_side():
    seen = []
    out = run("GET", "/v1/workouts", query={"page": 1}, transport=transport(seen, payload={"workouts": []}))
    assert json.loads(out) == {"workouts": []}
    assert seen[0].headers["api-key"] == "k-123" and str(seen[0].url) == "https://api.hevyapp.com/v1/workouts?page=1"
    assert "k-123" not in out


def test_put_sends_body():
    seen = []
    run("PUT", "v1/routines/abc", body={"routine": {"title": "Legs"}}, transport=transport(seen))
    assert seen[0].method == "PUT" and json.loads(seen[0].content) == {"routine": {"title": "Legs"}}


@pytest.mark.parametrize("path", ["https://evil.example/v1/x", "/v1/../secrets", "/v2/workouts", "/v1/workouts?x=1", ""])
def test_only_api_paths(path):
    with pytest.raises(ToolError, match="path must be"):
        run("GET", path, transport=transport([]))


def test_revoked_key():
    with pytest.raises(ToolError, match="401"):
        run("GET", "/v1/workouts", transport=transport([], status=401))
