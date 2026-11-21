from ipaddress import IPv4Interface, IPv4Network

import pytest

from pahra_companion import netinfo
from pahra_companion.netinfo import GuardError, LocalInterface


# ---------------------------------------------------------------- subnet guard


@pytest.mark.parametrize(
    "subnet",
    [
        "8.8.8.0/24",  # public
        "1.1.1.1/32",  # public host
        "100.64.0.0/24",  # CGNAT shared space is not RFC 1918
        "169.254.0.0/24",  # link-local
        "127.0.0.0/24",  # loopback
        "0.0.0.0/0",  # everything
        "192.169.1.0/24",  # just outside 192.168/16
        "172.32.0.0/24",  # just outside 172.16/12
    ],
)
def test_subnet_guard_rejects_non_private(subnet, home_ifaces):
    with pytest.raises(GuardError, match="private"):
        netinfo.check_subnet(subnet, home_ifaces)


@pytest.mark.parametrize("subnet", ["10.0.0.0/8", "192.168.0.0/16", "192.168.0.0/21", "172.16.0.0/12", "172.25.224.0/20"])
def test_subnet_guard_rejects_larger_than_22(subnet, home_ifaces):
    with pytest.raises(GuardError, match="too large"):
        netinfo.check_subnet(subnet, home_ifaces)


def test_subnet_guard_rejects_private_but_not_local(home_ifaces):
    with pytest.raises(GuardError, match="not a network this computer is attached to"):
        netinfo.check_subnet("192.168.50.0/24", home_ifaces)


@pytest.mark.parametrize("subnet", ["garbage", "192.168.1.0/33", "fe80::/64", "", "192.168.1.0/24; rm -rf /"])
def test_subnet_guard_rejects_malformed(subnet, home_ifaces):
    with pytest.raises(GuardError):
        netinfo.check_subnet(subnet, home_ifaces)


@pytest.mark.parametrize(
    "subnet,expected",
    [
        ("192.168.1.0/24", "192.168.1.0/24"),
        ("192.168.1.77/24", "192.168.1.0/24"),  # host bits are tolerated
        ("192.168.0.0/22", "192.168.0.0/22"),  # /22 containing the local /24
        ("192.168.1.128/25", "192.168.1.128/25"),
        ("172.25.224.0/22", "172.25.224.0/22"),  # inside the local /20
    ],
)
def test_subnet_guard_accepts_local_private(subnet, expected, home_ifaces):
    assert str(netinfo.check_subnet(subnet, home_ifaces)) == expected


def test_subnet_guard_with_no_interfaces():
    with pytest.raises(GuardError):
        netinfo.check_subnet("192.168.1.0/24", [])


@pytest.mark.parametrize(
    "ip",
    ["8.8.8.8", "100.64.1.1", "127.0.0.1", "192.168.50.20", "10.0.0.5", "not-an-ip", "192.168.1.255", "192.168.1.0", "::1"],
)
def test_ip_guard_rejects(ip, home_ifaces):
    with pytest.raises(GuardError):
        netinfo.check_lan_ip(ip, home_ifaces)


@pytest.mark.parametrize("ip", ["192.168.1.1", "192.168.1.31", " 192.168.1.254 ", "172.25.230.7"])
def test_ip_guard_accepts_local(ip, home_ifaces):
    assert str(netinfo.check_lan_ip(ip, home_ifaces)) == ip.strip()


def test_default_scan_subnet_prefers_gateway_interface(home_ifaces):
    net, clamped = netinfo.default_scan_subnet(home_ifaces, "192.168.1.1")
    assert net == IPv4Network("192.168.1.0/24") and not clamped


def test_default_scan_subnet_clamps_big_networks():
    ifs = [LocalInterface("corp", IPv4Interface("10.20.30.40/16"))]
    net, clamped = netinfo.default_scan_subnet(ifs, "10.20.0.1")
    assert net == IPv4Network("10.20.30.0/24") and clamped


def test_default_scan_subnet_skips_public_interfaces():
    ifs = [LocalInterface("weird", IPv4Interface("203.0.113.5/24"))]
    with pytest.raises(GuardError):
        netinfo.default_scan_subnet(ifs, None)


# ---------------------------------------------------------------- gateway parsers

ROUTE_PRINT_EN = """\
===========================================================================
IPv4 Route Table
===========================================================================
Active Routes:
Network Destination        Netmask          Gateway       Interface  Metric
          0.0.0.0          0.0.0.0      192.168.0.1    192.168.0.100     25
          0.0.0.0          0.0.0.0      10.8.0.1         10.8.0.6       50
        127.0.0.0        255.0.0.0         On-link         127.0.0.1    331
      192.168.0.0    255.255.255.0         On-link     192.168.0.100    281
===========================================================================
Persistent Routes:
  None
"""

ROUTE_PRINT_DE = """\
IPv4-Routentabelle
===========================================================================
Aktive Routen:
     Netzwerkziel    Netzwerkmaske          Gateway    Schnittstelle Metrik
          0.0.0.0          0.0.0.0      192.168.29.1   192.168.29.44     35
        127.0.0.0        255.0.0.0   Auf Verbindung         127.0.0.1    331
"""


def test_parse_route_print_english_picks_lowest_metric():
    routes = netinfo.parse_route_print(ROUTE_PRINT_EN)
    assert routes[0] == ("192.168.0.1", "192.168.0.100", 25)
    assert len(routes) == 2


def test_parse_route_print_localized():
    assert netinfo.parse_route_print(ROUTE_PRINT_DE)[0][:2] == ("192.168.29.1", "192.168.29.44")


def test_parse_proc_net_route():
    text = (
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
        "eth0\t00000000\t0101A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
        "eth0\t0001A8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0\n"
    )
    assert netinfo.parse_proc_net_route(text) == [("192.168.1.1", "eth0", 100)]


def test_parse_ip_route():
    text = "default via 192.168.1.1 dev wlan0 proto dhcp src 192.168.1.10 metric 600\n"
    assert netinfo.parse_ip_route(text) == [("192.168.1.1", "wlan0", 600)]


def test_parse_bsd_route_get():
    text = "   route to: default\ndestination: default\n    gateway: 192.168.1.1\n  interface: en0\n"
    assert netinfo.parse_bsd_route_get(text) == [("192.168.1.1", "en0", 0)]
