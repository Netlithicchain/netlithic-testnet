# Founder server mining after block 5000 (TEST NETWORK)

**Status: built on branch `founder-relay`, NOT deployed. Server timers are unchanged. Needs the founder's yes.**

## The rule in plain words
- Until block 5000 the founder server (netlithic-testnet-1) mines as it does today.
- From block 5000 it **stops mining and only relays** blocks and transactions.
- Backstop so the test network never freezes: if **nobody** has made a block for **30 minutes**,
  the server may make one block, and **never more than 1 block per 30 minutes**.
- The server mines to one public address: **`e924523b2614a39b9bb4ff8bfc05e747`** (founder/faucet).
  Anyone can look it up in the explorer and see how many blocks it made.
- Multisig: no change. It stays off and the treasury and reserve stay locked.

## Settings (env vars for `ops/founder_miner.py`)
| Setting | Default | Meaning |
|---|---|---|
| `NETLITHIC_FOUNDER_STOP_HEIGHT` | 5000 | block where the server stops normal mining |
| `NETLITHIC_FOUNDER_BACKSTOP` | 1 | 1 = backstop on, 0 = never mine after 5000 |
| `NETLITHIC_FOUNDER_IDLE_MINUTES` | 30 | no block from anyone for this long before the backstop may run |
| `NETLITHIC_FOUNDER_MIN_GAP_MINUTES` | 30 | at most one server block per this many minutes |
| `NETLITHIC_MINER_ADDRESS` | e924523b...e747 | public reward address |
| `NETLITHIC_FOUNDER_STATE` | /var/lib/netlithic/founder_miner.json | time of the server's last backstop block |

Because of the stall rule, a block made 10+ minutes after the last one may use 1/8 difficulty,
so a backstop block goes through quickly. The node also now reports `latest_timestamp` in
`/status` (information only, not a consensus change); the script falls back to `/chain` on old nodes.

## Deploy steps (only after the founder says yes, before block 5000)
1. Read-only look: `systemctl list-units --all 'netlithic*'` and `systemctl cat <miner timer> <miner service>`
   to find the current 10-minute miner unit and its exact `ExecStart`.
2. Backup: `mkdir -p /var/backups/netlithic/founder-relay-$(date +%Y%m%d-%H%M%S)`, copy the miner unit
   files and `/opt/netlithic/p2p_http.py` into it.
3. Copy `ops/founder_miner.py` and `p2p_http.py` into /opt/netlithic (same owner/permissions).
4. Dry run: `/opt/netlithic-venv/bin/python /opt/netlithic/ops/founder_miner.py --dry-run` -> prints the decision.
5. Change the miner service's `ExecStart` to
   `/opt/netlithic-venv/bin/python /opt/netlithic/ops/founder_miner.py` (keep its `EnvironmentFile`
   with the admin token; never print it). Keep the timer at every 10 minutes: before 5000 it mines as now,
   after 5000 it only checks.
6. `systemctl daemon-reload`, restart node A (for `latest_timestamp`), then watch one timer run in
   `journalctl -u <miner service> -n 5`.
7. Publish the address `e924523b2614a39b9bb4ff8bfc05e747` on the site (Marketing, founder's OK).

Rollback: put back the old unit files and `p2p_http.py` from the backup, `systemctl daemon-reload`,
restart node A and the timer.

## Note
With the server out, blocks after 5000 come only from community miners (the desktop app tries
every 60 s). If no one mines, the backstop keeps about 2 blocks per hour.
