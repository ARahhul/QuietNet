"""LAN discovery: ping sweep, ARP table, SSDP/UPnP, mDNS and TCP port probes.

Everything here is read-only and local. Callers must pass the subnet guard first
(netinfo.check_subnet / check_lan_ip); SSDP LOCATION URLs are re-checked against
the guard before they are fetched, so a hostile SSDP reply cannot make the
companion fetch an arbitrary URL.
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import sys
import time
import xml.etree.ElementTree as ET
from ipaddress import IPv4Address, IPv4Network
from typing import Any, Iterable
from urllib.parse import urljoin, urlsplit

import httpx

from . import netinfo

log = logging.getLogger(__name__)

PING_CONCURRENCY = 64
PROBE_CONCURRENCY = 128
PROBE_TIMEOUT = 0.5

# Port → what it usually means on a TV/streamer.
PROBE_PORTS: dict[int, str] = {
    5555: "adb",
    3000: "lg_ssap",
    3001: "lg_ssap_tls",
    8001: "samsung_ws",
    8002: "samsung_wss",
    8008: "cast_http",
    8009: "cast_tls",
    8060: "roku_ecp",
    7000: "airplay",
    80: "http",
}

MDNS_TYPES = (
    "_googlecast._tcp.local.",
    "_androidtvremote2._tcp.local.",
    "_adb-tls-connect._tcp.local.",
    "_adb-tls-pairing._tcp.local.",
    "_amzn-wplay._tcp.local.",
    "_airplay._tcp.local.",
)

SSDP_ADDR = ("239.255.255.250", 1900)
MAX_DESCRIPTION_BYTES = 256 * 1024

_NO_WINDOW = {"creationflags": 0x08000000} if sys.platform == "win32" else {}  # CREATE_NO_WINDOW


# --------------------------------------------------------------------------- ping


def ping_command(ip: str, platform: str | None = None) -> list[str]:
    platform = platform or sys.platform
    if platform == "win32":
        return ["ping", "-n", "1", "-w", "400", ip]
    if platform == "darwin":
        return ["ping", "-c", "1", "-W", "1000", ip]  # macOS -W is milliseconds
    return ["ping", "-c", "1", "-W", "1", ip]


_RTT = re.compile(r"[=<]\s*([\d.]+)\s*ms", re.IGNORECASE)


def judge_ping(returncode: int, output: str, platform: str | None = None) -> tuple[bool, float | None]:
    """(alive, rtt_ms). Decided by the exit code, never by localized words.

    Windows `ping` exits 0 even for "Destination host unreachable" replies sent by
    the local stack, so on Windows a reply must also carry the "TTL=" token, which
    is the same in every display language.
    """
    platform = platform or sys.platform
    alive = returncode == 0
    if alive and platform == "win32":
        alive = "ttl=" in output.lower()
    rtt = None
    if alive:
        m = _RTT.search(output)
        if m:
            try:
                rtt = float(m.group(1))
            except ValueError:
                rtt = None
    return alive, rtt


async def ping(ip: str) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        proc = await asyncio.create_subprocess_exec(
            *ping_command(ip),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
            **_NO_WINDOW,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    except (OSError, asyncio.TimeoutError) as exc:
        return {"ip": ip, "alive": False, "rtt_ms": None, "error": str(exc) or type(exc).__name__}
    alive, rtt = judge_ping(proc.returncode or 0, out.decode(errors="replace"))
    if alive and rtt is None:
        rtt = round((time.perf_counter() - started) * 1000, 1)
    return {"ip": ip, "alive": alive, "rtt_ms": rtt}


async def ping_sweep(network: IPv4Network, concurrency: int = PING_CONCURRENCY) -> dict[str, float | None]:
    """{ip: rtt_ms} for hosts that answered."""
    sem = asyncio.Semaphore(concurrency)

    async def one(ip: str):
        async with sem:
            return await ping(ip)

    results = await asyncio.gather(*(one(str(h)) for h in network.hosts()))
    return {r["ip"]: r["rtt_ms"] for r in results if r["alive"]}


# --------------------------------------------------------------------------- ARP

_ARP_ROW = re.compile(
    r"(?P<ip>(?:\d{1,3}\.){3}\d{1,3})\)?\s+(?:at\s+)?(?P<mac>[0-9a-fA-F]{1,2}(?:[:-][0-9a-fA-F]{1,2}){5})\b"
)


def normalize_mac(mac: str) -> str:
    parts = re.split(r"[:-]", mac.strip())
    return ":".join(p.zfill(2) for p in parts).lower()


def parse_arp_table(text: str) -> dict[str, str]:
    """{ip: mac} from `arp -a` (Windows, Linux, macOS) or `ip neigh` output.

    Skips broadcast and multicast MACs and incomplete entries.
    """
    table: dict[str, str] = {}
    for line in text.splitlines():
        m = _ARP_ROW.search(line)
        if not m:
            # `ip neigh`: "192.168.1.1 dev eth0 lladdr aa:bb:... REACHABLE"
            m = re.search(
                r"(?P<ip>(?:\d{1,3}\.){3}\d{1,3}).*?lladdr\s+(?P<mac>[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})", line
            )
            if not m:
                continue
        mac = normalize_mac(m.group("mac"))
        first = int(mac[:2], 16)
        if mac == "ff:ff:ff:ff:ff:ff" or mac == "00:00:00:00:00:00" or first & 0x01:
            continue
        table[m.group("ip")] = mac
    return table


def parse_proc_net_arp(text: str) -> dict[str, str]:
    table = {}
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 4 and parts[2] != "0x0":
            mac = normalize_mac(parts[3])
            if mac != "00:00:00:00:00:00":
                table[parts[0]] = mac
    return table


async def read_arp_table() -> dict[str, str]:
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/net/arp", encoding="ascii") as fh:
                return parse_proc_net_arp(fh.read())
        except OSError:
            pass
    cmd = ["arp", "-an"] if sys.platform == "darwin" else ["arp", "-a"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL,
            **_NO_WINDOW,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    except (OSError, asyncio.TimeoutError) as exc:
        log.warning("arp table read failed: %s", exc)
        return {}
    return parse_arp_table(out.decode(errors="replace"))


# --------------------------------------------------------------------------- SSDP


def build_msearch(st: str, mx: int = 2) -> bytes:
    return (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_ADDR[0]}:{SSDP_ADDR[1]}\r\n"
        'MAN: "ssdp:discover"\r\n'
        f"MX: {mx}\r\n"
        f"ST: {st}\r\n"
        "USER-AGENT: pahra-companion/0.1 UPnP/1.1\r\n"
        "\r\n"
    ).encode()


def parse_ssdp_response(data: bytes) -> dict[str, str] | None:
    """Headers of an SSDP reply or NOTIFY, keys lower-cased. None if it is not one."""
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover
        return None
    lines = text.split("\r\n") if "\r\n" in text else text.split("\n")
    if not lines or not (lines[0].upper().startswith("HTTP/") or lines[0].upper().startswith("NOTIFY")):
        return None
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return headers


class _SSDPProtocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.replies: list[tuple[str, dict[str, str]]] = []

    def datagram_received(self, data: bytes, addr) -> None:  # type: ignore[override]
        headers = parse_ssdp_response(data)
        if headers is not None:
            self.replies.append((addr[0], headers))

    def error_received(self, exc: Exception) -> None:  # e.g. WSAECONNRESET on Windows
        log.debug("ssdp socket error: %s", exc)


async def ssdp_search(
    st: str = "ssdp:all", timeout: float = 3.0, bind_ip: str | None = None
) -> list[dict[str, str]]:
    """M-SEARCH and collect replies for `timeout` seconds. Each reply gets an "ip" key."""
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        if bind_ip:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(bind_ip))
            sock.bind((bind_ip, 0))
        else:
            sock.bind(("0.0.0.0", 0))
        sock.setblocking(False)
        transport, proto = await loop.create_datagram_endpoint(_SSDPProtocol, sock=sock)
    except OSError as exc:
        sock.close()
        log.warning("ssdp socket setup failed: %s", exc)
        return []
    try:
        msg = build_msearch(st, mx=max(1, min(int(timeout), 3)))
        transport.sendto(msg, SSDP_ADDR)
        await asyncio.sleep(0.25)
        transport.sendto(msg, SSDP_ADDR)
        await asyncio.sleep(max(0.0, timeout - 0.25))
    finally:
        transport.close()
    out, seen = [], set()
    for ip, headers in proto.replies:
        key = (ip, headers.get("location", ""), headers.get("usn", ""), headers.get("st", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append({"ip": ip, **headers})
    return out


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_device_description(xml_text: str) -> dict[str, Any]:
    """Pick the interesting fields out of a UPnP device description document."""
    if "<!ENTITY" in xml_text.upper():
        raise ValueError("refusing XML with entity declarations")
    root = ET.fromstring(xml_text)
    info: dict[str, Any] = {"device_types": [], "services": []}
    url_base = None
    first_device = None
    for el in root.iter():
        tag = _strip_ns(el.tag)
        if tag == "URLBase" and el.text:
            url_base = el.text.strip()
        elif tag == "device" and first_device is None:
            first_device = el
        elif tag == "deviceType" and el.text:
            info["device_types"].append(el.text.strip())
        elif tag == "service":
            svc = {_strip_ns(c.tag): (c.text or "").strip() for c in el}
            if svc.get("serviceType"):
                info["services"].append({"service_type": svc["serviceType"], "control_url": svc.get("controlURL", "")})
    fields = {
        "friendlyName": "friendly_name",
        "manufacturer": "manufacturer",
        "manufacturerURL": "manufacturer_url",
        "modelName": "model_name",
        "modelNumber": "model_number",
        "modelDescription": "model_description",
        "deviceType": "device_type",
    }
    if first_device is not None:
        for child in first_device:
            key = fields.get(_strip_ns(child.tag))
            if key and child.text:
                info[key] = child.text.strip()
    info["url_base"] = url_base
    return info


def location_allowed(location: str, interfaces: list[netinfo.LocalInterface] | None = None) -> bool:
    """Only fetch http(s) URLs whose host is an IPv4 address on a local private network."""
    try:
        parts = urlsplit(location)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return False
        netinfo.check_lan_ip(parts.hostname, interfaces)
        return True
    except (ValueError, netinfo.GuardError):
        return False


async def fetch_description(client: httpx.AsyncClient, location: str) -> dict[str, Any] | None:
    try:
        async with client.stream("GET", location) as resp:
            if resp.status_code != 200:
                return None
            body = b""
            async for chunk in resp.aiter_bytes():
                body += chunk
                if len(body) > MAX_DESCRIPTION_BYTES:
                    return None
        info = parse_device_description(body.decode("utf-8", errors="replace"))
        info["location"] = location
        return info
    except (httpx.HTTPError, ET.ParseError, ValueError) as exc:
        log.debug("description fetch failed for %s: %s", location, exc)
        return None


async def ssdp_discover(
    timeout: float = 3.0,
    bind_ip: str | None = None,
    only_ip: str | None = None,
    st: str = "ssdp:all",
) -> dict[str, list[dict[str, Any]]]:
    """{ip: [reply + description]} for SSDP responders on the LAN."""
    replies = await ssdp_search(st=st, timeout=timeout, bind_ip=bind_ip)
    ifs = netinfo.local_interfaces()
    by_ip: dict[str, list[dict[str, Any]]] = {}
    locations: dict[str, str] = {}
    for r in replies:
        ip = r["ip"]
        if only_ip and ip != only_ip:
            continue
        try:
            netinfo.check_lan_ip(ip, ifs)
        except netinfo.GuardError:
            continue
        entry = {k: r.get(k) for k in ("st", "usn", "server", "location") if r.get(k)}
        by_ip.setdefault(ip, []).append(entry)
        loc = r.get("location")
        if loc and loc not in locations and location_allowed(loc, ifs):
            locations[loc] = ip
    # Fetch each distinct description once (capped, concurrently).
    sem = asyncio.Semaphore(10)
    descs: dict[str, dict[str, Any]] = {}
    async with httpx.AsyncClient(timeout=2.0, follow_redirects=False, verify=False) as client:

        async def get(loc: str):
            async with sem:
                d = await fetch_description(client, loc)
                if d:
                    descs[loc] = d

        await asyncio.gather(*(get(loc) for loc in list(locations)[:60]))
    for ip, entries in by_ip.items():
        for e in entries:
            d = descs.get(e.get("location", ""))
            if d:
                e["description"] = {k: v for k, v in d.items() if k not in ("services", "url_base")}
    return by_ip


# --------------------------------------------------------------------------- mDNS


def _decode_txt(props: dict) -> dict[str, str]:
    out = {}
    for k, v in (props or {}).items():
        key = k.decode(errors="replace") if isinstance(k, bytes) else str(k)
        if v is None:
            out[key] = ""
        else:
            out[key] = v.decode(errors="replace") if isinstance(v, bytes) else str(v)
    return out


async def mdns_browse(
    timeout: float = 3.0, bind_ip: str | None = None, types: Iterable[str] = MDNS_TYPES
) -> dict[str, list[dict[str, Any]]]:
    """{ip: [{type, name, port, txt}]} for TV-related mDNS services."""
    try:
        from zeroconf import IPVersion, ServiceStateChange
        from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf
    except ImportError:  # pragma: no cover
        return {}
    found: set[tuple[str, str]] = set()

    def on_change(zeroconf, service_type, name, state_change):  # noqa: ANN001
        if state_change in (ServiceStateChange.Added, ServiceStateChange.Updated):
            found.add((service_type, name))

    try:
        kwargs: dict[str, Any] = {"ip_version": IPVersion.V4Only}
        if bind_ip:
            kwargs["interfaces"] = [bind_ip]
        aiozc = AsyncZeroconf(**kwargs)
    except OSError as exc:
        log.warning("mdns setup failed: %s", exc)
        return {}
    result: dict[str, list[dict[str, Any]]] = {}
    try:
        browser = AsyncServiceBrowser(aiozc.zeroconf, list(types), handlers=[on_change])
        await asyncio.sleep(timeout)

        async def resolve(stype: str, name: str):
            info = AsyncServiceInfo(stype, name)
            try:
                ok = await info.async_request(aiozc.zeroconf, 1500)
            except Exception as exc:  # noqa: BLE001
                log.debug("mdns resolve failed for %s: %s", name, exc)
                return
            if not ok:
                return
            entry = {
                "type": stype.removesuffix(".local."),
                "name": name.removesuffix("." + stype),
                "port": info.port,
                "txt": _decode_txt(info.properties),
            }
            for addr in info.parsed_addresses(IPVersion.V4Only):
                result.setdefault(addr, []).append(entry)

        await asyncio.gather(*(resolve(t, n) for t, n in found))
        await browser.async_cancel()
    finally:
        await aiozc.async_close()
    return result


# --------------------------------------------------------------------------- TCP probes


async def tcp_open(ip: str, port: int, timeout: float = PROBE_TIMEOUT) -> bool:
    try:
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=timeout)
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), timeout=0.5)
    except (OSError, asyncio.TimeoutError):
        pass
    return True


async def probe_ports(
    ips: Iterable[str], ports: Iterable[int] = PROBE_PORTS, concurrency: int = PROBE_CONCURRENCY
) -> dict[str, list[int]]:
    sem = asyncio.Semaphore(concurrency)
    ports = list(ports)
    result: dict[str, list[int]] = {}

    async def one(ip: str, port: int):
        async with sem:
            if await tcp_open(ip, port):
                result.setdefault(ip, []).append(port)

    await asyncio.gather(*(one(ip, p) for ip in ips for p in ports))
    return {ip: sorted(p) for ip, p in result.items()}


# --------------------------------------------------------------------------- router admin page

_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


async def http_title(ip: str, timeout: float = 2.5) -> str | None:
    """<title> of the device's web page, following at most two same-host redirects. No login."""
    netinfo.check_lan_ip(ip)
    url = f"http://{ip}/"
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, verify=False) as client:
        for _ in range(3):
            try:
                resp = await client.get(url)
            except httpx.HTTPError:
                return None
            if resp.is_redirect and resp.headers.get("location"):
                nxt = urljoin(url, resp.headers["location"])
                if urlsplit(nxt).hostname != ip:
                    return None
                url = nxt
                continue
            m = _TITLE.search(resp.text[:65536])
            if m:
                return re.sub(r"\s+", " ", m.group(1)).strip()[:120] or None
            return None
    return None


# --------------------------------------------------------------------------- orchestration


async def gather_evidence(
    network: IPv4Network,
    bind_ip: str | None = None,
    exclude: Iterable[str] = (),
    listen_seconds: float = 3.0,
) -> dict[str, dict[str, Any]]:
    """Run ping sweep, SSDP and mDNS together, then ARP and port probes. {ip: evidence}."""
    sweep, ssdp, mdns = await asyncio.gather(
        ping_sweep(network),
        ssdp_discover(timeout=listen_seconds, bind_ip=bind_ip),
        mdns_browse(timeout=listen_seconds, bind_ip=bind_ip),
    )
    arp = await read_arp_table()
    excluded = set(exclude)

    def in_net(ip: str) -> bool:
        try:
            a = IPv4Address(ip)
        except ValueError:
            return False
        return a in network and a not in (network.network_address, network.broadcast_address)

    ips = {ip for ip in set(sweep) | set(ssdp) | set(mdns) | set(arp) if in_net(ip) and ip not in excluded}
    ports = await probe_ports(ips)
    evidence = {}
    for ip in sorted(ips, key=lambda s: IPv4Address(s)):
        evidence[ip] = {
            "ip": ip,
            "alive": ip in sweep,
            "rtt_ms": sweep.get(ip),
            "mac": arp.get(ip),
            "ssdp": ssdp.get(ip, []),
            "mdns": mdns.get(ip, []),
            "open_ports": ports.get(ip, []),
        }
    return evidence


async def probe_one(ip: str, bind_ip: str | None = None, listen_seconds: float = 3.0) -> dict[str, Any]:
    """Deep look at one host: ping, ports, ARP, and SSDP/mDNS answers from that IP only."""
    ping_r, ports, ssdp, mdns = await asyncio.gather(
        ping(ip),
        probe_ports([ip]),
        ssdp_discover(timeout=listen_seconds, bind_ip=bind_ip, only_ip=ip),
        mdns_browse(timeout=listen_seconds, bind_ip=bind_ip),
    )
    arp = await read_arp_table()
    return {
        "ip": ip,
        "alive": ping_r["alive"],
        "rtt_ms": ping_r["rtt_ms"],
        "mac": arp.get(ip),
        "ssdp": ssdp.get(ip, []),
        "mdns": mdns.get(ip, []),
        "open_ports": ports.get(ip, []),
    }
