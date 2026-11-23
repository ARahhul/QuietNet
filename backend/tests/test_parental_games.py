"""Parental controls together with the shared block list, and "Block games": QuietNet asks the AI once about each new
domain that devices with games blocked look up, and blocks the games it finds for those devices alone."""

import asyncio
import json
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from devtools import fake_llm
from pahra import games, parental
from pahra.adguard.client import AdGuardClient
from pahra.adguard.rules import SECTION_BEGIN, SECTION_END, assert_client_scoped, build_adguard_rule, is_parental_rule
from pahra.engine import Engine
from pahra.knowledge import must_allow_all
from pahra.main import create_app
from pahra.pipeline.explainer import Explainer

H = {"Authorization": "Bearer dev-token"}
AD = "tkacr.alphonso.tv"  # on the sample block list


def _model(calls: list):
    """The fake AI's game check, counting the calls."""

    def handler(req: httpx.Request) -> httpx.Response:
        msgs = json.loads(req.content)["messages"]
        calls.append(msgs)
        answer = fake_llm._game_check(msgs) if "mainly for games" in msgs[0]["content"] else {"domains": []}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer)}}]})

    return httpx.MockTransport(handler)


@pytest.fixture
def api(settings, fake_app):
    ag = AdGuardClient("http://fake", "a", "b", transport=httpx.ASGITransport(app=fake_app))
    eng = Engine(settings, ag=ag)
    calls: list = []
    eng.explainer = Explainer("k", "m-1", base_url="https://gw.example/v1", transport=_model(calls))
    with TestClient(create_app(settings, eng, background=False)) as c:
        c.engine, c.calls = eng, calls
        yield c


def _drive(api):
    async def go():
        await api.engine._on_observed(await api.engine.poller.poll_once())

    asyncio.run(go())


def _section(fake) -> list[str]:
    return fake.rules[fake.rules.index(SECTION_BEGIN) + 1 : fake.rules.index(SECTION_END)]


def _home(api, **kid_rules) -> tuple[dict, dict, str]:
    """A kid's phone in a kid profile with these rules, and a parent's phone. -> (kid device, parent device, profile)"""
    kid = api.post("/api/devices", json={"name": "Aarav phone", "type": "phone"}, headers=H).json()["device"]
    mum = api.post("/api/devices", json={"name": "Mum phone", "type": "phone"}, headers=H).json()["device"]
    pid = api.post("/api/parental/profiles", json={"name": "Aarav"}, headers=H).json()["created"]
    api.patch(f"/api/parental/profiles/{pid}", json=kid_rules, headers=H)
    api.patch(f"/api/parental/devices/{kid['id']}", json={"profile_id": pid}, headers=H)
    return kid, mum, pid


# --- a parent's block against the home's own allows ----------------------------------------------------------------
def test_a_parents_block_wins_over_the_homes_own_allows(api, fake):
    kid, mum, _ = _home(api, apps=["tiktok"])
    K, M = kid["client_id"], mum["client_id"]
    assert fake.match("www.tiktok.com", K)[0] and not fake.match("www.tiktok.com", M)[0]

    # The home lets names through for everyone (an allow is the strongest rule AdGuard has)
    api.post("/api/decisions", json={"domain": "tiktok.com", "action": "allow"}, headers=H)
    r = api.post("/api/sites/unblock", json={"names": ["v16.tiktokv.com"]}, headers=H).json()
    assert r["synced"]
    api.post("/api/decisions", json={"domain": "example-news.in", "action": "allow"}, headers=H)
    rules = _section(fake)
    assert build_adguard_rule("tiktok.com", [M], "allow") in rules  # the kid's phone is left out of it
    assert build_adguard_rule("v16.tiktokv.com", [M], "allow") in rules  # under a blocked name: left out too
    assert build_adguard_rule("example-news.in", [K, M], "allow") in rules  # nothing parental there: both keep it
    assert fake.match("www.tiktok.com", K)[0] and fake.match("v16.tiktokv.com", K)[0]
    assert not fake.match("www.tiktok.com", M)[0]
    # The query log tells a parent's block from the home's own rules, though both are `important`
    api.post("/api/decisions", json={"domain": "badsite.example", "action": "block"}, headers=H)
    fake.record(K, "www.tiktok.com")
    fake.record(K, "badsite.example")
    _drive(api)
    why = dict(api.engine.conn.execute("SELECT qname, blocked_by FROM queries WHERE device_id = ?", (kid["id"],)).fetchall())
    assert why == {"www.tiktok.com": "parental", "badsite.example": "rule"}
    kids = [x for x in _section(fake) if is_parental_rule(x)]
    assert kids and all(x.endswith(f"$important,client='{K}'") for x in kids)
    assert not any(is_parental_rule(x) for x in _section(fake) if x.endswith(",important"))  # the home's own


def test_bedtime_internet_off_leaves_the_device_out_of_every_allow_but_the_essentials(api, fake):
    kid, mum, _ = _home(api)
    K, M = kid["client_id"], mum["client_id"]
    for d in ("example-news.in", "whatsapp.net"):
        api.post("/api/decisions", json={"domain": d, "action": "allow"}, headers=H)
    bedtime = {K: parental.Effective(internet_off=True, bedtime=True), M: parental.Effective()}
    rules = api.engine.sync.desired_rules({}, effs=bedtime)
    assert build_adguard_rule("example-news.in", [M], "allow") in rules
    assert build_adguard_rule("whatsapp.net", [K, M], "allow") in rules  # an essential stays reachable anyway
    assert f"||*^$important,client='{K}'" in rules


def test_switching_the_list_off_keeps_the_adult_filter_on_kid_devices(api, fake):
    kid, mum, _ = _home(api, adult=True)
    K, M = kid["client_id"], mum["client_id"]
    api.patch("/api/settings", json={"shared_blocklist": False}, headers=H)
    optout = [r for r in _section(fake) if r.startswith("@@||*^")]
    assert optout == [f"@@||*^$client='{M}'"]  # an allow would skip the kid's adult filter
    assert fake.match("adult.example", K)[0] and fake.match(AD, K)[0]  # the list still applies to the kid
    assert not fake.match(AD, M)[0]


# --- Block games ---------------------------------------------------------------------------------------------------
def test_block_games_blocks_the_catalog_and_what_the_ai_finds_for_those_devices_only(api, fake):
    kid, mum, pid = _home(api, games=True)
    K, M = kid["client_id"], mum["client_id"]
    o = api.get("/api/parental", headers=H).json()
    assert next(k for k in o["kids"] if k["id"] == pid)["games"] is True
    assert next(d for d in o["devices"] if d["id"] == kid["id"])["now"]["games"] is True
    # AdGuard's gaming catalog straight away
    assert f"||roblox.com^$important,client='{K}'" in _section(fake)
    assert fake.match("www.roblox.com", K)[0] and not fake.match("www.roblox.com", M)[0]

    for name in ("ff.garena.com", "news.example-news.in", "api.mixpanel.com", "setup.rbxcdn.com", "connectivitycheck.gstatic.com"):
        fake.record(K, name)
    fake.record(M, "clashofclans.com")  # the parent's phone has no games block: never checked
    _drive(api)

    checks = {r["domain"]: (r["game"], r["source"]) for r in api.engine.conn.execute("SELECT * FROM game_checks")}
    assert checks == {
        "garena.com": (1, "ai"),
        "example-news.in": (0, "ai"),
        "mixpanel.com": (0, "known"),  # a tracker the list knows: not asked
        "rbxcdn.com": (1, "catalog"),  # in AdGuard's catalog: not asked
        "connectivitycheck.gstatic.com": (0, "known"),  # devices need it: never a game
    }
    assert len(api.calls) == 1 and "garena.com" in api.calls[0][-1]["content"]
    assert "clashofclans" not in json.dumps(api.calls)
    assert fake.match("ff.garena.com", K)[0] and not fake.match("ff.garena.com", M)[0]
    alerts = api.engine.conn.execute("SELECT device_id, domain, message FROM alerts WHERE type = 'game'").fetchall()
    assert {(a["device_id"], a["domain"]) for a in alerts} == {(kid["id"], "garena.com"), (kid["id"], "rbxcdn.com")}
    said = {a["domain"]: a["message"] for a in alerts}
    assert "hadn't seen before" in said["garena.com"] and "from now on" in said["garena.com"]
    assert "tried to open" in said["rbxcdn.com"] and "blocked it" in said["rbxcdn.com"]  # the catalog block came first

    # Each domain is asked about once
    fake.record(K, "ff.garena.com")
    fake.record(K, "news.example-news.in")
    _drive(api)
    assert len(api.calls) == 1

    # "Site not working?" for the whole home can't lift it for the kid; switching games off does
    api.post("/api/sites/unblock", json={"names": ["ff.garena.com"]}, headers=H)
    assert fake.match("ff.garena.com", K)[0] and not fake.match("ff.garena.com", M)[0]
    api.patch(f"/api/parental/profiles/{pid}", json={"games": False}, headers=H)
    assert not fake.match("ff.garena.com", K)[0] and not fake.match("www.roblox.com", K)[0]


def test_without_the_ai_the_check_waits_for_it(api, fake):
    kid, _, _ = _home(api, games=True)
    api.engine.explainer = Explainer("", "x")  # no AI key
    fake.record(kid["client_id"], "ff.garena.com")
    fake.record(kid["client_id"], "setup.rbxcdn.com")
    _drive(api)
    assert [r["domain"] for r in api.engine.conn.execute("SELECT domain FROM game_checks")] == ["rbxcdn.com"]
    api.engine.explainer = Explainer("k", "m-1", base_url="https://gw.example/v1", transport=_model(api.calls))
    fake.record(kid["client_id"], "ff.garena.com")
    _drive(api)
    assert api.engine.conn.execute("SELECT game FROM game_checks WHERE domain = 'garena.com'").fetchone()[0] == 1


def test_games_at_bedtime_only():
    layer = {
        "adult": False, "safe_search": False, "apps": [], "bedtime_apps": [], "bedtime_internet_off": False,
        "games": False, "bedtime_games": True, "bedtime": {"nights": list(range(7)), "start": "21:00", "end": "07:00"},
    }
    night, day = datetime(2026, 11, 24, 22, 0), datetime(2026, 11, 24, 12, 0)
    assert parental.effective([layer], night).games and parental.effective([layer], night).bedtime
    assert not parental.effective([layer], day).games
    assert parental.parental_rules({"kid": parental.effective([layer], night)}, {}, ["garena.com"]) == [
        "||garena.com^$important,client='kid'"
    ]
    assert parental.parental_rules({"kid": parental.effective([layer], day)}, {}, ["garena.com"]) == []


def test_bedtime_games_devices_get_their_lookups_checked(conn, user_id, add_device):
    did = add_device("kid-tab")
    p = parental.create_profile(conn, user_id, "kid", "Meera")
    parental.update_profile(conn, p["id"], {"bedtime_games": True})
    conn.execute("UPDATE devices SET parental_profile_id = ? WHERE id = ?", (p["id"], did))
    add_device("mum-phone")
    assert parental.game_devices(conn) == {did}


def test_what_an_allow_would_undo():
    assert parental.holds({"tiktok.com"}, "tiktok.com") and parental.holds({"tiktok.com"}, "v16.tiktok.com")
    assert parental.holds({"v16.tiktok.com"}, "tiktok.com")  # an allow on the parent covers the blocked name
    assert not parental.holds({"tiktok.com"}, "nottiktok.com") and not parental.holds(None, "tiktok.com")
    assert parental.holds(parental.ALL, "example.com") and not parental.holds(parental.ALL, parental.essentials()[0])


def test_must_allow_names_are_never_blocked_as_games():
    m = sorted(n for n in must_allow_all() if n.count(".") >= 2)[0]
    parent = m.split(".", 1)[1]
    assert games.blockable([m, parent, "garena.com", "not a domain"]) == ["garena.com"]
    for r in parental.parental_rules({"kid": parental.Effective(games=True)}, {}, games.blockable([m, "garena.com"])):
        assert_client_scoped(r)
