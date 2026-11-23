"""Hardening from the review of the deployed branch: one account can't change the engine for everyone, a public
server refuses to start with development defaults, and multi-step writes happen whole or not at all."""

import re

import httpx
import pytest
from fastapi.testclient import TestClient

from pahra.adguard.client import AdGuardClient
from pahra.adguard.rules import CLIENT_ID_RE
from pahra.api.deps import AccountView
from pahra.db import new_id, now_iso, transaction
from pahra.decisions import active_decision, create_decision, get_decision
from pahra.engine import Engine
from pahra.main import create_app
from pahra.site_fix import unblock

H = {"Authorization": "Bearer dev-token"}


@pytest.fixture
def api(settings, fake_app):
    ag = AdGuardClient("http://fake", "a", "b", transport=httpx.ASGITransport(app=fake_app))
    eng = Engine(settings, ag=ag)
    with TestClient(create_app(settings, eng, background=False)) as c:
        c.engine = eng
        yield c


def _home(conn, email: str, client_id: str) -> str:
    uid = new_id()
    conn.execute("INSERT INTO users(id, email, auto_mode, created_at) VALUES (?, ?, 0, ?)", (uid, email, now_iso()))
    _device(conn, uid, client_id)
    return uid


def _device(conn, uid: str, client_id: str) -> str:
    did = new_id()
    conn.execute(
        "INSERT INTO devices(id, user_id, client_id, name, type, setup_method, created_at) VALUES (?,?,?,?,'phone','dot',?)",
        (did, uid, client_id, client_id, now_iso()),
    )
    return did


def test_one_accounts_view_cant_change_the_engine_for_everyone(settings):
    eng = Engine(settings)
    view = AccountView(eng, "someone-else")
    with pytest.raises(AttributeError):
        view.user_id = "attacker"  # would have moved every request onto that account
    with pytest.raises(AttributeError):
        view.settings = None
    assert view.user_id == "someone-else" and eng.user_id != "attacker" and eng.settings is settings
    view.forwarder = {"running": True, "devices": 1}  # the laptop relay's state is shared on purpose
    assert eng.forwarder["running"]
    eng.conn.close()


SAFE = {
    "env": "production",
    "api_token": "a" * 48,
    "adguard_pass": "b" * 48,
    "api_docs": False,
}


def test_production_refuses_development_defaults(settings):
    assert settings.model_copy(update=SAFE).check_production() == []
    create_app(settings.model_copy(update=SAFE), background=False)  # a good config builds
    for bad in (
        {"api_token": "dev-token"},
        {"api_token": "short"},
        {"adguard_pass": "change-me"},
        {"firebase_auth_emulator_host": "127.0.0.1:9099"},
        {"api_docs": True},
    ):
        with pytest.raises(RuntimeError, match="Refusing to start in production"):
            create_app(settings.model_copy(update=SAFE | bad), background=False)
    # The cloud lets the local dashboard in on purpose, so CORS isn't one of the checks
    cloud = settings.model_copy(update=SAFE | {"cors_origins": "https://quietnet.web.app,http://localhost:5173"})
    assert cloud.check_production() == []


def test_development_keeps_its_defaults(settings):
    assert settings.env == "dev" and settings.check_production()  # the defaults are unsafe, but dev doesn't check
    create_app(settings, background=False)


def test_bulk_undo_skips_decisions_that_arent_this_homes(api):
    conn, me = api.engine.conn, api.engine.user_id
    _device(conn, me, "my-phone")
    other = _home(conn, "other@example.com", "their-phone")
    mine = create_decision(conn, me, "ads.example.com", "block")
    theirs = create_decision(conn, other, "ads.example.com", "block")

    # Another home's decision (or a made-up id) used to crash the request half-way, after undoing some, unsynced
    r = api.post("/api/decisions/undo-bulk", json={"ids": [theirs["id"], mine["id"], "no-such-id"]}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 1 and r.json()["synced"]
    assert get_decision(conn, mine["id"])["undone_at"] and get_decision(conn, theirs["id"])["undone_at"] is None
    assert api.post("/api/decisions/undo-bulk", json={"ids": ["x"] * 501}, headers=H).status_code == 422


def test_a_failed_write_leaves_nothing_half_saved(conn, user_id, add_device):
    add_device("my-phone")
    first = create_decision(conn, user_id, "ads.example.com", "block")
    with pytest.raises(RuntimeError):
        with transaction(conn):
            create_decision(conn, user_id, "ads.example.com", "allow")  # supersedes the block (a nested savepoint)
            raise RuntimeError("something failed before the request finished")
    assert active_decision(conn, "ads.example.com", None, user_id)["id"] == first["id"]  # the block is back in force
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1

    # Unblocking several names: one bad name refuses the lot, so none is half let through
    with pytest.raises(ValueError):
        unblock(conn, user_id, ["ads.example.com", "not a domain"])
    assert active_decision(conn, "ads.example.com", None, user_id)["action"] == "block"


def test_new_devices_get_an_address_nobody_can_guess(api):
    a = api.post("/api/devices", json={"name": "Living room TV", "type": "tv"}, headers=H).json()["device"]
    b = api.post("/api/devices", json={"name": "Living room TV", "type": "tv"}, headers=H).json()["device"]
    assert all(re.fullmatch(r"living-room-tv-[a-z2-7]{8}", d["client_id"]) for d in (a, b))
    assert a["client_id"] != b["client_id"]
    # A name someone picks only sets the readable part; the random part is always added
    c = api.post("/api/devices", json={"name": "x" * 60, "type": "phone", "client_id": "My Own Name!"}, headers=H).json()
    assert re.fullmatch(r"my-own-name-[a-z2-7]{8}", c["device"]["client_id"])
    long = api.post("/api/devices", json={"name": "y" * 60, "type": "phone"}, headers=H).json()["device"]["client_id"]
    assert len(long) == 39 and CLIENT_ID_RE.match(long)


def test_changing_a_devices_address_moves_its_rules_and_shuts_the_old_one_out(api, fake):
    tv = api.post("/api/devices", json={"name": "Living room TV", "type": "tv"}, headers=H).json()["device"]
    phone = api.post("/api/devices", json={"name": "Phone", "type": "phone"}, headers=H).json()["device"]
    old = tv["client_id"]
    api.post("/api/decisions", json={"domain": "ads.example.com", "action": "block", "device_id": tv["id"]}, headers=H)
    api.post("/api/decisions", json={"domain": "tracker.example.net", "action": "block"}, headers=H)  # whole home
    assert fake.match("ads.example.com", old)[0]

    r = api.post(f"/api/devices/{tv['id']}/client-id", headers=H)
    assert r.status_code == 200, r.text
    new = r.json()["device"]["client_id"]
    assert new != old and re.fullmatch(r"living-room-tv-[a-z2-7]{8}", new) and r.json()["synced"]
    assert r.json()["setup"]["values"]["dot_hostname"] == f"{new}.dns.pahra.app"  # what to enter on the TV now
    assert old not in fake.clients and new in fake.clients
    assert not any(old in rule for rule in fake.rules)
    # Its own block and the home's block follow it to the new address; the phone keeps the home's block
    assert fake.match("ads.example.com", new)[0] and fake.match("tracker.example.net", new)[0]
    assert fake.match("tracker.example.net", phone["client_id"])[0] and not fake.match("ads.example.com", phone["client_id"])[0]
    # The old address is a stranger now: only the rescue answers (Dynamic DNS, time), everything else is blocked
    assert fake.match("example.com", old)[0] and not fake.match("example.com", new)[0]
    assert api.post("/api/devices/not-mine/client-id", headers=H).status_code == 404


def test_links_carry_a_short_ticket_not_the_sign_in(api):
    from pahra import tickets

    dev = api.post("/api/devices", json={"name": "iPhone", "type": "phone", "brand": "Apple"}, headers=H).json()["device"]
    path = f"/devices/{dev['id']}/profile.mobileconfig"
    t = api.post("/api/tickets", json={"path": path}, headers=H).json()
    assert t["expires_in"] == 120
    assert api.get(f"/api{path}", params={"ticket": t["ticket"]}).status_code == 200

    # Only GET, only that exact path, only the two link paths, and only for two minutes
    assert api.post(f"/api{path}", params={"ticket": t["ticket"]}).status_code in (401, 405)
    assert api.get("/api/devices", params={"ticket": t["ticket"]}).status_code == 401
    assert api.get(f"/api/devices/{dev['id']}", params={"ticket": t["ticket"]}).status_code == 401
    assert api.post("/api/tickets", json={"path": "/devices"}, headers=H).status_code == 422
    key, uid = api.engine.ticket_key, api.engine.user_id
    old = tickets.make(key, uid, f"/api{path}", now=0)
    assert api.get(f"/api{path}", params={"ticket": old}).status_code == 401  # expired
    forged = tickets.make(b"x" * 32, uid, f"/api{path}")
    assert api.get(f"/api{path}", params={"ticket": forged}).status_code == 401  # not signed by this server
    assert api.get(f"/api{path}", params={"ticket": "garbage"}).status_code == 401
    # A ticket can't get another ticket, and a token in the URL opens nothing
    assert api.post("/api/tickets", json={"path": path}, params={"ticket": t["ticket"]}).status_code == 401
    assert api.get(f"/api{path}", params={"token": "dev-token"}).status_code == 401
