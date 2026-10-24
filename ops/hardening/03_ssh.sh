#!/usr/bin/env bash
# PREP ONLY. Keys-only SSH. Make sure your key works BEFORE running.
set -euo pipefail
test -s /root/.ssh/authorized_keys || ls /home/*/.ssh/authorized_keys >/dev/null || { echo "No authorized_keys found; aborting"; exit 1; }
cat > /etc/ssh/sshd_config.d/90-netlithic.conf <<'CONF'
PermitRootLogin prohibit-password
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
MaxAuthTries 3
X11Forwarding no
AllowTcpForwarding no
CONF
sshd -t
systemctl reload ssh || systemctl reload sshd
echo "Open a NEW ssh session to confirm before closing this one."
