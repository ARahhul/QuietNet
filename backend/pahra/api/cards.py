from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..adguard.client import AdGuardError
from ..db import rows_to_dicts
from ..decisions import DecisionError, create_decision, get_decision, reinstate, undo_decision
from ..engine import Engine
from ..knowledge import is_must_allow_any_device
from ..queries import CONF_RANK, blocked_domains, suggestions
from .deps import engine

router = APIRouter(tags=["suggestions & decisions"])


class DecisionIn(BaseModel):
    domain: str
    action: Literal["block", "allow"]
    device_id: str | None = None  # None = whole household
    confirm_must_allow: bool = False


class BulkIn(BaseModel):
    categories: list[Literal["ads", "tracking", "telemetry", "malware"]] = Field(
        default_factory=lambda: ["ads", "tracking", "telemetry"]
    )
    device_id: str | None = None
    min_confidence: Literal["high", "medium", "low"] = "medium"
    # The cards the user was looking at. Cards that arrived after the page rendered are left alone.
    domains: list[str] | None = Field(default=None, max_length=1000)


class UndoBulkIn(BaseModel):
    ids: list[str]
    # Decisions the undone ones replaced, to put back (undoing an unblock restores the block it lifted)
    restore: list[str] = Field(default_factory=list, max_length=200)


async def _push(e: Engine) -> bool:
    try:
        await e.sync.sync_rules()
        return True
    except AdGuardError:
        return False


@router.get("/suggestions")
async def get_suggestions(device_id: str | None = None, e: Engine = Depends(engine)):
    return suggestions(e.conn, e.user_id, device_id)


@router.get("/decisions")
async def list_decisions(active: bool = True, e: Engine = Depends(engine)):
    sql = "SELECT * FROM decisions WHERE user_id = ?" + (" AND undone_at IS NULL" if active else "")
    return rows_to_dicts(e.conn.execute(sql + " ORDER BY created_at DESC LIMIT 500", (e.user_id,)))


@router.get("/blocked")
async def list_blocked(e: Engine = Depends(engine)):
    """Every domain blocked right now, household-wide or on one device, newest first."""
    return blocked_domains(e.conn, e.user_id)


@router.post("/decisions", status_code=201)
async def decide(body: DecisionIn, e: Engine = Depends(engine)):
    if body.action == "block" and is_must_allow_any_device(body.domain) and not body.confirm_must_allow:
        raise HTTPException(
            409,
            {
                "warning": "must_allow",
                "message": f"{body.domain} is needed for devices or core apps to work (updates, time, streaming). "
                "Blocking it may break things. Send confirm_must_allow=true to block anyway.",
            },
        )
    try:
        d = create_decision(e.conn, e.user_id, body.domain, body.action, body.device_id, "user")
    except (DecisionError, ValueError) as ex:
        raise HTTPException(422, str(ex))
    return {"decision": d, "synced": await _push(e)}


@router.post("/decisions/bulk", status_code=201)
async def decide_bulk(body: BulkIn, e: Engine = Depends(engine)):
    """'Block all ads & trackers': one household rule per suggested domain (or per device if device_id given)."""
    cards = suggestions(e.conn, e.user_id, body.device_id)
    limit = CONF_RANK[body.min_confidence]
    shown = set(body.domains) if body.domains is not None else None
    created = []
    for c in cards:
        if shown is not None and c["domain"] not in shown:
            continue
        if c["category"] not in body.categories or CONF_RANK.get(c["confidence"], 3) > limit or c["must_allow"]:
            continue
        created.append(create_decision(e.conn, e.user_id, c["domain"], "block", body.device_id, "user"))
    return {"decisions": created, "count": len(created), "synced": await _push(e)}


@router.post("/decisions/{decision_id}/undo")
async def undo(decision_id: str, e: Engine = Depends(engine)):
    try:
        d = undo_decision(e.conn, e.user_id, decision_id)
    except DecisionError as ex:
        raise HTTPException(404, str(ex))
    return {"decision": d, "synced": await _push(e)}


@router.post("/decisions/undo-bulk")
async def undo_bulk(body: UndoBulkIn, e: Engine = Depends(engine)):
    out = []
    for i in body.ids:
        if get_decision(e.conn, i):
            out.append(undo_decision(e.conn, e.user_id, i))
    restored = [d for i in body.restore if (d := reinstate(e.conn, e.user_id, i))]
    return {"count": len(out), "restored": len(restored), "synced": await _push(e)}
