"""Short-lived tickets for the two links a browser opens without an Authorization header: the assistant's event
stream (EventSource) and the iPhone profile download (a plain link Safari follows).

A ticket names one account and one exact path and expires in two minutes, so the sign-in or the long-lived API
token never goes into a URL (URLs end up in access logs and browser history). The key lives only in memory: a
restart voids at most two minutes of tickets, and there is no secret to configure.
"""

import base64
import hashlib
import hmac
import re
import time

TTL_S = 120
# The only paths a ticket opens: GETs a browser makes without headers. The endpoints still check ownership.
PATHS = (
    re.compile(r"^/api/assistant/sessions/[A-Za-z0-9_-]{1,64}/stream$"),
    re.compile(r"^/api/devices/[A-Za-z0-9_-]{1,64}/profile\.mobileconfig$"),
)


def allowed(path: str) -> bool:
    return any(p.match(path) for p in PATHS)


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _sign(key: bytes, body: bytes) -> str:
    return _b64(hmac.new(key, body, hashlib.sha256).digest())


def make(key: bytes, user_id: str, path: str, ttl: int = TTL_S, now: float | None = None) -> str:
    body = f"{user_id}|{int((now if now is not None else time.time()) + ttl)}|{path}".encode()
    return f"{_b64(body)}.{_sign(key, body)}"


def check(key: bytes, ticket: str, path: str, now: float | None = None) -> str | None:
    """The account the ticket was made for, when it is genuine, unexpired and made for exactly this path."""
    try:
        enc, sig = ticket.split(".", 1)
        body = base64.urlsafe_b64decode(enc + "=" * (-len(enc) % 4))
    except ValueError:  # binascii.Error is a ValueError
        return None
    if not hmac.compare_digest(sig, _sign(key, body)):
        return None
    try:
        user_id, exp, for_path = body.decode().split("|", 2)
        expires = int(exp)
    except ValueError:  # UnicodeDecodeError too
        return None
    if for_path != path or expires < (now if now is not None else time.time()):
        return None
    return user_id
