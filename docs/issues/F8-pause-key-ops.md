# [ops] Pause key: rotation needs a hard fork; renewals are unlimited (review F8, Low)
**Labels:** security, governance
Problem: PAUSE_PUBKEY is a hardcoded consensus constant, so rotating or revoking it needs a hard fork, and nodes that disagree on it split. One key can renew a pause forever (each renewal resets the 10,080-block expiry).
Proposed fix: run PAUSE_KEY_CEREMONY.md before block 5000 and publish the fingerprint. In a later fork, consider 2-of-3 keys, an on-chain key-rotation tx signed by the old key, and a cap on total renewals (e.g. 4 = ~28 days) before an upgrade is required.
Needs Aric's decision.
