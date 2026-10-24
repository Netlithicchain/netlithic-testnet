# Server hardening checklist (PREP ONLY, not yet run on any server)

These scripts are reviewed but have **not** been executed anywhere. Read every
script before running it. Run them on a fresh Ubuntu 22.04/24.04 host as root,
in this order, and keep a second SSH session open in case you lock yourself out.

| # | Step | Script | Check |
|---|---|---|---|
| 1 | OS updates + unattended security upgrades | `01_updates.sh` | `unattended-upgrades --dry-run` |
| 2 | Dedicated `netlithic` user, no shell login, owns /opt/netlithic + /var/lib/netlithic | `02_user.sh` | `id netlithic` |
| 3 | SSH: keys only, no root login, no passwords | `03_ssh.sh` | `sshd -t` then reload; test a new session |
| 4 | Firewall: deny in; allow SSH + node port (8000) only | `04_firewall.sh` | `ufw status verbose` |
| 5 | fail2ban for sshd | `05_fail2ban.sh` | `fail2ban-client status sshd` |
| 6 | systemd unit with sandboxing + env file 0640 root:netlithic for secrets | `06_node_service.sh` + `netlithic-node.service` | `systemd-analyze security netlithic-node` |
| 7 | Kernel/network sysctls (syncookies, no redirects) | `07_sysctl.sh` | `sysctl net.ipv4.tcp_syncookies` |
| 8 | Backups of chain.json (existing `ops/backup_chain.sh`) via cron, off-host copy | manual | restore test |
| 9 | Health check timer (existing `ops/systemd/netlithic-healthcheck.*`) | manual | `systemctl list-timers` |

## Secrets rule
* `NETLITHIC_ADMIN_TOKEN`, `DISCORD_BOT_TOKEN`, and `NETLITHIC_ALERT_WEBHOOK` live only in
  `/etc/netlithic/node.env` (root:netlithic, mode 0640). They never go in git, unit files, or logs.
* Wallet files (`wallets.json`, `genesis_wallets.json`, `faucet_wallet.json`) and the
  pause key never go on a public node. Faucet keys only on the faucet host, mode 0600.
* The pause private key never touches any server.

## Node settings worth setting in node.env
```
NETLITHIC_RATE_PER_SECOND=5
NETLITHIC_RATE_BURST=40
NETLITHIC_MAX_MEMPOOL=5000
```
Put a reverse proxy (Caddy/nginx) in front if exposing over HTTPS; then the node
sees the proxy IP, so rate-limit at the proxy as well.

## Before height 5000
All nodes upgraded to the security-fork release (check the deployed git commit on each host).
