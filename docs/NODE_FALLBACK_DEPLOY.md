# Deploy plan: wallet, faucet and explorer fall back to node 2

**Status: NOT deployed. Needs the founder's yes first.**

## Does fallback give the founder server extra say over the chain? No.
In plain words: fallback only changes **which node the website reads from and sends to**.
It does not change how any node decides which chain is the real one.
- `node_pool.py` is used only by `webwallet.py`, `faucet.py` and `explorer.py`. No node code
  (`core.py`, `node.py`, `p2p_http.py`) imports it, so block checks, difficulty, the
  most-work rule and syncing are exactly the same with or without it.
- It only forwards reads (`/status`, `/balance`, ...) and already-signed transactions
  (`/submit_tx`, `/receive_tx`). Nodes check every transaction themselves. It never sends blocks,
  never calls `/mine`, `/sync` or `/add_peer`, and sends no admin token.
- The list of nodes only affects where the website looks. Node A is tried first because it is
  local, not because it ranks higher: every node, the founder's included, follows the chain
  with the most work that passes the rules.
- A clear "no" from a node (4xx) is passed back, never retried elsewhere, so a node can't be
  used to get around a rejection. The wallet also refuses a node on a different chain_id.
- The faucet key stays on testnet-1; node 2 only ever sees signed transactions.
Nothing needed fixing.

## What changes
- New file `node_pool.py`: try node A (127.0.0.1:8000) first; if it is down, times out, or answers 5xx,
  use node 2 (http://157.230.63.97:8000). A clear "no" (4xx, e.g. a rejected transaction) is passed back, not retried.
  A failed node rests 30 s, then is tried first again.
- `webwallet.py`, `faucet.py`, `explorer.py`: use the pool for all node reads and for broadcasts
  (`/receive_tx`, `/submit_tx`), which go to whichever node answers. New flag `--fallback-node`
  (env `NETLITHIC_FALLBACK_NODES`, default node 2; pass `''` to switch off).
- No consensus or key changes. Faucet key stays on testnet-1 only; node 2 never sees it (it only gets signed transactions).

## Steps on testnet-1 (137.184.199.238)
1. Backup: `mkdir -p /var/backups/netlithic/fallback-$(date +%Y%m%d-%H%M%S)` and copy
   `/opt/netlithic/{webwallet.py,faucet.py,explorer.py}` into it.
2. Copy in `node_pool.py`, `webwallet.py`, `faucet.py`, `explorer.py` to /opt/netlithic (owner/permissions as the existing files).
3. Check: `/opt/netlithic-venv/bin/python -c "import node_pool, webwallet, faucet, explorer"`.
4. Make sure the server can reach node 2: `curl -s --max-time 5 http://157.230.63.97:8000/status`.
5. Find the real unit names first (read-only): `systemctl list-units --all 'netlithic*'`.
   Known from earlier deploys: `netlithic-faucet.service`, `netlithic-explorer.service`,
   `netlithic-healthcheck.timer`; the web wallet unit is likely `netlithic-webwallet.service`
   (confirm with the list). Then restart only those three:
   `systemctl restart netlithic-webwallet netlithic-faucet netlithic-explorer`.
   (Live unit names were not re-checked from the box for this plan; SSH stayed off.)
6. Verify: wallet page loads, `/explorer/` loads, faucet status OK; then a short test: stop node A briefly
   (only with the founder's OK), confirm wallet/explorer still show heights from node 2, start node A again.

## Rollback
Copy the three backed-up files back, delete `/opt/netlithic/node_pool.py`, restart the same three services.

## Notes
- While on node 2, balances may lag node A by a block or so; mining still only happens on testnet-1.
- Faucet sends via node 2 rely on node 2 relaying to node A; nonces come from whichever node answered.

- Multisig: unchanged. It stays off and the treasury and reserve stay locked (founder's choice).
