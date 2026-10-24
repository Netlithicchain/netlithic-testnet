# Upgrade runbook: live testnet -> 0.5.0

Rehearsed end to end on an isolated replica on 2026-10-23 (MT): two network namespaces with the
real server IPs, a read-only copy of the live chain (height 455), the same unit layout, and
throwaway test wallets only. Every step below was executed there, including both rollbacks.

Order: **founder (137.184.199.238) first, then node 2 (157.230.63.97)**. Do both well before
block 4900 (the 0.5.0 upgrade deadline; after it, 0.5.0 nodes refuse peers older than 0.5.0).
Old and new versions sync fine with each other before 4900 (rehearsed in both directions,
including a block carrying a wallet transfer).

Never print or copy `/etc/netlithic/*` (admin token, faucet wallet). Nothing here touches wallets.

## 0. Prepare (on your laptop)
```bash
git fetch origin && git checkout release-0.5.0   # the 0.5.0 commit, after it is approved and pushed
git archive --format=tar.gz -o netlithic-0.5.0.tgz HEAD
sha256sum netlithic-0.5.0.tgz                    # note it
scp netlithic-0.5.0.tgz root@137.184.199.238:/root/
scp netlithic-0.5.0.tgz root@157.230.63.97:/root/
```
Record the current height: `curl -s http://137.184.199.238:8000/status | python3 -m json.tool | grep -E '"height"|tip'`

## 1. Founder server (137.184.199.238)

### 1a. Find exact unit names and paths (they are not all in the repo)
```bash
systemctl list-units --all 'netlithic*'
systemctl cat <node-A-unit> <node-B-unit> | grep -E 'ExecStart|WorkingDirectory|User'
```
Write down: node A unit (port 8000), node B unit (port 8001), the `--data-dir` of each,
explorer (`netlithic-explorer`), web wallet, faucet (`netlithic-faucet`), healthcheck/miner timers.
Below: `NODEA`, `NODEB`, `DATA_A`, `DATA_B` are those values; code is `/opt/netlithic`.

### 1b. Fix node B's peer flag (required)
0.5.0 refuses loopback/private peers. Node B's `--peer http://127.0.0.1:8000` is silently
skipped (only a "Skipping peer" line on stdout). It only kept syncing in the rehearsal because its
`peers.json` already listed public peers; a fresh node B would have no peers. Change it:
```bash
systemctl edit --full NODEB     # ExecStart: --peer http://137.184.199.238:8000
```
Do NOT set `NETLITHIC_ALLOW_PRIVATE_PEERS` on public nodes. (Rehearsed: a node B with an empty
data dir and the public peer synced from genesis, through the block-400 checkpoint, to the tip.)

### 1c. Stop (timers first so nothing restarts or mines mid-swap)
```bash
systemctl stop netlithic-healthcheck.timer <miner-timer>
systemctl stop netlithic-explorer <webwallet-unit> NODEB NODEA
systemctl is-active NODEA NODEB netlithic-explorer <webwallet-unit>   # all inactive
pgrep -af 'node.py|explorer.py|webwallet.py'                          # nothing
```
The faucet can stay running (it only talks HTTP to the node); leave its wallet file alone.

### 1d. Back up (code + data + checksum) BEFORE swapping
```bash
TS=$(date +%Y%m%d-%H%M); mkdir -p /root/backup-0.5.0
tar -C /opt -czf /root/backup-0.5.0/netlithic-code-$TS.tgz netlithic
tar -czf /root/backup-0.5.0/netlithic-data-$TS.tgz DATA_A DATA_B
sha256sum DATA_A/chain.json DATA_B/chain.json | tee /root/backup-0.5.0/chain-sha-$TS.txt
ls -lh /root/backup-0.5.0
```
Check the chain checksum now, while nodes are stopped; once they run, chain.json changes.

### 1e. Swap code, keep data
```bash
mv /opt/netlithic /opt/netlithic.prev
mkdir /opt/netlithic && tar -C /opt/netlithic -xzf /root/netlithic-0.5.0.tgz
chown -R --reference=/opt/netlithic.prev /opt/netlithic
cp -a /opt/netlithic.prev/config/. /opt/netlithic/config/ 2>/dev/null || true  # keep any local config
/opt/netlithic-venv/bin/pip install -r /opt/netlithic/requirements.txt
/opt/netlithic-venv/bin/python -c "import sys;sys.path.insert(0,'/opt/netlithic');import netlithic_version as v;print(v.VERSION)"   # 0.5.0
```
Data dirs are not touched (they live outside the code dir).

### 1f. Start and verify
```bash
systemctl start NODEA; sleep 10
curl -s http://127.0.0.1:8000/status | python3 -m json.tool | grep -E 'version|fork_ready|min_peer_version|upgrade_deadline_height|"height"'
# expect version 0.5.0, fork_ready true, min_peer_version 0.5.0, upgrade_deadline_height 4900,
# height >= the height you recorded, same tip as node 2
curl -s http://157.230.63.97:8000/status | python3 -m json.tool | grep -E '"height"|tip'
systemctl start NODEB netlithic-explorer <webwallet-unit>; sleep 15
curl -s http://127.0.0.1:8001/status | python3 -m json.tool | grep -E 'version|"height"'
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8081/   # 200, footer "Netlithic Explorer 0.5.0"
curl -s http://127.0.0.1:8082/api/info    # web wallet port per its unit; "version": "0.5.0"
journalctl -u NODEA -u NODEB --since -5min | grep -iE 'error|skipping peer|checkpoint'
systemctl start netlithic-healthcheck.timer <miner-timer>
```
Wait for one new block (old node 2 must accept it, and vice versa) before going on.
The block-400 checkpoint is validated when the node loads the chain; a mismatch would stop it
from starting/syncing, so a healthy start at the old height is the pass signal.

## 2. Node 2 (157.230.63.97, netlithic-testnet-2)
Relay only: one unit, `netlithic-node` (no wallets, no mining, no explorer/wallet, no timers).
Code `/opt/netlithic`, venv `/opt/netlithic-venv`, data `/var/lib/netlithic/node`, user `netlithic`.
There is no `/etc/netlithic/node.env`. (Verified on the live server during the 2026-10-23 upgrade.)
```bash
systemctl list-units --all 'netlithic*'          # expect only netlithic-node.service
systemctl cat netlithic-node | grep ExecStart     # --peer http://137.184.199.238:8000 (public)
systemctl stop netlithic-node; systemctl is-active netlithic-node; pgrep -af 'netlithic.py node'
TS=$(date +%Y%m%d-%H%M); B=/root/backup-0.5.0; mkdir -p $B; chmod 700 $B
tar -C /opt -czf $B/netlithic-code-$TS.tgz netlithic
tar -C /var/lib/netlithic -czf $B/netlithic-data-$TS.tgz node
cp -a /etc/systemd/system/netlithic-node.service $B/netlithic-node.service-$TS
sha256sum /var/lib/netlithic/node/chain.json | tee $B/chain-sha-$TS.txt
sha256sum $B/*-$TS.tgz | tee $B/tarballs-sha-$TS.txt
sha256sum -c $B/chain-sha-$TS.txt                 # while stopped
mv /opt/netlithic /opt/netlithic.prev
mkdir /opt/netlithic && tar -C /opt/netlithic -xzf /root/netlithic-0.5.0.tgz
chown -R --reference=/opt/netlithic.prev /opt/netlithic
/opt/netlithic-venv/bin/pip install -r /opt/netlithic/requirements.txt
systemctl start netlithic-node; sleep 15
curl -s http://127.0.0.1:8000/status | python3 -m json.tool | grep -E 'version|fork_ready|"height"|latest_hash|peers'
curl -s http://137.184.199.238:8000/status | python3 -m json.tool | grep -E 'version|"height"|latest_hash'
```
Its unit and `peers.json` already use only the founder's public address; no flag change is needed.
Never set `NETLITHIC_ALLOW_PRIVATE_PEERS`.

## 3. Final checks (both servers)
- All of founder :8000, :8001 and node 2 :8000 report `version 0.5.0`, the same height and tip.
- Mine or wait for one block; all three advance to the same new tip.
- Explorer home shows "ready for the block 5000 upgrade: 2 of 2 reachable".
- Web wallet loads, `/api/info` and an address lookup work. In rehearsal a throwaway in-memory
  wallet sent a signed transfer through `/api/send`; it was mined and the recipient balance updated.
- Stop the explorer's main node briefly only if you want to test fallback (rehearsed: still 200).

## 4. Rollback (per server; rehearsed on both)
Node 2:
```bash
systemctl stop netlithic-node
rm -rf /opt/netlithic && mv /opt/netlithic.prev /opt/netlithic
# only if data is suspected bad (otherwise keep it; old code reads the same chain):
#   rm -rf /var/lib/netlithic/node && tar -C /var/lib/netlithic -xzf /root/backup-0.5.0/netlithic-data-$TS.tgz
#   sha256sum -c /root/backup-0.5.0/chain-sha-$TS.txt
systemctl start netlithic-node   # catches up from the founder
```
Founder:
```bash
systemctl stop netlithic-healthcheck.timer <miner-timer> netlithic-explorer <webwallet-unit> NODEB NODEA
rm -rf /opt/netlithic && mv /opt/netlithic.prev /opt/netlithic
# optional data restore: tar -xzf /root/backup-0.5.0/netlithic-data-$TS.tgz -C /  (then sha256sum -c ... before start)
systemctl start NODEA NODEB netlithic-explorer <webwallet-unit> netlithic-healthcheck.timer <miner-timer>
```
Keep node B's public `--peer` flag on rollback; it works with the old code too.
Rollback is only safe before block 4900. In rehearsal, a rolled-back node restored to the backup
height re-synced the newer blocks from the upgraded peer, and vice versa.

## Not rehearsed
- Behaviour at block 4900/5000 (needs ~4,400 more blocks); covered by the unit tests.
- Exact live unit names and the web wallet port; read them in step 1a.
