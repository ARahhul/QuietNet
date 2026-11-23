import json

import pytest

from pahra.decisions import active_decision
from pahra.pipeline.clues import ClueFetcher
from pahra.pipeline.confidence import UNCONFIRMED_TEXT, compute_confidence
from pahra.pipeline.explainer import Explainer
from pahra.pipeline.fastchecks import dga_score, fast_check, pattern_hits
from pahra.pipeline.lists import ListIndex
from pahra.pipeline.run import Pipeline
from pahra.poller import Observed, Poller


@pytest.fixture(scope="module")
def lists():
    return ListIndex.sample()


@pytest.mark.parametrize(
    "lists_n,owner,threat,pattern,beacon,expected",
    [
        (2, False, False, False, False, "high"),
        (0, True, False, False, False, "high"),
        (0, False, True, False, False, "high"),
        (1, False, False, False, False, "medium"),
        (0, False, False, True, True, "medium"),
        (0, False, False, True, False, "low"),
        (0, False, False, False, False, "low"),
    ],
)
def test_confidence_table(lists_n, owner, threat, pattern, beacon, expected):
    assert compute_confidence(list_hits=lists_n, tracker_owner=owner, threat=threat, pattern=pattern, beacon=beacon) == expected


def test_fast_checks(lists):
    f = fast_check(lists, "alphonso.tv", ["tkacr.alphonso.tv"])
    assert "HaGeZi LG webOS list" in f.list_hits and "HaGeZi Pro list" in f.list_hits
    assert "acr" in f.patterns
    d = fast_check(lists, "doubleclick.net", ["googleads.g.doubleclick.net"])
    assert d.tracker_owner and d.tracker_confirmed and d.tracker_category in ("ads", "tracking")
    assert dga_score("xjq3k9zt7w2p.top") >= 0.6
    assert dga_score("netflix.com") == 0
    assert pattern_hits("device-metrics-us.amazon.com") == ["metrics"]
    unknown = fast_check(lists, "xjq3k9zt7w2p.top", ["xjq3k9zt7w2p.top"])
    assert unknown.suspicious_unknown and not unknown.known


@pytest.fixture
def pipeline(conn, lists, sync):
    return Pipeline(conn, lists, ClueFetcher(conn, online=False), Explainer("", "x"), sync)


def _dom(conn, d):
    r = conn.execute("SELECT * FROM domains WHERE domain = ?", (d,)).fetchone()
    return dict(r) if r else None


async def test_end_to_end_lg_tv(conn, ag, fake, user_id, add_device, pipeline):
    tv = add_device("rahhul-lg-tv", "tv", "LG")
    from datetime import datetime, timedelta, timezone

    t0 = datetime.now(timezone.utc) - timedelta(minutes=5)
    for i in range(6):
        fake.record("rahhul-lg-tv", "tkacr.alphonso.tv", t0 + timedelta(seconds=20 * i))
    for q in ["us.info.lgsmartad.com", "api-global.netflix.com", "snu.lge.com", "news.example-news.in", "xjq3k9zt7w2p.top"]:
        fake.record("rahhul-lg-tv", q, t0)

    obs = await Poller(conn, ag, 30).poll_once()
    await pipeline.process(obs)

    alph = _dom(conn, "alphonso.tv")
    assert alph["confidence"] == "high" and alph["category"] == "tracking"
    assert alph["owner"].startswith("Alphonso")
    assert "HaGeZi LG webOS list" in json.loads(alph["sources"])
    assert json.loads(alph["signals"])["acr"]

    # must-allow: Netflix and LG firmware are "needed", never quarantined
    assert _dom(conn, "api-global.netflix.com")["category"] == "needed"
    assert _dom(conn, "snu.lge.com")["category"] == "needed"

    # unknown site -> honest unknown card, no invented purpose
    news = _dom(conn, "example-news.in")
    assert news["confidence"] == "low" and news["what_it_does"] == UNCONFIRMED_TEXT and news["owner"] is None

    # random-looking unknown domain -> quarantined on that device and pushed with $client
    q = active_decision(conn, "xjq3k9zt7w2p.top", tv)
    assert q and q["action"] == "quarantine"
    assert fake.match("xjq3k9zt7w2p.top", "rahhul-lg-tv")[0]
    blocked, rules = fake.match("xjq3k9zt7w2p.top", "someone-else")  # only ever the rescue catch-all
    assert not blocked or all(r.startswith("||*^$client=~") for r in rules)

    # auto-mode is OFF by default: known trackers are cards, not blocks (keeps the "Block all" moment)
    assert active_decision(conn, "alphonso.tv", tv) is None

    # LG Live Plus alert with the fix guide
    alert = conn.execute("SELECT * FROM alerts WHERE type = 'acr_tracker'").fetchone()
    assert alert and "Live Plus" in json.loads(alert["fix_steps"])["title"]


async def test_auto_mode_blocks_high_confidence(conn, ag, fake, user_id, add_device, pipeline):
    conn.execute("UPDATE users SET auto_mode = 1")
    phone = add_device("rahhul-phone")
    fake.record("rahhul-phone", "googleads.g.doubleclick.net")
    fake.record("rahhul-phone", "www.google.com")
    await pipeline.process(await Poller(conn, ag, 30).poll_once())
    d = active_decision(conn, "doubleclick.net", phone)
    assert d and d["action"] == "block" and d["decided_by"] == "auto"
    assert fake.match("ads.doubleclick.net", "rahhul-phone")[0]
    assert active_decision(conn, "www.google.com", phone) is None


async def test_auto_mode_leaves_what_might_break_to_the_person(conn, ag, fake, user_id, add_device, pipeline):
    """Seen live: auto-mode blocked Facebook's dgw.c10r.facebook.com (medium break risk) and was about to block
    akadns.net, which Microsoft sign-in and many big sites resolve through."""
    conn.execute("UPDATE users SET auto_mode = 1")
    router = add_device("home-router", "router")
    fake.record("home-router", "googleads.g.doubleclick.net")
    await pipeline.process(await Poller(conn, ag, 30).poll_once())
    assert active_decision(conn, "doubleclick.net", router)["decided_by"] == "auto"

    # Medium break risk: stays a suggestion
    conn.execute("UPDATE domains SET break_risk = 'medium' WHERE domain = 'doubleclick.net'")
    conn.execute("UPDATE decisions SET undone_at = '2026-01-01T00:00:00Z'")
    fake.record("home-router", "googleads.g.doubleclick.net")
    await pipeline.process(await Poller(conn, ag, 30).poll_once())
    assert active_decision(conn, "doubleclick.net", router) is None

    # Akamai's DNS is never auto-blocked, whatever a list says about it
    from pahra.knowledge import is_must_allow

    assert is_must_allow("akadns.net", "router") and is_must_allow("login.msa.akadns6.net", "phone")


async def test_threat_feed_domain_is_blocked_with_alert(conn, ag, fake, user_id, add_device, pipeline, lists):
    bad = sorted(next(v for k, v in lists.lists.items() if lists.kinds[k] == "threat"))[0]
    phone = add_device("rahhul-phone")
    fake.record("rahhul-phone", bad)
    await pipeline.process(await Poller(conn, ag, 30).poll_once())
    from pahra.pipeline.normalize import normalize_domain

    nd = normalize_domain(bad)
    assert _dom(conn, nd)["category"] == "malware"
    assert active_decision(conn, nd, phone)["action"] == "block"
    assert conn.execute("SELECT COUNT(*) FROM alerts WHERE type='malware'").fetchone()[0] == 1


async def test_gemini_failure_falls_back(conn, ag, fake, user_id, add_device, lists, sync):
    class Boom(Explainer):
        def __init__(self):
            super().__init__("", "x")
            self.client = object()  # pretend enabled

        async def _call(self, batch):
            raise RuntimeError("429 RESOURCE_EXHAUSTED")

    p = Pipeline(conn, lists, ClueFetcher(conn, online=False), Boom(), sync)
    add_device("rahhul-phone")
    fake.record("rahhul-phone", "news.example-news.in")  # on no list: only this one goes to the AI
    await p.process(await Poller(conn, ag, 30).poll_once())
    m = _dom(conn, "example-news.in")
    assert m["explained_by"] == "lists" and m["category"] == "unknown" and m["what_it_does"] == UNCONFIRMED_TEXT
    assert "RESOURCE_EXHAUSTED" in p.explainer.last_error


async def test_gemini_payload_contains_no_device_or_ip_data(conn, ag, fake, user_id, add_device, lists, sync):
    sent = []

    class Spy(Explainer):
        def __init__(self):
            super().__init__("", "x")
            self.client = object()

        async def _call(self, batch):
            sent.extend(c.for_llm() for c in batch)
            return {}

    p = Pipeline(conn, lists, ClueFetcher(conn, online=False), Spy(), sync)
    add_device("rahhul-secret-phone")
    fake.record("rahhul-secret-phone", "news.example-news.in")
    await p.process(await Poller(conn, ag, 30).poll_once())
    blob = json.dumps(sent)
    assert sent and "rahhul" not in blob and "10.0.0." not in blob


class CountingAI(Explainer):
    """Pretends to be a working model and records every domain it was asked about."""

    def __init__(self):
        super().__init__("", "x")
        self.client = object()
        self.asked: list[str] = []

    async def _call(self, batch):
        self.asked.extend(c.domain for c in batch)
        return {}


async def test_only_unknown_domains_go_to_the_ai(conn, ag, fake, user_id, add_device, lists, sync):
    ai = CountingAI()
    p = Pipeline(conn, lists, ClueFetcher(conn, online=False), ai, sync)
    add_device("rahhul-phone")
    for q in [
        "api.mixpanel.com",  # block list (HaGeZi Pro)
        "googleads.g.doubleclick.net",  # every name it was looked up as is listed (g.doubleclick.net)
        "api-global.netflix.com",  # must-allow
        "www.wikipedia.org",  # one of the most visited sites, on no list
        "news.example-news.in",  # unknown
    ]:
        fake.record("rahhul-phone", q)
    await p.process(await Poller(conn, ag, 30).poll_once())
    assert ai.asked == ["example-news.in"]
    assert p.known_without_ai == 4

    mix = _dom(conn, "mixpanel.com")
    assert (mix["explained_by"], mix["category"], mix["confidence"]) == ("blocklist", "tracking", "high")
    assert mix["owner"] == "Mixpanel, Inc." and json.loads(mix["sources"]) == ["HaGeZi Pro list"]
    assert json.loads(mix["signals"])["blocklist"]
    assert _dom(conn, "doubleclick.net")["explained_by"] == "blocklist"
    assert _dom(conn, "api-global.netflix.com")["explained_by"] == "must_allow"
    wiki = _dom(conn, "wikipedia.org")
    assert (wiki["category"], wiki["explained_by"], wiki["sources"]) == ("needed", "clean_list", '["Tranco top sites"]')

    # Seen again: answered from the cache, the AI isn't asked twice
    fake.record("rahhul-phone", "news.example-news.in")
    await p.process(await Poller(conn, ag, 30).poll_once())
    assert ai.asked == ["example-news.in"]


async def test_listed_domains_are_not_auto_blocked_twice(conn, ag, fake, user_id, add_device, lists, sync, tmp_path):
    """AdGuard already blocks listed domains for everyone: Auto-mode adds no rule of its own for them."""
    from pahra.blocklist import write_adguard_file

    conn.execute("UPDATE users SET auto_mode = 1")
    p = Pipeline(conn, lists, ClueFetcher(conn, online=False), Explainer("", "x"), sync)
    sync.blocklist_file, sync.blocklist_digest = write_adguard_file(p.blocklist, tmp_path)
    await sync.sync_blocklist()
    phone = add_device("rahhul-phone")
    fake.record("rahhul-phone", "api.mixpanel.com")
    fake.record("rahhul-phone", "googleads.g.doubleclick.net")
    await p.process(await Poller(conn, ag, 30).poll_once())
    assert active_decision(conn, "mixpanel.com", phone) is None  # the list blocks it
    assert fake.match("api.mixpanel.com", "rahhul-phone")[0]
    # doubleclick.net as a whole isn't listed (only g.doubleclick.net), so Auto-mode still covers the rest of it
    assert active_decision(conn, "doubleclick.net", phone)["decided_by"] == "auto"
    assert conn.execute("SELECT COUNT(*) FROM alerts WHERE domain = 'mixpanel.com'").fetchone()[0] == 0
