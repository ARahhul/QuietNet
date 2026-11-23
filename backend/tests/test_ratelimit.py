"""The sliding-window limiter behind the per-account rate limits."""

from pahra.ratelimit import AD_HUNT, DAY, MINUTE, Limiter, blocked


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make():
    clock = Clock()
    return Limiter(clock), clock


def test_allows_up_to_the_limit_then_says_how_long_to_wait():
    lim, clock = make()
    rules = ((3, 60),)
    assert [lim.take(("u", "x"), rules) for _ in range(3)] == [0, 0, 0]
    clock.now += 20
    assert lim.take(("u", "x"), rules) == 40  # the first hit leaves the window 60s after it, 40s from now


def test_the_window_slides_instead_of_resetting():
    lim, clock = make()
    rules = ((2, 60),)
    lim.take(("u", "x"), rules)
    clock.now += 30
    lim.take(("u", "x"), rules)
    clock.now += 31  # the first hit is now 61s old; the second is 31s old
    assert lim.take(("u", "x"), rules) == 0
    assert lim.take(("u", "x"), rules) == 29  # two inside the window again: the older one is 31s old


def test_refused_requests_do_not_use_up_the_allowance():
    lim, clock = make()
    rules = ((1, 60),)
    lim.take(("u", "x"), rules)
    for _ in range(50):
        assert lim.take(("u", "x"), rules) > 0
    clock.now += 61
    assert lim.take(("u", "x"), rules) == 0  # hammering while blocked didn't push the wait out


def test_accounts_and_buckets_are_counted_apart():
    lim, _ = make()
    rules = ((1, 60),)
    assert lim.take(("asha", "hunt"), rules) == 0
    assert lim.take(("asha", "hunt"), rules) > 0
    assert lim.take(("ravi", "hunt"), rules) == 0
    assert lim.take(("asha", "api"), rules) == 0


def test_every_rule_of_a_bucket_must_pass_and_the_longest_wait_is_reported():
    lim, clock = make()
    rules = ((2, MINUTE), (3, DAY))
    for _ in range(2):
        lim.take(("u", "hunt"), rules)
    clock.now += 61
    lim.take(("u", "hunt"), rules)  # third of the day
    clock.now += 61
    assert 0 < lim.take(("u", "hunt"), rules) <= DAY  # the daily rule says no, though the minute rule has room
    assert lim.take(("u", "hunt"), rules) > MINUTE


def test_a_day_of_idleness_empties_the_table():
    lim, clock = make()
    for i in range(100):
        lim.record((f"u{i}", "api"))
    clock.now += DAY + 700
    lim.record(("late", "api"))
    assert list(lim._hits) == [("late", "api")]


def test_messages_are_plain():
    daily = blocked("ad_hunt", 7200, AD_HUNT)
    assert daily.status_code == 429 and daily.headers["Retry-After"] == "7200"
    assert daily.detail == "You've used today's 60 ad lookups. Try again in about 2 hours."
    assert blocked("ad_hunt", 12, AD_HUNT).detail == "That's a lot at once. Wait 12 seconds and try again."
