# Upgrade runbook: live testnet 0.5.0 -> 0.5.1 -> **0.5.3** (adds the pause key; points v4 with the review fixes)

> **0.5.3 supersedes 0.5.1 for the build that goes out** (security review, finding M1). The build carries
> `VERSION = MIN_PEER_VERSION = 0.5.3`. Wherever this runbook says "0.5.1" for the version a server must report, read
> **0.5.3**. (The file name and the tarball/branch names below keep "0.5.1" so existing notes still line up; use the
> names the founder approves.) Section 7 lists what is new in 0.5.3.

> **Unsupported builds (review round 3, N3).** **Only 0.5.3 is supported.** Every build before 0.5.3 is unsupported: 0.5.0, 0.5.1
> and every 0.5.2 development build, **including `eb4e3d5` and `4080424`** (the `points-v4r-fixes` / `points-v4r-fixes2` branches).
> The registry rules changed after `eb4e3d5` (the rules fingerprint is different: `registry_rules=3`, `registry_max_valid=10080`), so such
> a node is refused by peers from block 4900 and would split at the first registry key rotation or the first registry transaction with a
> far-future `valid_until`. A `4080424` build has the same payout rules as 0.5.3 but reports the old version, so peers refuse it from
> block 4900 as well. **All nodes must upgrade together, to 0.5.3, before block 4900** (and in any case before 6440).

**Status: DRAFT. Nothing in this runbook has been run.** This runbook now also covers **points v4** (section 6): the
payout rules that switch on at block 5000. See [POINTS_V4.md](POINTS_V4.md). It is modelled on
[UPGRADE_RUNBOOK_0.5.0.md](UPGRADE_RUNBOOK_0.5.0.md) and on the 2026-10-23 F9/F10 deploy (backups in
`/root/backup-f9f10/`). Do not start until the founder says go in chat.

## What 0.5.1 is, and why it exists

0.5.1 = 0.5.0 + **the pause public key** (`pause.PAUSE_PUBKEY`) + `MIN_PEER_VERSION` raised to 0.5.1
+ (on the branch `points-v4-protection`) **points v4**: 20/80 pots, 2% per-address cap, one-day payout lag, all at block 5000.
A 0.5.1 build **with** and **without** points v4 would pay differently and fork, yet report the same version string.
**When:** a node on plain 0.5.1 rules (no v4) pays at epoch block **6440**; a v4 node pays nothing there (v4's first payout is 7880),
so the split happens at **6440**, not 7880 (an earlier draft said 7880, which was wrong). The rules fingerprint `points_rules` in `/status` tells them apart: it must be
**identical on every server** (section 6).

A 0.5.0 node has an empty pause key. From block 5000 a pause tx is valid consensus, but a node with an
empty key calls it invalid and **rejects the whole block that carries it**, so it forks off at the first
pause. That is why every node must run 0.5.1 *with the key* before the first pause is ever sent.

**Never deploy a 0.5.1 whose key slot is empty.** It reports the same version string as a keyed 0.5.1, but
it is consensus-identical to 0.5.0. Three guards: the release gate below, `/status` fields
`pause_enabled` / `pause_key_id`, and the healthcheck `expected_pause_key_id`.

Before block 5000 a pause tx is rejected by every node (0.5.0 and 0.5.1), so deploying the key cannot
pause anything by accident. Don't send a test pause to the live network after 5000 either: that would be a
real pause. Practise with the drill (section 0).

Order: **founder (137.184.199.238) first, then node 2 (157.230.63.97), within about an hour of each other**,
and **well before block 4900**. Never set `NETLITHIC_ALLOW_PRIVATE_PEERS` or `NETLITHIC_CHECKPOINT_RESYNC`.
Never print or copy `/etc/netlithic/*` (admin token, faucet wallet, webhook URL) or any wallet or key file.

### Timing against block 4900 (read this first)
`MIN_PEER_VERSION = 0.5.3` and `UPGRADE_DEADLINE_HEIGHT = 4900` mean (same table for a 0.5.1 peer):

| Height | A 0.5.0 peer that says hello to a 0.5.1 node |
|---|---|
| below 4900 | accepted, answered `upgrade_needed: true` plus a warning. Healthcheck shows "old peer". |
| 4900 and up | **refused** (HTTP 400, "Upgrade needed"). |
| 4900 and up, a peer whose hello has **no rules fingerprint** (`points_v3_rules` / `points_rules`) | **refused** (HTTP 400). Before 0.5.2 a missing field was let through (the gate failed open). |

The gate only judges **new** `/hello` handshakes. It does not drop a 0.5.0 peer that is already connected, and
it does not stop blocks pushed to `/receive_block`. So it narrows the exposure and warns people; it is not a
hard guard against the fork. The real protection is that everyone is on 0.5.1 before the first pause.

Consequences for our own two servers: if the founder runs 0.5.1 and node 2 is still 0.5.0 **after 4900**, the
founder refuses node 2's new handshakes. So deploy both servers together, and do not start this deploy if
the live height is within about 150 blocks of 4900 unless you can finish both servers in one sitting.
Check the height first: `curl -s http://137.184.199.238:8000/status | grep -E '"height"'`.

## 0. Prepare (on your laptop, nothing touches a server yet)

1. the founder's **public** key is in `pause.py` through the tool (never by hand), and the gate passes:
   ```bash
   git fetch origin && git checkout release-0.5.1     # after the PR with the key is approved and merged
   python ops/set_pause_pubkey.py --check             # must print "slot is set" and exit 0. Exit 1 = STOP.
   ```
   Read the key and key id back to the founder and compare with his paper copy, character by character. Write the
   **key id** down: it is the expected `pause_key_id` for every check below.
2. CI is green on the release commit. Run locally too:
   `pytest -q`, `bandit -r . -x ./tests,./desktop/build,./desktop/dist --severity-level medium --confidence-level medium`,
   `semgrep scan --config p/python --config p/secrets --metrics=off --error --exclude '*.json'` (as CI does).
3. Run the drill: `python ops/drill/pause_drill.py` must end with `PAUSE DRILL PASSED`.
4. the founder tags the release only after approving (`v0.5.1`). Build the tarball from the tag:
   ```bash
   git archive --format=tar.gz -o netlithic-0.5.1.tgz v0.5.1
   sha256sum netlithic-0.5.1.tgz                      # note it; compare on each server after copying
   scp netlithic-0.5.1.tgz root@137.184.199.238:/root/
   scp netlithic-0.5.1.tgz root@157.230.63.97:/root/
   ```
5. SSH only after checking the host key fingerprint: founder `SHA256:YQpgdd5ibMfcgzZzigWVcBnHqn/diXe/yVYFNd/oCq4`,
   node 2 `SHA256:HtPHmk30r+mjpXv6ktpCBtcbMoE75WJH5EkykDqqecg`.
6. Record the current height and tips of both servers (public `GET /status`).

## 1. Founder server (137.184.199.238)

### 1a. Unit names (use the real ones; they are not all in the repo)
```bash
systemctl list-units --all 'netlithic*'
systemctl cat netlithic-node-a netlithic-node-b | grep -E 'ExecStart|WorkingDirectory|User'
```
Known layout: `netlithic-node-a` (port 8000, data `/var/lib/netlithic/a`), `netlithic-node-b` (port 8001,
data `/var/lib/netlithic/b`), explorer (8081), web wallet (8095), faucet (8090), gateway
(`/opt/netlithic-gateway`), healthcheck and miner timers. Code `/opt/netlithic`, venv `/opt/netlithic-venv`.
Below `NODEA`/`NODEB` are the two node units, `DATA_A`/`DATA_B` their data dirs.
Confirm node B's `--peer` is the public `http://137.184.199.238:8000` (not 127.0.0.1).

### 1b. Stop (timers first so nothing restarts or mines mid-swap)
```bash
systemctl stop netlithic-healthcheck.timer <miner-timer>
systemctl stop netlithic-explorer <webwallet-unit> NODEB NODEA
systemctl is-active NODEA NODEB netlithic-explorer <webwallet-unit>   # all inactive
pgrep -af 'node.py|explorer.py|webwallet.py'                          # nothing
```
The faucet can stay running (it only talks HTTP to the node); leave its wallet file alone.

### 1c. Back up BEFORE swapping (new directory, so the F9/F10 and 0.5.0 backups stay untouched)
```bash
TS=$(date +%Y%m%d-%H%M); B=/root/backup-0.5.1; mkdir -p $B; chmod 700 $B
tar -C /opt -czf $B/netlithic-code-$TS.tgz netlithic
tar -czf $B/netlithic-data-$TS.tgz DATA_A DATA_B
cp -a /etc/systemd/system/netlithic-*.service $B/ 2>/dev/null || true     # unit files only, not /etc/netlithic
sha256sum DATA_A/chain.json DATA_B/chain.json | tee $B/chain-sha-$TS.txt
sha256sum $B/*-$TS.tgz | tee $B/tarballs-sha-$TS.txt
sha256sum /root/netlithic-0.5.1.tgz                                        # equals the checksum you noted
ls -lh $B
```
Do **not** include `/etc/netlithic` in any archive. Check the chain checksums now, while the nodes are stopped.

### 1d. Swap the code, keep the data
```bash
mv /opt/netlithic /opt/netlithic.prev
mkdir /opt/netlithic && tar -C /opt/netlithic -xzf /root/netlithic-0.5.1.tgz
chown -R --reference=/opt/netlithic.prev /opt/netlithic
cp -a /opt/netlithic.prev/config/. /opt/netlithic/config/ 2>/dev/null || true   # keep any local config (checkpoints)
/opt/netlithic-venv/bin/pip install -r /opt/netlithic/requirements.txt
cd /opt/netlithic && /opt/netlithic-venv/bin/python ops/set_pause_pubkey.py --check    # slot set, exit 0
/opt/netlithic-venv/bin/python -c "import sys;sys.path.insert(0,'/opt/netlithic');import netlithic_version as v,pause;print(v.VERSION, pause.key_id())"
# expect: 0.5.3 <the key id you wrote down>
```
If the gateway runs code from `/opt/netlithic-gateway` separately, check whether it imports `/opt/netlithic`;
restart it with the rest if it does.

### 1e. Start and verify
```bash
systemctl start NODEA; sleep 10
curl -s http://127.0.0.1:8000/status | python3 -m json.tool | \
  grep -E '"version"|fork_ready|min_peer_version|upgrade_deadline_height|pause_enabled|pause_key_id|checkpoints_ok|"height"'
# expect version 0.5.3, fork_ready true, min_peer_version 0.5.3, upgrade_deadline_height 4900,
# pause_enabled true, pause_key_id = your key id, checkpoints_ok true, height >= recorded
curl -s http://127.0.0.1:8000/pause_status | python3 -m json.tool | grep -E 'enabled|paused|nonce'   # enabled true, paused false, nonce 0
systemctl start NODEB netlithic-explorer <webwallet-unit>; sleep 15
curl -s http://127.0.0.1:8001/status | python3 -m json.tool | grep -E '"version"|pause_key_id|"height"'
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8081/       # 200, footer shows 0.5.3
journalctl -u NODEA -u NODEB --since -5min | grep -iE 'error|skipping peer|checkpoint'
systemctl start netlithic-healthcheck.timer <miner-timer>
```
Node 2 still runs 0.5.0 at this point and keeps syncing (below 4900 it only gets a warning). Wait for one new
block before moving on. Continue to node 2 **now**, do not leave the servers split overnight.

## 2. Node 2 (157.230.63.97)
Relay only: unit `netlithic-node`, code `/opt/netlithic`, venv `/opt/netlithic-venv`, data
`/var/lib/netlithic/node`, user `netlithic`. No `/etc/netlithic/node.env`.
```bash
systemctl list-units --all 'netlithic*'          # expect only netlithic-node.service
systemctl cat netlithic-node | grep ExecStart     # --peer http://137.184.199.238:8000 (public)
systemctl stop netlithic-node; systemctl is-active netlithic-node; pgrep -af 'netlithic.py node'
TS=$(date +%Y%m%d-%H%M); B=/root/backup-0.5.1; mkdir -p $B; chmod 700 $B
tar -C /opt -czf $B/netlithic-code-$TS.tgz netlithic
tar -C /var/lib/netlithic -czf $B/netlithic-data-$TS.tgz node
cp -a /etc/systemd/system/netlithic-node.service $B/netlithic-node.service-$TS
sha256sum /var/lib/netlithic/node/chain.json | tee $B/chain-sha-$TS.txt
sha256sum $B/*-$TS.tgz | tee $B/tarballs-sha-$TS.txt
sha256sum -c $B/chain-sha-$TS.txt                 # while stopped
sha256sum /root/netlithic-0.5.1.tgz               # equals the noted checksum
mv /opt/netlithic /opt/netlithic.prev
mkdir /opt/netlithic && tar -C /opt/netlithic -xzf /root/netlithic-0.5.1.tgz
chown -R --reference=/opt/netlithic.prev /opt/netlithic
cp -a /opt/netlithic.prev/config/. /opt/netlithic/config/ 2>/dev/null || true
/opt/netlithic-venv/bin/pip install -r /opt/netlithic/requirements.txt
cd /opt/netlithic && /opt/netlithic-venv/bin/python ops/set_pause_pubkey.py --check
systemctl start netlithic-node; sleep 15
curl -s http://127.0.0.1:8000/status | python3 -m json.tool | \
  grep -E '"version"|fork_ready|pause_enabled|pause_key_id|checkpoints_ok|"height"|latest_hash'
curl -s http://137.184.199.238:8000/status | python3 -m json.tool | grep -E '"version"|pause_key_id|"height"|latest_hash'
```

## 3. Final checks (both servers)
- Founder :8000, :8001 and node 2 :8000 all report `version 0.5.3`, `pause_enabled true`, the **same
  `pause_key_id`** (equal to the one you wrote down), the same height and tip, `checkpoints_ok true`.
- `/pause_status` on each: `enabled true`, `paused false`, `nonce 0`.
- Wait for one new block; all three reach the same new tip.
- Explorer home shows "ready for the block 5000 upgrade: 2 of 2".
- Healthcheck: its `expected_version` default is now 0.5.3 (a server still on 0.5.1 shows as a version problem). Add `"expected_pause_key_id": "<key id>"` to
  `/etc/netlithic/healthcheck.json` **without printing the file** (it holds the webhook URL), e.g. edit it in an
  editor, then run the healthcheck once. An alert for a node with a different or empty key means that node would fork.
- Web wallet loads and `/api/info` answers.

## 4. After the deploy
- Tell node runners (draft in [RELEASE_PLAN.md](RELEASE_PLAN.md)): update to 0.5.3 before block 4900. Follow up with
  anyone the healthcheck lists as an old peer.
- Rebuild and publish the 0.5.1 desktop installers (the founder approves); 0.5.0 desktop apps get the "update needed" line.
- Keep the offline pause computer ready (PAUSE_KEY_CEREMONY.md). To use it later: `ops/pause_tx.py` (always with `--chain-id netlithic-dev-1` for a real pause), next nonce
  = `/pause_status` `nonce` + 1.

## 5. Rollback (per server)
Node 2:
```bash
systemctl stop netlithic-node
rm -rf /opt/netlithic && mv /opt/netlithic.prev /opt/netlithic
# only if data is suspected bad:
#   rm -rf /var/lib/netlithic/node && tar -C /var/lib/netlithic -xzf /root/backup-0.5.1/netlithic-data-$TS.tgz
#   sha256sum -c /root/backup-0.5.1/chain-sha-$TS.txt
systemctl start netlithic-node
```
Founder:
```bash
systemctl stop netlithic-healthcheck.timer <miner-timer> netlithic-explorer <webwallet-unit> NODEB NODEA
rm -rf /opt/netlithic && mv /opt/netlithic.prev /opt/netlithic
# optional data restore: tar -xzf /root/backup-0.5.1/netlithic-data-$TS.tgz -C /   (then sha256sum -c ... before start)
systemctl start NODEA NODEB netlithic-explorer <webwallet-unit> netlithic-healthcheck.timer <miner-timer>
```
**Rollback limits:** safe only while no pause tx is on the chain. Once a pause tx is mined, a rolled-back 0.5.0
(empty key) node rejects that block and cannot follow the chain. Also change `expected_pause_key_id` in the
healthcheck config to the older node's key id (or expect an alert about a different or empty key). A blank value now
raises a warning, not silence. Rolling the version gate back does not help
nodes that are already 0.5.1.

## 6. Points v4 additions (only if the points-v4-protection build is the one being deployed)
Nothing here changes the order of the steps above; these are extra checks and a different timeline.

Before the deploy:
1. Full test suite on a clean checkout: `pytest -q` (expect all passing), plus `python ops/sim/points_sim.py --v4`.
2. `git diff --stat release-0.5.1...points-v4-protection` is what the founder reviewed. The tarball is built from the approved tag only.
3. Note the expected rules fingerprint: on the build machine,
   `python -c "import core; print(core.points_v3_rules_hash())"`. Write it down next to the pause key id.

After the deploy, on **every** server (founder :8000, :8001, node 2):
```bash
curl -s http://127.0.0.1:8000/status | python3 -m json.tool | \
  grep -E '"points_rules"|"last_reorg_depth"|"max_reorg_depth_24h"|"version"|"height"'
curl -s 'http://127.0.0.1:8000/payout_preview' | python3 -m json.tool | head -30     # read-only; before the first period ends it says so in plain words
```
Expect: the same `points_rules` value everywhere (equal to the one written down), `max_reorg_depth_24h` 0.
**Different `points_rules` on any server = stop and fix before 5000.**

Timeline after block 5000: block 6440 is an epoch block but pays **nothing** under v4 (this is correct; a plain 0.5.1 node
would pay there and split off at 6440). Block 7880 pays the first period. In the day before 7880, anyone can run
`python ops/verify_payouts.py --node http://137.184.199.238:8000` and read the plain-words result. It sends GET requests only.
If something is clearly wrong the pause key stops payouts at the paying block (see PAUSE_KEY_CEREMONY.md); the period is
skipped, with no back pay (POINTS_V4.md).

Rollback caveat: after block 5000 a rollback to a build without v4 forks the node at **6440** (it pays there, v4 does not). The healthcheck now also
reports `deep_reorg:<node>` if any node saw a reorg of 3 blocks or more (or refused a too-deep one) in the last 24 hours
(treat as a warning to look at the logs).


### 7a. How the 0.5.3 peer gate behaves (review round 2)

The version gate is a **peer-connection rule only**. It never changes whether a block is valid, so it cannot split the chain by itself.

* **When it applies:** from block 4900 (`UPGRADE_DEADLINE_HEIGHT`). **Below block 4900 the gate is not enforced**: an older build is let in
  and only flagged, exactly as before. This is deliberate, so servers can be upgraded one by one without cutting each other off.
* **Who is checked:** a new peer on `/hello`, a peer we already know on its next `/hello` and in a periodic re-check, peers learned by
  gossip (they must answer `/status`; unreachable ones are skipped and remembered as rejected for a while), and `--peer` / `add_peer`
  additions (these may be unreachable, because an operator typed them on purpose).
* **Version strings:** a pre-release such as `0.5.3-rc1` counts as older than `0.5.3` and is refused. Odd strings such as `0.5.3.1` or
  `0.5.3+x` are treated as outdated (fail closed).
* **Upgrade before block 6440.** A plain 0.5.1 node pays an epoch reward at block 6440 and a v4 node does not, so a 0.5.1 node forks
  at 6440. A node that stays on 0.5.1 and runs **more than 720 blocks past 6440** cannot rejoin by just upgrading: it has to **wipe its
  chain data and resync** from a 0.5.3 peer. Upgrade every server and every public node **before block 6440**; do not wait.

## 7. What is new in 0.5.3 (security review fixes) and what to expect

* **0.5.3 versus the 0.5.2 development builds:** 0.5.3 adds the round-3 fix to the deep-reorg alert (junk chains can no longer hide it;
  `/status` and the healthcheck also show `refused_unvalidated_deep_reorgs_24h`, information only, never an alert). It changes no
  consensus rule relative to `4080424`, but the version is now 0.5.3 so builds before it are told apart and refused from block 4900.
* **Version gate:** `VERSION` and `MIN_PEER_VERSION` are 0.5.3. From height 4900 a hello without a rules fingerprint is refused as well.
  A 0.5.1 node splits at **6440**, so make sure every server reports 0.5.3 and the same `points_rules` before 4900.
* **Healthcheck expectations:** `expected_version` 0.5.3; `deep_reorg:<node>` at depth >= 3 or on a refused deep reorg;
  new `miner_share:<address>` and `miner_cluster:<root>` alerts when one address (or a group funded from one source) mined more than
  40% of the last 200 blocks. **Before turning the timer on, put the founder's own mining addresses in `miner_share_ignore` (and
  faucet/exchange funders in `miner_share_ignore_funders`) in `/etc/netlithic/healthcheck.json` (without printing that file),
  or the founder's own mining will page.** Config keys: `miner_share_window` (200), `miner_share_threshold_pct` (40),
  `miner_share_ignore`, `miner_share_ignore_funders`.
* **Reorg record file:** each node now writes `chain.json.reorgs.json` next to its chain file (a small log; safe to delete).
  Include it in nothing; it is not part of any backup requirement.
* **Pause expectations:** unchanged: the pause key stops payouts at the paying block. The registry key (if used) is a separate key;
  `registry_tool.py --check` must pass before the release.
* **Registry:** rotation, removal limits, `valid_until` and the break-glass plan are in POINTS_V4_REGISTRY.md.
* **verify_payouts.py:** now checks the downloaded chain first and compares against what the paying block actually paid;
  exit 2 if the node reports no fingerprint. Run it with `--full` once the day before 7880.
* **Pause/drill:** run `ops/drill/pause_drill.py` as before; nothing in the drill changed.

## Not rehearsed on a replica
This runbook was written from the 0.5.0 rehearsal and the F9/F10 deploy and has not been executed. A replica
rehearsal (same method as the 0.5.0 one) should be run first if time allows. The pause key itself is exercised
by `ops/drill/pause_drill.py` and `tests/test_pause_tools.py` only on a private chain with throwaway keys.
