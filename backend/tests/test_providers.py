"""Setup → Home WiFi: curated provider data, provider detection and AI guides for the rest."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from pahra import providers
from pahra.adguard.client import AdGuardClient
from pahra.engine import Engine
from pahra.main import create_app
from pahra.pipeline.explainer import Explainer

H = {"Authorization": "Bearer dev-token"}


def _ai(answer: dict | None, calls: list | None = None) -> Explainer:
    """An OpenAI-compatible explainer whose model always gives `answer`."""

    def handler(req: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(json.loads(req.content))
        if answer is None:
            return httpx.Response(500, text="down")
        content = json.dumps(answer)
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}}]})

    return Explainer("k", "m", base_url="https://gw.example/v1", transport=httpx.MockTransport(handler))


@pytest.fixture
def client(settings, fake_app):
    ag = AdGuardClient("http://fake", "a", "b", transport=httpx.ASGITransport(app=fake_app))
    eng = Engine(settings, ag=ag)
    with TestClient(create_app(settings, eng, background=False), client=("106.51.0.1", 50000)) as c:
        c.engine = eng
        yield c


# ---------------------------------------------------------------- curated data


def test_catalog_serves_guides_without_matching_internals(client):
    r = client.get("/api/providers", headers=H).json()
    jio = next(i for i in r["isps"] if i["key"] == "jio")
    assert jio["admin_urls"] == ["http://192.168.29.1"] and jio["source"] == "curated"
    assert "asns" not in jio and "aliases" not in jio and "as_match" not in jio
    assert {x["key"] for x in r["routers"]} >= {"asus", "tplink", "glinet"}


@pytest.mark.parametrize(
    ("typed", "key"),
    [("Airtel Xstream Fiber", "airtel"), ("act", "act"), ("BSNL FTTH", "bsnl"), ("Exact Broadband", None), ("Tata Communications", None)],
)
def test_typed_provider_names(typed, key):
    got = providers.find_isp(typed)
    assert (got["key"] if got else None) == key


def test_typed_router_models():
    assert providers.find_router("TP-Link Archer C6")["key"] == "tplink"
    assert providers.find_router("gl.inet Flint 2")["key"] == "glinet"
    assert providers.find_router("Linksys E5600") is None


def test_networks_map_to_providers():
    assert providers.match_network(24309, "")["key"] == "act"
    assert providers.match_network(None, "RELIANCEJIO-IN - Reliance Jio Infocomm Limited, IN")["key"] == "jio"
    assert providers.match_network(4755, "TATACOMM-AS - TATA Communications formerly VSNL, IN") is None
    assert providers.pretty_network("CABLELITE-AS-AP - Atria Convergence Technologies Pvt. Ltd., IN") == (
        "Atria Convergence Technologies Pvt. Ltd."
    )


# ---------------------------------------------------------------- detection


async def test_network_lookup_over_doh():
    providers._net_cache.clear()
    answers = {
        "1.0.51.106.origin.asn.cymru.com": '"24309 | 106.51.0.0/22 | IN | apnic | 2011-03-24"',
        "AS24309.asn.cymru.com": '"24309 | IN | apnic | 2004-12-08 | CABLELITE-AS-AP - Atria Convergence Technologies Pvt. Ltd., IN"',
    }

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["accept"] == "application/dns-json" and req.url.params["type"] == "TXT"
        data = answers[req.url.params["name"]]
        return httpx.Response(200, json={"Status": 0, "Answer": [{"name": "x", "type": 16, "data": data}]})

    net = await providers.lookup_network("106.51.0.1", transport=httpx.MockTransport(handler))
    assert net == {"asn": 24309, "country": "IN", "as_name": "CABLELITE-AS-AP - Atria Convergence Technologies Pvt. Ltd., IN"}
    assert await providers.lookup_network("192.168.1.5") is None  # never asks about private addresses


def test_detects_the_visitors_provider(client, monkeypatch):
    seen = []

    async def fake_lookup(ip, **_):
        seen.append(ip)
        return {"asn": 24309, "country": "IN", "as_name": "CABLELITE-AS-AP - Atria Convergence Technologies Pvt. Ltd., IN"}

    monkeypatch.setattr(providers, "lookup_network", fake_lookup)
    r = client.get("/api/network/provider", headers=H).json()
    assert seen == ["106.51.0.1"]  # the visitor's address, not the server's
    assert r["found"] and r["isp"]["key"] == "act" and r["network"] == "ACT Fibernet"


def test_unknown_network_is_named_but_has_no_guide(client, monkeypatch):
    async def fake_lookup(ip, **_):
        return {"asn": 64500, "country": "IN", "as_name": "EXAMPLENET-AS - Example Cable Networks Pvt Ltd, IN"}

    monkeypatch.setattr(providers, "lookup_network", fake_lookup)
    r = client.get("/api/network/provider", headers=H).json()
    assert r["found"] and r["isp"] is None and r["network"] == "Example Cable Networks Pvt Ltd"


def test_detection_can_be_turned_off(client, monkeypatch):
    client.engine.settings.provider_lookup = False

    async def boom(ip, **_):
        raise AssertionError("must not look up")

    monkeypatch.setattr(providers, "lookup_network", boom)
    assert client.get("/api/network/provider", headers=H).json()["found"] is False
    client.engine.settings.provider_lookup = True


# ---------------------------------------------------------------- guides for any provider / router

ISP_ANSWER = {
    "known": True,
    "name": "Example Cable",
    "short": "Example",
    "admin_urls": ["192.168.10.1", "http://evil.example.com/login", "http://127.0.0.1"],
    "logins": [{"user": "admin", "pass": "1234", "on": "their EC-100 box"}, {"pass": "no user"}],
    "dns_locked": "sometimes",  # not a known value
    "dns_note": "Some boxes lock it.",
    "cgnat": "often",
    "help_from": "the Example app",
    "ask_for": ["Ask for the admin login."],
    "own_ip": "",
}


def test_curated_provider_is_served_without_ai(client):
    calls = []
    client.engine.explainer = _ai(ISP_ANSWER, calls)
    g = client.post("/api/providers/guide", headers=H, json={"kind": "isp", "name": "airtel fiber"}).json()
    assert g["key"] == "airtel" and g["source"] == "curated" and calls == []


def test_ai_guide_is_checked_and_cached(client):
    calls = []
    client.engine.explainer = _ai(ISP_ANSWER, calls)
    g = client.post("/api/providers/guide", headers=H, json={"kind": "isp", "name": "Example Cable"}).json()
    assert g["source"] == "ai" and g["name"] == "Example Cable" and g["short"] == "Example"
    assert g["admin_urls"] == ["http://192.168.10.1"]  # public and loopback addresses dropped
    assert g["logins"] == [{"user": "admin", "pass": "1234", "on": "their EC-100 box"}]
    assert g["dns_locked"] == "varies" and g["cgnat"] == "often" and g["own_ip"] is None
    assert "Example Cable" in calls[0]["messages"][1]["content"]

    again = client.post("/api/providers/guide", headers=H, json={"kind": "isp", "name": "example  cable"}).json()
    assert again == g and len(calls) == 1  # served from the cache


def test_unknown_to_the_ai_gets_the_generic_guide(client):
    client.engine.explainer = _ai({"known": False})
    g = client.post("/api/providers/guide", headers=H, json={"kind": "isp", "name": "Raju Cable Net"}).json()
    assert g["source"] == "generic" and g["short"] == "Raju Cable Net"
    assert "Raju Cable Net" in g["ask_for"][0] and g["admin_urls"]


def test_ai_failure_falls_back_and_is_not_cached(client):
    calls = []
    client.engine.explainer = _ai(None, calls)
    g = client.post("/api/providers/guide", headers=H, json={"kind": "isp", "name": "Flaky Net"}).json()
    assert g["source"] == "generic"
    assert client.engine.conn.execute("SELECT COUNT(*) FROM provider_guides").fetchone()[0] == 0


def test_without_ai_key_everything_unknown_is_generic(client):
    g = client.post("/api/providers/guide", headers=H, json={"kind": "router", "name": "Linksys E5600"}).json()
    assert g["source"] == "generic" and g["brand"] == "Linksys E5600" and g["dot"] == "no"


def test_ai_router_guide(client):
    answer = {
        "known": True,
        "name": "Linksys E5600",
        "brand": "Linksys",
        "dot": "maybe",  # not a known value -> "no"
        "admin_url": "myrouter.local",
        "dns_menu": "Connectivity → Internet Settings",
        "login": "The password you set at first setup.",
    }
    client.engine.explainer = _ai(answer)
    g = client.post("/api/providers/guide", headers=H, json={"kind": "router", "name": "Linksys E5600"}).json()
    assert g["source"] == "ai" and g["brand"] == "Linksys" and g["dot"] == "no"
    assert g["admin_url"] == "http://myrouter.local" and g["dns_menu"].startswith("Connectivity")


def test_guide_rejects_blank_names(client):
    assert client.post("/api/providers/guide", headers=H, json={"kind": "isp", "name": "   "}).status_code == 422
