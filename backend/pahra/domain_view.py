"""The "View" panel: what a domain is, why it is (or isn't) blocked for this home, and what this home looked up.

Everything a home sees here is its own: its decisions and its own lookups. The description and list data are public
facts about the domain, shared by everyone.
"""

import json
import sqlite3
import time
from datetime import datetime, timezone

from .adguard.rules import validate_domain
from .blocklist import BlocklistIndex
from .knowledge import is_must_allow_any_device
from .pipeline.fastchecks import fast_check
from .pipeline.lists import ListIndex, parents
from .pipeline.normalize import normalize_domain
from .queries import _decision_index
from .site_fix import _now
from .timeutil import to_iso

CATEGORY_NAME = {
    "ads": "advertising",
    "tracking": "tracking",
    "telemetry": "device telemetry",
    "malware": "malware or phishing",
}
RULE_TITLE = {"user": "Blocked by your rule", "auto": "Blocked by Auto-mode"}


def _iso(ts: float | None) -> str | None:
    return to_iso(datetime.fromtimestamp(ts, timezone.utc)) if ts else None


def _scope(conn: sqlite3.Connection, d: dict) -> dict:
    if not d["device_id"]:
        return {"household": True, "device": None}
    v = conn.execute("SELECT id, name FROM devices WHERE id = ?", (d["device_id"],)).fetchone()
    return {"household": False, "device": {"id": v["id"], "name": v["name"]} if v else None}


def domain_detail(
    conn: sqlite3.Connection,
    user_id: str,
    name: str,
    *,
    blocklist: BlocklistIndex,
    blocklist_active: bool,
    lists: ListIndex,
    device_id: str | None = None,
    hours: int = 24,
) -> dict:
    name = validate_domain(name)
    domain = normalize_domain(name)
    row = conn.execute(
        "SELECT domain, category, owner, what_it_does, break_risk, break_note, confidence, sources, explained_by, signals "
        "FROM domains WHERE domain IN (?, ?) ORDER BY domain = ? DESC LIMIT 1",
        (name, domain, name),
    ).fetchone()
    info = dict(row) if row else {}
    sources = json.loads(info.get("sources") or "[]")
    signals = json.loads(info.get("signals") or "{}")

    # This home's lookups of the domain (all names under it) in the window
    since = time.time() - hours * 3600
    sql = (
        "SELECT q.qname, q.device_id, v.name AS device_name, COUNT(*) AS n, SUM(q.status = 'blocked') AS blocked, "
        "MAX(q.t) AS last FROM queries q JOIN devices v ON v.id = q.device_id "
        "WHERE v.user_id = ? AND q.domain = ? AND q.t >= ?"
    )
    args: list = [user_id, domain, since]
    if device_id:
        sql += " AND q.device_id = ?"
        args.append(device_id)
    looked_up: dict[str, dict] = {}
    devices: dict[str, dict] = {}
    for r in conn.execute(sql + " GROUP BY q.qname, q.device_id", args):
        q = looked_up.setdefault(r["qname"], {"name": r["qname"], "count": 0, "blocked": 0, "last_seen": 0.0})
        q["count"] += r["n"]
        q["blocked"] += r["blocked"] or 0
        q["last_seen"] = max(q["last_seen"], r["last"])
        dv = devices.setdefault(r["device_id"], {"id": r["device_id"], "name": r["device_name"], "count": 0, "blocked": 0, "last_seen": 0.0})
        dv["count"] += r["n"]
        dv["blocked"] += r["blocked"] or 0
        dv["last_seen"] = max(dv["last_seen"], r["last"])

    # A view of a whole domain (Suggestions, Report) counts as listed when every name looked up under it is listed,
    # as the pipeline decides: ads.example.com on the list doesn't put all of example.com on it
    whole = name == domain
    entry = blocklist.entry(name)
    covered: list[str] = []
    if entry is None and whole:
        entry = blocklist.match(domain, list(looked_up))
        if entry is not None:
            covered = [n for n in looked_up if n != domain]
    listed = entry is not None and entry.in_adguard and blocklist_active
    fast = fast_check(lists, name)
    clean = blocklist.clean_match(domain)
    must_allow = is_must_allow_any_device(name)

    # This home's rules on the name or a parent (they cover it, as in AdGuard) and, for a whole domain, on names under it
    names = parents(name)[:-1] or [name]
    decisions = [
        d
        for d in (
            dict(r)
            for r in conn.execute(
                "SELECT * FROM decisions WHERE user_id = ? AND undone_at IS NULL "
                f"AND (domain IN ({','.join('?' * len(names))}) OR (? AND domain LIKE ?)) ORDER BY created_at DESC",
                (user_id, *names, int(whole), f"%.{name}"),
            )
        )
        if d["domain"] in names or d["domain"].endswith("." + name)
    ]

    def applies(d: dict) -> bool:
        """The rule covers the device in view (or, with no device in view, the whole home)."""
        return d["device_id"] is None or d["device_id"] == device_id

    cover = [d for d in decisions if d["domain"] in names and applies(d)]
    allow = next((d for d in cover if d["action"] == "allow"), None)
    block = next((d for d in cover if d["action"] in ("block", "quarantine")), None)
    per_device, household = _decision_index(conn, user_id)
    for n, q in looked_up.items():
        q["let_through"] = _now(per_device, household, device_id or "", n)[0] == "allowed"
    pending = [n for n in covered if not looked_up[n]["let_through"]]  # listed names under the domain still blocked

    reasons: list[dict] = []
    if must_allow:
        reasons.append({
            "kind": "must_allow", "title": "On QuietNet's must-allow list",
            "detail": "Devices or core apps need it (updates, time, streaming). QuietNet never blocks it by itself.",
        })
    for d in decisions:
        scope = _scope(conn, d)
        if d["action"] == "allow":
            title, detail = "You let it through", "Your allow rule wins over every block, the block lists included."
        elif d["action"] == "quarantine":
            title, detail = "Held as new", "Brand new and suspicious, so QuietNet holds it until it knows more."
        else:
            title = RULE_TITLE.get(d["decided_by"], "Blocked by your rule")
            detail = "One of this home's own rules."
        reasons.append({
            "kind": "let_through" if d["action"] == "allow" else "held" if d["action"] == "quarantine" else "rule",
            "title": title, "detail": detail, "decision_id": d["id"], "rule_domain": d["domain"],
            "decided_by": d["decided_by"], "created_at": d["created_at"], **scope,
            "applies": applies(d),
        })
    if entry is not None and entry.in_adguard:
        what = CATEGORY_NAME.get(entry.category, entry.category)
        reasons.append({
            "kind": "blocklist", "title": "On QuietNet's ad & tracker list",
            "detail": f"Known {what} domain{f' run by {entry.owner}' if entry.owner else ''}. "
            "Blocked for every home on the very first lookup, without asking the AI.",
            "category": entry.category, "owner": entry.owner, "sources": list(entry.sources), "notes": entry.notes,
            "active": blocklist_active, "names": covered,
        })
    if fast.threat_hits:
        reasons.append({
            "kind": "threat_list", "title": "On a threat-intelligence list",
            "detail": "Reported as malware or phishing. Blocked for every home.", "sources": fast.threat_hits,
        })
    if clean is not None and not must_allow:
        reasons.append({
            "kind": "clean", "title": "On QuietNet's clean list",
            "detail": {
                "popular": "One of the most visited sites, and on no ad or tracker list.",
                "tracker_radar": "DuckDuckGo Tracker Radar lists it as a service apps need (content, sign-in, payments).",
                "seed": "Checked by hand: a service devices need.",
            }.get(clean.kind, "A service apps and devices use, not an ad or tracker."),
            "sources": list(clean.sources), "notes": clean.note,
        })

    if allow:
        status, by = "allowed", "let_through"
    elif block:
        status, by = "blocked", ("held" if block["action"] == "quarantine" else "auto" if block["decided_by"] == "auto" else "yours")
    elif listed and (pending or not covered):
        status, by = "blocked", "blocklist"
    elif listed:  # every listed name under the domain was let through
        status, by = "allowed", "let_through"
    elif fast.threat_hits:
        status, by = "blocked", "threat_list"
    else:
        status, by = "allowed", None
    let_through_ids = [allow["id"]] if allow else [
        d["id"] for d in decisions if d["action"] == "allow" and d["domain"] in covered and applies(d)
    ]

    for x in (*looked_up.values(), *devices.values()):
        x["last_seen"] = _iso(x["last_seen"])

    return {
        "name": name,
        "domain": domain,
        "category": info.get("category") or (entry.category if entry else "unknown"),
        "owner": info.get("owner") or (entry.owner if entry else None) or fast.tracker_owner or fast.entity,
        "what_it_does": info.get("what_it_does"),
        "break_risk": info.get("break_risk"),
        "break_note": info.get("break_note"),
        "confidence": info.get("confidence"),
        "sources": sources,
        "explained_by": info.get("explained_by"),
        "checked": bool(row),
        "acr": bool(signals.get("acr")),
        "must_allow": must_allow,
        "status": status,
        "blocked_by": by,
        "reasons": reasons,
        "looked_up": sorted(looked_up.values(), key=lambda x: -x["count"]),
        "devices": sorted(devices.values(), key=lambda x: -x["count"]),
        "hours": hours,
        "actions": {
            # Unblock lets these exact names through (an allow rule beats every block)
            "unblock": (pending if by == "blocklist" and covered else [name]) if status == "blocked" else None,
            # Block again: drop the allow rules, so the list (or your earlier rule) applies once more
            "undo_allow": (let_through_ids or None) if by == "let_through" else None,
            # Block: a rule of this home's own, on the domain as Suggestions shows it
            "block": domain if status == "allowed" and by is None else None,
        },
    }


def list_blocked(
    conn: sqlite3.Connection, user_id: str, blocklist: BlocklistIndex, *, device_id: str | None = None, hours: int = 24
) -> list[dict]:
    """What QuietNet's block list turned away for this home (or one device) lately, grouped by domain. Names the home
    has let through since are left out."""
    since = time.time() - hours * 3600
    sql = (
        "SELECT q.domain, q.qname, q.device_id, v.name AS device_name, COUNT(*) AS n, MAX(q.t) AS last "
        "FROM queries q JOIN devices v ON v.id = q.device_id "
        "WHERE v.user_id = ? AND q.t >= ? AND q.status = 'blocked' AND q.blocked_by = 'blocklist'"
    )
    args: list = [user_id, since]
    if device_id:
        sql += " AND q.device_id = ?"
        args.append(device_id)
    per_device, household = _decision_index(conn, user_id)
    groups: dict[str, dict] = {}
    for r in conn.execute(sql + " GROUP BY q.domain, q.qname, q.device_id", args):
        state, _ = _now(per_device, household, r["device_id"], r["qname"])
        if state == "allowed":
            continue
        g = groups.setdefault(r["domain"], {"domain": r["domain"], "names": [], "count": 0, "last_seen": 0.0, "devices": []})
        if r["qname"] not in g["names"]:
            g["names"].append(r["qname"])
        g["count"] += r["n"]
        g["last_seen"] = max(g["last_seen"], r["last"])
        if all(d["id"] != r["device_id"] for d in g["devices"]):
            g["devices"].append({"id": r["device_id"], "name": r["device_name"]})
    if not groups:
        return []
    marks = ",".join("?" * len(groups))
    info = {
        r["domain"]: dict(r)
        for r in conn.execute(
            f"SELECT domain, category, owner, what_it_does, break_risk FROM domains WHERE domain IN ({marks})", list(groups)
        )
    }
    out = []
    for g in groups.values():
        meta = info.get(g["domain"], {})
        e = blocklist.entry(g["names"][0])
        g.update(
            category=meta.get("category") or (e.category if e else "unknown"),
            owner=meta.get("owner") or (e.owner if e else None),
            what_it_does=meta.get("what_it_does"),
            break_risk=meta.get("break_risk"),
            sources=list(e.sources) if e else [],
            notes=e.notes if e else "",
            last_seen=_iso(g["last_seen"]),
        )
        out.append(g)
    out.sort(key=lambda g: g["last_seen"] or "", reverse=True)
    return out[:500]
