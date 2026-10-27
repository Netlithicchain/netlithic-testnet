# Overnight report – 2026-10-23 (America/Denver)

Branch: `overnight-hardening` in `/workspace/nl-overnight` (based on release-5000 @ 291205a). **Local only, nothing pushed.**
Head: **see `git log -1`**. Commits: f2a7711 (pause drill), f164321 (points attack tests, reorg requeue fix, docs), then a docs-only commit with this report.
Tests: **576 passed, 2 skipped** (baseline was 529 passed, 2 skipped; +47 new). The 2 HTTP-process drill tests were run 3 times in a row, all green.
Safety: throwaway keys only, generated inside the tests. No wallet or key file was read. `NETLITHIC_ALLOW_PRIVATE_PEERS` was set only in the test fixtures. No push, no SSH, no posts.

## In plain words

1. **The pause switch works as designed, but in the release that is shipped it is switched off.** `PAUSE_PUBKEY` is empty in v0.5.0 and in 291205a. If the live nodes run that, the founder cannot pause payouts in an emergency. Turning the key on later is a hard fork (old-key-less nodes would reject the first pause block and split off). This needs a planned release with the real public key, well before the first payout at block 6440. I could not check what the live nodes run, so please check that.
2. **The points system is open to farming with fake addresses.** The per-address limits (30 transfers, 10 per pair, 25,000 NLT cap, new-address wait) all count per address, and addresses are free. A ring of fake addresses sending 1 NLT around in a circle earns the full 30 points each and gets past the "no back-and-forth" rule. In a simulation with the real constants, 400 fake addresses took 96% of what was paid in the first period and about 1.9M of the 2.5M NLT month pool, at a cost of about 1.2 NLT in fees. Honest heavy miners, meanwhile, hit the 25,000 cap.
3. Everything else I tried (caps, minimums, boundaries, reciprocal rule, case/whitespace tricks, pause replay/forgery) behaves as documented.

## Task A – pause drill

Files: `tests/test_pause_drill.py` (21), `tests/test_pause_drill_http.py` (2, real node processes), `ops/drill/pause_drill_node.py`, `docs/PAUSE_DRILL_2026-10-23.md`.

Works: valid pause; forged / wrong key / malformed / wrong chain_id rejected by all nodes; replay rejected (same nonce, later block, after resume, other chain); expiry at 10,080 blocks then payouts resume with no back pay; renew; late renew counts as a new pause; resume; pause+resume in one block (last wins); fork boundary (rejected at 4999, accepted at 5000); reorg drops a non-winning pause; stored blocks cannot be altered into a bricking pause.

Fits INCIDENT_PLAN.md? Mostly, with these gaps (details in the doc):

| # | Severity | Gap | Fix |
|---|---|---|---|
| A1 | **High** (ops, release blocker) | Switch is OFF in shipped source (`PAUSE_PUBKEY=""`); adding it later splits keyless nodes | Ship a keyed release before 6440, confirm live version, then drill again on the real build |
| A2 | Medium | Key rotation (F8): a new `PAUSE_PUBKEY` invalidates old blocks that hold old-key pauses | Key-by-height list or on-chain rotation tx; 2-of-3 keys |
| A3 | Medium (runbook) | A pause must be mined at least 1 block before the payout block; a pause in the payout block only stops the next period | Add to the plan: pause at least ~10 blocks before 6440 + every 1,440 |
| A4 | Low/Medium | Orphaned pause vanished from the node after a reorg | **Fixed in branch**: `_requeue_orphaned_txs` in p2p_http.py + test (mutation-checked: fails without the fix). Node-local, not consensus |
| A5 | Low/Medium | Leaked pause key can renew for ever (cannot move coins) | Rotation path; optional renewal cap |
| A6 | Info | Expiry is fail-open (about 7 days); vesting continues during a pause | Document |
| A7 | Info | Plan has no key backup/loss steps and no cross-node verification checklist | Added a checklist and summary to the plan |

## Task B – points and payout review

File: `tests/test_points_abuse.py` (21 tests: unit rules with real constants, payouts via real `expected_payouts` on a synthetic chain, and real mined blocks). Tests named `test_finding_*` pass today and pin the weakness; a fix should make them fail on purpose.

Rules verified correct (tests pass): 30 per sender per period and reset next period; 10 per pair; reciprocal block in both orders and same block; 1 NLT minimum (1 NLT minus 1 unit earns 0); no self-transfer points; new-address wait exactly 1,440 blocks (1,439 fails, 1,440 passes); old rules before the fork; payouts never exceed the pool, each ≤ 25,000 NLT, deterministic order; address case/whitespace/length tricks cannot split counters.

Findings:

| # | Severity | Finding | Fix idea |
|---|---|---|---|
| B1 | **High** (economic) | **Sybil / ring farming.** All limits are per address; addresses are free; 3-cycle and circulant rings (offsets 1,2,3) defeat the reciprocal rule. 8 fakes each earn 30 pts. Simulation, real constants, first period: 400 fakes get 1.92M of 1.998M paid (96%); with 120 fakes + 20 honest users, fakes get 1.23M, honest users 0.034M. Cost is only fees (about 1.2 NLT for 400×30 txs), and a miner pays them to himself. 1 miner network-wide earns at most 3,600 pts per period, so about 120 fakes already match all mining combined | Weight points by distinct counterparties or by recipient age/history; ignore recipients funded by the sender; cap points per funding-source cluster; make points sub-linear per address; stronger new-address rule |
| B2 | Medium (design) | **Pool front-loading.** A period can pay out any share of the available pool; the first month-step (2.5M) is available from day 1. 400 fakes get most of it in one day, the rest in days, while the step is meant to last about 121 days. Even with no attacker, 7 honest users + 3 miners get 250,000 NLT in one day | Release only a fraction per period (for example pool_step / periods_in_month) |
| B3 | Medium | **Per-address cap punishes real heavy users and does not stop splitting.** In the sim the 3 honest miners hit the 25,000 cap while each fake gets its own allowance | Fix B1 first; consider no cap, or a cap by cluster |
| B4 | Medium | **New-address wait is weak.** Starts at first receipt of anything (a dust transfer or the faucet), and addresses seen before 5000 are exempt. So fakes made today are ready by the first payout (6440) | Start the clock at the first qualifying (≥1 NLT, from an aged address) receipt |
| B5 | Low/Medium | **Empty-block rule bypass.** A miner adds one 1-unit transfer to his own second address and earns 2.5 pts per block instead of 0 (test, plus consensus-level test); fee goes back to him. Still bounded by PoW | Not worth a fork alone; fixing B1 matters more. Rule cannot be enforced cheaply |
| B6 | Low | **Burst across period boundary**: 30 txs in the last block of a period and 30 in the first of the next = 60 pts | Info; by design |
| B7 | Low/Info | **Reciprocal-rule grief**: anyone can send A→B first in a period, blocking B→A points for that period for the pair | Info |
| B8 | Info | Faucet (code read only): 10 NLT per address and per IP per 24 h, 500 NLT/day total. Bounded; but each drip is a free "first receipt" for B4 | Fine; consider the B4 fix |

Important: these point rules are consensus rules and frozen in 0.5.0 (nodes refuse older peers from 4900). Fixing B1–B5 means a new fork height with enough notice before payout 6440 (or a delay of the first payout). I made no consensus changes and did not alter economics. Choosing the fix is a design decision for the founder; I recommend deciding before 6440.

## What is in the branch

- `tests/test_pause_drill.py`, `tests/test_pause_drill_http.py`, `ops/drill/pause_drill_node.py`
- `tests/test_points_abuse.py` (21)
- `p2p_http.py`: `_requeue_orphaned_txs` called after a successful `replace_chain` in `_sync_with_peer` (node-local; then `_revalidate_mempool` drops anything now invalid) + `tests/test_reorg_requeue.py` (3)
- `docs/PAUSE_DRILL_2026-10-23.md`, short section appended to `docs/INCIDENT_PLAN.md`

Review before use: the requeue change touches node code, so it needs a release (not consensus, but a new build) if wanted.
