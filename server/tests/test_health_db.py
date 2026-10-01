import health_db as hdb


def test_schema_created(tmp_path):
    db = hdb.connect(str(tmp_path / "h.db"))
    names = {r[0] for r in db.execute("select name from sqlite_master where type='table'")}
    assert {"partials", "daily", "workouts", "sleep", "imports", "files"} <= names


def test_readonly_refuses_writes(tmp_path):
    p = str(tmp_path / "h.db")
    hdb.connect(p).close()
    ro = hdb.connect_readonly(p)
    try:
        ro.execute("insert into imports values ('x', 'y', 1, null)")
        assert False, "write allowed"
    except Exception as e:
        assert "readonly" in str(e).lower()


def test_rules():
    assert hdb.rule("step_count", "count") == hdb.SUM
    assert hdb.rule("resting_heart_rate", "count/min") == hdb.MIN
    assert hdb.rule("weight_body_mass", "lb") == hdb.LAST
    assert hdb.rule("apple_stand_hour", "count") == hdb.COUNT
    assert hdb.rule("heart_rate", "count/min") == hdb.AVG
    assert hdb.rule("some_new_metric", "kcal") == hdb.SUM
    assert hdb.rule("some_new_metric", "%") == hdb.AVG


def test_rollup_combines_partials():
    # (n, total, lo, hi, last_ts, last_value) per file; the day's readings are 2, 4 (file 1) and 9 (file 2)
    parts = [(2, 6.0, 2.0, 4.0, "2026-09-01T09:00", 4.0), (1, 9.0, 9.0, 9.0, "2026-09-01T07:00", 9.0)]
    assert hdb.rollup(parts, hdb.SUM) == 15.0
    assert hdb.rollup(parts, hdb.AVG) == 5.0
    assert hdb.rollup(parts, hdb.LAST) == 4.0
    assert hdb.rollup(parts, hdb.MIN) == 2.0
    assert hdb.rollup(parts, hdb.COUNT) == 3


def test_partial_of_readings():
    assert hdb.partial([("2026-09-01T09:00", 4.0), ("2026-09-01T08:00", 2.0)]) == (2, 6.0, 2.0, 4.0, "2026-09-01T09:00", 4.0)
