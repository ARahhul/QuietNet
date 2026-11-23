"""QuietNet's fast lookup: the block list and the clean list.

Every domain a home looks up lands in one of three buckets:

* **block list** (red-flagged ad / tracker / telemetry domains): AdGuard loads the same list as a filter, so every home
  gets them blocked on the very first lookup. QuietNet describes them from the list data, without AI.
* **clean list** (must-allow, hand-checked "needed" seeds, Tracker Radar CDN / login / payment domains, the most
  visited sites): never blocked automatically, described from the list, without AI.
* everything else is unknown and goes to the AI explainer once (the verdict is cached in `domains`).

Lookups are dict hits on a name and its parent domains (about five per name), so a check costs about a microsecond.

The block list comes from BLOCKLIST_CSV_PATH when set: the curated dataset described in
docs/teammate-task-domain-dataset.txt (`domain,category,owner,sources,source_count,notes`; rows labelled clean or
needed go to the clean list). Otherwise it is built from the lists QuietNet already downloads: HaGeZi Pro and the
HaGeZi device lists, plus threat intelligence for labelling (AdGuard loads that feed itself).

Must-allow domains, and any domain whose block would cover one of them (a parent such as google.com for
time.google.com), are dropped while building, so the list never needs exceptions. The list only ever blocks: the file
holds nothing but `||domain^` lines, so it can't open anything up, and each home's own allow rules (`@@...$client`) beat
it in AdGuard. That is how a person unblocks one site.
"""

import csv
import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .adguard.rules import DOMAIN_RE
from .knowledge import must_allow_all, seed_needed, seed_verdict
from .pipeline.lists import ListIndex, parents

log = logging.getLogger("pahra.blocklist")

FILE_NAME = "quietnet-blocklist.txt"
FILTER_NAME = "QuietNet block list"
BLOCK_CATEGORIES = ("ads", "tracking", "telemetry", "malware")
POPULAR_SOURCE = "Tranco top sites"

_AD_TOKENS = {"ad", "ads", "adserver", "adservice", "adserv", "adsrv", "banner", "banners", "doubleclick", "pagead"}
_TOKEN_SPLIT = re.compile(r"[-_.\d]+")

CATEGORY_TEXT = {
    "ads": "Serves or measures advertising.",
    "tracking": "Collects data about what you do, for advertising or analytics.",
    "telemetry": "Sends usage data from the device back to its maker.",
    "malware": "Listed in threat-intelligence feeds as malicious (malware or phishing).",
}
# CSV category -> QuietNet category (acr stays "tracking" with the acr flag, as in seed_domains.yaml)
_CSV_CATEGORY = {
    "ads": "ads", "ad": "ads", "advertising": "ads",
    "tracking": "tracking", "tracker": "tracking", "analytics": "tracking",
    "telemetry": "telemetry",
    "acr": "tracking",
    "malware": "malware", "phishing": "malware", "threat": "malware",
}
_CSV_CLEAN = {"clean", "needed", "safe", "allow"}


@dataclass(frozen=True, slots=True)
class Entry:
    """Why a domain is on the block list. Shared between domains with the same labels (interned)."""

    category: str  # ads | tracking | telemetry | malware
    owner: str | None
    sources: tuple[str, ...]
    notes: str = ""
    acr: bool = False
    in_adguard: bool = True  # False: labels only (threat intel AdGuard already loads as its own filter)

    @property
    def break_risk(self) -> str:
        return "medium" if "caution" in self.notes.lower() else "low"

    @property
    def unconfirmed(self) -> bool:
        return "unconfirmed" in self.notes.lower()


@dataclass(frozen=True, slots=True)
class CleanEntry:
    """Why a domain is known to be clean."""

    kind: str  # must_allow | seed | tracker_radar | popular | list
    owner: str | None
    sources: tuple[str, ...]
    note: str = ""


def _valid(d: str) -> bool:
    return bool(DOMAIN_RE.match(d))


def _clean_name(raw: str) -> str:
    d = raw.strip().lower().strip(".")
    for prefix in ("*.", "www."):
        if d.startswith(prefix):
            d = d[len(prefix):]
    return d


def _guard_sets() -> tuple[frozenset[str], frozenset[str]]:
    """(must-allow names, their parent domains): a block on either would stop something a device needs."""
    must = must_allow_all()
    ancestors = frozenset(p for m in must for p in parents(m)[1:-1])
    return must, ancestors


def _blocks_needed(d: str, must: frozenset[str], ancestors: frozenset[str]) -> bool:
    return d in ancestors or any(p in must for p in parents(d))


def _looks_like_ads(d: str) -> bool:
    return any(tok in _AD_TOKENS for label in d.split(".")[:-1] for tok in _TOKEN_SPLIT.split(label))


@dataclass
class BlocklistIndex:
    blocked: dict[str, Entry] = field(default_factory=dict)
    clean: dict[str, CleanEntry] = field(default_factory=dict)  # matches the name and its subdomains
    clean_exact: dict[str, CleanEntry] = field(default_factory=dict)  # matches the normalised domain only
    source: str = "empty"
    built_at: str = ""
    dropped_must_allow: int = 0

    # ------------------------------------------------------------------ lookups
    def entry(self, name: str) -> Entry | None:
        """The block-list entry covering `name` (itself or a parent domain)."""
        for p in parents(name)[:-1]:  # never the bare TLD
            e = self.blocked.get(p)
            if e is not None:
                return e
        return None

    def covers(self, domain: str) -> Entry | None:
        """AdGuard blocks this name for every home (unless the home let it through)."""
        e = self.entry(domain)
        return e if e is not None and e.in_adguard else None

    def match(self, domain: str, qnames: Iterable[str] = ()) -> Entry | None:
        """Entry for a normalised domain: the domain itself is listed, or every name it was looked up as is
        (ads.example.com listed doesn't make all of example.com an ad domain)."""
        e = self.entry(domain)
        if e is not None:
            return e
        names = [q for q in qnames if q and q != domain]
        if not names:
            return None
        hits = [self.entry(q) for q in names]
        return hits[0] if all(h is not None for h in hits) else None

    def clean_match(self, domain: str) -> CleanEntry | None:
        c = self.clean_exact.get(domain) or (self.clean_exact.get(domain[4:]) if domain.startswith("www.") else None)
        if c is not None:
            return c
        for p in parents(domain)[:-1]:
            c = self.clean.get(p)
            if c is not None:
                return c
        return None

    # ------------------------------------------------------------------ output
    def adguard_domains(self) -> list[str]:
        """Domains for AdGuard's filter, without names a listed parent already covers."""
        mine = {d for d, e in self.blocked.items() if e.in_adguard}
        return sorted(d for d in mine if not any(p in mine for p in parents(d)[1:-1]))

    def render_adguard(self) -> str:
        names = self.adguard_domains()
        head = [
            f"! Title: {FILTER_NAME}",
            "! Description: Known ad, tracker and telemetry domains, blocked for every QuietNet home. "
            "Built by QuietNet; do not edit (it is rewritten on every list refresh).",
            f"! Source: {self.source}",
            f"! Built: {self.built_at}",
            f"! Domains: {len(names)}",
        ]
        return "\n".join([*head, *(f"||{d}^" for d in names)]) + "\n"

    def stats(self) -> dict:
        return {
            "source": self.source,
            "built_at": self.built_at,
            "blocked": len(self.blocked),
            "blocked_in_adguard": sum(1 for e in self.blocked.values() if e.in_adguard),
            "clean": len(self.clean) + len(self.clean_exact),
            "dropped_must_allow": self.dropped_must_allow,
        }

    # ------------------------------------------------------------------ building
    @classmethod
    def build(cls, lists: ListIndex, csv_path: str | Path | None = None) -> "BlocklistIndex":
        path = Path(csv_path) if csv_path else None
        if path and path.exists():
            idx = cls._from_csv(path)
        else:
            if path:
                log.warning("BLOCKLIST_CSV_PATH %s not found; building the block list from the downloaded lists", path)
            idx = cls._from_lists(lists)
        idx._add_clean(lists)
        idx.built_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return idx

    @classmethod
    def _from_lists(cls, lists: ListIndex) -> "BlocklistIndex":
        idx = cls(source=f"QuietNet lists ({lists.loaded_from}): " + ", ".join(lists.lists))
        must, ancestors = _guard_sets()
        member: dict[str, list[str]] = {}
        for name, doms in lists.lists.items():
            for d in doms:
                member.setdefault(d, []).append(name)
        interned: dict[tuple, Entry] = {}
        for d, names in member.items():
            if not _valid(d):
                continue
            if _blocks_needed(d, must, ancestors):
                idx.dropped_must_allow += 1
                continue
            kinds = {lists.kinds.get(n) for n in names}
            t = lists.tracker(d)
            if "threat" in kinds:
                cat = "malware"
            elif t is not None and t.blocked_by_ddg and t.pahra_category in BLOCK_CATEGORIES:
                cat = t.pahra_category
            elif "trackers" not in kinds:
                cat = "telemetry"  # only on device lists
            else:
                cat = "ads" if _looks_like_ads(d) else "tracking"
            owner = (t.owner if t is not None else None) or lists.entity(d)
            key = (cat, owner, tuple(sorted(names)), bool(kinds - {"threat"}))
            e = interned.get(key)
            if e is None:
                e = interned[key] = Entry(cat, owner, key[2], in_adguard=key[3])
            idx.blocked[d] = e
        return idx

    @classmethod
    def _from_csv(cls, path: Path) -> "BlocklistIndex":
        idx = cls(source=f"curated list {path.name}")
        must, ancestors = _guard_sets()
        interned: dict[tuple, Entry] = {}
        clean_interned: dict[tuple, CleanEntry] = {}
        with path.open(encoding="utf-8", errors="ignore", newline="") as f:
            for row in csv.DictReader(f):
                d = _clean_name(row.get("domain") or "")
                if not _valid(d):
                    continue
                raw_cat = (row.get("category") or "").strip().lower()
                owner = (row.get("owner") or "").strip() or None
                sources = tuple(s.strip() for s in (row.get("sources") or "").split(";") if s.strip()) or (path.name,)
                notes = (row.get("notes") or "").strip()
                if raw_cat in _CSV_CLEAN:
                    key = ("list", owner, sources, notes)
                    c = clean_interned.get(key) or clean_interned.setdefault(key, CleanEntry(*key))
                    idx.clean[d] = c
                    continue
                if _blocks_needed(d, must, ancestors):
                    idx.dropped_must_allow += 1
                    continue
                cat = _CSV_CATEGORY.get(raw_cat, "tracking")
                key = (cat, owner, sources, notes, raw_cat == "acr")
                e = interned.get(key)
                if e is None:
                    e = interned[key] = Entry(cat, owner, sources, notes, acr=key[4])
                idx.blocked[d] = e
        return idx

    def _add_clean(self, lists: ListIndex) -> None:
        must = CleanEntry("must_allow", None, ("QuietNet must-allow list",))
        for d in must_allow_all():
            self.clean[d] = must
        for d, s in seed_needed().items():
            self.clean.setdefault(d, CleanEntry("seed", s.get("owner"), ("QuietNet seed data",)))
        for d, t in lists.trackers.items():
            if t.pahra_category == "needed" and self.entry(d) is None:
                self.clean.setdefault(d, CleanEntry("tracker_radar", t.owner, ("DuckDuckGo Tracker Radar",), ", ".join(t.categories)))
        # Popular is not the same as clean: doubleclick.net is in the top 10. A popular name counts as clean only when
        # no list has it or any of its subdomains and Tracker Radar doesn't call it a tracker.
        if lists.popular:
            listed_parents = {p for d in self.blocked for p in parents(d)[1:-1]}
            popular = CleanEntry("popular", None, (POPULAR_SOURCE,))
            for d in lists.popular:
                if not _valid(d) or d in listed_parents or self.entry(d) is not None:
                    continue
                t = lists.tracker(d)
                if t is not None and t.blocked_by_ddg and t.pahra_category in BLOCK_CATEGORIES:
                    continue
                self.clean_exact.setdefault(d, popular)


def write_adguard_file(idx: BlocklistIndex, directory: Path) -> tuple[Path, str]:
    """Write the AdGuard filter atomically. Returns (path, sha256 of the domain lines)."""
    directory.mkdir(parents=True, exist_ok=True)
    dest = directory / FILE_NAME
    text = idx.render_adguard()
    body = text.split("\n", 5)[-1]  # the header has a timestamp: hash the rules only
    digest = hashlib.sha256(body.encode()).hexdigest()
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(dest)
    return dest, digest


# ---------------------------------------------------------------------- verdicts (no AI)
def blocked_verdict(domain: str, e: Entry) -> dict:
    """A domains-table verdict from the block-list labels (hand-written seed text wins when there is some)."""
    seed = seed_verdict(domain) or {}
    text = seed.get("what_it_does") or CATEGORY_TEXT[e.category]
    if e.notes and not e.unconfirmed and "caution" not in e.notes.lower() and not seed:
        text = f"{text} {e.notes}"
    return {
        "domain": domain,
        "category": seed.get("category") if seed.get("category") in BLOCK_CATEGORIES else e.category,
        "owner": seed.get("owner") or e.owner,
        "what_it_does": text,
        "break_risk": seed.get("break_risk") or e.break_risk,
        "break_note": seed.get("break_note") or (e.notes if e.break_risk != "low" else "Most devices keep working."),
        "confidence": "high",
        "sources": list(e.sources),
        "explained_by": "blocklist",
    }


def threat_verdict(domain: str, threat_hits: list[str], owner: str | None) -> dict:
    return {
        "domain": domain,
        "category": "malware",
        "owner": owner,
        "what_it_does": CATEGORY_TEXT["malware"],
        "break_risk": "low",
        "break_note": "Nothing legitimate should need it.",
        "confidence": "high",
        "sources": list(threat_hits),
        "explained_by": "threat_list",
    }


MUST_ALLOW_TEXT = (
    "Needed for the device or a core app (updates, time, streaming) to work. QuietNet never blocks it automatically."
)
_CLEAN_TEXT = {
    "must_allow": MUST_ALLOW_TEXT,
    "tracker_radar": "Delivers content, sign-in or payments for apps and websites. It isn't an ad or tracker.",
    "popular": "A widely used website or app service (one of the most visited on the internet). "
    "It isn't on any ad or tracker list.",
    "list": "On QuietNet's clean list: a service apps and devices use, not an ad or tracker.",
}


def clean_verdict(domain: str, c: CleanEntry) -> dict:
    seed = seed_verdict(domain) or {}
    text = seed.get("what_it_does") or _CLEAN_TEXT.get(c.kind) or _CLEAN_TEXT["list"]
    if c.kind == "list" and c.note:
        text = f"{text} {c.note}"
    return {
        "domain": domain,
        "category": "needed",
        "owner": seed.get("owner") or c.owner,
        "what_it_does": text,
        "break_risk": "high" if c.kind == "must_allow" else seed.get("break_risk") or "medium",
        "break_note": seed.get("break_note") or "Blocking it may stop an app, site or device from working.",
        "confidence": "medium" if c.kind == "popular" else "high",
        "sources": list(c.sources),
        "explained_by": "must_allow" if c.kind == "must_allow" else "clean_list",
    }
