# Netlithic threat model (v0, pre-audit)

Scope: chain rules (`core.py`, `economics.py`, `contributions.py`, `wallet.py`), the node
(`p2p_http.py`), the founder relay (`ops/founder_miner.py`), the web wallet (`webwallet.py`,
`webwallet/`) and the desktop app (`desktop/`). Testnet coins have no value. This doc doubles as
the scope for the external audit.

| Attacker goal | What defends it today (file / function) | Gaps / notes |
|---|---|---|
| **Double-spend** (spend the same coins twice) | Per-sender strict nonces: `Blockchain.validate_signed_tx` (expects `confirmed+1`), `get_confirmed_nonce`, `get_pending_nonce`, `validate_candidate_tx_against_chain_and_mempool`; balances re-checked on a running copy in `validate_block_transactions`. | A double-spend via a **chain rewrite** is still possible against anyone who accepts too few confirmations (see next row). No published confirmation guidance yet. |
| **Fork / deep reorg** | Heaviest-work rule: `replace_chain` only takes a chain with more `chain_work`; `is_valid_chain` re-validates every block from a matching genesis; `block_reject_reason` checks index, prev hash, PoW, hash. | **No reorg depth limit or checkpoints**: any heavier valid chain replaces ours, however deep. Small network = cheap 51%. `replace_chain` re-validates the whole chain each time (CPU cost grows with height). |
| **Draining the payout pool** | Pool can only pay via protocol epochs: `validate_signed_tx` rejects transfers from the community address; payouts are recomputed and must match exactly (`expected_payouts`), capped per address (`payout_cap`, 25k), vested (`unvested`, `restricted_amounts`); locked buckets need multisig (`locked_addresses`, `validate_multisig_tx`); `MAX_SUPPLY` check in `is_valid_chain`. | Pool share is pro rata to points, so whoever farms the most points takes up to the cap. Multisig is built but off (no keyholders yet). |
| **Points farming** | `contributions.points_from_blocks`: 0 points for empty self-mined blocks (`is_empty_self_mined`), self-sends and tiny transfers earn nothing (`min_point_transfer`), per-pair cap per epoch (`point_pair_cap`); locked addresses excluded in `expected_payouts`. | **Sybil**: one person with many addresses can send capped transfers between them and mine their own blocks with those transfers. Only cost is fees + hashing. Main open economic risk. |
| **Signature replay** | Ed25519 over canonical payload incl. `chain_id` (`wallet.verify_signed_tx`); chain_id must match (`validate_signed_tx`); nonce makes each tx one-time; `pubkey_to_address` must equal sender. | Replay across a future chain that reuses `netlithic-dev-1` is possible; chain_id should change on any relaunch. |
| **Timestamp / difficulty manipulation** | Pre-5000: timestamp > previous block. Post-5000: > median of last 11 (`median_time_past`); max 120 s in future (`MAX_FUTURE_BLOCK_SECONDS`); LWMA with capped solve times and ±1.5x / 2/3 step limits (`lwma_next_work`); allowed set via `allowed_difficulties`. | `stall_work` lets a block that is ≥10 min late use 1/8 work; a miner can fake a late timestamp (up to the 120 s future window, and MTP lets old-looking times) — worth an audit look. Future limit depends on the node's own clock. |
| **DoS on nodes** | Body caps `MAX_POST_BYTES` (1 MB) / `MAX_FETCH_BYTES` (16 MB); `MAX_PEERS` 32; seen-cache dedupe (`_already_seen_block/_tx`, 120 s TTL); `MAX_BLOCK_TXS` 1000; admin endpoints need `X-Admin-Token` via `hmac.compare_digest` (`is_admin_request`). | **No per-IP rate limiting**, **no mempool size cap**, thread-per-request server (`ThreadingHTTPServer`) with no connection limit, peer list can be filled by anyone up to 32. Whole-chain `/chain` fetches get heavier as height grows. |
| **Founder over-mining** (post-5000) | `ops/founder_miner.py` relay-only with 30-min backstop, public `/explorer/founder` page. | This is a policy in the founder's own script, not a consensus rule — other nodes can't enforce it; transparency page is the check. |
| **Key theft / supply chain** | Keys never committed (`.gitignore`), admin token in env, desktop builds ship SHA-256 checksums. | Installer **unsigned**; no branch protection / signed commits yet; web wallet keeps keys in the browser (XSS = key loss). |

## Top gaps, in priority order
1. No reorg depth limit / checkpoints, and no confirmation guidance (51% is cheap on a tiny chain).
2. Sybil points farming via many addresses.
3. No rate limits or mempool cap on public nodes.
4. Stall-work rule plus timestamp freedom needs a focused review.
5. Unsigned installer and no repo protections.
