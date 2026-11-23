import secrets
from collections.abc import Callable, Iterable

from fastapi import Depends, HTTPException, Query, Request

from .. import tickets
from ..auth import AuthError, account_for
from ..engine import Engine
from ..ratelimit import check_sign_in_attempts, note_bad_sign_in


class AccountView:
    """The engine as one account sees it: everything is shared except user_id, which is the signed-in account's.

    Routes filter everything by e.user_id, so handing them this view is what keeps each home to its own devices.
    """

    __slots__ = ("_engine", "user_id")
    # Engine state an API call may set on purpose for everyone (forwarder.apply records the laptop relay's state).
    # Anything else, user_id above all, can't be set through one account's view: it would change it for every home.
    _SHARED = frozenset({"forwarder"})

    def __init__(self, engine: Engine, user_id: str):
        object.__setattr__(self, "_engine", engine)
        object.__setattr__(self, "user_id", user_id)

    def __getattr__(self, name: str):
        return getattr(self._engine, name)

    def __setattr__(self, name: str, value) -> None:
        if name not in self._SHARED:
            raise AttributeError(f"can't set {name!r} through one account's view of the engine")
        setattr(self._engine, name, value)

    # QuietNet's shared block list, as this home has it (Settings can switch it off for one home)
    @property
    def home_uses_list(self) -> bool:
        row = self._engine.conn.execute("SELECT shared_blocklist FROM users WHERE id = ?", (self.user_id,)).fetchone()
        return bool(row and row["shared_blocklist"])

    @property
    def blocklist_on(self) -> bool:
        """The list is loaded in AdGuard and blocks for this home."""
        return self._engine.sync.blocklist_active and self.home_uses_list

    @property
    def blocklisted(self) -> Callable[..., bool]:
        """Engine.blocklisted for this home: never true once the home switched the list off. Read once per request,
        then called per domain."""
        return self._engine.blocklisted if self.home_uses_list else _never_listed


def _never_listed(domain: str, qnames: Iterable[str] = ()) -> bool:
    return False


def _given(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    return (auth[7:] if auth.lower().startswith("bearer ") else "").strip()


async def current_user(request: Request, ticket: str | None = Query(default=None)) -> str:
    """The account this request belongs to: a Firebase sign-in, or the operator's API token, sent as a Bearer.

    The assistant's EventSource and the iPhone profile link can't send headers: they carry a two-minute `?ticket=`
    from POST /api/tickets instead (pahra.tickets), valid for a GET of that one path. A token in the URL isn't
    accepted, since URLs end up in access logs and browser history.
    """
    if getattr(request.state, "user_id", None):
        return request.state.user_id
    e: Engine = request.app.state.engine
    check_sign_in_attempts(request)  # an address that just sent many wrong tokens waits
    if ticket is not None and not request.headers.get("authorization"):
        uid = tickets.check(e.ticket_key, ticket, request.url.path) if request.method == "GET" else None
        if uid is None:
            note_bad_sign_in(request)
            raise HTTPException(status_code=401, detail="missing or invalid token")
        request.state.user_id = uid
        return uid
    given = _given(request)
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
