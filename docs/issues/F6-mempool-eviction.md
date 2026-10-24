# [security] Mempool full = 503 with no eviction; rescans under the global lock (review F6, Medium)
**Labels:** security, mempool, performance
Problem: when the mempool is at MAX_MEMPOOL, p2p_http.py returns 503 and evicts nothing. Minimum-fee spam from many funded addresses therefore blocks honest transfers. Balance and nonce checks rescan the whole chain while holding state_lock.
Proposed fix: evict by fee priority (drop the lowest fee per byte), add a per-sender pending cap, and keep indexed balance and nonce state updated on add_block/replace_chain.
Done when: a flood test shows a higher-fee transfer gets in when the mempool is full, and lock hold time is bounded.
