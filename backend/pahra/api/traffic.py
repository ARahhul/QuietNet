from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Query

from ..engine import Engine
from ..timeutil import parse_iso
from ..traffic import analytics, live_queries, network_clients
from .deps import engine

router = APIRouter(tags=["live lookups & analytics"])

LIVE_STALE_S = 3  # while someone watches the live view, poll AdGuard at least this often


def _nudge(e: Engine) -> None:
    last = e.poller.last_poll
    if not last or (datetime.now(timezone.utc) - parse_iso(last)).total_seconds() >= LIVE_STALE_S:
        e.poller.poke()


@router.get("/live/queries")
async def get_live_queries(
    device_id: str | None = None,
    ip: str | None = None,
    status: Literal["all", "blocked", "allowed"] = "all",
    q: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=150, ge=1, le=500),
    e: Engine = Depends(engine),
):
    """Newest lookups first. Calling it also asks the poller to fetch fresh entries from AdGuard."""
    _nudge(e)
    rows = live_queries(e.conn, e.user_id, device_id=device_id, client_ip=ip, status=status, search=q, limit=limit)
    return {"queries": rows, "last_poll": e.poller.last_poll, "poll_error": e.poller.last_error}


@router.get("/live/clients")
async def get_live_clients(minutes: int = Query(default=60, ge=1, le=1440), e: Engine = Depends(engine)):
    """Every device and the addresses its lookups came from recently."""
    return network_clients(e.conn, e.user_id, minutes)


@router.get("/report/analytics")
async def get_analytics(
    hours: int = Query(default=24, ge=1, le=24),
    device_id: str | None = None,
    e: Engine = Depends(engine),
):
    """Lookups over time, per-device activity, record types, resolvers and top lists for the last `hours`."""
    return analytics(e.conn, e.user_id, min(hours, e.settings.query_log_hours), device_id)
