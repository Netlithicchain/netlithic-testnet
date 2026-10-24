#!/usr/bin/env bash
# OPTION B (recommended, ~$6/month): a second public node on its own droplet.
# NOT RUN. Run as root on a NEW Ubuntu 24.04 droplet (1 vCPU / 1 GB, $6/mo) after Aric approves.
# The node holds no wallets or keys and does not mine; it only relays and serves the chain.
set -euo pipefail
SEED=${SEED:-http://137.184.199.238:8000}
REPO=${REPO:?set REPO to the git clone URL (deploy key, read-only)}
BRANCH=${BRANCH:-sturdy-testnet}
PUBLIC_IP=${PUBLIC_IP:-$(curl -fsS https://api.ipify.org)}
apt-get update && apt-get install -y python3-venv git ufw
id netlithic >/dev/null 2>&1 || useradd --system --create-home --home-dir /opt/netlithic netlithic
sudo -u netlithic git clone --branch "$BRANCH" "$REPO" /opt/netlithic/code
sudo -u netlithic python3 -m venv /opt/netlithic/venv
sudo -u netlithic /opt/netlithic/venv/bin/pip install -r /opt/netlithic/code/requirements.txt
mkdir -p /var/lib/netlithic/node && chown -R netlithic: /var/lib/netlithic
cat > /etc/systemd/system/netlithic-node.service <<UNIT
[Unit]
Description=Netlithic public node 2
After=network-online.target

[Service]
User=netlithic
WorkingDirectory=/opt/netlithic/code
ExecStart=/opt/netlithic/venv/bin/python node.py 8000 --data-dir /var/lib/netlithic/node --host 0.0.0.0 --peer $SEED --advertise-url http://$PUBLIC_IP:8000
Restart=always
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/var/lib/netlithic

[Install]
WantedBy=multi-user.target
UNIT
ufw allow OpenSSH && ufw allow 8000/tcp && ufw --force enable
systemctl daemon-reload && systemctl enable --now netlithic-node
sleep 5 && curl -fsS http://127.0.0.1:8000/status | head -c 300; echo
echo "Then on netlithic-testnet-1: add --peer http://$PUBLIC_IP:8000 to node A (or POST /add_peer with the admin token)."
