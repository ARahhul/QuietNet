import pytest

from pahra_companion import discovery

ARP_WINDOWS = """
Interface: 192.168.1.10 --- 0xc
  Internet Address      Physical Address      Type
  192.168.1.1           a4-91-b1-12-34-56     dynamic
  192.168.1.31          a8-23-fe-12-34-56     dynamic
  192.168.1.255         ff-ff-ff-ff-ff-ff     static
  224.0.0.22            01-00-5e-00-00-16     static
  239.255.255.250       01-00-5e-7f-ff-fa     static
"""

ARP_WINDOWS_DE = """
Schnittstelle: 192.168.1.10 --- 0xc
  Internetadresse       Physische Adresse     Typ
  192.168.1.1           a4-91-b1-12-34-56     dynamisch
"""

ARP_LINUX = """
? (192.168.1.1) at a4:91:b1:12:34:56 [ether] on wlan0
? (192.168.1.9) at <incomplete> on wlan0
tv.lan (192.168.1.34) at f0:27:2d:11:22:33 [ether] on wlan0
"""

ARP_MACOS = "? (192.168.1.20) at 0:1c:62:a:b:c on en0 ifscope [ethernet]\n? (192.168.1.255) at ff:ff:ff:ff:ff:ff on en0 ifscope [ethernet]\n"

IP_NEIGH = "192.168.1.1 dev wlan0 lladdr a4:91:b1:12:34:56 REACHABLE\n192.168.1.7 dev wlan0 FAILED\n"


def test_arp_windows_skips_broadcast_and_multicast():
    assert discovery.parse_arp_table(ARP_WINDOWS) == {"192.168.1.1": "a4:91:b1:12:34:56", "192.168.1.31": "a8:23:fe:12:34:56"}


def test_arp_windows_localized():
    assert discovery.parse_arp_table(ARP_WINDOWS_DE) == {"192.168.1.1": "a4:91:b1:12:34:56"}


def test_arp_linux_and_macos():
    assert discovery.parse_arp_table(ARP_LINUX) == {"192.168.1.1": "a4:91:b1:12:34:56", "192.168.1.34": "f0:27:2d:11:22:33"}
    assert discovery.parse_arp_table(ARP_MACOS) == {"192.168.1.20": "00:1c:62:0a:0b:0c"}
    assert discovery.parse_arp_table(IP_NEIGH) == {"192.168.1.1": "a4:91:b1:12:34:56"}


def test_proc_net_arp():
    text = (
        "IP address       HW type     Flags       HW address            Mask     Device\n"
        "192.168.1.1      0x1         0x2         a4:91:b1:12:34:56     *        wlan0\n"
        "192.168.1.9      0x1         0x0         00:00:00:00:00:00     *        wlan0\n"
    )
    assert discovery.parse_proc_net_arp(text) == {"192.168.1.1": "a4:91:b1:12:34:56"}


@pytest.mark.parametrize(
    "platform,cmd",
    [
        ("win32", ["ping", "-n", "1", "-w", "400", "192.168.1.1"]),
        ("linux", ["ping", "-c", "1", "-W", "1", "192.168.1.1"]),
        ("darwin", ["ping", "-c", "1", "-W", "1000", "192.168.1.1"]),
    ],
)
def test_ping_command(platform, cmd):
    assert discovery.ping_command("192.168.1.1", platform) == cmd


@pytest.mark.parametrize(
    "rc,out,platform,alive,rtt",
    [
        (0, "Reply from 192.168.1.1: bytes=32 time=3ms TTL=64", "win32", True, 3.0),
        (0, "Antwort von 192.168.1.1: Bytes=32 Zeit<1ms TTL=64", "win32", True, 1.0),
        (0, "Réponse de 192.168.1.1 : octets=32 temps=12 ms TTL=64", "win32", True, 12.0),
        # Windows exits 0 when the local stack answers "Destination host unreachable".
        (0, "Reply from 192.168.1.10: Destination host unreachable.", "win32", False, None),
        (1, "Request timed out.", "win32", False, None),
        (0, "64 bytes from 192.168.1.1: icmp_seq=1 ttl=64 time=3.21 ms", "linux", True, 3.21),
        (1, "1 packets transmitted, 0 received", "linux", False, None),
    ],
)
def test_judge_ping_uses_exit_code(rc, out, platform, alive, rtt):
    assert discovery.judge_ping(rc, out, platform) == (alive, rtt)


def test_parse_ssdp_response():
    data = (
        b"HTTP/1.1 200 OK\r\nCACHE-CONTROL: max-age=1800\r\nLOCATION: http://192.168.1.31:1453/\r\n"
        b"SERVER: WebOS/4.1.0 UPnP/1.0\r\nST: urn:lge-com:service:webos-second-screen:1\r\nUSN: uuid:abc::urn:x\r\n\r\n"
    )
    h = discovery.parse_ssdp_response(data)
    assert h["location"] == "http://192.168.1.31:1453/"
    assert h["server"].startswith("WebOS")
    assert discovery.parse_ssdp_response(b"M-SEARCH * HTTP/1.1\r\n\r\n") is None


def test_build_msearch():
    msg = discovery.build_msearch("ssdp:all", mx=2).decode()
    assert msg.startswith("M-SEARCH * HTTP/1.1\r\n")
    assert 'MAN: "ssdp:discover"' in msg and "ST: ssdp:all" in msg and msg.endswith("\r\n\r\n")


DESCRIPTION = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <device>
    <deviceType>urn:schemas-upnp-org:device:Basic:1</deviceType>
    <friendlyName>[LG] webOS TV OLED55C1PTB</friendlyName>
    <manufacturer>LG Electronics</manufacturer>
    <modelName>OLED55C1PTB</modelName>
    <UDN>uuid:abc</UDN>
    <serviceList>
      <service><serviceType>urn:lge-com:service:webos-second-screen:1</serviceType><controlURL>/ctl</controlURL></service>
    </serviceList>
  </device>
</root>"""


def test_parse_device_description():
    d = discovery.parse_device_description(DESCRIPTION)
    assert d["friendly_name"] == "[LG] webOS TV OLED55C1PTB"
    assert d["manufacturer"] == "LG Electronics"
    assert d["model_name"] == "OLED55C1PTB"
    assert d["services"] == [{"service_type": "urn:lge-com:service:webos-second-screen:1", "control_url": "/ctl"}]


def test_parse_device_description_refuses_entities():
    evil = '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><root><device><friendlyName>&a;</friendlyName></device></root>'
    with pytest.raises(ValueError):
        discovery.parse_device_description(evil)


@pytest.mark.parametrize(
    "location,allowed",
    [
        ("http://192.168.1.31:1453/", True),
        ("https://192.168.1.31/desc.xml", True),
        ("http://8.8.8.8/desc.xml", False),  # off-LAN: never fetched
        ("http://192.168.50.1/desc.xml", False),  # private but not local
        ("http://evil.example.com/desc.xml", False),
        ("file:///etc/passwd", False),
        ("http://127.0.0.1:8765/mcp", False),
    ],
)
def test_location_guard(location, allowed, home_ifaces):
    assert discovery.location_allowed(location, home_ifaces) is allowed


async def test_tcp_open_on_closed_port():
    assert await discovery.tcp_open("127.0.0.1", 1, timeout=0.5) is False
