"""Import Nicholas's health data into the health database (health_db.DB_PATH).

Sources, each independent (one failing doesn't stop the others):
- Apple Health: Health Auto Export's iCloud sync folder. Each .hae file is concatenated
  LZFSE chunks of JSON; timestamps are seconds since 2001-01-01 UTC.
- Hevy: its REST API (HEVY_API_KEY).
- Eight Sleep: /users/{uid}/trends through eightsleep_api.

Safe to rerun: Apple files are only re-read when they change, and a changed file's
partials replace the old ones. Run hourly by launchd (com.nicholai.health-import).
"""
import datetime as dt
import glob
import json
import math
import os
import re
import subprocess
import sys
from zoneinfo import ZoneInfo

import health_db as hdb
import host

TZ = ZoneInfo(host.TIMEZONE)
APPLE_EPOCH = dt.datetime(2001, 1, 1, tzinfo=dt.timezone.utc)
APPLE_BASE = os.path.expanduser(
    "~/Library/Mobile Documents/iCloud~com~ifunography~HealthExport/Documents/AutoSync")
SF_DATALESS = 0x40000000  # an iCloud placeholder whose contents aren't downloaded
KG_METRICS = {"weight_body_mass", "lean_body_mass"}
SKIP_FIELDS = {"start", "end", "date"}
WAKE_DATED = {"sleep_analysis"}  # a night belongs to the morning he wakes up, like Eight Sleep
COPY_SUFFIX = re.compile(r" \d+$")  # iCloud conflict copies: "20260628 2.hae"


def log(msg: str) -> None:
    print(f"{dt.datetime.now(TZ):%Y-%m-%d %H:%M:%S} {msg}", flush=True)


# ── Apple Health ─────────────────────────────────────────────────────────


def decode_frames(raw: bytes) -> tuple[list[dict], int]:
    """Every JSON chunk in a .hae file, and how many chunks failed to decode."""
    import liblzfse
    frames, bad = [], 0
    for m in re.finditer(rb"bvx[124n\-]", raw):
        try:
            frames.append(json.loads(liblzfse.decompress(raw[m.start():])))
        except Exception:
            bad += 1
    return frames, bad


def local_dt(x) -> dt.datetime:
    if isinstance(x, (int, float)):
        return (APPLE_EPOCH + dt.timedelta(seconds=x)).astimezone(TZ)
    return dt.datetime.fromisoformat(str(x)).astimezone(TZ)


def snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def readings(entry: dict):
    """(metric suffix, value) pairs: qty or avg if present, else every numeric field."""
    for key in ("qty", "avg"):
        if isinstance(entry.get(key), (int, float)):
            if math.isfinite(entry[key]):
                yield "", float(entry[key])
            return
    for k, v in entry.items():
        if k not in SKIP_FIELDS and isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
            yield "_" + snake(k), float(v)


def is_dataless(path: str) -> bool:
    return bool(getattr(os.stat(path), "st_flags", 0) & SF_DATALESS)


def request_download(paths: list[str]) -> None:
    """Ask iCloud to download these files. brctl returns at once (~10 ms) and the files
    arrive within seconds; asking for a folder does nothing, so name every file."""
    for i in range(0, len(paths), 500):
        try:
            subprocess.run(["brctl", "download", *paths[i:i + 500]], capture_output=True, timeout=60)
        except subprocess.TimeoutExpired:
            log("apple: iCloud download request timed out; the next run asks again")


def _changed(db, path: str) -> bool:
    row = db.execute("select mtime from files where path=?", (path,)).fetchone()
    return row is None or row[0] != os.path.getmtime(path)


def _file_id(db, path: str) -> int:
    db.execute("insert or ignore into files (path, mtime) values (?, null)", (path,))
    return db.execute("select id from files where path=?", (path,)).fetchone()[0]


def _read(path: str) -> bytes | None:
    """The file's bytes, or None if iCloud hasn't downloaded it yet (never block on it)."""
    if is_dataless(path):
        return None
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError as e:  # Errno 35 when iCloud is still materializing it
        log(f"apple: skip {os.path.basename(path)}: {e}")
        return None


def _redo_daily(db, pairs) -> None:
    for metric, date in pairs:
        rows = db.execute("select n, total, lo, hi, last_ts, last_value, unit from partials where metric=? and date=?",
                          (metric, date)).fetchall()
        if not rows:
            db.execute("delete from daily where metric=? and date=?", (metric, date))
            continue
        unit = rows[-1][6]
        value = hdb.rollup([r[:6] for r in rows], hdb.rule(metric, unit))
        db.execute("insert or replace into daily values (?,?,?,?)", (date, metric, value, unit))


def _import_metric_file(db, path: str, raw: bytes) -> int:
    metric = os.path.basename(os.path.dirname(path))
    fid = _file_id(db, path)
    old = set(db.execute("select metric, date from partials where file=?", (fid,)))
    db.execute("delete from partials where file=?", (fid,))
    frames, bad = decode_frames(raw)
    if bad:
        log(f"apple: {bad} bad chunk(s) in {metric}/{os.path.basename(path)}")
    groups: dict[tuple[str, str], list] = {}  # (metric, local date) -> [(ts, value)]
    units: dict[str, str] = {}
    for entry in (e for f in frames for e in f.get("data", []) if isinstance(e, dict)):
        stamp = entry.get("end") if metric in WAKE_DATED else entry.get("start", entry.get("date"))
        if stamp is None:
            continue
        when = local_dt(stamp)
        unit = entry.get("unit")
        for suffix, value in readings(entry):
            name = metric + suffix
            if name in KG_METRICS and unit == "kg":
                value, unit = value * 2.20462, "lb"
            if name == "body_fat_percentage" and value <= 1:
                value, unit = value * 100, "%"
            groups.setdefault((name, when.date().isoformat()), []).append((when.isoformat(), value))
            units[name] = unit
    db.executemany("insert into partials values (?,?,?,?,?,?,?,?,?,?)",
                   [(m, d, fid, *hdb.partial(r), units[m]) for (m, d), r in groups.items()])
    _redo_daily(db, old | set(groups))
    return sum(len(r) for r in groups.values())


def _import_workout_file(db, path: str, raw: bytes) -> int:
    frames, _ = decode_frames(raw)
    if not frames:
        return 0
    w = frames[0]
    start, end = local_dt(w["start"]), local_dt(w["end"])
    wid = COPY_SUFFIX.sub("", os.path.basename(path)[:-4]).split("_")[-1]
    hr = (w.get("heartRateStatistics") or {}).get("average")
    db.execute("insert or replace into workouts (id, source, date, start, end, type, duration_min, kcal, avg_hr)"
               " values (?,?,?,?,?,?,?,?,?)",
               (wid, "apple", start.date().isoformat(), start.isoformat(), end.isoformat(),
                (w.get("activity") or {}).get("code", "workout"), round(w.get("duration", 0) / 60, 1),
                w.get("activeEnergy"), hr))
    return 1


def _drop_conflict_copies(db, paths: list[str]) -> list[str]:
    """Keep the newest of each file and its iCloud conflict copies ("20260628 2.hae"), which
    hold the same readings; forget anything imported from the others."""
    groups: dict[tuple[str, str], list[str]] = {}
    for p in paths:
        stem = COPY_SUFFIX.sub("", os.path.basename(p)[:-4])
        groups.setdefault((os.path.dirname(p), stem), []).append(p)
    keep = []
    for group in groups.values():
        newest = max(group, key=os.path.getmtime)
        keep.append(newest)
        for other in group:
            if other == newest:
                continue
            row = db.execute("select id from files where path=?", (other,)).fetchone()
            if row:
                pairs = set(db.execute("select metric, date from partials where file=?", (row[0],)))
                db.execute("delete from partials where file=?", (row[0],))
                db.execute("delete from files where id=?", (row[0],))
                _redo_daily(db, pairs)
    db.commit()
    return sorted(keep)


def import_apple(db, base: str = APPLE_BASE) -> int:
    """Import changed Apple Health files; returns how many files were read.

    Files iCloud hasn't downloaded are skipped and requested in one batch, so a later run
    (or a rerun a minute later) picks them up.
    """
    done, waiting = 0, []
    paths = sorted(glob.glob(os.path.join(base, "HealthMetrics", "*", "*.hae"))) + \
        sorted(glob.glob(os.path.join(base, "Workouts", "*.hae")))
    for path in _drop_conflict_copies(db, paths):
        mtime = os.path.getmtime(path)  # before reading, so a rewrite mid-read is caught next run
        if not _changed(db, path):
            continue
        raw = _read(path)
        if raw is None:
            waiting.append(path)
            continue
        try:
            if os.sep + "Workouts" + os.sep in path:
                _import_workout_file(db, path, raw)
            else:
                _import_metric_file(db, path, raw)
            done += 1
        except Exception as e:  # one bad file mustn't stop the rest; it's retried when it changes
            db.rollback()
            log(f"apple: skipped {path.split('AutoSync/')[-1]}: {type(e).__name__}: {e}")
        db.execute("insert into files (path, mtime) values (?, ?) on conflict(path) do update set mtime=excluded.mtime",
                   (path, mtime))
        db.commit()
    if waiting:
        log(f"apple: {len(waiting)} file(s) not downloaded from iCloud yet; asked for them")
        request_download(waiting)
    return done


# ── Hevy ─────────────────────────────────────────────────────────────────


def hevy_fetcher(key: str):
    import httpx

    def fetch(page: int) -> dict:
        r = httpx.get("https://api.hevyapp.com/v1/workouts", params={"page": page, "pageSize": 10},
                      headers={"api-key": key}, timeout=30)
        r.raise_for_status()
        return r.json()
    return fetch


def import_hevy(db, fetch_page) -> int:
    """Replace all Hevy workouts with what the API has now (it's ~7 requests)."""
    workouts, page, pages = [], 1, 1
    while page <= pages:
        data = fetch_page(page)
        pages = data.get("page_count") or 1
        workouts += data.get("workouts", [])
        page += 1
    db.execute("delete from workouts where source='hevy'")
    for w in workouts:
        start = dt.datetime.fromisoformat(w["start_time"]).astimezone(TZ)
        end = dt.datetime.fromisoformat(w["end_time"]).astimezone(TZ)
        work = [s for e in w.get("exercises", []) for s in e.get("sets", []) if s.get("type") != "warmup"]
        volume = sum((s.get("weight_kg") or 0) * (s.get("reps") or 0) for s in work)
        detail = [{"title": e["title"], "sets": [[s.get("weight_kg"), s.get("reps"), s.get("type")] for s in e.get("sets", [])]}
                  for e in w.get("exercises", [])]
        db.execute("insert or replace into workouts (id, source, date, start, end, type, duration_min, volume_kg, sets, detail)"
                   " values (?,?,?,?,?,?,?,?,?,?)",
                   (w["id"], "hevy", start.date().isoformat(), start.isoformat(), end.isoformat(), w.get("title", "workout"),
                    round((end - start).total_seconds() / 60, 1), round(volume, 1), len(work), json.dumps(detail)))
    db.commit()
    return len(workouts)


def link_workouts(db) -> int:
    """Point Apple workouts that overlap a Hevy workout at it, so they aren't counted twice."""
    hevy = [(i, dt.datetime.fromisoformat(s), dt.datetime.fromisoformat(e))
            for i, s, e in db.execute("select id, start, end from workouts where source='hevy'")]
    linked = 0
    for wid, s, e in db.execute("select id, start, end from workouts where source='apple'").fetchall():
        s, e = dt.datetime.fromisoformat(s), dt.datetime.fromisoformat(e)
        match = next((h for h, hs, he in hevy if hs < e and s < he), None)
        db.execute("update workouts set linked_to=? where id=?", (match, wid))
        linked += match is not None
    db.commit()
    return linked


# ── Eight Sleep ──────────────────────────────────────────────────────────

EIGHT_SLEEP_START = dt.date(2026, 1, 1)
WINDOW_DAYS = 60


def _current(x):
    return x.get("current") if isinstance(x, dict) else x


def _mean(series):
    vals = [v for _, v in series or [] if isinstance(v, (int, float))]
    return round(sum(vals) / len(vals), 2) if vals else None


def _minutes(sec):
    return round(sec / 60, 1) if isinstance(sec, (int, float)) else None


async def import_eight_sleep(db, fetch_days, today: dt.date | None = None) -> int:
    """Nights from 3 days before the newest one we have (or 2026-01-01) through today."""
    today = today or dt.datetime.now(TZ).date()
    newest = db.execute("select max(date) from sleep").fetchone()[0]
    start = dt.date.fromisoformat(newest) - dt.timedelta(days=3) if newest else EIGHT_SLEEP_START
    n = 0
    while start <= today:
        end = min(start + dt.timedelta(days=WINDOW_DAYS), today)
        for d in await fetch_days(start.isoformat(), end.isoformat()):
            q = d.get("sleepQualityScore") or {}
            ts = [s.get("timeseries") or {} for s in d.get("sessions") or []]
            db.execute("insert or replace into sleep values (?,?,?,?,?,?,?,?,?,?,?)", (
                d["day"], d.get("score"), _minutes(d.get("sleepDuration")), _minutes(d.get("deepDuration")),
                _minutes(d.get("remDuration")), _minutes(d.get("lightDuration")), _current(q.get("hrv")),
                _current(q.get("heartRate")), _current(q.get("respiratoryRate")),
                _mean([p for t in ts for p in t.get("tempBedC", [])]),
                _mean([p for t in ts for p in t.get("tempRoomC", [])])))
            n += 1
        start = end + dt.timedelta(days=1)
    db.commit()
    return n


def eight_sleep_fetcher():
    import eightsleep_api
    es = eightsleep_api.EightSleep()

    async def fetch(start: str, end: str) -> list[dict]:
        r = await es.request("GET", "client", "/users/{uid}/trends", params={
            "tz": host.TIMEZONE, "from": start, "to": end, "include-main": "false",
            "include-all-sessions": "true", "model-version": "v2"})
        return r.get("days", [])
    return fetch


# ── Main ─────────────────────────────────────────────────────────────────


def run_hevy(db) -> int:
    key = os.environ.get("HEVY_API_KEY")
    if not key:
        raise RuntimeError("HEVY_API_KEY isn't set (it lives in ~/.zshrc.local)")
    return import_hevy(db, hevy_fetcher(key))


def run_eight_sleep(db) -> int:
    import asyncio
    return asyncio.run(import_eight_sleep(db, eight_sleep_fetcher()))


def main(argv: list[str] | None = None) -> int:
    """Import every source; returns how many failed. --db PATH, --only apple|hevy|eight_sleep."""
    argv = argv if argv is not None else sys.argv[1:]
    path = argv[argv.index("--db") + 1] if "--db" in argv else hdb.DB_PATH
    only = argv[argv.index("--only") + 1] if "--only" in argv else None
    db = hdb.connect(path)
    sources = {"apple": lambda: import_apple(db), "hevy": lambda: run_hevy(db), "eight_sleep": lambda: run_eight_sleep(db)}
    failed = 0
    for name, run in sources.items():
        if only and name != only:
            continue
        try:
            n, err = run(), None
            log(f"{name}: {n}")
        except Exception as e:
            n, err = 0, f"{type(e).__name__}: {e}"
            failed += 1
            log(f"{name}: FAILED {err}")
        db.execute("insert or replace into imports values (?,?,?,?)", (name, dt.datetime.now(TZ).isoformat(), n, err))
        db.commit()
    link_workouts(db)
    return failed


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
