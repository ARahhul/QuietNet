import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..api.deps import current_user
from ..api.limits import limit, limit_api
from ..ratelimit import ASSISTANT_MESSAGE, ASSISTANT_START
from .service import AssistantService

router = APIRouter(prefix="/api", tags=["assistant"], dependencies=[Depends(current_user), Depends(limit_api)])


def service(request: Request) -> AssistantService:
    svc = getattr(request.app.state, "assistant", None)
    if svc is None:
        raise HTTPException(503, "assistant disabled (COMPANION_ENABLED=false)")
    return svc


class StartIn(BaseModel):
    description: str = Field(min_length=1, max_length=200)


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=500)


class ConfirmIn(BaseModel):
    call_id: str
    approve: bool


@router.get("/companion/health")
async def companion_health(svc: AssistantService = Depends(service)):
    return svc.companion.status()


@router.post("/assistant/sessions", status_code=201, dependencies=[Depends(limit("assistant_start", ASSISTANT_START))])
async def start(body: StartIn, svc: AssistantService = Depends(service), user_id: str = Depends(current_user)):
    if not svc.companion.connected:
        raise HTTPException(503, svc.companion.error or "companion not connected")
    s = svc.create(body.description, user_id)
    return {"session_id": s.id, "mode": "agent" if svc.genai else "guided"}


def _session(svc: AssistantService, sid: str, user_id: str):
    s = svc.sessions.get(sid)
    if not s or s.user_id != user_id:
        raise HTTPException(404, "session not found")
    return s


@router.get("/assistant/sessions/{sid}/stream")
async def stream(sid: str, svc: AssistantService = Depends(service), user_id: str = Depends(current_user)):
    s = _session(svc, sid, user_id)

    async def gen():
        i = 0
        while True:
            async with s.cond:
                try:
                    await asyncio.wait_for(s.cond.wait_for(lambda: len(s.events) > i), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                batch = s.events[i:]
            for ev in batch:
                yield f"data: {json.dumps(ev)}\n\n"
            i += len(batch)
            if s.done and i >= len(s.events):
                return

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/assistant/sessions/{sid}")
async def events(sid: str, svc: AssistantService = Depends(service), user_id: str = Depends(current_user)):
    s = _session(svc, sid, user_id)
    return {"session_id": s.id, "done": s.done, "events": s.events}


@router.post("/assistant/sessions/{sid}/messages", dependencies=[Depends(limit("assistant_message", ASSISTANT_MESSAGE))])
async def message(sid: str, body: MessageIn, svc: AssistantService = Depends(service), user_id: str = Depends(current_user)):
    s = _session(svc, sid, user_id)
    if s.done:
        raise HTTPException(409, "session finished")
    s.user_message(body.text)
    return {"ok": True}


@router.post("/assistant/sessions/{sid}/confirm")
async def confirm(sid: str, body: ConfirmIn, svc: AssistantService = Depends(service), user_id: str = Depends(current_user)):
    s = _session(svc, sid, user_id)
    if not s.answer_confirm(body.call_id, body.approve):
        raise HTTPException(409, "nothing to confirm with that id")
    return {"ok": True}
