# [security] Rate-limit and peer-cap bypasses (review F5, Medium)
**Labels:** security, p2p, post-release
Problem:
- Buckets are keyed on the exact IP, so rotating IPv6 addresses inside one /64 gets past them.
- Going over MAX_TRACKED_IPS (10,000) clears *every* bucket.
- Loopback is exempt, so behind a local reverse proxy every client is exempt, and with no trusted-proxy setting there's no limiting at all.
- MAX_PEERS_PER_HOST compares hostname strings, so DNS aliases and multiple v6 addresses get past it. There is no peer eviction.
Proposed fix: key IPv6 on its /64, use LRU eviction instead of clear(), add a `NETLITHIC_TRUSTED_PROXIES` setting, cap peers per resolved /24 or /64, and add peer scoring and eviction.
Done when: tests cover v6 rotation, a 10k+ flood, a proxied deployment and alias peers.
