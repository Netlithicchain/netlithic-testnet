"""Single source of the Netlithic software version.

0.5.0 = the block-5000 security fork release (locked, approved 2026-10-23).
0.5.1 = 0.5.0 plus the pause public key (pause.PAUSE_PUBKEY) and the version gate raised to 0.5.1,
        because a node whose PAUSE_PUBKEY is empty rejects any block that carries a pause tx.
        0.5.1 must only be released with PAUSE_PUBKEY set (ops/set_pause_pubkey.py --check).
0.5.2 = 0.5.1 plus the points-v4 payout rules (20/80 pots, registry) and the review fixes (flat block points,
        registry key rotation and prospective removals, fail-closed /hello). The version gate is raised to 0.5.2
        so that a plain 0.5.1 node, which would split off at block 6440 (the first v4 epoch block that pays nothing
        while a plain 0.5.1 pays), is turned away at the handshake after UPGRADE_DEADLINE_HEIGHT.
0.5.3 = 0.5.2 plus the second and third security-review fixes (registry key handover must be co-signed by the new
        key, valid_until is capped at mined height + 10,080, a deep-reorg alert that junk chains cannot hide). The
        rules fingerprint changed after the 0.5.2 dev builds (registry_rules=3, registry_max_valid=10080), so
        EVERY build before 0.5.3 (0.5.0, 0.5.1 and the 0.5.2 dev builds, e.g. eb4e3d5) is unsupported: peers on them are
        refused from block 4900 and consensus splits from them at the first registry key rotation or registry
        transaction with a far-future valid_until. All nodes must run 0.5.3 before block 4900.
A node is "fork-ready" when it enforces the rules that switch on at
SECURITY_FORK_HEIGHT; /status reports both so the explorer can count upgraded nodes.

Peer gate (peer level only, NOT block validity): it decides who we talk to, never which blocks are valid. Rules:
  * from UPGRADE_DEADLINE_HEIGHT (4900) a peer must report version >= MIN_PEER_VERSION and the right rules fingerprint,
    both on /hello and when we re-check peers we already know, gossip-discovered peers and --peer/add_peer peers;
  * a pre-release such as 0.5.3-rc1 counts as OLDER than 0.5.3; garbled strings count as outdated;
  * BELOW block 4900 the gate is deliberately not enforced (old builds are still allowed in, and flagged "upgrade
    needed"): a fresh or resyncing node must be able to find peers, and it re-checks them once it passes 4900;
  * it is hygiene, not security: version and fingerprint are self-reported and can be copied. Only consensus (blocks
    rejected at 6440) is a real gate.
"""
import re

VERSION = "0.5.3"
FORK_READY_HEIGHT = 5000   # must equal core.SECURITY_FORK_HEIGHT (checked in tests)


def version_string(component: str = "netlithic") -> str:
    return f"{component} {VERSION}"


# ---- Version gate (review F3) ----
# Builds older than MIN_PEER_VERSION don't enforce the block-5000 rules, or (0.5.0) don't know the
# pause key and would reject the first pause block, so they would fork off. Before
# UPGRADE_DEADLINE_HEIGHT we still peer with them but flag "upgrade needed"; from that height on,
# /hello refuses new handshakes from them. This does not disconnect peers that are already
# connected, so it limits exposure and warns people; it is not a hard fork guard (docs/UPGRADE_RUNBOOK_0.5.1.md).
MIN_PEER_VERSION = "0.5.3"
UPGRADE_DEADLINE_HEIGHT = FORK_READY_HEIGHT - 100   # ~4900: about 16 hours before the fork (blocks before 5000 are ~10 min apart)


# The version string is only a claim; the rules fingerprint is a check (a node that computes payouts differently reports a
# different one). Before this height /hello still accepts a peer that reports no fingerprint (old builds that never had
# one); from this height on a peer WITHOUT a valid fingerprint is refused, so "just leave the field out" no longer
# gets past the handshake. Same height as the version deadline so one date has to be announced.
RULES_FIELD_REQUIRED_HEIGHT = UPGRADE_DEADLINE_HEIGHT


def rules_field_required(height: int) -> bool:
    return int(height) >= RULES_FIELD_REQUIRED_HEIGHT


_VERSION_RE = re.compile(r"^(\d{1,6})(?:\.(\d{1,6}))?(?:\.(\d{1,6}))?(-[0-9A-Za-z][0-9A-Za-z.\-]*)?$")


def parse_version(v) -> tuple:
    """'0.5.0' -> (0, 5, 0). Missing/garbled versions (pre-0.5.0 builds had no
    version field at all) parse as (0, 0, 0), i.e. outdated. A pre-release suffix is ignored here; use
    version_key() / is_outdated() when comparing, because a pre-release is OLDER than its release."""
    m = _VERSION_RE.match(str(v).strip()) if isinstance(v, str) else None
    if not m:
        return (0, 0, 0)
    return tuple(int(x) if x else 0 for x in m.groups()[:3])


def version_key(v) -> tuple:
    """Sortable key: (major, minor, patch, is_release). '0.5.3-rc1' sorts BEFORE '0.5.3' (semver: a pre-release is
    older than its release), so a release candidate does not satisfy a minimum of 0.5.3. Garbled -> lowest."""
    m = _VERSION_RE.match(str(v).strip()) if isinstance(v, str) else None
    if not m:
        return (0, 0, 0, 0)
    return parse_version(v) + (0 if m.group(4) else 1,)


def is_outdated(v, minimum: str = MIN_PEER_VERSION) -> bool:
    return version_key(v) < version_key(minimum)
