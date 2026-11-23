"""Who is asking: one QuietNet account per home.

The dashboard signs people in with Firebase Authentication (Google, or a link sent by email) and sends the Firebase
ID token as its Bearer token. QuietNet checks the token's signature against Google's published keys and that it was
issued for this Firebase project, then finds or creates the account for that Firebase user. Every API route sees
only that account's devices, lookups, alerts and blocks (pahra.api.deps.engine hands routes a per-account view).

The server's own API_TOKEN still works and means the operator's account (the first, "seed" account): for scripts,
and for the browser that used the token before sign-in existed, which can move that account onto its Firebase
sign-in once (claim_operator).
"""

import asyncio
import base64
import json
import logging
import re
import sqlite3
import time

import httpx
from google.auth import exceptions as gexc
from google.auth import jwt as gjwt

from .db import new_id, now_iso, transaction

log = logging.getLogger("pahra.auth")

CERTS_URL = "https://www.googleapis.com/robot/v1/metadata/x509/securetoken@system.gserviceaccount.com"


class AuthError(Exception):
    """The token isn't a valid sign-in for this server (shown to nobody: the API answers 401)."""


def _segment(part: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


class FirebaseVerifier:
    """Checks Firebase ID tokens. Google's keys are fetched once and kept as long as Google says (Cache-Control)."""

    def __init__(self, project_id: str, emulator_host: str = "", transport: httpx.AsyncBaseTransport | None = None):
        self.project_id = project_id.strip()
        # Local development only: the Firebase Auth emulator signs nothing, so its tokens are taken as they are.
        self.emulator = bool(emulator_host.strip())
        self._transport = transport
        self._certs: dict[str, str] = {}
        self._fresh_until = 0.0
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.project_id)

    async def _keys(self) -> dict[str, str]:
        if self._certs and time.time() < self._fresh_until:
            return self._certs
        async with self._lock:
            if self._certs and time.time() < self._fresh_until:
                return self._certs
            async with httpx.AsyncClient(timeout=10, transport=self._transport) as c:
                r = await c.get(CERTS_URL)
            r.raise_for_status()
            m = re.search(r"max-age=(\d+)", r.headers.get("cache-control", ""))
            self._certs = r.json()
            self._fresh_until = time.time() + (int(m.group(1)) if m else 3600)
            return self._certs

    async def verify(self, token: str) -> dict:
        """The token's claims, or AuthError."""
        if not self.enabled:
            raise AuthError("sign-in isn't set up on this server")
        try:
            head, body, _ = token.split(".")
            header, claims = _segment(head), _segment(body)
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as e:
            raise AuthError("not a JWT") from e
        if self.emulator:
            if claims.get("exp", 0) < time.time():
                raise AuthError("token expired")
        else:
            if header.get("alg") != "RS256" or not header.get("kid"):
                raise AuthError("unexpected signing algorithm")
            try:
                keys = await self._keys()
            except (httpx.HTTPError, ValueError) as e:
                log.warning("couldn't fetch Firebase's signing keys: %s", e)
                raise AuthError("signing keys unavailable") from e
            if header["kid"] not in keys:
                self._fresh_until = 0  # Google rotated its keys: fetch again next time
                raise AuthError("unknown signing key")
            try:
                claims = gjwt.decode(token, certs={header["kid"]: keys[header["kid"]]}, audience=self.project_id, clock_skew_in_seconds=60)
            except (ValueError, gexc.GoogleAuthError) as e:
                raise AuthError(str(e)) from e
        if claims.get("aud") != self.project_id or claims.get("iss") != f"https://securetoken.google.com/{self.project_id}":
            raise AuthError("token was issued for another project")
        if not isinstance(claims.get("sub"), str) or not claims["sub"]:
            raise AuthError("token names no user")
        return claims


def account_for(conn: sqlite3.Connection, claims: dict) -> str:
    """The QuietNet account of this Firebase user, created on their first request."""
    uid = claims["sub"]
    row = conn.execute("SELECT id FROM users WHERE firebase_uid = ?", (uid,)).fetchone()
    if row:
        return row["id"]
    email = str(claims.get("email") or "").strip().lower()[:254] or f"{uid}@users.quietnet"
    if conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
        email = f"{uid}+{email}"[:254]  # emails stay unique; the account belongs to the Firebase user, not the email
    user_id = new_id()
    try:
        conn.execute(
            "INSERT INTO users(id, email, auto_mode, created_at, firebase_uid) VALUES (?, ?, 0, ?, ?)",
            (user_id, email, now_iso(), uid),
        )
    except sqlite3.IntegrityError:  # two first requests at once: the other one created it
        row = conn.execute("SELECT id FROM users WHERE firebase_uid = ?", (uid,)).fetchone()
        if row:
            return row["id"]
        raise
    log.info("new account %s for a Firebase sign-in", user_id)
    return user_id


class ClaimError(Exception):
    pass


def claim_operator(conn: sqlite3.Connection, operator_id: str, user_id: str, claims: dict) -> str:
    """Move the operator's account (everything set up with the API token) onto this Firebase sign-in.

    Only once, and only into an account that has nothing yet (the one created for this sign-in moments ago).
    """
    op = conn.execute("SELECT * FROM users WHERE id = ?", (operator_id,)).fetchone()
    if op is None:
        raise ClaimError("There's no account to move.")
    if op["firebase_uid"]:
        if op["firebase_uid"] == claims["sub"]:
            return op["id"]
        raise ClaimError("This server's first account already belongs to another sign-in.")
    if user_id != op["id"] and conn.execute("SELECT 1 FROM devices WHERE user_id = ?", (user_id,)).fetchone():
        raise ClaimError("This sign-in already has its own devices, so the first account can't be moved onto it.")
    with transaction(conn):  # never a deleted sign-in account without the operator account taking its place
        if user_id != op["id"]:
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        # The email stays: the server finds its operator account by SEED_USER_EMAIL when it starts.
        conn.execute("UPDATE users SET firebase_uid = ? WHERE id = ?", (claims["sub"], op["id"]))
    log.info("operator account %s moved onto a Firebase sign-in", op["id"])
    return op["id"]
