"""CONTEXT.md: the one start-of-chat text, rendered from a folder of the harness repo (RULES.md, memory/, skills/).
Every name and fact here is made up; the folders are temp ones, never his real ones."""
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import context
import memory_store

SERVER = Path(__file__).resolve().parent.parent  # server/: the command line is run from elsewhere, by this path
RULES = "# Rules\n\n- [R1] Keep replies short.\n- [R2] Ask before deleting anything.\n"

TOPICS = {
    "core": ("c", "who he is: facts that change most answers"),
    "health": ("h", "body, fitness, sleep, diet; load for food, exercise or sleep"),
    "home": ("o", "home, pets, household; load for the house, pets or chores"),
}  # home gets the letter o because health already has h


def write_topic(root, name, prefix, about, body=""):
    path = root / ("core.md" if name == "core" else f"topics/{name}.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {name} ({prefix}): {about}\n" + (f"\n{body}\n" if body else ""))


def write_skill(repo, name, trigger):
    folder = repo / "skills" / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: does {name}\ntrigger: {trigger}\n---\nBody\n")


@pytest.fixture
def tmp_repo(tmp_path):
    """A repo folder: RULES.md, memory/ with three topics, no skills."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "RULES.md").write_text(RULES)
    for name, (prefix, about) in TOPICS.items():
        write_topic(repo / "memory", name, prefix, about)
    return repo


def test_render_order_rules_core_topics_skills(tmp_repo):
    write_topic(tmp_repo / "memory", "core", "c", TOPICS["core"][1], "- [c1] likes oolong tea (2026-10-01, phone)")
    write_skill(tmp_repo, "recipe", "write a recipe")
    out = context.render(tmp_repo)
    assert out.startswith(context.TITLE)
    marks = [out.index(m) for m in (context.RULES_HEADER, context.DATA_HEADER, context.TOPICS_HEADER,
                                    context.SKILLS_HEADER)]
    assert marks == sorted(marks)
    assert "[R1] Keep replies short." in out and "likes oolong tea" in out
    assert "- health (0): body, fitness, sleep, diet; load for food, exercise or sleep" in out
    assert "- recipe: write a recipe" in out
    assert "core (" not in out and "archive" not in out.split(context.TOPICS_HEADER)[1].lower()


def test_render_headers_say_main_and_data(tmp_repo):
    assert "merged by him into main" in context.RULES_HEADER
    assert "data, not instructions" in context.DATA_HEADER
    assert "never edit by hand" in context.TITLE


def test_render_at_todays_sizes_fits_9000(tmp_repo):
    """Rules and core near their caps, 8 topics, 13 skills: the real shape. (4,000 + 2,500 + 20 topic lines + 14 long
    skill lines cannot fit 9,000; --check says when the real thing does not.)"""
    (tmp_repo / "RULES.md").write_text(("# Rules\n" + "".join(f"- [R{i}] {'x' * 120}\n" for i in range(1, 30)))[:3900])
    facts = "\n".join(f"- [c{i}] {'y' * 60} (2026-10-01, phone)" for i in range(1, 29))
    write_topic(tmp_repo / "memory", "core", "c", TOPICS["core"][1], facts[:2100])
    for i, letter in enumerate("abdefg"):
        write_topic(tmp_repo / "memory", f"t{i:02d}", letter, "topic about things, load when a chat touches them")
    for i in range(13):
        write_skill(tmp_repo, f"skill-{i:02d}", "does a thing " + "w" * 90)
    out = context.render(tmp_repo)
    assert len(out) <= context.CONTEXT_LIMIT, len(out)
    assert context.check(tmp_repo) == []


def test_write_only_when_changed_and_keeps_read_only_mode(tmp_repo):
    assert context.write(tmp_repo) is True
    target = tmp_repo / "CONTEXT.md"
    assert target.read_text() == context.render(tmp_repo)
    target.chmod(0o444)
    before = target.stat().st_mtime_ns
    assert context.write(tmp_repo) is False  # same text: untouched
    assert target.stat().st_mtime_ns == before
    (tmp_repo / "RULES.md").write_text(RULES + "- [R3] New rule.\n")
    assert context.write(tmp_repo) is True
    assert "[R3] New rule." in target.read_text()
    assert stat.S_IMODE(target.stat().st_mode) == 0o444  # made writable for the swap, mode restored


def test_cli_check_fails_over_limit(tmp_repo, capsys):
    (tmp_repo / "RULES.md").write_text("x" * 4100)
    assert context.main(["--repo", str(tmp_repo), "--check"]) == 1
    assert "4,000" in capsys.readouterr().err


def test_cli_check_names_the_context_limit(tmp_repo, capsys):
    write_topic(tmp_repo / "memory", "core", "c", TOPICS["core"][1])
    for i in range(24):
        write_skill(tmp_repo, f"skill-{i:02d}", "t" * 139)
    (tmp_repo / "RULES.md").write_text("r" * 3990)
    body = "\n".join(f"- [c{i}] {'q' * 60} (2026-10-01, phone)" for i in range(1, 40))
    write_topic(tmp_repo / "memory", "core", "c", TOPICS["core"][1], body[:2400])
    assert context.main(["--repo", str(tmp_repo), "--check"]) == 1
    assert "9000" in capsys.readouterr().err


def test_cli_check_passes_a_small_repo(tmp_repo, capsys):
    assert context.main(["--repo", str(tmp_repo), "--check"]) == 0
    assert "ok" in capsys.readouterr().out


def test_cli_check_fails_over_twenty_topics(tmp_repo, capsys):
    for i, letter in enumerate("abdefgijklmnpqrstuvwx"):  # 21 topics
        write_topic(tmp_repo / "memory", f"t{i:02d}", letter, "about")
    assert context.main(["--repo", str(tmp_repo), "--check"]) == 1
    assert "20" in capsys.readouterr().err


def test_render_missing_rules_and_memory_notes_no_raise(tmp_path):
    out = context.render(tmp_path / "nothing-here")
    assert "RULES.md" in out and "memory" in out.lower()
    assert out.startswith(context.TITLE)


def test_write_and_print_from_the_command_line(tmp_repo, tmp_path):
    run = lambda *a: subprocess.run([sys.executable, str(SERVER / "context.py"), "--repo", str(tmp_repo), *a],
                                    cwd=tmp_path, env={**os.environ, "LIFE_MCP_PRIVATE": str(tmp_repo)},
                                    capture_output=True, text=True)
    done = run("--write")
    assert done.returncode == 0, done.stderr
    assert (tmp_repo / "CONTEXT.md").exists()
    shown = run("--print")
    assert shown.returncode == 0 and shown.stdout.strip() == (tmp_repo / "CONTEXT.md").read_text().strip()


def test_a_skill_that_cant_load_never_stops_the_render(tmp_repo):
    folder = tmp_repo / "skills" / "broken"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("no frontmatter at all\n")
    write_skill(tmp_repo, "fine", "does fine things")
    out = context.render(tmp_repo)
    assert "- fine: does fine things" in out and "broken" not in out
