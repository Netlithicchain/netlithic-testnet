# Reorg rule (active from block 5000)

**Rule:** a node never replaces more than **720 blocks** (`MAX_REORG_DEPTH = 720`, about 12 hours at 60-second blocks; approved by Aric 2026-10-23, was 100)
of its own chain, even if a peer offers a chain with more work. It also never
replaces a block listed in `config/checkpoints.json`. Shorter forks (1–720 blocks)
are resolved normally by most work.

Why: a deep reorg is how a hidden-chain attack rewrites history. Wait for the
confirmations in [CONFIRMATIONS.md](CONFIRMATIONS.md) before treating coins as received.

Cost: after a network split lasting more than 720 blocks (~12 h), the two sides will not
merge on their own. Nodes on the losing side stay stuck until their operator acts.

## How to tell you are on the wrong side

- Your node's `/status` height or tip hash differs from the public explorer and
  from most other nodes for a long time, and the gap does not close.
- The node log shows "reorg too deep" / "refused" messages when syncing.
- The founder announces the correct tip (height + hash) in the usual channel.

## Recovery: checkpoint and resync

1. **Confirm the correct chain.** Get the announced correct block (height and hash)
   from the founder announcement. Check it against the public explorer.
2. **Stop your node** (close the desktop app, Ctrl+C, or
   `systemctl --user stop netlithic-node`).
3. **Back up your data folder** (the `--data-dir`, which holds `chain.json`).
   Do not touch or share any wallet files. Your coins are not in `chain.json`.
4. **Add the checkpoint.** Put the announced block in `config/checkpoints.json`:
   `{"5123": "<announced hash>"}`. Updating the software usually ships this file.
5. **Remove the bad chain.** Move `chain.json` (and `mempool.json`) out of the
   data folder. Keep `peers.json`.
6. **Start the node.** It re-downloads the chain from peers. It will only accept a chain
   that contains the checkpoint block.
7. **Check.** `/status` height and tip should match the explorer. Transactions you
   made only on the wrong side are not on the correct chain. Send them again if needed.

The founder server and node 2 follow the same steps. Whoever is on the correct side
publishes the checkpoint first.

**Since the PR #1 review:** depth is checked *before* full validation (cheap refusal). A malformed
`config/checkpoints.json` stops the node. Recovery runbook: [ECLIPSE_RECOVERY.md](ECLIPSE_RECOVERY.md).
