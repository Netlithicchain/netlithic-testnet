# Pause-switch drill and points review (2026-10-23)

Everything ran on throwaway chains with throwaway keys generated inside the tests.
Nothing touched a real wallet, a real key or a public node.

Reproduce: `python -m pytest tests/test_pause_drill.py tests/test_pause_drill_http.py tests/test_points_abuse.py tests/test_reorg_requeue.py`

## What the drill showed works

| Scenario | Result |
|---|---|
| Valid pause signed by the pause key | Accepted by all nodes; the next payout is skipped; pool keeps the coins |
| Forged / wrong-key / malformed / wrong-chain pause | Rejected by every node, the block carrying it is rejected |
| Replay (same nonce again, later block, after resume, other chain_id) | Rejected |
| Expiry at 10,080 blocks | Payouts resume by themselves, no back pay; renew before expiry keeps it going |
| Resume | Ends the pause at once |
| Fork boundary | A pause in block 4999 is rejected; in block 5000 it is accepted |
| Reorg | Nodes that switch chains drop a pause that is not on the winning chain |
| Stored block cannot be edited into a "bricking" pause | Blocks are normalized when stored |
| Separate node processes over HTTP | Same results |

## Gaps versus the incident plan

1. **The shipped release has no pause key (`PAUSE_PUBKEY = ""`), so the switch is OFF.** Step 2 of "First 30 minutes" does nothing until a release with the real key is running on the nodes. Adding the key later is a hard fork: a node without the key rejects the first pause block and splits off. The 4900 version gate does not help. Do it as a planned, versioned release well before block 6440.
2. **Timing.** The pause must be mined at least one block *before* the payout block (6440, then every 1,440 blocks). A pause in the payout block itself only stops the following period.
3. **Orphaned pause.** After a reorg, a pause that was only in the losing chain used to disappear from the node. Fixed in this branch: orphaned txs go back to the mempool (`p2p_http._requeue_orphaned_txs`). Still: after sending, check `/pause_status` on two or more nodes about 10 blocks later.
4. **Key rotation (F8).** "Publish a new PAUSE_PUBKEY in a release" breaks replay: old blocks that hold pauses signed by the old key become invalid for the new code. A real rotation needs a key-by-height list or an on-chain rotation tx, ideally 2-of-3.
5. **A leaked pause key can pause for ever** (unlimited renewals, each lasting 10,080 blocks). It cannot move coins. The fix is a rotation path (above) and, optionally, a renewal cap.
6. **Expiry is fail-open.** If the key is lost during an incident, payouts restart after 10,080 blocks (about 7 days).
7. **Vesting keeps running** during a pause; only new epoch payouts stop.
8. No written key backup / loss procedure, and no cross-node verification checklist in the plan. Suggested checklist after any pause tx: `/pause_status` on 2+ independent nodes shows the same `paused`, `nonce` and `expires_at`.
