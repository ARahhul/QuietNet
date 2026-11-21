from datetime import datetime, timedelta, timezone

from pahra.events import is_beacon
from pahra.poller import Poller


async def test_poll_aggregates_and_resumes_from_cursor(conn, ag, fake, add_device):
    dev = add_device("rahhul-tv", "tv", "LG")
    t0 = datetime.now(timezone.utc) - timedelta(minutes=10)
    for i in range(8):  # 20 s beacon
        fake.record("rahhul-tv", "tkacr.alphonso.tv", t0 + timedelta(seconds=20 * i))
    fake.record("rahhul-tv", "ads.doubleclick.net", t0)
    fake.record("rahhul-tv", "metrics.icloud.com", t0)
    fake.record("stranger", "evil.com", t0)

    p = Poller(conn, ag, 30)
    observed = await p.poll_once()
    domains = {o.domain for o in observed}
    assert domains == {"alphonso.tv", "doubleclick.net", "metrics.icloud.com"}
    alph = next(o for o in observed if o.domain == "alphonso.tv")
    assert alph.qnames == {"tkacr.alphonso.tv"} and alph.is_new

    row = conn.execute("SELECT * FROM events WHERE device_id = ? AND domain = 'alphonso.tv'", (dev,)).fetchone()
    assert row["count"] == 8
    assert abs(row["avg_interval_s"] - 20) < 0.5
    assert is_beacon(row)
    assert conn.execute("SELECT last_seen FROM devices WHERE id = ?", (dev,)).fetchone()[0]

    # nothing new -> nothing observed, counts unchanged
    assert await p.poll_once() == []
    fake.record("rahhul-tv", "alphonso.tv")
    again = await p.poll_once()
    assert [o.domain for o in again] == ["alphonso.tv"] and not again[0].is_new
    assert conn.execute("SELECT count FROM events WHERE domain='alphonso.tv'").fetchone()[0] == 9


async def test_blocked_queries_are_counted(conn, ag, fake, add_device, user_id, sync):
    from pahra.decisions import create_decision

    dev = add_device("rahhul-phone")
    create_decision(conn, user_id, "doubleclick.net", "block", device_id=dev)
    await sync.sync_rules()
    fake.record("rahhul-phone", "ads.doubleclick.net")
    await Poller(conn, ag, 30).poll_once()
    row = conn.execute("SELECT count, blocked_count FROM events WHERE domain='doubleclick.net'").fetchone()
    assert tuple(row) == (1, 1)
