"""Security / privacy alerts with fix-the-source steps (SPEC §7)."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from .db import new_id, now_iso
from .knowledge import fix_guide
from .timeutil import to_iso

DEDUPE_HOURS = 24


def create_alert(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    device: dict,
    type_: str,
    message: str,
    domain: str | None = None,
    acr: bool = False,
    force: bool = False,
) -> str | None:
    """Insert unless the same (device, domain, type) alert fired in the last 24 h."""
    if not force:
        since = to_iso(datetime.now(timezone.utc) - timedelta(hours=DEDUPE_HOURS))
        dup = conn.execute(
            "SELECT 1 FROM alerts WHERE device_id = ? AND type = ? AND IFNULL(domain, '') = IFNULL(?, '') AND created_at > ?",
            (device["id"], type_, domain, since),
        ).fetchone()
        if dup:
            return None
    guide = fix_guide(type_=type_, acr=acr, brand=device.get("brand"))
    steps = guide["steps"] if guide else []
    aid = new_id()
    conn.execute(
        "INSERT INTO alerts(id, user_id, device_id, domain, type, message, fix_steps, fix_source, read, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
        (
            aid, user_id, device["id"], domain, type_, message,
            json.dumps({"title": guide["title"], "steps": steps, "checked": guide.get("checked", False)} if guide else {}),
            "guide" if guide else None, now_iso(),
        ),
    )
    return aid
