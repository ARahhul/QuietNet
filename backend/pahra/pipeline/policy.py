"""Stage 6: per-device policy (must-allow, malware, quarantine release, auto-mode, alerts)."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from ..alerts import create_alert
from ..db import now_iso, row_to_dict
from ..decisions import active_decision, create_decision
from ..knowledge import is_must_allow
from ..timeutil import to_iso

AUTO_BLOCK_CATEGORIES = {"ads", "tracking", "telemetry"}
QUIET_RETURN_S = 3 * 24 * 3600
DGA_ALERT_COUNT = 5


def _device(conn: sqlite3.Connection, device_id: str) -> dict | None:
    return row_to_dict(conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone())


def _domain(conn: sqlite3.Connection, domain: str) -> dict | None:
    return row_to_dict(conn.execute("SELECT * FROM domains WHERE domain = ?", (domain,)).fetchone(), ("sources", "signals"))


def apply_policy(
    conn: sqlite3.Connection,
    device_id: str,
    domain: str,
    qnames: tuple[str, ...] = (),
    max_gap_s: float = 0.0,
    blocklisted: bool = False,
) -> bool:
    """Returns True when a decision changed (caller re-syncs AdGuard).

    `blocklisted`: AdGuard already blocks the domain for every home through QuietNet's block list, so no rule of
    this home's own is needed for it.
    """
    dev = _device(conn, device_id)
    dom = _domain(conn, domain)
    if not dev or not dom:
        return False
    user = conn.execute("SELECT * FROM users WHERE id = ?", (dev["user_id"],)).fetchone()
    blocklisted = blocklisted and bool(user["shared_blocklist"])  # the home may have switched the list off
    sig = dom["signals"] or {}
    mine = active_decision(conn, domain, device_id)
    household = active_decision(conn, domain, None, dev["user_id"])
    # The person let it through (e.g. "Site not working?"): nothing automatic blocks it again.
    let_through = any(d and d["action"] == "allow" for d in (mine, household))
    changed = False

    # 1. must-allow: never auto-block, release any quarantine
    if is_must_allow(domain, dev["type"], qnames):
        if mine and mine["action"] == "quarantine":
            conn.execute("UPDATE decisions SET undone_at = ? WHERE id = ?", (now_iso(), mine["id"]))
            changed = True
        return changed

    # 2. known malware / phishing: always block for this device + alert
    if dom["category"] == "malware":
        if let_through:
            create_alert(
                conn, user_id=dev["user_id"], device=dev, type_="malware", domain=domain,
                message=f"Your {dev['name']} reached {domain}, which is on a malware list. You unblocked it, so QuietNet "
                "let it through. Block it again from Live or Suggestions if that wasn't on purpose.",
            )
            return changed
        if not (mine and mine["action"] == "block") and not (household and household["action"] == "block"):
            create_decision(conn, dev["user_id"], domain, "block", device_id=device_id, decided_by="auto")
            changed = True
        create_alert(
            conn, user_id=dev["user_id"], device=dev, type_="malware", domain=domain,
            message=f"Your {dev['name']} tried to reach a known malicious site ({domain}). QuietNet blocked it.",
        )
        return changed

    # 3. quarantine release once judged safe
    if mine and mine["action"] == "quarantine" and dom["category"] == "needed":
        conn.execute("UPDATE decisions SET undone_at = ? WHERE id = ?", (now_iso(), mine["id"]))
        mine, changed = None, True

    # 4. auto-mode: HIGH confidence ads/trackers get blocked without asking (then notify), but only when
    #    blocking them is low risk; anything that might break an app or site waits in Suggestions for the person.
    if (
        user["auto_mode"]
        and dom["confidence"] == "high"
        and dom["category"] in AUTO_BLOCK_CATEGORIES
        and dom["break_risk"] == "low"
        and (mine is None or mine["action"] == "quarantine")
        and household is None
        and not blocklisted
    ):
        create_decision(conn, dev["user_id"], domain, "block", device_id=device_id, decided_by="auto")
        changed = True
        create_alert(
            conn, user_id=dev["user_id"], device=dev, type_="new_tracker", domain=domain,
            message=f"Auto-mode blocked {dom['owner'] or domain} ({dom['category']}) on your {dev['name']}.",
        )

    # 5. TV viewing-data (ACR) trackers: explain and give the fix-the-source guide
    if sig.get("acr") and dev["type"] == "tv":
        who = dom["owner"] or domain
        brand = dev.get("brand") or "smart"
        if blocklisted and not let_through:
            back = f" after {int(max_gap_s // 86400)} quiet days" if max_gap_s >= QUIET_RETURN_S else ""
            create_alert(
                conn, user_id=dev["user_id"], device=dev, type_="acr_tracker", domain=domain, acr=True,
                force=bool(back),
                message=f"Your {brand} TV is trying to share what you watch with {who}{back}. QuietNet blocks it; "
                "switch it off on the TV too so it stops trying.",
            )
        elif max_gap_s >= QUIET_RETURN_S:
            create_alert(
                conn, user_id=dev["user_id"], device=dev, type_="acr_tracker", domain=domain, acr=True, force=True,
                message=f"{who} is back on your {brand} TV after {int(max_gap_s // 86400)} quiet days. "
                "The setting may have switched back on after an update.",
            )
        else:
            create_alert(
                conn, user_id=dev["user_id"], device=dev, type_="acr_tracker", domain=domain, acr=True,
                message=f"Your {brand} TV is sharing what you watch with {who}.",
            )
    return changed


def check_odd_behaviour(conn: sqlite3.Connection, device_id: str) -> None:
    """Many random-looking domains from one device in an hour -> possible infection."""
    dev = _device(conn, device_id)
    if not dev:
        return
    since = to_iso(datetime.now(timezone.utc) - timedelta(hours=1))
    rows = conn.execute(
        "SELECT d.signals FROM events e JOIN domains d ON d.domain = e.domain WHERE e.device_id = ? AND e.last_ts > ?",
        (device_id, since),
    ).fetchall()
    n = sum(1 for r in rows if (json.loads(r["signals"] or "{}").get("dga") or 0) >= 0.6)
    if n >= DGA_ALERT_COUNT:
        create_alert(
            conn, user_id=dev["user_id"], device=dev, type_="odd_behaviour",
            message=f"Your {dev['name']} contacted {n} random-looking domains in the last hour. It may be infected.",
        )
