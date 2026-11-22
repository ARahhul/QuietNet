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


def test_api_docs_can_be_turned_off(settings, fake_app):
    for docs_on in (True, False):
        ag = AdGuardClient("http://fake", "a", "b", transport=httpx.ASGITransport(app=fake_app))
        s = settings.model_copy(update={"api_docs": docs_on})
        with TestClient(create_app(s, Engine(s, ag=ag), background=False)) as c:
            codes = [c.get(p).status_code for p in ("/docs", "/redoc", "/openapi.json")]
        assert codes == ([200] * 3 if docs_on else [404] * 3)


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


def test_blocked_list(api, fake):
    import asyncio

    tv = api.post("/api/devices", json={"name": "Living room TV", "type": "tv", "brand": "LG"}, headers=H).json()["device"]
    phone = api.post("/api/devices", json={"name": "Phone", "type": "phone"}, headers=H).json()["device"]
    for _ in range(3):
        fake.record("living-room-tv", "tkacr.alphonso.tv")
    fake.record("phone", "app-measurement.com")
    asyncio.run(_drive(api, fake))
    assert api.get("/api/blocked", headers=H).json() == []

    hh = api.post("/api/decisions", json={"domain": "alphonso.tv", "action": "block"}, headers=H).json()["decision"]
    api.post("/api/decisions", json={"domain": "app-measurement.com", "action": "block", "device_id": phone["id"]}, headers=H)
    # a per-device allow exempts the phone from the household block
    api.post("/api/decisions", json={"domain": "alphonso.tv", "action": "allow", "device_id": phone["id"]}, headers=H)
    # allows never show up in the blocked list
    api.post("/api/decisions", json={"domain": "example.org", "action": "allow"}, headers=H)

    rows = {b["domain"]: b for b in api.get("/api/blocked", headers=H).json()}
    assert set(rows) == {"alphonso.tv", "app-measurement.com"}
    a = rows["alphonso.tv"]
    assert a["device"] is None and a["allowed_on"] == [{"id": phone["id"], "name": "Phone"}]
    assert a["owner"] and a["category"] in ("tracking", "telemetry", "ads")
    assert a["action"] == "block" and a["decided_by"] == "user"
    m = rows["app-measurement.com"]
    assert m["device"] == {"id": phone["id"], "name": "Phone", "type": "phone"} and m["allowed_on"] == []
    assert tv["id"] != phone["id"]

    api.post(f"/api/decisions/{hh['id']}/undo", headers=H)
    assert [b["domain"] for b in api.get("/api/blocked", headers=H).json()] == ["app-measurement.com"]


def test_live_lookups_and_analytics(api, fake):
    import asyncio
    import time

    from pahra.poller import prune_queries

    tv = api.post("/api/devices", json={"name": "Living room TV", "type": "tv", "brand": "LG"}, headers=H).json()["device"]
    phone = api.post("/api/devices", json={"name": "Phone", "type": "phone"}, headers=H).json()["device"]
    api.post("/api/decisions", json={"domain": "alphonso.tv", "action": "block"}, headers=H)

    from datetime import datetime, timedelta, timezone

    t0 = datetime.now(timezone.utc) - timedelta(seconds=10)
    fake.record("living-room-tv", "tkacr.alphonso.tv", t0)  # blocked by our rule
    fake.record("living-room-tv", "api-global.netflix.com", t0 + timedelta(seconds=1), qtype="AAAA")
    fake.record("living-room-tv", "api-global.netflix.com", t0 + timedelta(seconds=2), cached=True)
    fake.record("phone", "www.google.com", t0 + timedelta(seconds=3), upstream="tls://1.0.0.1")
    asyncio.run(_drive(api, fake))

    live = api.get("/api/live/queries", headers=H).json()
    rows = live["queries"]
    assert len(rows) == 4 and live["last_poll"]
    assert rows[0]["qname"] == "www.google.com" and rows[0]["device"]["name"] == "Phone"  # newest first
    acr = next(r for r in rows if r["qname"] == "tkacr.alphonso.tv")
    assert acr["status"] == "blocked" and acr["blocked_by"] == "rule" and acr["domain"] == "alphonso.tv"
    assert {r["status"] for r in rows} == {"blocked", "allowed", "cached"}

    def qnames(**params):
        return [r["qname"] for r in api.get("/api/live/queries", params=params, headers=H).json()["queries"]]

    assert qnames(status="blocked") == ["tkacr.alphonso.tv"]
    assert len(qnames(status="allowed")) == 3  # cached answers count as allowed
    assert set(qnames(device_id=phone["id"])) == {"www.google.com"}
    assert qnames(q="netflix") == ["api-global.netflix.com"] * 2
    assert qnames(q="100%_") == []  # LIKE wildcards are literal

    clients = {c["name"]: c for c in api.get("/api/live/clients", headers=H).json()}
    tv_c = clients["Living room TV"]
    assert tv_c["queries"] == 3 and tv_c["blocked"] == 1 and tv_c["active"]
    assert len(tv_c["ips"]) == 1 and tv_c["ips"][0]["ip"]
    # filtering by address returns exactly the devices seen at it (two devices can share one, e.g. a home's public IP)
    tv_ip = tv_c["ips"][0]["ip"]
    at_ip = {c["id"] for c in clients.values() if any(i["ip"] == tv_ip for i in c["ips"])}
    by_ip = api.get("/api/live/queries", params={"ip": tv_ip}, headers=H).json()["queries"]
    assert tv["id"] in at_ip and {r["device"]["id"] for r in by_ip} == at_ip

    a = api.get("/api/report/analytics", headers=H).json()
    assert a["bucket_s"] == 600 and len(a["timeline"]["allowed"]) == 144
    t = a["totals"]
    assert (t["queries"], t["blocked"], t["blocked_by_rules"], t["cached"], t["active_devices"]) == (4, 1, 1, 1, 2)
    assert t["block_rules"] == 1
    assert sum(a["timeline"]["blocked"]) == 1 and sum(a["timeline"]["allowed"]) == 3
    assert [d["name"] for d in a["devices"]] == ["Living room TV", "Phone"]  # stable (creation) order for colours
    assert sum(a["devices"][0]["series"]) == 3
    assert {r["name"]: r["n"] for r in a["query_types"]} == {"A": 3, "AAAA": 1}
    assert {r["name"]: r["n"] for r in a["answered_by"]} == {"blocked": 1, "cache": 1, "tls://1.1.1.1": 1, "tls://1.0.0.1": 1}
    assert a["top_blocked"][0]["domain"] == "alphonso.tv" and a["top_blocked"][0]["n"] == 1
    netflix = next(r["domain"] for r in rows if "netflix" in r["qname"])
    assert a["top_allowed"][0]["domain"] == netflix and a["top_allowed"][0]["n"] == 2

    one = api.get("/api/report/analytics", params={"hours": 1, "device_id": phone["id"]}, headers=H).json()
    assert one["bucket_s"] == 60 and one["totals"]["queries"] == 1 and [d["name"] for d in one["devices"]] == ["Phone"]
    assert api.get("/api/report/analytics", params={"hours": 48}, headers=H).status_code == 422

    # lookups older than the retention window are dropped
    api.engine.conn.execute("UPDATE queries SET t = ? WHERE qname = 'www.google.com'", (time.time() - 25 * 3600,))
    assert prune_queries(api.engine.conn, 24) == 1
    assert len(api.get("/api/live/queries", headers=H).json()["queries"]) == 3
