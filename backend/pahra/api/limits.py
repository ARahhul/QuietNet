"""Route dependencies that apply the per-account rate limits (see pahra.ratelimit for the numbers)."""

from fastapi import Depends, Request

from ..ratelimit import API, Limiter, blocked, limiter_of
from .deps import current_user


def limit(bucket: str, rules: tuple[tuple[int, int], ...]):
    """`dependencies=[Depends(limit("ad_hunt", AD_HUNT))]` on a route: this account's own allowance for it."""

    async def check(request: Request, user_id: str = Depends(current_user)) -> None:
        lim: Limiter | None = limiter_of(request)
        if lim is None:
            return
        wait = lim.take((user_id, bucket), rules)
        if wait:
            raise blocked(bucket, wait, rules)

    return check


limit_api = limit("api", API)  # on every /api route
