"""The forge_* tools: the Forge workout tracker's own MCP server (a stdio script) proxied.

Forge keeps exercises, routines, workouts, recovery and stats in one SQLite file that its apps sync to; these tools work on the
copy kept on this Mac. Settings come from the environment, defaulting to folders under the home directory:
FORGE_MCP_SCRIPT (forge_mcp.py from the Forge repo), FORGE_BIN (its `forge` command line) and FORGE_DB (the database).
Importing this fails if the script or the command is missing, so server.py skips the group.
"""
import os
import sys
from pathlib import Path

from fastmcp.server import create_proxy

HOME = Path.home()
SCRIPT = Path(os.environ.get("FORGE_MCP_SCRIPT") or HOME / "Projects" / "forge" / "mcp" / "forge_mcp.py")
FORGE_BIN = Path(os.environ.get("FORGE_BIN") or HOME / ".local" / "bin" / "forge")
DB = os.environ.get("FORGE_DB") or str(HOME / "forge-hub" / "forge.sqlite")

for needed in (SCRIPT, FORGE_BIN):
    if not needed.exists():
        raise ImportError(f"{needed} is missing")

# The script is standard library only, so the connector's own interpreter runs it (launchd's PATH has no python).
mcp = create_proxy(
    {"mcpServers": {"forge": {"command": sys.executable, "args": [str(SCRIPT)],
                              "env": {"FORGE_DB": DB, "FORGE_BIN": str(FORGE_BIN), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                      "FORGE_AUTOSYNC": "0"}}}},   # this copy is the sync hub itself
    # The script speaks the plain initialize-handshake protocol, like the Eight Sleep server.
    mode="legacy",
    name="Forge",
)
