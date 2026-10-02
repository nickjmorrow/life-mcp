import asyncio

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import server
import skills_mcp


def run(coro):
    return asyncio.run(coro)


def write(root, name, front, body="Do the thing.", files=None):
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\n{front}\n---\n\n{body}\n")
    for path, content in (files or {}).items():
        (d / path).parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            (d / path).write_bytes(content)
        else:
            (d / path).write_text(content)
    return d


@pytest.fixture
def root(tmp_path):
    write(tmp_path, "research", 'name: research\ndescription: "Plan and publish a report."\ntrigger: "/research; deep dives"',
          files={"references/notes.md": "key {{HEVY_API_KEY}}", "font.woff2": b"\x00\xff\xfe"})
    write(tmp_path, "buddy", "name: buddy\ndescription: >\n  Coach for flat days.\n  Use on /buddy.")
    write(tmp_path, "broken", "description: no name here")
    (tmp_path / "not-a-skill").mkdir()
    return tmp_path


def call(mcp, tool, **args):
    async def go():
        async with Client(mcp) as c:
            return (await c.call_tool(tool, args)).content[0].text
    return run(go())


def test_load_all_skips_broken_and_uses_description_when_no_trigger(root):
    skills = skills_mcp.load_all(root)
    assert list(skills) == ["buddy", "research"]
    assert skills["research"].trigger == "/research; deep dives"
    assert skills["buddy"].trigger == "Coach for flat days. Use on /buddy."


def test_instructions_list_every_trigger(root):
    text = skills_mcp.instructions(skills_mcp.load_all(root))
    assert "skill_load" in text
    assert "research (/research; deep dives)" in text and "buddy (Coach for flat days." in text


def test_skill_load_returns_full_text_and_lists_files(root):
    out = call(skills_mcp.build(root), "skill_load", name="/Research")
    assert "Do the thing." in out and "name: research" in out
    assert "references/notes.md" in out and "font.woff2" in out


def test_skill_load_reads_edits_live(root):
    mcp = skills_mcp.build(root)
    (root / "buddy" / "SKILL.md").write_text("---\nname: buddy\ndescription: x\n---\nNew text.\n")
    assert "New text." in call(mcp, "skill_load", name="buddy")


def test_new_skill_loadable_before_restart(root):
    mcp = skills_mcp.build(root)
    write(root, "hevy", "name: hevy\ndescription: lifts")
    assert "Do the thing." in call(mcp, "skill_load", name="hevy")


def test_unknown_skill_names_the_real_ones(root):
    with pytest.raises(ToolError, match="buddy, research"):
        call(skills_mcp.build(root), "skill_load", name="nope")


def test_hevy_key_never_reaches_skill_text(root, monkeypatch):
    monkeypatch.setenv("HEVY_API_KEY", "k-123")  # hevy_api keeps it server-side now
    assert "k-123" not in call(skills_mcp.build(root), "skill_file", name="research", path="references/notes.md")


def test_secrets_filled_only_from_env(root, monkeypatch):
    monkeypatch.setattr(skills_mcp, "SECRETS", ("HEVY_API_KEY",))
    monkeypatch.setenv("HEVY_API_KEY", "k-123")
    assert call(skills_mcp.build(root), "skill_file", name="research", path="references/notes.md") == "key k-123"
    monkeypatch.delenv("HEVY_API_KEY")
    assert "{{HEVY_API_KEY}}" in call(skills_mcp.build(root), "skill_file", name="research", path="references/notes.md")


def test_other_placeholders_untouched(monkeypatch):
    monkeypatch.setenv("HOME_SECRET", "x")
    assert skills_mcp.fill("{{HOME_SECRET}}") == "{{HOME_SECRET}}"


def test_skill_file_refuses_paths_outside_the_skill(root):
    (root / "secret.txt").write_text("nope")
    with pytest.raises(ToolError, match="No file"):
        call(skills_mcp.build(root), "skill_file", name="research", path="../secret.txt")


def test_skill_file_binary_is_an_error(root):
    with pytest.raises(ToolError, match="binary"):
        call(skills_mcp.build(root), "skill_file", name="research", path="font.woff2")


def test_skill_list_has_full_descriptions(root):
    out = call(skills_mcp.build(root), "skill_list")
    assert "research: Plan and publish a report." in out and "buddy:" in out


def test_prompts_one_per_skill(root):
    async def go():
        async with Client(skills_mcp.build(root)) as c:
            names = {p.name for p in await c.list_prompts()}
            got = await c.get_prompt("research", {"request": "heat pumps"})
            return names, got.messages[0].content.text
    names, text = run(go())
    assert names == {"research", "buddy"}
    assert "Do the thing." in text and "Nicholas's request: heat pumps" in text


def test_skills_mount_puts_index_first():
    from fastmcp import FastMCP
    target = FastMCP("t", instructions="base")
    assert server.mount(server.group("skills"), target)
    names = {t.name for t in run(target.list_tools())}
    assert {"skill_load", "skill_file", "skill_list"} <= names
    assert target.instructions.startswith("Nicholas's own skills live in his private agent harness")
    assert target.instructions.endswith("base")


def test_memory_recall_carries_live_skill_index(monkeypatch, tmp_path):
    # claude.ai always calls memory_recall first, so the skill index rides along with it, read fresh each time.
    import memory_mcp

    write(tmp_path, "research", 'name: research\ndescription: d\ntrigger: "/research"')
    monkeypatch.setattr(skills_mcp, "SKILLS_DIR", tmp_path)
    mem = memory_mcp.build()
    assert "research (/research)" in call(mem, "memory_recall")
    write(tmp_path, "buddy", 'name: buddy\ndescription: d\ntrigger: "/buddy"')  # no restart needed
    assert "buddy (/buddy)" in call(mem, "memory_recall")


def test_missing_skills_folder_is_fine(tmp_path):
    assert skills_mcp.load_all(tmp_path / "no-such-private-repo") == {}
