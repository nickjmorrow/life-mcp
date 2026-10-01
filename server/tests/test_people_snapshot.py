import os
import sqlite3
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def helper(tmp_path_factory):
    out = str(tmp_path_factory.mktemp("bin") / "people-snapshot")
    subprocess.run(["swiftc", "-O", "-o", out, os.path.join(ROOT, "people", "people-snapshot.swift")], check=True)
    return out


def test_copies_a_readable_database(helper, tmp_path):
    src = str(tmp_path / "src.db")
    db = sqlite3.connect(src)
    db.execute("create table t(x)")
    db.execute("insert into t values (42)")
    db.commit()
    dest = str(tmp_path / "dest")
    r = subprocess.run([helper, "--dest", dest, "--src", src, "copy.db"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert sqlite3.connect(os.path.join(dest, "copy.db")).execute("select x from t").fetchone() == (42,)
    assert oct(os.stat(dest).st_mode & 0o777) == "0o700"
    assert oct(os.stat(os.path.join(dest, "copy.db")).st_mode & 0o777) == "0o600"


def test_unreadable_source_keeps_old_copy(helper, tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "copy.db").write_bytes(b"old")
    r = subprocess.run([helper, "--dest", str(dest), "--src", str(tmp_path / "missing.db"), "copy.db"],
                       capture_output=True, text=True)
    assert r.returncode == 1 and "missing.db" in r.stderr
    assert (dest / "copy.db").read_bytes() == b"old"
