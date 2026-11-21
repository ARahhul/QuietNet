from fastapi import APIRouter, Depends

from . import cards, devices, misc, traffic
from .deps import require_token

router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])
router.include_router(devices.router)
router.include_router(cards.router)
router.include_router(misc.router)
router.include_router(traffic.router)
