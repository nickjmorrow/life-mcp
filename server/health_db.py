"""The health database: Apple Health, Hevy and Eight Sleep in one SQLite file.

Raw Apple readings aren't copied in (millions of them; the full export would be ~30 GB):
they stay in Health Auto Export's iCloud files. Each file contributes a small summary per
metric and local date (`partials`), and a day's value is rolled up from those.

Written by health_import.py (hourly, launchd) and read by health_mcp.py (the connector's
health_* tools). Dates are local (the Mac's time zone); weight is pounds.
"""
import os
import sqlite3
import private

# Where the database lives: the private config's health_db (his is in synced storage), else Application Support.
DB_PATH = os.path.expanduser(private.get("health_db", "~/Library/Application Support/life-mcp/health.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS partials(metric TEXT, date TEXT, file INTEGER, n INTEGER, total REAL, lo REAL, hi REAL,
    last_ts TEXT, last_value REAL, unit TEXT);
CREATE INDEX IF NOT EXISTS partials_metric_date ON partials(metric, date);
CREATE INDEX IF NOT EXISTS partials_file ON partials(file);
CREATE TABLE IF NOT EXISTS daily(date TEXT, metric TEXT, value REAL, unit TEXT, PRIMARY KEY(date, metric));
CREATE TABLE IF NOT EXISTS workouts(id TEXT PRIMARY KEY, source TEXT, date TEXT, start TEXT, end TEXT, type TEXT,
    duration_min REAL, kcal REAL, avg_hr REAL, volume_kg REAL, sets INTEGER, detail TEXT, linked_to TEXT);
CREATE TABLE IF NOT EXISTS sleep(date TEXT PRIMARY KEY, score REAL, duration_min REAL, deep_min REAL, rem_min REAL,
    light_min REAL, hrv REAL, resting_hr REAL, resp_rate REAL, bed_temp_c REAL, room_temp_c REAL);
CREATE TABLE IF NOT EXISTS imports(source TEXT PRIMARY KEY, finished_at TEXT, rows INTEGER, error TEXT);
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY, path TEXT UNIQUE, mtime REAL);
"""

SUM, AVG, LAST, MIN, COUNT = "sum", "avg", "last", "min", "count"
RULES = {
    "resting_heart_rate": MIN,
    "apple_stand_hour": COUNT,
    **{m: LAST for m in ("weight_body_mass", "lean_body_mass", "body_fat_percentage", "body_mass_index",
                         "vo2_max", "height", "waist_circumference")},
}
# Units whose readings add up over a day (steps, energy, time, distance, food amounts).
SUM_UNITS = {"count", "kcal", "kJ", "min", "hr", "s", "km", "mi", "m", "ft", "yd", "g", "mg", "mcg", "L", "mL",
             "fl_oz_us", "cups", "IU"}


def connect(path: str = DB_PATH) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    return db


def connect_readonly(path: str = DB_PATH) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def rule(metric: str, unit: str | None) -> str:
    return RULES.get(metric) or (SUM if unit in SUM_UNITS else AVG)


Partial = tuple[int, float, float, float, str, float]  # n, total, lo, hi, last_ts, last_value


def partial(readings: list[tuple[str, float]]) -> Partial:
    """Summarize some (ts, value) readings so they can be combined with others later."""
    nums = [v for _, v in readings]
    last_ts, last_value = max(readings)
    return len(nums), sum(nums), min(nums), max(nums), last_ts, last_value


def rollup(parts: list[Partial], how: str) -> float:
    """A day's partials (from one or more files) -> the day's value."""
    n = sum(p[0] for p in parts)
    if how == SUM:
        return sum(p[1] for p in parts)
    if how == LAST:
        return max(parts, key=lambda p: p[4])[5]
    if how == MIN:
        return min(p[2] for p in parts)
    if how == COUNT:
        return n
    return sum(p[1] for p in parts) / n
