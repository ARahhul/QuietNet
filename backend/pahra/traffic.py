"""Read models over individual lookups (the `queries` table): live log, devices by IP, report analytics."""

import math
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

from .timeutil import to_iso

TRACKING = ("tracking", "telemetry", "ads")
ACTIVE_S = 5 * 60  # a device that asked something in the last 5 minutes counts as active


def _iso(t: float) -> str:
    return to_iso(datetime.fromtimestamp(t, timezone.utc))


def _like(s: str) -> str:
    return "%" + s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def live_queries(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    device_id: str | None = None,
    client_ip: str | None = None,
    status: str = "all",
    search: str | None = None,
    limit: int = 150,
) -> list[dict]:
    """Newest lookups first. `status` is all | blocked | allowed (allowed includes answers from cache)."""
    sql = (
        "SELECT q.*, v.name AS device_name, v.type AS device_type, d.category, d.owner "
        "FROM queries q JOIN devices v ON v.id = q.device_id LEFT JOIN domains d ON d.domain = q.domain "
        "WHERE v.user_id = ?"
    )
    args: list = [user_id]
    if device_id:
        sql += " AND q.device_id = ?"
        args.append(device_id)
    if client_ip:
        sql += " AND q.client_ip = ?"
        args.append(client_ip)
    if status == "blocked":
        sql += " AND q.status = 'blocked'"
    elif status == "allowed":
        sql += " AND q.status != 'blocked'"
    if search and search.strip():
        sql += " AND (q.qname LIKE ? ESCAPE '\\' OR d.owner LIKE ? ESCAPE '\\')"
        args += [_like(search.strip().lower()), _like(search.strip())]
    sql += " ORDER BY q.t DESC, q.id DESC LIMIT ?"
    args.append(limit)
    return [
        {
            "id": r["id"],
            "time": _iso(r["t"]),
            "device": {"id": r["device_id"], "name": r["device_name"], "type": r["device_type"]},
            "client_ip": r["client_ip"],
            "proto": r["proto"],
            "qname": r["qname"],
            "domain": r["domain"],
            "qtype": r["qtype"],
            "status": r["status"],
            "blocked_by": r["blocked_by"],
            "upstream": r["upstream"],
            "elapsed_ms": r["elapsed_ms"],
            "category": r["category"] or "unknown",
            "owner": r["owner"],
        }
        for r in conn.execute(sql, args)
    ]


def network_clients(conn: sqlite3.Connection, user_id: str, minutes: int = 60) -> list[dict]:
    """Every device with the addresses its lookups came from in the last `minutes`, busiest address first.

    `lan_ip` is the device's address on the home network when QuietNet knows it (laptop relay); `ips` are the
    source addresses AdGuard saw, which for encrypted DNS is the home's public address.
    """
    now = time.time()
    since = now - minutes * 60
    by_dev: dict[str, list[dict]] = defaultdict(list)
    for r in conn.execute(
        "SELECT q.device_id, q.client_ip, COUNT(*) n, SUM(q.status = 'blocked') b, MAX(q.t) last "
        "FROM queries q JOIN devices v ON v.id = q.device_id WHERE v.user_id = ? AND q.t >= ? "
        "GROUP BY q.device_id, q.client_ip ORDER BY n DESC",
        (user_id, since),
    ):
        by_dev[r["device_id"]].append(
            {"ip": r["client_ip"], "queries": r["n"], "blocked": r["b"], "last": _iso(r["last"])}
        )
    out = []
    for d in conn.execute(
        "SELECT id, name, type, brand, setup_method, lan_ip, linked_ip, last_seen FROM devices "
        "WHERE user_id = ? ORDER BY created_at",
        (user_id,),
    ):
        ips = by_dev.get(d["id"], [])
        last = max((i["last"] for i in ips), default=None)
        out.append(
            {
                **dict(d),
                "ips": ips,
                "queries": sum(i["queries"] for i in ips),
                "blocked": sum(i["blocked"] for i in ips),
                "last": last,
                "active": bool(last and datetime.fromisoformat(last).timestamp() >= now - ACTIVE_S),
            }
        )
    return out


def bucket_seconds(hours: int) -> int:
    return 600 if hours > 6 else 300 if hours > 1 else 60


def analytics(conn: sqlite3.Connection, user_id: str, hours: int = 24, device_id: str | None = None) -> dict:
    """Pi-hole style numbers for the last `hours`: totals, lookups over time, per-device activity, splits, top lists."""
    bucket_s = bucket_seconds(hours)
    end = math.ceil(time.time() / bucket_s) * bucket_s
    start = end - hours * 3600
    n = hours * 3600 // bucket_s

    where = "v.user_id = ? AND q.t >= ?"
    args: list = [user_id, start]
    if device_id:
        where += " AND q.device_id = ?"
        args.append(device_id)
    base = f"FROM queries q JOIN devices v ON v.id = q.device_id LEFT JOIN domains d ON d.domain = q.domain WHERE {where}"

    allowed, blocked = [0] * n, [0] * n
    series: dict[str, list[int]] = defaultdict(lambda: [0] * n)
    dev_q: Counter = Counter()
    dev_b: Counter = Counter()
    for r in conn.execute(
        f"SELECT CAST((q.t - ?) / ? AS INTEGER) b, q.device_id, q.status = 'blocked' bl, COUNT(*) c {base} "
        "GROUP BY b, q.device_id, bl",
        [start, bucket_s, *args],
    ):
        i = min(max(r["b"], 0), n - 1)
        (blocked if r["bl"] else allowed)[i] += r["c"]
        series[r["device_id"]][i] += r["c"]
        dev_q[r["device_id"]] += r["c"]
        if r["bl"]:
            dev_b[r["device_id"]] += r["c"]

    devices = [
        {
            "id": d["id"],
            "name": d["name"],
            "type": d["type"],
            "queries": dev_q[d["id"]],
            "blocked": dev_b[d["id"]],
            "series": series[d["id"]] if d["id"] in series else [0] * n,
        }
        for d in conn.execute("SELECT id, name, type FROM devices WHERE user_id = ? ORDER BY created_at", (user_id,))
        if not device_id or d["id"] == device_id
    ]

    t = conn.execute(
        f"SELECT COUNT(*) total, COALESCE(SUM(q.status = 'blocked'), 0) blocked, "
        f"COALESCE(SUM(q.status = 'blocked' AND COALESCE(q.blocked_by, '') != 'list'), 0) by_rules, "
        f"COALESCE(SUM(q.status = 'cached'), 0) cached, "
        f"COUNT(DISTINCT q.domain) domains, COUNT(DISTINCT q.device_id) devices, AVG(q.elapsed_ms) avg_ms {base}",
        args,
    ).fetchone()
    rule_sql = "SELECT COUNT(*) FROM decisions WHERE user_id = ? AND undone_at IS NULL AND action IN ('block', 'quarantine')"
    rule_args: list = [user_id]
    if device_id:
        rule_sql += " AND (device_id IS NULL OR device_id = ?)"
        rule_args.append(device_id)

    def top(extra: str, limit: int = 10) -> list[dict]:
        return [
            dict(r)
            for r in conn.execute(
                f"SELECT q.domain, COUNT(*) n, d.owner, COALESCE(d.category, 'unknown') category {base} {extra} "
                "GROUP BY q.domain ORDER BY n DESC, q.domain LIMIT ?",
                [*args, limit],
            )
        ]

    return {
        "hours": hours,
        "bucket_s": bucket_s,
        "start": _iso(start),
        "end": _iso(end),
        "totals": {
            "queries": t["total"],
            "blocked": t["blocked"],
            "blocked_by_rules": t["by_rules"],
            "cached": t["cached"],
            "domains": t["domains"],
            "active_devices": t["devices"],
            "avg_reply_ms": round(t["avg_ms"], 1) if t["avg_ms"] is not None else None,
            "block_rules": conn.execute(rule_sql, rule_args).fetchone()[0],
        },
        "timeline": {"allowed": allowed, "blocked": blocked},
        "devices": devices,
        # aliases avoid `name`: devices (v) has a name column, which GROUP BY would pick instead
        "query_types": [
            {"name": r["k"], "n": r["n"]}
            for r in conn.execute(
                f"SELECT COALESCE(q.qtype, 'Other') k, COUNT(*) n {base} GROUP BY k ORDER BY n DESC", args
            )
        ],
        "answered_by": [
            {"name": r["k"], "n": r["n"]}
            for r in conn.execute(
                "SELECT CASE WHEN q.status = 'blocked' THEN 'blocked' WHEN q.status = 'cached' THEN 'cache' "
                f"ELSE COALESCE(q.upstream, 'unknown') END k, COUNT(*) n {base} GROUP BY k ORDER BY n DESC",
                args,
            )
        ],
        "top_allowed": top("AND q.status != 'blocked'"),
        "top_blocked": top("AND q.status = 'blocked'"),
        "top_companies": [
            dict(r)
            for r in conn.execute(
                f"SELECT d.owner, COUNT(*) n, SUM(q.status = 'blocked') blocked, COUNT(DISTINCT q.domain) domains {base} "
                f"AND d.owner IS NOT NULL AND d.category IN {TRACKING} GROUP BY d.owner ORDER BY n DESC LIMIT 8",
                args,
            )
        ],
    }
