"""Parental controls (pahra.parental): the household's rules, kid profiles, and each device's own rules.

Every change is pushed to AdGuard straight away and answers with the whole overview, so the page can redraw from it.
"""

import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import parental
from ..engine import Engine
from ..ratelimit import DEVICE_CHANGE
from .deps import engine
from .devices import _get as get_device
from .devices import _sync_after_device_change
from .limits import limit

router = APIRouter(tags=["parental"])
change = [Depends(limit("device_change", DEVICE_CHANGE))]

MAX_KIDS = 20
_SERVICE_ID_RE = re.compile(r"^[a-z0-9_.-]{1,40}$")


class Bedtime(BaseModel):
    nights: list[int] = Field(max_length=7, description="0 = Sunday ... 6 = Saturday: the nights bedtime starts on")
    start: str = Field(description='"22:00"')
    end: str = Field(description='"07:00" (before start: the next morning)')


class ProfileIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)


class ProfilePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=40)
    adult: bool | None = None
    safe_search: bool | None = None
    apps: list[str] | None = Field(default=None, max_length=300)
    bedtime_apps: list[str] | None = Field(default=None, max_length=300)
    bedtime_internet_off: bool | None = None
    bedtime: Bedtime | None = None  # send null to remove the bedtime


class DevicePatch(BaseModel):
    profile_id: str | None = None  # the kid profile (null: none)
    skip_household: bool | None = None  # a parent's device: the household's rules don't apply
    own_rules: bool | None = None  # give the device rules of its own (false removes them)


def _profile_out(p: dict, local: datetime) -> dict:
    return {
        k: p[k] for k in ("id", "kind", "name", "adult", "safe_search", "apps", "bedtime_apps", "bedtime_internet_off", "bedtime")
    } | {"bedtime_now": parental.in_bedtime(p["bedtime"], local)}


def overview(e: Engine) -> dict:
    tz = parental.time_zone(e.conn, e.user_id)
    now = datetime.now(timezone.utc)
    local = now.astimezone(parental.zone(tz))
    household = parental.household(e.conn, e.user_id)
    kids = parental.kid_profiles(e.conn, e.user_id)
    effs = parental.all_effective(e.conn, now, e.user_id)
    devices = []
    for v in sorted(effs.values(), key=lambda v: v["device"]["name"].lower()):
        d, eff = v["device"], v["effective"]
        own = parental.own_profile(e.conn, d["id"])
        devices.append(
            {
                "id": d["id"],
                "name": d["name"],
                "type": d["type"],
                "profile_id": d["parental_profile_id"],
                "skip_household": bool(d["skip_household"]),
                "own": _profile_out(own, local) if own else None,
                "now": eff.summary(),
            }
        )
    return {
        "time_zone": tz,
        "household": _profile_out(household, local),
        "kids": [
            _profile_out(k, local) | {"devices": [d["id"] for d in devices if d["profile_id"] == k["id"]]} for k in kids
        ],
        "devices": devices,
        "essentials": list(parental.essentials()),
    }


async def _changed(e: Engine) -> dict:
    return overview(e) | {"synced": await _sync_after_device_change(e)}


def _profile(e: Engine, profile_id: str) -> dict:
    p = parental.get_profile(e.conn, e.user_id, profile_id)
    if not p:
        raise HTTPException(404, "profile not found")
    return p


@router.get("/parental")
async def get_parental(e: Engine = Depends(engine)):
    return overview(e)


@router.get("/parental/services")
async def services(e: Engine = Depends(engine)):
    """The apps and sites parental controls can block (AdGuard's blocked-service catalog)."""
    return [{k: s[k] for k in ("id", "name", "group", "icon_svg")} for s in await e.sync.service_catalog()]


@router.post("/parental/profiles", status_code=201, dependencies=change)
async def create_kid(body: ProfileIn, e: Engine = Depends(engine)):
    if len(parental.kid_profiles(e.conn, e.user_id)) >= MAX_KIDS:
        raise HTTPException(422, f"A home can have up to {MAX_KIDS} kid profiles.")
    p = parental.create_profile(e.conn, e.user_id, "kid", body.name.strip())
    return overview(e) | {"created": p["id"]}


async def _check_apps(e: Engine, ids: list[str]) -> list[str]:
    known = {s["id"] for s in await e.sync.service_catalog()}
    bad = [i for i in ids if not _SERVICE_ID_RE.match(i) or (known and i not in known)]
    if bad:
        raise HTTPException(422, f"Unknown app: {', '.join(bad[:3])}")
    return sorted(set(ids))


@router.patch("/parental/profiles/{profile_id}", dependencies=change)
async def patch_profile(profile_id: str, body: ProfilePatch, e: Engine = Depends(engine)):
    p = _profile(e, profile_id)
    changes = body.model_dump(exclude_unset=True, exclude={"bedtime"})
    changes = {k: v for k, v in changes.items() if v is not None}
    if "name" in changes:
        if p["kind"] != "kid":
            raise HTTPException(422, "Only kid profiles have a name to change.")
        changes["name"] = changes["name"].strip()
    for k in ("apps", "bedtime_apps"):
        if k in changes:
            changes[k] = await _check_apps(e, changes[k])
    if "bedtime" in body.model_fields_set:
        try:
            changes["bedtime"] = parental.validate_bedtime(body.bedtime.model_dump() if body.bedtime else None)
        except ValueError as ex:
            raise HTTPException(422, str(ex)) from None
    parental.update_profile(e.conn, p["id"], changes)
    return await _changed(e)


@router.delete("/parental/profiles/{profile_id}", dependencies=change)
async def delete_kid(profile_id: str, e: Engine = Depends(engine)):
    p = _profile(e, profile_id)
    if p["kind"] != "kid":
        raise HTTPException(422, "Only kid profiles can be deleted.")
    e.conn.execute("UPDATE devices SET parental_profile_id = NULL WHERE parental_profile_id = ?", (p["id"],))
    e.conn.execute("DELETE FROM parental_profiles WHERE id = ?", (p["id"],))
    return await _changed(e)


@router.patch("/parental/devices/{device_id}", dependencies=change)
async def patch_device(device_id: str, body: DevicePatch, e: Engine = Depends(engine)):
    dev = get_device(e, device_id)
    if "profile_id" in body.model_fields_set:
        if body.profile_id and _profile(e, body.profile_id)["kind"] != "kid":
            raise HTTPException(422, "A device can only join a kid profile.")
        e.conn.execute("UPDATE devices SET parental_profile_id = ? WHERE id = ?", (body.profile_id or None, dev["id"]))
    if body.skip_household is not None:
        e.conn.execute("UPDATE devices SET skip_household = ? WHERE id = ?", (int(body.skip_household), dev["id"]))
    if body.own_rules is True and not parental.own_profile(e.conn, dev["id"]):
        parental.create_profile(e.conn, e.user_id, "device", dev["name"], dev["id"])
    elif body.own_rules is False:
        e.conn.execute("DELETE FROM parental_profiles WHERE device_id = ? AND kind = 'device'", (dev["id"],))
    return await _changed(e)
