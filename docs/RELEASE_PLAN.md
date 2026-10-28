# Release plan: Netlithic 0.5.3 (security fork at block 5000, with the pause key)

> **Only 0.5.3 is supported** (`VERSION` and `MIN_PEER_VERSION` = 0.5.3). Builds before 0.5.3 (0.5.0, 0.5.1 and every 0.5.2 development build) are unsupported:
> a plain 0.5.1 node would split at block **6440**, and nodes before 0.5.3 are refused new handshakes from block 4900.
> All nodes must upgrade together, before block 4900. The node-runner announcement must give at least 14 days' notice.

**Version:** `0.5.3` (in `netlithic_version.py`). 0.5.0 was the first build with the block-5000 rules. Later builds add the pause
public key, the points-v4 payout rules and the review fixes, and raise `MIN_PEER_VERSION` to 0.5.3.
**Release rule:** a build ships only once the founder's pause PUBLIC key is in `pause.py`
(`python ops/set_pause_pubkey.py --check` must exit 0).
**Target:** every server upgraded **well before block 4900**, and in any case before block 5000. Check the
live height first; blocks are targeted at 60 s after block 5000 (about 10 minutes each at the current network speed),
so the time left to block 4900 depends on the pace you see. Every node must run 0.5.3 before block 5000 and before the first pause
is ever sent. A node with an empty pause key would reject the block carrying a pause tx, so it would split off.
Older nodes follow different rules from 5000 and split off too.

If you run a node on your own machine, update as described in [JOIN_TESTNET.md](JOIN_TESTNET.md) ("Update").

## What is in it

Reorg limit 720 plus shipped checkpoints ([REORG_RULE.md](REORG_RULE.md)), anti-farming v2 with the 25k
cap (see the points rules in `contributions.py`), and the signed payout pause with 10,080-block (~7 day)
auto-expiry ([INCIDENT_PLAN.md](INCIDENT_PLAN.md)).
Also stall hardening, rate limits and the version shown everywhere (`/status`,
wallet, explorer, desktop app, `--version`).

## Order

1. **Before release:** the founder runs the offline ceremony and hands over the **public key only**. It goes into
   `pause.py` with `python ops/set_pause_pubkey.py <public key>` (never by hand). Read the key and its key id
   back to the founder and compare with his paper copy. Run `--check`, the tests and `ops/drill/pause_drill.py`,
   merge the PR once CI is green, then tag `v0.5.3` (only after the founder approves).
2. **Founder server first.** Update it and restart. Check `/status` shows `"version": "0.5.3"`,
   `"fork_ready": true`, `"pause_enabled": true` and the expected `pause_key_id`, and that it keeps syncing.
3. **Node 2, right after.** Same steps; do not leave the two servers on different versions for long (a
   an upgraded server warns an older second server before block 4900 and refuses its new handshakes from 4900).
   Check that both nodes agree on height, tip hash and `pause_key_id`.
4. **Upgrade notice to node runners** (text below). The explorer home page shows
   "Nodes ready for the block 5000 upgrade: X of Y". Follow up until all are ready.
5. **At block 5000:** watch the explorer, the logs and the alerts. Use INCIDENT_PLAN.md if needed.

## Release checklist (security review follow-ups)

- [ ] **Never set `NETLITHIC_ALLOW_PRIVATE_PEERS` on public seeds.** It turns off the peer address checks (F1/F1b). It is for local test nets only. Check the systemd units and env files on both seeds.
- [ ] Desktop installers signed: Windows Authenticode and macOS notarization (see DESKTOP_UPGRADE_PLAN.md).
- [ ] Outreach to node runners finished **before block 4900**, when outdated peers start being refused.
- [ ] Pause-key ceremony done and `PAUSE_PUBKEY` set **before block 4900** in 0.5.3 (done offline by the founder).
      Release gate: `python ops/set_pause_pubkey.py --check` exits 0. **Never ship a build with an empty key.**
- [ ] All servers show the same `pause_key_id` in `/status`; healthcheck `expected_pause_key_id` is set to it.
- [ ] Each node logs its checkpoint count at startup and agrees with the block 400 checkpoint (F10).

## Operator update steps

From git:
1. Stop your node (Ctrl+C, close the app, or `systemctl --user stop netlithic-node`).
2. In your netlithic folder: `git pull` (on the release branch, or `git checkout v0.5.3`).
3. `pip install -r requirements.txt` if it changed.
4. Start the node again (`python netlithic.py node start ...` or `systemctl --user start netlithic-node`).
5. Check: `python netlithic.py --version` shows `netlithic 0.5.3`, and your node's `/status` shows
   `"version": "0.5.3"` and `"pause_enabled": true`.

Desktop app: download the 0.5.3 build from the releases page, close the old app and open the
new one. Your data folder and wallets are kept. Check "Netlithic 0.5.3" at the bottom of the page.

Your wallets and data folder are not changed by the update. Never send anyone your wallet files.

## Upgrade notice (draft)

> **Netlithic 0.5.3: please upgrade before block 4900.** At block 5000 the test network
> switches on new safety rules: a 720-block (~12 hour) reorg limit, fairer community payouts, and an
> emergency payout pause. Nodes that have not upgraded will split off at 5000.
> Nodes on 0.5.2 or older are warned now and refused new connections from block 4900.
> Update steps: stop the node, `git pull` (or download the 0.5.3 desktop app), restart. Then check
> that `/status` shows version 0.5.3. Test coins only, no value.

## Review fixes (PR #1 review F1-F4)
Sync from `/receive_block` is triggered only by known peers with public addresses, rate-limited. The stall rule has no wall clock.
Peers older than 0.5.3 (`MIN_PEER_VERSION`) get warned, then new handshakes are refused from block 4900
(already-connected peers are not dropped, so this is a warning and exposure limit, not a hard split guard). See [DESKTOP_UPGRADE_PLAN.md](DESKTOP_UPGRADE_PLAN.md),
[CHECKPOINT_SIGNING.md](CHECKPOINT_SIGNING.md), [ECLIPSE_RECOVERY.md](ECLIPSE_RECOVERY.md).
