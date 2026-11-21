"""Classify a LAN host as a TV/streamer platform from OUI, SSDP, mDNS and open ports.

Scoring is additive: each clue adds points to one platform and a reason string.
The highest score wins; ties or weak totals come back with low confidence, and
no clue at all gives "unknown". Every reason is returned so the agent (and the
user) can see why a guess was made.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from importlib import resources
from typing import Any

PLATFORMS = (
    "android_tv",
    "google_tv",
    "fire_tv",
    "webos",
    "tizen",
    "roku",
    "apple_tv",
    "router",
    "unknown",
)
ANDROID_FAMILY = ("android_tv", "google_tv", "fire_tv")
LOCKED = ("webos", "tizen", "roku", "apple_tv")

ROUTER_BRANDS = {
    "jiofiber": r"jio\s*fiber|jiofiber|reliance\s*jio|\bjio\b",
    "airtel": r"airtel|xstream",
    "act": r"\bact\b|act\s*fibernet|actcorp",
    "tp-link": r"tp-?link|archer|deco\b|tplinkwifi",
    "d-link": r"d-?link",
    "netgear": r"netgear|nighthawk|orbi",
    "huawei": r"huawei|echolife",
    "zte": r"\bzte\b",
    "nokia": r"nokia|alcatel",
    "sagemcom": r"sagemcom",
    "asus": r"\basus\b|rt-[a-z]{1,2}\d",
    "syrotech": r"syrotech",
}
JIOFIBER_DEFAULT_GATEWAY = "192.168.29.1"


@lru_cache(maxsize=1)
def _oui_table() -> dict[str, dict[str, str]]:
    raw = json.loads(resources.files("pahra_companion").joinpath("oui.json").read_text(encoding="utf-8"))
    table: dict[str, dict[str, str]] = {}
    for vendor, meta in raw["vendors"].items():
        for prefix in meta["prefixes"]:
            table[prefix.upper()] = {"vendor": vendor, "brand": meta["brand"], "kind": meta["kind"]}
    return table


def is_randomized_mac(mac: str | None) -> bool:
    """Locally administered bit set → randomised/private MAC; OUI says nothing."""
    if not mac:
        return False
    try:
        return bool(int(mac.replace("-", ":").split(":")[0], 16) & 0x02)
    except ValueError:
        return False


def oui_lookup(mac: str | None) -> dict[str, str] | None:
    if not mac or is_randomized_mac(mac):
        return None
    prefix = ":".join(p.zfill(2) for p in mac.replace("-", ":").split(":")[:3]).upper()
    return _oui_table().get(prefix)


def _texts(evidence: dict[str, Any]) -> str:
    """All human-readable strings from SSDP and mDNS, lower-cased, for brand/model regexes."""
    parts: list[str] = []
    for s in evidence.get("ssdp") or []:
        for k in ("server", "st", "usn"):
            if s.get(k):
                parts.append(str(s[k]))
        d = s.get("description") or {}
        for k in ("friendly_name", "manufacturer", "model_name", "model_number", "model_description", "device_type"):
            if d.get(k):
                parts.append(str(d[k]))
        parts.extend(d.get("device_types") or [])
    for m in evidence.get("mdns") or []:
        parts.append(str(m.get("name", "")))
        for k, v in (m.get("txt") or {}).items():
            if k in ("md", "fn", "model", "am", "manufacturer", "ty", "n"):
                parts.append(str(v))
    if evidence.get("http_title"):
        parts.append(str(evidence["http_title"]))
    return " | ".join(parts).lower()


def _mdns_types(evidence: dict[str, Any]) -> set[str]:
    return {str(m.get("type", "")).removesuffix(".local.").removesuffix(".") for m in evidence.get("mdns") or []}


def _mdns_txt(evidence: dict[str, Any], stype: str, key: str) -> str | None:
    for m in evidence.get("mdns") or []:
        if str(m.get("type", "")).startswith(stype):
            v = (m.get("txt") or {}).get(key)
            if v:
                return str(v)
    return None


def _first_desc(evidence: dict[str, Any], key: str) -> str | None:
    for s in evidence.get("ssdp") or []:
        v = (s.get("description") or {}).get(key)
        if v:
            return str(v)
    return None


def detect_router_brand(evidence: dict[str, Any]) -> str | None:
    text = _texts(evidence)
    for brand, pattern in ROUTER_BRANDS.items():
        if re.search(pattern, text):
            return brand
    vendor = oui_lookup(evidence.get("mac"))
    if vendor and vendor["kind"] == "router":
        return vendor["brand"]
    if evidence.get("ip") == JIOFIBER_DEFAULT_GATEWAY and evidence.get("is_gateway"):
        return "jiofiber"
    return None


def classify(evidence: dict[str, Any]) -> dict[str, Any]:
    """Return {platform, confidence, score, brand, model, friendly_name, vendor, reasons, capabilities}."""
    scores: dict[str, int] = {p: 0 for p in PLATFORMS if p != "unknown"}
    reasons: list[str] = []

    def add(platform: str, points: int, why: str) -> None:
        scores[platform] += points
        reasons.append(f"{platform} +{points}: {why}")

    ports = set(evidence.get("open_ports") or [])
    mtypes = _mdns_types(evidence)
    text = _texts(evidence)
    vendor = oui_lookup(evidence.get("mac"))
    randomized = is_randomized_mac(evidence.get("mac"))
    ssdp_sts = " ".join(str(s.get("st", "")) + " " + str(s.get("usn", "")) for s in evidence.get("ssdp") or []).lower()
    servers = " ".join(str(s.get("server", "")) for s in evidence.get("ssdp") or []).lower()
    manufacturer = (_first_desc(evidence, "manufacturer") or "").lower()

    # ---- router
    if evidence.get("is_gateway"):
        add("router", 5, "is the default gateway")
    if "internetgatewaydevice" in text or "wanipconnection" in text or "wanpppconnection" in text:
        add("router", 3, "advertises UPnP InternetGatewayDevice")
    if vendor and vendor["kind"] == "router":
        add("router", 1, f"MAC vendor {vendor['vendor']}")

    # ---- LG webOS
    if "webos" in servers or "webos" in text or "lge-com:service:webos-second-screen" in ssdp_sts:
        add("webos", 4, "SSDP mentions webOS")
    if 3000 in ports or 3001 in ports:
        add("webos", 2, "LG SSAP port 3000/3001 open")
    if "lg electronics" in manufacturer or re.search(r"\blg\b.*\b(tv|webos)\b", text):
        add("webos", 2, "UPnP manufacturer LG")
    if vendor and vendor["brand"] == "lg":
        add("webos", 1, f"MAC vendor {vendor['vendor']}")

    # ---- Samsung Tizen
    if "tizen" in text or "samsung.com:device:remotecontrolreceiver" in text:
        add("tizen", 4, "SSDP mentions Tizen / Samsung RemoteControlReceiver")
    if 8001 in ports or 8002 in ports:
        add("tizen", 2, "Samsung TV websocket port 8001/8002 open")
    if "samsung" in manufacturer or "samsung" in servers:
        add("tizen", 2, "UPnP manufacturer/server Samsung")
    if vendor and vendor["brand"] == "samsung":
        add("tizen", 1, f"MAC vendor {vendor['vendor']}")

    # ---- Roku
    if "roku:ecp" in ssdp_sts or "roku" in servers:
        add("roku", 4, "SSDP roku:ecp / Roku server")
    if 8060 in ports:
        add("roku", 2, "Roku ECP port 8060 open")
    if vendor and vendor["brand"] == "roku":
        add("roku", 2, "MAC vendor Roku")

    # ---- Apple TV (AirPlay alone is not enough: LG/Samsung/Sony TVs advertise it too)
    airplay_model = (_mdns_txt(evidence, "_airplay", "model") or _mdns_txt(evidence, "_airplay", "am") or "").lower()
    if airplay_model.startswith("appletv"):
        add("apple_tv", 5, f"AirPlay model {airplay_model}")
    if vendor and vendor["brand"] == "apple":
        add("apple_tv", 1, "MAC vendor Apple")
        if 7000 in ports or "_airplay._tcp" in mtypes:
            add("apple_tv", 1, "Apple vendor with AirPlay")

    # ---- Amazon Fire TV
    if "_amzn-wplay._tcp" in mtypes:
        add("fire_tv", 4, "mDNS _amzn-wplay (Fire TV)")
    model_name = (_first_desc(evidence, "model_name") or "").lower()
    if model_name.startswith("aft") or "fire tv" in text or "firetv" in text:
        add("fire_tv", 3, "model/friendly name says Fire TV")
    if "amazon" in manufacturer:
        add("fire_tv", 2, "UPnP manufacturer Amazon")
    if vendor and vendor["brand"] == "amazon":
        add("fire_tv", 2, "MAC vendor Amazon")

    # ---- Android TV / Google TV (split below)
    android = 0
    android_reasons: list[str] = []
    if "_androidtvremote2._tcp" in mtypes:
        android += 4
        android_reasons.append("+4: mDNS _androidtvremote2 (Android TV OS remote service)")
    if "_googlecast._tcp" in mtypes and "_amzn-wplay._tcp" not in mtypes:
        android += 1
        android_reasons.append("+1: Google Cast receiver")
    if re.search(r"bravia|mi ?tv|mi box|xiaomi|oneplus|tcl|shield|android ?tv|google ?tv|hisense|\bvu\b", text):
        android += 2
        android_reasons.append("+2: Android-TV-maker model/name")
    if vendor and vendor["brand"] in ("sony", "xiaomi", "oneplus", "google"):
        android += 1
        android_reasons.append(f"+1: MAC vendor {vendor['vendor']}")
    if android:
        google_markers = (
            "google tv" in text
            or "chromecast" in (_mdns_txt(evidence, "_googlecast", "md") or "").lower()
            and "_androidtvremote2._tcp" in mtypes
        )
        target = "google_tv" if google_markers else "android_tv"
        scores[target] += android
        reasons.extend(f"{target} {r}" for r in android_reasons)

    # ---- ADB / Wireless debugging favour the Android family
    adb_tls = bool(mtypes & {"_adb-tls-connect._tcp", "_adb-tls-pairing._tcp"})
    if 5555 in ports or adb_tls:
        fam = max(ANDROID_FAMILY, key=lambda p: scores[p])
        if scores[fam] == 0:
            fam = "android_tv"
        add(fam, 2, "ADB port 5555 open" if 5555 in ports else "Wireless debugging advertised over mDNS")

    best = max(scores, key=lambda p: scores[p])
    top = scores[best]
    runner_up = max((s for p, s in scores.items() if p != best), default=0)
    if top == 0:
        platform, confidence = "unknown", "none"
    else:
        platform = best
        if top >= 5 and top - runner_up >= 3:
            confidence = "high"
        elif top >= 3 and top > runner_up:
            confidence = "medium"
        else:
            confidence = "low"

    brand = _brand(platform, evidence, vendor, text)
    model = _first_desc(evidence, "model_name") or _mdns_txt(evidence, "_googlecast", "md") or (
        airplay_model or None
    )
    friendly = _first_desc(evidence, "friendly_name") or _mdns_txt(evidence, "_googlecast", "fn")
    if not friendly:
        for m in evidence.get("mdns") or []:
            if m.get("name"):
                friendly = str(m["name"])
                break

    adb_ports = {
        "connect": next((m.get("port") for m in evidence.get("mdns") or [] if m.get("type", "").startswith("_adb-tls-connect")), None),
        "pairing": next((m.get("port") for m in evidence.get("mdns") or [] if m.get("type", "").startswith("_adb-tls-pairing")), None),
    }
    capabilities = {
        "adb_reachable": 5555 in ports,
        "wireless_debugging_advertised": adb_tls,
        "wireless_debugging_ports": adb_ports if adb_tls else None,
        "automatable": platform in ANDROID_FAMILY,
    }
    return {
        "platform": platform,
        "confidence": confidence,
        "score": top,
        "brand": brand,
        "model": model,
        "friendly_name": friendly,
        "vendor": vendor["vendor"] if vendor else None,
        "randomized_mac": randomized,
        "reasons": reasons,
        "capabilities": capabilities,
    }


_TV_BRANDS = {
    "sony": r"sony|bravia",
    "tcl": r"\btcl\b",
    "xiaomi": r"xiaomi|mi ?tv|mi box|redmi",
    "oneplus": r"oneplus",
    "hisense": r"hisense",
    "vu": r"\bvu\b",
    "nvidia": r"shield",
    "philips": r"philips",
    "google": r"chromecast|google tv",
}


def _brand(platform: str, evidence: dict[str, Any], vendor: dict | None, text: str) -> str | None:
    if platform == "router":
        return detect_router_brand(evidence)
    fixed = {"webos": "lg", "tizen": "samsung", "fire_tv": "amazon", "roku": "roku", "apple_tv": "apple"}
    if platform in fixed:
        return fixed[platform]
    for brand, pattern in _TV_BRANDS.items():
        if re.search(pattern, text):
            return brand
    return vendor["brand"] if vendor else None
