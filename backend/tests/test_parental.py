"""Parental controls: bedtimes, layers that add up, the rules and client settings pushed to AdGuard, and the API."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from pahra import parental
from pahra.adguard.client import AdGuardClient
from pahra.adguard.rules import RuleSecurityError, assert_client_scoped, build_parental_rule, internet_off_rules, service_domains
from pahra.db import new_id, now_iso
from pahra.engine import Engine
from pahra.main import create_app
from pahra.poller import blocked_by_of, is_blocked

IST = ZoneInfo("Asia/Kolkata")
H = {"Authorization": "Bearer dev-token"}


def at(day: int, hhmm: str) -> datetime:
    """A wall-clock time in India on a day of the week (0 = Sunday), in a fixed week: 2026-11-22 is a Sunday."""
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 11, 22 + day, h, m, tzinfo=IST)


def profile(**kw) -> dict:
    base = {"adult": False, "safe_search": False, "apps": [], "bedtime_apps": [], "bedtime_internet_off": False, "bedtime": None}
    return base | kw


SCHOOL_NIGHTS = {"nights": [0, 1, 2, 3, 4], "start": "22:00", "end": "07:00"}  # Sunday to Thursday nights


# --- bedtime -------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "when, inside",
    [
        (at(1, "21:59"), False),  # Monday, before bedtime
        (at(1, "22:00"), True),  # Monday night, from the start minute
        (at(2, "06:59"), True),  # Tuesday morning, still Monday's night
        (at(2, "07:00"), False),  # ends at 07:00
        (at(5, "23:00"), False),  # Friday night isn't a school night
        (at(6, "06:00"), False),  # ... so Saturday morning is free
        (at(1, "03:00"), True),  # Monday 3am: Sunday was a school night
        (at(0, "03:00"), False),  # Sunday 3am: Saturday wasn't
    ],
)
def test_overnight_bedtime(when, inside):
    assert parental.in_bedtime(SCHOOL_NIGHTS, when) is inside


def test_same_day_window_and_week_wrap():
    homework = {"nights": [1], "start": "16:00", "end": "18:00"}
    assert parental.in_bedtime(homework, at(1, "16:30")) and not parental.in_bedtime(homework, at(1, "18:00"))
    assert not parental.in_bedtime(homework, at(2, "16:30"))
    saturday = {"nights": [6], "start": "23:00", "end": "08:00"}
    assert parental.in_bedtime(saturday, at(0, "07:00"))  # Saturday night runs into Sunday morning
    assert not parental.in_bedtime(None, at(0, "07:00"))


def test_bedtime_validation():
    assert parental.validate_bedtime({"nights": [3, 1, 1], "start": "21:30", "end": "06:45"}) == {
        "nights": [1, 3], "start": "21:30", "end": "06:45"
    }
    for bad in ({"nights": [7], "start": "21:00", "end": "07:00"}, {"nights": [1], "start": "25:00", "end": "07:00"},
                {"nights": [1], "start": "21:00", "end": "21:00"}):
        with pytest.raises(ValueError):
            parental.validate_bedtime(bad)
    with pytest.raises(ValueError):
        parental.validate_time_zone("Mars/Olympus")


# --- layers add up -------------------------------------------------------------------------------------------------
def test_layers_add_up_and_each_bedtime_is_its_own():
    household = profile(adult=True, apps=["dream11"])
    kid = profile(apps=["tiktok"], bedtime_apps=["youtube", "roblox"], bedtime=SCHOOL_NIGHTS)
    own = profile(safe_search=True, bedtime_internet_off=True, bedtime={"nights": [5], "start": "23:00", "end": "08:00"})

    day = parental.effective([household, kid, own], at(1, "15:00"))
    assert (day.adult, day.safe_search, day.apps, day.internet_off, day.bedtime) == (
        True, True, frozenset({"dream11", "tiktok"}), False, False
    )
    school_night = parental.effective([household, kid, own], at(1, "23:00"))
    assert school_night.apps == {"dream11", "tiktok", "youtube", "roblox"} and not school_night.internet_off
    friday_night = parental.effective([household, kid, own], at(5, "23:30"))  # only the device's own bedtime
    assert friday_night.internet_off and friday_night.apps == {"dream11", "tiktok"} and friday_night.bedtime
    assert not parental.effective([], at(1, "23:00")).active


def test_client_settings_only_when_needed():
    assert parental.client_settings(parental.Effective(apps=frozenset({"tiktok"}))) is None
    s = parental.client_settings(parental.Effective(adult=True))
    assert s["parental_enabled"] is True and s["safe_search"]["enabled"] is False
    s = parental.client_settings(parental.Effective(safe_search=True))
    assert s["parental_enabled"] is False and all(s["safe_search"].values()) and s["safe_search"]["youtube"]


# --- rules ---------------------------------------------------------------------------------------------------------
def test_parental_rules_are_client_scoped_and_important():
    assert build_parental_rule("tiktok.com", ["kid-b", "kid-a"]) == "||tiktok.com^$client='kid-a'|'kid-b',important"
    with pytest.raises(RuleSecurityError):
        build_parental_rule("tiktok.com", [])
    off = internet_off_rules(["kid-a"], ["whatsapp.net"])
    assert off == ["||*^$client='kid-a',important", "@@||whatsapp.net^$client='kid-a',important"]
    for r in off:
        assert_client_scoped(r)
    assert service_domains(["||tiktok.com^", "||*.weird.com^", "/regex/", "||ok.net^$important", "||b.tiktokv.com^"]) == [
        "b.tiktokv.com", "tiktok.com"
    ]


def test_devices_share_rules_and_internet_off_keeps_essentials():
    effs = {
        "kid-a": parental.Effective(apps=frozenset({"tiktok"})),
        "kid-b": parental.Effective(apps=frozenset({"tiktok"}), internet_off=True),
        "mum": parental.Effective(),
    }
    rules = parental.parental_rules(effs, {"tiktok": ["tiktok.com", "tiktokv.com"]})
    assert "||tiktok.com^$client='kid-a'|'kid-b',important" in rules
    assert "||*^$client='kid-b',important" in rules
    assert "@@||whatsapp.net^$client='kid-b',important" in rules
    assert not any("mum" in r for r in rules)


# --- pushed to AdGuard ---------------------------------------------------------------------------------------------
def _kid_setup(conn, user_id, add_device, **kid_rules):
    kid_dev, mum_dev = add_device("kid-phone"), add_device("mum-phone")
    kid = parental.create_profile(conn, user_id, "kid", "Aarav")
    parental.update_profile(conn, kid["id"], kid_rules)
    conn.execute("UPDATE devices SET parental_profile_id = ? WHERE id = ?", (kid["id"], kid_dev))
    conn.execute("UPDATE devices SET skip_household = 1 WHERE id = ?", (mum_dev,))
    return kid, kid_dev, mum_dev


async def test_sync_pushes_parental_rules_and_client_settings(conn, user_id, add_device, sync, fake):
    other = add_device("guest-tv", "tv")
    h = parental.household(conn, user_id)
    parental.update_profile(conn, h["id"], {"apps": ["dream11"]})
    _kid_setup(conn, user_id, add_device, adult=True, safe_search=True, apps=["tiktok"])
    # a household allow (e.g. from "Site not working?") can't undo a parent's block
    conn.execute(
        "INSERT INTO decisions(id, user_id, device_id, domain, action, adguard_rule, decided_by, created_at) "
        "VALUES (?, ?, NULL, 'tiktok.com', 'allow', '', 'user', ?)",
        (new_id(), user_id, now_iso()),
    )
    await sync.sync_all()

    assert fake.match("www.tiktok.com", "kid-phone")[0] is True
    assert fake.match("www.tiktok.com", "mum-phone")[0] is False
    assert fake.match("dream11.com", "kid-phone")[0] is True and fake.match("dream11.com", "guest-tv")[0] is True
    assert fake.match("dream11.com", "mum-phone")[0] is False  # mum skips the household's rules
    assert fake.verdict("adult.example", "kid-phone")[0] == "FilteredParental"
    assert fake.verdict("www.google.com", "kid-phone")[0] == "FilteredSafeSearch"
    assert fake.verdict("adult.example", "mum-phone")[0] == "NotFilteredNotFound"

    kid_client = fake.clients["kid-phone"]
    assert kid_client["use_global_settings"] is False and kid_client["parental_enabled"] is True
    assert kid_client["safe_search"]["youtube"] is True
    assert fake.clients["mum-phone"]["use_global_settings"] is True  # untouched: exactly as before
    assert other and fake.clients["guest-tv"]["use_global_settings"] is True
    assert sync.parental_changed() is False


async def test_bedtime_switches_on_the_tick(conn, user_id, add_device, sync, fake):
    kid, *_ = _kid_setup(conn, user_id, add_device, bedtime_internet_off=True, bedtime=SCHOOL_NIGHTS)
    afternoon = at(1, "15:00").astimezone(timezone.utc)
    night = at(1, "23:00").astimezone(timezone.utc)
    sync.parental_fp = parental.fingerprint(parental.effective_by_client(conn, afternoon))
    assert sync.parental_changed(afternoon) is False
    assert sync.parental_changed(night) is True  # the engine's tick runs sync_all() on this
    rules = parental.parental_rules(parental.effective_by_client(conn, night), {})
    assert "||*^$client='kid-phone',important" in rules
    assert parental.parental_rules(parental.effective_by_client(conn, afternoon), {}) == []


async def test_removing_controls_puts_the_client_back_on_global_settings(conn, user_id, add_device, sync, fake):
    kid, *_ = _kid_setup(conn, user_id, add_device, adult=True)
    await sync.sync_all()
    assert fake.clients["kid-phone"]["use_global_settings"] is False
    parental.update_profile(conn, kid["id"], {"adult": False})
    await sync.sync_all()
    assert fake.clients["kid-phone"]["use_global_settings"] is True


async def test_service_catalog_is_cached(sync, fake_app):
    cat = await sync.service_catalog()
    tiktok = next(s for s in cat if s["id"] == "tiktok")
    assert tiktok["group"] == "social_network" and "tiktokv.com" in tiktok["domains"] and tiktok["icon_svg"]
    assert next(s for s in cat if s["id"] == "9gag")["domains"] == ["9gag.com"]  # the odd rule is skipped
    fake_app.router.routes = [r for r in fake_app.router.routes if getattr(r, "path", "") != "/control/blocked_services/all"]
    assert await sync.service_catalog() == cat  # served from the cache


# --- query log -----------------------------------------------------------------------------------------------------
def test_query_log_reasons():
    parental_rule = {"reason": "FilteredBlackList", "rules": [{"filter_list_id": 0, "text": "||tiktok.com^$client='k',important"}]}
    ours = {"reason": "FilteredBlackList", "rules": [{"filter_list_id": 0, "text": "||ads.com^$client='k'"}]}
    lists = {"reason": "FilteredBlackList", "rules": [{"filter_list_id": 3, "text": "||ads.com^"}]}
    assert [blocked_by_of(e) for e in (parental_rule, ours, lists, {"reason": "FilteredParental"})] == [
        "parental", "rule", "list", "parental"
    ]
    assert not is_blocked({"reason": "FilteredSafeSearch"}) and is_blocked({"reason": "FilteredParental"})


# --- API -----------------------------------------------------------------------------------------------------------
@pytest.fixture
def api(settings, fake_app):
    ag = AdGuardClient("http://fake", "a", "b", transport=httpx.ASGITransport(app=fake_app))
    eng = Engine(settings, ag=ag)
    with TestClient(create_app(settings, eng, background=False)) as c:
        c.engine = eng
        yield c


def test_api_kid_profile_flow(api, fake_app):
    fake = fake_app.state.ag
    dev = api.post("/api/devices", json={"name": "Aarav phone", "type": "phone"}, headers=H).json()["device"]
    mum = api.post("/api/devices", json={"name": "Mum phone", "type": "phone"}, headers=H).json()["device"]

    o = api.get("/api/parental", headers=H).json()
    assert o["time_zone"] == "Asia/Kolkata" and o["household"]["kind"] == "household" and o["kids"] == []
    assert {d["name"] for d in o["devices"]} == {"Aarav phone", "Mum phone"} and "whatsapp.net" in o["essentials"]
    assert any(s["id"] == "roblox" for s in api.get("/api/parental/services", headers=H).json())

    r = api.post("/api/parental/profiles", json={"name": "Aarav"}, headers=H)
    assert r.status_code == 201
    kid = r.json()["created"]
    r = api.patch(
        f"/api/parental/profiles/{kid}",
        json={"adult": True, "apps": ["roblox"], "bedtime_apps": ["youtube"], "bedtime": {"nights": [1, 2], "start": "21:00", "end": "07:00"}},
        headers=H,
    )
    assert r.status_code == 200 and r.json()["synced"] is True
    r = api.patch(f"/api/parental/devices/{dev['id']}", json={"profile_id": kid}, headers=H).json()
    assert next(k for k in r["kids"] if k["id"] == kid)["devices"] == [dev["id"]]
    aarav = next(d for d in r["devices"] if d["id"] == dev["id"])
    assert aarav["now"]["adult"] is True and "roblox" in aarav["now"]["apps"]
    assert fake.match("www.roblox.com", dev["client_id"])[0] and not fake.match("www.roblox.com", mum["client_id"])[0]

    # own rules for one device, and a parent's device skipping the household's rules
    r = api.patch(f"/api/parental/devices/{dev['id']}", json={"own_rules": True}, headers=H).json()
    own = next(d for d in r["devices"] if d["id"] == dev["id"])["own"]
    api.patch(f"/api/parental/profiles/{own['id']}", json={"safe_search": True}, headers=H)
    api.patch(f"/api/parental/profiles/{r['household']['id']}", json={"apps": ["dream11"]}, headers=H)
    api.patch(f"/api/parental/devices/{mum['id']}", json={"skip_household": True}, headers=H)
    assert fake.verdict("www.google.com", dev["client_id"])[0] == "FilteredSafeSearch"
    assert fake.match("dream11.com", dev["client_id"])[0] and not fake.match("dream11.com", mum["client_id"])[0]

    # deleting the kid profile leaves the device with the household's and its own rules
    r = api.delete(f"/api/parental/profiles/{kid}", headers=H).json()
    after = next(d for d in r["devices"] if d["id"] == dev["id"])
    assert after["profile_id"] is None and after["now"]["apps"] == ["dream11"] and after["now"]["safe_search"]
    assert not fake.match("www.roblox.com", dev["client_id"])[0]


def test_api_validation(api):
    kid = api.post("/api/parental/profiles", json={"name": "Meera"}, headers=H).json()["created"]
    household = api.get("/api/parental", headers=H).json()["household"]["id"]
    bad = [
        (f"/api/parental/profiles/{kid}", {"apps": ["not-an-app"]}),
        (f"/api/parental/profiles/{kid}", {"bedtime": {"nights": [1], "start": "22:00", "end": "22:00"}}),
        (f"/api/parental/profiles/{household}", {"name": "Renamed"}),
    ]
    for url, body in bad:
        assert api.patch(url, json=body, headers=H).status_code == 422, body
    assert api.delete(f"/api/parental/profiles/{household}", headers=H).status_code == 422
    r = api.patch(f"/api/parental/profiles/{kid}", json={"bedtime": {"nights": [1], "start": "22:00", "end": "07:00"}}, headers=H)
    assert next(k for k in r.json()["kids"] if k["id"] == kid)["bedtime"]["start"] == "22:00"
    r = api.patch(f"/api/parental/profiles/{kid}", json={"bedtime": None}, headers=H)
    assert next(k for k in r.json()["kids"] if k["id"] == kid)["bedtime"] is None
    assert api.patch("/api/settings", json={"time_zone": "Nowhere/Land"}, headers=H).status_code == 422
    assert api.patch("/api/settings", json={"time_zone": "Europe/London"}, headers=H).json()["time_zone"] == "Europe/London"


def test_api_keeps_each_home_to_its_own(api):
    conn = api.engine.conn
    other = new_id()
    conn.execute("INSERT INTO users(id, email, created_at) VALUES (?, 'ravi@example.com', ?)", (other, now_iso()))
    their_kid = parental.create_profile(conn, other, "kid", "Ravi's kid")
    their_dev = new_id()
    conn.execute(
        "INSERT INTO devices(id, user_id, client_id, name, type, setup_method, created_at) VALUES (?, ?, 'ravi-tv', 'TV', 'tv', 'dot', ?)",
        (their_dev, other, now_iso()),
    )
    assert api.patch(f"/api/parental/profiles/{their_kid['id']}", json={"adult": True}, headers=H).status_code == 404
    assert api.delete(f"/api/parental/profiles/{their_kid['id']}", headers=H).status_code == 404
    assert api.patch(f"/api/parental/devices/{their_dev}", json={"own_rules": True}, headers=H).status_code == 404
    mine = api.post("/api/devices", json={"name": "My phone", "type": "phone"}, headers=H).json()["device"]
    r = api.patch(f"/api/parental/devices/{mine['id']}", json={"profile_id": their_kid["id"]}, headers=H)
    assert r.status_code == 404
    assert [d["name"] for d in api.get("/api/parental", headers=H).json()["devices"]] == ["My phone"]
