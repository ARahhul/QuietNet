#!/usr/bin/env python3
"""Configure a fresh AdGuard Home for QuietNet through the admin API (stdlib only).

Run from the laptop with the SSH tunnel up (or on the VM itself):
    python adguard_init.py --dns-host dns.pahra.app --user admin --password '<strong>'

Steps (each is idempotent):
  1. finish the first-run install wizard (admin on :3000, DNS on :53)
  2. enable encryption: DoH :443 and DoT :853 with the Let's Encrypt wildcard cert
  3. upstreams (Cloudflare + Quad9 over TLS), demo TTLs (blocked 10 s, max cache 30 s)
  4. query log kept 24 h (QuietNet polls it every 30 s)
  5. global filter lists = malware/phishing only. Ad/tracker blocking is per-device via $client rules,
     otherwise ads would already be gone before "Block all ads" on stage.
  6. close the resolver: an empty access list means "answer everyone" (an open resolver that gets
     abused for DDoS amplification). A placeholder entry keeps it closed until the backend adds devices.
"""

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

TIF = "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/adblock/tif.mini.txt"
CLOSED = "pahra-closed"  # matches no device; the backend keeps operator entries and adds device ClientIDs


class AG:
    def __init__(self, base: str, user: str, pw: str):
        self.base = base.rstrip("/")
        self.auth = "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()

    def call(self, method: str, path: str, body=None, auth=True):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if auth:
            req.add_header("Authorization", self.auth)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
                return json.loads(raw) if raw and raw.strip().startswith((b"{", b"[")) else raw.decode()
        except urllib.error.HTTPError as e:
            raise SystemExit(f"{method} {path} -> {e.code}: {e.read().decode()[:300]}")
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            raise SystemExit(f"Can't reach AdGuard at {self.base} ({e}). Is the SSH tunnel up (infra/tunnel.ps1) "
                             "and the container running (docker ps on the VM)?")

    def wait_ready(self, seconds: int = 30) -> dict:
        end = time.time() + seconds
        while True:
            try:
                st = self.call("GET", "/control/status")
                if isinstance(st, dict):
                    return st
            except SystemExit:
                if time.time() > end:
                    raise
            time.sleep(1)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:3000")
    p.add_argument("--dns-host", required=True, help="e.g. dns.pahra.app")
    p.add_argument("--user", default="admin")
    # Read from $ADGUARD_PASS when not given, so it doesn't show up in `ps` / shell history on the VM.
    p.add_argument("--password", default=os.environ.get("ADGUARD_PASS"), required=not os.environ.get("ADGUARD_PASS"))
    p.add_argument("--max-cache-ttl", type=int, default=30)
    p.add_argument("--no-https", action="store_true",
                   help="after harden.sh: Caddy owns :443, so AdGuard must not listen there (DoT :853 stays)")
    a = p.parse_args()
    ag = AG(a.url, a.user, a.password)

    # 1. install wizard
    try:
        status = ag.call("GET", "/control/install/get_addresses", auth=False)
        if isinstance(status, dict) and "interfaces" in status:
            print("==> completing first-run setup")
            ag.call("POST", "/control/install/configure", {
                "web": {"ip": "0.0.0.0", "port": 3000},
                "dns": {"ip": "0.0.0.0", "port": 53},
                "username": a.user, "password": a.password,
            }, auth=False)
    except SystemExit as e:
        if "Can't reach" in str(e):
            raise
        # already installed: install endpoints are gone
    print("AdGuard version:", ag.wait_ready().get("version"))  # the web server restarts after the wizard

    # 2. encryption (DoH + DoT with the wildcard cert)
    live = f"/etc/letsencrypt/live/{a.dns_host}"
    tls = {
        "enabled": True, "server_name": a.dns_host, "force_https": False,
        "port_https": 0 if a.no_https else 443, "port_dns_over_tls": 853, "port_dns_over_quic": 853,
        "certificate_path": f"{live}/fullchain.pem", "private_key_path": f"{live}/privkey.pem",
        "certificate_chain": "", "private_key": "", "private_key_saved": False,
    }
    v = ag.call("POST", "/control/tls/validate", tls)
    if isinstance(v, dict) and v.get("warning_validation"):
        print("TLS warning:", v["warning_validation"])
    ag.call("POST", "/control/tls/configure", tls)
    print("==> encryption on: DoT :853 and DoH :443 for", a.dns_host)

    # 3. upstreams + demo TTLs
    ag.call("POST", "/control/dns_config", {
        "upstream_dns": ["tls://1.1.1.1", "tls://9.9.9.9"],
        "bootstrap_dns": ["1.1.1.1", "9.9.9.9"],
        "blocking_mode": "nxdomain",
        "blocked_response_ttl": 10,
        "cache_ttl_max": a.max_cache_ttl,
        "ratelimit": 30,
        "refuse_any": True,  # ANY queries are the classic amplification vector
    })
    print(f"==> upstreams set; blocked TTL 10 s; max cache TTL {a.max_cache_ttl} s")

    # 4. query log 24 h
    ag.call("PUT", "/control/querylog/config/update",
            {"enabled": True, "interval": 24 * 3600 * 1000, "anonymize_client_ip": False, "ignored": []})
    print("==> query log retention 24 h")

    # 5. global filters: threat intel only
    st = ag.call("GET", "/control/filtering/status")
    for f in st.get("filters") or []:
        if f["url"] != TIF:
            ag.call("POST", "/control/filtering/remove_url", {"url": f["url"], "whitelist": False})
            print("   removed global list:", f["name"])
    if not any(f["url"] == TIF for f in st.get("filters") or []):
        ag.call("POST", "/control/filtering/add_url", {"name": "HaGeZi Threat Intelligence (mini)", "url": TIF, "whitelist": False})
    ag.call("POST", "/control/filtering/config", {"enabled": True, "interval": 12})
    print("==> global filtering = threat intel only (ads/trackers are per-device $client rules)")

    # 6. never run as an open resolver
    acl = ag.call("GET", "/control/access/list")
    if not acl.get("allowed_clients"):
        ag.call("POST", "/control/access/set", {
            "allowed_clients": [CLOSED],
            "disallowed_clients": acl.get("disallowed_clients") or [],
            "blocked_hosts": acl.get("blocked_hosts") or [],
        })
        print(f"==> resolver closed to strangers (allowed_clients=[{CLOSED}]); the backend adds each device")

    print("\nDone. Next, verify from your laptop:")
    print(f"  kdig @{a.dns_host} +tls-host=test.{a.dns_host} doubleclick.net")
    print(f"  curl -H 'accept: application/dns-json' 'https://{a.dns_host}/dns-query/test?name=example.com&type=A'")
    print(f"  Also open https://{a.dns_host}/ in a browser. If the AdGuard LOGIN page appears, the admin UI is")
    print("  public on :443; see infra/README.md 'Hardening: hide the admin UI'.")


if __name__ == "__main__":
    sys.exit(main())
