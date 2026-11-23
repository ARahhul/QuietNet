"""Home internet providers and WiFi routers for Setup → Home WiFi.

* Hand-checked guides live in data/providers.yaml and are served as they are.
* The provider is detected from the visitor's public IP with Team Cymru's IP→network service,
  asked over DNS-over-HTTPS. The IP leaves the server only inside that DNS question
  (to Cloudflare's resolver and Team Cymru). Set PROVIDER_LOOKUP=false to turn it off.
* Any other provider or router gets an AI-written guide. Its fields are checked (admin pages must be
  a private IP or a known router name, odds and capabilities must be known values), then cached.
"""

import ipaddress
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

import httpx
import yaml

from .db import now_iso
from .knowledge import DATA
from .prompts import ISP_GUIDE, ROUTER_GUIDE

log = logging.getLogger("pahra.providers")

Kind = Literal["isp", "router"]
ODDS = ("usually", "often", "varies")
DOT = ("yes", "advanced", "no")
ISP_FIELDS = (
    "key", "name", "short", "admin_urls", "logins", "sticker_note", "dns_locked", "dns_note",
    "cgnat", "help_from", "ask_for", "own_ip",
)  # fmt: skip
ROUTER_FIELDS = ("key", "name", "brand", "dot", "admin_url", "dns_menu", "login")
DOH_URL = "https://1.1.1.1/dns-query"
CACHE_DAYS = 30


@lru_cache
def data() -> dict:
    return yaml.safe_load((DATA / "providers.yaml").read_text(encoding="utf-8")) or {}


def norm(name: str) -> str:
    """'TP-Link Archer C6' -> 'tp link archer c6'"""
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def public_isp(entry: dict, source: str = "curated") -> dict:
    g = {k: entry.get(k) for k in ISP_FIELDS}
    g["logins"] = g["logins"] or []
    g["ask_for"] = g["ask_for"] or []
    g["source"] = source
    return g


def public_router(entry: dict, source: str = "curated") -> dict:
    return {k: entry.get(k) for k in ROUTER_FIELDS} | {"source": source}


def catalog() -> dict:
    d = data()
    return {"isps": [public_isp(i) for i in d["isps"]], "routers": [public_router(r) for r in d["routers"]]}


def _named(name: str, entries: list[dict]) -> dict | None:
    n = norm(name)
    padded = f" {n} "
    for e in entries:
        for a in [e["key"], e["name"], *(e.get("aliases") or [])]:
            if (a := norm(a)) and f" {a} " in padded:
                return e
    return None


def find_isp(name: str) -> dict | None:
    return _named(name, data()["isps"])


def find_router(name: str) -> dict | None:
    return _named(name, data()["routers"])


def match_network(asn: int | None, as_name: str) -> dict | None:
    up = (as_name or "").upper()
    for i in data()["isps"]:
        if (asn and asn in (i.get("asns") or [])) or any(m in up for m in i.get("as_match") or []):
            return i
    return None


def pretty_network(as_name: str) -> str:
    """'CABLELITE-AS-AP - Atria Convergence Technologies Pvt. Ltd., IN' -> 'Atria Convergence Technologies Pvt. Ltd.'"""
    n = as_name.split(" - ", 1)[1] if " - " in as_name else as_name
    return re.sub(r",\s*[A-Z]{2}$", "", n.strip())[:80]


# ---------------------------------------------------------------- detection

_net_cache: dict[str, tuple[float, dict | None]] = {}


def _origin_name(ip: str) -> str | None:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if not a.is_global:
        return None
    if a.version == 4:
        return ".".join(reversed(ip.split("."))) + ".origin.asn.cymru.com"
    return ".".join(a.exploded.replace(":", "")[::-1]) + ".origin6.asn.cymru.com"


async def _txt(c: httpx.AsyncClient, name: str) -> str | None:
    r = await c.get(DOH_URL, params={"name": name, "type": "TXT"}, headers={"accept": "application/dns-json"})
    r.raise_for_status()
    for a in r.json().get("Answer") or []:
        if a.get("type") == 16:
            return a["data"].replace('" "', "").strip('"')
    return None


async def lookup_network(ip: str, *, transport: httpx.AsyncBaseTransport | None = None) -> dict | None:
    """{asn, as_name, country} for a public IP, or None. Remembered for a day."""
    name = _origin_name(ip)
    if not name:
        return None
    hit = _net_cache.get(ip)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    try:
        async with httpx.AsyncClient(timeout=5, transport=transport) as c:
            origin = await _txt(c, name)  # "24309 | 106.51.0.0/22 | IN | apnic | 2011-03-24"
            if not origin:
                return None
            fields = [p.strip() for p in origin.split("|")]
            asn = int(fields[0].split()[0])
            desc = [p.strip() for p in (await _txt(c, f"AS{asn}.asn.cymru.com") or "").split("|")]
    except (httpx.HTTPError, ValueError, KeyError, IndexError) as e:
        log.info("network lookup failed: %s", e)
        return None
    out = {
        "asn": asn,
        "country": fields[2] if len(fields) > 2 else "",
        "as_name": desc[4] if len(desc) > 4 else "",
    }
    if len(_net_cache) > 1000:
        _net_cache.clear()
    _net_cache[ip] = (time.monotonic() + 86400, out)
    return out


async def detect(ip: str | None) -> dict:
    """Which provider this connection belongs to (curated guide when we have one)."""
    net = await lookup_network(ip) if ip else None
    if not net:
        return {"found": False, "asn": None, "country": None, "network": None, "isp": None}
    isp = match_network(net["asn"], net["as_name"])
    return {
        "found": True,
        "asn": net["asn"],
        "country": net["country"],
        "network": isp["name"] if isp else pretty_network(net["as_name"]),
        "isp": public_isp(isp) if isp else None,
    }


# ---------------------------------------------------------------- AI guides for everything else

def _text(v, n: int = 240) -> str:
    return " ".join(str(v).split())[:n] if isinstance(v, (str, int, float)) else ""


def _admin_url(u) -> str | None:
    u = _text(u, 100)
    if not u:
        return None
    if not re.match(r"^https?://", u, re.I):
        u = "http://" + u
    try:
        parts = urlsplit(u)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return None
    try:
        ip = ipaddress.ip_address(host)
        ok = ip.version == 4 and ip.is_private and not ip.is_loopback
    except ValueError:
        ok = host in data().get("admin_hosts", [])
    if not ok:
        return None
    return f"{parts.scheme.lower()}://{host}" + (f":{port}" if port else "")


def _list(v) -> list:
    return v if isinstance(v, list) else []


def _from_ai(kind: str, name: str, a: dict) -> dict | None:
    """Checked guide from the model's answer, or None when the model doesn't know it."""
    if a.get("known") is not True:
        return None
    key = "ai-" + norm(name).replace(" ", "-")
    if kind == "isp":
        urls = [u for u in map(_admin_url, _list(a.get("admin_urls"))[:3]) if u]
        logins = [
            {"user": _text(x.get("user"), 40), "pass": _text(x.get("pass"), 40), "on": _text(x.get("on"), 60) or None}
            for x in _list(a.get("logins"))[:3]
            if isinstance(x, dict) and _text(x.get("user"), 40)
        ]
        g = {
            "key": key,
            "name": _text(a.get("name"), 60) or name,
            "short": _text(a.get("short"), 30) or name,
            "admin_urls": urls or data()["generic_isp"]["admin_urls"],
            "logins": logins,
            "sticker_note": _text(a.get("sticker_note")) or None,
            "dns_locked": a.get("dns_locked") if a.get("dns_locked") in ODDS else "varies",
            "dns_note": _text(a.get("dns_note")) or "Depends on the box.",
            "cgnat": a.get("cgnat") if a.get("cgnat") in ODDS else "varies",
            "help_from": _text(a.get("help_from"), 120) or "their customer care",
            "ask_for": [t for t in map(_text, _list(a.get("ask_for"))[:3]) if t]
            or [f"Ask {name} for the router's admin login, or ask them to set the DNS for you."],
            "own_ip": _text(a.get("own_ip")) or None,
        }
        return public_isp(g, "ai")
    g = {
        "key": key,
        "name": _text(a.get("name"), 60) or name,
        "brand": _text(a.get("brand"), 30) or name,
        "dot": a.get("dot") if a.get("dot") in DOT else "no",
        "admin_url": _admin_url(a.get("admin_url")) or "",
        "dns_menu": _text(a.get("dns_menu"), 120) or None,
        "login": _text(a.get("login")) or data()["generic_router"]["login"],
    }
    return public_router(g, "ai")


def generic_guide(kind: str, name: str) -> dict:
    """The safe fallback when nobody knows this provider / router."""
    key = "other-" + norm(name).replace(" ", "-")
    if kind == "isp":
        base = data()["generic_isp"]
        g = {
            **base,
            "key": key,
            "name": name,
            "short": name,
            "help_from": base["help_from"].format(name=name),
            "ask_for": [t.format(name=name) for t in base["ask_for"]],
        }
        return public_isp(g, "generic")
    return public_router({**data()["generic_router"], "key": key, "name": name, "brand": name}, "generic")


async def guide_for(conn, explainer, kind: str, name: str) -> dict:
    """Curated guide if we have one, else a cached or freshly written AI guide, else the generic one."""
    name = _text(name, 60)
    key = norm(name)
    if not key:
        raise ValueError("empty name")
    known = find_isp(name) if kind == "isp" else find_router(name)
    if known:
        return public_isp(known) if kind == "isp" else public_router(known)

    row = conn.execute("SELECT guide, created_at FROM provider_guides WHERE kind = ? AND key = ?", (kind, key)).fetchone()
    if row and datetime.fromisoformat(row["created_at"]) > datetime.now(timezone.utc) - timedelta(days=CACHE_DAYS):
        return json.loads(row["guide"])

    if not explainer.enabled:
        return generic_guide(kind, name)
    with explainer.tracing.trace("home-wifi-guide", input={"kind": kind, "name": name}, tags=("home-wifi",)) as t:
        try:
            answer = await explainer.ask_json(ISP_GUIDE if kind == "isp" else ROUTER_GUIDE, name=name)
        except Exception as e:  # quota, network, not JSON: don't cache, just fall back this time
            log.warning("AI %s guide for %r failed: %s", kind, name, e)
            t.update(level="ERROR", status_message=f"{e.__class__.__name__}: {e}"[:500], output={"source": "generic"})
            return generic_guide(kind, name)
        guide = _from_ai(kind, name, answer) or generic_guide(kind, name)
        t.update(output=guide)
    conn.execute(
        "INSERT OR REPLACE INTO provider_guides (kind, key, guide, created_at) VALUES (?, ?, ?, ?)",
        (kind, key, json.dumps(guide), now_iso()),
    )
    return guide
