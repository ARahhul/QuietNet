import secrets

from fastapi import HTTPException, Query, Request

from ..engine import Engine


def engine(request: Request) -> Engine:
    return request.app.state.engine


def require_token(request: Request, token: str | None = Query(default=None)) -> None:
    """Bearer token (single-user hackathon auth). `?token=` is accepted for EventSource and downloads."""
    expected = request.app.state.engine.settings.api_token
    auth = request.headers.get("authorization", "")
    given = auth[7:] if auth.lower().startswith("bearer ") else token
    if not given or not secrets.compare_digest(given, expected):
        raise HTTPException(status_code=401, detail="missing or invalid token")
