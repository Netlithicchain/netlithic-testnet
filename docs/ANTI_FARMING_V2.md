# Anti-farming v2: proposal, implementation, tradeoffs

Points (and the pro-rata epoch payouts from them) are computed from the chain,
so farming them is a consensus-level problem. v1 already requires transfers of
at least 1 NLT to someone else, and caps a sender->recipient pair at 10 point
transfers per epoch. In v1 you can still farm points with:

1. **Ping-pong:** A->B, then B->A, back and forth. Both sides earn and no coins
   are used up.
2. **Many pairs:** one sender sends to hundreds of throwaway addresses, 10 each.
3. **Fresh sybils:** make new addresses, fund them, and have them all send.

## v2 rules (consensus, active at `SECURITY_FORK_HEIGHT` = 5000)

Code: `contributions.points_from_blocks(..., rules_height, first_seen)`. Core
passes `rules_height=SECURITY_FORK_HEIGHT`, so blocks before 5000 score
exactly as before and history does not change.

| Rule | Parameter | Stops |
|---|---|---|
| Reciprocal: if A->B already earned this epoch, B->A earns nothing | always on | ping-pong |
| Per-sender cap per epoch | `SENDER_TX_CAP_PER_EPOCH = 30` (per 1440-block period, i.e. ~30/day) | many-pairs fan-out |
| Minimum address age: the sender must have first received coins ≥ N blocks earlier | `MIN_ADDRESS_AGE_BLOCKS = 1440` (~1 day at 60 s) | instant sybils |

Transfers that don't qualify are still valid and still go through. They just
earn no points. Mining points don't change.

Tests: `tests/test_security_fork.py` (ping-pong, sender cap, address age,
pre-fork history unchanged).

## Tradeoffs

* **Honest users can lose points.** Two friends paying each other back in the
  same epoch: only the first direction earns. A shop that sends more than 30
  refunds in an epoch stops earning after 30. These users get less, but
  nothing they send is blocked.
* **New users wait.** A brand-new wallet can't earn tx points for ~2.4 h. We
  think this is acceptable for a testnet, and it is the cheapest sybil brake.
* **It doesn't stop sybils completely.** Someone patient can age many
  addresses and use three-way rings (A->B->C->A). It raises the cost, and the
  per-pair and per-sender caps bound how much each address earns. Real sybil
  resistance would need identity or stake, which is out of scope.
* **Consensus change.** Every node must compute points identically, so this
  is a hard fork at 5000 like the other rules.
* **CPU.** `first_seen_heights` is one pass over the chain, the same order of
  cost as today's points computation.

## Parameters and review

Values: 30 per period, 1440-block address wait, 1440-block testnet payout period, reciprocal rule. Change them before
height 5000 only. After 5000 a change needs a new fork height.

The v2 rules stay as built and the per-address
payout cap stays at 25,000 test coins. **Review after the first payout:** once the
first post-5000 epoch pays out, check the payout list for farming patterns (many
new addresses, ring transfers, one miner dominating) and decide whether the
values need a new fork height.
