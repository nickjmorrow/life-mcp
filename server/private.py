"""Personal settings and skills, kept out of this public repo, in a private folder: <dir>/config.toml
and <dir>/skills. LIFE_MCP_PRIVATE points at it. Without it, the default is the harness's live tree (LIVE: the last
commit its owner approved) when this Mac has one, else ~/Projects/personal-agent-harness (his agent's setup, a checkout
whose files may hold changes nobody approved). run.sh and cards-web.sh set LIFE_MCP_PRIVATE to the live tree after
loading ~/.zshrc.local (private-env.sh), so nothing there can move it. Missing file or key = the default, so the code
runs (and its tests pass) without it.
"""
import os
import tomllib
from pathlib import Path
from typing import Any

LIVE = Path.home() / "Library" / "Application Support" / "personal-agent" / "live"
CHECKOUT = Path.home() / "Projects" / "personal-agent-harness"


def _default() -> Path:
    return LIVE if LIVE.is_dir() else CHECKOUT


DIR = Path(os.environ.get("LIFE_MCP_PRIVATE") or _default()).expanduser()

try:
    CONFIG: dict[str, Any] = tomllib.loads((DIR / "config.toml").read_text())
except (FileNotFoundError, NotADirectoryError):
    CONFIG = {}


def get(key: str, default: Any = None) -> Any:
    """A top-level setting, or a dotted one like "home.apple_tv_ip"."""
    value: Any = CONFIG
    for part in key.split("."):
        if not isinstance(value, dict) or part not in value:
            return default
        value = value[part]
    return value
