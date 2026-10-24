#!/usr/bin/env bash
# PREP ONLY. Default-deny inbound; SSH + node port.
set -euo pipefail
NODE_PORT="${NODE_PORT:-8000}"
SSH_PORT="${SSH_PORT:-22}"
apt-get -y install ufw
ufw default deny incoming
ufw default allow outgoing
ufw limit "${SSH_PORT}/tcp"
ufw allow "${NODE_PORT}/tcp"
ufw --force enable
ufw status verbose
