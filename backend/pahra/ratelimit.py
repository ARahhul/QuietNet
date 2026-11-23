"""Rate limits, per account.

Each home (account) gets its own allowance, so one busy or misbehaving home can't use up the AI budget or slow the
server for the others. Two kinds of limit:

* a general one on every API call: far above what the dashboard sends (it polls a few times a minute), so only a
  script or a stuck loop ever meets it;
* tighter ones, with a per-minute and a per-day count, on the calls that cost money or touch AdGuard (the AI
  features, device changes).

Plus a brake on wrong sign-in attempts per IP address, so the access token can't be guessed by trying many.

It is a sliding window kept in memory: QuietNet runs as one process, a restart forgives everyone, and nothing here
needs a database or another service. If it ever runs as several processes, move the counts to Redis.
"""

import math
import time
from collections import deque
from collections.abc import Callable

from fastapi import HTTPException, Request

MINUTE, DAY = 60, 86400

# (allowed, within this many seconds). Every one of a bucket's rules must pass.
API = ((240, MINUTE),)  # the dashboard sends ~40 a minute with a page open; 240 leaves room for several tabs
AD_HUNT = ((6, MINUTE), (60, DAY))  # one AI call each, reading up to 160 names
SCREENSHOT = ((4, MINUTE), (30, DAY))  # a vision call: the dearest one
GUIDE = ((6, MINUTE), (40, DAY))  # AI-written router guide (cached afterwards)
ASSISTANT_START = ((5, MINUTE), (20, DAY))
ASSISTANT_MESSAGE = ((20, MINUTE), (200, DAY))
DEVICE_CHANGE = ((20, MINUTE),)  # adding, editing or deleting a device rewrites AdGuard's config
POLL_NOW = ((6, MINUTE),)
BAD_SIGN_IN = (20, MINUTE)  # wrong tokens from one address

_WHAT = {
    "ad_hunt": "ad lookups",
    "screenshot": "screenshot reads",
    "guide": "guide requests",
    "assistant_start": "assistant chats",
    "assistant_message": "assistant messages",
}


class Limiter:
    """Sliding-window counts by key. `clock` is injectable so tests don't sleep."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self._hits: dict[tuple, deque[float]] = {}
        self._last_sweep = clock()

    def retry_after(self, key: tuple, rules: tuple[tuple[int, int], ...]) -> int:
        """Seconds until a request would be allowed (0 = allowed now). Counts nothing."""
        now = self.clock()
        hits = self._hits.setdefault(key, deque())
        longest = max(window for _, window in rules)
        while hits and hits[0] <= now - longest:  # forget only what no rule of this bucket still needs
            hits.popleft()
        wait = 0.0
        for allowed, window in rules:
            # `hits[-allowed]` is the allowed-th newest hit: if it is still inside this rule's window, the rule is
            # full, and the next request fits once that hit is `window` seconds old.
            if len(hits) >= allowed and hits[-allowed] > now - window:
                wait = max(wait, hits[-allowed] + window - now)
        return math.ceil(wait) if wait > 0 else 0

    def record(self, key: tuple) -> None:
        self._hits.setdefault(key, deque()).append(self.clock())
        self._sweep()

    def take(self, key: tuple, rules: tuple[tuple[int, int], ...]) -> int:
        """Count a request if the rules allow it. Returns 0, or the seconds to wait (and counts nothing)."""
        wait = self.retry_after(key, rules)
        if not wait:
            self.record(key)
        return wait

    def _sweep(self) -> None:
        """Forget keys nobody has used for a day, so the table can't grow without bound."""
        now = self.clock()
        if now - self._last_sweep < 600:
            return
        self._last_sweep = now
        for k in [k for k, h in self._hits.items() if not h or h[-1] <= now - DAY]:
            del self._hits[k]


def _plain(seconds: int) -> str:
    if seconds < 90:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    if seconds < 5400:
        return f"{round(seconds / 60)} minutes"
    return f"{round(seconds / 3600)} hours"


def blocked(bucket: str, wait: int, rules: tuple[tuple[int, int], ...]) -> HTTPException:
    daily = next((n for n, w in rules if w == DAY), None)
    what = _WHAT.get(bucket)
    if what and daily and wait > MINUTE:
        msg = f"You've used today's {daily} {what}. Try again in about {_plain(wait)}."
    else:
        msg = f"That's a lot at once. Wait {_plain(wait)} and try again."
    return HTTPException(status_code=429, detail=msg, headers={"Retry-After": str(wait)})


def limiter_of(request: Request) -> Limiter | None:
    return getattr(request.app.state, "limiter", None)


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def check_sign_in_attempts(request: Request) -> None:
    """Refuse an address that has just sent too many wrong tokens (before even looking at this one)."""
    lim = limiter_of(request)
    if lim is None:
        return
    wait = lim.retry_after(("bad-sign-in", client_ip(request)), (BAD_SIGN_IN,))
    if wait:
        raise HTTPException(429, f"Too many wrong sign-ins from here. Wait {_plain(wait)} and try again.", headers={"Retry-After": str(wait)})


def note_bad_sign_in(request: Request) -> None:
    lim = limiter_of(request)
    if lim is not None:
        lim.record(("bad-sign-in", client_ip(request)))
