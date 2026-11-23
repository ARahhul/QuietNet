"""Polls AdGuard's query log every N seconds and feeds new traffic into the pipeline.

The cursor (newest processed entry time) is stored in SQLite, so when the laptop
sleeps and comes back, polling resumes where it left off (AdGuard keeps 24 h of logs).
"""

import asyncio
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from .adguard.client import AdGuardClient, AdGuardError
from .adguard.rules import is_parental_rule
from .db import get_meta, set_meta
from .events import ingest_event
from .pipeline.normalize import normalize_domain
from .timeutil import parse_ag_time, to_iso

log = logging.getLogger("pahra.poller")

PAGE = 500
KEEP_DEFAULT_HOURS = 24
MAX_PAGES = 20
FIRST_RUN_WINDOW = timedelta(hours=1)


@dataclass
class Observed:
    """A (device, normalised domain) pair seen in this poll, with the raw names that mapped to it."""

    device_id: str
    domain: str
    qnames: set[str] = field(default_factory=set)
    is_new: bool = False
    max_gap_s: float = 0.0  # longest quiet period before a query in this poll ("tracker came back")


class Poller:
    def __init__(
        self,
        conn: sqlite3.Connection,
        ag: AdGuardClient,
        interval_s: int,
        on_observed: Callable[[list[Observed]], Awaitable[None]] | None = None,
        on_tick: Callable[[], Awaitable[None]] | None = None,
        keep_hours: int = KEEP_DEFAULT_HOURS,
    ):
        self.conn = conn
        self.ag = ag
        self.interval_s = interval_s
        self.keep_hours = keep_hours
        self.on_observed = on_observed
        self.on_tick = on_tick
        self.last_poll: str | None = None
        self.last_error: str | None = None
        self._wake = asyncio.Event()
        self.running = False
        self.read_until = 0.0  # monotonic time up to which the query log has been read and processed

    def poke(self) -> None:
        """Poll now instead of waiting for the next tick (e.g. right after a device is added)."""
        self._wake.set()

    async def poll_fresh(self, timeout: float = 8.0) -> None:
        """Return once the query log has been read from now (for "is it working yet?" checks)."""
        if not self.running:  # no background loop (tests, CLI): poll right here
            observed = await self.poll_once()
            if observed and self.on_observed:
                await self.on_observed(observed)
            return
        t0 = time.monotonic()
        self.poke()
        while self.read_until < t0 and time.monotonic() < t0 + timeout:
            await asyncio.sleep(0.2)

    async def run_forever(self) -> None:
        self.running = True
        while True:
            started = time.monotonic()
            try:
                observed = await self.poll_once()
                if observed and self.on_observed:
                    await self.on_observed(observed)
                if self.on_tick:
                    await self.on_tick()
                self.last_error = None
            except AdGuardError as e:
                self.last_error = str(e)
                log.warning("poll failed: %s", e)
            except Exception:  # keep the loop alive; log the bug
                log.exception("poller iteration crashed")
            self.read_until = started
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.interval_s)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def _fetch_new(self, cursor: datetime | None) -> list[dict]:
        out: list[dict] = []
        older: str | None = None
        floor = None if cursor else datetime.now(timezone.utc) - FIRST_RUN_WINDOW
        for _ in range(MAX_PAGES):
            data = await self.ag.querylog(older_than=older, limit=PAGE) or {}
            batch = data.get("data") or []
            if not batch:
                break
            stop = False
            for e in batch:
                t = parse_ag_time(e["time"])
                if (cursor and t <= cursor) or (floor and t < floor):
                    stop = True
                    break
                e["_t"] = t
                out.append(e)
            if stop or len(batch) < PAGE:
                break
            older = data.get("oldest") or batch[-1]["time"]
        return out

    def _device_maps(self) -> tuple[dict[str, str], dict[str, str]]:
        by_cid, by_ip = {}, {}
        for r in self.conn.execute("SELECT id, client_id, linked_ip FROM devices"):
            by_cid[r["client_id"]] = r["id"]
            if r["linked_ip"]:
                by_ip[r["linked_ip"]] = r["id"]
        return by_cid, by_ip

    async def poll_once(self) -> list[Observed]:
        cursor_s = get_meta(self.conn, "querylog_cursor")
        cursor = parse_ag_time(cursor_s) if cursor_s else None
        entries = await self._fetch_new(cursor)
        self.last_poll = to_iso(datetime.now(timezone.utc))
        if not entries:
            return []

        by_cid, by_ip = self._device_maps()
        observed: dict[tuple[str, str], Observed] = {}
        last_seen: dict[str, datetime] = {}
        entries.reverse()  # AdGuard lists newest first; reversing keeps arrival order for equal timestamps
        entries.sort(key=lambda e: e["_t"])
        for e in entries:
            cid = e.get("client_id") or (e.get("client_info") or {}).get("name") or ""
            dev = by_cid.get(cid) or by_ip.get(e.get("client", ""))
            if not dev:
                continue
            qname = ((e.get("question") or {}).get("name") or "").lower().strip(".")
            if not qname:
                continue
            domain = normalize_domain(qname)
            blocked = is_blocked(e)
            gap = ingest_event(self.conn, dev, domain, e["_t"], blocked)
            log_query(self.conn, dev, qname, domain, e, blocked)
            ob = observed.setdefault((dev, domain), Observed(dev, domain))
            ob.qnames.add(qname)
            ob.is_new = ob.is_new or gap is None
            ob.max_gap_s = max(ob.max_gap_s, gap or 0.0)
            last_seen[dev] = e["_t"]

        for dev, t in last_seen.items():
            self.conn.execute("UPDATE devices SET last_seen = ? WHERE id = ?", (to_iso(t), dev))
        newest = max(entries, key=lambda e: e["_t"])
        set_meta(self.conn, "querylog_cursor", newest["time"])
        prune_queries(self.conn, self.keep_hours)
        return list(observed.values())


def is_blocked(e: dict) -> bool:
    """AdGuard's "Filtered…" reasons, except SafeSearch: that answers with the search engine's safe address."""
    reason = str(e.get("reason", ""))
    return reason.startswith("Filtered") and reason != "FilteredSafeSearch"


def blocked_by_of(e: dict) -> str:
    """rule (one of your QuietNet rules) | parental (parental controls) | list (an AdGuard filter list)."""
    if e.get("reason") == "FilteredParental":
        return "parental"
    # rules from filter_list_id 0 are AdGuard's custom rules, which only QuietNet writes
    custom = [(r or {}).get("text") or "" for r in (e.get("rules") or []) if not (r or {}).get("filter_list_id")]
    if any(is_parental_rule(t) for t in custom):
        return "parental"
    return "rule" if custom else "list"


def log_query(conn: sqlite3.Connection, device_id: str, qname: str, domain: str, e: dict, blocked: bool) -> None:
    """Keep one AdGuard query-log entry for the live view and the report charts."""
    if blocked:
        status = "blocked"
        blocked_by = blocked_by_of(e)
    else:
        status = "cached" if e.get("cached") else "allowed"
        blocked_by = None
    try:
        elapsed = float(e.get("elapsedMs") or 0) or None
    except (TypeError, ValueError):
        elapsed = None
    conn.execute(
        "INSERT INTO queries(device_id, t, qname, domain, qtype, client_ip, proto, status, blocked_by, upstream, elapsed_ms) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            device_id,
            e["_t"].timestamp(),
            qname,
            domain,
            (e.get("question") or {}).get("type"),
            e.get("client") or None,
            e.get("client_proto") or None,
            status,
            blocked_by,
            e.get("upstream") or None,
            elapsed,
        ),
    )


def prune_queries(conn: sqlite3.Connection, keep_hours: int) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=keep_hours)).timestamp()
    return conn.execute("DELETE FROM queries WHERE t < ?", (cutoff,)).rowcount
