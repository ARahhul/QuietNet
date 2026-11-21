import re

import pytest

from pahra_companion import setup_options as so
from pahra_companion.fingerprint import ANDROID_FAMILY, PLATFORMS

KINDS = {"adb_private_dns", "manual_dns_tv", "router_dns", "forwarder", "hotspot"}
PLACEHOLDER = re.compile(r"\{(\w+)\}")


@pytest.mark.parametrize("name", so.GUIDE_FILES)
def test_guide_files_are_well_formed(name):
    g = so.load_guide(name)
    assert g["platform"] == name
    assert g["title"] and isinstance(g.get("limitations"), list)
    for opt in g["options"]:
        assert opt["kind"] in KINDS, opt["id"]
        assert opt["variants"], opt["id"]
        for v in opt["variants"]:
            assert v["label"] and v["steps"], (opt["id"], v)
            assert v["verified"] is False, f"{opt['id']} / {v['label']} claims verified without hardware testing"
            for step in v["steps"]:
                assert isinstance(step, str) and step.strip()
                for ph in PLACEHOLDER.findall(step):
                    assert ph in so.PLACEHOLDERS, f"unknown placeholder {{{ph}}} in {opt['id']}"


def ids(result):
    return [o["id"] for o in result["options"]]


def top(result):
    return result["options"][0]


@pytest.mark.parametrize("platform", ANDROID_FAMILY)
@pytest.mark.parametrize("cgnat", ["cgnat", "not_cgnat", "unknown", None])
def test_android_family_always_starts_with_adb(platform, cgnat):
    r = so.setup_options(platform, cgnat=cgnat)
    first = top(r)
    assert first["kind"] == "adb_private_dns"
    assert first["recommended"] and first["automatable"] and first["gives_per_device_identity"]
    assert first["dns_target"] == "hostname"
    assert [o["rank"] for o in r["options"]] == list(range(1, len(r["options"]) + 1))


@pytest.mark.parametrize("platform", ["webos", "tizen", "apple_tv"])
def test_locked_tv_not_behind_cgnat_prefers_router(platform):
    r = so.setup_options(platform, cgnat="not_cgnat")
    assert top(r)["kind"] == "router_dns" and top(r)["recommended"]
    assert r["options"][1]["kind"] == "manual_dns_tv" and r["options"][1]["dns_target"] == "pahra_vm"
    assert all(o["kind"] != "adb_private_dns" for o in r["options"])
    fwd = next(o for o in r["options"] if o["dns_target"] == "laptop_forwarder")
    assert fwd["available"] is True and fwd["recommended"] is False and fwd["starts_forwarder"] is True
    assert r["options"][-1] is fwd


@pytest.mark.parametrize("platform", ["webos", "tizen", "apple_tv"])
@pytest.mark.parametrize("cgnat", ["cgnat", "unknown"])
def test_locked_tv_behind_or_unknown_cgnat_prefers_laptop_relay(platform, cgnat):
    r = so.setup_options(platform, cgnat=cgnat)
    assert [o["dns_target"] for o in r["options"]] == ["laptop_forwarder", "pahra_vm_via_hotspot", "pahra_vm"]
    fwd, hotspot, router = r["options"]
    assert fwd["id"] == f"{platform}_manual_dns:laptop_forwarder"
    assert fwd["kind"] == "manual_dns_tv"
    assert fwd["recommended"] and fwd["available"]
    assert fwd["starts_forwarder"] is True and fwd["needs_laptop_on"] is True
    assert fwd["gives_per_device_identity"] is True
    assert fwd["needs"] == ["forwarder_ip"]
    assert any("{forwarder_ip}" in st for st in fwd["steps"])
    assert not any("{dns_server_ip}" in st for st in fwd["steps"])
    assert any("keep this laptop on" in st.lower() for st in fwd["steps"])
    assert hotspot["recommended"] is False and hotspot["available"] and "Demo only" in hotspot["why"]
    assert hotspot["starts_forwarder"] is False
    assert router["kind"] == "router_dns" and router["recommended"] is False and "CGNAT" in router["why"]
    assert any("CGNAT" in s for s in r["impossible"])


@pytest.mark.parametrize("platform", ANDROID_FAMILY)
def test_android_family_falls_back_to_relay_behind_cgnat(platform):
    r = so.setup_options(platform, cgnat="cgnat")
    assert [o["kind"] for o in r["options"]][:2] == ["adb_private_dns", "manual_dns_tv"]
    assert r["options"][1]["dns_target"] == "laptop_forwarder" and r["options"][1]["starts_forwarder"]


def test_roku_cannot_use_relay_through_router():
    r = so.setup_options("roku", cgnat="cgnat")
    relay = next(o for o in r["options"] if o["dns_target"] == "laptop_forwarder")
    assert relay["id"] == "laptop_forwarder:router"
    assert relay["available"] is False and relay["starts_forwarder"] is False and relay["steps"] == []
    assert "REFUSES" in relay["why"]


def test_no_option_claims_unbuilt_features():
    for p in PLATFORMS:
        for state in ("cgnat", "not_cgnat", "unknown"):
            r = so.setup_options(p, cgnat=state)
            blob = str(r).lower()
            assert "not built" not in blob and "stretch" not in blob, (p, state)
            for o in r["options"]:
                assert isinstance(o["starts_forwarder"], bool) and isinstance(o["needs_laptop_on"], bool)
                if o["starts_forwarder"]:
                    assert o["available"] and o["needs_laptop_on"]


def test_webos_says_what_is_impossible():
    r = so.setup_options("webos", brand="LG", cgnat="cgnat")
    text = " ".join(r["impossible"])
    assert "No ADB" in text and "Private DNS" in text
    assert top(r)["variant"].startswith("webOS 6.0")
    assert len(top(r)["other_variants"]) == 2


def test_roku_is_router_only():
    ok = so.setup_options("roku", cgnat="not_cgnat")
    assert ids(ok) == ["router_dns"] and top(ok)["recommended"]
    behind = so.setup_options("roku", cgnat="cgnat")
    assert all(not o["recommended"] for o in behind["options"])
    assert any("no DNS setting" in s for s in behind["impossible"])


def test_router_platform_uses_router_brand_variant():
    r = so.setup_options("router", brand="JioFiber", cgnat="cgnat")
    assert top(r)["variant"] == "JioFiber" and top(r)["recommended"] is False
    r = so.setup_options("router", brand="tp-link", cgnat="not_cgnat")
    assert top(r)["variant"].startswith("TP-Link") and top(r)["recommended"] is True


def test_router_brand_flows_into_tv_options():
    r = so.setup_options("tizen", cgnat="not_cgnat", router_brand="airtel")
    assert top(r)["variant"] == "Airtel Xstream Fiber"


def test_brand_specific_enable_adb_steps():
    r = so.setup_options("android_tv", brand="Sony", cgnat="cgnat")
    assert top(r)["variant"].startswith("Sony BRAVIA")
    r = so.setup_options("android_tv", brand="xiaomi")
    assert top(r)["variant"].startswith("Xiaomi")
    r = so.setup_options("android_tv")
    assert top(r)["variant"].startswith("Android TV 9-11")
    assert any("Wireless debugging" in v["label"] for v in top(r)["other_variants"])


def test_fire_tv_steps_mention_device_name_seven_times():
    steps = " ".join(top(so.setup_options("fire_tv"))["steps"])
    assert "My Fire TV" in steps and "7 times" in steps and "ADB debugging" in steps


def test_unknown_platform():
    r = so.setup_options("unknown", cgnat="not_cgnat")
    assert top(r)["kind"] == "router_dns"
    assert "probe_device" in r["impossible"][0]


@pytest.mark.parametrize("value,expected", [(True, "cgnat"), (False, "not_cgnat"), ("CGNAT", "cgnat"),
                                             ("not-cgnat", "not_cgnat"), (None, "unknown"), ("", "unknown")])
def test_normalize_cgnat(value, expected):
    assert so.normalize_cgnat(value) == expected


def test_bad_inputs():
    with pytest.raises(ValueError):
        so.setup_options("toaster")
    with pytest.raises(ValueError):
        so.setup_options("webos", cgnat="maybe")


def test_every_platform_returns_something():
    for p in PLATFORMS:
        r = so.setup_options(p, cgnat="not_cgnat")
        assert r["ok"] and r["options"], p
