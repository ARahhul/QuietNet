"""Langfuse tracing of every AI call, and the prompts pushed to Langfuse Prompt Management."""

import json
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from langfuse import Langfuse
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from pahra.adguard.client import AdGuardClient
from pahra.assistant.service import AssistantService
from pahra.engine import Engine
from pahra.main import create_app
from pahra.pipeline.clues import Clues
from pahra.pipeline.explainer import Explainer
from pahra.pipeline.fastchecks import FastResult
from pahra.prompts import ALL_PROMPTS, ISP_GUIDE
from pahra.tracing import Tracing

from .stub_companion import FIRE, LG
from .test_assistant import FakeGemini, drive, env  # noqa: F401  (env is a fixture)

H = {"Authorization": "Bearer dev-token"}
VERDICT = {"domain": "ads.example.net", "category": "ads", "owner": "X", "what_it_does": "Ads.", "break_risk": "low", "break_note": ""}


class Recorder:
    """A Langfuse client whose spans land in memory instead of being sent."""

    def __init__(self):
        self.exporter = InMemorySpanExporter()
        self.lf = Langfuse(
            public_key=f"pk-lf-test-{uuid.uuid4().hex[:8]}",  # the SDK keeps one client per key
            secret_key="sk-lf-test",
            base_url="http://127.0.0.1:9",
            span_exporter=self.exporter,
            tracer_provider=TracerProvider(),
        )
        self.lf.get_prompt = self._offline  # use the code's prompt text; no network in tests
        self.tracing = Tracing(self.lf)

    @staticmethod
    def _offline(*a, **kw):
        raise ConnectionError("offline")

    def spans(self) -> list[dict]:
        self.lf.flush()
        out = []
        for sp in self.exporter.get_finished_spans():
            a = dict(sp.attributes or {})
            out.append({"name": sp.name, "root": sp.parent is None, "type": a.get("langfuse.observation.type"), **a})
        return out


@pytest.fixture
def rec():
    r = Recorder()
    yield r
    r.lf.shutdown()


def _openai(content: dict | None, calls: list | None = None, usage: dict | None = None) -> httpx.MockTransport:
    def handler(req: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(json.loads(req.content))
        if content is None:
            return httpx.Response(500, text="model down")
        body = {"choices": [{"message": {"role": "assistant", "content": json.dumps(content)}}]}
        if usage:
            body["usage"] = usage
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler)


def _clues(domain: str) -> Clues:
    return Clues(domain=domain, fast=FastResult(domain=domain), domain_age_days=None, beacon=False, sample_names=[])


async def test_domain_explanations_are_traced(rec):
    usage = {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70}
    ex = Explainer("k", "m-1", base_url="https://gw.example/v1", transport=_openai({"domains": [VERDICT]}, usage=usage), tracing=rec.tracing)
    out = await ex.explain([_clues("ads.example.net")])
    assert out["ads.example.net"]["explained_by"] == "ai"

    spans = rec.spans()
    root = next(s for s in spans if s["root"])
    gen = next(s for s in spans if s["type"] == "generation")
    assert root["langfuse.trace.name"] == "explain-domains" and "suggestions" in root["langfuse.trace.tags"]
    assert gen["name"] == "explain-domains" and gen["langfuse.observation.model.name"] == "m-1"
    sent = json.loads(gen["langfuse.observation.input"])
    assert sent[0]["role"] == "system" and "ads.example.net" in sent[1]["content"]
    assert "what_it_does" in gen["langfuse.observation.output"]
    assert json.loads(gen["langfuse.observation.usage_details"]) == {"input": 50, "output": 20, "total": 70}
    assert json.loads(gen["langfuse.observation.model.parameters"]) == {"temperature": 0.2}


async def test_failed_model_call_is_an_error_in_the_trace(rec):
    ex = Explainer("k", "m-1", base_url="https://gw.example/v1", transport=_openai(None), tracing=rec.tracing)
    out = await ex.explain([_clues("ads.example.net")])
    assert out["ads.example.net"]["explained_by"] == "lists"  # still falls back
    gen = next(s for s in rec.spans() if s["type"] == "generation")
    assert gen["langfuse.observation.level"] == "ERROR" and "500" in gen["langfuse.observation.status_message"]


def test_home_wifi_guides_are_traced(settings, fake_app, rec):
    ag = AdGuardClient("http://fake", "a", "b", transport=httpx.ASGITransport(app=fake_app))
    eng = Engine(settings, ag=ag)
    answer = {"known": True, "name": "Example Cable", "short": "Example", "admin_urls": ["192.168.10.1"], "dns_locked": "often", "cgnat": "often"}
    eng.explainer = Explainer("k", "m-2", base_url="https://gw.example/v1", transport=_openai(answer), tracing=rec.tracing)
    with TestClient(create_app(settings, eng, background=False)) as c:
        g = c.post("/api/providers/guide", headers=H, json={"kind": "isp", "name": "Example Cable"}).json()
    assert g["source"] == "ai"
    spans = rec.spans()
    root = next(s for s in spans if s["root"])
    gen = next(s for s in spans if s["type"] == "generation")
    assert root["langfuse.trace.name"] == "home-wifi-guide"
    assert json.loads(root["langfuse.observation.input"]) == {"kind": "isp", "name": "Example Cable"}
    assert json.loads(root["langfuse.observation.output"])["source"] == "ai"
    assert gen["name"] == "isp-guide" and "Internet provider: Example Cable" in gen["langfuse.observation.input"]


class EditedPrompt:
    """What get_prompt returns for a prompt someone edited in Langfuse."""

    name = ISP_GUIDE.name
    version = 7
    is_fallback = False
    labels = ["production"]
    tags: list = []
    config = {"temperature": 0.7}
    prompt = [{"role": "system", "content": "EDITED IN LANGFUSE"}, {"role": "user", "content": "Provider: {{name}}"}]

    def compile(self, **kw):
        return [{"role": m["role"], "content": m["content"].replace("{{name}}", kw["name"])} for m in self.prompt]


async def test_the_production_prompt_in_langfuse_is_what_runs(rec):
    rec.lf.get_prompt = lambda *a, **kw: EditedPrompt()
    calls = []
    ex = Explainer("k", "m-3", base_url="https://gw.example/v1", transport=_openai({"known": False}, calls), tracing=rec.tracing)
    await ex.ask_json(ISP_GUIDE, name="Foo Net")
    msgs = calls[0]["messages"]
    assert msgs == [{"role": "system", "content": "EDITED IN LANGFUSE"}, {"role": "user", "content": "Provider: Foo Net"}]
    assert calls[0]["temperature"] == 0.7  # the prompt's config in Langfuse
    gen = next(s for s in rec.spans() if s["type"] == "generation")
    assert gen["langfuse.observation.prompt.name"] == ISP_GUIDE.name and gen["langfuse.observation.prompt.version"] == 7


async def test_without_langfuse_the_code_prompt_is_used():
    calls = []
    ex = Explainer("k", "m", base_url="https://gw.example/v1", transport=_openai({"known": False}, calls))
    await ex.ask_json(ISP_GUIDE, name="Foo Net")
    assert calls[0]["messages"] == ISP_GUIDE.compile(name="Foo Net")


async def test_assistant_session_is_one_trace_without_addresses(env, rec):  # noqa: F811
    eng, srv, calls, _ = env
    eng.tracing = rec.tracing
    svc = AssistantService(eng, target=srv, genai_client=FakeGemini())
    await svc.start()
    s = svc.create("bedroom fire tv")
    done = await drive(s, ["that one", True, True])
    assert done["outcome"] == "connected"
    spans = rec.spans()
    root = next(x for x in spans if x["root"])
    assert root["type"] == "agent" and root["langfuse.trace.name"] == "tv-assistant" and root["session.id"] == s.id
    steps = [x for x in spans if x["name"] == "tv-assistant-step"]
    tools = {x["name"] for x in spans if x["type"] == "tool"}
    assert len(steps) >= 7 and {"scan_network", "adb_connect", "set_private_dns", "finish"} <= tools
    blob = json.dumps([{k: v for k, v in x.items() if isinstance(v, (str, int, float))} for x in spans])
    for secret in (FIRE["ip"], LG["ip"], FIRE["mac"], "192.168.29", "dns.pahra.app"):
        assert secret not in blob, secret
    await svc.stop()


# ---------------------------------------------------------------- pushing prompts to Langfuse


class NotFoundError(Exception):
    status_code = 404


class FakeLangfuse:
    """Just enough of Langfuse's prompt API: latest production version per name."""

    def __init__(self):
        self.store: dict[str, list[dict]] = {}

    def get_prompt(self, name, **kw):
        if name not in self.store:
            raise NotFoundError(name)
        v = self.store[name][-1]

        class P:  # like the real API, messages come back with an extra "type" field
            prompt = [{**m, "type": "chatmessage"} for m in v["prompt"]]
            config, version = v["config"], len(self.store[name])

        return P()

    def create_prompt(self, *, name, prompt, config, **kw):
        self.store.setdefault(name, []).append({"prompt": prompt, "config": config, "labels": kw["labels"]})

    def shutdown(self):
        pass


async def test_prompts_are_pushed_then_kept_in_sync(conn):
    fake = FakeLangfuse()
    t = Tracing(fake)
    r = await t.push_prompts(conn)
    assert set(r) == {p.name for p in ALL_PROMPTS} and set(r.values()) == {"created"}
    assert all(v[0]["labels"] == ["production"] for v in fake.store.values())

    assert set((await t.push_prompts(conn)).values()) == {"in sync (v1)"}

    # Someone edits a prompt in Langfuse: QuietNet leaves it alone.
    fake.store[ISP_GUIDE.name].append({"prompt": [{"role": "system", "content": "tuned"}], "config": {}, "labels": ["production"]})
    assert (await t.push_prompts(conn))[ISP_GUIDE.name] == "kept Langfuse's edit (v2)"
    # ...unless told to overwrite it.
    assert (await t.push_prompts(conn, update=True))[ISP_GUIDE.name] == "updated from code"
    assert fake.store[ISP_GUIDE.name][-1]["prompt"] == ISP_GUIDE.messages


async def test_code_changes_reach_langfuse_when_nobody_edited_there(conn, monkeypatch):
    fake = FakeLangfuse()
    t = Tracing(fake)
    await t.push_prompts(conn)
    monkeypatch.setattr(ISP_GUIDE, "messages", [{"role": "system", "content": "v2 from code"}, {"role": "user", "content": "{{name}}"}])
    assert (await t.push_prompts(conn))[ISP_GUIDE.name] == "updated from code"
    assert fake.store[ISP_GUIDE.name][-1]["prompt"][0]["content"] == "v2 from code"


def test_health_shows_tracing_state(settings, fake_app):
    ag = AdGuardClient("http://fake", "a", "b", transport=httpx.ASGITransport(app=fake_app))
    with TestClient(create_app(settings, Engine(settings, ag=ag), background=False)) as c:
        assert c.get("/api/health", headers=H).json()["tracing"] == {"enabled": False, "prompts": {}, "error": None}
