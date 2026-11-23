""""Site not working?": find what QuietNet blocked that a site needs, let it through, and undo that."""

import base64
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from pahra import site_fix
from pahra.adguard.client import AdGuardClient
from pahra.db import now_iso
from pahra.decisions import active_decision, create_decision
from pahra.engine import Engine
from pahra.main import create_app
from pahra.pipeline.explainer import Explainer
from pahra.pipeline.policy import apply_policy

H = {"Authorization": "Bearer dev-token"}
PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 64).decode()


def _model(answer, calls: list) -> httpx.MockTransport:
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(json.loads(req.content))
        if answer is None:
            return httpx.Response(500, text="model down")
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer)}}]})

    return httpx.MockTransport(handler)


@pytest.fixture
def make(settings, fake_app):
    """make(answer) -> (TestClient, calls to the model, engine, phone device)"""
    clients = []

    def _make(answer=None, *, ai: bool = True):
        ag = AdGuardClient("http://fake", "a", "b", transport=httpx.ASGITransport(app=fake_app))
        eng = Engine(settings, ag=ag)
        calls: list = []
        if ai:
            eng.explainer = Explainer("k", "m-1", base_url="https://gw.example/v1", transport=_model(answer, calls))
        c = TestClient(create_app(settings, eng, background=False))
        c.__enter__()
        clients.append(c)
        dev = c.post("/api/devices", headers=H, json={"name": "Phone", "type": "phone", "setup_method": "dot"}).json()["device"]
        return c, calls, eng, dev

    yield _make
    for c in clients:
        c.__exit__(None, None, None)


def lookups(eng, dev, rows: list[tuple]) -> None:
    """(qname, normalised domain, status, seconds ago[, blocked_by])"""
    now = time.time()
    for qname, domain, status, ago, *by in rows:
        eng.conn.execute(
            "INSERT INTO queries(device_id, t, qname, domain, qtype, status, blocked_by) VALUES (?,?,?,?,?,?,?)",
            (dev["id"], now - ago, qname, domain, "A", status, (by or [None])[0]),
        )


def known(eng, domain: str, category: str, owner: str | None = None) -> None:
    eng.conn.execute(
        "INSERT INTO domains(domain, root_domain, category, owner, confidence, first_seen) VALUES (?,?,?,?,?,?)",
        (domain, domain, category, owner, "high", now_iso()),
    )


def rule(eng, dev, domain: str, *, action: str = "block", by: str = "user", household: bool = False) -> dict:
    return create_decision(eng.conn, eng.user_id, domain, action, None if household else dev["id"], by)


IRCTC = [
    ("www.irctc.co.in", "irctc.co.in", "allowed", 50),
    ("static.irctc.co.in", "irctc.co.in", "blocked", 45, "rule"),  # a wrong block on all of irctc.co.in
    ("api.irctc.co.in", "irctc.co.in", "blocked", 44, "rule"),
    ("checkout.razorpay.com", "razorpay.com", "blocked", 40, "list"),  # a block list stopped the payment page
    ("www.googletagmanager.com", "googletagmanager.com", "blocked", 42, "list"),
    ("old.example.org", "example.org", "blocked", 3 * 3600, "list"),  # long before: not looked at
]
# The same, with block lists stopping the site's own names (no QuietNet rule)
LISTED = [(q, d, s, ago, "list") if s == "blocked" else (q, d, s, ago) for q, d, s, ago, *_ in IRCTC]


def test_finds_what_broke_the_site_and_unblocks_it_whatever_blocked_it(make, fake):
    answer = {
        "site": "IRCTC",
        "keywords": ["irctc"],
        "summary": "IRCTC's own pages and its payment gateway were blocked.",
        "candidates": [
            {"name": "checkout.razorpay.com", "why": "IRCTC takes payments through Razorpay.", "confidence": "high"},
            {"name": "www.googletagmanager.com", "why": "Analytics; rarely needed.", "confidence": "low"},
            {"name": "irctc.co.in", "why": "Parent, never looked up.", "confidence": "high"},
            {"name": "pay.invented.example", "why": "Never blocked.", "confidence": "high"},
        ],
        "not_quietnet": False,
        "note": "",
    }
    c, calls, eng, dev = make(answer)
    lookups(eng, dev, IRCTC)
    known(eng, "googletagmanager.com", "tracking", "Google")
    block = rule(eng, dev, "irctc.co.in", household=True)

    r = c.post("/api/sites/fix", headers=H, json={"what": "IRCTC payment page won't load", "minutes": 15})
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["site"] == "IRCTC" and got["ai"] is True and got["looked_at"] == 4
    by_name = {x["name"]: x for x in got["candidates"]}
    # IRCTC's own names come first and become one: the rule that blocked all of irctc.co.in is lifted
    assert list(by_name) == ["irctc.co.in", "checkout.razorpay.com", "www.googletagmanager.com"]
    own = by_name["irctc.co.in"]
    assert own["looked_up"] == ["api.irctc.co.in", "static.irctc.co.in"] and own["times"] == 2
    assert own["blocked_by"] == "yours" and own["selected"] is True and own["devices"] == [{"id": dev["id"], "name": "Phone"}]
    pay = by_name["checkout.razorpay.com"]
    assert pay["blocked_by"] == "list" and pay["selected"] is True and pay["warning"] == ""
    gtm = by_name["www.googletagmanager.com"]
    assert gtm["selected"] is False and gtm["warning"].startswith("Usually a tracker")

    # What the model saw: the site's names first, what blocked each, what worked, nothing from hours ago
    user = calls[0]["messages"][-1]["content"]
    blocked = user.split("blocked by):", 1)[1].split("Some names", 1)[0].strip().splitlines()
    assert blocked[0].startswith(("static.irctc.co.in |", "api.irctc.co.in |"))
    assert "checkout.razorpay.com | - | not checked yet | 1 |" in user and user.count("| a block list") == 2
    assert "| your rule" in user and "www.irctc.co.in | - | 1" in user.split("that worked", 1)[1]
    assert "old.example.org" not in user

    # Unblocking lets both through: the household rule is replaced, the block list is overridden
    r = c.post("/api/sites/unblock", headers=H, json={"names": ["irctc.co.in", "checkout.razorpay.com"]})
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["count"] == 2 and out["replaced"] == [block["id"]] and out["synced"] is True
    assert not fake.match("static.irctc.co.in", dev["client_id"])[0]
    assert any(x.startswith("@@||checkout.razorpay.com^$client=") for x in fake.rules)
    assert c.get("/api/blocked", headers=H).json() == []

    # Asked again, they're no longer blocked
    again = c.post("/api/sites/fix", headers=H, json={"what": "IRCTC", "minutes": 15}).json()
    assert {"static.irctc.co.in", "checkout.razorpay.com"} <= set(again["already_unblocked"])
    assert [x["name"] for x in again["candidates"]] == ["www.googletagmanager.com"]

    # Undo puts the household block back and takes the allow rules away
    r = c.post("/api/decisions/undo-bulk", headers=H, json={"ids": [d["id"] for d in out["decisions"]], "restore": out["replaced"]})
    assert r.json()["count"] == 2 and r.json()["restored"] == 1
    assert fake.match("static.irctc.co.in", dev["client_id"])[0]
    assert not any(x.startswith(("@@||irctc.co.in^", "@@||checkout.razorpay.com^")) for x in fake.rules)
    assert [b["domain"] for b in c.get("/api/blocked", headers=H).json()] == ["irctc.co.in"]


def test_a_screenshot_goes_to_the_model(make):
    c, calls, eng, dev = make({"site": "SBI", "keywords": ["onlinesbi"], "summary": "Found it.", "candidates": [], "not_quietnet": False, "note": ""})
    lookups(eng, dev, [("retail.onlinesbi.sbi", "onlinesbi.sbi", "blocked", 20, "list"), ("x.tracker.example", "tracker.example", "blocked", 10, "list")])
    got = c.post("/api/sites/fix", headers=H, json={"image": PNG}).json()
    parts = calls[0]["messages"][-1]["content"]
    assert parts[0]["text"].startswith("Not working: (they sent only a screenshot)\nA screenshot of it is attached.")
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    # The model named the site but skipped its blocked name: it's added anyway, first and ticked
    assert [x["name"] for x in got["candidates"]] == ["retail.onlinesbi.sbi"]
    assert got["candidates"][0]["selected"] is True and "SBI's own names" in got["candidates"][0]["why"]


def test_without_ai_the_blocked_names_are_still_listed(make):
    c, calls, eng, dev = make(ai=False)
    lookups(eng, dev, LISTED + [("old.irctc.co.in", "irctc.co.in", "blocked", 30, "rule")])  # that rule is gone since
    got = c.post("/api/sites/fix", headers=H, json={"what": "irctc.co.in", "image": PNG}).json()
    assert got["ai"] is False and "screenshot wasn't read" in got["note"]
    assert got["already_unblocked"] == ["old.irctc.co.in"] and got["looked_at"] == 4
    names = [x["name"] for x in got["candidates"]]
    assert names[:2] == ["api.irctc.co.in", "static.irctc.co.in"]  # no QuietNet rule here: by exact name, newest first
    assert [x["selected"] for x in got["candidates"]] == [True, True, False, False]
    assert got["summary"].startswith("QuietNet blocked 4 names around then. The site's own names are first and ticked.")
    # Only a screenshot, and no AI to read it
    r = c.post("/api/sites/fix", headers=H, json={"image": PNG})
    assert r.status_code == 503 and "Type the site's name" in r.json()["detail"]
    assert c.post("/api/sites/fix", headers=H, json={"what": "x"}).status_code == 422


def test_when_the_ai_fails_the_list_still_comes_back(make):
    c, calls, eng, dev = make(None)
    lookups(eng, dev, LISTED[:2])
    r = c.post("/api/sites/fix", headers=H, json={"what": "IRCTC"})
    assert r.status_code == 200 and len(calls) == 1
    got = r.json()
    assert got["note"].startswith("The AI couldn't look just now") and [x["name"] for x in got["candidates"]] == ["static.irctc.co.in"]


def test_nothing_blocked_means_no_ai_call(make):
    c, calls, eng, dev = make({"candidates": []})
    got = c.post("/api/sites/fix", headers=H, json={"what": "IRCTC"}).json()
    assert calls == [] and got["summary"].startswith("QuietNet saw no lookups from your devices")
    lookups(eng, dev, IRCTC[:1])
    got = c.post("/api/sites/fix", headers=H, json={"what": "IRCTC"}).json()
    assert calls == [] and got["not_quietnet"] is True
    assert "isn't what's stopping it. The site's names (www.irctc.co.in) were looked up and answered." in got["summary"]


def test_one_device_and_its_own_unblock(make, fake):
    c, calls, eng, dev = make({"site": "", "keywords": [], "summary": "x", "candidates": [], "not_quietnet": False, "note": ""})
    tv = c.post("/api/devices", headers=H, json={"name": "TV", "type": "tv", "setup_method": "dot"}).json()["device"]
    lookups(eng, dev, [("cdn.hotstar.com", "hotstar.com", "blocked", 30, "rule")])
    lookups(eng, tv, [("img.zee5.com", "zee5.com", "blocked", 30, "rule")])
    held = rule(eng, tv, "zee5.com", action="quarantine", by="auto")
    hh = rule(eng, dev, "hotstar.com", household=True)
    got = c.post("/api/sites/fix", headers=H, json={"what": "ZEE5", "device_id": tv["id"]}).json()
    assert got["looked_at"] == 1 and "on TV (tv)" in calls[0]["messages"][-1]["content"]
    assert got["candidates"][0]["name"] == "zee5.com" and got["candidates"][0]["blocked_by"] == "held"

    # On the TV only: its hold is replaced; the household block stays for the phone, with the TV exempt
    out = c.post("/api/sites/unblock", headers=H, json={"names": ["zee5.com", "hotstar.com"], "device_id": tv["id"]}).json()
    assert set(out["replaced"]) == {held["id"]}
    assert not fake.match("img.zee5.com", tv["client_id"])[0] and not fake.match("cdn.hotstar.com", tv["client_id"])[0]
    assert fake.match("cdn.hotstar.com", dev["client_id"])[0]
    blocked = c.get("/api/blocked", headers=H).json()
    assert [(b["id"], [d["name"] for d in b["allowed_on"]]) for b in blocked] == [(hh["id"], ["TV"])]


def test_a_household_unblock_lifts_every_devices_block(make, fake):
    c, calls, eng, dev = make(ai=False)
    tv = c.post("/api/devices", headers=H, json={"name": "TV", "type": "tv", "setup_method": "dot"}).json()["device"]
    a = rule(eng, dev, "cdn.example.in", by="auto")
    b = rule(eng, tv, "cdn.example.in", action="quarantine", by="auto")
    out = c.post("/api/sites/unblock", headers=H, json={"names": ["cdn.example.in"]}).json()
    assert set(out["replaced"]) == {a["id"], b["id"]}
    assert c.get("/api/blocked", headers=H).json() == []
    assert not fake.match("cdn.example.in", tv["client_id"])[0]
    # Unblocking it again changes nothing; a name that isn't a domain is refused
    assert c.post("/api/sites/unblock", headers=H, json={"names": ["cdn.example.in"]}).json()["count"] == 0
    assert c.post("/api/sites/unblock", headers=H, json={"names": ["not a name"]}).status_code == 422


def test_nothing_automatic_blocks_again_what_the_person_unblocked(conn, user_id, add_device):
    """Auto-mode, holds and the malware list leave a name the person let through alone."""
    conn.execute("UPDATE users SET auto_mode = 1")
    phone = add_device("phone-1")
    conn.execute(
        "INSERT INTO domains(domain, root_domain, category, confidence, break_risk, first_seen) VALUES "
        "('bad.example', 'bad.example', 'malware', 'high', 'low', ?), ('ads.example', 'ads.example', 'ads', 'high', 'low', ?)",
        (now_iso(), now_iso()),
    )
    site_fix.unblock(conn, user_id, ["bad.example", "ads.example"])
    assert apply_policy(conn, phone, "bad.example") is False and apply_policy(conn, phone, "ads.example") is False
    assert active_decision(conn, "bad.example", phone) is None and active_decision(conn, "ads.example", phone) is None
    alert = conn.execute("SELECT message FROM alerts WHERE type = 'malware'").fetchone()
    assert "You unblocked it, so QuietNet let it through" in alert["message"]


def test_another_homes_household_rules_dont_count(conn, user_id, add_device):
    """A household rule is one home's: it never stops another home's malware block or Auto-mode."""
    conn.execute("UPDATE users SET auto_mode = 1")
    other = "other-home"
    conn.execute("INSERT INTO users(id, email, created_at) VALUES (?, 'b@example.test', ?)", (other, now_iso()))
    add_device("their-phone", uid=other)
    mine = add_device("my-phone")
    conn.execute(
        "INSERT INTO domains(domain, root_domain, category, confidence, break_risk, first_seen) VALUES "
        "('bad.example', 'bad.example', 'malware', 'high', 'low', ?)",
        (now_iso(),),
    )
    site_fix.unblock(conn, other, ["bad.example"])  # they let it through at their home
    assert apply_policy(conn, mine, "bad.example") is True
    assert active_decision(conn, "bad.example", mine)["action"] == "block"


def test_keywords_name_the_site():
    assert site_fix.keywords("IRCTC payment page won't load") == ["irctc"]
    assert site_fix.keywords("https://www.onlinesbi.sbi login not working") == ["onlinesbi", "sbi"]


def test_parental_blocks_are_not_offered_and_cant_be_unblocked_per_device(make):
    c, calls, eng, dev = make(ai=False)
    lookups(eng, dev, [
        ("www.tiktok.com", "tiktok.com", "blocked", 40, "parental"),
        ("checkout.razorpay.com", "razorpay.com", "blocked", 30, "list"),
    ])
    got = c.post("/api/sites/fix", headers=H, json={"what": "tiktok", "minutes": 15, "device_id": dev["id"]}).json()
    assert [x["name"] for x in got["candidates"]] == ["checkout.razorpay.com"]
    assert got["parental"] == ["www.tiktok.com"]

    r = c.post("/api/sites/unblock", headers=H, json={"names": ["www.tiktok.com"], "device_id": dev["id"]})
    assert r.status_code == 409 and "Parental page" in r.json()["detail"]
    # for the household it goes ahead: an allow can't lift a parental block, but it frees other devices
    assert c.post("/api/sites/unblock", headers=H, json={"names": ["www.tiktok.com"]}).status_code == 201


def test_only_parental_blocks_says_so(make):
    c, calls, eng, dev = make(ai=False)
    lookups(eng, dev, [("www.roblox.com", "roblox.com", "blocked", 40, "parental")])
    got = c.post("/api/sites/fix", headers=H, json={"what": "roblox", "minutes": 15}).json()
    assert got["candidates"] == [] and got["parental"] == ["www.roblox.com"] and "Parental page" in got["summary"]
