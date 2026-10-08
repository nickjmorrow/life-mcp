"""Which private folder the connector reads: the clean copy of the harness's main whenever the Mac has one, and nothing in
~/.zshrc.local can point it elsewhere. Every path here is a temp folder standing in for the home folder."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parent.parent
CLEAN_REL = Path("Library") / "Application Support" / "personal-agent-harness"


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".zshrc.local").write_text(f"export LIFE_MCP_PRIVATE='{tmp_path / 'elsewhere'}'\n")
    return home


def private_dir(home, env_value=None):
    env = {k: v for k, v in os.environ.items() if k != "LIFE_MCP_PRIVATE"}
    env["HOME"] = str(home)
    if env_value is not None:
        env["LIFE_MCP_PRIVATE"] = env_value
    done = subprocess.run([sys.executable, "-c", "import private; print(private.DIR)"], cwd=SERVER, env=env,
                          capture_output=True, text=True, check=True)
    return Path(done.stdout.strip())


def test_the_default_is_the_clean_copy_when_there_is_one(home):
    (home / CLEAN_REL).mkdir(parents=True)
    assert private_dir(home) == home / CLEAN_REL


def test_the_default_is_the_checkout_without_a_clean_copy(home):
    assert private_dir(home) == home / "Projects" / "personal-agent-harness"


def test_the_variable_still_wins_when_set(home, tmp_path):
    (home / CLEAN_REL).mkdir(parents=True)
    assert private_dir(home, str(tmp_path / "chosen")) == tmp_path / "chosen"


def sourced(home, script="private-env.sh"):
    """LIFE_MCP_PRIVATE after the launcher's own steps: ~/.zshrc.local, then private-env.sh."""
    env = {k: v for k, v in os.environ.items() if k != "LIFE_MCP_PRIVATE"}
    env["HOME"] = str(home)
    done = subprocess.run(["/bin/zsh", "-fc", f'source ~/.zshrc.local; cd {SERVER}; source ./{script}; '
                           'print -r -- "$LIFE_MCP_PRIVATE"'], env=env, capture_output=True, text=True, check=True)
    return done.stdout.rstrip("\n")


def test_private_env_overrides_zshrc_local_with_the_clean_copy(home):
    (home / CLEAN_REL).mkdir(parents=True)
    assert sourced(home) == str(home / CLEAN_REL)


def test_private_env_leaves_things_alone_without_a_clean_copy(home, tmp_path):
    assert sourced(home) == str(tmp_path / "elsewhere")


@pytest.mark.parametrize("launcher", ["run.sh"])
def test_each_launcher_sources_private_env_after_zshrc_local_and_before_it_starts(launcher):
    lines = (SERVER / launcher).read_text().splitlines()
    rc = lines.index("source ~/.zshrc.local")
    env = next(i for i, line in enumerate(lines) if line.startswith("source ./private-env.sh"))
    start = next(i for i, line in enumerate(lines) if line.startswith("exec "))
    assert rc < env < start
    assert not any("LIFE_MCP_PRIVATE" in line for line in lines[env + 1:])
