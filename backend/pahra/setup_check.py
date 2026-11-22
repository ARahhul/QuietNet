"""Setup: "I've done it, check": is this device's DNS really reaching QuietNet?

Checked on QuietNet's side: AdGuard's query log is read fresh, then we count this device's lookups in the last
10 minutes. A lookup only counts as this device's when it arrives with its ClientID (encrypted DNS) or from the
home's linked public IP (plain DNS on the router), so a green result means the setup is really working.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable

from .screen_help import method_key
from .timeutil import parse_iso, to_iso

log = logging.getLogger("pahra.setup_check")

WINDOW_S = 600
ENCRYPTED = {"dot": "DNS-over-TLS", "doh": "DNS-over-HTTPS", "doq": "DNS-over-QUIC", "dnscrypt": "DNSCrypt"}

TIPS = {
    "plain": [
        "Click Save or Apply on the router, then restart it (or give it a minute to apply).",
        "Turn WiFi off and on on your phone so it picks up the new DNS, then open any website.",
        "If Primary DNS goes back to the old value or is greyed out, your provider has locked it. "
        "Paste a screenshot of the page and the AI will check it.",
    ],
    "dot_router": [
        "Applying restarts the router's internet connection: wait a minute, then open any website.",
        "Copy the TLS hostname from above instead of typing it; one wrong letter and nothing arrives.",
        "Make sure DNS-over-TLS is switched on and QuietNet is the only server in its list.",
    ],
    "dot": [
        "Settings → Private DNS should show the hostname with no 'Couldn't connect' warning. "
        "If it warns, copy the hostname from above again.",
        "Turn WiFi off and on, then open any website.",
    ],
    "doh_apple": [
        "Settings → General → VPN & Device Management must list 'QuietNet DNS'. "
        "If it says 'Profile Downloaded', tap it and Install.",
        "Open Safari and load any website.",
    ],
    "doh": [
        "Make sure you clicked Save in Windows' DNS settings, then turn WiFi off and on.",
        "Chrome and Edge can use their own secure DNS: set it to the same URL, or turn it off.",
    ],
    "adb": [
        "Restart the TV, then open any app.",
        "Check both adb commands ran without errors.",
    ],
    "forwarder": [
        "Keep the laptop on, on the same WiFi, with QuietNet running.",
        "Check the TV's DNS is set to the laptop address shown above, then restart the TV.",
    ],
}


def _ago(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 10:
        return "just now"
    if s < 90:
        return f"{s} seconds ago"
    if s < 90 * 60:
        return f"{round(s / 60)} minutes ago"
    if s < 36 * 3600:
        return f"{round(s / 3600)} hours ago"
    return f"{round(s / 86400)} days ago"


async def check(e, device: dict, home_ip: Callable[[], Awaitable[str | None]]) -> dict:
    """{ok, lookups, seen_at, encrypted, message, tips, link_ip} for one device, from a fresh read of the query log.

    link_ip: for plain DNS on a router that isn't linked to the address this request comes from, that address,
    so the dashboard can offer to link it (only right when the person is on their home WiFi, which they confirm).
    """
    try:
        await e.poller.poll_fresh()
    except Exception as ex:  # AdGuard unreachable: answer from what we already have
        log.warning("fresh poll for a setup check failed: %s", ex)
    now = time.time()
    did, name = device["id"], device["name"]
    recent = e.conn.execute(
        "SELECT COUNT(*) AS n, MAX(t) AS last FROM queries WHERE device_id = ? AND t > ?", (did, now - WINDOW_S)
    ).fetchone()
    if recent["n"]:
        proto = e.conn.execute("SELECT proto FROM queries WHERE device_id = ? ORDER BY t DESC LIMIT 1", (did,)).fetchone()
        how = ENCRYPTED.get((proto["proto"] or "").lower()) if proto else None
        n = recent["n"]
        return {
            "ok": True,
            "lookups": n,
            "seen_at": to_iso(datetime.fromtimestamp(recent["last"], timezone.utc)),
            "encrypted": how is not None,
            "message": f"It's working. QuietNet saw {n} lookup{'' if n == 1 else 's'} from {name} in the last 10 minutes, "
            f"the latest {_ago(now - recent['last'])}" + (f", encrypted ({how})." if how else "."),
            "tips": [],
            "link_ip": None,
        }

    key = method_key(device)
    tips = list(TIPS.get(key, TIPS["plain"]))
    row = e.conn.execute("SELECT last_seen FROM devices WHERE id = ?", (did,)).fetchone()
    last_seen = row["last_seen"] if row else None
    if last_seen:
        message = f"Nothing from {name} in the last 10 minutes. QuietNet last saw it {_ago(now - parse_iso(last_seen).timestamp())}."
    else:
        message = f"Not yet: no lookups from {name} have reached QuietNet."

    # Plain DNS on a router: QuietNet answers and recognises the home only by its linked public IP.
    link_ip = None
    if key == "plain":
        linked, here = device.get("linked_ip"), await home_ip()
        if not linked:
            message = (
                f"QuietNet can't recognise {name} yet: it doesn't know your home's internet address, "
                "so it doesn't answer your router's lookups."
            )
            tips.insert(0, "Link it from your home WiFi: QuietNet then answers lookups coming from your home's address.")
            link_ip = here
        elif here and here != linked:
            tips.insert(
                0,
                f"QuietNet knows your home by the address {linked}, but this page is coming from {here}. "
                "If you're on your home WiFi, your provider has changed your address: link the new one.",
            )
            link_ip = here
    return {"ok": False, "lookups": 0, "seen_at": last_seen, "encrypted": None, "message": message, "tips": tips, "link_ip": link_ip}
