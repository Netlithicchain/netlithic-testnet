# F1b: Peer-URL SSRF gaps after F1 (R1-R3)

**Severity:** High (R1), Medium (R2, R3). **Status:** fixed in PR #1 (e90c938), merged into `release-5000` as c34e639. Follow-up: F9.

The F1 fix only gated the `/receive_block` sync trigger. The second review found three more routes:

- **R1:** `/hello`, admin `/add_peer` and `_try_add_peer` (which also covers `--peer`, `peers.json` and peer discovery) accepted private, loopback and link-local URLs. `/hello` is open to anyone, and it fetches `<url>/status` before adding the peer. That made it a blind SSRF into the node's own network, including 169.254.169.254 cloud metadata.
- **R2:** DNS rebinding. Even with a resolve-time check, a hostname can resolve to a public IP during the check and to 127.0.0.1 during the fetch.
- **R3:** urllib followed redirects, so a public peer could answer with a 302 to an internal address.

## Fix
- `peer_address_allowed()` runs in `_try_add_peer`, `handle_hello` and `handle_add_peer` before any network fetch. A URL is allowed only if every address its host resolves to is public.
- `netutil.urlopen(..., allow_ip=...)` checks `sock.getpeername()` right after connecting, before any request bytes go out. Every peer fetch (`_get_json` and `_post_json`) passes `fetch_ip_allowed`. Environment proxies are disabled for guarded fetches, so the check sees the real peer.
- `netutil.urlopen` never follows redirects. A 3xx response raises `HTTPError`.
- `NETLITHIC_ALLOW_PRIVATE_PEERS=1` turns all of this off, for local or LAN test networks only. The test suite sets it for its 127.0.0.1 nodes.

## Tests
`tests/test_review_r_fixes.py`
