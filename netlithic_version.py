"""Single source of the Netlithic software version.

0.5.0 = the block-5000 security fork release (locked, approved 2026-10-23).
A node is "fork-ready" when it enforces the rules that switch on at
SECURITY_FORK_HEIGHT; /status reports both so the explorer can count upgraded nodes.
"""
VERSION = "0.5.0"
FORK_READY_HEIGHT = 5000   # must equal core.SECURITY_FORK_HEIGHT (checked in tests)


def version_string(component: str = "netlithic") -> str:
    return f"{component} {VERSION}"


# ---- Version gate (review F3) ----
# Builds older than MIN_PEER_VERSION don't enforce the block-5000 rules and
# will fork off there. Before UPGRADE_DEADLINE_HEIGHT we still peer with them
# but flag "upgrade needed"; from that height on, /hello refuses them.
MIN_PEER_VERSION = "0.5.0"
UPGRADE_DEADLINE_HEIGHT = FORK_READY_HEIGHT - 100   # ~4900: ~100 min before the fork at 60 s


def parse_version(v) -> tuple:
    """'0.5.0' -> (0, 5, 0). Missing/garbled versions (pre-0.5.0 builds had no
    version field at all) parse as (0, 0, 0), i.e. outdated."""
    try:
        parts = str(v).strip().split("-")[0].split(".")
        nums = tuple(int(p) for p in parts[:3])
        if len(nums) == 0 or any(n < 0 for n in nums):
            return (0, 0, 0)
        return nums + (0,) * (3 - len(nums))
    except (ValueError, TypeError, AttributeError):
        return (0, 0, 0)


def is_outdated(v, minimum: str = MIN_PEER_VERSION) -> bool:
    return parse_version(v) < parse_version(minimum)
