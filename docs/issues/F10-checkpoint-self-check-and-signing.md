# F10: Node self-check against checkpoints on load; signed checkpoints

**Severity:** Medium, non-blocking (Security Reviewer, PR #1). **Status:** open.

1. **Self-check on load.** Checkpoints are only applied when a chain is *replaced*. A node whose local chain.json already disagrees with a checkpoint (for example after an eclipse, or a copied data folder) keeps running on it without complaint. At startup, after `init_checkpoints()`, compare every checkpoint at or below the local height with the local chain:
   - On a mismatch, log an error and either refuse to start or quarantine the chain and resync from seeds. ECLIPSE_RECOVERY.md has the steps.
   - Expose the result in `/status` as `checkpoints_ok`.
2. **Signed checkpoints (later).** Today the checkpoints are trusted because they ship with the release. Follow CHECKPOINT_SIGNING.md: sign checkpoint sets with a release key, verify the signature at load, and allow a signed checkpoint to be distributed outside a release.

## Acceptance
- A test where the local chain has a block 400 that doesn't match the checkpoint, and the node refuses or quarantines it at startup.
