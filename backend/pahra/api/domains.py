"""The "View" panel behind every domain the dashboard lists, and what QuietNet's block list turned away."""

from fastapi import APIRouter, Depends, HTTPException, Query

from ..domain_view import domain_detail, list_blocked
from ..engine import Engine
from .deps import engine
from .devices import _get as get_device

router = APIRouter(tags=["domains"])


@router.get("/domains/{name}")
async def view_domain(
    name: str, device_id: str | None = None, hours: int = Query(24, ge=1, le=168), e: Engine = Depends(engine)
):
    """What the domain is, why it is or isn't blocked for this home (or one device), and this home's lookups of it."""
    if device_id:
        get_device(e, device_id)
    try:
        return domain_detail(
            e.conn, e.user_id, name, blocklist=e.blocklist, blocklist_active=e.sync.blocklist_active,
            home_uses_list=e.home_uses_list, lists=e.lists, device_id=device_id, hours=hours,
        )
    except ValueError as ex:
        raise HTTPException(422, str(ex)) from None


@router.get("/blocked/by-list")
async def blocked_by_list(
    device_id: str | None = None, hours: int = Query(24, ge=1, le=168), e: Engine = Depends(engine)
):
    """Names QuietNet's block list turned away for this home lately, grouped by domain (let-through names left out)."""
    if device_id:
        get_device(e, device_id)
    return {
        "active": e.sync.blocklist_active,
        "home_off": not e.home_uses_list,  # switched off in Settings: what's listed below was turned away before that
        "hours": hours,
        "domains": list_blocked(e.conn, e.user_id, e.blocklist, device_id=device_id, hours=hours),
    }
