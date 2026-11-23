"""Setup: "I've done it, check": QuietNet confirms on its own side that a device's lookups are arriving."""

import asyncio
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from pahra.adguard.client import AdGuardClient
from pahra.engine import Engine
from pahra.main import create_app
from pahra.poller import Poller

H = {"Authorization": "Bearer dev-token"}


@pytest.fixture
def make(settings, fake_app):
    """make(client_ip=None) -> (TestClient, fake AdGuard state, engine)"""
    clients = []

    def _make(client_ip: str | None = None):
        ag = AdGuardClient("http://fake", "a", "b", transport=httpx.ASGITransport(app=fake_app))
        eng = Engine(settings, ag=ag)
        kw = {"client": (client_ip, 50000)} if client_ip else {}
        c = TestClient(create_app(settings, eng, background=False), **kw)
        c.__enter__()
        clients.append(c)
        return c, fake_app.state.ag, eng

    yield _make
    for c in clients:
        c.__exit__(None, None, None)


def _device(c, **kw) -> dict:
    r = c.post("/api/devices", headers=H, json={"name": "Pixel", "type": "phone", "brand": "Android", "setup_method": "dot"} | kw)
    assert r.status_code in (200, 201), r.text
    return r.json()["device"]


def test_not_yet_then_working_once_lookups_arrive(make):
    c, fake, _ = make()
    dev = _device(c)
    r = c.post(f"/api/devices/{dev['id']}/check", headers=H).json()
    assert r["ok"] is False and r["message"] == "Not yet: no lookups from Pixel have reached QuietNet."
    assert any("Private DNS" in t for t in r["tips"])

    # The phone starts using QuietNet: the check reads AdGuard's log fresh, no waiting for the next poll
    fake.record(dev["client_id"], "example.com")
    fake.record(dev["client_id"], "connectivitycheck.gstatic.com")
    r = c.post(f"/api/devices/{dev['id']}/check", headers=H).json()
    assert r["ok"] is True and r["lookups"] == 2 and r["encrypted"] is True and r["tips"] == []
    assert r["message"] == "It's working. QuietNet saw 2 lookups from Pixel in the last 10 minutes, the latest just now, encrypted (DNS-over-TLS)."
    # ...and the device itself now counts as seen, so the waiting card turns green too
    assert c.get(f"/api/devices/{dev['id']}", headers=H).json()["last_seen"]


def test_plain_dns_router_whose_home_address_changed(make):
    c, _, _ = make(client_ip="49.36.1.1")
    dev = _device(c, name="Home router", type="router", brand="TP-Link", setup_method="ipv4_demo")
    r = c.patch(f"/api/devices/{dev['id']}", headers=H, json={"linked_ip": "106.51.0.9", "allow_shared_ipv4": True})
    assert r.status_code == 200, r.text
    got = c.post(f"/api/devices/{dev['id']}/check", headers=H).json()
    assert got["ok"] is False and got["link_ip"] == "49.36.1.1"
    assert "106.51.0.9" in got["tips"][0] and "49.36.1.1" in got["tips"][0]
    assert any("restart it" in t for t in got["tips"])


def test_plain_dns_router_that_was_never_linked(make):
    c, _, _ = make(client_ip="49.36.1.1")
    dev = _device(c, name="Home router", type="router", brand="TP-Link", setup_method="ipv4_demo")
    got = c.post(f"/api/devices/{dev['id']}/check", headers=H).json()
    assert got["ok"] is False and got["link_ip"] == "49.36.1.1"
    assert got["message"].startswith("QuietNet can't recognise Home router yet")
    assert got["tips"][0].startswith("Link it from your home WiFi")
    # Linking it (what the dashboard's button does) makes the next check look for that address
    c.patch(f"/api/devices/{dev['id']}", headers=H, json={"linked_ip": "49.36.1.1", "allow_shared_ipv4": True})
    again = c.post(f"/api/devices/{dev['id']}/check", headers=H).json()
    assert again["link_ip"] is None and again["message"] == "Not yet: no lookups from Home router have reached QuietNet."


def test_says_when_it_was_last_seen(make):
    c, _, eng = make()
    dev = _device(c)
    two_hours_ago = time.time() - 2 * 3600
    eng.conn.execute(
        "INSERT INTO queries(device_id, t, qname, domain, status) VALUES (?, ?, 'example.com', 'example.com', 'allowed')",
        (dev["id"], two_hours_ago),
    )
    eng.conn.execute("UPDATE devices SET last_seen = ? WHERE id = ?", (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(two_hours_ago)), dev["id"]))
    got = c.post(f"/api/devices/{dev['id']}/check", headers=H).json()
    assert got["ok"] is False and got["message"] == "Nothing from Pixel in the last 10 minutes. QuietNet last saw it 2 hours ago."


def test_unknown_device(make):
    c, _, _ = make()
    assert c.post("/api/devices/nope/check", headers=H).status_code == 404


async def test_fresh_read_while_the_poller_is_running(conn, ag, fake, add_device):
    did = add_device("tv-1", "tv")
    p = Poller(conn, ag, interval_s=60)
    task = asyncio.create_task(p.run_forever())
    try:
        await asyncio.sleep(0.2)  # first read done; the next one is a minute away
        fake.record("tv-1", "example.com")
        t0 = time.monotonic()
        await p.poll_fresh(timeout=5)
        assert time.monotonic() - t0 < 2
        assert conn.execute("SELECT COUNT(*) FROM queries WHERE device_id = ?", (did,)).fetchone()[0] == 1
    finally:
        task.cancel()
