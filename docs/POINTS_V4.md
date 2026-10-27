# Points v4: what changes at block 5000, and what it does not do

**Status: built on local branches (`points-v4-protection`, then `points-v4-registry`, then `points-v4r-fixes` with the security-review fixes) only. Nothing is pushed, tagged or deployed. the founder decides.**
Test coins have no value. Nothing in this page is about price, return or investment.
An optional stop-gap on top of v4, limiting the transfer pot to a registered list, is in `POINTS_V4_REGISTRY.md` (temporary, centralized, local branch only).

Points v4 **replaces** points v3 (which never shipped). It takes v3 (slow release, two pots, 25,000 cap), changes
three numbers, adds a one-day delay, and turns the funding-family rule off. Everything switches on at **one announced
height: block 5000** (`core.POINTS_V4_HEIGHT`, equal to `SECURITY_FORK_HEIGHT`). Before 5000 nothing changes.

## In plain words

* **Be honest about the goal.** This is a checkpointed testnet. v4 makes cheating *more expensive and less
  rewarding*. It does **not** make the testnet farming-proof, and nothing here should be read as a promise that it is.
* Every day the chain releases a slice of the community pool (about 0.82% of what is available, about 20,548 NLT on a
  2.5M pool). v4 splits that slice into **two pots**: 20% for people who send real transfers, 80% for people who mine.
* **No single address can take more than 2% of a pot** in one day. Be careful what that means: it holds one *address*.
  The mining pot is shared **by block share**, so a miner who spreads their blocks over many addresses takes about
  their whole block share (a miner with 60% of the blocks takes about 60% of the mining pot). The split into two pots and
  the 2% limit do **not** protect the mining pot. Only hash power limits a miner. See "What is NOT protected", item 0.
* **Every block earns its block points from block 5000 on, empty or not.** (Review fix H1: before, a block with no
  transfers earned nothing, and one 1-base-unit self-transfer in a block was enough to make it count, which let a 5% miner
  take about 83% of the pot on a quiet chain.)
* A day's rewards are **not paid on the day**. They are paid, and can be spent, one day later (1,440 blocks), at the
  end of the *next* period. By then the blocks that earned them are older than the 720-block reorg limit, so they
  cannot be rewritten any more.
* If payouts are paused, the paused day is simply skipped. Its coins stay in the pool. There is no back pay.
* The "funding family" rule (a best-effort attempt to spot fake addresses funded by one source) is **OFF**.

## Parameters (all consensus: every node must agree)

| Parameter | Value | Where | In the rules hash |
|---|---|---|---|
| Fork height (everything below switches on) | 5000 | `core.POINTS_V4_HEIGHT` | yes (`fork_height`) |
| Period length | 1,440 blocks | `economics.epoch_length_at` | n/a (existing) |
| Release per period | `pool * 1,440 / 175,200` (~0.82%) | `Blockchain._v4_period_inputs` | n/a (existing) |
| Transfer pot | 20% of the release | `contributions.TX_POT_PERCENT` | yes |
| Mining pot | 80% of the release (the rest) | derived | yes (via the above) |
| Per-address limit | 2% of each pot per period (rounded down, in base units) | `contributions.POT_CAP_PERCENT` | yes |
| Old per-address cap | 25,000 NLT, kept as a backstop (never reached: 2% limit is at most about 411 NLT) | `economics.payout_cap` | n/a (existing) |
| Payout lag | 1 period (1,440 blocks) | `contributions.PAYOUT_LAG_PERIODS` | yes |
| Funding-family rule | OFF | `contributions.FAMILY_RULE_ENABLED` | yes |
| Per-point ceiling | OFF (None) | `contributions.MAX_BASE_PER_POINT` | yes |
| Points | 2.5 per mined block (every block from the fork; before the fork only non-empty ones), 1 per qualifying transfer | `contributions.points_from_blocks(flat_block_points_height=...)` | yes (`block_points=flat`) |
| Transfer rules | at least 1 NLT, pair cap 10, sender cap 30, reciprocal rule, new-address wait 1,440 blocks | existing | partly |
| Vesting | unchanged (`vest_start` = the block that pays; same length) | existing | n/a |
| Reorg limit | 720 blocks, unchanged | `core.MAX_REORG_DEPTH` | n/a |

`core.points_v3_rules_hash()` (shown in `/status` as `points_rules`, and still as `points_v3_rules` for old tools)
covers the rows marked yes, and the registry constants. `/hello` refuses a peer with a different value, and, from height 4900
(`netlithic_version.RULES_FIELD_REQUIRED_HEIGHT`) also a peer that sends **no** value at all (it used to be let through). That is a handshake check, **not**
consensus: a node that never says hello can still push blocks. The real protection is that everyone runs the same build.

## What exactly is paid when (the lag)

Block `X` is an epoch block (6440, 7880, 9320, ...). From the fork, the epoch block at `X` pays the period that
ended one period *before* the one that just ended:

| Epoch block | Pays the points earned in blocks | Release budget measured |
|---|---|---|
| 6440 | nothing under v4 (the only full period so far is still too fresh). **A plain 0.5.1 node would pay at 6440, so a node that is not on 0.5.3 splits from the network at 6440.** | |
| 7880 | [5000, 6440) = "period 1" | pool right after block 6440 |
| 9320 | [6440, 7880) = "period 2" | pool right after block 7880 |

So **the first payout moves from block 6440 to block 7880** (about a day later). The 6440 block is mined without
payouts, and that is valid. (Earlier drafts said a stale node would split at 7880. That was wrong: a node on the
plain 0.5.1 rules pays at 6440, a block the v4 chain leaves unpaid, so it forks at **6440**. The 0.5.3 version gate exists
to keep such nodes away before then.)

Why one day: the blocks that decide who earns what are at least 1,440 blocks old when the payout is made, which is
twice the reorg limit (720). A legal reorg can no longer change the list. It also gives the team a day to look at the
proposed list (`ops/verify_payouts.py`) and, if something is clearly wrong, to pause payouts before they are credited.

### Pause and skipped payouts
* The pause check happens at the **paying block**. If payouts are paused at block `X`, the block carries no payouts.
* The period that would have been paid at `X` is **not paid later**. Its coins stay in the pool (they are not
  burned, and are not re-shared). The next epoch block that is not paused pays only its own period.
* A pause still stops community payouts only. Mining rewards, transfers and everything else carry on.
* Boundary: a block at `X` pays period `N`; blocks in period `N+1` are still being earned and are not touched.
  A transfer mined in the very last block of a period counts in that period, a transfer in the next block counts in the next.

## What `payout_preview` and `ops/verify_payouts.py` are

* `GET /payout_preview?period=N` (no `period` = the latest finished one) shows exactly the list the chain will pay for
  period `N`, with each address's mining and transfer points, the two pots and the per-address limits. It is read-only
  and uses the same code the chain uses, so there is no second set of rules to drift. It is computed on a copy of the
  chain (it does not hold the node's main lock), cached per (period, chain tip), and rate-limited per client (HTTP 429).
  The registry is read as of the paying block, the same as the real payout.
* `ops/verify_payouts.py --node http://HOST:PORT [--period N] [--full]` downloads the chain and **checks it before trusting
  it** (block numbers, hash links, each block's own hash, proof of work, and every signed transfer's signature; `--full`
  also runs the node's whole validator). If the chain fails, it stops with exit 2. It then recomputes the list with its
  **own, separate implementation of the payout arithmetic** (points, release budget, pots, the 2% limits), compares it
  with the node's answer **and with the payouts actually paid in the paying block** (if that block exists), and says in plain
  words if: an address is over its limit; five addresses take more than a quarter of the payout; 20 or more addresses
  earn only from transfers and all sit at the 30-transfer limit; more than half the release is unspent; any list differs;
  or the node reports **no rules fingerprint** (that is now an error, not a pass). Exit code 0 = nothing odd, 1 = look at it,
  2 = could not check. It sends only GET requests, writes nothing in the repo and needs no keys. What it shares with the
  node (registry scan, economics settings, the signature check) and what it cannot see are printed at the end of every run.

## Which builds are supported
**Only 0.5.3.** Builds before 0.5.3 (0.5.0, 0.5.1 and every 0.5.2 development build, including `eb4e3d5` and `4080424`) are unsupported.
All nodes must upgrade together, before block 4900 (and in any case before 6440).

## The reorg tripwire (off-chain, not consensus)
`/status` has `last_reorg_depth`, `max_reorg_depth_24h`, and (new) `rejected_deep_reorgs_24h` and
`max_rejected_reorg_depth_24h` (reorgs the node refused as too deep or against a checkpoint). The record is saved to
`chain.json.reorgs.json` next to the chain file, so a restart no longer clears it (the file is local, not consensus, and
ignored by git). A reorg of 3 blocks **or more** writes a warning to the node log, and the healthcheck raises
`deep_reorg:<node>` at depth >= 3 (the same line the warning uses), or when any deep reorg was refused. The 720-block cap is unchanged.
Checkpoints stay hard-coded in each release (block 400 exists); there are no live signed checkpoints.
The alert stays up for up to 24 hours after one event, by design.

Review round 3 (N1): a chain only counts as a refused deep reorg after it passes a cheap structural check (the shared part must be
our own blocks; every divergent block must have the right index, link, hash, proof of work and an allowed difficulty) and then a full
validation. Junk cannot use up the one-validation-per-30-seconds slot, and a junk body that copies the real tip hash cannot make the
real chain look "already seen", because a tip is remembered only after a verdict and only for chains that passed the cheap check.
`/status` also shows `refused_unvalidated_deep_reorgs_24h` (and `_total`): offers refused without a verdict (made-up chains, or
arrived while the budget was spent). It is volume only, in memory only, never an alert. The healthcheck prints it as an "Info" line.

## What changes for honest users and miners (from the simulator)
Numbers below are produced by `python ops/sim/points_sim.py --v4` (one period, release 20,548 NLT, synthetic chains).

* A quiet network pays out **less** than v3 on purpose. The 2% limit means a pot is only paid out in full once at least
  50 addresses share it. With 20 honest users and 3 miners, 10% of the day's release is paid; the rest stays in the pool.
* 3 miners are each held to 2% of the mining pot (about 329 NLT) instead of sharing 14,384 NLT. That is a limit on
  *addresses*, not on people: see item 0 below and `python ops/sim/points_sim.py --mining`.
* In the 400-fake ring, honest users still get very little in v3 and in v4, because the ring dilutes the transfer pot.

## What is NOT protected (read this)

0. **The mining pot follows block share, and the 2% limit does not stop a miner with many addresses.** Simulator
   (`--mining`; mining pot 16,438 NLT per period; each address held at 329 NLT): one miner with 30% of the blocks takes 5,137 NLT (31%)
   from 25 addresses; with 60% it takes 10,046 NLT (61%) from 50 addresses; with 90% it takes 14,840 NLT (90%) from 50
   addresses. On one address the same miner is held to 329 NLT (2%). Addresses cost nothing, so assume any big miner can split.
   The only real limit on the mining pot is **hash power**: you need the blocks. Nothing in v4 stops a majority miner,
   and a majority miner can also rewrite history inside the reorg limit. The registry guards the transfer pot only.
   What v4 does about it: it removes the *cheap* trick (the empty-block discount, which let a 5% miner take 83% of a quiet chain's pot
   with one tiny self-transfer per block; that is now 5%), and the healthcheck raises `miner_share:<address>` or `miner_cluster:<root>`
   when one address, or a group of addresses funded from one source, mined more than 40% of the last 200 blocks. A miner that
   funds its addresses from different places is invisible to that alert.

1. **A ring of fake addresses still gets paid.** v4 limits it to the 20% transfer pot (and 2% of that per address),
   not to zero. In the simulator a 400-address ring takes about 4,089 NLT of one period (v3: 6,134 NLT, and v3 with the
   family rule: 0 only if the ring is funded from one known source). It is **80%** of what v4 pays in that world,
   because the cap leaves most of the pot unspent. The unspent coins stay in the pool.
2. **The 2% limit does not catch rings.** It only matters when fewer than about 50 addresses share a pot. A ring of
   hundreds of addresses stays under it.
3. **Fakes that mine their own first coins** (each mines one block) look like honest new users. The family rule could
   not catch them either (simulator: same 6,134 NLT under v3 with the rule on).
4. **Self-stuffing is not fixed** (item 5 of the plan, deliberately not built). A miner can still send transfers
   between addresses they own and earn transfer points (up to the pair cap of 10 per pair per period, and at most 30
   per sender). Any rule based on "who funded whom" has the same two holes as the family rule: one funding step of dust
   (staging) breaks it, and two or more funders defeat it, and it also penalises honest friends and exchange customers.
   We are not shipping a rule we know can be sidestepped cheaply and that hurts honest users. `tests/test_points_v4_attacks.py`
   keeps a test showing it still works, so nobody assumes otherwise.
5. **The funding-family rule is best-effort and OFF.** The code, the issuer list and its tests stay. If switched on later
   it catches only the laziest farm (all fakes funded from one hub that traces back to a known issuer). Turning it on
   needs a valid issuer list; with the rule off, an empty or unused issuer list neither stops a node from starting nor
   changes any payout.
6. **A deep reorg is not prevented by the tripwire**, it is only noticed. Protection is the 720-block cap, the hard-coded
   checkpoints, and the one-day lag. Counters are in memory.
7. **A pause only helps if someone looks.** The lag gives a day to look. Nobody is watching unless a person runs the check.
8. **The handshake fingerprint is not consensus.** Nodes running a build with other constants can be refused on hello (from
   height 4900 also a node that sends no fingerprint), but a node that never says hello is not stopped by it. The version gate
   (0.5.3) and the fingerprint only keep honest, stale nodes away; they do not stop an attacker.
9. **Nothing here fixes hash power attacks or a network with very few miners.**

## Upgrade steps (summary; the full server steps are in `UPGRADE_RUNBOOK_0.5.1.md`, section 6)
1. the founder approves the branch. Nothing leaves the box before that.
2. Run the whole test suite on a clean checkout, then the points simulator and `ops/verify_payouts.py` against a test node.
3. Announce the fork height (block 5000), the first payout at block 7880, and the new numbers (20/80, 2%, one-day lag).
4. Every node must run the same build well before block 5000. Check `/status` `points_rules` on every server: all equal. Servers must run **0.5.3**; `MIN_PEER_VERSION` is 0.5.3 and a plain 0.5.1 node splits at 6440.
5. After 5000, run `ops/verify_payouts.py` once a day before the payout block (6440 pays nothing; 7880 pays the first period).
6. If something looks wrong in the proposed list, a pause (the existing pause key) stops payouts at the paying block; the
   period is skipped and its coins stay in the pool.

## Rollback
Before block 5000 the build can be swapped back with the same steps as the 0.5.1 rollback. After 5000, a node on
the old rules computes different payouts at the first paying block and would fork, so a rollback after the fork needs the
whole network to move together. Treat the release as one-way.

## Open choices for the founder
See `/workspace/v4-report/SUMMARY.md` in the build report. In short: the first payout date (7880), whether the 2% limit
should be higher or lower, whether to turn the family rule on, whether to build any self-stuffing protection, whether to remove or reshape the per-address mining limit (it does not do what it looks like it does, item 0), the
version string for the build, and whether to announce the date.
