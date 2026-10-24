#!/usr/bin/env bash
# PREP ONLY. OS updates and unattended security upgrades.
set -euo pipefail
apt-get update
apt-get -y upgrade
apt-get -y install unattended-upgrades
dpkg-reconfigure -f noninteractive unattended-upgrades
