# F9: Explorer peer fetch guard and /hello DNS off the request thread

**Severity:** Low/Medium, non-blocking (Security Reviewer, PR #1). **Status:** open.

1. **explorer.py:90.** `node_statuses()` fetches `<peer>/status` for every URL in the node's peer list using plain `urlopen`. Since F1b, nodes only accept public peers, but the explorer should not have to trust that. Pass `allow_ip=netutil.is_public_ip` (the same connect-time check `p2p_http` uses), so a bad peer list can't point the explorer at internal addresses.
2. **/hello DNS lookup on the request thread.** `peer_address_allowed()` calls `getaddrinfo` inside `handle_hello`, which is open to anyone. A slow or attacker-controlled DNS name holds up a server thread for the whole resolver timeout. Options: resolve with a short timeout in a small bounded worker pool; or reject hostnames in `/hello` and accept only IP literals, keeping names for operator-configured peers; and cap concurrent /hello lookups per client IP.

## Acceptance
- A test showing the explorer refuses a peer that connects to a loopback address.
- A test showing a slow resolver can't block other requests beyond a fixed bound.
