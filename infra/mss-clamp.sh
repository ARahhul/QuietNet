#!/usr/bin/env bash
# Clamp the TCP MSS of forwarded (container) traffic, now and at every boot.
# Why: on Azure, HTTPS from Docker containers to Fastly-hosted sites (PyPI, raw.githubusercontent.com,
# where the blocklists live) stalls in the TLS handshake, while the host itself is fine. Large packets
# from those servers get lost behind Docker's NAT. A smaller MSS keeps every packet under the limit.
# Called by vm-setup.sh; safe to re-run.
set -euo pipefail
RULE="-p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss 1360"

sudo tee /etc/systemd/system/quietnet-mss-clamp.service >/dev/null <<EOF
[Unit]
Description=Clamp TCP MSS for container traffic (QuietNet)
After=network-pre.target
Before=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'iptables -t mangle -C FORWARD $RULE 2>/dev/null || iptables -t mangle -A FORWARD $RULE'

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now quietnet-mss-clamp.service >/dev/null
sudo systemctl restart quietnet-mss-clamp.service
echo "   TCP MSS for container traffic clamped to 1360 (quietnet-mss-clamp.service)"
