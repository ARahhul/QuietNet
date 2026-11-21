"""CGNAT check: the router's WAN IP (UPnP IGD) against the public IP the internet sees.

Why it matters: when the home is behind carrier-grade NAT (common on JioFiber,
Airtel and ACT IPv4), the Pahra VM sees a public IPv4 address shared with other
customers. Pointing the router's plain DNS at the VM then gives no per-home
identity, so router DNS is only recommended when the verdict is "not_cgnat".
"""

from __future__ import annotations

import asyncio
import logging
import re
from ipaddress import IPv4Address
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from . import discovery, netinfo

log = logging.getLogger(__name__)

IGD_ST = "urn:schemas-upnp-org:device:InternetGatewayDevice:1"
WAN_SERVICES = (
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANIPConnection:2",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
)
PUBLIC_IP_URL = "https://1.1.1.1/cdn-cgi/trace"


def find_wan_service(description: dict[str, Any], location: str) -> tuple[str, str] | None:
    """(absolute control URL, service type) of the first WAN*Connection service."""
    base = description.get("url_base") or location
    for wanted in WAN_SERVICES:
        for svc in description.get("services") or []:
            if svc.get("service_type") == wanted and svc.get("control_url"):
                return urljoin(base, svc["control_url"]), wanted
    return None


def soap_envelope(service_type: str) -> str:
    return (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f'<s:Body><u:GetExternalIPAddress xmlns:u="{service_type}"/></s:Body>'
        "</s:Envelope>"
    )


def parse_external_ip(soap_xml: str) -> str | None:
    m = re.search(r"<(?:\w+:)?NewExternalIPAddress>\s*([^<\s]*)\s*</", soap_xml)
    if not m or not m.group(1):
        return None
    try:
        return str(IPv4Address(m.group(1)))
    except ValueError:
        return None


def parse_trace(text: str) -> str | None:
    m = re.search(r"^ip=(\S+)$", text, re.MULTILINE)
    return m.group(1) if m else None


def verdict(wan_ip: str | None, public_ip: str | None) -> dict[str, Any]:
    """{"verdict": cgnat | not_cgnat | unknown, "reason": ...}."""
    if not wan_ip:
        return {
            "verdict": "unknown",
            "reason": "router did not answer UPnP IGD (UPnP off or unsupported), so its WAN IP is unknown",
        }
    try:
        wan = IPv4Address(wan_ip)
    except ValueError:
        return {"verdict": "unknown", "reason": f"router reported an invalid WAN IP {wan_ip!r}"}
    if wan in netinfo.CGNAT_SPACE:
        return {"verdict": "cgnat", "reason": f"router WAN IP {wan} is in the CGNAT range 100.64.0.0/10"}
    if netinfo.is_rfc1918(wan):
        return {
            "verdict": "cgnat",
            "reason": f"router WAN IP {wan} is private: another NAT sits upstream (ISP CGNAT or a second router/ONT)",
        }
    if public_ip and public_ip != str(wan):
        return {
            "verdict": "cgnat",
            "reason": f"router WAN IP {wan} differs from the public IP {public_ip}: the ISP translates again",
        }
    if not public_ip:
        return {"verdict": "not_cgnat", "reason": f"router WAN IP {wan} is public (public IP lookup failed)"}
    return {"verdict": "not_cgnat", "reason": f"router WAN IP {wan} matches the public IP"}


async def public_ip(timeout: float = 4.0) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(PUBLIC_IP_URL)
            r.raise_for_status()
            return parse_trace(r.text)
    except httpx.HTTPError as exc:
        log.info("public IP lookup failed: %s", exc)
        return None


async def igd_wan_ip(bind_ip: str | None = None, timeout: float = 2.5) -> dict[str, Any]:
    """Ask the LAN's UPnP Internet Gateway Device for its external IP."""
    replies = await discovery.ssdp_search(st=IGD_ST, timeout=timeout, bind_ip=bind_ip)
    ifs = netinfo.local_interfaces()
    async with httpx.AsyncClient(timeout=3.0, follow_redirects=False, verify=False) as client:
        for r in replies:
            loc = r.get("location")
            if not loc or not discovery.location_allowed(loc, ifs):
                continue
            desc = await discovery.fetch_description(client, loc)
            if not desc:
                continue
            svc = find_wan_service(desc, loc)
            if not svc:
                continue
            control_url, service_type = svc
            if urlsplit(control_url).hostname != urlsplit(loc).hostname:
                continue  # never follow a control URL to another host
            try:
                resp = await client.post(
                    control_url,
                    content=soap_envelope(service_type),
                    headers={
                        "Content-Type": 'text/xml; charset="utf-8"',
                        "SOAPAction": f'"{service_type}#GetExternalIPAddress"',
                    },
                )
            except httpx.HTTPError as exc:
                log.info("IGD SOAP call failed: %s", exc)
                continue
            wan = parse_external_ip(resp.text)
            return {
                "wan_ip": wan,
                "igd": {
                    "ip": r["ip"],
                    "manufacturer": desc.get("manufacturer"),
                    "model": desc.get("model_name"),
                    "friendly_name": desc.get("friendly_name"),
                    "service": service_type,
                },
            }
    return {"wan_ip": None, "igd": None}


async def check(bind_ip: str | None = None) -> dict[str, Any]:
    igd, pub = await asyncio.gather(igd_wan_ip(bind_ip=bind_ip), public_ip())
    v = verdict(igd["wan_ip"], pub)
    return {**v, "wan_ip": igd["wan_ip"], "public_ip": pub, "igd": igd["igd"]}
