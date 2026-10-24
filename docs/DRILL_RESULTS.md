# Local 51% / reorg drill: results

Run on the build box only. No network, no servers, no real wallets. Run
date: 2026-10-22. To reproduce: `python ops/drill/attack_drill.py --json out.json`
(about 30 s). The raw output is in `docs/drill_results.json`.

## Setup

* 4 independent `Blockchain` node instances in a throwaway directory. They run
  in one process and talk by passing blocks and chains directly, not over HTTP.
  All of them run the real validation code: `add_block`, `replace_chain` and
  `is_valid_chain`.
* For speed, the fork heights are moved down to 3 and the work floor is low.
  The reorg limit is set to **20** in the drill (production uses 100), so the
  deep-reorg case runs in seconds.
* The honest miner builds 36–70 blocks at 60 s spacing, and all nodes accept
  them one by one.
* The attacker has about 2x the honest hashrate (30 s blocks). It mines a
  secret fork starting N blocks back, makes it 5 blocks longer, then
  broadcasts the whole chain to every node.
* "Before" means the security fork is inactive, which is the old behaviour.
  "After" means the fork is active.

## Results

| Scenario | Before: nodes reorged / honest blocks erased | After |
|---|---|---|
| Reorg 6 deep | 4/4, 6 erased | 4/4, 6 erased (inside the limit, as designed) |
| Reorg 15 deep | 4/4, 15 erased | 4/4, 15 erased (inside the limit) |
| Reorg 40 deep | **4/4, 40 erased** | **0/4**, "reorg of 40 blocks exceeds limit 20" |
| Reorg 10 deep through a checkpointed block | 0/4 (*) | 0/4, checkpoint mismatch |
| Fake stall: backdate the previous block, then claim 1/8 work | **cheap block allowed** | refused (needs a real 10 min gap after the newest of the last 11 blocks) |

(*) Checkpoints are new code in this branch. They apply whenever
`config/checkpoints.json` exists, whatever the fork height, so "before" here
means the new code with the fork off. The old code has no checkpoints, so on
the old release that reorg would have gone through too.

## What this means

* Before the fix, a majority miner could rewrite any amount of history. Every
  node followed the heaviest chain.
* After the fix, synced nodes refuse to drop more than `MAX_REORG_DEPTH` blocks.
  Shallow reorgs still work, which is how normal forks heal. Anything inside
  the limit can still be double-spent by a majority miner, which is why
  `CONFIRMATIONS.md` says to wait 60-120 confirmations (721 for large deposits) before crediting
  large deposits.
* The 1/8-work stall rule could be triggered without a real stall by
  backdating. This is now fixed after 5000. Blocks are still valid when they
  are backdated (above MTP), but they no longer unlock cheap work.

## Limits of this drill

* The nodes are in-process, not separate OS processes talking HTTP. The HTTP
  paths for blocks and chains go through the same `add_block` and
  `replace_chain` code, and `tests/test_hardening.py` and
  `tests/test_net_limits.py` cover the HTTP layer.
* The drill doesn't cover eclipse attacks (one victim fed only attacker peers)
  or a node that was offline during the attack and later syncs to the
  attacker's chain. The reorg limit doesn't protect against either. Checkpoints
  and more independent peers do.


Note (2026-10-23): the drill uses a test limit of 20 blocks; the real post-5000 limit is now 720 blocks (~12 h).
