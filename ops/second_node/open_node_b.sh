#!/usr/bin/env bash
# OPTION A (free): make node B on the same droplet a second public node.
# NOT RUN. Review, then run as root on netlithic-testnet-1 only after Aric approves.
# Gives a second endpoint (survives node A crashing or a bad restart) but NOT a
# second machine: a droplet outage takes both down.
set -euo pipefail
UNIT=${UNIT:-netlithic-nodeB.service}       # check with: systemctl list-units 'netlithic*'
PUBLIC_IP=${PUBLIC_IP:-137.184.199.238}
mkdir -p /etc/systemd/system/$UNIT.d
# Replace ExecStart so node B listens publicly and advertises its public URL.
# Copy the CURRENT ExecStart from `systemctl cat $UNIT` and change only --host and --advertise-url.
CURRENT=$(systemctl show -p ExecStart --value "$UNIT" | sed -n 's/.*argv\[\]=\([^;]*\);.*/\1/p')
echo "current: $CURRENT"
NEW=$(echo "$CURRENT" | sed -E 's/--host [^ ]+/--host 0.0.0.0/; s/ --advertise-url [^ ]+//')
NEW="$NEW --advertise-url http://$PUBLIC_IP:8001"
cat > /etc/systemd/system/$UNIT.d/public.conf <<CONF
[Service]
ExecStart=
ExecStart=$NEW
CONF
systemctl daemon-reload
ufw allow 8001/tcp
systemctl restart "$UNIT"
sleep 3
curl -fsS "http://$PUBLIC_IP:8001/status" | head -c 300; echo
# Rollback: rm /etc/systemd/system/$UNIT.d/public.conf; systemctl daemon-reload;
#           systemctl restart $UNIT; ufw delete allow 8001/tcp
