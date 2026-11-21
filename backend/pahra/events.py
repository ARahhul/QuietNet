"""Per-device domain aggregates with Welford running stats over request gaps."""

import math
import sqlite3
from datetime import datetime, timedelta, timezone

from .timeutil import parse_iso, to_iso

MIN_GAP_S = 1.0  # bursts (several subdomains collapsing into one root) are not intervals


def ingest_event(conn: sqlite3.Connection, device_id: str, domain: str, ts: datetime, blocked: bool) -> float | None:
    """Add one query. Returns None if this (device, domain) pair is new, else the gap (s) since the previous query."""
    row = conn.execute("SELECT * FROM events WHERE device_id = ? AND domain = ?", (device_id, domain)).fetchone()
    ts_s = to_iso(ts)
    if row is None:
        conn.execute(
            "INSERT INTO events(device_id, domain, count, blocked_count, first_ts, last_ts) VALUES (?, ?, 1, ?, ?, ?)",
            (device_id, domain, int(blocked), ts_s, ts_s),
        )
        return None

    last = parse_iso(row["last_ts"])
    n, mean, m2 = row["intervals_n"], row["avg_interval_s"], row["m2_interval"]
    gap = (ts - last).total_seconds()
    if gap >= MIN_GAP_S:
        n += 1
        mean = mean or 0.0
        d = gap - mean
        mean += d / n
        m2 += d * (gap - mean)
    conn.execute(
        "UPDATE events SET count = count + 1, blocked_count = blocked_count + ?, last_ts = ?, "
        "intervals_n = ?, avg_interval_s = ?, m2_interval = ? WHERE id = ?",
        (int(blocked), max(ts_s, row["last_ts"]), n, mean, m2, row["id"]),
    )
    return gap


def interval_stats(row: sqlite3.Row | dict) -> tuple[int, float | None, float | None]:
    n = row["intervals_n"]
    mean = row["avg_interval_s"]
    if n < 2 or mean is None:
        return n, mean, None
    return n, mean, math.sqrt(row["m2_interval"] / (n - 1))


def is_beacon(row: sqlite3.Row | dict) -> bool:
    """Regular 15-30 s cadence = likely beacon (SPEC §4 stage 4)."""
    n, mean, sd = interval_stats(row)
    return bool(n >= 4 and mean is not None and sd is not None and 15 <= mean <= 30 and sd / mean <= 0.25)


def purge_old_events(conn: sqlite3.Connection, days: int) -> int:
    cutoff = to_iso(datetime.now(timezone.utc) - timedelta(days=days))
    return conn.execute("DELETE FROM events WHERE last_ts < ?", (cutoff,)).rowcount
