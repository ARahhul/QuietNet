"""Block / allow / quarantine decisions. Each row stores the exact AdGuard rule text."""

import sqlite3

from .adguard.rules import build_adguard_rule
from .db import new_id, now_iso, row_to_dict, transaction


class DecisionError(ValueError):
    pass


def _target_client_ids(conn: sqlite3.Connection, user_id: str, device_id: str | None) -> list[str]:
    if device_id:
        row = conn.execute(
            "SELECT client_id FROM devices WHERE id = ? AND user_id = ?", (device_id, user_id)
        ).fetchone()
        if not row:
            raise DecisionError("device not found")
        return [row["client_id"]]
    rows = conn.execute("SELECT client_id FROM devices WHERE user_id = ?", (user_id,)).fetchall()
    if not rows:
        raise DecisionError("no devices yet: add a device before blocking")
    return [r["client_id"] for r in rows]


def active_decision(conn: sqlite3.Connection, domain: str, device_id: str | None, user_id: str | None = None) -> dict | None:
    """The decision in force for one device, or (device_id None) for a household: pass its user_id, since every
    home has its own household decisions."""
    if device_id is None:
        sql = "SELECT * FROM decisions WHERE domain = ? AND device_id IS NULL AND undone_at IS NULL"
        args: tuple = (domain,)
        if user_id is not None:
            sql, args = sql + " AND user_id = ?", (domain, user_id)
        row = conn.execute(sql, args).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM decisions WHERE domain = ? AND device_id = ? AND undone_at IS NULL", (domain, device_id)
        ).fetchone()
    return row_to_dict(row)


def create_decision(
    conn: sqlite3.Connection,
    user_id: str,
    domain: str,
    action: str,
    device_id: str | None = None,
    decided_by: str = "user",
) -> dict:
    """Record a decision, superseding any active one for the same (device scope, domain)."""
    ids = _target_client_ids(conn, user_id, device_id)
    rule = build_adguard_rule(domain, ids, action)
    ts = now_iso()
    did = new_id()
    with transaction(conn):  # the old decision is only superseded if the new one is saved
        if device_id is None:
            conn.execute(
                "UPDATE decisions SET undone_at = ? WHERE user_id = ? AND domain = ? AND device_id IS NULL AND undone_at IS NULL",
                (ts, user_id, domain),
            )
        else:
            conn.execute(
                "UPDATE decisions SET undone_at = ? WHERE domain = ? AND device_id = ? AND undone_at IS NULL",
                (ts, domain, device_id),
            )
        conn.execute(
            "INSERT INTO decisions(id, user_id, device_id, domain, action, adguard_rule, decided_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (did, user_id, device_id, domain, action, rule, decided_by, ts),
        )
    return get_decision(conn, did)  # type: ignore[return-value]


def get_decision(conn: sqlite3.Connection, decision_id: str) -> dict | None:
    return row_to_dict(conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone())


def undo_decision(conn: sqlite3.Connection, user_id: str, decision_id: str) -> dict:
    d = get_decision(conn, decision_id)
    if not d or d["user_id"] != user_id:
        raise DecisionError("decision not found")
    if d["undone_at"] is None:
        conn.execute("UPDATE decisions SET undone_at = ? WHERE id = ?", (now_iso(), decision_id))
    return get_decision(conn, decision_id)  # type: ignore[return-value]


def reinstate(conn: sqlite3.Connection, user_id: str, decision_id: str) -> dict | None:
    """Put back a decision another one replaced (undoing an unblock restores the block it lifted), unless a newer
    decision now covers the same domain for the same device or household."""
    d = get_decision(conn, decision_id)
    if not d or d["user_id"] != user_id or d["undone_at"] is None:
        return None
    if active_decision(conn, d["domain"], d["device_id"], user_id):
        return None
    conn.execute("UPDATE decisions SET undone_at = NULL WHERE id = ?", (decision_id,))
    return get_decision(conn, decision_id)


def release_quarantines(conn: sqlite3.Connection, domain: str) -> int:
    cur = conn.execute(
        "UPDATE decisions SET undone_at = ? WHERE domain = ? AND action = 'quarantine' AND undone_at IS NULL",
        (now_iso(), domain),
    )
    return cur.rowcount
