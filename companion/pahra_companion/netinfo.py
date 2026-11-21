"""Local network facts and the subnet guard.

The guard is the companion's main safety rail: every IP or subnet a tool touches
must be IPv4, inside RFC 1918 space, and on a network that one of this machine's
interfaces is attached to. Scans are capped at /22 (1024 addresses).
"""

from __future__ import annotations

import ipaddress
import logging
import re
import subprocess
import sys
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Interface, IPv4Network

import ifaddr

log = logging.getLogger(__name__)

RFC1918 = (
    IPv4Network("10.0.0.0/8"),
    IPv4Network("172.16.0.0/12"),
    IPv4Network("192.168.0.0/16"),
)
CGNAT_SPACE = IPv4Network("100.64.0.0/10")
# Smallest prefix length a scan may use: /22 = 1024 addresses.
MAX_SCAN_PREFIX = 22
# When the local interface sits on a network larger than /22 and no subnet is
# given, scan the /24 around this machine instead.
CLAMP_PREFIX = 24


class GuardError(ValueError):
    """An IP or subnet failed the local-RFC1918 guard."""

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.hint = hint


@dataclass(frozen=True)
class LocalInterface:
    name: str
    iface: IPv4Interface

    @property
    def ip(self) -> IPv4Address:
        return self.iface.ip

    @property
    def network(self) -> IPv4Network:
        return self.iface.network

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "ip": str(self.ip),
            "prefix": self.iface.network.prefixlen,
            "network": str(self.network),
            "private": is_rfc1918(self.ip),
        }


def is_rfc1918(addr: IPv4Address | IPv4Network) -> bool:
    if isinstance(addr, IPv4Network):
        return any(addr.subnet_of(n) for n in RFC1918)
    return any(addr in n for n in RFC1918)


# --------------------------------------------------------------------------- interfaces


def local_interfaces() -> list[LocalInterface]:
    """IPv4 interfaces on this machine, without loopback and link-local (169.254/16)."""
    out: list[LocalInterface] = []
    for adapter in ifaddr.get_adapters():
        for ip in adapter.ips:
            if not isinstance(ip.ip, str):
                continue  # IPv6 entries are tuples
            try:
                iface = IPv4Interface(f"{ip.ip}/{ip.network_prefix}")
            except ValueError:
                continue
            if iface.ip.is_loopback or iface.ip.is_link_local or iface.ip.is_unspecified:
                continue
            out.append(LocalInterface(name=adapter.nice_name or adapter.name, iface=iface))
    return out


def ipv6_global_addresses() -> list[str]:
    """Global-unicast IPv6 addresses (2000::/3) on this machine."""
    found: list[str] = []
    for adapter in ifaddr.get_adapters():
        for ip in adapter.ips:
            if isinstance(ip.ip, tuple):
                try:
                    addr = ipaddress.IPv6Address(ip.ip[0])
                except ValueError:
                    continue
                if addr in ipaddress.IPv6Network("2000::/3"):
                    found.append(str(addr))
    return found


# --------------------------------------------------------------------------- default gateway

_IPV4 = r"(?:\d{1,3}\.){3}\d{1,3}"


def parse_route_print(text: str) -> list[tuple[str, str, int]]:
    """Default routes from Windows `route print -4`: [(gateway, interface_ip, metric)].

    Language-neutral: matches rows by shape ("0.0.0.0 0.0.0.0 <gw> <if> <metric>")
    rather than by the localized "Active Routes" header.
    """
    routes = []
    row = re.compile(rf"^\s*0\.0\.0\.0\s+0\.0\.0\.0\s+({_IPV4})\s+({_IPV4})\s+(\d+)\s*$")
    for line in text.splitlines():
        m = row.match(line)
        if m:
            routes.append((m.group(1), m.group(2), int(m.group(3))))
    return sorted(routes, key=lambda r: r[2])


def parse_proc_net_route(text: str) -> list[tuple[str, str, int]]:
    """Default routes from Linux /proc/net/route: [(gateway, iface_name, metric)]."""
    routes = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 8:
            continue
        iface, dest, gw, flags, metric = parts[0], parts[1], parts[2], parts[3], parts[6]
        if dest != "00000000":
            continue
        try:
            if not int(flags, 16) & 0x2:  # RTF_GATEWAY
                continue
            gw_ip = str(IPv4Address(int(gw, 16).to_bytes(4, "little")))
            routes.append((gw_ip, iface, int(metric)))
        except ValueError:
            continue
    return sorted(routes, key=lambda r: r[2])


def parse_ip_route(text: str) -> list[tuple[str, str, int]]:
    """Default routes from `ip -4 route show default`: [(gateway, dev, metric)]."""
    routes = []
    for line in text.splitlines():
        m = re.search(rf"^default via ({_IPV4})(?:.*?\bdev (\S+))?(?:.*?\bmetric (\d+))?", line.strip())
        if m:
            routes.append((m.group(1), m.group(2) or "", int(m.group(3) or 0)))
    return sorted(routes, key=lambda r: r[2])


def parse_bsd_route_get(text: str) -> list[tuple[str, str, int]]:
    """Default route from macOS/BSD `route -n get default`."""
    gw = re.search(rf"gateway:\s*({_IPV4})", text)
    iface = re.search(r"interface:\s*(\S+)", text)
    if not gw:
        return []
    return [(gw.group(1), iface.group(1) if iface else "", 0)]


def _run(cmd: list[str], timeout: float = 5.0) -> str:
    kwargs: dict = {"capture_output": True, "timeout": timeout, "stdin": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    proc = subprocess.run(cmd, **kwargs)
    return proc.stdout.decode(errors="replace")


def default_gateway() -> dict | None:
    """{"ip": gateway, "interface": interface ip or name} for the lowest-metric default route."""
    try:
        if sys.platform == "win32":
            routes = parse_route_print(_run(["route", "print", "-4"]))
            if not routes:
                out = _run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-Command",
                        "Get-NetRoute -AddressFamily IPv4 -DestinationPrefix 0.0.0.0/0 | "
                        "Sort-Object RouteMetric | ForEach-Object { $_.NextHop }",
                    ],
                    timeout=10,
                )
                routes = [(ip, "", i) for i, ip in enumerate(re.findall(_IPV4, out)) if ip != "0.0.0.0"]
        elif sys.platform.startswith("linux"):
            try:
                with open("/proc/net/route", encoding="ascii") as fh:
                    routes = parse_proc_net_route(fh.read())
            except OSError:
                routes = []
            if not routes:
                routes = parse_ip_route(_run(["ip", "-4", "route", "show", "default"]))
        else:
            routes = parse_bsd_route_get(_run(["route", "-n", "get", "default"]))
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("default gateway lookup failed: %s", exc)
        return None
    if not routes:
        return None
    gw, iface, _metric = routes[0]
    return {"ip": gw, "interface": iface}


# --------------------------------------------------------------------------- guard


def _ifaces(interfaces: list[LocalInterface] | None) -> list[LocalInterface]:
    return local_interfaces() if interfaces is None else interfaces


def check_lan_ip(ip: str, interfaces: list[LocalInterface] | None = None) -> IPv4Address:
    """Return the address if it is RFC 1918 and on a locally attached network, else raise GuardError."""
    try:
        addr = IPv4Address(str(ip).strip())
    except ValueError:
        raise GuardError(f"not an IPv4 address: {ip!r}", "Pass a LAN IPv4 address such as 192.168.1.20.") from None
    if not is_rfc1918(addr):
        raise GuardError(
            f"{addr} is not a private (RFC 1918) address",
            "The companion only talks to devices on your home LAN (10/8, 172.16/12, 192.168/16).",
        )
    for li in _ifaces(interfaces):
        if is_rfc1918(li.network) and addr in li.network:
            if addr == li.network.network_address or addr == li.network.broadcast_address:
                if li.network.prefixlen < 31:
                    raise GuardError(f"{addr} is the network or broadcast address of {li.network}")
            return addr
    raise GuardError(
        f"{addr} is not on a network this computer is attached to",
        "Run get_network_context to see the local subnets, then pick a device from scan_network.",
    )


def check_subnet(subnet: str, interfaces: list[LocalInterface] | None = None) -> IPv4Network:
    """Validate a scan range: IPv4, RFC 1918, at most /22, and overlapping a local interface network."""
    try:
        net = IPv4Network(str(subnet).strip(), strict=False)
    except ValueError:
        raise GuardError(f"not an IPv4 subnet: {subnet!r}", "Use CIDR form, e.g. 192.168.1.0/24.") from None
    if not is_rfc1918(net):
        raise GuardError(
            f"{net} is not inside private (RFC 1918) space",
            "Only home-LAN ranges can be scanned (10/8, 172.16/12, 192.168/16).",
        )
    if net.prefixlen < MAX_SCAN_PREFIX:
        raise GuardError(
            f"{net} is too large ({net.num_addresses} addresses); the limit is /{MAX_SCAN_PREFIX}",
            "Scan your own /24, e.g. the subnet from get_network_context.",
        )
    for li in _ifaces(interfaces):
        if is_rfc1918(li.network) and net.overlaps(li.network):
            return net
    raise GuardError(
        f"{net} is not a network this computer is attached to",
        "Run get_network_context to see the local subnets.",
    )


def primary_interface(
    interfaces: list[LocalInterface] | None = None, gateway_ip: str | None = None
) -> LocalInterface | None:
    """The private interface that holds the default gateway, else the first private one."""
    ifs = [li for li in _ifaces(interfaces) if is_rfc1918(li.ip)]
    if gateway_ip:
        try:
            gw = IPv4Address(gateway_ip)
            for li in ifs:
                if gw in li.network:
                    return li
        except ValueError:
            pass
    # Prefer ordinary home-sized networks over virtual adapters with big ranges.
    ifs.sort(key=lambda li: (li.network.prefixlen < MAX_SCAN_PREFIX, -li.network.prefixlen))
    return ifs[0] if ifs else None


def default_scan_subnet(
    interfaces: list[LocalInterface] | None = None, gateway_ip: str | None = None
) -> tuple[IPv4Network, bool]:
    """(subnet, clamped) to scan when the caller gives none."""
    li = primary_interface(interfaces, gateway_ip)
    if li is None:
        raise GuardError(
            "this computer has no private IPv4 address",
            "Connect the laptop to the same Wi-Fi or LAN as the TV.",
        )
    if li.network.prefixlen >= MAX_SCAN_PREFIX:
        return li.network, False
    return IPv4Interface(f"{li.ip}/{CLAMP_PREFIX}").network, True
