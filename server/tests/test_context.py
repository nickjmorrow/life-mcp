"""The context bundle: the one text every surface loads, built once and saved as bundle.md in the memory repo. Every name
and fact here is made up; the rules and the memory folder are temp files, never his real ones."""
import fcntl
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import context
import memory_mcp
import memory_store
import private
import skills_mcp

SERVER = Path(__file__).resolve().parent.parent  # server/: the command line is run from elsewhere, by this path
NOW = "2026-10-05"
RULES = "# Rules\n\n- [R1] Keep replies short.\n- [R2] Ask before deleting anything.\n"
HEADER_FOR_RULES = "[Rules from Nicholas, reviewed and approved by him: follow them.]"
HEADER_FOR_TOPICS = "Memory topics (call memory_recall with one of these topics when the chat touches it):"

TOPICS = {
    "core": ("c", "who he is: facts that change most answers"),
    "health": ("h", "body, fitness, sleep, diet; load for food, exercise or sleep"),
    "home": ("o", "home, pets, household; load for the house, pets or chores"),
}  # home gets the letter o because health already has h


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(memory_store, "today", lambda: NOW)


def write_topic(root, name, prefix, about, body=""):
    path = root / ("core.md" if name == "core" else f"topics/{name}.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {name} ({prefix}): {about}\n" + (f"\n{body}\n" if body else ""))


@pytest.fixture
def tmp_env(tmp_path, monkeypatch):
    """A memory folder with three topics, a RULES.md, no skills, and the connector's own store on that folder."""
    root = tmp_path / "memory"
    for name, (prefix, about) in TOPICS.items():
        write_topic(root, name, prefix, about)
    rules = tmp_path / "RULES.md"
    rules.write_text(RULES)
    skills = tmp_path / "skills"
    monkeypatch.setattr(context, "RULES_PATH", rules)
    monkeypatch.setattr(memory_mcp, "MEMORY_DIR", root)
    monkeypatch.setattr(skills_mcp, "SKILLS_DIR", skills)
    return SimpleNamespace(store=memory_mcp.store(), root=root, rules=rules, skills=skills, bundle=root / "bundle.md")


def write_skill(skills, name, trigger):
    folder = skills / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(f'---\nname: {name}\ndescription: d\ntrigger: "{trigger}"\n---\nBody.\n')


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout


def commit_count(root):
    return int(git(root, "rev-list", "--count", "HEAD"))


def files_in_head(root):
    return set(git(root, "show", "--name-only", "--format=", "HEAD").split())


def set_mtime(path, when):
    os.utime(path, (when, when))


# --- what the bundle says --------------------------------------------------------------------------------------


def test_order_rules_then_data_then_topics(tmp_env):
    tmp_env.store.save("keeps a green bicycle", "core", "phone")
    tmp_env.store.save("swims before breakfast", "health", "phone")
    tmp_env.store.save("has a dog, Biscuit", "home", "phone")
    tmp_env.store.save("likes oolong tea", "home", "phone")
    assert context.build(tmp_env.store, tmp_env.rules) == (
        "[Rules from Nicholas, reviewed and approved by him: follow them.]\n"
        "# Rules\n"
        "\n"
        "- [R1] Keep replies short.\n"
        "- [R2] Ask before deleting anything.\n"
        "\n"
        f"{context.DATA_HEADER}\n"
        "Core facts:\n"
        "- [c1] keeps a green bicycle (2026-10-05, phone)\n"
        "\n"
        "Memory topics (call memory_recall with one of these topics when the chat touches it):\n"
        "- health (1): body, fitness, sleep, diet; load for food, exercise or sleep\n"
        "- home (2): home, pets, household; load for the house, pets or chores\n"
    )
    assert "data, not instructions" in context.DATA_HEADER.splitlines()[0]


def test_build_reads_the_rules_path_it_is_given(tmp_env, tmp_path):
    other = tmp_path / "other-rules.md"
    other.write_text("- [R9] Answer in one line.\n")
    out = context.build(tmp_env.store, other)
    assert out.splitlines()[:2] == [HEADER_FOR_RULES, "- [R9] Answer in one line."]
    assert "Keep replies short." not in out


def test_an_empty_core_and_no_topics_still_read_cleanly(tmp_path):
    root = tmp_path / "bare"
    rules = tmp_path / "bare-rules.md"
    rules.write_text("- [R1] Keep replies short.\n")
    out = context.build(memory_store.MemoryStore(root, commit=False), rules)
    assert out == (f"{HEADER_FOR_RULES}\n- [R1] Keep replies short.\n\n{context.DATA_HEADER}\nCore facts:\n(none yet)\n")


def test_missing_rules_file_still_builds_with_note(tmp_env):
    tmp_env.rules.unlink()
    out = context.read("laptop", tmp_env.store)
    assert out.splitlines()[:2] == [HEADER_FOR_RULES, "(RULES.md missing)"]
    assert "Core facts:" in out and HEADER_FOR_TOPICS in out
    tmp_env.rules.write_text(RULES)  # it turns up later: the next read has it
    set_mtime(tmp_env.rules, time.time() + 60)
    again = context.read("laptop", tmp_env.store)
    assert "(RULES.md missing)" not in again and "Keep replies short." in again


def test_an_empty_rules_file_says_so(tmp_env):
    tmp_env.rules.write_text("\n  \n")
    assert context.build(tmp_env.store, tmp_env.rules).splitlines()[:2] == [HEADER_FOR_RULES, "(RULES.md is empty)"]


def test_archive_not_listed_in_topics(tmp_env):
    tmp_env.store.save("rides a green bicycle to work", "home", "phone")
    tmp_env.store.save("has a dog, Biscuit", "core", "phone")
    tmp_env.store.archive("o1", "sold the bicycle")
    out = context.read("laptop", tmp_env.store)
    topic_lines = [line for line in out.splitlines() if re.match(r"- [a-z-]+ \(\d+\): ", line)]
    assert topic_lines == ["- health (0): body, fitness, sleep, diet; load for food, exercise or sleep",
                           "- home (0): home, pets, household; load for the house, pets or chores"]
    assert "green bicycle" not in out and "archive" not in out.lower()  # archived facts are never loaded
    assert "Biscuit" in out  # core is in full, not as a topic line


def test_full_budgets_fit_laptop_limit(tmp_env):
    tmp_env.rules.write_text("r" * context.RULES_LIMIT)
    core = []
    while len("\n".join(core + [f"- [c{len(core) + 1}] {'x' * 90} (2026-10-05, phone)"])) <= memory_store.CORE_LIMIT:
        core.append(f"- [c{len(core) + 1}] {'x' * 90} (2026-10-05, phone)")
    write_topic(tmp_env.root, "core", "c", "who he is: facts that change most answers", "\n".join(core))
    assert tmp_env.store.size("core") <= memory_store.CORE_LIMIT  # a core the store itself would hold
    assert tmp_env.store.size("core") > memory_store.CORE_LIMIT - 120  # and it's full
    names = ["health", "mind", "home", "taste", "work", "money", "projects", "devices", "travel", "garden",
             "kitchen", "books"]
    for letter, name in zip("abdefghijklm", names):
        write_topic(tmp_env.root, name, letter, f"{name} things, habits and plans; load for questions about {name}"
                                                f" or anything close to it, like its tools or people")
    bundle = context.build(tmp_env.store, tmp_env.rules)
    assert len([line for line in bundle.splitlines() if re.match(r"- [a-z]+ \(\d+\): ", line)]) == 12
    assert "r" * context.RULES_LIMIT in bundle
    assert len(bundle) < context.LAPTOP_LIMIT


def test_an_over_budget_bundle_is_built_whole_but_flagged(tmp_env, capsys):
    tmp_env.rules.write_text("r" * (context.LAPTOP_LIMIT + 100))  # over its own budget and the laptop's
    out = context.build(tmp_env.store, tmp_env.rules)
    assert "r" * (context.LAPTOP_LIMIT + 100) in out  # never cut: a rule that quietly vanished would be worse
    err = capsys.readouterr().err
    assert "RULES.md" in err and "4,000" in err and "laptop" in err and "9,000" in err


def test_a_bundle_within_budget_says_nothing(tmp_env, capsys):
    context.build(tmp_env.store, tmp_env.rules)
    assert capsys.readouterr().err == ""


# --- what each surface gets ------------------------------------------------------------------------------------


def test_phone_is_bundle_file_plus_skill_index(tmp_env):
    write_skill(tmp_env.skills, "research", "/research")
    phone, laptop = context.read("phone", tmp_env.store), context.read("laptop", tmp_env.store)
    assert laptop == tmp_env.bundle.read_text()
    assert phone.startswith(laptop) and phone[len(laptop):].strip().startswith("Nicholas's own skills")
    assert phone.endswith(skills_mcp.instructions(skills_mcp.load_all(tmp_env.skills)))
    assert "research (/research)" in phone and "Nicholas's own skills" not in laptop
    write_skill(tmp_env.skills, "buddy", "/buddy")  # read fresh: no restart
    assert "buddy (/buddy)" in context.read("phone", tmp_env.store)


def test_no_skills_means_the_phone_gets_just_the_bundle(tmp_env):
    assert context.read("phone", tmp_env.store) == context.read("laptop", tmp_env.store)


def test_hob_and_laptop_identical(tmp_env):
    tmp_env.store.save("has a dog, Biscuit", "home", "phone")
    laptop, hob = context.read("laptop", tmp_env.store), context.read("hob", tmp_env.store)
    assert hob == laptop == tmp_env.bundle.read_text()


@pytest.mark.parametrize("trouble", ["not importable", "load_all raises"])
def test_a_skills_problem_leaves_the_bundle_without_the_index(tmp_env, monkeypatch, capsys, trouble):
    write_skill(tmp_env.skills, "research", "/research")
    if trouble == "not importable":
        monkeypatch.setitem(sys.modules, "skills_mcp", None)  # `import skills_mcp` now fails
    else:
        monkeypatch.setattr(skills_mcp, "load_all", lambda root: 1 / 0)
    assert context.read("phone", tmp_env.store) == context.read("laptop", tmp_env.store)
    assert "skill index" in capsys.readouterr().err


def test_memory_and_context_load_even_when_skills_cannot_be_imported():
    code = "import sys; sys.modules['skills_mcp'] = None; import context, memory_mcp; memory_mcp.build(); print('ok')"
    done = subprocess.run([sys.executable, "-c", code], cwd=SERVER, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0 and done.stdout.strip() == "ok", done.stderr


def test_a_surface_that_isnt_one_is_refused(tmp_env):
    for surface in ("tablet", "Phone", "", None):
        with pytest.raises(ValueError, match="phone, laptop, hob"):
            context.read(surface, tmp_env.store)


def test_read_uses_the_connectors_memory_when_given_no_store(tmp_env):
    tmp_env.store.save("has a dog, Biscuit", "home", "phone")
    assert context.read("laptop") == context.read("laptop", tmp_env.store)
    assert "- home (1): " in context.read("laptop")


def test_the_tests_never_read_the_real_rules_file():
    assert context.RULES_PATH == private.DIR / "RULES.md" and not context.RULES_PATH.exists()


# --- keeping it current ----------------------------------------------------------------------------------------


def test_save_rewrites_bundle_in_same_commit(tmp_env):
    tmp_env.store.save("has a dog, Biscuit", "home", "phone")  # the first commit takes in every file in the folder
    assert "- home (1): " in tmp_env.bundle.read_text() and "bundle.md" in files_in_head(tmp_env.root)
    tmp_env.store.save("likes oolong tea", "home", "phone")
    assert "- home (2): " in tmp_env.bundle.read_text()
    assert files_in_head(tmp_env.root) == {"topics/home.md", "bundle.md", ".ids.json"}  # one commit holds all three
    assert commit_count(tmp_env.root) == 2 and git(tmp_env.root, "status", "--porcelain") == ""
    tmp_env.store.update("o1", remove=True)  # every kind of write keeps it current
    assert "- home (1): " in tmp_env.bundle.read_text() and "bundle.md" in files_in_head(tmp_env.root)


def test_first_read_builds_and_commits_the_bundle(tmp_env):
    assert not tmp_env.bundle.exists()
    out = context.read("laptop", tmp_env.store)
    assert tmp_env.bundle.read_text() == out and "Keep replies short." in out
    assert commit_count(tmp_env.root) == 1 and "bundle.md" in files_in_head(tmp_env.root)
    assert git(tmp_env.root, "status", "--porcelain") == ""


def test_a_fresh_install_gets_a_folder_a_repo_and_a_bundle(tmp_path, tmp_env):
    root = tmp_path / "fresh" / "memory"  # nothing there yet: not even the folder
    out = context.read("laptop", memory_store.MemoryStore(root))
    assert out.splitlines()[0] == HEADER_FOR_RULES and "Core facts:\n(none yet)\n" in out
    assert (root / "bundle.md").read_text() == out
    assert commit_count(root) == 1 and files_in_head(root) == {"bundle.md"}


def test_a_fresh_bundle_is_read_as_is(tmp_env, monkeypatch):
    set_mtime(tmp_env.rules, time.time() - 3600)  # RULES.md is older than the bundle's commit
    tmp_env.store.save("has a dog, Biscuit", "home", "phone")  # commits the bundle with it
    before = commit_count(tmp_env.root)
    monkeypatch.setattr(context, "build", lambda *args: pytest.fail("rebuilt a bundle that was fresh"))
    assert context.read("laptop", tmp_env.store) == tmp_env.bundle.read_text()
    assert context.read("phone", tmp_env.store) == tmp_env.bundle.read_text()
    assert commit_count(tmp_env.root) == before


def test_rules_change_makes_read_rebuild(tmp_env):
    set_mtime(tmp_env.rules, time.time() - 3600)
    tmp_env.store.save("has a dog, Biscuit", "home", "phone")
    before = commit_count(tmp_env.root)
    assert "Keep replies short." in context.read("laptop", tmp_env.store)
    assert commit_count(tmp_env.root) == before  # nothing changed, nothing rebuilt
    tmp_env.rules.write_text("# Rules\n\n- [R1] Answer in one line.\n")
    set_mtime(tmp_env.rules, time.time() + 60)  # newer than the bundle's last commit
    out = context.read("laptop", tmp_env.store)
    assert "Answer in one line." in out and "Keep replies short." not in out
    assert tmp_env.bundle.read_text() == out
    assert commit_count(tmp_env.root) == before + 1 and files_in_head(tmp_env.root) == {"bundle.md"}
    assert context.read("laptop", tmp_env.store) == out and commit_count(tmp_env.root) == before + 1


def test_a_commit_without_the_bundle_makes_read_rebuild(tmp_env):
    set_mtime(tmp_env.rules, time.time() - 3600)  # so only the newer commit can make it stale
    tmp_env.store.save("has a dog, Biscuit", "home", "phone")
    home = tmp_env.root / "topics" / "home.md"
    home.write_text(home.read_text() + f"- [o2] plays chess ({NOW}, phone)\n")  # an edit by hand, committed by hand
    git(tmp_env.root, "add", "-A")
    git(tmp_env.root, "-c", "user.name=Tester", "-c", "user.email=tester@example.com", "-c", "commit.gpgsign=false",
        "commit", "-q", "--no-verify", "-m", "by hand")
    assert "- home (1): " in tmp_env.bundle.read_text()  # the bundle hasn't heard
    before = commit_count(tmp_env.root)
    assert "- home (2): " in context.read("laptop", tmp_env.store)
    assert commit_count(tmp_env.root) == before + 1 and files_in_head(tmp_env.root) == {"bundle.md"}


def test_a_bundle_nobody_committed_is_rebuilt(tmp_env):
    root = tmp_env.root
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "core.md", "topics")
    git(root, "-c", "user.name=Tester", "-c", "user.email=tester@example.com", "-c", "commit.gpgsign=false",
        "commit", "-q", "--no-verify", "-m", "start")  # the repo has history, but none for bundle.md
    tmp_env.bundle.write_text("left over from somewhere\n")
    set_mtime(tmp_env.rules, time.time() - 3600)
    out = context.read("laptop", tmp_env.store)
    assert "left over" not in out and "Keep replies short." in out
    assert tmp_env.bundle.read_text() == out and files_in_head(root) == {"bundle.md"}


def test_a_vanished_rules_file_does_not_replace_a_good_bundle(tmp_env):
    set_mtime(tmp_env.rules, time.time() - 3600)
    tmp_env.store.save("has a dog, Biscuit", "home", "phone")
    tmp_env.rules.unlink()  # say, a checkout swapping the file for a moment
    before = commit_count(tmp_env.root)
    out = context.read("laptop", tmp_env.store)
    assert "Keep replies short." in out and "(RULES.md missing)" not in out
    assert commit_count(tmp_env.root) == before


def test_a_bundle_that_cannot_be_saved_is_still_served(tmp_env, capsys):
    set_mtime(tmp_env.rules, time.time() - 3600)
    tmp_env.store.save("has a dog, Biscuit", "home", "phone")
    old = tmp_env.bundle.read_text()
    tmp_env.rules.write_text("# Rules\n\n- [R1] Answer in one line.\n")
    set_mtime(tmp_env.rules, time.time() + 60)
    lock = tmp_env.root / ".git" / "index.lock"
    lock.write_text("")  # a stuck git
    out = context.read("laptop", tmp_env.store)
    assert "Answer in one line." in out  # the text is right even though it couldn't be committed
    assert tmp_env.bundle.read_text() == old  # and the file went back as it was
    assert "Context" in capsys.readouterr().err
    lock.unlink()
    assert context.read("laptop", tmp_env.store) == out and tmp_env.bundle.read_text() == out


def test_a_write_builds_the_bundle_under_the_stores_lock(tmp_env, monkeypatch):
    held = []
    real_build = context.build

    def build(store, rules_path):
        fd = os.open(tmp_env.root / ".lock", os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held.append(False)
        except BlockingIOError:
            held.append(True)
        finally:
            os.close(fd)
        return real_build(store, rules_path)

    monkeypatch.setattr(context, "build", build)
    assert context.write(tmp_env.store) is True
    assert held == [True]
    assert context.write(tmp_env.store) is False  # nothing changed: no second commit
    assert commit_count(tmp_env.root) == 1


# --- the command line ------------------------------------------------------------------------------------------


def test_cli_write_and_print(tmp_env, capsys):
    write_skill(tmp_env.skills, "research", "/research")
    assert context.main(["--write"]) == 0
    assert capsys.readouterr().out.strip() == "bundle.md rebuilt and committed"
    assert commit_count(tmp_env.root) == 1
    assert context.main(["--write"]) == 0
    assert capsys.readouterr().out.strip() == "bundle.md is up to date"
    assert commit_count(tmp_env.root) == 1
    bundle = tmp_env.bundle.read_text()
    for surface in ("laptop", "hob"):
        assert context.main(["--print", surface]) == 0
        assert capsys.readouterr().out == bundle  # byte for byte, the file
    assert context.main(["--print", "phone"]) == 0
    printed = capsys.readouterr().out
    assert printed.startswith(bundle) and printed.endswith("\n")
    assert "research (/research)" in printed


@pytest.mark.parametrize("args", [[], ["--print", "tablet"], ["--write", "--print", "phone"]])
def test_cli_wants_exactly_one_thing(tmp_env, capsys, args):
    with pytest.raises(SystemExit) as stopped:
        context.main(args)
    assert stopped.value.code == 2
    assert "usage" in capsys.readouterr().err


def test_cli_write_that_fails_says_so_and_exits_1(tmp_env, capsys):
    tmp_env.store.save("has a dog, Biscuit", "home", "phone")
    (tmp_env.root / ".git" / "index.lock").write_text("")  # a stuck git
    tmp_env.rules.write_text("# Rules\n\n- [R1] Answer in one line.\n")
    assert context.main(["--write"]) == 1
    assert "Context" in capsys.readouterr().err


def test_cli_runs_from_any_folder_and_honors_the_private_folder(tmp_path):
    private_dir = tmp_path / "private"
    private_dir.mkdir()
    memory = tmp_path / "elsewhere" / "memory"
    (private_dir / "config.toml").write_text(f'memory_dir = "{memory}"\n')
    (private_dir / "RULES.md").write_text("- [R1] Keep replies short.\n")
    write_skill(private_dir / "skills", "research", "/research")
    memory.mkdir(parents=True)
    write_topic(memory, "core", "c", "facts", "- [c1] keeps a green bicycle (2026-10-05, phone)")
    env = {**os.environ, "LIFE_MCP_PRIVATE": str(private_dir), "HOME": str(tmp_path / "home")}

    def run(*args):
        return subprocess.run([sys.executable, str(SERVER / "context.py"), *args], cwd=tmp_path, env=env,
                              capture_output=True, text=True, timeout=120)

    done = run("--write")
    assert done.returncode == 0, done.stderr
    saved = (memory / "bundle.md").read_text()
    assert "- [R1] Keep replies short." in saved and "keeps a green bicycle" in saved
    assert commit_count(memory) == 1 and files_in_head(memory) == {"core.md", "bundle.md"}
    phone = run("--print", "phone")
    assert phone.returncode == 0, phone.stderr
    assert phone.stdout.startswith(saved) and "research (/research)" in phone.stdout
    assert run("--print", "tablet").returncode == 2


# --- a stale flag that a rebuild can't clear, and a RULES.md that can't be read -----------------------------------


def reword_by_hand(root):
    """Reword a topic entry and commit it by hand: the bundle's text (counts and about lines) stays the same, so a
    rebuild has nothing to commit and the bundle's last commit stays behind HEAD."""
    home = root / "topics" / "home.md"
    home.write_text(home.read_text().replace("has a dog, Biscuit", "has a dog called Biscuit"))
    git(root, "add", "-A")
    git(root, "-c", "user.name=Tester", "-c", "user.email=tester@example.com", "-c", "commit.gpgsign=false",
        "commit", "-q", "--no-verify", "-m", "by hand")


def test_a_stale_flag_whose_rebuild_changes_nothing_never_takes_the_lock(tmp_env, monkeypatch):
    set_mtime(tmp_env.rules, time.time() - 3600)
    tmp_env.store.save("has a dog, Biscuit", "home", "phone")
    reword_by_hand(tmp_env.root)
    assert context._stale(tmp_env.store, tmp_env.rules)  # HEAD is past the bundle's last commit, for good
    good = tmp_env.bundle.read_text()
    monkeypatch.setattr(context, "write", lambda store: pytest.fail("took the store's write lock for a no-op"))
    for _ in range(3):  # every recall, not just the first
        assert context.read("laptop", tmp_env.store) == good
        assert context.read("phone", tmp_env.store) == good


def test_a_stale_bundle_that_differs_is_still_rebuilt_under_the_lock(tmp_env):
    set_mtime(tmp_env.rules, time.time() - 3600)
    tmp_env.store.save("has a dog, Biscuit", "home", "phone")
    reword_by_hand(tmp_env.root)
    tmp_env.rules.write_text("# Rules\n\n- [R1] Answer in one line.\n")
    set_mtime(tmp_env.rules, time.time() + 60)
    before = commit_count(tmp_env.root)
    out = context.read("laptop", tmp_env.store)
    assert "Answer in one line." in out and tmp_env.bundle.read_text() == out
    assert commit_count(tmp_env.root) == before + 1


@pytest.mark.parametrize("damage", ["unreadable", "not utf-8"])
def test_a_rules_file_that_cannot_be_read_serves_the_bundle_on_disk(tmp_env, capsys, damage):
    set_mtime(tmp_env.rules, time.time() - 3600)
    tmp_env.store.save("has a dog, Biscuit", "home", "phone")
    good = tmp_env.bundle.read_text()
    if damage == "unreadable":
        tmp_env.rules.chmod(0)
    else:
        tmp_env.rules.write_bytes(b"# Rules\n\n- [R1] \xff\xfe broken\n")
    set_mtime(tmp_env.rules, time.time() + 60)  # newer than the bundle: stale
    before = commit_count(tmp_env.root)
    try:
        assert context.read("laptop", tmp_env.store) == good
        assert context.read("phone", tmp_env.store) == good
    finally:
        tmp_env.rules.chmod(0o600)
    err = capsys.readouterr().err
    assert "Context" in err and "RULES.md" in err
    assert commit_count(tmp_env.root) == before and tmp_env.bundle.read_text() == good


def test_a_rules_file_that_cannot_be_read_and_no_bundle_is_an_error(tmp_env):
    tmp_env.rules.chmod(0)
    try:
        with pytest.raises(PermissionError):
            context.read("laptop", tmp_env.store)
    finally:
        tmp_env.rules.chmod(0o600)
