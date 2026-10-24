# [economics] Anti-farming v2 gaps: self-stuffing, rings, sybil scaling (review F7, Medium)
**Labels:** economics, anti-farming, review-after-first-payout
Problem:
(a) A miner includes one transfer between its own aged sybils and still earns 2.5 points per block (contributions.py:33-39).
(b) The back-and-forth rule only catches A<->B, so a ring like A->B->C->A passes (contributions.py:111).
(c) The 30-per-period cap applies per address, so N pre-aged sybils get 30*N.
Proposed fix: a minimum transfer value for points; count each unique (sender, receiver) pair once per period; weight points by recipient diversity and graph distance; give no points when the block's miner funded the sender.
Needs Aric's decision (economics). Review with the first-payout data (block 6440).
