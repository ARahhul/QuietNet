""""Saw an ad?": the AI picks, from what the household looked up around then, the names that served the ads."""

import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from pahra import ad_hunt
from pahra.adguard.client import AdGuardClient
from pahra.db import now_iso
from pahra.engine import Engine
from pahra.main import create_app
from pahra.pipeline.explainer import Explainer

H = {"Authorization": "Bearer dev-token"}
WHERE = "Hotstar app, banner on the home screen"


def _model(answer, calls: list) -> httpx.MockTransport:
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(json.loads(req.content))
        if answer is None:
            return httpx.Response(500, text="model down")
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer)}}]})

    return httpx.MockTransport(handler)


@pytest.fixture
def make(settings, fake_app):
    """make(answer) -> (TestClient, calls to the model, engine, router device)"""
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
        dev = c.post("/api/devices", headers=H, json={"name": "Home WiFi", "type": "router", "setup_method": "ipv4_demo"}).json()["device"]
        return c, calls, eng, dev

    yield _make
    for c in clients:
        c.__exit__(None, None, None)


def lookups(eng, dev, rows: list[tuple[str, str, str, float]]) -> None:
    """(qname, normalised domain, status, seconds ago)"""
    now = time.time()
    for qname, domain, status, ago in rows:
        eng.conn.execute(
            "INSERT INTO queries(device_id, t, qname, domain, qtype, status) VALUES (?,?,?,?,?,?)",
            (dev["id"], now - ago, qname, domain, "A", status),
        )


def known(eng, domain: str, category: str, owner: str) -> None:
    eng.conn.execute(
        "INSERT INTO domains(domain, root_domain, category, owner, confidence, first_seen) VALUES (?,?,?,?,?,?)",
        (domain, domain, category, owner, "high", now_iso()),
    )


HOTSTAR = [
    ("bifrost-api.hotstar.com", "bifrost-api.hotstar.com", "allowed", 40),
    ("brand-img1.hotstarext.com", "brand-img1.hotstarext.com", "allowed", 38),
    ("apix.hotstar.com", "apix.hotstar.com", "allowed", 60),
    ("hesads.akamaized.net", "hesads.akamaized.net", "blocked", 39),
    ("a.root-servers.net", "root-servers.net", "allowed", 20),
    ("www.wikipedia.org", "wikipedia.org", "allowed", 3 * 3600),  # long before: not looked at
]


def test_finds_the_apps_own_ad_servers_and_they_can_be_blocked(make, fake):
    answer = {
        "summary": "Hotstar's own ad API and its brand-image host were looked up when the banner showed.",
        "candidates": [
            {"name": "bifrost-api.hotstar.com", "role": "ad decisions", "why": "Hotstar asks it which ad to show.",
             "confidence": "high", "break_risk": "medium", "break_note": "Hotstar keeps playing; banners go blank."},
            {"name": "brand-img1.hotstarext.com", "role": "ad images", "why": "Serves brand pictures.",
             "confidence": "medium", "break_risk": "low", "break_note": "Ad pictures go blank."},
            {"name": "hotstar.com", "role": "ad server", "why": "Parent of the above.", "confidence": "high", "break_risk": "high"},
            {"name": "ads.invented.example", "role": "ad server", "why": "Never looked up.", "confidence": "high", "break_risk": "low"},
            {"name": "hesads.akamaized.net", "role": "ad server", "why": "Already blocked.", "confidence": "high", "break_risk": "low"},
            {"name": "a.root-servers.net", "role": "ad server", "why": "Nonsense.", "confidence": "low", "break_risk": "high"},
        ],
        "in_stream": False,
        "note": "",
    }
    c, calls, eng, dev = make(answer)
    lookups(eng, dev, HOTSTAR)
    known(eng, "bifrost-api.hotstar.com", "needed", "Hotstar")

    r = c.post("/api/ads/hunt", headers=H, json={"where": WHERE, "minutes": 15})
    assert r.status_code == 200, r.text
    got = r.json()
    assert [x["name"] for x in got["candidates"]] == ["bifrost-api.hotstar.com", "brand-img1.hotstarext.com"]
    bifrost, brand = got["candidates"]
    assert bifrost["must_allow"] is True and bifrost["owner"] == "Hotstar"
    # Only the name that says what it is comes ticked: Hotstar's API is shown for the person to choose
    assert brand["selected"] is True and bifrost["selected"] is False
    assert bifrost["role"] == "ad decisions" and bifrost["lookups"] == 1
    assert got["looked_at"] == 3 and got["in_stream"] is False

    # What the model saw: the app's names first, the blocked one apart, nothing from hours ago, no root servers
    user = calls[0]["messages"][-1]["content"]
    names = user.split("still answers", 1)[1].split("Already blocked", 1)[0]
    assert names.strip().splitlines()[1].startswith(("bifrost-api.hotstar.com |", "brand-img1.hotstarext.com |"))
    assert "bifrost-api.hotstar.com | Hotstar | needed | 1 |" in names
    assert "wikipedia" not in user and "root-servers" not in user
    assert "hesads.akamaized.net" in user.split("Already blocked", 1)[1]

    # Blocking what it found is an ordinary decision (the app's domain is must-allow, so it's confirmed)
    r = c.post("/api/decisions", headers=H, json={"domain": "bifrost-api.hotstar.com", "action": "block", "confirm_must_allow": True})
    assert r.status_code == 201, r.text
    assert any(x.startswith("||bifrost-api.hotstar.com^$client=") for x in fake.rules)


def test_live_video_ads_are_called_out(make):
    c, calls, eng, dev = make({"summary": "Nothing here looks like an ad name.", "candidates": [], "in_stream": False, "note": ""})
    lookups(eng, dev, [("live13p-ssai-akt-mum.cdn.hotstar.com", "cdn.hotstar.com", "allowed", 30)])
    got = c.post("/api/ads/hunt", headers=H, json={"where": "Hotstar, during the cricket", "minutes": 5}).json()
    assert got["candidates"] == [] and got["in_stream"] is True
    assert "stitched into the video" in got["note"]


def test_no_lookups_means_no_ai_call(make):
    c, calls, eng, dev = make({"candidates": []})
    got = c.post("/api/ads/hunt", headers=H, json={"where": WHERE, "minutes": 15}).json()
    assert calls == [] and got["candidates"] == []
    assert got["summary"].startswith("QuietNet saw no lookups from your devices in the last 15 minutes")


def test_one_device_only(make):
    c, calls, eng, dev = make({"summary": "", "candidates": [], "in_stream": False, "note": ""})
    other = c.post("/api/devices", headers=H, json={"name": "Phone", "type": "phone", "setup_method": "dot"}).json()["device"]
    lookups(eng, dev, HOTSTAR[:2])
    lookups(eng, other, [("ads.pubmatic.com", "pubmatic.com", "allowed", 10)])
    got = c.post("/api/ads/hunt", headers=H, json={"where": WHERE, "minutes": 15, "device_id": other["id"]}).json()
    assert got["looked_at"] == 1
    user = calls[0]["messages"][-1]["content"]
    assert "ads.pubmatic.com" in user and "bifrost" not in user and "on Phone (phone)" in user


def test_without_ai_or_when_it_fails(make):
    c, _, eng, dev = make(ai=False)
    assert c.post("/api/ads/hunt", headers=H, json={"where": WHERE}).status_code == 503
    c, _, eng, dev = make(None)
    lookups(eng, dev, HOTSTAR[:1])
    r = c.post("/api/ads/hunt", headers=H, json={"where": WHERE})
    assert r.status_code == 502 and "couldn't look" in r.json()["detail"]
    assert c.post("/api/ads/hunt", headers=H, json={"where": "x"}).status_code == 422


def test_keywords_name_the_app():
    assert ad_hunt.keywords("Hotstar app, banner on the home screen") == ["hotstar"]
    assert ad_hunt.keywords("ZEE5 before the video") == ["zee5"]


def test_a_busy_ai_is_asked_once_more(make):
    """The AI gateway's rate limit (HTTP 429) on the first try doesn't fail the lookup."""
    c, calls, eng, dev = make({"summary": "Found it.", "candidates": [], "in_stream": False, "note": ""})
    lookups(eng, dev, HOTSTAR[:2])
    tries = []
    answer = {"choices": [{"message": {"content": json.dumps({"summary": "Found it.", "candidates": [], "in_stream": False, "note": ""})}}]}

    def busy_once(req: httpx.Request) -> httpx.Response:
        tries.append(1)
        if len(tries) == 1:
            return httpx.Response(429, json={"error": {"message": "provider returned 429"}})
        return httpx.Response(200, json=answer)

    eng.explainer = Explainer("k", "m-1", base_url="https://gw.example/v1", transport=httpx.MockTransport(busy_once))
    eng.explainer.retry_delay = 0
    r = c.post("/api/ads/hunt", headers=H, json={"where": WHERE})
    assert r.status_code == 200, r.text
    assert r.json()["summary"] == "Found it." and len(tries) == 2

    # Busy twice in a row: the person is told to try again
    tries.clear()
    eng.explainer = Explainer("k", "m-1", base_url="https://gw.example/v1", transport=httpx.MockTransport(lambda req: tries.append(1) or httpx.Response(429)))
    eng.explainer.retry_delay = 0
    assert c.post("/api/ads/hunt", headers=H, json={"where": WHERE}).status_code == 502
    assert len(tries) == 2


def test_only_names_quietnet_can_confirm_come_ticked():
    """From a real lookup: the model was 'high confidence, low risk' about every one of these."""
    row = lambda category=None: {"owner": None, "category": category, "n": 3, "last": time.time()}
    sure = {"confidence": "high", "break_risk": "low", "role": "ad images", "why": "x"}
    names = ["brand-img1.hotstarext.com", "img.hotstar.com", "secure-media.hotstar.com", "cdn.fbsbx.com",
             "peoplestack-pa.googleapis.com", "ads.pubmatic.com"]
    answered = {n: row() for n in names} | {"tracker.example.net": row("tracking")}
    got = ad_hunt.clean({"candidates": [{"name": n} | sure for n in [*names[:5], "tracker.example.net"]]}, answered)
    ticked = {c["name"] for c in got["candidates"] if c["selected"]}
    assert ticked == {"brand-img1.hotstarext.com", "tracker.example.net"}


def test_the_named_apps_ad_hosts_are_never_left_out(make):
    """The model picked other apps' trackers and skipped Hotstar's brand-image host: it's added, first and ticked."""
    answer = {
        "summary": "Ad networks were active.",
        "candidates": [{"name": "a.klaviyo.com", "role": "ad network", "why": "Marketing.", "confidence": "high", "break_risk": "low"}],
        "in_stream": False,
        "note": "",
    }
    c, calls, eng, dev = make(answer)
    lookups(eng, dev, HOTSTAR + [("a.klaviyo.com", "klaviyo.com", "allowed", 30)])
    got = c.post("/api/ads/hunt", headers=H, json={"where": WHERE, "minutes": 15}).json()
    names = [x["name"] for x in got["candidates"]]
    assert names == ["brand-img1.hotstarext.com", "a.klaviyo.com"]
    brand = got["candidates"][0]
    assert brand["role"] == "ad images" and brand["selected"] is True and brand["confidence"] == "medium"
