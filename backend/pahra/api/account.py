"""The signed-in account (see pahra.auth)."""

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import tickets
from ..auth import ClaimError, claim_operator
from ..engine import Engine
from .deps import engine

router = APIRouter(tags=["account"])


@router.get("/account")
async def account(request: Request, e: Engine = Depends(engine)):
    claims = getattr(request.state, "claims", None)
    operator = request.app.state.engine.user_id
    return {
        "signed_in_with": "firebase" if claims else "token",
        "email": (claims or {}).get("email"),
        "operator": e.user_id == operator,
        # The operator's account can still be moved onto a sign-in (POST /account/claim)
        "can_claim": bool(claims)
        and e.user_id != operator
        and not e.conn.execute("SELECT firebase_uid FROM users WHERE id = ?", (operator,)).fetchone()["firebase_uid"],
    }


class TicketIn(BaseModel):
    path: str = Field(min_length=1, max_length=200)  # under /api, e.g. /devices/<id>/profile.mobileconfig


@router.post("/tickets")
async def ticket(body: TicketIn, e: Engine = Depends(engine)):
    """A two-minute ticket for one link that can't carry the sign-in: the assistant's stream or the iPhone profile.
    Only a Bearer sign-in gets one (a ticket can't make another: tickets only open GETs)."""
    path = "/api" + body.path
    if not tickets.allowed(path):
        raise HTTPException(422, "Tickets are only for the assistant's stream and the iPhone profile download.")
    return {"ticket": tickets.make(e.ticket_key, e.user_id, path), "expires_in": tickets.TTL_S}


class ClaimIn(BaseModel):
    token: str = Field(min_length=1, max_length=500)


@router.post("/account/claim")
async def claim(body: ClaimIn, request: Request, e: Engine = Depends(engine)):
    """Move the devices set up with this server's access token into the signed-in account (once)."""
    claims = getattr(request.state, "claims", None)
    if not claims:
        raise HTTPException(400, "Sign in first.")
    if not secrets.compare_digest(body.token.strip().encode(), e.settings.api_token.encode()):
        raise HTTPException(403, "That isn't this server's access token.")
    try:
        uid = claim_operator(e.conn, request.app.state.engine.user_id, e.user_id, claims)
    except ClaimError as ex:
        raise HTTPException(409, str(ex)) from None
    request.state.user_id = uid
    return {"ok": True}
