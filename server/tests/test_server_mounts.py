import asyncio
from pathlib import Path

import pytest
from fastmcp import FastMCP

import memory_mcp
import server

# A few tools each group must serve once mounted (every group in server.GROUPS needs an entry).
EXPECTED = {
    "memory": {"memory_recall", "memory_save", "memory_update"},
    "health": {"health_summary", "health_compare", "health_query"},
    "people": {"people_find", "people_keep_in_touch", "people_note"},
    "cards": {"cards_status", "cards_next", "cards_rate"},
    "hue": {"hue_status", "hue_set_light", "hue_list_home"},
    "eight_sleep": {"eight_sleep_get_me"},
    "eight_sleep_extras": {"eight_sleep_list_alarms", "eight_sleep_pod_status", "eight_sleep_set_bedtime"},
    "reminders": {"reminders_add", "reminders_list_lists"},
    "hevy": {"hevy_api"},
    "music": {"music_play", "music_now_playing", "music_set_volume"},
    "music_library": {"music_recent", "music_taste", "music_status"},
    "tv": {"tv_status", "tv_open_app", "tv_remote"},
    "home": {"home_list", "home_set", "home_run_scene", "home_motion"},
    "skills": {"skill_load", "skill_file", "skill_list"},
}
HERE = Path(server.__file__).parent


def names(mcp):
    return {t.name for t in asyncio.run(mcp.list_tools())}


def test_every_group_has_expectations():
    assert {g.label for g in server.GROUPS} == set(EXPECTED)


@pytest.mark.parametrize("g", [g for g in server.GROUPS if g.label != "eight_sleep"], ids=lambda g: g.label)
def test_group_mounts_its_tools(g):
    # (eight_sleep proxies a separately installed npm server, so it's skipped when that's missing.)
    if not ((g.path or HERE) / f"{g.module}.py").exists():
        pytest.skip(f"{g.module}.py isn't in this checkout")
    target = FastMCP("t")
    assert server.mount(g, target)
    assert EXPECTED[g.label] <= names(target)


def test_one_away_tool_and_nothing_hidden():
    # The npm server's eight_sleep_set_away_mode works on his account (checked live 2026-09-26),
    # so there's no replacement and nothing is hidden.
    target = FastMCP("t")
    server.mount(server.group("eight_sleep_extras"), target)
    assert "eight_sleep_away" not in names(target)
    assert not hasattr(server, "HideTools")


def test_a_group_that_fails_to_load_is_logged_and_skipped(capsys):
    target = FastMCP("t", instructions="base")
    broken = server.Group("vacuum", "no_such_module_here", instructions=" vacuum stuff")
    loaded = server.mount_all(target, (broken, server.group("tv")))
    assert loaded == ["tv"]
    assert "vacuum tools not loaded" in capsys.readouterr().err
    assert "tv_status" in names(target) and "vacuum" not in target.instructions


def test_instructions_are_added_in_order_and_skills_go_first():
    target = FastMCP("t", instructions="base.")
    server.mount_all(target, (server.group("memory"), server.group("people"), server.group("skills")))
    text = target.instructions
    assert text.startswith("Nicholas's own skills live in his private agent harness")
    assert text.index("base.") < text.index("Claude memories") < text.index("people_note")


def test_mount_all_hands_the_memory_guard_every_tool(monkeypatch):
    monkeypatch.setattr(memory_mcp, "TOOL_NAMES", set())
    target = FastMCP("t")
    server.mount_all(target, (server.group("tv"),))
    assert "tv_open_app" in asyncio.run(memory_mcp.tool_names())


def test_the_main_block_uses_mount_all():
    src = (HERE / "server.py").read_text()
    assert "mount_all()" in src.split('if __name__ == "__main__":')[1]


def test_ensure_properties_sees_existing_titles(monkeypatch):
    # `logseq list property` items carry "block/title", not "title"; existing names must not be re-created.
    calls = []

    async def fake_cli(*args, json_out=False):
        calls.append(args)
        if args[:2] == ("list", "property"):
            return {"items": [{"block/title": "saved-on"}]}
        return {"result": [1]}

    monkeypatch.setattr(server, "cli", fake_cli)
    asyncio.run(server.ensure_properties(["saved-on", "stage"]))
    assert [a for a in calls if a[:2] == ("upsert", "property")] == [("upsert", "property", "--name=stage")]
