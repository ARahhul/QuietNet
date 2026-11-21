import ipaddress
import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ..adguard.client import AdGuardError
from .. import forwarder
from ..db import get_meta, new_id, now_iso, row_to_dict
from ..engine import Engine
from ..queries import device_activity, devices_overview
from ..setup_guides import CGNAT_NET, default_setup_method, mobileconfig, setup_guide
from .deps import engine

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


def slug_client_id(e: Engine, name: str, wanted: str | None) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (wanted or name).lower()).strip("-")[:40].strip("-") or "device"
    cid, n = base, 2
    while e.conn.execute("SELECT 1 FROM devices WHERE client_id = ?", (cid,)).fetchone():
        cid = f"{base[:36]}-{n}"
        n += 1
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


@router.post("/devices", status_code=201)
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


@router.get("/devices")
async def list_devices(e: Engine = Depends(engine)):
    return devices_overview(e.conn, e.user_id)


@router.get("/devices/{device_id}")
async def get_device(device_id: str, e: Engine = Depends(engine)):
    _get(e, device_id)
    return next(d for d in devices_overview(e.conn, e.user_id) if d["id"] == device_id)


@router.patch("/devices/{device_id}")
async def patch_device(device_id: str, body: DevicePatch, e: Engine = Depends(engine)):
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
    updates = {k: v for k, v in body.model_dump(exclude={"allow_shared_ipv4"}).items() if v is not None}
    if "linked_ip" in updates and updates["linked_ip"] == "":
        updates["linked_ip"] = None
    if updates:
        sets = ", ".join(f"{k} = ?" for k in updates)
        e.conn.execute(f"UPDATE devices SET {sets} WHERE id = ?", (*updates.values(), dev["id"]))
    synced = await _sync_after_device_change(e)
    return {"device": _get(e, device_id), "synced": synced}


@router.delete("/devices/{device_id}", status_code=204)
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


@router.get("/devices/{device_id}/setup")
async def device_setup(device_id: str, e: Engine = Depends(engine)):
    return setup_guide(e.settings, _get(e, device_id), get_meta(e.conn, "forwarder_listen_ip"))


@router.get("/devices/{device_id}/profile.mobileconfig")
async def device_profile(device_id: str, e: Engine = Depends(engine)):
    dev = _get(e, device_id)
    return Response(
        mobileconfig(e.settings, dev),
        media_type="application/x-apple-aspen-config",
        headers={"Content-Disposition": f'attachment; filename="pahra-{dev["client_id"]}.mobileconfig"'},
    )


@router.get("/devices/{device_id}/activity")
async def activity(device_id: str, e: Engine = Depends(engine)):
    _get(e, device_id)
    return device_activity(e.conn, e.user_id, device_id)
