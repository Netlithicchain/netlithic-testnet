# Checkpoints: what ships now and the signing proposal (review F4)

**Now (0.5.0):** `config/checkpoints.json` ships inside the release, next to the code. It is empty on
purpose, because no hash has yet been checked against two independent nodes. It is loaded from the
code's folder, whatever the working directory, or from `NETLITHIC_CHECKPOINTS_FILE`. A malformed file
(bad JSON, or a value that isn't a 64-hex hash) **stops the node at startup** with `CheckpointError`.
Before, it silently ran with no checkpoints.

**Adding a checkpoint (each release):**
1. Pick a height at least 1,440 blocks below the tip.
2. Read the hash at that height from the founder server *and* from one independent node. They must match.
3. Add `"height": "hash"` and commit it in a signed commit (Aric's hardware key) in the release PR.
   The signed tag `v0.x.y` covers it.

**Proposal (not built):** a detached Ed25519 signature `config/checkpoints.json.sig` from an offline
checkpoint key (separate from the pause key, made with the same ceremony). Nodes would verify it
against a pubkey embedded in the release and refuse unsigned or invalid files. That way a tampered file
on disk can't steer a fresh node. Decision for Aric.
