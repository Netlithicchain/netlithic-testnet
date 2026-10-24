# Runbook: node stuck after a long split or an eclipse (review F4)

From block 5000 a node never rolls back more than **720 blocks (~12 h)**. A node cut off for longer
(network outage, or an attacker feeding it only their own peers) stays on its own branch.

**Symptoms:** the log repeats `reorg of N blocks exceeds limit 720 ... peer advertises a chain we refuse`.
`/status` height or tip differs from the explorer / founder server.

**Recovery (operator, on their own machine):**
1. Stop the node.
2. Check the correct tip from at least two sources you trust (the explorer and the founder server's `/status`),
   then compare the `latest_hash` values.
3. Move `chain.json` aside (`mv chain.json chain.json.bak-DATE`). Leave the wallet files alone.
4. Edit `peers.json` so it lists only known-good peers (founder server and the published seeds).
5. Start the node. It syncs from genesis, and the reorg limit doesn't apply to a fresh chain.
6. Confirm `/status` `latest_hash` matches the explorer.

**Fresh-node eclipse:** a brand-new node only trusts its peer list, and the checkpoints
(see CHECKPOINT_SIGNING.md). Always start with the published seeds.
**Alerting:** the reorg alert (ops) should fire on repeated refusals. A node whose peers advertise more
work than it accepts gets checked by a person.
