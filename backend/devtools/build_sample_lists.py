"""Build `pahra/data/sample_lists/` from the REAL downloaded lists.

The bundled samples only contain entries that are genuinely on each upstream list,
so offline/dev cards never claim a source that isn't true.

    python -m devtools.build_sample_lists <cache_dir>
"""

import json
import sys
from pathlib import Path

from devtools.fake_adguard import PROFILES
from pahra.pipeline.lists import SAMPLE_DIR, SOURCES, _parse_domains, parents

EXTRA = ["doubleclick.net", "googlesyndication.com", "google-analytics.com", "facebook.net", "scorecardresearch.com",
         "samba.tv", "tvinteractive.tv", "inscape.tv", "adsmeasurement.com"]


def main(cache: Path) -> None:
    names = {fq for prof in PROFILES.values() for fq, _ in prof} | set(EXTRA)
    candidates = {p for n in names for p in parents(n) if "." in p}
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    for src in SOURCES:
        f = cache / f"{src.slug}.txt"
        if not f.exists():
            print("missing", f)
            continue
        real = _parse_domains(f.read_text(encoding="utf-8", errors="ignore"))
        keep = sorted(candidates & real)
        # keep a few hundred real entries too so the sample is not trivially small
        keep += sorted(real - set(keep))[:300]
        (SAMPLE_DIR / f"{src.slug}.txt").write_text(
            f"# Sample of {src.name} (subset of the real list, built by devtools/build_sample_lists.py)\n"
            + "\n".join(keep) + "\n",
            encoding="utf-8",
        )
        print(src.slug, len(keep), "hits:", sorted(candidates & real))
    tds = json.loads((cache / "tds.json").read_text(encoding="utf-8"))
    trackers = {k: v for k, v in tds["trackers"].items() if k in candidates}
    domains = {k: v for k, v in tds["domains"].items() if k in candidates}
    (SAMPLE_DIR / "tds.json").write_text(
        json.dumps({"trackers": {k: {"domain": v["domain"], "owner": v.get("owner"), "categories": v.get("categories", []), "default": v.get("default")}
                                 for k, v in trackers.items()}, "domains": domains}, indent=1),
        encoding="utf-8",
    )
    print("tds trackers:", sorted(trackers))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
