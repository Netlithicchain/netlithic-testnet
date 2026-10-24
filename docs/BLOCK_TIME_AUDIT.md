# Block-time audit

Data: public explorer pages `https://testnet.netlithicchain.com/explorer/block/N`, blocks 1-434,
read 2026-10-23 12:48 UTC (6:48 AM MT). Height then: 434.

## Real block time today (before block 5000)

| Window | Blocks | Mean | Median | Min | Max |
|---|---|---|---|---|---|
| Last 100 | 100 | 623 s (10.4 min) | 620 s | 601 s | 663 s |
| Last 288 | 288 | 383 s | 607 s | 3 s | 663 s |
| All since block 1 | 417 | 348 s | 600 s | 3 s | 663 s |

The early average is lower because of short bursts of community mining (address `b3e0835f…`
mined 182 of the first blocks). Recently the founder server makes nearly every block.

**Cause:** not the difficulty rule. Pre-fork difficulty is "leading hex zeros", target 15 s
(`core.TARGET_BLOCK_TIME`), and the chain sits at difficulty 2 (416 blocks) or 3 (10 blocks):
a block takes ~256 hashes, i.e. milliseconds. Blocks come every ~10 min only because the
founder server mines **one block per systemd timer run, every 10 minutes**
(`ops/founder_miner.py`; `ops/healthcheck.py` notes "timer mines every 10").
From block 5000 the LWMA rule targets **60 s**, *if there is community hashrate*. The founder
server stops mining at 5000; with no other miners its backstop makes one block per 30 min.

## Every block-count rule in real time

| Rule | Blocks | At ~10 min (today) | At 60 s (LWMA target) | At 30 min (backstop only) |
|---|---|---|---|---|
| Reorg limit (`core.MAX_REORG_DEPTH`) | 100 | 16.7 h | 1 h 40 min | 50 h |
| Pause auto-expiry (`pause.PAUSE_EXPIRY_BLOCKS`), intended ~7 days | 1,008 | 7.0 days | **16.8 h** | 21 days |
| New-address wait (`contributions.MIN_ADDRESS_AGE_BLOCKS`), approved as "~2.4 h" | 144 | 24 h | 2.4 h | 3 days |
| Testnet payout period (`economics.TESTNET_SHORT_EPOCHS`), chosen as "~1 day" | 144 | 24 h | **2.4 h** | 3 days |

All four rules only bite from block 5000, so the 60 s column is the one that matters.
Mismatches: the pause would end after ~17 h instead of 7 days, and payouts would run ~10 times a day.

**Block 5000 ETA:** 4,566 blocks to go x 623 s = ~32.9 days -> **about Nov 25, 2026** (MT),
if the 10-minute timer keeps running and no one mines faster.
**Nov 9 release target:** ~17 days from now, around block ~2,800, leaving ~2,200 blocks
(~15 days) for nodes to update before 5000. The target holds.

## Values from block 5000 (approved by Aric 2026-10-23, set in code on branch `proposals`)

| Rule | Was | Now (from 5000) | Real time at 60 s | Where |
|---|---|---|---|---|
| Reorg limit | 100 | **720** | ~12 h | `core.MAX_REORG_DEPTH` |
| Pause expiry | 1,008 | **10,080** | ~7 days | `pause.PAUSE_EXPIRY_BLOCKS` |
| Testnet payout period | 144 | **1,440** | ~1 day | `economics.TESTNET_SHORT_EPOCHS` |
| New-address wait | 144 | **1,440** | ~1 day | `contributions.MIN_ADDRESS_AGE_BLOCKS` |
| Sender cap | 30 / period | 30 / period | ~30 per day | `contributions.SENDER_TX_CAP_PER_EPOCH` |

Before block 5000 nothing changes: all four rules only apply from 5000 (`SECURITY_FORK_HEIGHT`
/ short-epoch fork height), and the pre-5000 monthly epoch is untouched.

**First post-fork payout recomputed:** the old plan (5144) assumed 144-block periods. With
1,440 the first payout is at **block 6440** (points from blocks 5000-6439), then 7880, 9320, ...
At 60 s blocks that is ~1 day after block 5000 (~Nov 26 if 5000 lands ~Nov 25). If nobody but
the founder backstop mines (1 block / 30 min) it would take ~30 days.

Recommended exchange confirmations: 60-120 blocks (~1-2 h), 721 for large deposits
(docs/CONFIRMATIONS.md).

If block times stay far from 60 s, every block count drifts; counting by median-time-past
instead is a possible later fork, not proposed now.

`time_params.py` records the intended times and their block counts; tests check the code
constants equal them.
