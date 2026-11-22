"""Setup → Home WiFi: provider and router guides, and which provider this connection is from."""

from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from .. import providers
from ..engine import Engine
from .deps import engine
from .misc import home_ip

router = APIRouter(tags=["setup"])


@router.get("/providers")
async def provider_catalog():
    """Hand-checked guides for the common providers and router brands."""
    return providers.catalog()


@router.get("/network/provider")
async def network_provider(request: Request, e: Engine = Depends(engine)):
    """The provider of the connection the dashboard is opened from (the home's, when on home WiFi)."""
    if not e.settings.provider_lookup:
        return {"found": False, "asn": None, "country": None, "network": None, "isp": None}
    return await providers.detect(await home_ip(request))


class GuideIn(BaseModel):
    kind: Literal["isp", "router"]
    name: str = Field(min_length=1, max_length=60, pattern=r"\S")


@router.post("/providers/guide")
async def provider_guide(body: GuideIn, e: Engine = Depends(engine)):
    """A guide for any provider or router: hand-checked, AI-written (cached), or generic."""
    return await providers.guide_for(e.conn, e.explainer, body.kind, body.name)
