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
    assert not fake.match("xjq3k9zt7w2p.top", "someone-else")[0]

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
    fake.record("rahhul-phone", "api.mixpanel.com")
    await p.process(await Poller(conn, ag, 30).poll_once())
    m = _dom(conn, "mixpanel.com")
    assert m["explained_by"] == "lists" and m["category"] in ("tracking", "ads")
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
    fake.record("rahhul-secret-phone", "api.mixpanel.com")
    await p.process(await Poller(conn, ag, 30).poll_once())
    blob = json.dumps(sent)
    assert sent and "rahhul" not in blob and "10.0.0." not in blob
