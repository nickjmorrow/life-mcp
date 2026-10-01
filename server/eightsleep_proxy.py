"""The eight_sleep_* tools of the npm Eight Sleep MCP server, proxied over stdio.

Its login lives in ~/.eight-sleep-mcp/. Node is named explicitly because launchd's PATH has no
/opt/homebrew/bin. Importing this fails if the npm server isn't installed, so server.py skips the group.
"""
import os

from fastmcp.server import create_proxy

EIGHT_SLEEP = "/opt/homebrew/bin/eight-sleep-mcp-server"  # npm: eight-sleep-mcp-unofficial
NODE = "/opt/homebrew/bin/node"

if not os.path.exists(EIGHT_SLEEP):
    raise ImportError(f"{EIGHT_SLEEP} is missing")

mcp = create_proxy(
    {"mcpServers": {"eight_sleep": {"command": NODE, "args": [os.path.realpath(EIGHT_SLEEP)]}}},
    # The npm server speaks only the older initialize-handshake protocol era.
    mode="legacy",
    name="Eight Sleep",
)
