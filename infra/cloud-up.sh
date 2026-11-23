#!/usr/bin/env bash
# Bring up (or update) the whole QuietNet stack on the VM: AdGuard + backend + Caddy.
#   bash ~/quietnet/infra/cloud-up.sh quietnet.in
# First run cloud-secrets.sh. DNS records needed at your DNS provider, all pointing at the VM:
#   dns.<domain>   *.dns.<domain>   api.<domain>
# Safe to re-run after every upload: it rebuilds the backend and leaves the certificate,
# AdGuard settings and the backend database alone.
set -euo pipefail
DOMAIN="${1:?usage: cloud-up.sh <domain e.g. quietnet.in>}"
DNS_HOST="dns.$DOMAIN"
API_HOST="api.$DOMAIN"
cd "$(dirname "$0")"
COMPOSE=(sudo docker compose -f docker-compose.cloud.yml)
CONF=agd-conf/AdGuardHome.yaml

[ -f backend.env ] || { echo "Missing backend.env: run  bash cloud-secrets.sh $DOMAIN  first." >&2; exit 1; }
get() { grep -m1 "^$1=" backend.env | cut -d= -f2-; }
# docker compose reads .env for ${DNS_HOST}/${API_HOST}, so later manual "docker compose" calls work too
printf 'DNS_HOST=%s\nAPI_HOST=%s\n' "$DNS_HOST" "$API_HOST" > .env

echo "==> Certificate for $DNS_HOST and *.$DNS_HOST"
if sudo test -f "/etc/letsencrypt/live/$DNS_HOST/fullchain.pem"; then
  echo "   already issued (renewals are automatic)"
else
  bash issue-cert.sh "$DNS_HOST"
fi

echo "==> AdGuard"
"${COMPOSE[@]}" up -d adguardhome
for _ in $(seq 30); do curl -s -o /dev/null http://127.0.0.1:3000/ && break; sleep 1; done
# --no-https: Caddy owns :443 and passes only /dns-query* to AdGuard, so the admin page is never public
ADGUARD_PASS="$(get ADGUARD_PASS)" python3 adguard_init.py --no-https --dns-host "$DNS_HOST" --user "$(get ADGUARD_USER)"

# Settings the admin API doesn't expose: plain-HTTP DoH (only Caddy can reach it) and trusting
# Caddy's X-Forwarded-For, so each device's real address shows up in the query log.
needs_patch=$(sudo python3 - "$CONF" <<'PY'
import sys, yaml
c = yaml.safe_load(open(sys.argv[1]))
tls, dns, http = c.get("tls") or {}, c.get("dns") or {}, c.get("http") or {}
# AdGuard v0.108+/v1 (config schema 30+) keeps the switch under http.doh; older versions under tls
doh_ok = http["doh"].get("insecure_enabled") is True if "doh" in http else tls.get("allow_unencrypted_doh") is True
# QuietNet's block list is a local file AdGuard may only read from a safe path (v0.107.47+)
fs = (c.get("filtering") or {}).get("safe_fs_patterns")
fs_ok = fs is None or "/opt/adguardhome/work/userfilters/*" in fs
ok = tls.get("port_https") == 0 and doh_ok and fs_ok and "172.16.0.0/12" in (dns.get("trusted_proxies") or [])
print("no" if ok else "yes")
PY
)
if [ "$needs_patch" = yes ]; then
  echo "   enabling DoH behind Caddy (restarting AdGuard once)"
  sudo docker stop pahra-dns-gateway >/dev/null
  sudo cp "$CONF" "$CONF.bak"
  sudo python3 - "$CONF" <<'PY'
import sys, yaml
path = sys.argv[1]
c = yaml.safe_load(open(path))
c.setdefault("tls", {})["port_https"] = 0
if "doh" in (c.get("http") or {}):
    c["http"]["doh"]["insecure_enabled"] = True  # AdGuard v0.108+/v1
else:
    c["tls"]["allow_unencrypted_doh"] = True  # older AdGuard
proxies = c.setdefault("dns", {}).setdefault("trusted_proxies", ["127.0.0.0/8", "::1"])
if "172.16.0.0/12" not in proxies:
    proxies.append("172.16.0.0/12")  # Docker's bridge networks
fs = (c.get("filtering") or {}).get("safe_fs_patterns")
if fs is not None and "/opt/adguardhome/work/userfilters/*" not in fs:
    fs.append("/opt/adguardhome/work/userfilters/*")  # QuietNet's block list file
yaml.safe_dump(c, open(path, "w"), sort_keys=False)
PY
  sudo docker start pahra-dns-gateway >/dev/null
fi

# The backend (uid 10001) writes QuietNet's block list here; AdGuard loads it from work/userfilters/
sudo mkdir -p agd-work/userfilters
sudo chown 10001 agd-work/userfilters

echo "==> Backend + Caddy (building the backend image)"
"${COMPOSE[@]}" up -d --build --remove-orphans
sudo docker image prune -f >/dev/null || true
# Caddy sees Caddyfile.cloud through a single-file bind mount, which keeps pointing at the old file
# once an upload replaces it: restart Caddy whenever the file changed.
sum=$(sha256sum Caddyfile.cloud | cut -d' ' -f1)
if [ "$(cat .caddyfile.sha 2>/dev/null)" != "$sum" ]; then
  sudo docker restart pahra-doh-proxy >/dev/null && echo "   Caddy restarted for the new Caddyfile"
  echo "$sum" > .caddyfile.sha
fi

echo "==> Checks"
TOKEN="$(get API_TOKEN)"
check() {  # name, seconds, command...
  local name=$1 secs=$2; shift 2
  for _ in $(seq "$secs"); do
    if out=$("$@" 2>/dev/null); then echo "   ok    $name"; return 0; fi
    sleep 1
  done
  echo "   FAIL  $name"; return 1
}
fail=0
check "backend answers on 127.0.0.1:8000" 90 \
  curl -fsS -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/api/health || fail=1
echo "$out" | python3 -c 'import json,sys; h=json.load(sys.stdin); b=h.get("blocklist") or {}; print("         adguard:", h["adguard"], "| llm:", h["llm"]["enabled"], "| block list:", b.get("active"), b.get("blocked_in_adguard"), b.get("error") or "")' 2>/dev/null || true
check "https://$API_HOST (Caddy's own certificate)" 120 \
  curl -fsS --resolve "$API_HOST:443:127.0.0.1" -H "Authorization: Bearer $TOKEN" "https://$API_HOST/api/health" || fail=1
check "https://$DNS_HOST/ hides the AdGuard login (404)" 20 \
  sh -c "[ \"\$(curl -s -o /dev/null -w '%{http_code}' --resolve $DNS_HOST:443:127.0.0.1 https://$DNS_HOST/)\" = 404 ]" || fail=1
# A DNS query for example.com (RFC 8484 GET). HTTP 200 = AdGuard answered; it REFUSES unknown devices, which is fine here.
DOH_Q="q80BAAABAAAAAAAAB2V4YW1wbGUDY29tAAABAAE"
check "DoH reaches AdGuard (https://$DNS_HOST/dns-query/<id>)" 20 \
  sh -c "[ \"\$(curl -s -o /dev/null -w '%{http_code}' -H 'accept: application/dns-message' --resolve $DNS_HOST:443:127.0.0.1 'https://$DNS_HOST/dns-query/cloud-up-check?dns=$DOH_Q')\" = 200 ]" || fail=1

if [ "$fail" = 1 ]; then
  echo
  echo "Something failed. Logs:  sudo docker compose -f ~/quietnet/infra/docker-compose.cloud.yml logs --tail 50"
  echo "If only the $API_HOST check failed: is there an A record for $API_HOST pointing at this VM?"
  exit 1
fi
cat <<EOF

QuietNet is running on this VM.
  API for the dashboard:  https://$API_HOST
  DoH for devices:        https://$DNS_HOST/dns-query/<device-id>
  DoT for devices:        <device-id>.$DNS_HOST:853
Dashboard access token (paste it into the dashboard once per browser):
  grep API_TOKEN ~/quietnet/infra/backend.env
EOF
