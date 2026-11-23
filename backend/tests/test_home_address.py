"""A home's public IP changes (ACT hands out a new one on every reconnect): QuietNet follows the router's Dynamic DNS name."""

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from pahra import home_address
from pahra.adguard.client import AdGuardClient
from pahra.engine import Engine
from pahra.main import create_app

H = {"Authorization": "Bearer dev-token"}
HOME = "49.36.1.1"
NAME = "myhome.tplinkdns.com"


@pytest.fixture
def dns(monkeypatch):
    """What each Dynamic DNS name points to right now (edit it to move a home)."""
    table: dict[str, list[str] | Exception] = {NAME: [HOME]}

    async def fake_resolve(host: str, timeout: float = 5.0) -> list[str]:
        got = table.get(host, OSError("no such name"))
        if isinstance(got, Exception):
            raise got
        return got

    monkeypatch.setattr(home_address, "resolve_ipv4", fake_resolve)
    return table


@pytest.fixture
def home(settings, fake_app, dns):
    """(TestClient opened from the home's address, fake AdGuard, engine, router device)"""
    ag = AdGuardClient("http://fake", "a", "b", transport=httpx.ASGITransport(app=fake_app))
    eng = Engine(settings, ag=ag)
    c = TestClient(create_app(settings, eng, background=False), client=(HOME, 50000))
    c.__enter__()
    r = c.post("/api/devices", headers=H, json={"name": "Home WiFi", "type": "router", "brand": "TP-Link", "setup_method": "ipv4_demo"})
    assert r.status_code == 201, r.text
    yield c, fake_app.state.ag, eng, r.json()["device"]
    c.__exit__(None, None, None)


def test_follow_from_home_then_the_address_moves(home, dns):
    c, fake, eng, dev = home
    r = c.patch(f"/api/devices/{dev['id']}", headers=H, json={"ddns_host": "  MyHome.TPLinkDNS.com "})
    assert r.status_code == 200, r.text
    got = r.json()["device"]
    assert got["ddns_host"] == NAME and got["linked_ip"] == HOME  # linked to where the page is opened from
    assert got["ddns_status"]["ip"] == HOME and got["ddns_status"]["error"] is None
    assert HOME in fake.clients[dev["client_id"]]["ids"]

    # The router reconnects, ACT hands out a new address, the router updates its Dynamic DNS name
    dns[NAME] = ["49.36.9.9"]
    assert asyncio.run(home_address.follow_all(eng)) == 1
    asyncio.run(eng.sync.sync_all())
    now = c.get(f"/api/devices/{dev['id']}", headers=H).json()
    assert now["linked_ip"] == "49.36.9.9" and now["ddns_status"]["ip"] == "49.36.9.9"
    assert fake.clients[dev["client_id"]]["ids"] == [dev["client_id"], "49.36.9.9"]
    alert = eng.conn.execute("SELECT * FROM alerts WHERE type = 'home_address'").fetchone()
    assert alert["message"] == (
        f"Your home's internet address changed from {HOME} to 49.36.9.9. "
        f"QuietNet followed it through {NAME}, so Home WiFi stays protected."
    )
    assert asyncio.run(home_address.follow_all(eng)) == 0  # nothing new: no second alert


def test_a_name_pointing_at_someone_else_is_refused(home, dns):
    """Otherwise anyone could point QuietNet at another home and read its lookups."""
    c, _, _, dev = home
    dns["neighbour.ddns.net"] = ["49.36.200.200"]
    r = c.patch(f"/api/devices/{dev['id']}", headers=H, json={"ddns_host": "neighbour.ddns.net"})
    assert r.status_code == 409
    assert r.json()["detail"].startswith("neighbour.ddns.net points to 49.36.200.200, but this page is coming from 49.36.1.1.")
    assert c.get(f"/api/devices/{dev['id']}", headers=H).json()["ddns_host"] is None


@pytest.mark.parametrize(
    "points_to, wanted",
    [
        (OSError("no such name"), "couldn't be looked up"),
        (["192.168.0.1"], "points to 192.168.0.1, which isn't a public internet address"),
        (["100.72.14.9"], "isn't a public internet address"),  # behind CGNAT
    ],
)
def test_names_that_cant_be_followed(home, dns, points_to, wanted):
    c, _, _, dev = home
    dns["broken.no-ip.org"] = points_to
    r = c.patch(f"/api/devices/{dev['id']}", headers=H, json={"ddns_host": "broken.no-ip.org"})
    assert r.status_code == 422 and wanted in r.json()["detail"]
    assert c.patch(f"/api/devices/{dev['id']}", headers=H, json={"ddns_host": "not a host!"}).status_code == 422


def test_when_the_name_stops_resolving_the_link_stays(home, dns):
    c, _, eng, dev = home
    c.patch(f"/api/devices/{dev['id']}", headers=H, json={"ddns_host": NAME})
    dns[NAME] = OSError("timed out")
    assert asyncio.run(home_address.follow_all(eng)) == 0
    now = c.get(f"/api/devices/{dev['id']}", headers=H).json()
    assert now["linked_ip"] == HOME and "couldn't be looked up" in now["ddns_status"]["error"]


def test_stop_following(home, dns):
    c, _, eng, dev = home
    c.patch(f"/api/devices/{dev['id']}", headers=H, json={"ddns_host": NAME})
    r = c.patch(f"/api/devices/{dev['id']}", headers=H, json={"ddns_host": ""}).json()["device"]
    assert r["ddns_host"] is None and r["ddns_status"] is None and r["linked_ip"] == HOME
    dns[NAME] = ["49.36.9.9"]
    assert asyncio.run(home_address.follow_all(eng)) == 0
