"""One account per home: people sign in with Firebase and see only their own devices, lookups, alerts and blocks."""

import datetime as dt
import time

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from google.auth import crypt, jwt

from pahra.adguard.client import AdGuardClient
from pahra.auth import CERTS_URL, FirebaseVerifier
from pahra.engine import Engine
from pahra.main import create_app

PROJECT = "quietnet-test"
OPERATOR = {"Authorization": "Bearer dev-token"}


def _keypair(kid: str):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, kid)])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(1).not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    pem_key = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return crypt.RSASigner.from_string(pem_key, key_id=kid), cert.public_bytes(serialization.Encoding.PEM).decode()


GOOGLE, GOOGLE_CERT = _keypair("google-1")
STRANGER, _ = _keypair("google-1")  # same key id, different key


def token(uid: str, email: str | None = None, *, signer=GOOGLE, aud=PROJECT, iss=None, ttl=3600) -> str:
    now = int(time.time())
    claims = {
        "iss": iss or f"https://securetoken.google.com/{aud}", "aud": aud, "sub": uid, "user_id": uid,
        "iat": now - 10, "exp": now + ttl, "auth_time": now - 10,
    }
    if email:
        claims |= {"email": email, "email_verified": True}
    return jwt.encode(signer, claims).decode()


def bearer(t: str) -> dict:
    return {"Authorization": f"Bearer {t}"}


@pytest.fixture
def app(settings, fake_app):
    s = settings.model_copy(update={"firebase_project_id": PROJECT})
    ag = AdGuardClient("http://fake", "a", "b", transport=httpx.ASGITransport(app=fake_app))
    eng = Engine(s, ag=ag)
    fetched = []

    def google(req: httpx.Request) -> httpx.Response:
        assert str(req.url) == CERTS_URL
        fetched.append(1)
        return httpx.Response(200, json={"google-1": GOOGLE_CERT}, headers={"cache-control": "public, max-age=3600"})

    eng.auth = FirebaseVerifier(PROJECT, transport=httpx.MockTransport(google))
    c = TestClient(create_app(s, eng, background=False))
    c.__enter__()
    yield c, eng, fake_app.state.ag, fetched
    c.__exit__(None, None, None)


def test_each_home_sees_only_its_own(app):
    c, eng, fake, fetched = app
    asha, ravi = bearer(token("uid-asha", "asha@example.com")), bearer(token("uid-ravi", "ravi@example.com"))

    tv = c.post("/api/devices", headers=asha, json={"name": "Asha TV", "type": "tv", "setup_method": "dot"}).json()["device"]
    c.post("/api/devices", headers=ravi, json={"name": "Ravi phone", "type": "phone", "setup_method": "dot"})
    assert [d["name"] for d in c.get("/api/devices", headers=asha).json()] == ["Asha TV"]
    assert [d["name"] for d in c.get("/api/devices", headers=ravi).json()] == ["Ravi phone"]
    assert c.get(f"/api/devices/{tv['id']}", headers=ravi).status_code == 404
    assert c.get(f"/api/devices/{tv['id']}/activity", headers=ravi).status_code == 404
    assert c.post("/api/decisions", headers=ravi, json={"domain": "ads.example.com", "action": "block", "device_id": tv["id"]}).status_code == 422

    # Lookups and alerts stay with their home
    eng.conn.execute(
        "INSERT INTO queries(device_id, t, qname, domain, status) VALUES (?, ?, 'tracker.example.net', 'example.net', 'allowed')",
        (tv["id"], time.time()),
    )
    eng.conn.execute(
        "INSERT INTO alerts(id, user_id, device_id, type, message, created_at) "
        "SELECT 'a1', user_id, id, 'new_tracker', 'Asha only', '2026-01-01T00:00:00+00:00' FROM devices WHERE id = ?",
        (tv["id"],),
    )
    assert len(c.get("/api/live/queries", headers=asha).json()["queries"]) == 1
    assert c.get("/api/live/queries", headers=ravi).json()["queries"] == []
    assert [a["message"] for a in c.get("/api/alerts", headers=asha).json()] == ["Asha only"]
    assert c.get("/api/alerts", headers=ravi).json() == []
    assert c.post("/api/alerts/a1/read", headers=ravi).status_code == 404

    # A household block covers only that home's devices
    c.post("/api/decisions", headers=ravi, json={"domain": "ads.example.com", "action": "block"})
    rule = next(r for r in fake.rules if r.startswith("||ads.example.com^"))
    assert "ravi-phone" in rule and "asha-tv" not in rule

    # The operator's token is its own (empty) home; Google's keys were fetched once
    assert c.get("/api/devices", headers=OPERATOR).json() == []
    assert len(fetched) == 1


@pytest.mark.parametrize(
    "bad",
    [
        token("uid-x", signer=STRANGER),  # not signed by Google
        token("uid-x", aud="someone-elses-project"),
        token("uid-x", iss="https://evil.example/quietnet-test"),
        token("uid-x", ttl=-600),  # expired
        "not.a.token",
        "dev-token-but-wrong",
    ],
)
def test_bad_sign_ins_are_refused(app, bad):
    c, *_ = app
    assert c.get("/api/devices", headers=bearer(bad)).status_code == 401


def test_the_operator_moves_their_home_onto_a_sign_in(app):
    c, eng, *_ = app
    c.post("/api/devices", headers=OPERATOR, json={"name": "Home WiFi", "type": "router", "setup_method": "ipv4_demo"})
    me = bearer(token("uid-owner", "owner@example.com"))
    assert c.get("/api/devices", headers=me).json() == []  # a fresh, empty account at first
    assert c.get("/api/account", headers=me).json() == {
        "signed_in_with": "firebase", "email": "owner@example.com", "operator": False, "can_claim": True
    }

    assert c.post("/api/account/claim", headers=me, json={"token": "wrong"}).status_code == 403
    r = c.post("/api/account/claim", headers=me, json={"token": "dev-token"})
    assert r.status_code == 200, r.text
    assert [d["name"] for d in c.get("/api/devices", headers=me).json()] == ["Home WiFi"]
    assert c.get("/api/account", headers=me).json()["operator"] is True
    # The token still opens the same home, and nobody else can take it
    assert [d["name"] for d in c.get("/api/devices", headers=OPERATOR).json()] == ["Home WiFi"]
    other = bearer(token("uid-other", "other@example.com"))
    assert c.post("/api/account/claim", headers=other, json={"token": "dev-token"}).status_code == 409
    assert c.get("/api/devices", headers=other).json() == []
    # Claiming needs a sign-in, not just the token
    assert c.post("/api/account/claim", headers=OPERATOR, json={"token": "dev-token"}).status_code == 400


def test_one_address_belongs_to_one_device(app):
    c, *_ = app
    asha, ravi = bearer(token("uid-asha", "asha@example.com")), bearer(token("uid-ravi", "ravi@example.com"))
    a = c.post("/api/devices", headers=asha, json={"name": "Asha WiFi", "type": "router", "setup_method": "ipv4_demo"}).json()["device"]
    b = c.post("/api/devices", headers=ravi, json={"name": "Ravi WiFi", "type": "router", "setup_method": "ipv4_demo"}).json()["device"]
    link = {"linked_ip": "49.36.1.1", "allow_shared_ipv4": True}
    assert c.patch(f"/api/devices/{a['id']}", headers=asha, json=link).status_code == 200
    r = c.patch(f"/api/devices/{b['id']}", headers=ravi, json=link)
    assert r.status_code == 409 and "already linked to another QuietNet device" in r.json()["detail"]


def test_without_a_firebase_project_only_the_token_works(settings, fake_app):
    ag = AdGuardClient("http://fake", "a", "b", transport=httpx.ASGITransport(app=fake_app))
    eng = Engine(settings, ag=ag)
    with TestClient(create_app(settings, eng, background=False)) as c:
        assert c.get("/api/devices", headers=bearer(token("uid-asha", "asha@example.com"))).status_code == 401
        assert c.get("/api/devices", headers=OPERATOR).status_code == 200
        assert c.get("/api/account", headers=OPERATOR).json()["signed_in_with"] == "token"
