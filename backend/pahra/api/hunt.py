""""Saw an ad?" (see pahra.ad_hunt). Blocking what it finds goes through POST /decisions as usual."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import ad_hunt
from ..engine import Engine
from ..ratelimit import AD_HUNT
from .deps import engine
from .limits import limit
from .devices import _get as get_device

log = logging.getLogger("pahra.api.hunt")
router = APIRouter(tags=["ads"])


class HuntIn(BaseModel):
    where: str = Field(min_length=2, max_length=120, description='e.g. "Hotstar app, banner on the home screen"')
    minutes: int = Field(15, ge=1, le=360, description="how long ago, at most")
    device_id: str | None = None
    note: str = Field("", max_length=200)


@router.post("/ads/hunt", dependencies=[Depends(limit("ad_hunt", AD_HUNT))])
async def hunt(body: HuntIn, e: Engine = Depends(engine)):
    """Find the names that most likely served ads the person just saw. Nothing is blocked here."""
    if not e.explainer.enabled:
        raise HTTPException(503, "AI isn't set up on this server, so it can't look for the ad. Add an AI key to turn it on.")
    device = get_device(e, body.device_id) if body.device_id else None
    try:
        await e.poller.poll_fresh()  # include the last few seconds of lookups
    except Exception as ex:  # AdGuard unreachable: look at what we already have
        log.warning("fresh poll before an ad hunt failed: %s", ex)
    try:
        return await ad_hunt.hunt(
            e.explainer, e.conn, e.user_id, where=body.where.strip(), minutes=body.minutes, device=device, note=body.note
        )
    except ad_hunt.HuntFailed:
        raise HTTPException(502, "The AI couldn't look just now. Try again in a moment.") from None
