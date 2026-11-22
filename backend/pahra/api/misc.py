from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import httpx

from ..adguard.client import AdGuardError
from ..db import rows_to_dicts
from ..engine import Engine
from ..queries import weekly_report
from ..setup_guides import is_cgnat
from .deps import engine

router = APIRouter(tags=["alerts, report, settings, system"])


@router.get("/alerts")
async def alerts(unread_only: bool = False, e: Engine = Depends(engine)):
    sql = (
        "SELECT a.*, v.name AS device_name FROM alerts a LEFT JOIN devices v ON v.id = a.device_id WHERE a.user_id = ?"
        + (" AND a.read = 0" if unread_only else "")
        + " ORDER BY a.created_at DESC LIMIT 200"
    )
    return rows_to_dicts(e.conn.execute(sql, (e.user_id,)), ("fix_steps",))


@router.post("/alerts/{alert_id}/read")
async def mark_read(alert_id: str, e: Engine = Depends(engine)):
    n = e.conn.execute("UPDATE alerts SET read = 1 WHERE id = ? AND user_id = ?", (alert_id, e.user_id)).rowcount
    if not n:
        raise HTTPException(404, "alert not found")
    return {"ok": True}


@router.get("/report/weekly")
async def report(e: Engine = Depends(engine)):
    return weekly_report(e.conn, e.user_id)


class SettingsPatch(BaseModel):
    auto_mode: bool | None = None


def _settings(e: Engine) -> dict:
    u = e.conn.execute("SELECT * FROM users WHERE id = ?", (e.user_id,)).fetchone()
    return {
        "email": u["email"],
        "auto_mode": bool(u["auto_mode"]),
        "dns_hostname": e.settings.dns_hostname,
        "vm_ip": e.settings.vm_ip,
    }


@router.get("/settings")
async def get_settings(e: Engine = Depends(engine)):
    return _settings(e)


@router.patch("/settings")
async def patch_settings(body: SettingsPatch, e: Engine = Depends(engine)):
    if body.auto_mode is not None:
        e.conn.execute("UPDATE users SET auto_mode = ? WHERE id = ?", (int(body.auto_mode), e.user_id))
    return _settings(e)


@router.get("/health")
async def health(e: Engine = Depends(engine)):
    try:
        st = await e.ag.status()
        ag = {"ok": True, "version": st.get("version"), "protection_enabled": st.get("protection_enabled")}
    except AdGuardError as ex:
        ag = {"ok": False, "error": str(ex)}
    comp = e.companion
    return {
        "adguard": ag,
        "poller": {"last_poll": e.poller.last_poll, "error": e.poller.last_error, "interval_s": e.poller.interval_s},
        "sync": {"dirty": e.sync.dirty, "last_ok": e.sync.last_ok, "error": e.sync.last_error},
        "lists": e.lists.stats(),
        "llm": {"enabled": e.explainer.enabled, "model": e.explainer.model, "error": e.explainer.last_error},
        "domains_explained": e.conn.execute("SELECT COUNT(*) FROM domains").fetchone()[0],
        "companion": comp.status() if comp else {"enabled": False},
        "forwarder": e.forwarder,
    }


@router.post("/poll-now")
async def poll_now(e: Engine = Depends(engine)):
    e.poller.poke()
    return {"ok": True}


async def public_ip() -> str | None:
    """The laptop is at home in the hybrid setup, so its public IP is the home's public IP."""
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get("https://1.1.1.1/cdn-cgi/trace")
        for line in r.text.splitlines():
            if line.startswith("ip="):
                return line[3:]
    except httpx.HTTPError:
        return None
    return None


class CgnatIn(BaseModel):
    router_wan_ip: str


@router.get("/network/public-ip")
async def get_public_ip():
    return {"public_ip": await public_ip()}


@router.post("/network/cgnat")
async def cgnat(body: CgnatIn):
    pub = await public_ip()
    verdict = is_cgnat(body.router_wan_ip, pub)
    return {
        "public_ip": pub,
        "router_wan_ip": body.router_wan_ip,
        "cgnat": verdict,
        "advice": (
            "Behind CGNAT: don't link IPv4. Use IPv6 linking, encrypted DNS per device, or a hotspot for the demo."
            if verdict
            else "Dedicated public IP: IPv4 linking on port 53 is OK."
            if verdict is False
            else "Couldn't tell. Check the WAN IP on your router's status page."
        ),
    }
