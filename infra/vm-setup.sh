#!/usr/bin/env bash
# One-time VM preparation (Ubuntu 22.04/24.04). Run as a sudo-capable user:
#   bash vm-setup.sh
set -euo pipefail

echo "==> Installing Docker, Compose and certbot (Cloudflare DNS plugin)"
sudo apt-get update -y
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io docker-compose-v2 certbot python3-certbot-dns-cloudflare
sudo usermod -aG docker "$USER" || true

echo "==> Freeing port 53 (systemd-resolved stub listener)"
if grep -q '^#\?DNSStubListener' /etc/systemd/resolved.conf; then
  sudo sed -i 's/^#\?DNSStubListener=.*/DNSStubListener=no/' /etc/systemd/resolved.conf
else
  echo 'DNSStubListener=no' | sudo tee -a /etc/systemd/resolved.conf >/dev/null
fi
sudo ln -sf /run/systemd/resolve/resolv.conf /etc/resolv.conf
sudo systemctl restart systemd-resolved

echo "==> Making HTTPS from containers reliable (TCP MSS clamp)"
bash "$(dirname "$0")/mss-clamp.sh"

echo "==> Opening firewall ports 22, 53, 443, 853 (tcp+udp)"
if command -v ufw >/dev/null && sudo ufw status | grep -q '^Status: active'; then
  for p in 53 443 853; do sudo ufw allow "$p/tcp"; sudo ufw allow "$p/udp"; done
  sudo ufw allow 22/tcp
fi
# Oracle Cloud images ship iptables REJECT rules: insert ACCEPTs before them.
if sudo iptables -S INPUT 2>/dev/null | grep -q REJECT; then
  # insert right before the first REJECT rule (its position differs between images)
  pos=$(sudo iptables -L INPUT --line-numbers -n | awk '$2=="REJECT"{print $1; exit}')
  for proto in tcp udp; do
    for p in 53 443 853; do
      sudo iptables -C INPUT -p "$proto" --dport "$p" -j ACCEPT 2>/dev/null || \
        sudo iptables -I INPUT "${pos:-1}" -p "$proto" --dport "$p" -j ACCEPT
    done
  done
  # non-interactive: otherwise debconf stops to ask "save current rules?"
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y iptables-persistent
  sudo netfilter-persistent save || true
fi

cat <<'EOF'

Done. Also open 53, 443 and 853 (TCP+UDP) in your cloud provider's firewall:
  - GCP:    VPC network -> Firewall -> allow tcp:53,443,853 udp:53,443,853
  - Oracle: VCN -> Security List -> Ingress rules for the same ports
  - Azure:  azure-vm.ps1 has already opened them in the network security group
Do NOT open 3000. The admin API stays on 127.0.0.1 and is reached through the SSH tunnel.

Next: bash issue-cert.sh <dns-hostname> <email>   then   sudo docker compose up -d
(Plain "docker" without sudo works after you log out and back in: you were just added to the docker group.)
EOF
