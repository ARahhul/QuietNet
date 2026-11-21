import pytest

from conftest import load_fixture
from pahra_companion import fingerprint


@pytest.mark.parametrize(
    "fixture,platform,brand,automatable",
    [
        ("lg_webos", "webos", "lg", False),
        ("samsung_tizen", "tizen", "samsung", False),
        ("google_tv", "google_tv", "google", True),
        ("fire_tv", "fire_tv", "amazon", True),
        ("jiofiber_router", "router", "jiofiber", False),
        ("roku", "roku", "roku", False),
        ("apple_tv", "apple_tv", "apple", False),
    ],
)
def test_classifier_on_fixtures(fixture, platform, brand, automatable):
    fp = fingerprint.classify(load_fixture(fixture))
    assert fp["platform"] == platform, fp["reasons"]
    assert fp["brand"] == brand
    assert fp["confidence"] in ("high", "medium")
    assert fp["capabilities"]["automatable"] is automatable
    assert fp["reasons"], "every guess must say why"


def test_lg_details():
    fp = fingerprint.classify(load_fixture("lg_webos"))
    assert fp["confidence"] == "high"
    assert fp["model"] == "OLED55C1PTB"
    assert fp["vendor"] == "LG Electronics"
    assert fp["capabilities"]["adb_reachable"] is False


def test_lg_airplay_is_not_apple_tv():
    fp = fingerprint.classify(load_fixture("lg_webos"))
    assert not any(r.startswith("apple_tv") for r in fp["reasons"])


def test_google_tv_wireless_debugging_capabilities():
    fp = fingerprint.classify(load_fixture("google_tv"))
    caps = fp["capabilities"]
    assert caps["wireless_debugging_advertised"] is True
    assert caps["wireless_debugging_ports"] == {"connect": 39457, "pairing": 41231}
    assert caps["adb_reachable"] is False
    assert fp["randomized_mac"] is True
    assert fp["friendly_name"] == "Living Room TV"


def test_fire_tv_adb():
    fp = fingerprint.classify(load_fixture("fire_tv"))
    assert fp["capabilities"]["adb_reachable"] is True
    assert fp["model"] == "AFTKA"
    assert fp["confidence"] == "high"


def test_router_brand_from_default_jiofiber_gateway_alone():
    ev = {"ip": "192.168.29.1", "is_gateway": True, "open_ports": [80], "ssdp": [], "mdns": []}
    fp = fingerprint.classify(ev)
    assert fp["platform"] == "router"
    assert fp["brand"] == "jiofiber"


def test_android_tv_sony_from_cast_and_remote():
    ev = {
        "ip": "192.168.1.40",
        "mac": "fc:f1:52:00:00:01",
        "open_ports": [8008, 8009, 5555],
        "ssdp": [{"description": {"friendly_name": "BRAVIA 4K UR2", "manufacturer": "Sony", "model_name": "BRAVIA 4K UR2"}}],
        "mdns": [
            {"type": "_googlecast._tcp", "name": "BRAVIA-4K-UR2-abc", "txt": {"md": "BRAVIA 4K UR2", "fn": "Bedroom TV"}},
            {"type": "_androidtvremote2._tcp", "name": "Bedroom TV", "txt": {}},
        ],
    }
    fp = fingerprint.classify(ev)
    assert fp["platform"] == "android_tv"
    assert fp["brand"] == "sony"
    assert fp["capabilities"] == {
        "adb_reachable": True,
        "wireless_debugging_advertised": False,
        "wireless_debugging_ports": None,
        "automatable": True,
    }


def test_nothing_known_is_unknown():
    fp = fingerprint.classify({"ip": "192.168.1.50", "mac": "02:aa:bb:cc:dd:ee", "open_ports": [], "ssdp": [], "mdns": []})
    assert fp["platform"] == "unknown"
    assert fp["confidence"] == "none"
    assert fp["capabilities"]["automatable"] is False


def test_lg_ports_only_is_webos_with_lower_confidence():
    fp = fingerprint.classify({"ip": "192.168.1.51", "mac": "06:00:00:00:00:01", "open_ports": [3000, 3001]})
    assert fp["platform"] == "webos"
    assert fp["confidence"] == "low"


def test_oui_lookup_and_randomized_mac():
    assert fingerprint.oui_lookup("A8-23-FE-12-34-56")["brand"] == "lg"
    assert fingerprint.oui_lookup("da:a1:19:12:34:56") is None  # locally administered
    assert fingerprint.is_randomized_mac("da:a1:19:12:34:56")
    assert not fingerprint.is_randomized_mac("f0:27:2d:11:22:33")
    assert fingerprint.oui_lookup(None) is None


def test_oui_table_is_well_formed():
    table = fingerprint._oui_table()
    assert len(table) > 100
    for prefix, meta in table.items():
        assert len(prefix) == 8 and prefix.count(":") == 2, prefix
        assert not int(prefix[:2], 16) & 0x03, f"{prefix} is not a universal unicast OUI"
        assert meta["kind"] in ("tv", "streamer", "router", "mixed")
