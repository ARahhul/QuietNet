import httpx
import pytest
from fastapi.testclient import TestClient

from pahra.adguard.client import AdGuardClient
from pahra.engine import Engine
from pahra.main import create_app

H = {"Authorization": "Bearer dev-token"}


@pytest.fixture
def api(settings, fake_app):
    ag = AdGuardClient("http://fake", "a", "b", transport=httpx.ASGITransport(app=fake_app))
    eng = Engine(settings, ag=ag)
    app = create_app(settings, eng, background=False)
    with TestClient(app) as c:
        c.engine = eng
        yield c


def test_requires_token(api):
    assert api.get("/api/devices").status_code == 401
    assert api.get("/api/devices", params={"token": "dev-token"}).status_code == 200


async def _drive(api, fake):
    # simulate a poll + pipeline run (background tasks are off in tests)
    obs = await api.engine.poller.poll_once()
    await api.engine.pipeline.process(obs)


def test_full_flow_block_all_and_undo(api, fake):
    import asyncio

    r = api.post("/api/devices", json={"name": "Rahhul LG TV", "type": "tv", "brand": "LG"}, headers=H)
    assert r.status_code == 201, r.text
    tv = r.json()["device"]
    assert tv["client_id"] == "rahhul-lg-tv" and tv["setup_method"] == "ipv4_demo"
    assert "rahhul-lg-tv" in fake.clients  # persistent client created in AdGuard
    phone = api.post("/api/devices", json={"name": "Rahhul Phone", "type": "phone", "brand": "Android"}, headers=H).json()
    assert phone["setup"]["values"]["dot_hostname"] == "rahhul-phone.dns.pahra.app"
    assert set(fake.access["allowed_clients"]) == {"rahhul-lg-tv", "rahhul-phone"}

    for q in ["tkacr.alphonso.tv", "us.ad.lgsmartad.com", "api-global.netflix.com", "googleads.g.doubleclick.net"]:
        fake.record("rahhul-lg-tv", q)
    fake.record("rahhul-phone", "app-measurement.com")
    asyncio.run(_drive(api, fake))

    cards = api.get("/api/suggestions", headers=H).json()
    domains = [c["domain"] for c in cards]
    assert domains[0] == "alphonso.tv"  # ACR tracker sorts first
    assert not any("netflix" in d for d in domains)
    assert all(c["sources"] for c in cards if c["confidence"] == "high")

    devs = api.get("/api/devices", headers=H).json()
    assert next(d for d in devs if d["client_id"] == "rahhul-lg-tv")["risk_score"] > 0

    r = api.post("/api/decisions/bulk", json={}, headers=H).json()
    assert r["synced"] and r["count"] >= 3
    assert fake.match("alphonso.tv", "rahhul-lg-tv")[0]
    assert fake.match("app-measurement.com", "rahhul-phone")[0]
    assert not fake.match("alphonso.tv", "someone-else")[0]
    assert api.get("/api/suggestions", headers=H).json() == []

    api.post("/api/decisions/undo-bulk", json={"ids": [d["id"] for d in r["decisions"]]}, headers=H)
    assert not fake.match("alphonso.tv", "rahhul-lg-tv")[0]
    assert len(api.get("/api/suggestions", headers=H).json()) == len(cards)

    # "Block all (N)" blocks only the N cards on screen, not ones that arrived after the page rendered
    r = api.post("/api/decisions/bulk", json={"domains": ["alphonso.tv"]}, headers=H).json()
    assert [d["domain"] for d in r["decisions"]] == ["alphonso.tv"]
    assert not fake.match("app-measurement.com", "rahhul-phone")[0]
    api.post("/api/decisions/undo-bulk", json={"ids": [d["id"] for d in r["decisions"]]}, headers=H)

    acts = api.get(f"/api/devices/{tv['id']}/activity", headers=H).json()
    assert any(g["owner"].startswith("Alphonso") for g in acts)
    alerts = api.get("/api/alerts", headers=H).json()
    assert any(a["type"] == "acr_tracker" for a in alerts)
    rep = api.get("/api/report/weekly", headers=H).json()
    assert rep["queries"] >= 5 and rep["top_collectors"]


def test_must_allow_warning(api):
    api.post("/api/devices", json={"name": "tv", "type": "tv"}, headers=H)
    r = api.post("/api/decisions", json={"domain": "netflix.com", "action": "block"}, headers=H)
    assert r.status_code == 409 and r.json()["detail"]["warning"] == "must_allow"
    r = api.post("/api/decisions", json={"domain": "netflix.com", "action": "block", "confirm_must_allow": True}, headers=H)
    assert r.status_code == 201


def test_linked_ip_guards(api):
    d = api.post("/api/devices", json={"name": "home router", "type": "router"}, headers=H).json()["device"]
    url = f"/api/devices/{d['id']}"
    assert api.patch(url, json={"linked_ip": "100.70.1.2"}, headers=H).status_code == 422  # CGNAT
    assert api.patch(url, json={"linked_ip": "192.168.1.5"}, headers=H).status_code == 422  # LAN
    assert api.patch(url, json={"linked_ip": "49.36.10.20"}, headers=H).status_code == 409  # shared IPv4 warning
    r = api.patch(url, json={"linked_ip": "49.36.10.20", "allow_shared_ipv4": True}, headers=H)
    assert r.status_code == 200 and r.json()["device"]["linked_ip"] == "49.36.10.20"
    r = api.patch(url, json={"linked_ip": "2401:4900:1c00::1"}, headers=H)
    assert r.status_code == 200


def test_mobileconfig(api):
    d = api.post("/api/devices", json={"name": "Rahhul iPhone", "type": "phone", "brand": "Apple"}, headers=H).json()
    assert d["device"]["setup_method"] == "doh"
    r = api.get(f"/api/devices/{d['device']['id']}/profile.mobileconfig", params={"token": "dev-token"})
    assert r.status_code == 200 and "https://dns.pahra.app/dns-query/rahhul-iphone" in r.text


def test_health(api):
    h = api.get("/api/health", headers=H).json()
    assert h["adguard"]["ok"] and h["lists"]["loaded_from"] == "bundled sample"
