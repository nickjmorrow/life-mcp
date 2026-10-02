"""Skills built into their tools' descriptions."""
import skill_tools


def test_body_strips_frontmatter_and_tolerates_missing(tmp_path):
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "SKILL.md").write_text("---\nname: x\ndescription: d\n---\n\n# X\n\nDo it.\n")
    assert skill_tools.body("x", tmp_path) == "# X\n\nDo it."
    assert skill_tools.body("missing", tmp_path) == ""


def test_describe():
    assert skill_tools.describe("Doc.", "x", "") == "Doc."
    out = skill_tools.describe("Doc.", "x", "# X\n\nDo it.")
    assert out.startswith("Doc.\n\nFollow his x skill") and "skill_file with skill 'x'" in out and out.endswith("Do it.")


def test_used_logs_once_per_half_hour(monkeypatch):
    import json, usage_log
    monkeypatch.setattr(skill_tools, "_logged", {})
    clock = iter([10_000.0, 10_000.0, 10_500.0, 12_000.0, 12_000.0])
    monkeypatch.setattr(skill_tools.time, "time", lambda: next(clock))
    skill_tools.used("x"); skill_tools.used("x"); skill_tools.used("x")
    assert [json.loads(l)["skill"] for l in usage_log.PATH.read_text().splitlines()] == ["x", "x"]
