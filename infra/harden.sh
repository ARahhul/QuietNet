#!/usr/bin/env bash
# Hide AdGuard's login page from the internet: Caddy takes :443 and forwards ONLY /dns-query* to AdGuard.
# Run on the VM, in the infra folder, after adguard_init.py has finished:
#   bash harden.sh dns.example.com
# After this, re-run adguard_init.py only with --no-https (otherwise AdGuard wants :443 back).
set -euo pipefail
DNS_HOST="${1:?usage: harden.sh <dns-hostname e.g. dns.example.com>}"
cd "$(dirname "$0")"
CONF=agd-conf/AdGuardHome.yaml
[ -f "$CONF" ] || { echo "Missing $CONF: start AdGuard and run adguard_init.py first." >&2; exit 1; }

echo "==> Stopping AdGuard"
sudo docker compose down

echo "==> AdGuard: no HTTPS listener of its own; serve DoH over plain HTTP to Caddy only; trust Caddy's X-Forwarded-For"
sudo cp "$CONF" "$CONF.bak"
sudo python3 - "$CONF" <<'PY'
import sys
import yaml  # python3-yaml ships with Ubuntu cloud images
path = sys.argv[1]
with open(path) as f:
    c = yaml.safe_load(f)
c.setdefault("tls", {})["port_https"] = 0
if "doh" in (c.get("http") or {}):
    c["http"]["doh"]["insecure_enabled"] = True  # AdGuard v0.108+/v1
else:
    c["tls"]["allow_unencrypted_doh"] = True  # older AdGuard
proxies = c.setdefault("dns", {}).setdefault("trusted_proxies", ["127.0.0.0/8", "::1"])
if "172.16.0.0/12" not in proxies:
    proxies.append("172.16.0.0/12")  # Docker's bridge networks
with open(path, "w") as f:
    yaml.safe_dump(c, f, sort_keys=False)
print("   updated", path, "(backup: AdGuardHome.yaml.bak)")
PY

echo "==> Starting AdGuard + Caddy"
sudo DNS_HOST="$DNS_HOST" docker compose -f docker-compose.harden.yml up -d
sleep 3
code=$(curl -s -o /dev/null -w '%{http_code}' "https://$DNS_HOST/" || true)
echo "https://$DNS_HOST/ -> HTTP $code (404 expected: the login page is hidden)"
echo "Check DoH still works from the laptop: companion\\.venv\\Scripts\\python infra\\verify.py --dns-host $DNS_HOST --client-id <a device id>"
