import secrets

from fastapi import Depends, HTTPException, Query, Request

from ..auth import AuthError, account_for
from ..engine import Engine
from ..ratelimit import check_sign_in_attempts, note_bad_sign_in


class AccountView:
    """The engine as one account sees it: everything is shared except user_id, which is the signed-in account's.

    Routes filter everything by e.user_id, so handing them this view is what keeps each home to its own devices.
    """

    __slots__ = ("_engine", "user_id")

    def __init__(self, engine: Engine, user_id: str):
        object.__setattr__(self, "_engine", engine)
        object.__setattr__(self, "user_id", user_id)

    def __getattr__(self, name: str):
        return getattr(self._engine, name)

    def __setattr__(self, name: str, value) -> None:
        setattr(self._engine, name, value)


def _given(request: Request, token: str | None) -> str:
    auth = request.headers.get("authorization", "")
    return (auth[7:] if auth.lower().startswith("bearer ") else token or "").strip()


async def current_user(request: Request, token: str | None = Query(default=None)) -> str:
    """The account this request belongs to: a Firebase sign-in, or the operator's API token.

    `?token=` is accepted for EventSource and downloads, which can't send headers.
    """
    if getattr(request.state, "user_id", None):
        return request.state.user_id
    e: Engine = request.app.state.engine
    check_sign_in_attempts(request)  # an address that just sent many wrong tokens waits
    given = _given(request, token)
    if not given:
        raise HTTPException(status_code=401, detail="missing or invalid token")
    if secrets.compare_digest(given.encode(), e.settings.api_token.encode()):
        uid = e.user_id
    elif e.auth.enabled and given.count(".") == 2:
        try:
            claims = await e.auth.verify(given)
        except AuthError:
            note_bad_sign_in(request)
            raise HTTPException(status_code=401, detail="missing or invalid token") from None
        request.state.claims = claims
        uid = account_for(e.conn, claims)
    else:
        note_bad_sign_in(request)
        raise HTTPException(status_code=401, detail="missing or invalid token")
    request.state.user_id = uid
    return uid


require_token = current_user  # older name, used by the assistant router


def engine(request: Request, user_id: str = Depends(current_user)) -> Engine:
    return AccountView(request.app.state.engine, user_id)  # type: ignore[return-value]
