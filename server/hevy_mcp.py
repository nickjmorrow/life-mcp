"""hevy_api: Hevy's REST API (workouts, routines, exercise history) with the API key kept on the server.

The key (HEVY_API_KEY, from ~/.zshrc.local via run.sh) is added here, so it never appears in a chat.
server.py mounts this server (namespace "hevy").
"""
import json
import os
import re
from typing import Annotated, Any, Literal

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

BASE = "https://api.hevyapp.com"
PATH = re.compile(r"^/?v1/[A-Za-z0-9_\-/]+$")  # API paths only: no host, query string or ".."
MAX_CHARS = 60_000

mcp = FastMCP("Hevy")


def _path(path: str) -> str:
    path = path.strip()
    if not PATH.match(path) or ".." in path:
        raise ToolError("path must be a Hevy API path like /v1/workouts or /v1/routines/<id>.")
    return "/" + path.lstrip("/")


async def request(method: str, path: str, query: dict | None = None, body: dict | None = None,
                  transport: Any = None) -> str:
    """Call Hevy's REST API with the server's key; returns the JSON response as text."""
    key = os.environ.get("HEVY_API_KEY")
    if not key:
        raise ToolError("HEVY_API_KEY isn't set on the server.")
    async with httpx.AsyncClient(base_url=BASE, timeout=30, transport=transport) as http:
        r = await http.request(method, _path(path), params=query, json=body if method != "GET" else None,
                               headers={"api-key": key, "accept": "application/json"})
    if r.status_code == 401:
        raise ToolError("Hevy rejected the API key (401): it was probably revoked; Nicholas makes a new one "
                        "at hevy.com → Settings → Developer and updates HEVY_API_KEY on the server.")
    if r.status_code >= 400:
        raise ToolError(f"Hevy answered {r.status_code}: {r.text[:500]}")
    text = json.dumps(r.json(), separators=(",", ":")) if r.content else "{}"
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS] + f"\n[cut at {MAX_CHARS} characters: page with pageSize]"


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True})
async def api(
    method: Literal["GET", "POST", "PUT"],
    path: Annotated[str, Field(description="API path, e.g. /v1/workouts, /v1/routines/<id>, /v1/exercise_history/<template id>")],
    query: Annotated[dict[str, Any] | None, Field(description="Query parameters, e.g. {\"page\": 1, \"pageSize\": 10}")] = None,
    body: Annotated[dict[str, Any] | None, Field(description="JSON body for POST/PUT")] = None,
) -> str:
    """Call Hevy's REST API as Nicholas (the server adds his API key). Returns the JSON response."""
    return await request(method, path, query, body)
