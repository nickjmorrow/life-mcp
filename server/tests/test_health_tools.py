import datetime as dt

import pytest
from fastmcp.exceptions import ToolError

import health_db as hdb
import health_mcp as hm

TODAY = dt.date(2026, 9, 28)


def d(n):
    return (TODAY - dt.timedelta(days=n)).isoformat()


@pytest.fixture
def db(tmp_path):
    db = hdb.connect(str(tmp_path / "h.db"))
    for n in range(0, 40):
        db.execute("insert into daily values (?,?,?,?)", (d(n), "step_count", 10000 if n >= 7 else 5000, "count"))
        db.execute("insert into sleep (date, score, duration_min) values (?,?,?)",
                   (d(n), 70 if n % 2 else 80, 420))
    for n in range(1, 40, 2):  # a workout every other day, so the next day's sleep is 80
        db.execute("insert into workouts (id, source, date, start, end, type, duration_min, volume_kg, detail)"
                   " values (?,?,?,?,?,?,?,?,?)", (f"w{n}", "hevy", d(n), d(n) + "T16:00:00-05:00",
                   d(n) + "T17:00:00-05:00", "Push", 60, 1000,
                   '[{"title": "Bench Press (Barbell)", "sets": [[80, 5, "normal"], [90, 3, "normal"]]}]'))
    db.execute("insert into imports values ('apple', ?, 5, null)", (d(0) + "T09:00:00-05:00",))
    db.commit()
    return db


@pytest.fixture
def h(db):
    return hm.HealthData(db, today=TODAY)


def test_resolve_friendly_names(h):
    assert h.resolve("Sleep Score") == ("sleep", "score")
    assert h.resolve("steps") == ("daily", "step_count")
    assert h.resolve("step_count") == ("daily", "step_count")
    assert h.resolve("bench press") == ("exercise", "Bench Press (Barbell)")
    with pytest.raises(ToolError, match="health_metrics"):
        h.resolve("zebra count")


def test_summary_compares_with_usual(h):
    out = h.summary("week", end=d(0))
    assert "steps 5,000/day (usual 10,000" in out
    assert "sleep" in out and "training" in out


def test_summary_skips_missing_metrics(h):
    out = h.summary("week")
    assert "weight" not in out and "None" not in out and "nan" not in out


def test_trend_by_week(h):
    out = h.trend("steps", start=d(20), end=d(0), bucket="week")
    assert "average" in out and "5,000" in out and "10,000" in out


def test_exercise_trend_top_set_in_lb(h):
    assert "198" in h.trend("bench press", start=d(10), end=d(0))  # 90 kg top set = 198 lb


def test_compare_sleep_after_workout_days(h):
    out = h.compare("sleep score", "workout", lag_days=1)
    assert "80" in out and "70" in out and "+10" in out


def test_compare_before_after_date(h):
    out = h.compare("steps", f"date:{d(6)}")
    assert "10,000" in out and "5,000" in out


def test_compare_small_groups_warned(h):
    assert "too few" in h.compare("steps", f"date:{d(1)}", start=d(3))


def test_metrics_lists_sources(h):
    out = h.metrics()
    assert "step_count" in out and "sleep score" in out and "Bench Press (Barbell)" in out


def test_query_guard(h):
    assert "10000" in h.query("select max(value) from daily where metric='step_count'")
    for bad in ["select 1; drop table daily", "PRAGMA table_info(daily)", "attach 'x' as y", "delete from daily"]:
        with pytest.raises(ToolError):
            h.query(bad)


def test_stale_data_noted(db):
    h = hm.HealthData(db, today=TODAY + dt.timedelta(days=5))
    assert "days old" in h.summary("week")


def test_build_registers_tools(tmp_path):
    hdb.connect(str(tmp_path / "h.db")).close()
    import asyncio
    names = {t.name for t in asyncio.run(hm.build(str(tmp_path / "h.db")).list_tools())}
    assert names == {"health_summary", "health_trend", "health_compare", "health_metrics", "health_query"}


def test_week_without_workouts_shows_zero(tmp_path):
    db = hdb.connect(str(tmp_path / "h.db"))
    for n in range(8, 30, 2):
        db.execute("insert into workouts (id, source, date, start, end, type, duration_min) values (?,?,?,?,?,?,?)",
                   (f"w{n}", "hevy", d(n), d(n) + "T16:00:00-05:00", d(n) + "T17:00:00-05:00", "Push", 60))
    db.commit()
    out = hm.HealthData(db, today=TODAY).summary("week")
    assert "- workouts 0 (usual" in out


def test_summary_leaves_out_todays_partial_day(tmp_path):
    db = hdb.connect(str(tmp_path / "h.db"))
    for n in range(1, 40):
        db.execute("insert into daily values (?,?,?,?)", (d(n), "step_count", 8000, "count"))
    db.execute("insert into daily values (?,?,?,?)", (d(0), "step_count", 300, "count"))
    db.commit()
    assert "steps 8,000/day" in hm.HealthData(db, today=TODAY).summary("week")


def test_weekly_trend_sums_workouts_and_day_bucket_respected(h):
    out = h.trend("workouts", start=d(27), end=d(0), bucket="week")
    assert "max 4" in out or "max 3" in out
    long = h.trend("steps", start=d(39), end=d(0), bucket="day")
    assert f"{d(39)}:" in long


def test_period_in_weeks_and_months(h):
    assert h.summary("2 weeks", end=d(0)).startswith(f"last 14 days {d(13)}..{d(0)}")
    assert h.summary("3 months", end=d(0)).startswith(f"last 90 days")
    with pytest.raises(ToolError, match="period"):
        h.summary("0 days")


def test_summary_diff_has_units(h):
    out = h.summary("week", end=d(0))
    assert "-5,000/day)" in out


def test_query_allows_harmless_words_but_not_attach(h):
    assert "x" in h.query("select replace('y', 'y', 'x') as v")
    assert h.query("select count(*) from daily where metric like '%update%' or 1").endswith("40")
    for bad in ["attach 'x.db' as y", "select * from pragma_table_info('daily')"]:
        with pytest.raises(ToolError):
            h.query(bad)


def test_runaway_query_stopped(h, monkeypatch):
    monkeypatch.setattr(hm, "QUERY_SECONDS", 0.2)
    with pytest.raises(ToolError, match="too long"):
        h.query("with recursive r(n) as (select 1 union all select n + 1 from r) select count(*) from r")


def test_missing_database_says_so(tmp_path):
    import asyncio
    m = hm.build(str(tmp_path / "none.db"))
    with pytest.raises(Exception, match="No health data"):
        asyncio.run(m.call_tool("health_summary", {}))


def test_no_change_reads_same(tmp_path):
    db = hdb.connect(str(tmp_path / "h.db"))
    for n in range(0, 35):
        db.execute("insert into sleep (date, score) values (?,?)", (d(n), 80))
    db.commit()
    assert "sleep score 80 (usual 80, same)" in hm.HealthData(db, today=TODAY).summary("week")
