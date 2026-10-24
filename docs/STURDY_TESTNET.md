# Sturdier testnet: difficulty fork, second node, health alerts, deploy runbook

Status: built and tested on branch `sturdy-testnet`. **Nothing deployed.**

## 1. Automatic difficulty (LWMA fork)

| | Before block 5000 (unchanged) | From block 5000 |
|---|---|---|
| `difficulty` field means | leading hex zeros | work = expected hashes per block |
| Valid proof of work | hash starts with that many `0`s | `int(hash,16) * work < 2^256` |
| Retarget | every 10 blocks, +/-1 zero (16x steps) | every block, LWMA over the last 45 blocks |
| Target block time | 15 s (code constant) | **60 s** |
| Timestamp rule | after previous block, <= now + 120 s | after median of last 11 blocks, <= now + 120 s |

- Fork height `LWMA_FORK_HEIGHT = 5000` (core.py). Live height was 48 on 2026-10-21. Old blocks are
  validated by the old rule, so the live chain stays valid (test replays a real copy of it:
  `tests/data/live_chain.json`). The first post-fork block keeps the same work as before (16^2 = 256).
- Clamps: work moves at most x1.5 up / x2/3 down per block, never below 256. Each solve time counts
  as at most 6 targets, and timestamps are read monotonically, so one lying timestamp shifts work by
  only a few percent and the next honest block undoes it.
- Stall rule: if a block comes 10 minutes (10 targets) or more after the previous one, it may use
  1/8 of the normal work. A fast miner that leaves can't freeze the chain; the 10-minute server
  timer always gets a block through.
- Chain work for fork choice is now counted in expected hashes (16^zeros for old blocks). Every
  node computes it the same way, so this is consistent; it is not part of any block hash.
- **Genesis / economics_sha256 is not affected**: the new constants live in code, not
  `config/genesis_config.json`; a test checks the hash still starts `de4df9b9`.
- Nodes, the server miner and desktop clients all use `core.py`, so they agree as long as they run
  this version **before block 5000**. Old nodes/apps will reject post-fork blocks and stop following
  the chain at 5000 (they need the update; nothing else breaks).

### Target time: recommend 60 s
- 15 s on a tiny network over plain HTTP gossip means frequent forks/orphans (block relay plus
  validation can take seconds), lots of empty blocks, and a fast chain-file growth.
- 60 s gives a few desktop miners a fair chance, fewer stale blocks, and the desktop app's 60 s
  pace already matches it. Cost: confirmations take ~1 minute.
- Trade-off to know: `blocks_per_month = 175200` in genesis assumes 15 s blocks. At 60 s a
  "month" of unlocks/epochs takes ~4 x 30.4 = ~122 days. Today the chain actually runs ~1 block per
  1-10 min, so it's already slower than 15 s. Changing blocks_per_month would change the genesis
  hash (a new chain), so leave it for the testnet and set it to match the target for mainnet.
- To use 15 s instead, change `LWMA_TARGET_BLOCK_TIME` (and `STALL_SECONDS` follows) before deploy.

## 2. Second public node (plan, scripts not run)
- **Option A, free**: open node B (:8001) publicly: `ops/second_node/open_node_b.sh`. Protects
  against node A crashing, not against the droplet going down.
- **Option B, recommended, $6/month** (DigitalOcean Basic 1 vCPU / 1 GB / 25 GB): own droplet,
  `ops/second_node/new_droplet.sh`. No wallets or keys on it, no mining, just relays and serves.
  ($12/month 2 GB only if the chain file gets large.)
- Clients: the desktop app now takes a seed list (`DEFAULT_SEEDS` in `desktop/netlithic_desktop.py`,
  or `NETLITHIC_SEED_URLS=a,b`, or `--seed` repeated). Its local node peers with every seed; for
  "network height" it asks the last seed that answered and falls back to the others in order, and
  takes the highest. After node 2 is live, add its URL to `DEFAULT_SEEDS` and rebuild the app.
- Nodes already accept several `--peer` flags; add node 2 to node A's peers and vice versa.
- Web wallet / faucet / explorer stay on node A locally (same machine). Follow-up idea: let
  `--node` take a fallback list pointing at node B.

## 3. Health alerts (disabled by default)
`ops/healthcheck.py` (read-only GET checks) + `ops/systemd/netlithic-healthcheck.{service,timer}`
(every 5 min). Checks: nodes answer, height advancing (alert after 30 min stuck), nodes within 3
blocks, faucet balance >= 200 NLT, HTTPS cert >= 14 days, disk >= 15% free. De-duplicated (repeat
every 6 h) with a "Recovered" message. Config: `ops/healthcheck.example.json` -> `/etc/netlithic/healthcheck.json`.

Channel proposal: a **webhook** (a private Discord/Slack channel webhook, or an ntfy.sh topic for phone
push). The server can't send from the bot's email inbox; email works only through an SMTP relay
account (fields provided). Turn on with `"enabled": true` plus `webhook_url`.

Draft alert text: `Netlithic testnet:\n- Node http://127.0.0.1:8000 has been stuck at block 812 for 34 minutes.`

## 4. Deploy runbook (netlithic-testnet-1), only after approval
Fill in real unit names/paths from step 0. Times: ~20 min. Must finish well before height 5000
(if height > 4500 at deploy time, raise `LWMA_FORK_HEIGHT` first, re-test, re-commit).

0. Inspect: `systemctl list-units 'netlithic*' --all`, `systemctl cat <node A unit> <node B unit> <miner timer/service>`;
   note code dir, data dirs, venv. `curl -s :8000/status | jq .height` (< 4500).
1. Pause mining: `systemctl stop <miner>.timer`.
2. Backup: `cd <code dir> && git rev-parse HEAD > /root/netlithic-prev-commit` and
   `ops/backup_chain.sh <dataA> <dataB>` (prints the backup folder; chain/mempool/peers only, no keys).
3. Code: `git fetch && git checkout sturdy-testnet && git log -1` (expect the reported commit), then
   `<venv>/bin/python -m pytest -q` (all pass).
4. Restart: `systemctl restart <node B>` then `<node A>`; check both: `curl -s :8000/status`, `:8001/status`
   same height and hash; `journalctl -u <node A> -n 50` shows no "Rejected chain".
5. Resume mining: `systemctl start <miner>.timer`; after 10 min height +1 on both nodes.
6. Health check: `cp ops/healthcheck.example.json /etc/netlithic/healthcheck.json; chmod 600`, install the
   two units into /etc/systemd/system, `systemctl daemon-reload && systemctl enable --now netlithic-healthcheck.timer`;
   `journalctl -u netlithic-healthcheck -n 20` shows "healthy". (Alerts still off.)
7. Web wallet/faucet/explorer: `systemctl restart` them (they import core.py); load the site once.
8. Rebuild and release the desktop app from this branch before block 5000.

Rollback (any step): `systemctl stop <miner>.timer <node A> <node B>`;
`git checkout $(cat /root/netlithic-prev-commit)`; only if chain files were damaged copy them back from
the backup folder; start nodes, check `/status`, start the timer. Before block 5000 the old code accepts
every block the new code made, so rollback is clean. After 5000 rollback means losing post-fork blocks
(restore the backup) — don't roll back past the fork without deciding that explicitly.
