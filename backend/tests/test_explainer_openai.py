"""The OpenAI-compatible explainer path (AICredits, OpenRouter, OpenAI...), against a mock endpoint."""

import json

import httpx

from pahra.pipeline.clues import Clues
from pahra.pipeline.explainer import Explainer, parse_verdicts
from pahra.pipeline.fastchecks import FastResult

VERDICT = {
    "domain": "metrics.example-tracker.net",
    "category": "tracking",
    "owner": "Example Tracker",
    "what_it_does": "Tracks how you use apps.",
    "break_risk": "low",
    "break_note": "Apps keep working.",
}


def _clues(domain: str) -> Clues:
    return Clues(domain=domain, fast=FastResult(domain=domain), domain_age_days=None, beacon=False, sample_names=[f"api.{domain}"])


def _reply(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}}]})


async def test_openai_compatible_call_and_label():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return _reply(json.dumps({"domains": [VERDICT]}))

    ex = Explainer("k-123", "openai/some-model", base_url="https://gw.example/v1/", transport=httpx.MockTransport(handler))
    assert ex.enabled and ex.label == "ai"
    out = await ex.explain([_clues("metrics.example-tracker.net")])

    assert out["metrics.example-tracker.net"]["explained_by"] == "ai"
    assert out["metrics.example-tracker.net"]["owner"] == "Example Tracker"
    req = seen[0]
    assert str(req.url) == "https://gw.example/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer k-123"
    body = json.loads(req.content)
    assert body["model"] == "openai/some-model" and body["response_format"] == {"type": "json_object"}
    assert "metrics.example-tracker.net" in body["messages"][1]["content"]


async def test_retries_plain_request_when_json_mode_is_rejected():
    bodies = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        bodies.append(body)
        if "response_format" in body:
            return httpx.Response(400, json={"error": {"message": "response_format not supported"}})
        return _reply("```json\n" + json.dumps([VERDICT]) + "\n```")

    ex = Explainer("k", "m", base_url="https://gw.example/v1", transport=httpx.MockTransport(handler))
    out = await ex.explain([_clues("metrics.example-tracker.net")])
    assert len(bodies) == 2 and "temperature" not in bodies[1]
    assert out["metrics.example-tracker.net"]["explained_by"] == "ai"


async def test_endpoint_error_falls_back_to_list_text():
    ex = Explainer(
        "bad", "m", base_url="https://gw.example/v1",
        transport=httpx.MockTransport(lambda req: httpx.Response(401, json={"error": "invalid key"})),
    )
    out = await ex.explain([_clues("metrics.example-tracker.net")])
    assert out["metrics.example-tracker.net"]["explained_by"] in ("lists", "seed")
    assert "HTTP 401" in ex.last_error


def test_parse_verdicts_skips_bad_entries_and_null_owner():
    text = json.dumps({"domains": [{**VERDICT, "owner": None}, {"domain": "x.com", "category": "nonsense"}]})
    got = parse_verdicts(text)
    assert [v.domain for v in got] == ["metrics.example-tracker.net"] and got[0].owner == ""
