"""PROPOSAL pending founder approval -- NOT ACTIVE. Time-based values for block-count rules.

Before 2026-10-23 every rule was a block count (100-block reorg limit, 1008-block pause expiry,
144-block address wait and testnet payout epoch). Before block 5000 the chain makes
~1 block per 10 minutes (founder timer), after it LWMA targets 60 s. The same block
count therefore means very different real time before and after the fork.
These constants state each rule as a time and convert it at the 60 s target.
Nothing in core/pause/contributions/economics reads this module yet; apply() exists
only so tests and drills can try the values. See docs/BLOCK_TIME_AUDIT.md.
"""
PROPOSAL_ACTIVE = True              # all four values approved 2026-10-23 and set in the modules
TARGET_BLOCK_SECONDS = 60           # == core.LWMA_TARGET_BLOCK_TIME (checked in tests)

HOUR = 3600
DAY = 24 * HOUR

# rule -> intended real time (seconds)
REORG_LIMIT_SECONDS = 12 * HOUR          # APPROVED 2026-10-23 -> 720, now in core
PAUSE_EXPIRY_SECONDS = 7 * DAY           # founder: "about 7 days"
PAYOUT_EPOCH_SECONDS = 1 * DAY           # 144 blocks was chosen as ~1 day at 10-min blocks
MIN_ADDRESS_AGE_SECONDS = 1 * DAY        # APPROVED 2026-10-23 -> 1440, now in contributions


def blocks_for(seconds: int, block_seconds: int = TARGET_BLOCK_SECONDS) -> int:
    """Blocks covering `seconds` at the target pace (rounded up, at least 1)."""
    return max(1, -(-int(seconds) // int(block_seconds)))


PROPOSED_MAX_REORG_DEPTH = blocks_for(REORG_LIMIT_SECONDS)              # 720 (active)
PROPOSED_PAUSE_EXPIRY_BLOCKS = blocks_for(PAUSE_EXPIRY_SECONDS)         # 10080 (active)
PROPOSED_PAYOUT_EPOCH_BLOCKS = blocks_for(PAYOUT_EPOCH_SECONDS)         # 1440 (active)
PROPOSED_MIN_ADDRESS_AGE_BLOCKS = blocks_for(MIN_ADDRESS_AGE_SECONDS)   # 1440 (active)


def apply():
    """Patch the live module constants to the proposed values (tests/drills only)."""
    import core, pause, contributions, economics
    core.MAX_REORG_DEPTH = PROPOSED_MAX_REORG_DEPTH
    pause.PAUSE_EXPIRY_BLOCKS = PROPOSED_PAUSE_EXPIRY_BLOCKS
    contributions.MIN_ADDRESS_AGE_BLOCKS = PROPOSED_MIN_ADDRESS_AGE_BLOCKS
    economics.TESTNET_SHORT_EPOCHS = {
        cid: (h, PROPOSED_PAYOUT_EPOCH_BLOCKS) for cid, (h, _) in economics.TESTNET_SHORT_EPOCHS.items()}
