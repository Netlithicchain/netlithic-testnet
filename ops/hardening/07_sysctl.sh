#!/usr/bin/env bash
# PREP ONLY. Basic network sysctls.
set -euo pipefail
cat > /etc/sysctl.d/90-netlithic.conf <<'CONF'
net.ipv4.tcp_syncookies = 1
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.all.send_redirects = 0
net.ipv4.conf.all.rp_filter = 1
net.ipv6.conf.all.accept_redirects = 0
kernel.kptr_restrict = 2
kernel.dmesg_restrict = 1
CONF
sysctl --system
