"""Stage 5: batched Gemini Flash explanations with a strict JSON schema.

Only the domain name and public clues are sent (PRD §8). If no API key is set, or the
call fails / hits the free-tier quota, we fall back to curated seed text or list-derived
text so the pipeline never stalls.
"""

import json
import logging
from typing import Literal

from pydantic import BaseModel

from ..knowledge import seed_verdict
from .clues import Clues
from .confidence import UNCONFIRMED_TEXT

log = logging.getLogger("pahra.explainer")

Category = Literal["ads", "tracking", "telemetry", "needed", "malware", "unknown"]
Risk = Literal["low", "medium", "high"]


class DomainVerdict(BaseModel):
    domain: str
    category: Category
    owner: str  # empty string when not confirmed
    what_it_does: str
    break_risk: Risk
    break_note: str


SYSTEM = """You explain internet domains to non-technical households in India.
For each domain you get public clues (blocklists it is on, DuckDuckGo Tracker Radar owner/category,
domain age, name patterns). Return one JSON object per domain.
Rules:
- what_it_does: ONE plain sentence, max 22 words, no jargon (say "ads", "tracks what you watch", "sends usage data").
- Only state a purpose the clues support or that is widely known about this exact domain.
  If you are not sure, set category "unknown" and what_it_does "We couldn't confirm what this does."
- owner: the company only if confirmed by the clues or widely known for this exact domain, else "".
- category: ads | tracking | telemetry (device maker collecting usage data) | needed (required for apps/devices to work) | malware | unknown.
- break_risk: what happens if blocked. low = nothing visible breaks; medium = a feature may stop; high = app/device stops working.
- break_note: short, e.g. "TV and apps keep working."
"""


def fallback_verdict(c: Clues) -> dict:
    """Seed text, else something honest derived from the lists alone."""
    seed = seed_verdict(c.domain)
    if seed:
        return {k: seed.get(k) for k in ("domain", "category", "owner", "what_it_does", "break_risk", "break_note")} | {
            "explained_by": "seed"
        }
    f = c.fast
    cat = "malware" if f.threat_hits else f.tracker_category or (
        "telemetry" if any("HaGeZi" in h and "Pro" not in h for h in f.list_hits)
        else "tracking" if f.list_hits else "unknown"
    )
    text = {
        "malware": "Listed in threat-intelligence feeds as malicious (malware or phishing).",
        "ads": "Serves or measures advertising.",
        "tracking": "Collects data about what you do, for advertising or analytics.",
        "telemetry": "Sends usage data from the device back to its maker.",
        "needed": "Used to deliver content for apps and websites.",
        "unknown": UNCONFIRMED_TEXT,
    }[cat]
    return {
        "domain": c.domain,
        "category": cat,
        "owner": f.tracker_owner or f.entity,
        "what_it_does": text,
        "break_risk": "low" if cat in ("ads", "tracking", "telemetry", "malware") else "medium",
        "break_note": "Most devices keep working." if cat != "unknown" else "",
        "explained_by": "lists",
    }


class Explainer:
    def __init__(self, api_key: str, model: str, batch_size: int = 20):
        self.model = model
        self.batch_size = batch_size
        self.client = None
        self.last_error: str | None = None
        if api_key:
            from google import genai

            self.client = genai.Client(api_key=api_key)

    @property
    def enabled(self) -> bool:
        return self.client is not None

    async def _call(self, batch: list[Clues]) -> dict[str, dict]:
        from google.genai import types

        payload = json.dumps([c.for_llm() for c in batch], ensure_ascii=False)
        resp = await self.client.aio.models.generate_content(  # type: ignore[union-attr]
            model=self.model,
            contents=f"Explain these domains:\n{payload}",
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM,
                response_mime_type="application/json",
                response_schema=list[DomainVerdict],
                temperature=0.2,
            ),
        )
        parsed = resp.parsed or [DomainVerdict.model_validate(x) for x in json.loads(resp.text or "[]")]
        out = {}
        for v in parsed:
            d = v.model_dump()
            d["owner"] = d["owner"] or None
            d["explained_by"] = "gemini"
            out[d["domain"].lower()] = d
        return out

    async def explain(self, clues: list[Clues]) -> dict[str, dict]:
        """domain -> verdict dict (category, owner, what_it_does, break_risk, break_note, explained_by)."""
        results: dict[str, dict] = {}
        need_ai: list[Clues] = []
        for c in clues:
            seed = seed_verdict(c.domain)
            if seed or not self.enabled:
                results[c.domain] = fallback_verdict(c)
            else:
                need_ai.append(c)
        for i in range(0, len(need_ai), self.batch_size):
            batch = need_ai[i : i + self.batch_size]
            try:
                got = await self._call(batch)
                self.last_error = None
            except Exception as e:  # quota, network, schema errors -> honest fallback
                self.last_error = f"{e.__class__.__name__}: {e}"[:300]
                log.warning("Gemini batch failed, using list-based text: %s", self.last_error)
                got = {}
            for c in batch:
                results[c.domain] = got.get(c.domain) or fallback_verdict(c)
        return results
