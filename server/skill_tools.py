"""Skills that ride on their tools: a skill's text built into the description of the tool it's about.

claude.ai only sees the skill index if it calls memory_recall first, and when a request clearly names a tool's
domain ("quiz me", "what did I squat") it often finds that tool by tool search and skips both. A tool's description
is the one thing it always reads before calling the tool, so a tool-bound skill goes there (read once, at
startup: a change to the skill needs a connector restart). The skill's own folder still serves its other files
through skill_file.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

import private
import usage_log

SKILLS_DIR = private.DIR / "skills"
_logged: dict[str, float] = {}


def body(name: str, root: Path | None = None) -> str:
    """The skill's SKILL.md without its frontmatter, or "" if it can't be read."""
    try:
        text = ((root or SKILLS_DIR) / name / "SKILL.md").read_text()
    except OSError:
        return ""
    return re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S).strip()


def describe(doc: str, name: str, text: str | None = None) -> str:
    """doc, then the skill's text (read from disk unless given)."""
    text = body(name) if text is None else text
    if not text:
        return doc
    return (f"{doc.strip()}\n\nFollow his {name} skill, included here in full (no need to skill_load it; its other "
            f"files come from skill_file with skill '{name}'):\n\n{text}")


def used(name: str) -> None:
    """Count the skill as loaded for the lessons job's usage review, at most once per 30 minutes (one chat)."""
    if time.time() - _logged.get(name, 0.0) > 1800:
        _logged[name] = time.time()
        usage_log.record("skill_load", skill=name)
