"""Pre-explain domains before a demo, so cards show finished text instantly (no Gemini quota risk on stage).

    python -m devtools.prewarm                    # the demo TV/phone profiles + everything seen in the last 7 days
    python -m devtools.prewarm --file my.txt      # also these domains (one per line)
    python -m devtools.prewarm --force            # re-explain domains that are already cached

Uses the same stages as the live pipeline (fast checks, public clues, Gemini or list/seed text, then the
deterministic confidence rules) and only writes the shared `domains` cache. It never creates decisions,
quarantines or rules. Reads settings from backend/.env like the API does.
"""

import argparse
import asyncio
import json
from collections import Counter

from pahra.config import get_settings
from pahra.engine import Engine
from pahra.knowledge import is_acr, is_must_allow_any_device
from pahra.pipeline.confidence import apply_confidence
from pahra.pipeline.fastchecks import fast_check
from pahra.pipeline.normalize import normalize_domain


def demo_domains() -> set[str]:
    from devtools.fake_adguard import PROFILES

    return {q for profile in PROFILES.values() for q, _ in profile}


async def prewarm(eng: Engine, qnames: set[str], force: bool = False, batch: int = 20) -> Counter:
    by_domain: dict[str, set[str]] = {}
    for q in qnames:
        q = q.strip().lower().rstrip(".")
        if q and not q.startswith("#"):
            by_domain.setdefault(normalize_domain(q), set()).add(q)
    todo = sorted(d for d in by_domain if force or not eng.conn.execute("SELECT 1 FROM domains WHERE domain = ?", (d,)).fetchone())
    stats: Counter = Counter(skipped_cached=len(by_domain) - len(todo))
    for i in range(0, len(todo), batch):
        chunk = todo[i : i + batch]
        clue_list = await eng.clues.gather([(fast_check(eng.lists, d, sorted(by_domain[d])), sorted(by_domain[d])) for d in chunk])
        verdicts = await eng.explainer.explain(clue_list)
        for c in clue_list:
            v = apply_confidence(verdicts[c.domain], c.fast, c.beacon, is_must_allow_any_device(c.domain))
            signals = c.fast.signals() | {"beacon": c.beacon, "domain_age_days": c.domain_age_days,
                                          "acr": is_acr(c.domain), "suspicious_unknown": c.fast.suspicious_unknown}
            eng.pipeline.save_domain(c.domain, v, signals)
            stats[f"explained_by_{v.get('explained_by') or 'unknown'}"] += 1
            stats[f"confidence_{v.get('confidence') or 'low'}"] += 1
    return stats


async def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--file", help="extra domains, one per line")
    p.add_argument("--no-demo", action="store_true", help="skip the built-in demo device profiles")
    p.add_argument("--no-db", action="store_true", help="skip domains already seen in the events table")
    p.add_argument("--force", action="store_true", help="re-explain cached domains too")
    a = p.parse_args()

    eng = Engine(get_settings())
    names: set[str] = set() if a.no_demo else demo_domains()
    if not a.no_db:
        names |= {r["domain"] for r in eng.conn.execute("SELECT DISTINCT domain FROM events")}
    if a.file:
        with open(a.file, encoding="utf-8") as f:
            names |= set(f.read().split())
    print(f"Gemini: {'on (' + eng.settings.gemini_model + ')' if eng.explainer.enabled else 'off: list/seed text only'}")
    stats = await prewarm(eng, names, a.force)
    print(json.dumps(dict(stats), indent=2))
    if eng.explainer.last_error:
        print("Gemini error (fell back to list text):", eng.explainer.last_error)


if __name__ == "__main__":
    asyncio.run(main())
