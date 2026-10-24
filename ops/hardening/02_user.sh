#!/usr/bin/env bash
# PREP ONLY. Unprivileged service user.
set -euo pipefail
id netlithic >/dev/null 2>&1 || useradd --system --home /opt/netlithic --shell /usr/sbin/nologin netlithic
install -d -o netlithic -g netlithic -m 0750 /opt/netlithic /var/lib/netlithic
install -d -o root -g netlithic -m 0750 /etc/netlithic
