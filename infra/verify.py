#!/usr/bin/env python3
"""Check a Pahra DNS server from the laptop (Windows has no kdig/dig). Needs dnspython + httpx:
run it with the companion's Python:

    companion\\.venv\\Scripts\\python infra\\verify.py --dns-host dns.example.com --client-id test-phone --vm-ip 203.0.113.10

Checks:
  1. DoT on :853 with the device's own name (<client-id>.dns.<domain>) and a valid wildcard certificate
  2. DoH at https://dns.<domain>/dns-query/<client-id>
  3. the AdGuard admin/login page is NOT public on https://dns.<domain>/
  4. plain DNS on :53 from this network is REFUSED (the access list is closed to strangers)
Use a client id that exists in Pahra (add the device first), or 1 and 2 are REFUSED too.
"""

import argparse
import socket
import ssl
import sys

import dns.exception
import dns.message
import dns.query
import dns.rcode
import dns.rdatatype
import httpx

PROBE = "example.com"


def _answer_summary(r: dns.message.Message) -> str:
    ips = [rr.to_text() for rrset in r.answer for rr in rrset if rrset.rdtype == dns.rdatatype.A]
    return f"{dns.rcode.to_text(r.rcode())} {', '.join(ips) or '(no A records)'}"


def check_dot(server: str, sni: str, port: int = 853, name: str = PROBE) -> tuple[bool, str]:
    ip = socket.gethostbyname(server)
    ctx = ssl.create_default_context()  # verifies the certificate chain AND that it covers `sni`
    q = dns.message.make_query(name, "A")
    r = dns.query.tls(q, ip, port=port, server_hostname=sni, ssl_context=ctx, timeout=6)
    return r.rcode() == dns.rcode.NOERROR and bool(r.answer), _answer_summary(r)


def check_doh(url: str, name: str = PROBE) -> tuple[bool, str]:
    q = dns.message.make_query(name, "A")
    q.id = 0  # RFC 8484: cache-friendly id
    resp = httpx.post(url, content=q.to_wire(), headers={"content-type": "application/dns-message",
                                                          "accept": "application/dns-message"}, timeout=8)
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"
    r = dns.message.from_wire(resp.content)
    return r.rcode() == dns.rcode.NOERROR and bool(r.answer), _answer_summary(r)


def check_admin_hidden(base: str) -> tuple[bool, str]:
    try:
        resp = httpx.get(base, timeout=8, follow_redirects=True)
    except httpx.HTTPError as e:
        return True, f"no web page ({e.__class__.__name__})"
    body = resp.text.lower()
    exposed = "adguard" in body and ("login" in body or "password" in body or "<title>adguard" in body)
    return not exposed, f"HTTP {resp.status_code}" + (": AdGuard login page is PUBLIC" if exposed else "")


def check_plain_refused(vm_ip: str, name: str = PROBE) -> tuple[bool, str]:
    q = dns.message.make_query(name, "A")
    try:
        r = dns.query.udp(q, vm_ip, port=53, timeout=4)
    except dns.exception.Timeout:
        return False, "timed out: is UDP 53 open in the cloud firewall?"
    rc = dns.rcode.to_text(r.rcode())
    return r.rcode() == dns.rcode.REFUSED, rc + ("" if rc == "REFUSED" else ": anyone can use this server (open resolver)")


def main() -> int:
    p = argparse.ArgumentParser(description="Verify a Pahra DNS server")
    p.add_argument("--dns-host", required=True, help="e.g. dns.example.com")
    p.add_argument("--client-id", required=True, help="a device's Pahra id, e.g. test-phone")
    p.add_argument("--vm-ip", help="VM public IP, for the port-53 check")
    a = p.parse_args()

    checks = [
        ("DoT  " + f"{a.client_id}.{a.dns_host}:853", lambda: check_dot(a.dns_host, f"{a.client_id}.{a.dns_host}")),
        ("DoH  " + f"https://{a.dns_host}/dns-query/{a.client_id}", lambda: check_doh(f"https://{a.dns_host}/dns-query/{a.client_id}")),
        ("Admin page hidden on https://" + a.dns_host + "/", lambda: check_admin_hidden(f"https://{a.dns_host}/")),
    ]
    if a.vm_ip:
        checks.append((f"Port 53 closed to strangers ({a.vm_ip})", lambda: check_plain_refused(a.vm_ip)))
    failed = 0
    for label, fn in checks:
        try:
            ok, detail = fn()
        except Exception as e:  # show the reason, keep checking
            ok, detail = False, f"{e.__class__.__name__}: {e}"
        failed += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {label}: {detail}")
    if failed:
        print("\nSee infra/README.md 'Troubleshooting'.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
