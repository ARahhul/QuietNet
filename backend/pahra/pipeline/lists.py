"""In-memory blocklists + DuckDuckGo Tracker Radar (TDS) for <1 ms Stage-3 checks.

Downloaded at startup (cached on disk, refreshed daily). If a download fails we
fall back to the on-disk cache, then to the small bundled sample lists, so the
engine always starts.

Note: abuse.ch's public hostfiles now require an Auth-Key, so threat intel comes
from HaGeZi's Threat Intelligence Feeds list (which aggregates abuse.ch and others).
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

log = logging.getLogger("pahra.lists")

HAGEZI = "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/wildcard/{}-onlydomains.txt"
TDS_URL = "https://staticcdn.duckduckgo.com/trackerblocking/v6/current/extension-tds.json"
SAMPLE_DIR = Path(__file__).resolve().parent.parent / "data" / "sample_lists"
MAX_AGE_S = 24 * 3600


@dataclass(frozen=True)
class ListSource:
    name: str  # shown to users as a source ("HaGeZi LG webOS list")
    slug: str
    kind: str  # trackers | native | threat


SOURCES: list[ListSource] = [
    ListSource("HaGeZi Pro list", "pro", "trackers"),
    ListSource("HaGeZi LG webOS list", "native.lgwebos", "native"),
    ListSource("HaGeZi Samsung list", "native.samsung", "native"),
    ListSource("HaGeZi Amazon list", "native.amazon", "native"),
    ListSource("HaGeZi Roku list", "native.roku", "native"),
    ListSource("HaGeZi Xiaomi list", "native.xiaomi", "native"),
    ListSource("HaGeZi Apple list", "native.apple", "native"),
    ListSource("HaGeZi TikTok list", "native.tiktok", "native"),
    ListSource("HaGeZi Windows/Office list", "native.winoffice", "native"),
    ListSource("Threat intel feeds (HaGeZi TIF, incl. abuse.ch)", "tif.mini", "threat"),
]

# TDS category -> Pahra category
_TDS_CATEGORY = {
    "Advertising": "ads",
    "Ad Motivated Tracking": "tracking",
    "Ad Fraud": "ads",
    "Analytics": "tracking",
    "Third-Party Analytics Marketing": "tracking",
    "Audience Measurement": "tracking",
    "Session Replay": "tracking",
    "Action Pixels": "tracking",
    "Obscure Ownership": "tracking",
    "Unknown High Risk Behavior": "tracking",
    "Badge": "tracking",
    "Social Network": "tracking",
    "Embedded Content": "needed",
    "CDN": "needed",
    "Online Payment": "needed",
    "SSO": "needed",
    "Federated Login": "needed",
    "Support Chat Widget": "needed",
    "Consent Management Platform": "needed",
    "Tag Manager": "tracking",
    "Malware": "malware",
}


@dataclass
class TrackerInfo:
    domain: str
    owner: str | None
    categories: list[str]
    blocked_by_ddg: bool = True  # TDS default=block -> confirmed tracker; ignore -> mixed-use platform

    @property
    def pahra_category(self) -> str | None:
        mapped = [_TDS_CATEGORY.get(c) for c in self.categories]
        for pref in ("malware", "ads", "tracking", "needed"):
            if pref in mapped:
                return pref
        return None


def parents(fqdn: str) -> list[str]:
    """a.b.example.com -> [a.b.example.com, b.example.com, example.com, com]"""
    labels = fqdn.lower().strip(".").split(".")
    return [".".join(labels[i:]) for i in range(len(labels))]


def _parse_domains(text: str) -> set[str]:
    out = set()
    for line in text.splitlines():
        line = line.strip().lower()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        if " " in line:  # tolerate hosts format
            line = line.split()[-1]
        out.add(line.lstrip("*.").strip("."))
    return out


@dataclass
class ListIndex:
    lists: dict[str, set[str]] = field(default_factory=dict)
    kinds: dict[str, str] = field(default_factory=dict)
    trackers: dict[str, TrackerInfo] = field(default_factory=dict)
    entity_domains: dict[str, str] = field(default_factory=dict)  # domain -> entity (TDS "domains")
    loaded_from: str = "empty"

    # ---------------------------------------------------------------- lookups
    def list_hits(self, *names: str) -> list[str]:
        hits: list[str] = []
        for list_name, domains in self.lists.items():
            if any(p in domains for n in names for p in parents(n)):
                hits.append(list_name)
        return hits

    def threat_hits(self, *names: str) -> list[str]:
        return [h for h in self.list_hits(*names) if self.kinds.get(h) == "threat"]

    def tracker(self, *names: str) -> TrackerInfo | None:
        for n in names:
            for p in parents(n):
                if p in self.trackers:
                    return self.trackers[p]
        return None

    def entity(self, *names: str) -> str | None:
        for n in names:
            for p in parents(n):
                if p in self.entity_domains:
                    return self.entity_domains[p]
        return None

    # ---------------------------------------------------------------- loading
    def load_tds(self, data: dict) -> None:
        self.trackers = {}
        for dom, t in (data.get("trackers") or {}).items():
            owner = (t.get("owner") or {}).get("displayName") or (t.get("owner") or {}).get("name")
            self.trackers[dom.lower()] = TrackerInfo(dom.lower(), owner, list(t.get("categories") or []), t.get("default", "block") == "block")
        self.entity_domains = {k.lower(): v for k, v in (data.get("domains") or {}).items()}

    @classmethod
    def from_dir(cls, directory: Path, label: str) -> "ListIndex":
        idx = cls(loaded_from=label)
        for src in SOURCES:
            f = directory / f"{src.slug}.txt"
            if f.exists():
                idx.lists[src.name] = _parse_domains(f.read_text(encoding="utf-8", errors="ignore"))
                idx.kinds[src.name] = src.kind
        tds = directory / "tds.json"
        if tds.exists():
            idx.load_tds(json.loads(tds.read_text(encoding="utf-8")))
        return idx

    @classmethod
    def sample(cls) -> "ListIndex":
        return cls.from_dir(SAMPLE_DIR, "bundled sample")

    def stats(self) -> dict:
        return {
            "loaded_from": self.loaded_from,
            "lists": {k: len(v) for k, v in self.lists.items()},
            "tracker_radar_entries": len(self.trackers),
        }


async def _download(client: httpx.AsyncClient, url: str, dest: Path) -> None:
    if dest.exists() and time.time() - dest.stat().st_mtime < MAX_AGE_S:
        return
    r = await client.get(url, follow_redirects=True)
    r.raise_for_status()
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_bytes(r.content)
    tmp.replace(dest)


async def load_lists(cache_dir: Path, offline: bool = False) -> ListIndex:
    if offline:
        return ListIndex.sample()
    jobs = [(HAGEZI.format(s.slug), cache_dir / f"{s.slug}.txt") for s in SOURCES]
    jobs.append((TDS_URL, cache_dir / "tds.json"))
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0), headers={"User-Agent": "pahra/0.1"}) as c:
        results = await asyncio.gather(*(_download(c, u, d) for u, d in jobs), return_exceptions=True)
    for (url, _), res in zip(jobs, results):
        if isinstance(res, Exception):
            log.warning("list download failed (%s): %s", url, res)
    idx = ListIndex.from_dir(cache_dir, "downloaded")
    if not idx.lists:
        log.warning("no lists available on disk; using bundled sample lists")
        return ListIndex.sample()
    return idx
