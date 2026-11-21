"""Stage 4: parallel clue enrichment with asyncio.gather (public information only)."""

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from ..events import is_beacon
from .fastchecks import FastResult
from .normalize import is_shared_platform, root_domain

log = logging.getLogger("pahra.clues")

RDAP_URL = "https://rdap.org/domain/{}"


@dataclass
class Clues:
    domain: str
    fast: FastResult
    domain_age_days: int | None
    beacon: bool
    sample_names: list[str]

    def for_llm(self) -> dict:
        """Only public facts about the domain. No user, device, IP or timing data."""
        f = self.fast
        return {
            "domain": self.domain,
            "example_hostnames": self.sample_names[:5],
            "on_lists": f.list_hits + f.threat_hits,
            "tracker_radar_owner": f.tracker_owner or f.entity,
            "tracker_radar_category": f.tracker_category,
            "domain_age_days": self.domain_age_days,
            "regular_beacon_pattern": self.beacon,
            "name_patterns": f.patterns,
            "random_looking_name": f.dga >= 0.6,
        }


class ClueFetcher:
    def __init__(self, conn: sqlite3.Connection, online: bool = True):
        self.conn = conn
        self.online = online
        self._age_cache: dict[str, int | None] = {}
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(4.0), headers={"User-Agent": "pahra/0.1"})

    async def aclose(self) -> None:
        await self._http.aclose()

    async def domain_age_days(self, domain: str) -> int | None:
        root = root_domain(domain)
        if not self.online or is_shared_platform(domain):
            return None
        if root in self._age_cache:
            return self._age_cache[root]
        age = None
        try:
            r = await self._http.get(RDAP_URL.format(root), follow_redirects=True)
            if r.status_code == 200:
                for ev in r.json().get("events", []):
                    if ev.get("eventAction") == "registration":
                        reg = datetime.fromisoformat(ev["eventDate"].replace("Z", "+00:00"))
                        age = (datetime.now(timezone.utc) - reg).days
                        break
        except (httpx.HTTPError, ValueError, KeyError) as e:
            log.debug("rdap failed for %s: %s", root, e)
        self._age_cache[root] = age
        return age

    def beacon(self, domain: str) -> bool:
        rows = self.conn.execute("SELECT * FROM events WHERE domain = ?", (domain,)).fetchall()
        return any(is_beacon(r) for r in rows)

    async def one(self, fast: FastResult, sample_names: list[str]) -> Clues:
        # List / Tracker Radar / threat-feed clues are already in `fast` (in-memory);
        # the network clue (RDAP age) is the slow part.
        age = await self.domain_age_days(fast.domain)
        return Clues(fast.domain, fast, age, self.beacon(fast.domain), sorted(sample_names))

    async def gather(self, items: list[tuple[FastResult, list[str]]]) -> list[Clues]:
        return list(await asyncio.gather(*(self.one(f, names) for f, names in items)))
