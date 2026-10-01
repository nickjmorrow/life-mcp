import asyncio

import health_db as hdb
import health_import as hi

HEVY = {"id": "h1", "title": "Legs", "start_time": "2026-09-01T21:05:00+00:00", "end_time": "2026-09-01T22:00:00+00:00",
        "exercises": [{"title": "Squat (Barbell)", "sets": [
            {"type": "warmup", "weight_kg": 20, "reps": 10},
            {"type": "normal", "weight_kg": 100, "reps": 5},
            {"type": "normal", "weight_kg": 100, "reps": 5}]}]}


def test_hevy_import_and_volume(tmp_path):
    db = hdb.connect(str(tmp_path / "h.db"))
    pages = {1: {"page_count": 1, "workouts": [HEVY]}}
    assert hi.import_hevy(db, lambda p: pages[p]) == 1
    row = db.execute("select source, date, type, duration_min, volume_kg, sets, detail from workouts").fetchone()
    assert row[:6] == ("hevy", "2026-09-01", "Legs", 55.0, 1000.0, 2)
    assert "Squat (Barbell)" in row[6]


def test_hevy_removes_deleted_workouts(tmp_path):
    db = hdb.connect(str(tmp_path / "h.db"))
    hi.import_hevy(db, lambda p: {"page_count": 1, "workouts": [HEVY]})
    hi.import_hevy(db, lambda p: {"page_count": 1, "workouts": []})
    assert db.execute("select count(*) from workouts").fetchone()[0] == 0


def test_link_apple_workout_to_overlapping_hevy(tmp_path):
    db = hdb.connect(str(tmp_path / "h.db"))
    hi.import_hevy(db, lambda p: {"page_count": 1, "workouts": [HEVY]})
    db.execute("insert into workouts (id, source, date, start, end, type) values"
               " ('a1','apple','2026-09-01','2026-09-01T16:00:00-05:00','2026-09-01T17:05:00-05:00','traditional_strength_training'),"
               " ('a2','apple','2026-09-01','2026-09-01T06:00:00-05:00','2026-09-01T06:20:00-05:00','running')")
    assert hi.link_workouts(db) == 1
    assert dict(db.execute("select id, linked_to from workouts where source='apple'")) == {"a1": "h1", "a2": None}


DAY = {"day": "2026-09-02", "score": 80, "sleepDuration": 27000, "deepDuration": 3600, "remDuration": 5400,
       "lightDuration": 18000, "sleepQualityScore": {"hrv": {"current": 55.6}, "heartRate": 52, "respiratoryRate": {"current": 14.7}},
       "sessions": [{"timeseries": {"tempBedC": [["t", 20.0], ["t", 22.0]], "tempRoomC": [["t", 21.0]]}}]}


def test_eight_sleep_import(tmp_path):
    db = hdb.connect(str(tmp_path / "h.db"))

    async def fetch(start, end):
        return [DAY] if start <= DAY["day"] <= end else []
    assert asyncio.run(hi.import_eight_sleep(db, fetch, today=hi.dt.date(2026, 9, 28))) == 1
    assert db.execute("select * from sleep").fetchone() == (
        "2026-09-02", 80, 450.0, 60.0, 90.0, 300.0, 55.6, 52, 14.7, 21.0, 21.0)


def test_eight_sleep_windows_from_latest_night(tmp_path):
    db = hdb.connect(str(tmp_path / "h.db"))
    db.execute("insert into sleep (date, score) values ('2026-09-20', 70)")
    asked = []

    async def fetch(start, end):
        asked.append((start, end))
        return []
    asyncio.run(hi.import_eight_sleep(db, fetch, today=hi.dt.date(2026, 9, 28)))
    assert asked == [("2026-09-17", "2026-09-28")]


def test_main_keeps_going_when_a_source_fails(tmp_path, monkeypatch):
    db_path = str(tmp_path / "h.db")
    monkeypatch.setattr(hi, "import_apple", lambda db, base=None: (_ for _ in ()).throw(RuntimeError("icloud down")))
    monkeypatch.setattr(hi, "run_hevy", lambda db: 3)
    monkeypatch.setattr(hi, "run_eight_sleep", lambda db: 2)
    assert hi.main(["--db", db_path]) == 1
    rows = dict((s, (n, e)) for s, _, n, e in hdb.connect(db_path).execute("select * from imports"))
    assert rows["hevy"] == (3, None) and rows["eight_sleep"] == (2, None)
    assert "icloud down" in rows["apple"][1]
