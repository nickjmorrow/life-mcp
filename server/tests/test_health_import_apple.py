import os

import health_db as hdb
import health_import as hi
from health_fixtures import t, write_metric, write_workout


def daily(db, metric):
    return dict(db.execute("select date, value from daily where metric=? order by date", (metric,)))


def test_decode_frames_skips_bad_chunk():
    from health_fixtures import hae
    frames, bad = hi.decode_frames(hae({"data": [1]}, {"data": [2]}, garbage=True))
    assert [f["data"] for f in frames] == [[1], [2]] and bad == 1


def test_steps_summed_per_local_day(tmp_path):
    base = str(tmp_path / "AutoSync")
    write_metric(base, "step_count", "20260901", [
        {"start": t("2026-09-01T14:00:00"), "qty": 100, "unit": "count"},
        {"start": t("2026-09-01T15:00:00"), "qty": 50, "unit": "count"}], split=2)
    db = hdb.connect(str(tmp_path / "h.db"))
    hi.import_apple(db, base)
    assert daily(db, "step_count") == {"2026-09-01": 150}


def test_local_date_not_utc(tmp_path):
    base = str(tmp_path / "AutoSync")
    # 04:30 UTC on Sep 2 is 11:30pm on Sep 1 in Chicago.
    write_metric(base, "step_count", "20260901", [{"start": t("2026-09-02T04:30:00"), "qty": 7, "unit": "count"}])
    db = hdb.connect(str(tmp_path / "h.db"))
    hi.import_apple(db, base)
    assert daily(db, "step_count") == {"2026-09-01": 7}


def test_heart_rate_uses_avg_and_resting_min_and_weight_last_in_lb(tmp_path):
    base = str(tmp_path / "AutoSync")
    write_metric(base, "heart_rate", "20260901", [
        {"start": t("2026-09-01T14:00:00"), "min": 50, "avg": 60, "max": 70, "unit": "count/min"},
        {"start": t("2026-09-01T15:00:00"), "min": 70, "avg": 80, "max": 90, "unit": "count/min"}])
    write_metric(base, "resting_heart_rate", "20260901", [
        {"start": t("2026-09-01T14:00:00"), "qty": 55, "unit": "count/min"},
        {"start": t("2026-09-01T20:00:00"), "qty": 52, "unit": "count/min"}])
    write_metric(base, "weight_body_mass", "20260901", [
        {"start": t("2026-09-01T13:00:00"), "qty": 80, "unit": "kg"},
        {"start": t("2026-09-01T23:00:00"), "qty": 79, "unit": "kg"}])
    db = hdb.connect(str(tmp_path / "h.db"))
    hi.import_apple(db, base)
    assert daily(db, "heart_rate") == {"2026-09-01": 70}
    assert daily(db, "resting_heart_rate") == {"2026-09-01": 52}
    assert round(daily(db, "weight_body_mass")["2026-09-01"], 1) == 174.2


def test_multi_field_entries_become_separate_metrics(tmp_path):
    base = str(tmp_path / "AutoSync")
    write_metric(base, "blood_pressure", "20260901", [
        {"start": t("2026-09-01T14:00:00"), "systolic": 120, "diastolic": 80, "unit": "mmHg"}])
    db = hdb.connect(str(tmp_path / "h.db"))
    hi.import_apple(db, base)
    assert daily(db, "blood_pressure_systolic") == {"2026-09-01": 120}
    assert daily(db, "blood_pressure_diastolic") == {"2026-09-01": 80}


def test_rerun_replaces_file_samples(tmp_path):
    base = str(tmp_path / "AutoSync")
    path = write_metric(base, "step_count", "20260901", [{"start": t("2026-09-01T14:00:00"), "qty": 10, "unit": "count"}])
    db = hdb.connect(str(tmp_path / "h.db"))
    hi.import_apple(db, base)
    write_metric(base, "step_count", "20260901", [{"start": t("2026-09-01T14:00:00"), "qty": 30, "unit": "count"}])
    os.utime(path, (1, 2_000_000_000))
    hi.import_apple(db, base)
    assert daily(db, "step_count") == {"2026-09-01": 30}
    assert db.execute("select count(*) from partials").fetchone()[0] == 1


def test_unchanged_files_skipped(tmp_path):
    base = str(tmp_path / "AutoSync")
    write_metric(base, "step_count", "20260901", [{"start": t("2026-09-01T14:00:00"), "qty": 10, "unit": "count"}])
    db = hdb.connect(str(tmp_path / "h.db"))
    assert hi.import_apple(db, base) == 1
    assert hi.import_apple(db, base) == 0


def test_dataless_file_skipped(tmp_path, monkeypatch):
    base = str(tmp_path / "AutoSync")
    write_metric(base, "step_count", "20260901", [{"start": t("2026-09-01T14:00:00"), "qty": 10, "unit": "count"}])
    asked = []
    monkeypatch.setattr(hi, "is_dataless", lambda p: True)
    monkeypatch.setattr(hi, "request_download", asked.append)
    db = hdb.connect(str(tmp_path / "h.db"))
    assert hi.import_apple(db, base) == 0
    assert len(asked) == 1 and len(asked[0]) == 1 and daily(db, "step_count") == {}


def test_apple_workout(tmp_path):
    base = str(tmp_path / "AutoSync")
    write_workout(base, "traditional_strength_training_20260901_AAAA-1111.hae", {
        "activity": {"code": "traditional_strength_training"},
        "start": t("2026-09-01T21:00:00"), "end": t("2026-09-01T22:00:00"), "duration": 3600,
        "activeEnergy": 400.4, "heartRateStatistics": {"average": 110.2}})
    db = hdb.connect(str(tmp_path / "h.db"))
    hi.import_apple(db, base)
    row = db.execute("select id, source, date, type, duration_min, kcal, avg_hr from workouts").fetchone()
    assert row == ("AAAA-1111", "apple", "2026-09-01", "traditional_strength_training", 60.0, 400.4, 110.2)


def test_many_dataless_files_one_download_request(tmp_path, monkeypatch):
    base = str(tmp_path / "AutoSync")
    for day in ("20260901", "20260902", "20260903"):
        write_metric(base, "step_count", day, [{"start": t("2026-09-01T14:00:00"), "qty": 1, "unit": "count"}])
    asked = []
    monkeypatch.setattr(hi, "is_dataless", lambda p: True)
    monkeypatch.setattr(hi, "request_download", asked.append)
    hi.import_apple(hdb.connect(str(tmp_path / "h.db")), base)
    assert len(asked) == 1 and len(asked[0]) == 3  # one request naming all three files


def test_conflict_copy_counted_once(tmp_path):
    base = str(tmp_path / "AutoSync")
    e = [{"start": t("2026-09-01T14:00:00"), "qty": 10, "unit": "count"}]
    write_metric(base, "step_count", "20260901", e)
    copy = write_metric(base, "step_count", "20260901 2", e)
    os.utime(copy, (1, 2_000_000_000))  # the copy is newer
    db = hdb.connect(str(tmp_path / "h.db"))
    hi.import_apple(db, base)
    assert daily(db, "step_count") == {"2026-09-01": 10}


def test_newer_conflict_copy_wins_on_rerun(tmp_path):
    base = str(tmp_path / "AutoSync")
    write_metric(base, "step_count", "20260901", [{"start": t("2026-09-01T14:00:00"), "qty": 10, "unit": "count"}])
    db = hdb.connect(str(tmp_path / "h.db"))
    hi.import_apple(db, base)
    copy = write_metric(base, "step_count", "20260901 2", [{"start": t("2026-09-01T14:00:00"), "qty": 25, "unit": "count"}])
    os.utime(copy, (1, 2_000_000_000))
    hi.import_apple(db, base)
    assert daily(db, "step_count") == {"2026-09-01": 25}


def test_bad_file_skipped_and_others_imported(tmp_path, monkeypatch):
    base = str(tmp_path / "AutoSync")
    write_metric(base, "active_energy", "20260901", [{"start": t("2026-09-01T14:00:00"), "qty": 5, "unit": "kcal"}])
    write_metric(base, "step_count", "20260901", [{"start": t("2026-09-01T14:00:00"), "qty": 10, "unit": "count"}])
    real = hi.decode_frames
    monkeypatch.setattr(hi, "decode_frames", lambda raw: (_ for _ in ()).throw(ValueError("boom"))
                        if b"active" in __import__("liblzfse").decompress(raw) else real(raw))
    db = hdb.connect(str(tmp_path / "h.db"))
    hi.import_apple(db, base)
    assert daily(db, "step_count") == {"2026-09-01": 10} and daily(db, "active_energy") == {}


def test_entry_without_time_skipped(tmp_path):
    base = str(tmp_path / "AutoSync")
    write_metric(base, "step_count", "20260901", [{"qty": 3, "unit": "count"},
                                                 {"start": t("2026-09-01T14:00:00"), "qty": 10, "unit": "count"}])
    db = hdb.connect(str(tmp_path / "h.db"))
    hi.import_apple(db, base)
    assert daily(db, "step_count") == {"2026-09-01": 10}


def test_sleep_analysis_dated_by_wake_up(tmp_path):
    base = str(tmp_path / "AutoSync")
    # 22:40 on 9/18 to 06:13 on 9/19, Chicago time
    write_metric(base, "sleep_analysis", "20260919", [{"start": t("2026-09-19T03:40:00"), "end": t("2026-09-19T11:13:00"),
                                                       "totalSleep": 7.2, "unit": "hr"}])
    db = hdb.connect(str(tmp_path / "h.db"))
    hi.import_apple(db, base)
    assert daily(db, "sleep_analysis_total_sleep") == {"2026-09-19": 7.2}


def test_workout_conflict_copy_keeps_one_id(tmp_path):
    base = str(tmp_path / "AutoSync")
    w = {"activity": {"code": "running"}, "start": t("2026-09-01T11:00:00"), "end": t("2026-09-01T11:20:00"), "duration": 1200}
    write_workout(base, "running_20260901_BBBB-2222.hae", w)
    db = hdb.connect(str(tmp_path / "h.db"))
    hi.import_apple(db, base)
    write_workout(base, "running_20260901_BBBB-2222 2.hae", w)
    os.utime(os.path.join(base, "Workouts", "running_20260901_BBBB-2222 2.hae"), (1, 2_000_000_000))
    hi.import_apple(db, base)
    assert db.execute("select id from workouts").fetchall() == [("BBBB-2222",)]


def test_nan_readings_dropped():
    assert list(hi.readings({"qty": float("nan")})) == []
    assert list(hi.readings({"systolic": float("inf"), "diastolic": 80})) == [("_diastolic", 80.0)]


def test_download_timeout_is_not_a_failure(monkeypatch):
    import subprocess

    def slow(*a, **k):
        raise subprocess.TimeoutExpired("brctl", 60)
    monkeypatch.setattr(hi.subprocess, "run", slow)
    hi.request_download(["/x.hae"])  # no exception
