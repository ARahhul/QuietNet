"""A home can switch QuietNet's shared ad & tracker list off for itself (Settings). Its own rules keep working, the
other homes keep the list, and the dashboard stops saying the list blocks things there."""

import asyncio
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from pahra.adguard.client import AdGuardClient
from pahra.adguard.rules import SECTION_BEGIN, SECTION_END, assert_client_scoped
from pahra.db import init_sqlite_wal, new_id, now_iso
from pahra.engine import Engine
from pahra.main import create_app

H = {"Authorization": "Bearer dev-token"}
AD = "tkacr.alphonso.tv"  # on the sample list


@pytest.fixture
def api(settings, fake_app):
    ag = AdGuardClient("http://fake", "a", "b", transport=httpx.ASGITransport(app=fake_app))
    eng = Engine(settings, ag=ag)
    with TestClient(create_app(settings, eng, background=False)) as c:
        c.engine = eng
        yield c


def _drive(api):
    async def go():
        obs = await api.engine.poller.poll_once()
        await api.engine.pipeline.process(obs)

    asyncio.run(go())


def _neighbour(api) -> str:
    """Another home on the same server, with one device."""
    uid, cid = new_id(), "neighbour-phone-abcdefgh"
    api.engine.conn.execute("INSERT INTO users(id, email, auto_mode, created_at) VALUES (?, 'n@example.com', 0, ?)", (uid, now_iso()))
    api.engine.conn.execute(
        "INSERT INTO devices(id, user_id, client_id, name, type, setup_method, created_at) VALUES (?,?,?,?,'phone','dot',?)",
        (new_id(), uid, cid, "Phone", now_iso()),
    )
    return cid


def _section(fake) -> list[str]:
    return fake.rules[fake.rules.index(SECTION_BEGIN) + 1 : fake.rules.index(SECTION_END)]


def test_switching_the_list_off_keeps_the_homes_own_rules_and_other_homes_protected(api, fake):
    tv = api.post("/api/devices", json={"name": "TV", "type": "tv", "brand": "LG"}, headers=H).json()["device"]
    phone = api.post("/api/devices", json={"name": "Phone", "type": "phone"}, headers=H).json()["device"]
    TV, PHONE, OTHER = tv["client_id"], phone["client_id"], _neighbour(api)
    # The home's own rules: a block for everyone at home, and an allow under it for the phone
    assert api.post("/api/decisions", json={"domain": "example-news.in", "action": "block"}, headers=H).json()["synced"]
    api.post("/api/decisions", json={"domain": "news.example-news.in", "action": "allow", "device_id": phone["id"]}, headers=H)
    assert api.get("/api/settings", headers=H).json()["shared_blocklist"] is True
    assert all(fake.match(AD, c)[0] for c in (TV, PHONE, OTHER))

    s = api.patch("/api/settings", json={"shared_blocklist": False}, headers=H).json()
    assert s["shared_blocklist"] is False and s["synced"] is True
    rules = _section(fake)
    for r in rules:
        if "$client=~" not in r:  # the rescue rules for strangers name the clients they leave out
            assert_client_scoped(r)
    assert [r for r in rules if r.startswith("@@||*^")] == [f"@@||*^$client='{min(TV, PHONE)}'|'{max(TV, PHONE)}'"]

    # The list no longer blocks for this home, and still does for the neighbour
    assert not fake.match(AD, TV)[0] and not fake.match(AD, PHONE)[0]
    assert fake.match(AD, OTHER)[0]
    # The home's own block still applies, and its own allow still beats it on the phone
    assert fake.match("example-news.in", TV)[0] and fake.match("news.example-news.in", TV)[0]
    assert not fake.match("news.example-news.in", PHONE)[0]
    assert not fake.match("example-news.in", OTHER)[0]  # one home's block never reaches another

    # The dashboard says so instead of claiming the list blocks it
    assert api.get("/api/blocked/by-list", headers=H).json()["home_off"] is True
    v = api.get(f"/api/domains/{AD}", headers=H).json()
    why = next(r for r in v["reasons"] if r["kind"] == "blocklist")
    assert why["home_off"] and not why["active"] and "Settings" in why["detail"]
    assert v["blocked_by"] != "blocklist"

    # Switching it back on drops the allow-everything rule
    s = api.patch("/api/settings", json={"shared_blocklist": True}, headers=H).json()
    assert s["shared_blocklist"] is True and not any(r.startswith("@@||*^") for r in _section(fake))
    assert fake.match(AD, TV)[0]
    # Saving the same value again changes nothing in AdGuard
    assert "synced" not in api.patch("/api/settings", json={"shared_blocklist": True}, headers=H).json()


def test_with_the_list_off_suggestions_and_auto_mode_cover_listed_ads_again(api, fake):
    tv = api.post("/api/devices", json={"name": "TV", "type": "tv", "brand": "LG"}, headers=H).json()["device"]
    fake.record(tv["client_id"], AD)
    _drive(api)
    assert AD.split(".", 1)[1] not in {c["domain"] for c in api.get("/api/suggestions", headers=H).json()}  # the list has it

    api.patch("/api/settings", json={"shared_blocklist": False}, headers=H)
    assert "alphonso.tv" in {c["domain"] for c in api.get("/api/suggestions", headers=H).json()}

    # Auto-mode blocks a high-confidence, low-risk ad for this home with a rule of its own
    api.patch("/api/settings", json={"auto_mode": True}, headers=H)
    phone = api.post("/api/devices", json={"name": "Phone", "type": "phone"}, headers=H).json()["device"]
    fake.record(phone["client_id"], "api.mixpanel.com")
    assert not fake.match("api.mixpanel.com", phone["client_id"])[0]  # nothing blocks it for this home yet
    _drive(api)
    asyncio.run(api.engine.sync.sync_rules())
    assert fake.match("api.mixpanel.com", phone["client_id"])[0]
    mine = api.engine.conn.execute(
        "SELECT decided_by, device_id FROM decisions WHERE domain = 'mixpanel.com' AND undone_at IS NULL"
    ).fetchall()
    assert [(r["decided_by"], r["device_id"]) for r in mine] == [("auto", phone["id"])]


def test_rules_saved_before_this_change_are_rewritten_on_the_next_sync(api, fake):
    tv = api.post("/api/devices", json={"name": "TV", "type": "tv", "brand": "LG"}, headers=H).json()["device"]
    old = f"||samsungads.com^$client='{tv['client_id']}'"  # how rules were stored before `important`
    api.engine.conn.execute(
        "INSERT INTO decisions(id, user_id, device_id, domain, action, adguard_rule, decided_by, created_at) "
        "VALUES (?, ?, NULL, 'samsungads.com', 'block', ?, 'user', ?)",
        (new_id(), api.engine.user_id, old, now_iso()),
    )
    asyncio.run(api.engine.sync.sync_rules())
    assert old not in fake.rules and old + ",important" in fake.rules


def test_an_older_database_gets_the_switch_on(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, auto_mode INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)")
    old.execute("INSERT INTO users VALUES ('u1', 'a@example.com', 0, '2026-01-01')")
    old.commit()
    old.close()
    conn = init_sqlite_wal(str(path))
    assert conn.execute("SELECT shared_blocklist FROM users WHERE id = 'u1'").fetchone()[0] == 1
    conn.close()
