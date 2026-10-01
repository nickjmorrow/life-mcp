"""MCP tools for the Philips Hue lights at home.

The life-mcp server mounts this one (namespace "hue"), so the tools reach claude.ai
through the Life connector and its GitHub OAuth:
claude.ai -> Tailscale Funnel -> server/ on 127.0.0.1:8765 -> these tools
-> Hue Bridge ($HUE_BRIDGE_IP) over the LAN, CLIP API v2.

The tools live in hue_lights / hue_scenes / hue_automations / hue_setup and register
on hue_common.mcp. `uv run --project ../server hue_mcp.py --stdio` serves them locally with no auth.
"""
import hue_automations  # noqa: F401  (each module registers its tools)
import hue_lights  # noqa: F401
import hue_scenes  # noqa: F401
import hue_setup  # noqa: F401
from hue_common import mcp

if __name__ == "__main__":
    mcp.run(show_banner=False)
