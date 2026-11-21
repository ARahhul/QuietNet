"""Rank the ways to point a device's DNS at Pahra, using the hand-written guides/*.yaml.

Ranking rules (the "playbook"):
  * Android family (android_tv, google_tv, fire_tv): ADB Private DNS first. It is automatic,
    gives the TV its own identity, and works behind CGNAT.
  * Then, and for locked platforms (webos, tizen, roku, apple_tv):
      - not behind CGNAT: router DNS, then manual DNS on the TV pointed at the Pahra VM, then
        manual DNS pointed at the laptop DNS relay (available, not recommended).
      - behind CGNAT or unknown: manual DNS on the TV pointed at the laptop DNS relay (recommended),
        then a phone hotspot (demo only), then router DNS marked not recommended.
  * Devices with no DNS setting (Roku) cannot use the relay: router DNS pointed at it would
    refuse every unmapped device. That variant is listed as unavailable.
  * Anything a platform cannot do is listed under "impossible".
"""

from __future__ import annotations

import copy
from functools import lru_cache
from importlib import resources
from typing import Any

import yaml

from .fingerprint import ANDROID_FAMILY, PLATFORMS

GUIDE_FILES = ("android_tv", "google_tv", "fire_tv", "webos", "tizen", "roku", "apple_tv", "router", "common")

PLACEHOLDERS = {
    "hostname": "Pahra DoT hostname for this device, e.g. <device-id>.dns.<domain> (from the Pahra backend)",
    "dns_server_ip": "IPv4 address of the Pahra DNS server (the VM)",
    "gateway_ip": "the router's LAN IP (get_network_context.gateway.ip)",
    "forwarder_ip": "this laptop's LAN IP where the Pahra DNS relay listens (start_dns_forwarder's listen_ip)",
}

_CGNAT_ALIASES = {
    "cgnat": "cgnat",
    "true": "cgnat",
    "yes": "cgnat",
    "behind_cgnat": "cgnat",
    "not_cgnat": "not_cgnat",
    "false": "not_cgnat",
    "no": "not_cgnat",
    "public": "not_cgnat",
    "unknown": "unknown",
    "": "unknown",
}


@lru_cache(maxsize=None)
def load_guide(name: str) -> dict[str, Any]:
    if name not in GUIDE_FILES:
        raise KeyError(name)
    text = resources.files("pahra_companion").joinpath("guides", f"{name}.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(text)


def normalize_cgnat(value: Any) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return "cgnat" if value else "not_cgnat"
    key = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    if key not in _CGNAT_ALIASES:
        raise ValueError(f"cgnat must be one of cgnat, not_cgnat, unknown (got {value!r})")
    return _CGNAT_ALIASES[key]


def _norm_brand(brand: str | None) -> str | None:
    if not brand:
        return None
    b = brand.strip().lower().replace(" ", "")
    aliases = {
        "lg": "lg", "lgelectronics": "lg", "samsung": "samsung", "sony": "sony", "bravia": "sony",
        "tcl": "tcl", "xiaomi": "xiaomi", "mi": "xiaomi", "redmi": "xiaomi", "oneplus": "oneplus",
        "amazon": "amazon", "google": "google", "roku": "roku", "apple": "apple",
        "jio": "jiofiber", "jiofiber": "jiofiber", "reliancejio": "jiofiber",
        "airtel": "airtel", "airtelxstream": "airtel", "xstream": "airtel",
        "tplink": "tp-link", "tp-link": "tp-link", "act": "act", "actfibernet": "act",
    }
    return aliases.get(b, b)


def _option(guide: dict[str, Any], kind: str) -> dict[str, Any] | None:
    for opt in guide.get("options") or []:
        if opt.get("kind") == kind:
            return opt
    return None


def _pick_variant(option: dict[str, Any], brand: str | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    variants = option.get("variants") or []
    chosen = None
    if brand:
        chosen = next((v for v in variants if brand in (v.get("brands") or [])), None)
    if chosen is None:
        chosen = next((v for v in variants if not v.get("brands") and not v.get("min_sdk")), None)
    if chosen is None and variants:
        chosen = variants[0]
    others = [v for v in variants if v is not chosen]
    return chosen or {"label": "", "steps": [], "verified": False}, others


def _render(
    option: dict[str, Any],
    brand: str | None,
    *,
    id_suffix: str = "",
    recommended: bool,
    why: str,
    dns_target: str | None = None,
    prefix_steps: list[str] | None = None,
    available: bool | None = None,
    extra_notes: list[str] | None = None,
    rename: dict[str, str] | None = None,
    starts_forwarder: bool | None = None,
    needs_laptop_on: bool | None = None,
    identity: bool | None = None,
    no_steps: bool = False,
) -> dict[str, Any]:
    chosen, others = _pick_variant(option, brand)

    def flag(value: bool | None, key: str) -> bool:
        return bool(option.get(key, False)) if value is None else value

    def sub(steps: list[str]) -> list[str]:
        out = []
        for st in steps:
            for old, new in (rename or {}).items():
                st = st.replace("{" + old + "}", "{" + new + "}")
            out.append(st)
        return out

    steps = [] if no_steps else list(prefix_steps or []) + sub(list(chosen.get("steps") or []))
    needs = list(option.get("needs") or [])
    if rename:
        needs = [rename.get(n, n) for n in needs]
    return {
        "id": option["id"] + (f":{id_suffix}" if id_suffix else ""),
        "kind": option["kind"],
        "title": option["title"],
        "recommended": recommended,
        "available": option.get("available", True) if available is None else available,
        "automatable": bool(option.get("automatable", False)),
        "gives_per_device_identity": flag(identity, "gives_per_device_identity"),
        "starts_forwarder": flag(starts_forwarder, "starts_forwarder"),
        "needs_laptop_on": flag(needs_laptop_on, "needs_laptop_on"),
        "why": why,
        "dns_target": dns_target,
        "needs": needs,
        "variant": chosen.get("label"),
        "verified": bool(chosen.get("verified", False)),
        "steps": steps,
        "other_variants": [] if no_steps else [
            {"label": v.get("label"), "verified": bool(v.get("verified", False)), "steps": sub(list(v.get("steps") or []))}
            for v in others
        ],
        "notes": list(option.get("notes") or []) + list(extra_notes or []),
    }


def _manual_paths(manual: dict[str, Any] | None, brand: str | None, state: str, router_brand: str | None) -> list[dict]:
    """Ordered non-ADB paths for a TV: router DNS and manual DNS with a target, per CGNAT state."""
    router_opt = _option(load_guide("router"), "router_dns")
    common = load_guide("common")
    hotspot = _option(common, "hotspot")
    forwarder = _option(common, "forwarder")
    out: list[dict] = []

    def relay(recommended: bool, why: str) -> dict:
        """Manual DNS on the TV pointed at this laptop's DNS relay (start_dns_forwarder)."""
        return _render(manual, brand, id_suffix="laptop_forwarder", recommended=recommended,
                       dns_target="laptop_forwarder", available=True,
                       prefix_steps=forwarder["variants"][0]["steps"][:2], rename={"dns_server_ip": "forwarder_ip"},
                       starts_forwarder=True, needs_laptop_on=True, identity=True, why=why,
                       extra_notes=list(forwarder.get("notes") or []))

    if state == "not_cgnat":
        out.append(_render(router_opt, router_brand, recommended=True, dns_target="pahra_vm",
                           why="Not behind CGNAT: the router can send DNS to Pahra and Pahra can link your home's public IP."))
        if manual:
            out.append(_render(manual, brand, id_suffix="pahra_vm", recommended=True, dns_target="pahra_vm",
                               why="Only this TV uses Pahra. Pahra links your home's public IP to it."))
            out.append(relay(False, "Also works and gives the TV its own identity, but it needs this laptop on all the "
                                    "time. Not needed while your home has its own public IP."))
        return out
    reason = ("Behind CGNAT: your public IPv4 is shared with other customers, so Pahra cannot tell your home apart."
              if state == "cgnat" else
              "CGNAT status unknown: router DNS works only if your home has its own public IPv4.")
    if manual:
        out.append(relay(True, reason + " The TV sends DNS to this laptop, and the relay forwards it to Pahra under "
                                        "the TV's own identity. It needs this laptop on, with Pahra running."))
        out.append(_render(manual, brand, id_suffix="hotspot", recommended=False, dns_target="pahra_vm_via_hotspot",
                           prefix_steps=(hotspot["variants"][0]["steps"][:1]),
                           why="Demo only: on a phone hotspot Pahra can link the hotspot's public IP to this TV for a while.",
                           extra_notes=list(hotspot.get("notes") or [])))
    else:
        out.append(_render(forwarder, None, id_suffix="router", recommended=False, dns_target="laptop_forwarder",
                           available=False, starts_forwarder=False, needs_laptop_on=True, no_steps=True,
                           why="This device has no DNS setting, so the relay could only be reached by pointing the "
                               "router's DNS at this laptop. The relay answers only mapped TVs and REFUSES every "
                               "other device, so the whole home would lose DNS. Not offered."))
    out.append(_render(router_opt, router_brand, recommended=False, dns_target="pahra_vm", why=reason))
    return out


def setup_options(
    platform: str,
    brand: str | None = None,
    model: str | None = None,
    cgnat: Any = None,
    router_brand: str | None = None,
) -> dict[str, Any]:
    platform = (platform or "unknown").strip().lower()
    if platform not in PLATFORMS:
        raise ValueError(f"unknown platform {platform!r}; expected one of {', '.join(PLATFORMS)}")
    state = normalize_cgnat(cgnat)
    brand_n = _norm_brand(brand)
    router_n = _norm_brand(router_brand) or (brand_n if platform == "router" else None)
    options: list[dict] = []
    impossible: list[str] = []
    summary = None

    if platform == "router":
        guide = load_guide("router")
        summary = guide.get("summary")
        router_opt = _option(guide, "router_dns")
        ok = state == "not_cgnat"
        options.append(_render(router_opt, router_n, recommended=ok, dns_target="pahra_vm",
                               why="Not behind CGNAT." if ok else
                               ("Behind CGNAT: router DNS gives Pahra no way to identify your home." if state == "cgnat"
                                else "CGNAT status unknown: works only if your home has its own public IPv4.")))
        impossible = list(guide.get("limitations") or [])
    elif platform == "unknown":
        options = _manual_paths(None, None, state, router_n)
        impossible = ["The device type is unknown. Run probe_device on it, or ask the user for the brand and model."]
    else:
        guide = load_guide(platform)
        summary = guide.get("summary")
        impossible = list(guide.get("limitations") or [])
        if platform in ANDROID_FAMILY:
            adb_opt = _option(guide, "adb_private_dns")
            options.append(_render(adb_opt, brand_n, recommended=True, dns_target="hostname",
                                   why="Automatic: Pahra sets Private DNS over ADB. The TV gets its own identity, and this works behind CGNAT."))
        options.extend(_manual_paths(_option(guide, "manual_dns_tv"), brand_n, state, router_n))
        if state != "not_cgnat":
            impossible.append(
                "Router DNS pointed at the Pahra VM cannot identify your home while you are behind CGNAT "
                "(or until CGNAT is ruled out)."
            )

    # Available options first, keep relative order; then number them.
    options.sort(key=lambda o: (not o["available"], not o["recommended"]))
    for i, o in enumerate(options, 1):
        o["rank"] = i
    return {
        "ok": True,
        "platform": platform,
        "brand": brand_n,
        "model": model,
        "router_brand": router_n,
        "cgnat": state,
        "summary": summary,
        "options": copy.deepcopy(options),
        "impossible": impossible,
        "placeholders": PLACEHOLDERS,
        "note": "Guide steps are hand-written and marked verified=false until checked on real hardware. Menus vary by OS version; other_variants lists the alternatives.",
    }
