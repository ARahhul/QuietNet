"""pahra-companion MCP server: tool and prompt registration, plus the CLI entry point.

Run over stdio (default; the Pahra backend spawns it) or streamable HTTP for MCP Inspector:

    pahra-companion
    pahra-companion --http 127.0.0.1:8765
"""

import argparse
import asyncio
import functools
import logging
import os
import sys
from ipaddress import IPv4Address
from typing import Annotated, Any

from mcp.types import ToolAnnotations
from pydantic import Field

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server

    MCP_V2 = True
except ImportError:  # pragma: no cover - mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[no-redef]

    MCP_V2 = False

from . import __version__, cgnat, discovery, fingerprint, netinfo
from . import forwarder as fwdmod
from .adb import Adb, AdbCommandRejected, AdbError, AdbMissing, INSTALL_HINT, validate_code, validate_hostname
from .setup_options import setup_options

log = logging.getLogger("pahra_companion")

SERVER_NAME = "pahra-companion"
INSTRUCTIONS = """\
Pahra home companion. Finds TVs and streamers on this computer's home LAN and points their DNS at Pahra.
Guardrails: only RFC 1918 subnets attached to a local interface (scans at most /22); no credential guessing;
never logs into routers or TVs; ADB is limited to fixed Private-DNS commands. Every tool returns a JSON object
with "ok"; on failure it carries "error" and usually "hint". Read-only tools have readOnlyHint=true; ask the
user before calling any tool without it. Use the connect_device prompt for the full playbook."""

READ_ONLY = ToolAnnotations.model_validate(
    {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
)
READ_ONLY_INTERNET = ToolAnnotations.model_validate(
    {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}
)
CONNECTS = ToolAnnotations.model_validate(
    {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
)
CHANGES_DEVICE = ToolAnnotations.model_validate(
    {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False}
)

STARTS_RELAY = ToolAnnotations.model_validate(
    {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}
)
STOPS_RELAY = ToolAnnotations.model_validate(
    {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
)

_adb = Adb()
_forwarder = fwdmod.DnsForwarder()

# Inline item schema (no $ref) so simple JSON-schema consumers such as Gemini can use it.
ForwarderMappings = Annotated[
    list[dict[str, str]],
    Field(
        description='TVs to relay: [{"ip": "<TV LAN IPv4>", "client_id": "<Pahra client id, [a-z0-9-]>"}]',
        json_schema_extra={
            "items": {
                "type": "object",
                "properties": {"ip": {"type": "string"}, "client_id": {"type": "string"}},
                "required": ["ip", "client_id"],
            }
        },
    ),
]


def _err(error: str, hint: str | None = None, code: str = "error", **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": False, "error": error, "code": code}
    if hint:
        out["hint"] = hint
    out.update(extra)
    return out


def _safe(fn):
    """Turn expected failures into {ok: false, error, hint} instead of MCP errors."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except netinfo.GuardError as e:
            return _err(str(e), e.hint, "guard")
        except AdbMissing as e:
            return _err(str(e), e.hint, "adb_missing")
        except AdbCommandRejected as e:
            return _err(str(e), e.hint, "rejected")
        except AdbError as e:
            return _err(str(e), e.hint, "adb_error")
        except ValueError as e:
            return _err(str(e), None, "invalid_argument")
        except Exception as e:  # noqa: BLE001
            log.exception("tool %s failed", fn.__name__)
            return _err(f"{type(e).__name__}: {e}", "Unexpected error in the companion; see its log.", "internal")

    return wrapper


def _port(port: int) -> int:
    port = int(port)
    if not 1 <= port <= 65535:
        raise ValueError(f"port must be 1-65535 (got {port})")
    return port


def _gateway_ip() -> str | None:
    gw = netinfo.default_gateway()
    return gw["ip"] if gw else None


def _bind_ip_for(ifs: list[netinfo.LocalInterface], target: IPv4Address | None, gateway: str | None) -> str | None:
    if target is not None:
        for li in ifs:
            if target in li.network:
                return str(li.ip)
    li = netinfo.primary_interface(ifs, gateway)
    return str(li.ip) if li else None


def _adb_info() -> dict[str, Any]:
    path = _adb.path
    return {"available": bool(path), "path": path, "hint": None if path else INSTALL_HINT}


def _summarize_device(ev: dict[str, Any], fp: dict[str, Any]) -> dict[str, Any]:
    ssdp_names = sorted(
        {
            (s.get("description") or {}).get("friendly_name") or ""
            for s in ev.get("ssdp") or []
            if (s.get("description") or {}).get("friendly_name")
        }
    )
    return {
        "ip": ev["ip"],
        "mac": ev.get("mac"),
        "vendor": fp["vendor"],
        "randomized_mac": fp["randomized_mac"],
        "alive": ev.get("alive"),
        "rtt_ms": ev.get("rtt_ms"),
        "is_gateway": bool(ev.get("is_gateway")),
        "open_ports": ev.get("open_ports") or [],
        "platform": fp["platform"],
        "confidence": fp["confidence"],
        "brand": fp["brand"],
        "model": fp["model"],
        "friendly_name": fp["friendly_name"],
        "ssdp_names": ssdp_names,
        "ssdp_servers": sorted({s["server"] for s in ev.get("ssdp") or [] if s.get("server")}),
        "mdns_services": sorted({m["type"] for m in ev.get("mdns") or [] if m.get("type")}),
        "capabilities": fp["capabilities"],
        "reasons": fp["reasons"],
    }


# --------------------------------------------------------------------------- read-only tools


@_safe
async def get_network_context() -> dict[str, Any]:
    """Describe this computer's home network: local IPv4 interfaces, the subnet to scan, the default gateway
    (router IP, MAC vendor, admin-page <title> without logging in, brand guess), public IPv4 (via 1.1.1.1),
    CGNAT verdict (router's UPnP WAN IP vs public IP: cgnat | not_cgnat | unknown), whether IPv6 is present,
    and whether Android platform-tools (adb) is installed."""
    ifs = netinfo.local_interfaces()
    gw = await asyncio.to_thread(netinfo.default_gateway)
    gw_ip = gw["ip"] if gw else None
    primary = netinfo.primary_interface(ifs, gw_ip)
    try:
        scan_net, clamped = netinfo.default_scan_subnet(ifs, gw_ip)
        scan = {"subnet": str(scan_net), "clamped": clamped}
    except netinfo.GuardError as e:
        scan = {"subnet": None, "clamped": False, "error": str(e), "hint": e.hint}
    bind_ip = str(primary.ip) if primary else None

    router: dict[str, Any] | None = None
    gw_ok = False
    if gw_ip:
        try:
            netinfo.check_lan_ip(gw_ip, ifs)
            gw_ok = True
        except netinfo.GuardError:
            gw_ok = False

    async def _title():
        return await discovery.http_title(gw_ip) if gw_ok else None

    title, nat, arp = await asyncio.gather(_title(), cgnat.check(bind_ip), discovery.read_arp_table())
    if gw_ip:
        mac = arp.get(gw_ip)
        igd = nat.get("igd") or {}
        ev = {
            "ip": gw_ip,
            "mac": mac,
            "is_gateway": True,
            "http_title": title,
            "ssdp": [{"description": {"manufacturer": igd.get("manufacturer"), "model_name": igd.get("model"),
                                      "friendly_name": igd.get("friendly_name")}}] if igd else [],
        }
        vendor = fingerprint.oui_lookup(mac)
        router = {
            "ip": gw_ip,
            "mac": mac,
            "vendor": vendor["vendor"] if vendor else None,
            "http_title": title,
            "upnp": igd or None,
            "brand_guess": fingerprint.detect_router_brand(ev),
            "on_local_subnet": gw_ok,
        }
    v6 = netinfo.ipv6_global_addresses()
    return {
        "ok": True,
        "platform": sys.platform,
        "interfaces": [li.as_dict() for li in ifs],
        "primary_interface": primary.as_dict() if primary else None,
        "scan": scan,
        "gateway": router,
        "public_ip": nat.get("public_ip"),
        "cgnat": {k: nat.get(k) for k in ("verdict", "reason", "wan_ip")},
        "ipv6": {"present": bool(v6), "global_address_count": len(v6)},
        "adb": _adb_info(),
    }


@_safe
async def scan_network(subnet: str | None = None) -> dict[str, Any]:
    """Find devices on the LAN: concurrent ping sweep, ARP table (MACs/vendors), SSDP/UPnP descriptions
    (friendlyName, manufacturer, modelName), mDNS (Cast, Android TV remote, ADB wireless debugging, Fire TV,
    AirPlay) and TCP probes (5555 ADB, 3000/3001 LG, 8001/8002 Samsung, 8008/8009 Cast, 8060 Roku, 7000 AirPlay,
    80). Each device gets a platform guess (android_tv | google_tv | fire_tv | webos | tizen | roku | apple_tv |
    router | unknown) with confidence and reasons. `subnet` defaults to the local LAN; it must be a local
    RFC 1918 subnet no larger than /22. Takes about 4-8 seconds."""
    started = asyncio.get_running_loop().time()
    ifs = netinfo.local_interfaces()
    gw_ip = await asyncio.to_thread(_gateway_ip)
    if subnet:
        net, clamped = netinfo.check_subnet(subnet, ifs), False
    else:
        net, clamped = netinfo.default_scan_subnet(ifs, gw_ip)
    bind_ip = _bind_ip_for(ifs, net.network_address + 1, gw_ip)
    own = {str(li.ip) for li in ifs}
    evidence = await discovery.gather_evidence(net, bind_ip=bind_ip, exclude=own)
    devices = []
    for ip, ev in evidence.items():
        ev["is_gateway"] = ip == gw_ip
        devices.append(_summarize_device(ev, fingerprint.classify(ev)))
    counts: dict[str, int] = {}
    for d in devices:
        counts[d["platform"]] = counts.get(d["platform"], 0) + 1
    return {
        "ok": True,
        "subnet": str(net),
        "clamped": clamped,
        "addresses_scanned": max(net.num_addresses - 2, 1),
        "duration_s": round(asyncio.get_running_loop().time() - started, 1),
        "device_count": len(devices),
        "platform_counts": counts,
        "devices": devices,
        "note": "Devices that block ping can still appear via ARP, SSDP or mDNS. TVs in deep standby may not answer at all.",
    }


@_safe
async def ping_device(ip: str) -> dict[str, Any]:
    """Check whether a LAN device answers ping, with round-trip time in ms. `ip` must be on the local LAN."""
    addr = netinfo.check_lan_ip(ip)
    r = await discovery.ping(str(addr))
    return {"ok": True, "ip": str(addr), "alive": r["alive"], "rtt_ms": r["rtt_ms"]}


@_safe
async def probe_device(ip: str) -> dict[str, Any]:
    """Deep fingerprint of one LAN device: ping, TCP ports (5555 ADB, 3000/3001 LG SSAP, 8001/8002 Samsung,
    8008/8009 Cast, 8060 Roku, 7000 AirPlay, 80), ARP MAC + vendor, and the SSDP/mDNS answers from that IP.
    Returns platform, confidence, reasons and capabilities: adb_reachable (5555 open),
    wireless_debugging_advertised (+ ports), automatable (Android family). Takes about 3-4 seconds."""
    addr = netinfo.check_lan_ip(ip)
    ifs = netinfo.local_interfaces()
    gw_ip = await asyncio.to_thread(_gateway_ip)
    ev = await discovery.probe_one(str(addr), bind_ip=_bind_ip_for(ifs, addr, gw_ip))
    ev["is_gateway"] = str(addr) == gw_ip
    if ev["is_gateway"] or 80 in ev["open_ports"]:
        ev["http_title"] = await discovery.http_title(str(addr))
    fp = fingerprint.classify(ev)
    return {
        "ok": True,
        **_summarize_device(ev, fp),
        "http_title": ev.get("http_title"),
        "ssdp": ev["ssdp"],
        "mdns": ev["mdns"],
        "next": _next_hint(fp),
    }


def _next_hint(fp: dict[str, Any]) -> str:
    caps = fp["capabilities"]
    if fp["platform"] in fingerprint.ANDROID_FAMILY:
        if caps["adb_reachable"]:
            return "ADB port is open: call adb_status, then adb_connect (asks the user first)."
        if caps["wireless_debugging_advertised"]:
            return "Wireless debugging is on: ask the user for the pairing code and port shown on the TV, then adb_pair."
        return "ADB is off: show the enable-ADB steps from get_setup_options and wait for the user."
    if fp["platform"] == "unknown":
        return "Platform unknown: ask the user for brand/model, or call get_setup_options(platform='unknown')."
    return "This platform cannot be automated: call get_setup_options for manual/router guidance."


@_safe
async def adb_status(ip: str, port: int = 5555) -> dict[str, Any]:
    """ADB state of a LAN device, without changing anything: not_listening (port closed: ADB off) |
    listening (port open, this computer not connected yet: call adb_connect) | unauthorized (user must accept
    "Allow debugging?" on the TV) | offline | connected (+ sdk, manufacturer, model, current Private DNS).
    Also reports adb_available=false when platform-tools is not installed."""
    addr = netinfo.check_lan_ip(ip)
    port = _port(port)
    serial = f"{addr}:{port}"
    listening = await discovery.tcp_open(str(addr), port, timeout=1.0)
    base = {"ok": True, "ip": str(addr), "port": port, "serial": serial, "adb_available": bool(_adb.path)}
    if not listening:
        return {**base, "status": "not_listening",
                "hint": "ADB is off or the TV is asleep. Show the enable-ADB steps from get_setup_options."}
    if not _adb.path:
        return {**base, "status": "listening", "hint": INSTALL_HINT}
    state = await _adb.state(serial)
    if state == "device":
        info = await _adb.device_info(serial)
        dns = await _adb.get_private_dns(serial)
        return {**base, "status": "connected", **info, "private_dns": dns,
                "private_dns_supported": (info["sdk"] or 0) >= 28}
    if state == "unauthorized":
        return {**base, "status": "unauthorized",
                "hint": 'Ask the user to accept "Allow USB debugging?" on the TV (tick "Always allow"), then call adb_connect again.'}
    if state == "offline":
        return {**base, "status": "offline", "hint": "Call adb_connect to reconnect."}
    return {**base, "status": "listening", "hint": "ADB is listening; call adb_connect (asks the user first)."}


@_safe
async def get_setup_options(
    platform: str,
    brand: str | None = None,
    model: str | None = None,
    cgnat: str | None = None,
    router_brand: str | None = None,
) -> dict[str, Any]:
    """Ranked ways to point a device's DNS at Pahra, with step-by-step instructions from the hand-written
    guide library. platform: android_tv | google_tv | fire_tv | webos | tizen | roku | apple_tv | router |
    unknown. brand: TV brand (e.g. sony, tcl, xiaomi, oneplus, lg, samsung) or, for platform=router, the router
    brand (jiofiber, airtel, tp-link, act). cgnat: cgnat | not_cgnat | unknown (from get_network_context).
    router_brand: router brand for the router-DNS option. Steps contain placeholders such as {hostname} and
    {dns_server_ip} for the caller to fill in. Also lists what is impossible on that platform."""
    return setup_options(platform, brand=brand, model=model, cgnat=cgnat, router_brand=router_brand)


# --------------------------------------------------------------------------- mutating tools


@_safe
async def adb_connect(ip: str, port: int = 5555) -> dict[str, Any]:
    """Connect this computer's adb to a LAN device (Android TV / Google TV / Fire TV). The first time, the TV
    shows "Allow USB debugging?" and the result is status=unauthorized until the user accepts; then call again.
    Use the Wireless debugging port (not the pairing port) on Android 11+ when 5555 is closed."""
    addr = netinfo.check_lan_ip(ip)
    port = _port(port)
    serial = f"{addr}:{port}"
    if not await discovery.tcp_open(str(addr), port, timeout=1.0):
        return _err(f"nothing is listening on {serial}", "ADB is off. Show the enable-ADB steps from get_setup_options.",
                    "not_listening", status="not_listening")
    r = await _adb.connect(serial)
    if r["connected"]:
        info = await _adb.device_info(serial)
        return {"ok": True, "status": "connected", "serial": serial, **info,
                "private_dns_supported": (info["sdk"] or 0) >= 28, "output": r["output"]}
    if r["state"] == "unauthorized":
        return _err("the TV has not authorised this computer yet",
                    'Ask the user to accept "Allow USB debugging?" on the TV (tick "Always allow"), then call adb_connect again.',
                    "unauthorized", status="unauthorized", serial=serial, output=r["output"])
    return _err(f"adb could not connect to {serial}", "Check that ADB/USB debugging is on and the TV is awake.",
                "connect_failed", status=r["state"] or "failed", serial=serial, output=r["output"])


@_safe
async def adb_pair(ip: str, port: int, code: str) -> dict[str, Any]:
    """Pair with an Android 11+ / Google TV device using Wireless debugging: `port` and the 6-digit `code`
    come from the TV's "Pair device with pairing code" screen. After pairing, call adb_connect with the port
    shown on the main Wireless debugging screen (not the pairing port)."""
    addr = netinfo.check_lan_ip(ip)
    port = _port(port)
    code = validate_code(code)
    r = await _adb.pair(f"{addr}:{port}", code)
    if r["paired"]:
        return {"ok": True, "paired": True, "output": r["output"],
                "next": "Call adb_connect with the IP:port shown on the TV's Wireless debugging screen."}
    return _err("pairing failed", "Check the code and pairing port on the TV; the code expires when the dialog closes.",
                "pair_failed", output=r["output"])


async def _require_connected(serial: str) -> dict[str, Any] | None:
    state = await _adb.state(serial)
    if state != "device":
        return _err(f"{serial} is not connected over adb (state: {state or 'not connected'})",
                    "Call adb_connect first (and have the user accept the prompt on the TV).", "not_connected",
                    status=state or "not_connected")
    return None


@_safe
async def set_private_dns(ip: str, hostname: str, port: int = 5555) -> dict[str, Any]:
    """Point an ADB-connected Android TV / Google TV / Fire TV at Pahra: sets
    `settings put global private_dns_specifier <hostname>` and `private_dns_mode hostname`, then reads both
    back. Needs Android 9+ (SDK 28). `hostname` is the device's Pahra DoT hostname from the backend.
    Returns before/after values; undo with clear_private_dns."""
    addr = netinfo.check_lan_ip(ip)
    port = _port(port)
    hostname = validate_hostname(hostname)
    serial = f"{addr}:{port}"
    if (e := await _require_connected(serial)) is not None:
        return e
    r = await _adb.set_private_dns(serial, hostname)
    if not r["verified"]:
        return _err("Private DNS read-back did not match", "The device may not support Private DNS over ADB; use get_setup_options.",
                    "verify_failed", **r, hostname=hostname)
    return {"ok": True, "serial": serial, "hostname": hostname, **r,
            "next": "Verify the device's ClientID appears in the Pahra/AdGuard query log; if not within ~60 s, fall back to get_setup_options."}


@_safe
async def clear_private_dns(ip: str, port: int = 5555) -> dict[str, Any]:
    """Undo set_private_dns: sets private_dns_mode back to opportunistic (Android's default "Automatic") and
    reads it back. Returns before/after values."""
    addr = netinfo.check_lan_ip(ip)
    port = _port(port)
    serial = f"{addr}:{port}"
    if (e := await _require_connected(serial)) is not None:
        return e
    r = await _adb.clear_private_dns(serial)
    if not r["verified"]:
        return _err("Private DNS read-back did not match", None, "verify_failed", **r)
    return {"ok": True, "serial": serial, **r}


# --------------------------------------------------------------------------- DNS relay (laptop forwarder)


def _forwarder_listen_ip() -> str:
    """The primary private LAN IP: the one get_network_context reports. Never 0.0.0.0."""
    li = netinfo.primary_interface(netinfo.local_interfaces(), _gateway_ip())
    if li is None:
        raise netinfo.GuardError("this computer has no private IPv4 address to listen on",
                                 "Connect the laptop to the same Wi-Fi or LAN as the TV.")
    return str(li.ip)


@_safe
async def start_dns_forwarder(mappings: ForwarderMappings, dns_hostname: str, port: int = 53) -> dict[str, Any]:
    """Start the laptop DNS relay for TVs that only take a plain IPv4 DNS server (LG webOS, Samsung Tizen,
    Apple TV), so each keeps its own Pahra identity even behind CGNAT. Listens on this laptop's LAN IP
    (UDP+TCP `port`), answers only the mapped TV IPs (others get REFUSED), and forwards each query over DoH
    to https://<dns_hostname>/dns-query/<client_id> without caching. Calling it again replaces the whole
    mapping set. The user then sets the TV's DNS server to the returned listen_ip. The laptop must stay on."""
    port = _port(port)
    try:
        hostname = validate_hostname(dns_hostname)
    except AdbCommandRejected as e:
        raise ValueError(f"dns_hostname must be a plain DNS name like dns.example.com (got {dns_hostname!r})") from e
    table = fwdmod.validate_mappings(mappings)
    ifs = netinfo.local_interfaces()
    checked = {str(netinfo.check_lan_ip(ip, ifs)): cid for ip, cid in table.items()}
    listen_ip = await asyncio.to_thread(_forwarder_listen_ip)
    if listen_ip in checked:
        raise ValueError(f"{listen_ip} is this laptop; it cannot relay for itself")
    try:
        r = await _forwarder.start(listen_ip, port, checked, hostname)
    except fwdmod.BindError as e:
        return _err(str(e), e.hint, "bind_failed", running=False, listen_ip=listen_ip, port=port)
    st = _forwarder.status()
    return {
        "ok": True,
        "running": True,
        "listen_ip": st["listen_ip"],
        "port": st["port"],
        "mappings": st["mappings"],
        "upstream_template": f"https://{hostname}/dns-query/<client_id>",
        "rebound": r["rebound"],
        "hint": fwdmod.firewall_hint(st["port"]),
    }


@_safe
async def stop_dns_forwarder() -> dict[str, Any]:
    """Stop the laptop DNS relay. TVs pointed at it lose DNS until it is started again or their DNS is changed."""
    was_running = _forwarder.running
    await _forwarder.stop()
    return {"ok": True, "running": False, "was_running": was_running, "stats": _forwarder.stats.as_dict()}


@_safe
async def dns_forwarder_status() -> dict[str, Any]:
    """State of the laptop DNS relay: running, listen_ip, port, mappings, counters
    (queries, forwarded, refused, errors) and per-client query counts with the last query time (UTC)."""
    return {"ok": True, **_forwarder.status(), "started_at": _forwarder.started_at if _forwarder.running else None}


# --------------------------------------------------------------------------- prompt

CONNECT_DEVICE_PLAYBOOK = """\
You are helping the user connect a TV or streaming device to Pahra (a per-device DNS filter).
The user described the device as: "{description}"

Follow this playbook with the pahra-companion tools. Ask the user before every tool that is not read-only
(adb_connect, adb_pair, set_private_dns, clear_private_dns, start_dns_forwarder, stop_dns_forwarder).

1. Context: call get_network_context. Note the scan subnet, the gateway, the CGNAT verdict
   (cgnat | not_cgnat | unknown) and whether adb is installed.
2. Find it: call scan_network. Match the description against brand, friendly_name, model and platform.
   If several devices match, show the candidates and ask the user to pick. If none match, ask whether the TV is
   on and on the same Wi-Fi, then rescan once.
3. Check it: call ping_device and probe_device on the chosen IP. Use the platform and capabilities from
   probe_device.
4. Android family (android_tv, google_tv, fire_tv):
   a. If capabilities.adb_reachable is false and wireless debugging is not advertised, call
      get_setup_options(platform, brand, model, cgnat) and show the enable-ADB steps of the top option.
      Wait for the user to say they are done, then probe_device again.
   b. If only Wireless debugging is advertised (Android 11+), ask for the pairing port and 6-digit code shown
      on the TV, then adb_pair, then adb_connect to the connect port.
   c. Otherwise adb_connect. If the status is unauthorized, ask the user to accept "Allow USB debugging?" on the
      TV (tick Always allow), then call adb_connect again.
   d. Check the SDK level (adb_status / adb_connect). If it is below 28, Private DNS is impossible: go to step 5.
   e. Call set_private_dns(ip, hostname) with the device's Pahra hostname that the caller provides. Never
      invent a hostname. Report before/after.
   f. The caller verifies that the device's ClientID appears in the Pahra query log. If it does not, go to
      step 5. Offer clear_private_dns to undo.
5. Locked platforms (webos, tizen, roku, apple_tv), ADB refused, or verification failed:
   call get_setup_options(platform, brand, model, cgnat, router_brand) and present the options in rank order.
   - Router DNS is recommended only when the CGNAT verdict is not_cgnat.
   - Otherwise prefer manual DNS on the TV pointed at the laptop relay (options with starts_forwarder=true):
     call start_dns_forwarder with the TV's LAN IP and its Pahra client_id (both from the caller; include
     every TV already relayed, because the call replaces the whole mapping set), then tell the user to type the
     returned listen_ip as the TV's DNS server and to keep this laptop on. Pass on the firewall hint. Then
     check dns_forwarder_status or the Pahra query log for the TV's queries. A phone hotspot is a demo-only
     fallback.
   - Fill placeholders ({{dns_server_ip}}, {{hostname}}, {{gateway_ip}}, {{forwarder_ip}} = the relay's
     listen_ip) with values the caller provides.
   - Say plainly what is impossible (the "impossible" list), e.g. Roku has no DNS setting at all.
   - Steps marked verified=false are unconfirmed on real hardware: say that menus may differ by OS version
     and offer the other_variants.
Guardrails: never guess or ask for router/TV passwords, never log into the router or TV for the user, and only
touch devices on the local LAN. If adb is missing, tell the user to install it: winget install Google.PlatformTools
"""


def connect_device(description: str) -> str:
    """Playbook for finding a TV the user describes and connecting it to Pahra (ADB Private DNS where possible,
    otherwise ranked manual/router guidance that accounts for CGNAT)."""
    return CONNECT_DEVICE_PLAYBOOK.format(description=description.strip() or "a TV")


# --------------------------------------------------------------------------- registration

TOOLS = (
    (get_network_context, READ_ONLY_INTERNET),
    (scan_network, READ_ONLY),
    (ping_device, READ_ONLY),
    (probe_device, READ_ONLY),
    (adb_status, READ_ONLY),
    (get_setup_options, READ_ONLY),
    (adb_connect, CONNECTS),
    (adb_pair, CONNECTS),
    (set_private_dns, CHANGES_DEVICE),
    (clear_private_dns, CHANGES_DEVICE),
    (start_dns_forwarder, STARTS_RELAY),
    (stop_dns_forwarder, STOPS_RELAY),
    (dns_forwarder_status, READ_ONLY),
)
READ_ONLY_TOOLS = frozenset(fn.__name__ for fn, ann in TOOLS if ann.model_dump(by_alias=True).get("readOnlyHint"))


def _doc(fn) -> str:
    return " ".join((fn.__doc__ or "").split())


def create_server():
    kwargs: dict[str, Any] = {"instructions": INSTRUCTIONS}
    if MCP_V2:
        kwargs["version"] = __version__
    server = _Server(SERVER_NAME, **kwargs)
    for fn, ann in TOOLS:
        server.tool(name=fn.__name__, description=_doc(fn), annotations=ann)(fn)
    server.prompt(name="connect_device", description=_doc(connect_device))(connect_device)
    return server


def _parse_hostport(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    if not host or not port.isdigit():
        raise SystemExit(f"--http expects HOST:PORT, got {value!r}")
    if host not in ("127.0.0.1", "localhost", "::1", "[::1]"):
        raise SystemExit(
            "refusing to listen on a non-loopback address: the mutating ADB tools must not be reachable from the LAN"
        )
    return host.strip("[]"), int(port)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="pahra-companion", description=__doc__.splitlines()[0])
    parser.add_argument("--http", metavar="HOST:PORT", nargs="?", const="127.0.0.1:8765",
                        help="serve streamable HTTP (for MCP Inspector) instead of stdio; default 127.0.0.1:8765")
    parser.add_argument("--log-level", default=os.environ.get("PAHRA_COMPANION_LOG_LEVEL", "INFO"))
    parser.add_argument("--version", action="version", version=f"pahra-companion {__version__}")
    args = parser.parse_args(argv)
    # stdout carries the MCP stdio stream: log to stderr only.
    logging.basicConfig(stream=sys.stderr, level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    server = create_server()
    if args.http:
        host, port = _parse_hostport(args.http)
        log.info("pahra-companion %s on http://%s:%d/mcp", __version__, host, port)
        if MCP_V2:
            server.run("streamable-http", host=host, port=port)
        else:  # pragma: no cover
            server.settings.host, server.settings.port = host, port
            server.run(transport="streamable-http")
    else:
        server.run()


if __name__ == "__main__":
    main()
