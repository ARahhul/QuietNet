"""QuietNet's block list and clean list (pahra.blocklist): built fast, loaded into AdGuard for every home, explained
without AI, and each home can still let a name through for itself."""

import asyncio
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from pahra.adguard.client import AdGuardClient
from pahra.blocklist import FILTER_NAME, BlocklistIndex, write_adguard_file
from pahra.db import new_id, now_iso
from pahra.domain_view import domain_detail
from pahra.engine import Engine
from pahra.knowledge import must_allow_all
from pahra.main import create_app
from pahra.pipeline.lists import ListIndex, parents

H = {"Authorization": "Bearer dev-token"}


@pytest.fixture(scope="module")
def lists():
    return ListIndex.sample()


@pytest.fixture(scope="module")
def index(lists):
    return BlocklistIndex.build(lists)


def test_lookups_cover_subdomains_but_not_whole_sites(index):
    assert index.entry("googleads.g.doubleclick.net").category == "ads"  # g.doubleclick.net is listed
    assert index.entry("doubleclick.net") is None and index.entry("com") is None
    # A normalised domain counts as listed when every name it was looked up as is listed
    assert index.match("doubleclick.net", ["googleads.g.doubleclick.net"]) is not None
    assert index.match("doubleclick.net", ["googleads.g.doubleclick.net", "www.doubleclick.net"]) is None
    alph = index.entry("tkacr.alphonso.tv")
    assert set(alph.sources) == {"HaGeZi Pro list", "HaGeZi LG webOS list"}


def test_must_allow_never_ends_up_on_the_list(index):
    must = must_allow_all()
    for d in index.adguard_domains():
        assert not any(p in must for p in parents(d)), d  # not a must-allow name or one of its subdomains
        assert not any(m.endswith("." + d) for m in must), d  # nor a parent whose block would cover one
    assert index.dropped_must_allow > 0


def test_the_adguard_file_only_ever_blocks(index, tmp_path):
    path, digest = write_adguard_file(index, tmp_path)
    rules = [line for line in path.read_text().splitlines() if line and not line.startswith("!")]
    assert rules and all(r.startswith("||") and r.endswith("^") and "$" not in r and "@@" not in r for r in rules)
    names = {r[2:-1] for r in rules}
    assert not any(p in names for n in names for p in parents(n)[1:-1])  # children of a listed parent are left out
    # threat intel only labels domains here: AdGuard loads that feed as its own filter
    assert all(index.blocked[n].in_adguard for n in names)
    assert write_adguard_file(index, tmp_path)[1] == digest  # same list, same digest (the timestamp isn't hashed)


def test_clean_list(index):
    assert index.clean_match("api-global.netflix.com").kind == "must_allow"
    assert index.clean_match("www.wikipedia.org").kind == "popular"
    assert index.clean_match("en.wikipedia.org") is None  # popular sites match exactly, not every subdomain
    # Popular isn't clean when a list or Tracker Radar flags it (doubleclick.net is in the top 10)
    assert index.clean_match("doubleclick.net") is None


def test_curated_csv_replaces_the_downloaded_lists(lists, tmp_path):
    csv = tmp_path / "domains.csv"
    csv.write_text(
        "domain,category,owner,sources,source_count,notes\n"
        "ads.criteo-example.com,ads,Criteo,hagezi-pro;oisd-big,2,\n"
        "*.samsungacr-example.com,acr,Samsung,hagezi-native.samsung,1,only seen on TVs\n"
        "connect.facebook-example.net,tracking,Meta,oisd-big,1,CAUTION: also used for login\n"
        "time.google.com,tracking,,x,1,\n"
        "cdn.example-video.com,clean,ExampleCo,manual,1,serves video\n",
        encoding="utf-8",
    )
    idx = BlocklistIndex.build(lists, csv)
    assert idx.source == "curated list domains.csv"
    e = idx.entry("x.ads.criteo-example.com")
    assert (e.category, e.owner, e.sources, e.break_risk) == ("ads", "Criteo", ("hagezi-pro", "oisd-big"), "low")
    acr = idx.entry("samsungacr-example.com")
    assert acr.category == "tracking" and acr.acr and acr.notes == "only seen on TVs"
    assert idx.entry("connect.facebook-example.net").break_risk == "medium"
    assert idx.entry("time.google.com") is None and idx.dropped_must_allow == 1
    assert idx.clean_match("cdn.example-video.com").owner == "ExampleCo"
    assert idx.entry("mixpanel.com") is None  # the downloaded lists aren't used any more


async def test_adguard_loads_the_list_once_and_reloads_it_when_it_changes(
    sync, fake, ag, index, lists, tmp_path, conn, caplog
):
    sync.blocklist_file, sync.blocklist_digest = write_adguard_file(index, tmp_path)
    assert await sync.sync_blocklist()
    assert await sync.sync_blocklist()  # idempotent: still one filter
    assert [f["name"] for f in fake.filters] == [FILTER_NAME]
    fid = fake.filters[0]["id"]
    before = fake.filters[0]["rules_count"]
    assert sync.blocklist_active and sync.blocklist_status()["filter_id"] == str(fid)
    # Every client, known or not, is turned away from a listed name
    assert fake.match("api.mixpanel.com", "anyone")[0]
    assert fake.match_detail("api.mixpanel.com", "anyone")[1] == [("||mixpanel.com^", fid)]

    # A new build (here: a curated list) is picked up without re-adding the filter
    csv = tmp_path / "domains.csv"
    csv.write_text("domain,category,owner,sources,source_count,notes\nads.example-new.com,ads,,x,1,\n", encoding="utf-8")
    sync.blocklist_file, sync.blocklist_digest = write_adguard_file(BlocklistIndex.build(lists, csv), tmp_path)
    with caplog.at_level(logging.INFO, logger="pahra.sync"):
        await sync.sync_blocklist()
    assert len(fake.filters) == 1 and fake.match("ads.example-new.com", "anyone")[0]
    assert not fake.match("api.mixpanel.com", "anyone")[0]
    after = fake.filters[0]["rules_count"]
    assert after != before
    assert f"AdGuard has the block list ({after} rules)" in caplog.text  # the reloaded file's count, not the old one


async def test_a_refused_list_is_retried_later(sync, tmp_path, conn, settings):
    from pahra.adguard.client import AdGuardError

    sync.blocklist_file, sync.blocklist_digest = tmp_path / "missing.txt", "x"  # AdGuard can't open it
    with pytest.raises(AdGuardError):
        await sync.sync_blocklist()
    assert not sync.blocklist_active and sync.blocklist_error
    assert not sync.blocklist_due()  # waits before trying again
    sync._blocklist_retry_at = 0
    assert sync.blocklist_due()


# ----------------------------------------------------------------------- through the API
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


def test_view_explains_a_list_block_and_unblock_lets_it_through(api, fake):
    assert api.engine.sync.blocklist_active  # the engine loaded the list into AdGuard at start
    health = api.get("/api/health", headers=H).json()["blocklist"]
    assert health["active"] and health["blocked_in_adguard"] > 0

    tv = api.post("/api/devices", json={"name": "Living room TV", "type": "tv", "brand": "LG"}, headers=H).json()["device"]
    phone = api.post("/api/devices", json={"name": "Phone", "type": "phone"}, headers=H).json()["device"]
    for _ in range(3):
        fake.record("living-room-tv", "tkacr.alphonso.tv")
    fake.record("phone", "api.mixpanel.com")
    fake.record("phone", "news.example-news.in")
    _drive(api)

    listed = api.get("/api/blocked/by-list", headers=H).json()
    assert listed["active"]
    rows = {g["domain"]: g for g in listed["domains"]}
    assert set(rows) == {"alphonso.tv", "mixpanel.com"}
    a = rows["alphonso.tv"]
    assert a["names"] == ["tkacr.alphonso.tv"] and a["count"] == 3 and a["devices"] == [{"id": tv["id"], "name": "Living room TV"}]
    assert a["owner"].startswith("Alphonso") and "HaGeZi LG webOS list" in a["sources"]
    only_phone = api.get("/api/blocked/by-list", params={"device_id": phone["id"]}, headers=H).json()["domains"]
    assert [g["domain"] for g in only_phone] == ["mixpanel.com"]

    v = api.get("/api/domains/tkacr.alphonso.tv", headers=H).json()
    assert (v["status"], v["blocked_by"], v["domain"], v["explained_by"]) == ("blocked", "blocklist", "alphonso.tv", "blocklist")
    why = next(r for r in v["reasons"] if r["kind"] == "blocklist")
    assert why["active"] and "HaGeZi Pro list" in why["sources"] and why["category"] == "tracking"
    assert v["looked_up"][0]["name"] == "tkacr.alphonso.tv" and v["looked_up"][0]["blocked"] == 3
    assert v["devices"] == [{"id": tv["id"], "name": "Living room TV", "count": 3, "blocked": 3, "last_seen": v["devices"][0]["last_seen"]}]
    assert v["actions"] == {"unblock": ["tkacr.alphonso.tv"], "undo_allow": None, "block": None}

    # Unblock for the TV only: its own allow rule beats the list there, the phone stays protected
    r = api.post("/api/sites/unblock", json={"names": v["actions"]["unblock"], "device_id": tv["id"]}, headers=H).json()
    assert r["synced"] and not fake.match("tkacr.alphonso.tv", "living-room-tv")[0]
    assert fake.match("tkacr.alphonso.tv", "phone")[0]
    on_tv = api.get("/api/domains/tkacr.alphonso.tv", params={"device_id": tv["id"]}, headers=H).json()
    assert on_tv["status"] == "allowed" and on_tv["blocked_by"] == "let_through"
    assert on_tv["actions"]["undo_allow"] == [r["decisions"][0]["id"]]
    assert api.get("/api/domains/tkacr.alphonso.tv", params={"device_id": phone["id"]}, headers=H).json()["status"] == "blocked"
    assert [g["domain"] for g in api.get("/api/blocked/by-list", headers=H).json()["domains"]] == ["mixpanel.com"]

    # "Block again" drops the allow rule, and the list applies once more
    api.post("/api/decisions/undo-bulk", json={"ids": on_tv["actions"]["undo_allow"]}, headers=H)
    assert fake.match("tkacr.alphonso.tv", "living-room-tv")[0]

    # Something on no list: allowed, with a Block button for a rule of this home's own
    n = api.get("/api/domains/news.example-news.in", headers=H).json()
    assert (n["status"], n["blocked_by"], n["actions"]["block"]) == ("allowed", None, "example-news.in")
    must = api.get("/api/domains/api-global.netflix.com", headers=H).json()
    assert must["must_allow"] and must["reasons"][0]["kind"] == "must_allow"
    assert api.get("/api/domains/not a domain", headers=H).status_code == 422


def test_view_of_a_whole_domain_follows_the_names_looked_up_under_it(api, fake):
    """Suggestions and the Report name a whole domain; the list may only cover the names looked up under it."""
    api.post("/api/devices", json={"name": "Phone", "type": "phone"}, headers=H)
    fake.record("phone", "googleads.g.doubleclick.net")  # g.doubleclick.net is listed, doubleclick.net isn't
    _drive(api)
    v = api.get("/api/domains/doubleclick.net", headers=H).json()
    assert (v["status"], v["blocked_by"]) == ("blocked", "blocklist")
    assert next(r for r in v["reasons"] if r["kind"] == "blocklist")["names"] == ["googleads.g.doubleclick.net"]
    # Unblock lets the names through, not every name under doubleclick.net
    assert v["actions"]["unblock"] == ["googleads.g.doubleclick.net"]
    r = api.post("/api/sites/unblock", json={"names": v["actions"]["unblock"]}, headers=H).json()
    after = api.get("/api/domains/doubleclick.net", headers=H).json()
    assert (after["status"], after["blocked_by"]) == ("allowed", "let_through")
    assert after["looked_up"][0]["let_through"] and after["actions"]["undo_allow"] == [r["decisions"][0]["id"]]
    assert after["actions"]["block"] is None
    # a name that was never looked up isn't covered by that unblock
    assert fake.match("other.g.doubleclick.net", "phone")[0]


def test_no_card_for_what_the_list_already_blocks_name_by_name(api, fake):
    """doubleclick.net isn't listed, g.doubleclick.net is: a device that only looked up listed names gets no card."""
    phone = api.post("/api/devices", json={"name": "Phone", "type": "phone"}, headers=H).json()["device"]
    tv = api.post("/api/devices", json={"name": "TV", "type": "tv", "brand": "LG"}, headers=H).json()["device"]
    fake.record("phone", "googleads.g.doubleclick.net")
    fake.record("tv", "googleads.g.doubleclick.net")
    fake.record("tv", "doubleclick.net")  # the bare domain isn't listed, so the TV got an answer for it
    _drive(api)

    cards = {c["domain"]: c for c in api.get("/api/suggestions", headers=H).json()}
    assert [d["name"] for d in cards["doubleclick.net"]["devices"]] == ["TV"]
    assert "doubleclick.net" not in {c["domain"] for c in api.get("/api/suggestions", params={"device_id": phone["id"]}, headers=H).json()}

    def on_device(dev):
        groups = api.get(f"/api/devices/{dev['id']}/activity", headers=H).json()
        return next(x for g in groups for x in g["domains"] if x["domain"] == "doubleclick.net")

    assert on_device(phone)["blocklist"] and not on_device(tv)["blocklist"]


def test_a_curated_list_dropped_in_later_is_picked_up_on_the_next_poll(api, fake, tmp_path):
    csv = tmp_path / "domains.csv"
    api.engine.settings.blocklist_csv_path = str(csv)
    asyncio.run(api.engine._on_tick())  # set, but no file yet: still the downloaded lists
    assert api.engine.blocklist.source != "curated list domains.csv" and fake.match("api.mixpanel.com", "anyone")[0]

    csv.write_text("domain,category,owner,sources,source_count,notes\nads.example-new.com,ads,,x,1,\n", encoding="utf-8")
    asyncio.run(api.engine._on_tick())
    assert api.engine.blocklist.source == "curated list domains.csv" and api.engine.pipeline.blocklist is api.engine.blocklist
    assert fake.match("ads.example-new.com", "anyone")[0] and not fake.match("api.mixpanel.com", "anyone")[0]
    assert len(fake.filters) == 1


def test_view_shows_only_this_homes_lookups(conn, user_id, add_device, index, lists):
    mine = add_device("my-phone")
    other_user = new_id()
    conn.execute("INSERT INTO users(id, email, created_at) VALUES (?, ?, ?)", (other_user, "other@example.com", now_iso()))
    theirs = add_device("their-phone", uid=other_user)
    import time

    for dev, n in ((mine, 1), (theirs, 5)):
        for _ in range(n):
            conn.execute(
                "INSERT INTO queries(device_id, t, qname, domain, status, blocked_by) VALUES (?, ?, 'api.mixpanel.com', "
                "'mixpanel.com', 'blocked', 'blocklist')",
                (dev, time.time()),
            )
    v = domain_detail(conn, user_id, "api.mixpanel.com", blocklist=index, blocklist_active=True, lists=lists)
    assert [d["id"] for d in v["devices"]] == [mine] and v["looked_up"][0]["count"] == 1
