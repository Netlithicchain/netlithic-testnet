#!/usr/bin/env bash
# PREP ONLY. Install the sandboxed node unit. Secrets go in /etc/netlithic/node.env
# (created empty here; fill it by hand, never commit it).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
install -m 0644 "$HERE/netlithic-node.service" /etc/systemd/system/netlithic-node.service
if [ ! -f /etc/netlithic/node.env ]; then
  install -o root -g netlithic -m 0640 /dev/null /etc/netlithic/node.env
  echo "Created empty /etc/netlithic/node.env; add NETLITHIC_ADMIN_TOKEN etc. by hand."
fi
systemctl daemon-reload
systemctl enable netlithic-node
systemd-analyze security netlithic-node || true
echo "Start with: systemctl start netlithic-node"
