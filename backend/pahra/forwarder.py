"""Laptop DNS relay for locked TVs (LG webOS, Samsung Tizen) behind CGNAT.

The relay itself runs inside pahra-companion (start_dns_forwarder): it listens on the laptop's LAN IP:53
and forwards each TV's queries over DoH to https://<dns_hostname>/dns-query/<client_id>, so every TV
keeps its own identity even though the home has no linkable public IP.

The backend owns the {TV LAN IP -> client_id} mapping (devices with setup_method='forwarder') and
re-sends the whole set whenever it changes and on startup (the relay stops with the companion).
"""

import logging

from .db import set_meta

log = logging.getLogger("pahra.forwarder")

FORWARDER_TOOLS = {"start_dns_forwarder", "stop_dns_forwarder", "dns_forwarder_status"}


def mappings(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT client_id, lan_ip FROM devices WHERE setup_method = 'forwarder' AND lan_ip IS NOT NULL ORDER BY created_at"
    )
    return [{"ip": r["lan_ip"], "client_id": r["client_id"]} for r in rows]


def available(companion) -> bool:
    return bool(companion and companion.connected and "start_dns_forwarder" in companion.tools)


async def apply(engine) -> dict:
    """Make the running relay match the database. Returns the new state (also kept on engine.forwarder)."""
    comp = engine.companion
    m = mappings(engine.conn)
    res: dict = {}
    if not available(comp):
        state = {"running": False, "devices": len(m),
                 "error": ("the home companion isn't connected, so the relay can't run" if m else None)}
    elif not m:
        if engine.forwarder.get("running"):
            res = await comp.call("stop_dns_forwarder", {})
        state = {"running": False, "devices": 0}
    else:
        res = await comp.call("start_dns_forwarder", {"mappings": m, "dns_hostname": engine.settings.dns_hostname})
        ok = bool(res.get("ok"))
        state = {
            "running": ok,
            "devices": len(m),
            "listen_ip": res.get("listen_ip"),
            "port": res.get("port"),
            "hint": res.get("hint"),
            "error": None if ok else (res.get("error") or "the relay didn't start"),
            "code": None if ok else res.get("code"),
        }
        if ok and res.get("listen_ip"):
            set_meta(engine.conn, "forwarder_listen_ip", res["listen_ip"])
    if state.get("error"):
        log.warning("DNS relay: %s", state["error"])
    engine.forwarder = state
    return state
