import pytest

from pahra_companion import cgnat
from pahra_companion.discovery import parse_device_description


@pytest.mark.parametrize(
    "wan,public,expected",
    [
        ("100.72.13.5", "49.36.1.2", "cgnat"),  # shared address space
        ("100.64.0.1", None, "cgnat"),
        ("10.10.0.2", "49.36.1.2", "cgnat"),  # private WAN: double NAT / CGNAT
        ("49.36.9.9", "49.36.1.2", "cgnat"),  # WAN differs from public
        ("49.36.1.2", "49.36.1.2", "not_cgnat"),
        ("49.36.1.2", None, "not_cgnat"),
        (None, "49.36.1.2", "unknown"),  # IGD unavailable
        ("not-an-ip", None, "unknown"),
    ],
)
def test_verdict(wan, public, expected):
    v = cgnat.verdict(wan, public)
    assert v["verdict"] == expected
    assert v["reason"]


IGD_DESC = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
 <device>
  <deviceType>urn:schemas-upnp-org:device:InternetGatewayDevice:1</deviceType>
  <friendlyName>Home Gateway</friendlyName>
  <manufacturer>Example</manufacturer>
  <deviceList><device>
   <deviceType>urn:schemas-upnp-org:device:WANDevice:1</deviceType>
   <deviceList><device>
    <deviceType>urn:schemas-upnp-org:device:WANConnectionDevice:1</deviceType>
    <serviceList><service>
     <serviceType>urn:schemas-upnp-org:service:WANIPConnection:1</serviceType>
     <controlURL>/ctl/IPConn</controlURL>
    </service></serviceList>
   </device></deviceList>
  </device></deviceList>
 </device>
</root>"""


def test_find_wan_service_resolves_relative_control_url():
    desc = parse_device_description(IGD_DESC)
    assert cgnat.find_wan_service(desc, "http://192.168.29.1:1900/rootDesc.xml") == (
        "http://192.168.29.1:1900/ctl/IPConn",
        "urn:schemas-upnp-org:service:WANIPConnection:1",
    )


def test_parse_external_ip():
    soap = (
        '<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
        '<u:GetExternalIPAddressResponse xmlns:u="urn:schemas-upnp-org:service:WANIPConnection:1">'
        "<NewExternalIPAddress>100.72.13.5</NewExternalIPAddress></u:GetExternalIPAddressResponse></s:Body></s:Envelope>"
    )
    assert cgnat.parse_external_ip(soap) == "100.72.13.5"
    assert cgnat.parse_external_ip("<NewExternalIPAddress></NewExternalIPAddress>") is None


def test_soap_envelope():
    body = cgnat.soap_envelope("urn:schemas-upnp-org:service:WANIPConnection:1")
    assert 'GetExternalIPAddress xmlns:u="urn:schemas-upnp-org:service:WANIPConnection:1"' in body


def test_parse_trace():
    text = "fl=123\nh=1.1.1.1\nip=49.36.1.2\nts=1700000000\nvisit_scheme=https\n"
    assert cgnat.parse_trace(text) == "49.36.1.2"
    assert cgnat.parse_trace("nothing") is None
