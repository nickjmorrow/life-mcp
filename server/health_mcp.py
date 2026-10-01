"""The health_* connector tools: Nicholas's Apple Health, Hevy and Eight Sleep data in one place.

Reads the health database (health_db.DB_PATH, filled hourly by health_import.py) through a read-only
connection. HealthData holds the logic; build() wraps it as FastMCP tools.
"""
import datetime as dt
import json
import os
import re
import sqlite3
import time
import statistics
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

import health_db as hdb
import host

TZ = ZoneInfo(host.TIMEZONE)
KG_TO_LB = 2.20462
SLEEP_COLS = {"score", "duration_min", "deep_min", "rem_min", "light_min", "hrv", "resting_hr", "resp_rate",
              "bed_temp_c", "room_temp_c"}
ALIASES = {
    "sleep": ("sleep", "score"), "sleep score": ("sleep", "score"), "sleep duration": ("sleep", "duration_min"),
    "time asleep": ("sleep", "duration_min"), "deep sleep": ("sleep", "deep_min"), "rem sleep": ("sleep", "rem_min"),
    "sleep hrv": ("sleep", "hrv"), "sleeping heart rate": ("sleep", "resting_hr"), "breathing rate": ("sleep", "resp_rate"),
    "bed temperature": ("sleep", "bed_temp_c"), "room temperature": ("sleep", "room_temp_c"),
    "workouts": ("workouts", "count"), "workout minutes": ("workouts", "minutes"), "training volume": ("workouts", "volume_lb"),
    "steps": ("daily", "step_count"), "active energy": ("daily", "active_energy"),
    "exercise minutes": ("daily", "apple_exercise_time"), "resting heart rate": ("daily", "resting_heart_rate"),
    "hrv": ("daily", "heart_rate_variability"), "heart rate": ("daily", "heart_rate"),
    "weight": ("daily", "weight_body_mass"), "body fat": ("daily", "body_fat_percentage"), "vo2 max": ("daily", "vo2_max"),
}
# (domain, label, metric, how to combine a period: avg per day / total / last, format)
SUMMARY = [
    ("sleep", "sleep score", "sleep score", "avg", "{:.0f}"),
    ("sleep", "time asleep", "sleep duration", "avg", "{:.1f}h", 1 / 60),
    ("sleep", "sleep hrv", "sleep hrv", "avg", "{:.0f} ms"),
    ("training", "workouts", "workouts", "total", "{:.0f}"),
    ("training", "workout time", "workout minutes", "total", "{:.0f} min"),
    ("training", "lifting volume", "training volume", "total", "{:,.0f} lb"),
    ("activity", "steps", "steps", "avg", "{:,.0f}/day"),
    ("activity", "active energy", "active energy", "avg", "{:,.0f} kcal/day"),
    ("activity", "exercise", "exercise minutes", "avg", "{:.0f} min/day"),
    ("heart", "resting heart rate", "resting heart rate", "avg", "{:.0f} bpm"),
    ("heart", "hrv", "hrv", "avg", "{:.0f} ms"),
    ("body", "weight", "weight", "last", "{:.1f} lb"),
    ("body", "body fat", "body fat", "last", "{:.1f}%"),
]
PERIODS = {"week": 7, "month": 30}
# Apple Health's today is still filling up, so by default these stop at yesterday; last night's
# sleep and today's workouts are complete, so those include today.
UNTIL_YESTERDAY = {"activity", "heart", "body"}
STALE_DAYS = 2
MIN_GROUP = 5
ROW_LIMIT = 500
QUERY_SECONDS = 10
UNITS = {"day": 1, "week": 7, "month": 30}
# health_query may only read: SQLite asks this for every action a statement takes.
READ_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, getattr(sqlite3, "SQLITE_RECURSIVE", 33)}


def _authorize(action, arg1, arg2, dbname, source):
    if action == sqlite3.SQLITE_READ and (arg1 or "").startswith("pragma_"):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK if action in READ_ACTIONS else sqlite3.SQLITE_DENY


def parse_period(period: str) -> tuple[int, str]:
    """'week' / 'month' / '10 days' / '2 weeks' / '3 months' -> (days, label)."""
    if period in PERIODS:
        return PERIODS[period], period
    m = re.fullmatch(r"\s*(\d+)\s*(day|week|month)s?\s*", period.lower())
    if not m or int(m[1]) < 1:
        raise ToolError("period must be week, month, or like '10 days', '2 weeks', '3 months'.")
    days = int(m[1]) * UNITS[m[2]]
    return days, f"last {days} days"

INSTRUCTIONS = (
    " The health_* tools answer questions about Nicholas's health data across Apple Health, Hevy and"
    " Eight Sleep (sleep, workouts, steps, heart, weight): health_summary for 'how am I doing',"
    " health_compare for patterns ('do I sleep worse after gym days' = sleep score, split workout,"
    " lag_days 1), health_trend for one metric over time. The eight_sleep_* tools stay for controlling"
    " the bed and for last night in detail. Describe the data; no medical advice or diagnosis, and"
    " don't override any treatment plan noted in memory."
)


def safe_sql(sql: str) -> str:
    s = sql.strip().rstrip(";").strip()
    if ";" in s:
        raise ToolError("One statement at a time.")
    if not re.match(r"(?is)^(select|with)\b", s):
        raise ToolError("Only read-only SELECT queries are allowed.")
    return s


def _fmt(fmt: str, v: float) -> str:
    return fmt.format(v)


def _num(v: float) -> str:
    return f"{v:,.0f}" if abs(v) >= 100 else f"{v:.1f}".rstrip("0").rstrip(".")


class HealthData:
    def __init__(self, db, today: dt.date | None = None):
        self.db = db
        self.today = today or dt.datetime.now(TZ).date()

    # ── names and series ─────────────────────────────────────────────

    def _exercises(self) -> list[str]:
        names = set()
        for (detail,) in self.db.execute("select detail from workouts where source='hevy' and detail is not null"):
            names |= {e["title"] for e in json.loads(detail)}
        return sorted(names)

    def resolve(self, name: str) -> tuple[str, str]:
        key = name.strip().lower().replace("_", " ")
        if key in ALIASES:
            return ALIASES[key]
        snake = key.replace(" ", "_")
        metrics = [m for (m,) in self.db.execute("select distinct metric from daily")]
        if snake in metrics:
            return "daily", snake
        if snake in SLEEP_COLS:
            return "sleep", snake
        close = [m for m in metrics if snake in m]
        if len(close) == 1:
            return "daily", close[0]
        ex = [e for e in self._exercises() if key in e.lower()]
        if ex:
            return "exercise", min(ex, key=len)
        raise ToolError(f"No health metric called '{name}'. Call health_metrics to see what's available.")

    def series(self, name: str, start: str, end: str) -> dict[str, float]:
        kind, key = self.resolve(name)
        if kind == "daily":
            rows = self.db.execute("select date, value from daily where metric=? and date between ? and ?", (key, start, end))
        elif kind == "sleep":
            rows = self.db.execute(f"select date, {key} from sleep where {key} is not null and date between ? and ?", (start, end))
        elif kind == "workouts":
            expr = {"count": "count(*)", "minutes": "sum(duration_min)", "volume_lb": f"sum(volume_kg) * {KG_TO_LB}"}[key]
            where = "linked_to is null" + (" and source='hevy'" if key == "volume_lb" else "")
            rows = self.db.execute(f"select date, {expr} from workouts where {where} and date between ? and ? group by date",
                                   (start, end))
        else:  # exercise: heaviest set that day, in lb
            out = {}
            for date, detail in self.db.execute("select date, detail from workouts where source='hevy' and date between ? and ?",
                                                (start, end)):
                for e in json.loads(detail or "[]"):
                    if e["title"] == key:
                        top = max((s[0] or 0 for s in e["sets"]), default=0) * KG_TO_LB
                        out[date] = max(out.get(date, 0), round(top, 1))
            return out
        return {d: v for d, v in rows if v is not None}

    # ── tools ────────────────────────────────────────────────────────

    def _combine(self, values: dict[str, float], how: str, metric: str | None = None, end: str | None = None) -> float | None:
        if not values:
            # No workouts in a period is a real 0, as long as there's any history before it.
            if how == "total" and metric and end and self.series(metric, "0000-01-01", end):
                return 0.0
            return None
        if how == "total":
            return sum(values.values())
        if how == "last":
            return values[max(values)]
        return sum(values.values()) / len(values)

    def _freshness(self) -> list[str]:
        notes = []
        newest = {"apple health": self.db.execute("select max(date) from daily").fetchone()[0],
                  "eight sleep": self.db.execute("select max(date) from sleep").fetchone()[0]}
        for src, day in newest.items():
            if day and (self.today - dt.date.fromisoformat(day)).days > STALE_DAYS:
                notes.append(f"{src} data is {(self.today - dt.date.fromisoformat(day)).days} days old (newest {day})")
        for src, err in self.db.execute("select source, error from imports where error is not null"):
            notes.append(f"last {src} import failed: {err}")
        return notes

    def summary(self, period: str = "week", end: str | None = None) -> str:
        days, label = parse_period(period)
        end_d = dt.date.fromisoformat(end) if end else self.today
        start_d = end_d - dt.timedelta(days=days - 1)
        before = f"the previous 4 {label}s" if label in PERIODS else f"the 4 periods before"
        lines, domain = [f"{label} {start_d}..{end_d} vs {before}"], None
        for row in SUMMARY:
            dom, label, metric, how, fmt = row[:5]
            scale = row[5] if len(row) > 5 else 1
            shift = dt.timedelta(days=1 if end is None and dom in UNTIL_YESTERDAY else 0)
            rs, re_ = start_d - shift, end_d - shift
            try:
                now = self._combine(self.series(metric, rs.isoformat(), re_.isoformat()), how, metric, re_.isoformat())
            except ToolError:
                continue
            if now is None:
                continue
            past = []
            for k in range(1, 5):
                pe = rs - dt.timedelta(days=days * (k - 1) + 1)
                ps = pe - dt.timedelta(days=days - 1)
                v = self._combine(self.series(metric, ps.isoformat(), pe.isoformat()), how, metric, pe.isoformat())
                if v is not None:
                    past.append(v)
            if dom != domain:
                lines.append(dom)
                domain = dom
            text = f"- {label} {_fmt(fmt, now * scale)}"
            if past:
                usual = sum(past) / len(past)
                unit = re.sub(r"\{[^}]*\}", "", fmt)  # "{:.1f}h" -> "h"
                diff = _num(abs(now - usual) * scale)
                change = "same" if diff == "0" else f"{'+' if now >= usual else '-'}{diff}{unit}"
                text += f" (usual {_fmt(fmt, usual * scale)}, {change})"
            lines.append(text)
        notes = self._freshness()
        return "\n".join(lines + ([f"note: {n}" for n in notes] if notes else []))

    def trend(self, metric: str, start: str | None = None, end: str | None = None, bucket: str | None = None) -> str:
        end = end or self.today.isoformat()
        start = start or (dt.date.fromisoformat(end) - dt.timedelta(days=89)).isoformat()
        s = self.series(metric, start, end)
        if not s:
            return f"No {metric} data between {start} and {end}."
        bucket = bucket or ("week" if len(s) > 60 else "day")
        if bucket == "week":
            weeks: dict[str, list[float]] = {}
            for day, v in s.items():
                mon = dt.date.fromisoformat(day) - dt.timedelta(days=dt.date.fromisoformat(day).weekday())
                weeks.setdefault(mon.isoformat(), []).append(v)
            total = self.resolve(metric)[0] == "workouts"  # workouts add up over a week; the rest average
            s = {w: sum(v) if total else sum(v) / len(v) for w, v in weeks.items()}
        items = sorted(s.items())
        vals = [v for _, v in items]
        lines = [f"{metric} by {bucket}, {start}..{end}: average {_num(sum(vals) / len(vals))},"
                 f" min {_num(min(vals))}, max {_num(max(vals))}"]
        if len(items) > 1:
            jumps = [(abs(b[1] - a[1]), a, b) for a, b in zip(items, items[1:])]
            _, a, b = max(jumps)
            lines.append(f"biggest change: {a[0]} {_num(a[1])} -> {b[0]} {_num(b[1])}")
        lines += [f"{day}: {_num(v)}" for day, v in items]
        return "\n".join(lines)

    def _split(self, split: str):
        """(label_a, label_b, test(date) -> True/False/None)."""
        if split == "weekday":
            return "weekdays", "weekends", lambda d: dt.date.fromisoformat(d).weekday() < 5
        if split.startswith("date:"):
            cut = split[5:]
            return f"from {cut} on", f"before {cut}", lambda d: d >= cut
        if split.startswith("above:"):
            other = self.series(split[6:], "0000-01-01", "9999-12-31")
            if not other:
                raise ToolError(f"No data for {split[6:]}.")
            med = statistics.median(other.values())
            return f"{split[6:]} above {_num(med)}", f"{split[6:]} at or below {_num(med)}", \
                lambda d: None if d not in other else other[d] > med
        if split.split(":")[0] == "workout":
            want = split[8:].lower()
            rows = self.db.execute("select date, type from workouts where linked_to is null").fetchall()
            days = {d for d, t in rows if want in (t or "").lower()}
            first = min((d for d, _ in rows), default=None)
            return "workout days", "rest days", lambda d: None if first is None or d < first else d in days
        raise ToolError("split must be workout, workout:<type>, weekday, date:YYYY-MM-DD or above:<metric>.")

    def compare(self, metric: str, split: str, lag_days: int = 0, start: str | None = None, end: str | None = None) -> str:
        s = self.series(metric, start or "0000-01-01", end or self.today.isoformat())
        label_a, label_b, test = self._split(split)
        a, b = [], []
        for day, v in s.items():
            when = (dt.date.fromisoformat(day) - dt.timedelta(days=lag_days)).isoformat()
            side = test(when)
            if side is not None:
                (a if side else b).append(v)
        prefix = f"the day after " if lag_days == 1 else (f"{lag_days} days after " if lag_days else "")
        if not a or not b:
            return f"Not enough data to compare {metric} by {split}: {len(a)} vs {len(b)} days."
        ma, mb = sum(a) / len(a), sum(b) / len(b)
        out = (f"{metric} {prefix}{label_a}: {_num(ma)} (n={len(a)}); {prefix}{label_b}: {_num(mb)} (n={len(b)});"
               f" difference {'+' if ma >= mb else '-'}{_num(abs(ma - mb))}")
        if min(len(a), len(b)) < MIN_GROUP:
            out += f"\nnote: too few days in one group (<{MIN_GROUP}) to mean much."
        return out

    def metrics(self) -> str:
        lines = ["daily metrics (Apple Health):"]
        for m, first, last, unit in self.db.execute(
                "select metric, min(date), max(date), max(unit) from daily group by metric order by metric"):
            lines.append(f"- {m} ({unit}) {first}..{last}")
        first, last = self.db.execute("select min(date), max(date) from sleep").fetchone()
        if first:
            lines.append(f"sleep (Eight Sleep) {first}..{last}: " + ", ".join(k for k, v in ALIASES.items() if v[0] == "sleep"))
        lines.append("workouts (Hevy + Apple): workouts, workout minutes, training volume")
        ex = self._exercises()
        if ex:
            lines.append("exercises (top set, lb): " + ", ".join(ex))
        return "\n".join(lines)

    def query(self, sql: str) -> str:
        sql = safe_sql(sql)
        deadline = time.monotonic() + QUERY_SECONDS
        self.db.set_authorizer(_authorize)
        self.db.set_progress_handler(lambda: time.monotonic() > deadline, 10_000)
        try:
            cur = self.db.execute(sql)
            rows = cur.fetchmany(ROW_LIMIT)
        except sqlite3.DatabaseError as e:
            if "interrupt" in str(e).lower():
                raise ToolError(f"That query ran too long (over {QUERY_SECONDS} s); narrow it down.")
            if "not authorized" in str(e).lower():
                raise ToolError("Only read-only SELECT queries over the health tables are allowed.")
            raise ToolError(f"SQL error: {e}")
        finally:
            self.db.set_authorizer(None)
            self.db.set_progress_handler(None, 0)
        head = "\t".join(c[0] for c in cur.description)
        more = "\n(first 500 rows)" if len(rows) == ROW_LIMIT else ""
        return "\n".join([head] + ["\t".join("" if v is None else str(v) for v in r) for r in rows]) + more


def build(db_path: str = hdb.DB_PATH) -> FastMCP:
    mcp = FastMCP("Health")
    READ = {"readOnlyHint": True}

    def data() -> HealthData:
        if not os.path.exists(db_path):
            raise ToolError("No health data imported yet: the hourly importer (com.nicholai.health-import) hasn't run.")
        return HealthData(hdb.connect_readonly(db_path))

    @mcp.tool(annotations=READ)
    def health_summary(
        period: Annotated[str, Field(description="week, month, or 'N days'")] = "week",
        end: Annotated[str | None, Field(description="YYYY-MM-DD; default today")] = None,
    ) -> str:
        """How Nicholas is doing: sleep, training, activity, heart and body for a period vs his usual."""
        return data().summary(period, end)

    @mcp.tool(annotations=READ)
    def health_trend(
        metric: Annotated[str, Field(description="e.g. 'sleep score', 'resting heart rate', 'steps', 'bench press'")],
        start: Annotated[str | None, Field(description="YYYY-MM-DD; default 90 days ago")] = None,
        end: Annotated[str | None, Field(description="YYYY-MM-DD; default today")] = None,
        bucket: Annotated[Literal["day", "week"] | None, Field(description="default: day, or week past 60 days")] = None,
    ) -> str:
        """One metric over time with average, min, max and the biggest change."""
        return data().trend(metric, start, end, bucket)

    @mcp.tool(annotations=READ)
    def health_compare(
        metric: Annotated[str, Field(description="What to compare, e.g. 'sleep score'")],
        split: Annotated[str, Field(description="workout | workout:<type> | weekday | date:YYYY-MM-DD | above:<metric>")],
        lag_days: Annotated[int, Field(ge=0, le=7, description="1 = the next day, e.g. sleep after gym days")] = 0,
        start: Annotated[str | None, Field(description="YYYY-MM-DD")] = None,
        end: Annotated[str | None, Field(description="YYYY-MM-DD")] = None,
    ) -> str:
        """Split a metric's days into two groups and compare their averages."""
        return data().compare(metric, split, lag_days, start, end)

    @mcp.tool(annotations=READ)
    def health_metrics() -> str:
        """Every health metric available, with date ranges."""
        return data().metrics()

    @mcp.tool(annotations=READ)
    def health_query(sql: Annotated[str, Field(description="One read-only SELECT over tables daily, partials, workouts, sleep, imports")]) -> str:
        """Read-only SQL for questions the other health tools don't cover (max 500 rows)."""
        return data().query(sql)

    return mcp
