# Release plan: Netlithic 0.5.0 (security fork at block 5000)

**Version:** `0.5.0` (in `netlithic_version.py`). Locked 2026-10-23 (Aric approved).
**Target release:** on or before **Sun Nov 9, 2026**, and well before the chain reaches block 5000.
Every node must run 0.5.0 before block 5000. Older nodes will follow different rules
from 5000 and split off.

## What is in it

Reorg limit 720 plus shipped checkpoints ([REORG_RULE.md](REORG_RULE.md)), anti-farming v2 with the 25k
cap ([ANTI_FARMING_V2.md](ANTI_FARMING_V2.md)), and the signed payout pause with 10,080-block (~7 day)
auto-expiry ([INCIDENT_PLAN.md](INCIDENT_PLAN.md), [PAUSE_KEY_CEREMONY.md](PAUSE_KEY_CEREMONY.md)).
Also stall hardening, rate limits and the version shown everywhere (`/status`,
wallet, explorer, desktop app, `--version`).

## Order

1. **Before release:** merge the PR into `release-5000` once CI is green. Aric sets
   `PAUSE_PUBKEY` (the public key only, from the ceremony). Tag `v0.5.0`.
2. **Founder server first.** Update it and restart. Check `/status` shows `"version": "0.5.0"`
   and `"fork_ready": true`, and that it keeps syncing and mining for a few hours.
3. **Node 2.** Same steps. Check that both nodes agree on height and tip hash.
4. **Upgrade notice to node runners** (text below). The explorer home page shows
   "Nodes ready for the block 5000 upgrade: X of Y". Follow up until all are ready.
5. **At block 5000:** watch the explorer, the logs and the alerts. Use INCIDENT_PLAN.md if needed.

## Operator update steps

From git:
1. Stop your node (Ctrl+C, close the app, or `systemctl --user stop netlithic-node`).
2. In your netlithic folder: `git pull` (on the release branch, or `git checkout v0.5.0`).
3. `pip install -r requirements.txt` if it changed.
4. Start the node again (`python netlithic.py node start ...` or `systemctl --user start netlithic-node`).
5. Check: `python netlithic.py --version` shows `netlithic 0.5.0`, and your node's `/status` shows
   `"version": "0.5.0"`.

Desktop app: download the 0.5.0 build from the releases page, close the old app and open the
new one. Your data folder and wallets are kept. Check "Netlithic 0.5.0" at the bottom of the page.

Your wallets and data folder are not changed by the update. Never send anyone your wallet files.

## Upgrade notice (draft)

> **Netlithic 0.5.0: please upgrade before block 5000.** At block 5000 the test network
> switches on new safety rules: a 720-block (~12 hour) reorg limit, fairer community payouts, and an
> emergency payout pause. Nodes that have not upgraded will split off at 5000.
> Update steps: stop the node, `git pull` (or download the 0.5.0 desktop app), restart. Then check
> that `/status` shows version 0.5.0. Test coins only, no value.

## Review fixes (PR #1 review F1-F4)
Sync from `/receive_block` is triggered only by known peers with public addresses, rate-limited. The stall rule has no wall clock.
Pre-0.5.0 peers get warned, then are refused from block 4900. See [DESKTOP_UPGRADE_PLAN.md](DESKTOP_UPGRADE_PLAN.md),
[CHECKPOINT_SIGNING.md](CHECKPOINT_SIGNING.md), [ECLIPSE_RECOVERY.md](ECLIPSE_RECOVERY.md). Tracking drafts are in `docs/issues/`.
