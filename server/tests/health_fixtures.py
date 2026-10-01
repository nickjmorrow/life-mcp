"""Synthetic Health Auto Export files: JSON compressed with LZFSE, like the real ones."""
import json
import os

import liblzfse

EPOCH_2001 = 978307200  # 2001-01-01 UTC as a Unix timestamp


def t(iso_utc: str) -> float:
    """Apple-epoch seconds for a UTC time like '2026-09-01T14:00:00'."""
    import datetime as dt
    return dt.datetime.fromisoformat(iso_utc).replace(tzinfo=dt.timezone.utc).timestamp() - EPOCH_2001


def hae(*chunks, garbage=False) -> bytes:
    out = b"".join(liblzfse.compress(json.dumps(c).encode()) for c in chunks)
    return out + (b"bvx2 not really lzfse" if garbage else b"")


def write_metric(base, metric, day, entries, garbage=False, split=1):
    d = os.path.join(base, "HealthMetrics", metric)
    os.makedirs(d, exist_ok=True)
    per = max(1, len(entries) // split)
    chunks = [{"metric": metric, "data": entries[i:i + per]} for i in range(0, len(entries), per)] or [{"data": []}]
    path = os.path.join(d, f"{day}.hae")
    with open(path, "wb") as f:
        f.write(hae(*chunks, garbage=garbage))
    return path


def write_workout(base, name, obj):
    d = os.path.join(base, "Workouts")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, name), "wb") as f:
        f.write(hae(obj))
