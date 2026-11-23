"""Read models for the dashboard: suggestion cards, activity, risk score, weekly report."""

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .events import is_beacon
from .knowledge import is_must_allow_any_device
from .timeutil import parse_iso, to_iso

CARD_CATEGORIES = {"malware", "tracking", "telemetry", "ads"}
CONF_RANK = {"high": 0, "medium": 1, "low": 2}


def _decision_index(conn: sqlite3.Connection, user_id: str) -> tuple[dict, dict]:
    per_device: dict[tuple[str, str], dict] = {}
    household: dict[str, dict] = {}
    for r in conn.execute("SELECT * FROM decisions WHERE user_id = ? AND undone_at IS NULL", (user_id,)):
        d = dict(r)
        if d["device_id"]:
            per_device[(d["device_id"], d["domain"])] = d
        else:
            household[d["domain"]] = d
    return per_device, household


def effective_decision(per_device: dict, household: dict, device_id: str, domain: str) -> dict | None:
    mine = per_device.get((device_id, domain))
    if mine and mine["action"] in ("allow", "block"):
        return mine
    if domain in household:
        return household[domain]
    return mine  # quarantine or None


def _rows(conn: sqlite3.Connection, user_id: str, device_id: str | None = None, since: str | None = None) -> list[dict]:
    sql = (
        "SELECT e.*, d.category, d.owner, d.what_it_does, d.break_risk, d.break_note, d.confidence, d.sources, "
        "d.signals, d.explained_by, v.name AS device_name, v.type AS device_type, v.brand AS device_brand "
        "FROM events e JOIN domains d ON d.domain = e.domain JOIN devices v ON v.id = e.device_id WHERE v.user_id = ?"
    )
    args: list = [user_id]
    if device_id:
        sql += " AND e.device_id = ?"
        args.append(device_id)
    if since:
        sql += " AND e.last_ts >= ?"
        args.append(since)
    out = []
    for r in conn.execute(sql, args):
        d = dict(r)
        d["sources"] = json.loads(d["sources"] or "[]")
        d["signals"] = json.loads(d["signals"] or "{}")
        out.append(d)
    return out


def _category_rank(cat: str, acr: bool) -> float:
    if cat == "malware":
        return 0
    if acr:
        return 0.5
    return {"tracking": 1, "telemetry": 1, "ads": 2, "unknown": 3}.get(cat, 4)


def suggestions(conn: sqlite3.Connection, user_id: str, device_id: str | None = None) -> list[dict]:
    per_device, household = _decision_index(conn, user_id)
    cards: dict[str, dict] = {}
    for r in _rows(conn, user_id, device_id):
        cat = r["category"]
        sig = r["signals"]
        interesting_unknown = cat == "unknown" and (
            r["device_type"] in ("tv", "router", "other") or sig.get("suspicious_unknown") or sig.get("beacon")
        )
        if cat not in CARD_CATEGORIES and not interesting_unknown:
            continue
        dec = effective_decision(per_device, household, r["device_id"], r["domain"])
        if dec and dec["action"] in ("block", "allow"):
            continue
        card = cards.setdefault(
            r["domain"],
            {
                "domain": r["domain"],
                "category": cat,
                "owner": r["owner"],
                "what_it_does": r["what_it_does"],
                "break_risk": r["break_risk"],
                "break_note": r["break_note"],
                "confidence": r["confidence"],
                "sources": r["sources"],
                "explained_by": r["explained_by"],
                "acr": bool(sig.get("acr")),
                "must_allow": is_must_allow_any_device(r["domain"]),
                "total_count": 0,
                "devices": [],
            },
        )
        card["total_count"] += r["count"]
        card["devices"].append(
            {
                "device_id": r["device_id"],
                "name": r["device_name"],
                "type": r["device_type"],
                "count": r["count"],
                "last_ts": r["last_ts"],
                "beacon": is_beacon(r),
                "avg_interval_s": round(r["avg_interval_s"], 1) if r["avg_interval_s"] else None,
                "quarantined": bool(dec and dec["action"] == "quarantine"),
                "quarantine_decision_id": dec["id"] if dec and dec["action"] == "quarantine" else None,
            }
        )
    out = list(cards.values())
    for c in out:
        c["status"] = "quarantined" if any(d["quarantined"] for d in c["devices"]) else "pending"
    out.sort(key=lambda c: (_category_rank(c["category"], c["acr"]), CONF_RANK.get(c["confidence"], 3), -c["total_count"]))
    return out


def device_risk(rows: list[dict], per_device: dict, household: dict) -> int:
    score = 0.0
    for r in rows:
        dec = effective_decision(per_device, household, r["device_id"], r["domain"])
        blocked = bool(dec and dec["action"] in ("block", "quarantine"))
        cat = r["category"]
        if cat == "malware":
            score += 5 if blocked else 30
        elif cat in ("tracking", "telemetry"):
            score += (1 if blocked else 6) + (0 if blocked or not r["signals"].get("acr") else 8)
        elif cat == "ads":
            score += 0.5 if blocked else 3
        elif cat == "unknown":
            score += 1
    return min(100, round(score))


def devices_overview(conn: sqlite3.Connection, user_id: str) -> list[dict]:
    per_device, household = _decision_index(conn, user_id)
    rows_by_dev: dict[str, list[dict]] = defaultdict(list)
    for r in _rows(conn, user_id):
        rows_by_dev[r["device_id"]].append(r)
    online_cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
    out = []
    for d in conn.execute("SELECT * FROM devices WHERE user_id = ? ORDER BY created_at", (user_id,)):
        dev = dict(d)
        rows = rows_by_dev.get(dev["id"], [])
        dev["risk_score"] = device_risk(rows, per_device, household)
        dev["stats"] = {
            "domains": len(rows),
            "queries": sum(r["count"] for r in rows),
            "blocked_queries": sum(r["blocked_count"] for r in rows),
            "trackers": sum(1 for r in rows if r["category"] in ("tracking", "telemetry", "ads")),
            "threats": sum(1 for r in rows if r["category"] == "malware"),
        }
        dev["online"] = bool(dev["last_seen"] and parse_iso(dev["last_seen"]) > online_cutoff)
        out.append(dev)
    return out


def device_activity(conn: sqlite3.Connection, user_id: str, device_id: str) -> list[dict]:
    per_device, household = _decision_index(conn, user_id)
    groups: dict[str, dict] = {}
    for r in _rows(conn, user_id, device_id):
        owner = r["owner"] or "Unknown company"
        g = groups.setdefault(owner, {"owner": owner, "count": 0, "domains": []})
        dec = effective_decision(per_device, household, device_id, r["domain"])
        g["count"] += r["count"]
        g["domains"].append(
            {
                "domain": r["domain"],
                "category": r["category"],
                "confidence": r["confidence"],
                "what_it_does": r["what_it_does"],
                "count": r["count"],
                "blocked_count": r["blocked_count"],
                "last_ts": r["last_ts"],
                "beacon": is_beacon(r),
                "decision": {"id": dec["id"], "action": dec["action"], "household": dec["device_id"] is None} if dec else None,
            }
        )
    out = sorted(groups.values(), key=lambda g: -g["count"])
    for g in out:
        g["domains"].sort(key=lambda x: -x["count"])
    return out


def blocked_domains(conn: sqlite3.Connection, user_id: str) -> list[dict]:
    """Every block rule in force: household and per-device blocks, plus quarantines a household rule doesn't override.

    Each row carries what we know about the domain, which devices it applies to, how often those devices were
    stopped from reaching it, and the devices a per-device allow exempts from a household block.
    """
    per_device, household = _decision_index(conn, user_id)
    devices = {r["id"]: dict(r) for r in conn.execute("SELECT id, name, type FROM devices WHERE user_id = ?", (user_id,))}
    stats: dict[tuple[str, str], dict] = {}
    for r in conn.execute(
        "SELECT e.device_id, e.domain, e.count, e.blocked_count, e.last_ts FROM events e "
        "JOIN devices v ON v.id = e.device_id WHERE v.user_id = ?",
        (user_id,),
    ):
        stats[(r["device_id"], r["domain"])] = dict(r)
    info = {
        r["domain"]: dict(r)
        for r in conn.execute(
            "SELECT domain, category, owner, what_it_does, confidence, break_risk, break_note FROM domains WHERE domain IN "
            "(SELECT domain FROM decisions WHERE user_id = ? AND undone_at IS NULL)",
            (user_id,),
        )
    }

    rules = [d for d in household.values() if d["action"] == "block"]
    rules += [
        d
        for (_, dom), d in per_device.items()
        if d["action"] == "block" or (d["action"] == "quarantine" and dom not in household)
    ]
    out = []
    for d in rules:
        dom = d["domain"]
        if d["device_id"]:
            scope_ids = [d["device_id"]]
            exempt: list[str] = []
        else:
            exempt = [i for i in devices if (m := per_device.get((i, dom))) and m["action"] == "allow"]
            scope_ids = [i for i in devices if i not in exempt]
        hits = [stats[(i, dom)] for i in scope_ids if (i, dom) in stats]
        meta = info.get(dom, {})
        dev = devices.get(d["device_id"]) if d["device_id"] else None
        out.append(
            {
                "id": d["id"],
                "domain": dom,
                "action": d["action"],
                "decided_by": d["decided_by"],
                "created_at": d["created_at"],
                "device": {"id": dev["id"], "name": dev["name"], "type": dev["type"]} if dev else None,
                "allowed_on": [{"id": i, "name": devices[i]["name"]} for i in exempt],
                "category": meta.get("category") or "unknown",
                "owner": meta.get("owner"),
                "what_it_does": meta.get("what_it_does"),
                "confidence": meta.get("confidence"),
                "break_risk": meta.get("break_risk"),
                "break_note": meta.get("break_note"),
                "must_allow": is_must_allow_any_device(dom),
                "blocked_count": sum(h["blocked_count"] for h in hits),
                "last_seen": max((h["last_ts"] for h in hits), default=None),
            }
        )
    out.sort(key=lambda b: b["created_at"], reverse=True)
    return out


def weekly_report(conn: sqlite3.Connection, user_id: str) -> dict:
    since = to_iso(datetime.now(timezone.utc) - timedelta(days=7))
    rows = _rows(conn, user_id, since=since)
    per_device, household = _decision_index(conn, user_id)
    collectors: dict[str, dict] = {}
    for r in rows:
        if r["category"] in ("tracking", "telemetry", "ads"):
            k = r["owner"] or r["domain"]
            c = collectors.setdefault(k, {"owner": k, "queries": 0, "blocked": 0, "domains": set()})
            c["queries"] += r["count"]
            c["blocked"] += r["blocked_count"]
            c["domains"].add(r["domain"])
    top = sorted(collectors.values(), key=lambda c: -c["queries"])[:5]
    for c in top:
        c["domains"] = sorted(c["domains"])
    devs = devices_overview(conn, user_id)
    decisions = conn.execute(
        "SELECT action, decided_by, COUNT(*) n FROM decisions WHERE user_id = ? AND created_at >= ? GROUP BY action, decided_by",
        (user_id, since),
    ).fetchall()
    alerts = conn.execute(
        "SELECT type, COUNT(*) n FROM alerts WHERE user_id = ? AND created_at >= ? GROUP BY type", (user_id, since)
    ).fetchall()
    return {
        "since": since,
        "queries": sum(r["count"] for r in rows),
        "blocked_queries": sum(r["blocked_count"] for r in rows),
        "top_collectors": top,
        "devices": [
            {"id": d["id"], "name": d["name"], "risk_score": d["risk_score"], **d["stats"]} for d in devs
        ],
        "devices_to_check": [d["name"] for d in devs if d["risk_score"] >= 50],
        "decisions": [dict(r) for r in decisions],
        "alerts": [dict(r) for r in alerts],
    }
