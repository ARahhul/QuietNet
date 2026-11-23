from fastapi import APIRouter, Depends

from . import account, cards, devices, hunt, misc, parental, providers, screen, sites, traffic
from .deps import current_user
from .limits import limit_api

router = APIRouter(prefix="/api", dependencies=[Depends(current_user), Depends(limit_api)])
router.include_router(account.router)
router.include_router(devices.router)
router.include_router(cards.router)
router.include_router(misc.router)
router.include_router(providers.router)
router.include_router(screen.router)
router.include_router(traffic.router)
router.include_router(hunt.router)
router.include_router(sites.router)
router.include_router(parental.router)
