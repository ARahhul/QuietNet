"""Deterministic 'connect my TV' playbook: same companion tools, no LLM.

Used when no Gemini key is configured, or as a fallback when the LLM fails mid-way, so the
demo never depends on an LLM being up.
"""

import re

from .. import forwarder
from .session import (ANDROID_FAMILY, PLATFORM_BRAND, PLATFORM_LABEL, TV_PLATFORMS, AssistantSession, brand_name,
                      is_forwarder_option)

ALIASES = {
    "lg": {"webos"}, "webos": {"webos"}, "samsung": {"tizen"}, "tizen": {"tizen"},
    "fire": {"fire_tv"}, "firestick": {"fire_tv"}, "amazon": {"fire_tv"},
    "google": {"google_tv", "android_tv"}, "android": {"android_tv", "google_tv"}, "chromecast": {"google_tv"},
    "sony": {"android_tv", "google_tv"}, "bravia": {"android_tv", "google_tv"}, "tcl": {"android_tv", "google_tv"},
    "xiaomi": {"android_tv"}, "mi": {"android_tv"}, "oneplus": {"android_tv"}, "roku": {"roku"},
    "apple": {"apple_tv"},
}


def score(desc: str, d: dict) -> float:
    words = set(re.findall(r"[a-z0-9]+", desc.lower()))
    hay = " ".join(str(d.get(k) or "") for k in ("friendly_name", "brand", "model", "vendor", "platform")).lower()
    s = 0.0
    for w in words:
        if len(w) >= 2 and w in hay:
            s += 2
        if d.get("platform") in ALIASES.get(w, set()):
            s += 3
    if d.get("platform") in TV_PLATFORMS:
        s += 1
    return s + float(d.get("confidence") or 0) if isinstance(d.get("confidence"), (int, float)) else s


def guess_platform(desc: str) -> str:
    for w in re.findall(r"[a-z0-9]+", desc.lower()):
        if w in ALIASES:
            return sorted(ALIASES[w])[0]
    return "unknown"


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _options_args(s: AssistantSession, platform: str, brand: str | None, model: str | None) -> dict:
    net = s.network or {}
    args = {"platform": platform, "brand": brand, "model": model,
            "cgnat": (net.get("cgnat") or {}).get("verdict"),
            "router_brand": (net.get("gateway") or {}).get("brand_guess")}
    return {k: v for k, v in args.items() if v is not None}


def _relay_first(opts: dict) -> bool:
    """Is the laptop relay the companion's top recommendation among the hand-done options?"""
    manual = [o for o in opts.get("options") or [] if o.get("available", True) and not o.get("automatable")]
    return bool(manual) and is_forwarder_option(manual[0]) and bool(manual[0].get("recommended"))


async def _relay(s: AssistantSession, brand: str | None) -> bool:
    """Offer the laptop DNS relay. Returns True if the session finished through it."""
    ip = s.target_ip
    await s.say("This TV can't take Pahra's private address directly, and your internet provider shares one public IP "
                "between many homes (CGNAT). Pahra can relay the TV's DNS through this laptop instead, so it keeps its own identity.")
    res = await s.setup_forwarder(ip, s.label(ip), brand)
    if not res.get("ok"):
        if not res.get("declined"):
            await s.say(f"The relay couldn't start ({res.get('error')}). {res.get('hint') or ''}".strip())
        return False
    steps = s.option_steps(is_forwarder_option) or [f"On the TV, set the DNS server to {s.forwarder_ip}."]
    await s.say("Now on the TV:\n" + "\n".join(f"{i}. {x}" for i, x in enumerate(steps, 1)))
    if s.forwarder_hint:
        await s.say(s.forwarder_hint)
    for _ in range(3):
        r = await s.ask("Reply 'done' once the TV's DNS setting is saved, or 'skip'.")
        if "skip" in r.lower():
            break
        if await s.verify_connected():
            await s.finish("connected", f"{_cap(s.label(ip))} now uses Pahra through this laptop. Keep the laptop on and Pahra "
                           "running while you watch: if it sleeps, the TV can't look up names until it wakes.")
            return True
        await s.say(f"No query from the TV yet. Check that its DNS shows {s.forwarder_ip} and that Windows Firewall allows "
                    "DNS on this laptop, then reply 'done' again.")
    await s.finish("guide", "The relay is running, but the TV hasn't used it yet. Here are the steps again.",
                   s.guide_from_options(prefer_manual=True, use_forwarder=True))
    return True


async def _guide(s: AssistantSession, platform: str, brand: str | None, model: str | None, why: str) -> None:
    opts = await s.call_tool("get_setup_options", _options_args(s, platform, brand, model))
    if s.target_ip and _relay_first(opts) and forwarder.available(s.companion):
        if await _relay(s, brand):
            return
    g = s.guide_from_options(prefer_manual=True, use_forwarder=bool(s.forwarder_ip))
    await s.finish("guide", why, g or {"title": "Connect through your router", "steps": [
        "Open your router's admin page and set its DNS server to the Pahra server IP.",
        "If your ISP uses CGNAT, use a phone hotspot for the demo instead.",
    ], "values": {}, "ai_suggested": False})


async def run_guided(s: AssistantSession) -> None:
    await s.emit({"type": "mode", "mode": "guided"})
    await s.call_tool("get_network_context", {})
    scan = await s.call_tool("scan_network", {})
    devices = [d for d in scan.get("devices") or [] if not d.get("is_gateway") and d.get("platform") != "router"]
    ranked = sorted(devices, key=lambda d: -score(s.description, d))
    tvs = [d for d in ranked if d.get("platform") in TV_PLATFORMS] or ranked[:5]

    if not tvs:
        reply = await s.ask("I couldn't find a TV on this WiFi. Is it switched on (not in deep standby) and on the same network? Reply 'retry' or 'skip'.")
        if "retry" in reply.lower():
            scan = await s.call_tool("scan_network", {})
            devices = [d for d in scan.get("devices") or [] if not d.get("is_gateway")]
            tvs = sorted(devices, key=lambda d: -score(s.description, d))[:5]
        if not tvs:
            return await _guide(s, guess_platform(s.description), None, None,
                                "I couldn't see the TV on the network, so here is how to set it up by hand.")

    choices = tvs[:5]
    await s.emit({
        "type": "devices",
        "devices": [
            {
                "handle": s.redactor.handle_for(d["ip"]),
                "label": s.label(d["ip"]),
                "platform": PLATFORM_LABEL.get(d.get("platform", "unknown"), d.get("platform")),
                "detail": f"{d['ip']} · {d.get('vendor') or brand_name(d.get('brand')) or 'unknown maker'}",
            }
            for d in choices
        ],
    })
    reply = await s.ask("Which one is it? Tap it, or reply with its number.")
    chosen = choices[0]
    for i, d in enumerate(choices, 1):
        h = s.redactor.handle_for(d["ip"])
        if h in reply or reply.strip() == str(i) or d["ip"] in reply:
            chosen = d
            break
    ip = chosen["ip"]
    s.target_ip = ip

    await s.call_tool("ping_device", {"ip": ip})
    probe = await s.call_tool("probe_device", {"ip": ip})
    platform = probe.get("platform") if probe.get("ok", True) and probe.get("platform") else chosen.get("platform", "unknown")
    brand = probe.get("brand") or chosen.get("brand") or PLATFORM_BRAND.get(platform)
    model = probe.get("model") or chosen.get("model")
    caps = probe.get("capabilities") or chosen.get("capabilities") or {}

    if platform in (None, "", "unknown"):
        # Fingerprinting failed (TV asleep, randomized MAC, no SSDP/mDNS): ask instead of guessing.
        guess = guess_platform(s.description)
        if guess == "unknown":
            reply = await s.ask("I can't tell what kind of TV this is. What brand is it? (e.g. LG, Samsung, Sony, Mi, OnePlus, Fire TV)")
            guess = guess_platform(reply)
            brand = brand or next((w for w in re.findall(r"[a-z]+", reply.lower()) if w in ALIASES), None)
        else:
            brand = brand or next((w for w in re.findall(r"[a-z]+", s.description.lower()) if w in ALIASES), None)
        platform = guess
        brand = brand or PLATFORM_BRAND.get(platform)
        if platform != "unknown":
            s.labels[ip] = f"your {PLATFORM_BRAND.get(platform) or brand_name(brand)} TV"
        if platform == "unknown":
            return await _guide(s, platform, brand, model, "I couldn't tell what kind of device this is, so here is the manual way.")

    if platform not in ANDROID_FAMILY:
        return await _guide(s, platform, brand, model,
                            f"{_cap(s.label(ip))} runs {PLATFORM_LABEL.get(platform, platform)}, which can't be set up automatically. Here's the best way to connect it.")

    # Wireless-debugging-only devices (Android 11+/Google TV) need pairing first.
    if not caps.get("adb_reachable") and caps.get("wireless_debugging_advertised"):
        r = await s.ask("On the TV open Developer options → Wireless debugging → Pair device with pairing code. "
                        "Reply with the pairing PORT and the 6-digit CODE, e.g. '37123 482913'.", kind="pairing_code")
        nums = re.findall(r"\d+", r)
        if len(nums) >= 2:
            port, code = int(nums[0]), nums[1]
            pr = await s.call_tool("adb_pair", {"ip": ip, "port": port, "code": code})
            if pr.get("ok"):
                r2 = await s.ask("Paired. Now reply with the port shown on the main Wireless debugging screen (IP address & port).")
                p2 = re.findall(r"\d+", r2)
                if p2:
                    s.adb_port = int(p2[-1])

    for _attempt in range(4):
        st = await s.call_tool("adb_status", {"ip": ip, "port": s.adb_port})
        status = st.get("status")
        if not st.get("adb_available", True):
            return await _guide(s, platform, brand, model, "ADB isn't installed on this computer (winget install Google.PlatformTools). Meanwhile, here's the manual way.")
        if status == "not_listening":
            opts = await s.call_tool("get_setup_options", _options_args(s, platform, brand, model))
            adb_opt = next((o for o in opts.get("options") or [] if o.get("automatable")), None)
            if adb_opt:
                await s.say("Developer mode (ADB) is off on the TV. Turn it on like this:\n" +
                            "\n".join(f"{i}. {s.fill(x)}" for i, x in enumerate(adb_opt.get("steps") or [], 1)))
            r = await s.ask("Reply 'done' when ADB debugging is on, or 'skip' to set it up by hand.")
            if "skip" in r.lower():
                return await _guide(s, platform, brand, model, "No problem. Here's how to set it up by hand.")
            continue
        if status in ("listening", "offline", "unauthorized") or status is None:
            res = await s.call_tool("adb_connect", {"ip": ip, "port": s.adb_port})
            if res.get("declined"):
                return await _guide(s, platform, brand, model, "Okay, I won't touch the TV. Here's how to set it up yourself.")
            if res.get("status") == "unauthorized" or res.get("code") == "unauthorized":
                r = await s.ask("Look at the TV: press Allow on \"Allow USB debugging?\" (tick Always allow). Then reply 'done'.")
                if "skip" in r.lower():
                    return await _guide(s, platform, brand, model, "Here's how to set it up by hand.")
            continue
        if status == "connected":
            if not st.get("private_dns_supported", (st.get("sdk") or 0) >= 28):
                return await _guide(s, platform, brand, model, f"This TV runs Android SDK {st.get('sdk')}; Private DNS needs Android 9+. Use the router or manual DNS instead.")
            res = await s.set_private_dns(ip, s.label(ip), brand)
            if res.get("declined"):
                return await _guide(s, platform, brand, model, "Okay, nothing was changed. Here's how to do it yourself.")
            if not res.get("ok"):
                return await _guide(s, platform, brand, model, f"Setting Private DNS didn't work ({res.get('error')}). Here's another way.")
            if await s.verify_connected():
                return await s.finish("connected", f"{s.label(ip)} now uses Pahra ({s.hostname()}). Its trackers will appear in Suggestions within a minute.")
            return await _guide(s, platform, brand, model,
                                "Private DNS is set, but the TV hasn't used it yet. Some Fire OS builds ignore it. Here's a reliable alternative.")
    await _guide(s, platform, brand, model, "I couldn't get ADB working. Here's the manual way.")
