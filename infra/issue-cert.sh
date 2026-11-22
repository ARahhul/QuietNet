#!/usr/bin/env bash
# Issue the wildcard certificate needed for DoT ClientIDs (*.dns.<domain>) via DNS-01.
#   bash issue-cert.sh dns.pahra.app you@example.com
# The DNS provider is chosen by which credentials file exists:
#   ~/.secrets/cloudflare.ini   dns_cloudflare_api_token = <token with Zone:DNS:Edit>
#   ~/.secrets/desec.ini        dns_desec_token = <deSEC token>   (deSEC gives free <name>.dedyn.io domains)
set -euo pipefail
DNS_HOST="${1:?usage: issue-cert.sh <dns-hostname e.g. dns.pahra.app> <email>}"
EMAIL="${2:?usage: issue-cert.sh <dns-hostname> <email>}"
SECRETS="$HOME/.secrets"

if [ -f "$SECRETS/cloudflare.ini" ]; then
  CREDS="$SECRETS/cloudflare.ini"
  CERTBOT=certbot
  AUTH=(--dns-cloudflare --dns-cloudflare-credentials "$CREDS" --dns-cloudflare-propagation-seconds 30)
elif [ -f "$SECRETS/desec.ini" ]; then
  CREDS="$SECRETS/desec.ini"
  # Ubuntu doesn't package the deSEC plugin, so certbot and the plugin get their own venv.
  VENV=/opt/certbot-desec
  if [ ! -x "$VENV/bin/certbot" ]; then
    echo "==> Installing certbot with the deSEC plugin in $VENV"
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv >/dev/null
    sudo python3 -m venv "$VENV"
    sudo "$VENV/bin/pip" install -q --upgrade pip
    sudo "$VENV/bin/pip" install -q certbot certbot-dns-desec
  fi
  # Renewals run from Ubuntu's certbot.timer, whose /usr/bin/certbot lacks the plugin: point it at the venv.
  sudo mkdir -p /etc/systemd/system/certbot.service.d
  printf '[Service]\nExecStart=\nExecStart=%s/bin/certbot -q renew --no-random-sleep-on-renew\n' "$VENV" \
    | sudo tee /etc/systemd/system/certbot.service.d/desec.conf >/dev/null
  sudo systemctl daemon-reload
  sudo systemctl enable --now certbot.timer >/dev/null 2>&1 || true
  CERTBOT="$VENV/bin/certbot"
  AUTH=(--authenticator dns-desec --dns-desec-credentials "$CREDS" --dns-desec-propagation-seconds 90)
else
  echo "Missing DNS provider credentials. Create one of:" >&2
  echo "  $SECRETS/cloudflare.ini   dns_cloudflare_api_token = <token>" >&2
  echo "  $SECRETS/desec.ini        dns_desec_token = <token>" >&2
  exit 1
fi
chmod 600 "$CREDS"

sudo "$CERTBOT" certonly \
  "${AUTH[@]}" \
  -d "$DNS_HOST" -d "*.$DNS_HOST" \
  --agree-tos -m "$EMAIL" --non-interactive \
  --deploy-hook "docker restart pahra-dns-gateway || true"

# AdGuard runs as root in the container but the private key must be readable through the bind mount.
sudo chmod 0755 /etc/letsencrypt/{live,archive}
echo "Certificate ready: /etc/letsencrypt/live/$DNS_HOST/{fullchain,privkey}.pem"
echo "Renewal is automatic (certbot timer); the deploy hook restarts AdGuard."
