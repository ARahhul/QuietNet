#!/usr/bin/env bash
# Issue the wildcard certificate needed for DoT ClientIDs (*.dns.<domain>) via DNS-01.
#   bash issue-cert.sh dns.pahra.app you@example.com
# Needs ~/.secrets/cloudflare.ini containing:  dns_cloudflare_api_token = <token with Zone:DNS:Edit>
set -euo pipefail
DNS_HOST="${1:?usage: issue-cert.sh <dns-hostname e.g. dns.pahra.app> <email>}"
EMAIL="${2:?usage: issue-cert.sh <dns-hostname> <email>}"
CREDS="$HOME/.secrets/cloudflare.ini"

if [ ! -f "$CREDS" ]; then
  echo "Missing $CREDS. Create it with: dns_cloudflare_api_token = <token>" >&2
  exit 1
fi
chmod 600 "$CREDS"

sudo certbot certonly \
  --dns-cloudflare \
  --dns-cloudflare-credentials "$CREDS" \
  --dns-cloudflare-propagation-seconds 30 \
  -d "$DNS_HOST" -d "*.$DNS_HOST" \
  --agree-tos -m "$EMAIL" --non-interactive \
  --deploy-hook "docker restart pahra-dns-gateway || true"

# AdGuard runs as root in the container but the private key must be readable through the bind mount.
sudo chmod 0755 /etc/letsencrypt/{live,archive}
echo "Certificate ready: /etc/letsencrypt/live/$DNS_HOST/{fullchain,privkey}.pem"
echo "Renewal is automatic (certbot timer); the deploy hook restarts AdGuard."
