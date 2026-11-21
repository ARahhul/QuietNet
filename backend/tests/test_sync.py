from pahra.adguard.rules import SECTION_BEGIN, SECTION_END
from pahra.decisions import create_decision, undo_decision


async def test_block_on_device_a_does_not_affect_device_b(conn, user_id, sync, fake, add_device):
    a = add_device("alice-phone")
    add_device("bob-phone", uid=None)
    create_decision(conn, user_id, "doubleclick.net", "block", device_id=a)
    assert await sync.sync_rules() is True

    assert fake.match("ads.doubleclick.net", "alice-phone")[0] is True
    assert fake.match("ads.doubleclick.net", "bob-phone")[0] is False
    assert fake.match("ads.doubleclick.net", "stranger")[0] is False


async def test_other_users_devices_are_untouched_by_household_rule(conn, user_id, sync, fake, add_device):
    from pahra.db import ensure_seed_user

    other = ensure_seed_user(conn, "other@x.in")
    add_device("rahhul-tv")
    add_device("rahhul-phone")
    add_device("neighbour-tv", uid=other)
    create_decision(conn, user_id, "alphonso.tv", "block")  # whole household
    await sync.sync_rules()
    assert fake.match("alphonso.tv", "rahhul-tv")[0]
    assert fake.match("alphonso.tv", "rahhul-phone")[0]
    assert not fake.match("alphonso.tv", "neighbour-tv")[0]


async def test_sync_is_idempotent_and_undo_removes_exactly_one_line(conn, user_id, sync, fake, add_device):
    fake.rules = ["||operator-own.example^"]
    d = add_device("rahhul-tv")
    d1 = create_decision(conn, user_id, "alphonso.tv", "block", device_id=d)
    create_decision(conn, user_id, "lgsmartad.com", "block", device_id=d)
    assert await sync.sync_rules() is True
    assert await sync.sync_rules() is False  # nothing changed

    before = list(fake.rules)
    undo_decision(conn, user_id, d1["id"])
    await sync.sync_rules()
    removed = set(before) - set(fake.rules)
    assert removed == {d1["adguard_rule"]}
    assert fake.rules[0] == "||operator-own.example^"
    assert SECTION_BEGIN in fake.rules and SECTION_END in fake.rules
    assert not fake.match("alphonso.tv", "rahhul-tv")[0]


async def test_household_rule_grows_with_new_device(conn, user_id, sync, fake, add_device):
    add_device("rahhul-phone")
    create_decision(conn, user_id, "doubleclick.net", "block")
    await sync.sync_rules()
    add_device("rahhul-laptop")
    await sync.sync_rules()
    assert fake.match("doubleclick.net", "rahhul-laptop")[0]


async def test_allow_overrides_block_for_one_device(conn, user_id, sync, fake, add_device):
    tv = add_device("rahhul-tv")
    add_device("rahhul-phone")
    create_decision(conn, user_id, "lgtvsdp.com", "block")
    create_decision(conn, user_id, "lgtvsdp.com", "allow", device_id=tv)
    await sync.sync_rules()
    assert not fake.match("us.lgtvsdp.com", "rahhul-tv")[0]
    assert fake.match("us.lgtvsdp.com", "rahhul-phone")[0]


async def test_clients_and_access_list(conn, sync, fake, add_device):
    fake.access["allowed_clients"] = ["198.51.100.7"]  # operator's own IP stays
    add_device("rahhul-phone")
    add_device("rahhul-tv")
    await sync.sync_clients()
    assert set(fake.clients) == {"rahhul-phone", "rahhul-tv"}
    assert fake.clients["rahhul-tv"]["ids"] == ["rahhul-tv"]
    assert set(fake.access["allowed_clients"]) == {"198.51.100.7", "rahhul-phone", "rahhul-tv"}
