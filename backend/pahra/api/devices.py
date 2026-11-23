import base64
import ipaddress
import re
import secrets
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ..adguard.client import AdGuardError
from .. import forwarder, home_address, setup_check
from ..adguard.rules import validate_domain
from ..db import get_meta, new_id, now_iso, row_to_dict, transaction
from ..engine import Engine
from ..queries import device_activity, devices_overview
from ..setup_guides import CGNAT_NET, default_setup_method, mobileconfig, setup_guide
from ..ratelimit import DEVICE_CHANGE
from .deps import engine
from .limits import limit
from .misc import home_ip

router = APIRouter(tags=["devices"])

DeviceType = Literal["tv", "phone", "laptop", "router", "other"]
SetupMethod = Literal["dot", "doh", "ipv6", "ipv4_demo", "adb"]


class DeviceIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    type: DeviceType
    brand: str | None = Field(default=None, max_length=30)
    setup_method: SetupMethod | None = None
    client_id: str | None = None


class DevicePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    brand: str | None = None
    setup_method: SetupMethod | None = None
    linked_ip: str | None = None
    allow_shared_ipv4: bool = False  # hackathon demo only (CGNAT)
    # Router's Dynamic DNS name to follow ("" stops following). Set from the home: it must point here.
    ddns_host: str | None = Field(default=None, max_length=253)


def slug_client_id(e: Engine, name: str, wanted: str | None) -> str:
    """A device's address (its AdGuard ClientID): a readable part from its name, then 8 random characters.

    The address is all it takes to use QuietNet's resolver as that device (its lookups land in the home's log and new
    domains go to the AI), so it mustn't be guessable from the name: `living-room-tv-k3x7q2ma`, 40 random bits.
    """
    base = re.sub(r"[^a-z0-9]+", "-", (wanted or name).lower()).strip("-")[:30].strip("-") or "device"
    while True:
        cid = f"{base}-{base64.b32encode(secrets.token_bytes(5)).decode().lower()}"
        if not e.conn.execute("SELECT 1 FROM devices WHERE client_id = ?", (cid,)).fetchone():
            return cid


def _get(e: Engine, device_id: str) -> dict:
    d = row_to_dict(
        e.conn.execute("SELECT * FROM devices WHERE id = ? AND user_id = ?", (device_id, e.user_id)).fetchone()
    )
    if not d:
        raise HTTPException(404, "device not found")
    return d


async def _sync_after_device_change(e: Engine) -> bool:
    try:
        await e.sync.sync_all()
        e.poller.poke()
        return True
    except AdGuardError:
        return False


@router.post("/devices", status_code=201, dependencies=[Depends(limit("device_change", DEVICE_CHANGE))])
async def create_device(body: DeviceIn, e: Engine = Depends(engine)):
    cid = slug_client_id(e, body.name, body.client_id)
    did = new_id()
    method = body.setup_method or default_setup_method(body.type, body.brand)
    e.conn.execute(
        "INSERT INTO devices(id, user_id, client_id, name, type, brand, setup_method, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (did, e.user_id, cid, body.name.strip(), body.type, body.brand, method, now_iso()),
    )
    synced = await _sync_after_device_change(e)
    dev = _get(e, did)
    return {"device": dev, "setup": setup_guide(e.settings, dev), "synced": synced}


def _with_follow(e: Engine, d: dict) -> dict:
    return d | {"ddns_status": e.home_follow.get(d["id"]) if d.get("ddns_host") else None}


@router.get("/devices")
async def list_devices(e: Engine = Depends(engine)):
    return [_with_follow(e, d) for d in devices_overview(e.conn, e.user_id, e.blocklisted)]


@router.get("/devices/{device_id}")
async def get_device(device_id: str, e: Engine = Depends(engine)):
    _get(e, device_id)
    return _with_follow(e, next(d for d in devices_overview(e.conn, e.user_id, e.blocklisted) if d["id"] == device_id))


async def _follow_from_here(e: Engine, request: Request, raw: str) -> dict:
    """Columns to set for a Dynamic DNS name: it must point at the address this request comes from (the home)."""
    try:
        host = validate_domain(raw.strip().lower().removeprefix("http://").removeprefix("https://").split("/")[0])
    except ValueError:
        raise HTTPException(422, "That doesn't look like a hostname. Copy it from the router, e.g. myhome.tplinkdns.com.")
    ips, problem = await home_address.public_addresses(host)
    if problem:
        raise HTTPException(422, problem)
    here = await home_ip(request)
    if here not in ips:
        raise HTTPException(
            409,
            f"{host} points to {', '.join(ips)}, but this page is coming from {here or 'an unknown address'}. "
            "Open this page on your home WiFi (not mobile data), and give the router a minute to update the name.",
        )
    return {"ddns_host": host, "linked_ip": here}


@router.patch("/devices/{device_id}", dependencies=[Depends(limit("device_change", DEVICE_CHANGE))])
async def patch_device(device_id: str, body: DevicePatch, request: Request, e: Engine = Depends(engine)):
    dev = _get(e, device_id)
    if body.linked_ip is not None and body.linked_ip != "":
        try:
            ip = ipaddress.ip_address(body.linked_ip)
        except ValueError:
            raise HTTPException(422, "linked_ip is not an IP address")
        if ip.version == 4 and ip in CGNAT_NET:
            raise HTTPException(422, "that address is inside CGNAT space (100.64.0.0/10); use IPv6 or a hotspot")
        if ip.is_private or ip.is_loopback:
            raise HTTPException(422, "link your home's PUBLIC IP, not a LAN address")
        if ip.version == 4 and not body.allow_shared_ipv4:
            raise HTTPException(
                409,
                "IPv4 linking is unsafe if your ISP uses CGNAT (others on the same IP would share this profile). "
                "Prefer IPv6; set allow_shared_ipv4=true only for the demo.",
            )
    updates = {k: v for k, v in body.model_dump(exclude={"allow_shared_ipv4", "ddns_host"}).items() if v is not None}
    if "linked_ip" in updates and updates["linked_ip"] == "":
        updates["linked_ip"] = None
    if body.ddns_host is not None:
        if body.ddns_host.strip():
            updates |= await _follow_from_here(e, request, body.ddns_host)
            e.home_follow[dev["id"]] = {"host": updates["ddns_host"], "checked_at": now_iso(), "ip": updates["linked_ip"], "error": None}
        else:
            updates["ddns_host"] = None
            e.home_follow.pop(dev["id"], None)
    if updates.get("linked_ip") and e.conn.execute(
        "SELECT 1 FROM devices WHERE linked_ip = ? AND id != ?", (updates["linked_ip"], dev["id"])
    ).fetchone():
        raise HTTPException(
            409,
            f"{updates['linked_ip']} is already linked to another QuietNet device. One internet address can only belong "
            "to one device: if it's another home on the same shared address, use encrypted DNS on each device instead.",
        )
    if updates:
        sets = ", ".join(f"{k} = ?" for k in updates)
        e.conn.execute(f"UPDATE devices SET {sets} WHERE id = ?", (*updates.values(), dev["id"]))
    synced = await _sync_after_device_change(e)
    return {"device": _with_follow(e, _get(e, device_id)), "synced": synced}


@router.delete("/devices/{device_id}", status_code=204, dependencies=[Depends(limit("device_change", DEVICE_CHANGE))])
async def delete_device(device_id: str, e: Engine = Depends(engine)):
    dev = _get(e, device_id)
    e.conn.execute("DELETE FROM devices WHERE id = ?", (dev["id"],))
    await e.sync.remove_client(dev["client_id"])
    try:
        await e.sync.sync_all()
    except AdGuardError:
        pass
    if dev["setup_method"] == "forwarder":
        await forwarder.apply(e)  # stop relaying for that TV
    return Response(status_code=204)


@router.post("/devices/{device_id}/client-id", dependencies=[Depends(limit("device_change", DEVICE_CHANGE))])
async def change_client_id(device_id: str, e: Engine = Depends(engine)):
    """Give a device a new address, e.g. one set up before addresses had a random part, or one someone else may
    know. The old address stops working straight away: the device needs the new one entered (the setup steps)."""
    dev = _get(e, device_id)
    old = dev["client_id"]
    with transaction(e.conn):
        e.conn.execute("UPDATE devices SET client_id = ? WHERE id = ?", (slug_client_id(e, dev["name"], None), dev["id"]))
        e.sync.refresh_rules()  # its own rules and its home's household rules now name the new address
    await e.sync.remove_client(old)
    synced = await _sync_after_device_change(e)  # adds the new client and drops the old one from the allow-list
    if dev["setup_method"] == "forwarder":
        await forwarder.apply(e)  # the laptop relay forwards that TV under its new address
    dev = _get(e, device_id)
    return {
        "device": _with_follow(e, dev),
        "setup": setup_guide(e.settings, dev, get_meta(e.conn, "forwarder_listen_ip")),
        "synced": synced,
    }


@router.get("/devices/{device_id}/setup")
async def device_setup(device_id: str, e: Engine = Depends(engine)):
    return setup_guide(e.settings, _get(e, device_id), get_meta(e.conn, "forwarder_listen_ip"))


@router.post("/devices/{device_id}/check")
async def device_check(device_id: str, request: Request, e: Engine = Depends(engine)):
    """"I've done it": are this device's lookups reaching QuietNet? Reads AdGuard's query log fresh first."""
    return await setup_check.check(e, _get(e, device_id), lambda: home_ip(request))


@router.get("/devices/{device_id}/profile.mobileconfig")
async def device_profile(device_id: str, e: Engine = Depends(engine)):
    dev = _get(e, device_id)
    return Response(
        mobileconfig(e.settings, dev),
        media_type="application/x-apple-aspen-config",
        headers={"Content-Disposition": f'attachment; filename="quietnet-{dev["client_id"]}.mobileconfig"'},
    )


@router.get("/devices/{device_id}/activity")
async def activity(device_id: str, e: Engine = Depends(engine)):
    _get(e, device_id)
    return device_activity(e.conn, e.user_id, device_id, e.blocklisted)
