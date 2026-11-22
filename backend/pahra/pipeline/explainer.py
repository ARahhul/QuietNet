"""Stage 5: batched AI explanations with a strict JSON shape.

Two kinds of provider: Gemini (its own SDK, schema-enforced output), or any OpenAI-compatible
chat-completions endpoint (AICredits, OpenRouter, OpenAI...) given LLM_BASE_URL + LLM_API_KEY.

Only the domain name and public clues are sent (PRD §8). If no API key is set, or the
call fails / hits the free-tier quota, we fall back to curated seed text or list-derived
text so the pipeline never stalls.

Prompts come from pahra.prompts (via Langfuse when configured) and every model call is traced
(pahra.tracing).
"""

import asyncio
import base64
import json
import logging
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ValidationError

from ..knowledge import seed_verdict
from ..prompts import EXPLAIN_DOMAINS, PromptDef
from ..tracing import Prompt, Tracing, gemini_usage, openai_usage
from .clues import Clues
from .confidence import UNCONFIRMED_TEXT

log = logging.getLogger("pahra.explainer")

# Answers that mean "busy, try again shortly" rather than "this request is wrong"
BUSY = {429, 502, 503, 504}


def _retry_after(r, default: float) -> float:
    """The gateway's Retry-After (seconds), kept short: a person is waiting on the answer."""
    try:
        return min(max(float(r.headers.get("retry-after", default)), 0.0), 5.0)
    except ValueError:
        return default

# explained_by values that mean "an AI model wrote this text" (the UI shows "explained by AI")
AI_LABELS = ("gemini", "ai")

Category = Literal["ads", "tracking", "telemetry", "needed", "malware", "unknown"]
Risk = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class Image:
    """A picture to show the model (a screenshot the user pasted)."""

    mime: str  # image/png | image/jpeg | image/webp
    data: bytes

    def data_url(self) -> str:
        return f"data:{self.mime};base64,{base64.b64encode(self.data).decode()}"

    def describe(self) -> str:
        return f"screenshot: {self.mime}, {max(1, len(self.data) // 1024)} KB"


class DomainVerdict(BaseModel):
    domain: str
    category: Category
    owner: str  # empty string when not confirmed
    what_it_does: str
    break_risk: Risk
    break_note: str


def strip_fences(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        t = t.rsplit("```", 1)[0]
    return t.strip()


def parse_verdicts(text: str) -> list[DomainVerdict]:
    """Model reply -> verdicts. Tolerates ```json fences, a bare list, and skips malformed entries."""
    data = json.loads(strip_fences(text) or "[]")
    if isinstance(data, dict):
        data = data.get("domains", next((v for v in data.values() if isinstance(v, list)), []))
    out = []
    for x in data if isinstance(data, list) else []:
        if not isinstance(x, dict):
            continue
        x = {**x, "owner": x.get("owner") or "", "break_note": x.get("break_note") or ""}
        try:
            out.append(DomainVerdict.model_validate(x))
        except ValidationError:
            log.info("skipping malformed verdict for %s", x.get("domain"))
    return out


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
    def __init__(
        self,
        api_key: str,
        model: str,
        batch_size: int = 20,
        *,
        base_url: str = "",
        transport=None,
        tracing: Tracing | None = None,
    ):
        """With base_url, api_key belongs to that OpenAI-compatible endpoint; without, it's a Gemini key."""
        self.model = model
        self.batch_size = batch_size
        self.retry_delay = 1.5  # seconds before the one retry when the AI is busy
        self.client = None
        self.provider: str | None = None
        self.last_error: str | None = None
        self.tracing = tracing or Tracing()
        if api_key and base_url:
            import httpx

            self.client = httpx.AsyncClient(
                base_url=base_url.rstrip("/"),
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=90,
                transport=transport,
            )
            self.provider = "openai"
        elif api_key:
            from google import genai

            self.client = genai.Client(api_key=api_key)
            self.provider = "gemini"

    @property
    def enabled(self) -> bool:
        return self.client is not None

    @property
    def label(self) -> str:
        """explained_by value for text this explainer wrote."""
        return "ai" if self.provider == "openai" else "gemini"

    async def complete(
        self,
        d: PromptDef,
        *,
        schema=None,
        metadata: dict | None = None,
        images: list[Image] | None = None,
        **values,
    ) -> str:
        """Run one prompt on the configured model and return the reply text. Traced as a generation.

        images are attached to the user message. Traces show a placeholder, never the picture.
        """
        p = await self.tracing.prompt(d, **values)
        name = d.name.rsplit("/", 1)[-1]
        traced = p.messages + [{"role": "user", "content": f"[{i.describe()}]"} for i in images or []]
        with self.tracing.generation(
            name, model=self.model, prompt=p, input=traced, metadata={"provider": self.provider, **(metadata or {})}
        ) as gen:
            if self.provider == "openai":
                text, usage = await self._chat(p, images or [])
            else:
                text, usage = await self._gemini(p, schema, images or [])
            gen.update(output=text, usage_details=usage)
        return text

    async def _gemini(self, p: Prompt, schema=None, images: list[Image] = ()) -> tuple[str, dict]:
        from google.genai import types

        contents = [types.Part.from_bytes(data=i.data, mime_type=i.mime) for i in images] + [types.Part(text=p.user)]
        resp = await self.client.aio.models.generate_content(  # type: ignore[union-attr]
            model=self.model,
            contents=contents if images else p.user,
            config=types.GenerateContentConfig(
                system_instruction=p.system,
                response_mime_type="application/json",
                response_schema=schema,
                temperature=p.temperature,
            ),
        )
        return resp.text or "", gemini_usage(resp)

    async def _chat(self, p: Prompt, images: list[Image] = ()) -> tuple[str, dict]:
        """One JSON-mode chat completion on the OpenAI-compatible endpoint."""
        messages = p.messages
        if images:  # OpenAI vision format: the last user message becomes text + image parts
            last = max(i for i, m in enumerate(messages) if m["role"] == "user")
            parts = [{"type": "text", "text": messages[last]["content"]}]
            parts += [{"type": "image_url", "image_url": {"url": i.data_url()}} for i in images]
            messages = [*messages[:last], {"role": "user", "content": parts}, *messages[last + 1 :]]
        body = {"model": self.model, "messages": messages, "temperature": p.temperature, "response_format": {"type": "json_object"}}
        r = await self.client.post("/chat/completions", json=body)  # type: ignore[union-attr]
        if r.status_code in BUSY:
            # The gateway or its model is busy (rate limit, overload): one more try after a short pause.
            log.info("AI answered HTTP %s, trying once more", r.status_code)
            await asyncio.sleep(_retry_after(r, self.retry_delay))
            r = await self.client.post("/chat/completions", json=body)  # type: ignore[union-attr]
        if r.status_code == 400:
            # Some models/gateways reject JSON mode or a custom temperature: ask plainly instead.
            r = await self.client.post("/chat/completions", json={"model": self.model, "messages": messages})  # type: ignore[union-attr]
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        j = r.json()
        return j["choices"][0]["message"]["content"] or "", openai_usage(j)

    async def ask_json(self, d: PromptDef, *, images: list[Image] | None = None, **values) -> dict:
        """A one-off prompt whose answer is a JSON object (e.g. a router guide, a screenshot read).

        Raises when no key is set, the call fails, or the reply isn't a JSON object.
        """
        if not self.enabled:
            raise RuntimeError("no AI key set")
        data = json.loads(strip_fences(await self.complete(d, images=images, **values)) or "{}")
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return data

    async def _call(self, batch: list[Clues]) -> dict[str, dict]:
        payload = json.dumps([c.for_llm() for c in batch], ensure_ascii=False)
        text = await self.complete(
            EXPLAIN_DOMAINS, schema=list[DomainVerdict], metadata={"domains": len(batch)}, domains=payload
        )
        out = {}
        for v in parse_verdicts(text):
            d = v.model_dump()
            d["owner"] = d["owner"] or None
            d["explained_by"] = self.label
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
            with self.tracing.trace("explain-domains", input=[c.domain for c in batch], tags=("suggestions",)) as t:
                try:
                    got = await self._call(batch)
                    self.last_error = None
                except Exception as e:  # quota, network, schema errors -> honest fallback
                    self.last_error = f"{e.__class__.__name__}: {e}"[:300]
                    log.warning("AI batch failed, using list-based text: %s", self.last_error)
                    t.update(level="ERROR", status_message=self.last_error)
                    got = {}
                for c in batch:
                    results[c.domain] = got.get(c.domain) or fallback_verdict(c)
                t.update(output={d: results[d]["explained_by"] for d in (c.domain for c in batch)})
        return results
