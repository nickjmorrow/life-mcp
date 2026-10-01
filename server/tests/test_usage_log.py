import asyncio
import json

from fastmcp import Client

import skills_mcp
import usage_log


def test_record_appends_one_line(tmp_path, monkeypatch):
    path = tmp_path / "usage" / "connector.jsonl"
    monkeypatch.setattr(usage_log, "PATH", path)
    usage_log.record("skill_file", skill="hevy", path="references/api.md")
    usage_log.record("memory_recall")
    lines = [json.loads(l) for l in path.read_text().splitlines()]
    assert [l["tool"] for l in lines] == ["skill_file", "memory_recall"]
    assert lines[0]["skill"] == "hevy" and lines[0]["path"] == "references/api.md"
    assert "skill" not in lines[1] and lines[1]["ts"].endswith("Z")
    assert path.stat().st_mode & 0o777 == 0o600 and path.parent.stat().st_mode & 0o777 == 0o700


def test_record_never_raises(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setattr(usage_log, "PATH", blocker / "connector.jsonl")  # parent is a file: can't write
    usage_log.record("skill_load", skill="x")


def test_skill_tools_are_logged(tmp_path, monkeypatch):
    log = tmp_path / "connector.jsonl"
    monkeypatch.setattr(usage_log, "PATH", log)
    d = tmp_path / "skills" / "hevy"
    (d / "references").mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: hevy\ndescription: d\n---\nbody\n")
    (d / "references" / "api.md").write_text("api")

    async def go():
        async with Client(skills_mcp.build(tmp_path / "skills")) as c:
            await c.call_tool("skill_load", {"name": "hevy"})
            await c.call_tool("skill_file", {"name": "hevy", "path": "references/api.md"})
            await c.call_tool("skill_list", {})
    asyncio.run(go())
    tools = [json.loads(l)["tool"] for l in log.read_text().splitlines()]
    assert tools == ["skill_load", "skill_file"]
