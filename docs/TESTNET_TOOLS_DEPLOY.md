# Deploying the faucet and explorer (testnet server)

Not deployed yet. These are the planned steps for `netlithic-testnet-1`.

| Service | Listens on | Public? |
|---|---|---|
| node A / node B | :8000 / :8001 (existing) | node A only (8000) |
| gateway | :8080 (existing) | yes |
| explorer | 127.0.0.1:8081 -> open as :8082, or bind 0.0.0.0:8081 | yes, read-only |
| faucet | 0.0.0.0:8090 | yes, rate limited |

1. `git pull` the `testnet-tools` branch into the existing code folder (as the netlithic user).
2. **Faucet wallet**: `python netlithic.py wallet new faucet --wallets /etc/netlithic/faucet_wallet.json`,
   then `chmod 600`, owned by the netlithic user. Only the address is printed.
3. **Fund it**: point the server's miner at the faucet address for some blocks
   (10 test NLT each), or send test NLT from the miner's reward address. Check with
   `netlithic.py balance <faucet address>`. No founder keys are involved.
4. systemd units (same style as the node units: `User=netlithic`, `Restart=always`,
   `NoNewPrivileges=yes`, `ProtectSystem=strict`, `ReadWritePaths=` the data dir):
   - `netlithic-explorer.service`: `ExecStart=.../python explorer.py --node http://127.0.0.1:8000 --host 0.0.0.0 --port 8081`
   - `netlithic-faucet.service`: `Environment=NETLITHIC_FAUCET_KEY_FILE=/etc/netlithic/faucet_wallet.json`,
     `ExecStart=.../python faucet.py --node http://127.0.0.1:8000 --host 0.0.0.0 --port 8090 --state-file /var/lib/netlithic/faucet_state.json`
     (add `--trust-proxy` only if a reverse proxy you control sits in front)
5. Firewall: `ufw allow 8081/tcp` and `ufw allow 8090/tcp`. Nothing else changes.
6. Check: `curl :8081/health`, `curl :8090/api/info`, then drip once to a test address.
7. Later: put both behind HTTPS (Caddy) on 443 and close 8081/8090.
