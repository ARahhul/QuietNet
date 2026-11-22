"""Whole-home setup on a router: encrypted DNS guides and the home-IP / CGNAT check."""

import httpx
from fastapi.testclient import TestClient

from pahra.adguard.client import AdGuardClient
from pahra.engine import Engine
from pahra.main import create_app
from pahra.setup_guides import setup_guide

H = {"Authorization": "Bearer dev-token"}


def _router(brand, method, linked_ip=None):
    return {"client_id": "home", "name": "Home", "type": "router", "brand": brand, "setup_method": method, "linked_ip": linked_ip}


def test_asus_router_gets_dot_steps_with_its_own_hostname(settings):
    g = setup_guide(settings, _router("ASUS", "dot"))
    text = " ".join(g["steps"])
    assert g["method"] == "dot"
    assert "DNS-over-TLS" in text and f"home.{settings.dns_hostname}" in text and "853" in text
    assert "Private DNS provider hostname" not in text  # not the Android phone steps


def test_other_routers_get_generic_dot_steps(settings):
    text = " ".join(setup_guide(settings, _router("TP-Link", "dot"))["steps"])
    assert "DNS-over-TLS" in text and "can't do it" in text


def test_plain_dns_router_mentions_the_linked_home_ip(settings):
    text = " ".join(setup_guide(settings, _router("Jio", "ipv4_demo", "203.0.113.7"))["steps"])
    assert "203.0.113.7" in text


def test_cgnat_check_uses_the_visitors_address_in_cloud_mode(settings, fake_app):
    ag = AdGuardClient("http://fake", "a", "b", transport=httpx.ASGITransport(app=fake_app))
    app = create_app(settings, Engine(settings, ag=ag), background=False)
    # As if behind Caddy: the TestClient's address stands in for the visitor's public IP.
    with TestClient(app, client=("49.36.10.20", 50000)) as c:
        assert c.get("/api/network/public-ip", headers=H).json()["public_ip"] == "49.36.10.20"
        same = c.post("/api/network/cgnat", json={"router_wan_ip": "49.36.10.20"}, headers=H).json()
        shared = c.post("/api/network/cgnat", json={"router_wan_ip": "100.72.14.9"}, headers=H).json()
    assert same["cgnat"] is False and "own public IP" in same["advice"]
    assert shared["cgnat"] is True and "shared" in shared["advice"]
