"""Follow a home's public address through its router's Dynamic DNS hostname.

Plain DNS on a router is recognised only by the home's public IP (devices.linked_ip), and providers like ACT hand
out a new one whenever the router reconnects. Most routers can keep a Dynamic DNS name (myhome.tplinkdns.com,
a No-IP name, ...) pointed at the current address. QuietNet looks that name up every minute and moves the link
when it changes, so the home stays recognised without anyone opening the dashboard.

Until it does, the home is a stranger to AdGuard. In "rescue" mode (pahra.adguard.sync) strangers can still look
up Dynamic DNS names, which is what lets the router report its new address in the first place.

A hostname can only be set from the home itself (it must point at the address the dashboard is opened from), so
nobody can point QuietNet at someone else's home and read its lookups.
"""

import asyncio
import ipaddress
import logging
import socket
from datetime import datetime, timezone

from .alerts import create_alert
from .setup_guides import CGNAT_NET
from .timeutil import to_iso

log = logging.getLogger("pahra.home_address")


async def resolve_ipv4(host: str, timeout: float = 5.0) -> list[str]:
    """The name's IPv4 addresses, from the server's own resolver (not AdGuard). Replaced in tests."""
    loop = asyncio.get_running_loop()
    infos = await asyncio.wait_for(loop.getaddrinfo(host, None, family=socket.AF_INET, type=socket.SOCK_DGRAM), timeout)
    out: list[str] = []
    for *_, sockaddr in infos:
        if sockaddr[0] not in out:
            out.append(sockaddr[0])
    return out


def usable(ip: str) -> bool:
    """A public address a home can have (not private, not CGNAT, not reserved)."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.is_global and not (a.version == 4 and a in CGNAT_NET)


async def public_addresses(host: str) -> tuple[list[str], str | None]:
    """(usable addresses, problem in plain words or None)."""
    try:
        ips = await resolve_ipv4(host)
    except (OSError, asyncio.TimeoutError):
        return [], f"{host} couldn't be looked up. Check the spelling, and that Dynamic DNS is switched on in the router."
    good = [ip for ip in ips if usable(ip)]
    if not good:
        shown = ", ".join(ips) or "nothing"
        return [], f"{host} points to {shown}, which isn't a public internet address."
    return good, None


async def follow_all(e) -> int:
    """Re-resolve every home's Dynamic DNS name; move links that changed. Returns how many moved."""
    moved = 0
    rows = e.conn.execute("SELECT * FROM devices WHERE ddns_host IS NOT NULL AND ddns_host != ''").fetchall()
    for row in rows:
        dev = dict(row)
        ips, problem = await public_addresses(dev["ddns_host"])
        status = {"host": dev["ddns_host"], "checked_at": to_iso(datetime.now(timezone.utc)), "ip": ips[0] if ips else None, "error": problem}
        e.home_follow[dev["id"]] = status
        if problem:
            log.warning("following %s for %s: %s", dev["ddns_host"], dev["id"], problem)
            continue
        if dev["linked_ip"] in ips:
            continue
        new, old = ips[0], dev["linked_ip"]
        if e.conn.execute("SELECT 1 FROM devices WHERE linked_ip = ? AND id != ?", (new, dev["id"])).fetchone():
            status["error"] = f"{dev['ddns_host']} points to {new}, which another QuietNet device already uses."
            log.warning("following %s for %s: %s is taken", dev["ddns_host"], dev["id"], new)
            continue
        e.conn.execute("UPDATE devices SET linked_ip = ? WHERE id = ?", (new, dev["id"]))
        moved += 1
        log.info("home address of %s moved %s -> %s (via %s)", dev["id"], old, new, dev["ddns_host"])
        create_alert(
            e.conn,
            user_id=dev["user_id"],
            device=dev,
            type_="home_address",
            message=(
                f"Your home's internet address changed{f' from {old}' if old else ''} to {new}. "
                f"QuietNet followed it through {dev['ddns_host']}, so {dev['name']} stays protected."
            ),
            force=True,
        )
    return moved
