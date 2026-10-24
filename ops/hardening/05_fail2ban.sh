#!/usr/bin/env bash
# PREP ONLY. fail2ban for sshd.
set -euo pipefail
apt-get -y install fail2ban
cat > /etc/fail2ban/jail.d/sshd.local <<'CONF'
[sshd]
enabled = true
maxretry = 5
bantime = 1h
CONF
systemctl enable --now fail2ban
fail2ban-client status sshd
