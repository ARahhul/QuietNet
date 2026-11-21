"""Stage 3: in-memory fast checks (lists, name patterns, random-looking names)."""

import math
import re
from collections import Counter
from dataclasses import asdict, dataclass, field

import tldextract

from .lists import ListIndex

_extract = tldextract.TLDExtract(suffix_list_urls=())

PATTERN_TOKENS = {
    "ad", "ads", "adserver", "adservice", "track", "tracker", "tracking", "metrics", "metric",
    "telemetry", "acr", "analytics", "stats", "pixel", "beacon", "log", "logs", "events",
}
AFFIX_TOKENS = ("acr", "telemetry", "metrics", "analytics", "tracking")
SUSPICIOUS_TLDS ={"top", "xyz", "click", "zip", "mov", "gq", "tk", "ml", "cf", "ga", "work", "rest", "cyou", "sbs"}
_TOKEN_SPLIT = re.compile(r"[-_.\d]+")


def shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts = Counter(s)
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


def dga_score(fqdn: str) -> float:
    """0..1 heuristic for algorithmically generated names (possible malware)."""
    ext = _extract(fqdn)
    label = ext.domain.lower()
    if len(label) < 10:
        return 0.0
    ent = shannon_entropy(label)
    vowels = sum(ch in "aeiou" for ch in label) / len(label)
    digits = sum(ch.isdigit() for ch in label) / len(label)
    score = 0.0
    if ent >= 3.3:
        score += 0.45
    if vowels < 0.25:
        score += 0.25
    if 0.1 <= digits <= 0.6:
        score += 0.15
    if ext.suffix in SUSPICIOUS_TLDS:
        score += 0.15
    return min(score, 1.0)


def pattern_hits(*names: str) -> list[str]:
    hits = set()
    for n in names:
        ext = _extract(n)
        for label in (ext.subdomain.split(".") if ext.subdomain else []):
            for tok in _TOKEN_SPLIT.split(label.lower()):
                if tok in PATTERN_TOKENS:
                    hits.add(tok)
                    continue
                for s in AFFIX_TOKENS:  # glued names: "tkacr", "telemetryhub", "lgmetrics"
                    if tok.endswith(s) or (s != "acr" and tok.startswith(s)):
                        hits.add(s)
    return sorted(hits)


@dataclass
class FastResult:
    domain: str
    list_hits: list[str] = field(default_factory=list)     # non-threat lists
    threat_hits: list[str] = field(default_factory=list)
    patterns: list[str] = field(default_factory=list)
    dga: float = 0.0
    tracker_owner: str | None = None
    tracker_category: str | None = None
    tracker_confirmed: bool = False                        # TDS default=block
    entity: str | None = None

    @property
    def known(self) -> bool:
        return bool(self.list_hits or self.threat_hits or self.tracker_owner or self.entity)

    @property
    def suspicious_unknown(self) -> bool:
        """Brand-new and suspicious, but not on any list -> quarantine until judged (SPEC F8)."""
        if self.threat_hits:
            return True
        if self.known:
            return False
        return self.dga >= 0.6 or bool(set(self.patterns) & {"acr", "telemetry", "beacon", "tracking", "track"})

    def signals(self) -> dict:
        d = asdict(self)
        d.pop("domain")
        return d


def fast_check(idx: ListIndex, domain: str, qnames: list[str] | tuple[str, ...] = ()) -> FastResult:
    names = (domain, *qnames)
    hits = idx.list_hits(*names)
    threats = [h for h in hits if idx.kinds.get(h) == "threat"]
    t = idx.tracker(*names)
    return FastResult(
        domain=domain,
        list_hits=[h for h in hits if h not in threats],
        threat_hits=threats,
        patterns=pattern_hits(*names),
        dga=max((dga_score(n) for n in names), default=0.0),
        tracker_owner=t.owner if t else None,
        tracker_category=t.pahra_category if t and t.blocked_by_ddg else None,
        tracker_confirmed=bool(t and t.blocked_by_ddg),
        entity=idx.entity(*names),
    )
