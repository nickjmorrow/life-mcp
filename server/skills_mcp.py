"""Nicholas's skills, served by the connector instead of uploaded to claude.ai.

The skills live in the private folder's skills/ (private.DIR) (one folder per skill with a SKILL.md, the same
format claude.ai and Claude Code use; Claude Code reads the folder directly through
~/.claude/skills). claude.ai has no copy: the connector's instructions carry a short index
(each skill's `trigger` frontmatter line), and Claude calls skill_load to get the full text.

Skill text is read fresh on every call, so edits are live at once. The index is built when
the service starts, so a new skill or a changed trigger needs a restart.

A skill can name a secret as {{VAR}} (only the names in SECRETS); skill_load fills it from
the service's environment (~/.zshrc.local via run.sh), so the repo never holds the value.
"""
import dataclasses
import os
import re
import sys
from pathlib import Path
from typing import Annotated

import yaml
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

import usage_log
import private

SKILLS_DIR = private.DIR / "skills"  # his agent's skills, in the private folder
SECRETS: tuple[str, ...] = ()  # none today: hevy_api keeps the Hevy key server-side
READ = {"readOnlyHint": True}


@dataclasses.dataclass
class Skill:
    name: str
    description: str
    trigger: str
    dir: Path


def parse(skill_md: Path) -> Skill:
    """A skill from its SKILL.md frontmatter. Raises ValueError if the frontmatter is unusable."""
    text = skill_md.read_text()
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if not m:
        raise ValueError("no frontmatter")
    meta = yaml.safe_load(m.group(1)) or {}
    name, description = meta.get("name"), meta.get("description")
    if not name or not description:
        raise ValueError("frontmatter needs name and description")
    return Skill(str(name), str(description).strip(), str(meta.get("trigger") or description).strip(),
                 skill_md.parent)


def load_all(root: Path) -> dict[str, Skill]:
    """Every skill under root, by name. A missing folder has none; broken ones are skipped with a note in the log."""
    skills = {}
    for skill_md in sorted(root.glob("*/SKILL.md")):
        try:
            s = parse(skill_md)
        except Exception as e:
            print(f"Skill {skill_md.parent.name} skipped: {e}", file=sys.stderr)
            continue
        skills[s.name] = s
    return skills


def fill(text: str) -> str:
    """Put the allowed secrets into {{VAR}} placeholders; others are left as written."""
    for var in SECRETS:
        if os.environ.get(var):
            text = text.replace("{{" + var + "}}", os.environ[var])
    return text


def instructions(skills: dict[str, Skill]) -> str:
    """The index claude.ai sees in every conversation. Kept short: clients cut long instructions."""
    lines = "; ".join(f"{s.name} ({s.trigger})" for s in skills.values())
    return (
        "Nicholas's own skills live in his private agent harness, not in claude.ai. Before any task that matches one,"
        " call skill_load with its name first and follow what it returns exactly as you would a skill."
        " They take priority over built-in skills for the same request (e.g. research over docs or"
        " deep-research), and '/name' in his message means that skill. Skills: " + lines + "."
    )


def build(root: Path | None = None) -> FastMCP:
    root = root or SKILLS_DIR
    mcp = FastMCP("Skills")

    def find(name: str) -> Skill:
        skills = load_all(root)
        skill = skills.get(name.strip().lstrip("/").lower())
        if not skill:
            raise ToolError(f"No skill named {name!r}. Skills: {', '.join(skills) or 'none'}.")
        return skill

    def others(skill: Skill) -> list[str]:
        return sorted(str(p.relative_to(skill.dir)) for p in skill.dir.rglob("*")
                      if p.is_file() and p.name != "SKILL.md" and not p.name.startswith("."))

    def body(skill: Skill) -> str:
        text = fill((skill.dir / "SKILL.md").read_text())
        files = others(skill)
        if files:
            text += ("\n\n---\nFiles in this skill (read them with skill_file when the text points to"
                     " them): " + ", ".join(files))
        return text

    @mcp.tool(annotations=READ)
    def skill_load(
        name: Annotated[str, Field(description="Skill name from the index, e.g. 'research'")],
    ) -> str:
        """Load one of Nicholas's skills (full instructions). Call before a task that matches a skill
        in the index, then follow the returned text as the skill."""
        skill = find(name)
        usage_log.record("skill_load", skill=skill.name)
        return body(skill)

    @mcp.tool(annotations=READ)
    def skill_file(
        name: Annotated[str, Field(description="Skill name")],
        path: Annotated[str, Field(description="File path inside the skill, e.g. 'references/api.md'")],
    ) -> str:
        """Read a supporting file from a skill (references, templates) that its instructions point to."""
        skill = find(name)
        target = (skill.dir / path).resolve()
        if not target.is_relative_to(skill.dir.resolve()) or not target.is_file():
            raise ToolError(f"No file {path!r} in {skill.name}. Files: {', '.join(others(skill)) or 'none'}.")
        usage_log.record("skill_file", skill=skill.name, path=path)
        try:
            return fill(target.read_text())
        except UnicodeDecodeError:
            raise ToolError(f"{path} is a binary file; it can only be used on his Macs.") from None

    @mcp.tool(annotations=READ)
    def skill_list() -> str:
        """List every skill with its full description (the index shows only short triggers)."""
        skills = load_all(root)
        return "\n\n".join(f"{s.name}: {s.description}" for s in skills.values()) or "No skills."

    # One prompt per skill, so claude.ai's attach menu can start one directly (like /research).
    def make_prompt(name: str):
        def prompt(request: str = "") -> str:
            text = body(find(name))
            return text + (f"\n\n---\nNicholas's request: {request}" if request else "")
        return prompt

    for skill in load_all(root).values():
        mcp.prompt(make_prompt(skill.name), name=skill.name, description=skill.trigger)

    return mcp
