"""Static hand-checked knowledge: must-allow lists, seed explanations, fix guides."""

from functools import lru_cache
from pathlib import Path

import yaml

from .pipeline.lists import parents

DATA = Path(__file__).with_name("data")


@lru_cache
def _yaml(name: str) -> dict:
    return yaml.safe_load((DATA / name).read_text(encoding="utf-8")) or {}


def must_allow_set(device_type: str | None) -> set[str]:
    d = _yaml("must_allow.yaml")
    out = set(d.get("all") or [])
    if device_type:
        out |= set(d.get(device_type) or [])
    return out


def is_must_allow(domain: str, device_type: str | None = None, qnames: tuple[str, ...] = ()) -> bool:
    allow = must_allow_set(device_type)
    return any(p in allow for n in (domain, *qnames) for p in parents(n))


def is_must_allow_any_device(domain: str) -> bool:
    d = _yaml("must_allow.yaml")
    allow = {x for v in d.values() for x in (v or [])}
    return any(p in allow for p in parents(domain))


def seed_verdict(domain: str) -> dict | None:
    seeds = _yaml("seed_domains.yaml")
    for p in parents(domain):
        if p in seeds:
            return {"domain": domain, **seeds[p]}
    return None


def is_acr(domain: str) -> bool:
    s = seed_verdict(domain)
    return bool(s and s.get("acr"))


def fix_guide(*, type_: str | None = None, acr: bool = False, brand: str | None = None) -> dict | None:
    """Most specific matching guide wins (more match keys = more specific)."""
    best, best_score = None, -1
    for key, g in _yaml("fix_guides.yaml").items():
        m = g.get("match") or {}
        ok = True
        if "type" in m and m["type"] != type_:
            ok = False
        if "acr" in m and bool(m["acr"]) != acr:
            ok = False
        if "brand" in m and (brand or "").lower() != str(m["brand"]).lower():
            ok = False
        if ok and len(m) > best_score:
            best, best_score = {"id": key, **g}, len(m)
    return best
