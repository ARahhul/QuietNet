""""Site not working?" (see pahra.site_fix): find what QuietNet blocked that a site needs, then let it through."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import screen_help, site_fix
from ..decisions import DecisionError
from ..engine import Engine
from ..ratelimit import SITE_FIX
from .cards import _push
from .deps import engine
from .devices import _get as get_device
from .limits import limit

log = logging.getLogger("pahra.api.sites")
router = APIRouter(tags=["sites"])


class FixIn(BaseModel):
    what: str = Field("", max_length=160, description="e.g. \"IRCTC payment page won't load\"")
    image: str | None = Field(None, max_length=7_500_000, description="a screenshot of it: data:image/png|jpeg|webp;base64,...")
    minutes: int = Field(15, ge=1, le=360, description="how long ago, at most")
    device_id: str | None = None


class UnblockIn(BaseModel):
    names: list[str] = Field(min_length=1, max_length=20)
    device_id: str | None = None  # None = whole household


@router.post("/sites/fix", dependencies=[Depends(limit("site_fix", SITE_FIX))])
async def fix(body: FixIn, e: Engine = Depends(engine)):
    """What QuietNet blocked around then that a site or app needs. Nothing is unblocked here."""
    what = body.what.strip()
    if len(what) < 2 and not body.image:
        raise HTTPException(422, "Say which site or app isn't working, or add a screenshot of it.")
    ai = e.explainer if e.explainer.enabled else None
    image = None
    if body.image:
        try:
            image = screen_help.decode_image(body.image)
        except screen_help.BadImage as ex:
            raise HTTPException(422, str(ex)) from None
    device = get_device(e, body.device_id) if body.device_id else None
    try:
        await e.poller.poll_fresh()  # include the last few seconds of lookups
    except Exception as ex:  # AdGuard unreachable: look at what we already have
        log.warning("fresh poll before a site check failed: %s", ex)
    try:
        return await site_fix.find(ai, e.conn, e.user_id, what=what, minutes=body.minutes, device=device, image=image)
    except site_fix.NeedsAI:
        raise HTTPException(503, "AI isn't set up on this server, so it can't read screenshots. Type the site's name instead.") from None


@router.post("/sites/unblock", status_code=201)
async def unblock(body: UnblockIn, e: Engine = Depends(engine)):
    """Let these names through for one device or the household, whatever blocked them (one of QuietNet's rules or
    a block list). Undo with POST /decisions/undo-bulk {ids: the decisions, restore: replaced}."""
    if body.device_id:
        get_device(e, body.device_id)
    try:
        out = site_fix.unblock(e.conn, e.user_id, body.names, body.device_id)
    except (DecisionError, ValueError) as ex:
        raise HTTPException(422, str(ex)) from None
    return out | {"count": len(out["decisions"]), "synced": await _push(e)}
