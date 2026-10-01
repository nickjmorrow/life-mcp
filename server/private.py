"""Personal settings and skills, kept out of this public repo, in a private folder: <dir>/config.toml
and <dir>/skills. LIFE_MCP_PRIVATE points at it (default ~/Projects/personal-agent-harness, his agent's setup). Missing file or
key = the default, so the code runs (and its tests pass) without it.
"""
import os
import tomllib
from pathlib import Path
from typing import Any

DIR = Path(os.environ.get("LIFE_MCP_PRIVATE", Path.home() / "Projects" / "personal-agent-harness")).expanduser()

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
