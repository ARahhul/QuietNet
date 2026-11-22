"""Setup: "Stuck? Show QuietNet your screen" (see pahra.screen_help)."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import screen_help
from ..db import get_meta
from ..engine import Engine
from .deps import engine
from .devices import _get as get_device

router = APIRouter(tags=["setup"])


class Turn(BaseModel):
    """One earlier answer in this conversation, as the dashboard showed it."""

    question: str = Field("", max_length=300)
    screen: str = Field("", max_length=300)
    steps: list[str] = Field(default_factory=list, max_length=8)


class ScreenIn(BaseModel):
    image: str = Field(max_length=7_500_000, description="data:image/png|jpeg|webp;base64,...")
    device_id: str | None = None
    isp: str = Field("", max_length=60)
    router: str = Field("", max_length=60)
    question: str = Field("", max_length=300)
    history: list[Turn] = Field(default_factory=list, max_length=8)


@router.post("/setup/screenshot")
async def screenshot_help(body: ScreenIn, e: Engine = Depends(engine)):
    """Read a screenshot the person is stuck on and say what to do next. The picture isn't stored."""
    if not e.explainer.enabled:
        raise HTTPException(503, "AI isn't set up on this server, so it can't read screenshots. Add an AI key to turn it on.")
    try:
        image = screen_help.decode_image(body.image)
    except screen_help.BadImage as ex:
        raise HTTPException(422, str(ex)) from None
    device = get_device(e, body.device_id) if body.device_id else None
    try:
        return await screen_help.read_screen(
            e.explainer,
            e.settings,
            image,
            device=device,
            forwarder_ip=get_meta(e.conn, "forwarder_listen_ip"),
            isp=body.isp,
            router=body.router,
            question=body.question,
            history=[t.model_dump() for t in body.history],
        )
    except screen_help.ReadFailed:
        raise HTTPException(502, "The AI couldn't read that screenshot just now. Try again in a moment.") from None
