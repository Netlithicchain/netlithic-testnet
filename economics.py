"""Netlithic supply rules: fixed max supply, genesis allocations, block-reward schedule.

All on-chain amounts are integers in base units. 1 NLT = 10**DECIMALS base units.
"""
import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Dict, Tuple

DECIMALS = 8
BASE_UNITS_PER_NLT = 10 ** DECIMALS
MAX_SUPPLY_NLT = 1_000_000_000
MAX_SUPPLY = MAX_SUPPLY_NLT * BASE_UNITS_PER_NLT  # 10**17 base units, fixed in code

# Shares of MAX_SUPPLY, in percent. The miner bucket is NOT minted at genesis:
# it is paid out gradually as block rewards (see block_reward()).
BUCKET_PERCENT = {"community": 60, "miners": 20, "treasury": 15, "reserve": 5}
GENESIS_BUCKETS = ("community", "treasury", "reserve")  # minted at genesis, locked
MINER_BUCKET = MAX_SUPPLY * BUCKET_PERCENT["miners"] // 100  # 200M NLT

# Block reward schedule: 10 NLT per block, halving every 10,000,000 blocks.
# Sum of the halving series = 2 * 10 NLT * 10M = 200M NLT = MINER_BUCKET,
# and block_reward() additionally clamps to what is left, so it can never exceed it.
INITIAL_BLOCK_REWARD = 10 * BASE_UNITS_PER_NLT
HALVING_INTERVAL_BLOCKS = 10_000_000

# ---- Proposed defaults for time locks and community payouts (the founder to confirm) ----
BLOCKS_PER_MONTH = 175_200  # 30.4 days of 15-second blocks
# bucket -> (unlock start height, number of equal monthly unlocks)
DEFAULT_UNLOCKS = {
    "treasury": (0, 48),                      # incl. ops/dev fund: monthly over 4 years
    "reserve": (12 * BLOCKS_PER_MONTH, 72),   # 1-year cliff, then monthly over 6 years
    "community": (0, 240),                    # monthly over 20 years (2.5M NLT/month)
}
DEFAULT_EPOCH_BLOCKS = BLOCKS_PER_MONTH       # community payout once a month
DEFAULT_PAYOUT_CAP_NLT = 25_000               # per address per epoch (1% of a month's unlock)
DEFAULT_VESTING_EPOCHS = 6                    # payouts vest linearly over 6 epochs
APPROVAL_MODES = ("none", "multisig", "single_key_dev")   # treasury/reserve spend approval
DEFAULT_MULTISIG_THRESHOLD = 3
DEFAULT_MULTISIG_KEYHOLDERS = 5
DEFAULT_MIN_FEE = 10_000            # base units = 0.0001 NLT (proposal)
DEV_CHAIN_PREFIX = "netlithic-dev"   # dev-only features are refused on any other chain_id

# Contribution points are integers in tenths, so weights like 2.5 stay exact.
POINT_SCALE = 10
DEFAULT_POINTS_PER_BLOCK_TENTHS = 25   # 2.5 points per (non-empty) mined block
DEFAULT_POINTS_PER_TX_TENTHS = 10      # 1 point per transfer sent
# Anti-farming (proposals): a transfer earns points only if amount >= MIN_POINT_TRANSFER,
# it is not to the sender itself, and at most PAIR_CAP_PER_EPOCH transfers per
# sender->recipient pair per epoch (epoch = block index // epoch_blocks) earn points.
MIN_POINT_TRANSFER = 1 * BASE_UNITS_PER_NLT
PAIR_CAP_PER_EPOCH = 10

# ---- Testnet-only short payout epochs (a code rule, NOT a config field) ----
# chain_id -> (fork_height, short_epoch_blocks). From fork_height on, community payout
# epochs on that chain are short_epoch_blocks long, so a payout can be shown soon.
# It lives in code (not in Economics fields), so params_hash() and the genesis block
# are unchanged; every node running this code applies it at the same height.
# Only chains whose chain_id starts with DEV_CHAIN_PREFIX can use it; any other
# chain_id (mainnet) is refused below and keeps the normal 175,200-block epoch.
# Aric confirmed 2026-10-22: fork at block 5000 (same height the founder server
# stops mining and the difficulty fork activates), epoch 144 blocks
# (~1 day at the founder miner's 10-minute pace; ~2.4 h at 1-minute blocks).
# Updated by Aric 2026-10-23: 1440 blocks (~1 day at 60 s). First payout at block 6440.
# EXPLICIT chain_id dependency (review note): the 1440-block daily period
# applies ONLY when config/genesis_config.json has chain_id == SHORT_EPOCH_CHAIN_ID.
# Any other chain_id (a renamed testnet, mainnet) silently keeps the normal
# 175,200-block epochs. If the testnet chain_id ever changes, update this
# constant in the same release (test_short_epoch_bound_to_shipped_chain_id in tests/test_review_fixes.py guards it).
SHORT_EPOCH_CHAIN_ID = "netlithic-dev-1"
SHORT_EPOCH_START_HEIGHT = 5000
SHORT_EPOCH_BLOCKS = 1440
TESTNET_SHORT_EPOCHS = {SHORT_EPOCH_CHAIN_ID: (SHORT_EPOCH_START_HEIGHT, SHORT_EPOCH_BLOCKS)}


CONFIG_PATH = os.environ.get(
    "NETLITHIC_GENESIS_CONFIG",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "genesis_config.json"),
)


def is_dev_chain(chain_id: str) -> bool:
    return str(chain_id).startswith(DEV_CHAIN_PREFIX)


def format_points(tenths: int) -> str:
    whole, frac = divmod(int(tenths), POINT_SCALE)
    return f"{whole}.{frac}" if frac else str(whole)


def placeholder_address(bucket: str) -> str:
    """Deterministic stand-in address for an unconfigured bucket. It is a hash of
    a label, not of any public key, so nobody holds a key for it."""
    return hashlib.sha256(f"netlithic-unconfigured-placeholder:{bucket}".encode()).hexdigest()[:32]


@dataclass(frozen=True)
class Economics:
    allocations: Dict[str, str]          # bucket -> address (genesis buckets only)
    placeholders: tuple = ()             # buckets still on placeholder addresses
    genesis_timestamp: float = 1700000000.0
    miner_bucket: int = MINER_BUCKET
    initial_reward: int = INITIAL_BLOCK_REWARD
    halving_interval: int = HALVING_INTERVAL_BLOCKS
    max_supply: int = MAX_SUPPLY
    blocks_per_month: int = BLOCKS_PER_MONTH
    unlocks: Dict[str, Tuple[int, int]] = field(default_factory=lambda: dict(DEFAULT_UNLOCKS))
    epoch_blocks: int = DEFAULT_EPOCH_BLOCKS
    payout_cap: int = DEFAULT_PAYOUT_CAP_NLT * BASE_UNITS_PER_NLT
    vesting_epochs: int = DEFAULT_VESTING_EPOCHS
    # "none": treasury/reserve can't spend at all (default until governance exists).
    # "single_key_dev": the address key alone may spend what's unlocked. DEV/TEST ONLY,
    # a stand-in for a future multisig/governance approval, not a real safeguard.
    # Default "none": no treasury/reserve spends at all. "multisig" only when explicitly
    # configured with real keyholders (built for when independent contributors join).
    spend_approval: str = "none"
    multisig_threshold: int = DEFAULT_MULTISIG_THRESHOLD
    keyholders: Tuple[str, ...] = ()
    points_per_block: int = DEFAULT_POINTS_PER_BLOCK_TENTHS
    points_per_tx: int = DEFAULT_POINTS_PER_TX_TENTHS
    min_point_transfer: int = MIN_POINT_TRANSFER
    point_pair_cap: int = PAIR_CAP_PER_EPOCH
    chain_id: str = "netlithic-dev-1"
    min_fee: int = DEFAULT_MIN_FEE

    def __hash__(self):
        return hash(self.params_hash())

    def params_hash(self) -> str:
        """Hash of every rule that affects consensus; stored in the genesis block,
        so nodes with different settings are on different chains."""
        d = asdict(self)
        d.pop("placeholders", None)
        d["unlocks"] = {k: list(v) for k, v in sorted(self.unlocks.items())}
        return hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()

    # ---- time locks ----
    def unlocked(self, bucket: str, height: int) -> int:
        """Amount of a genesis bucket released by `height` (linear monthly steps)."""
        total = self.allocation_amount(bucket)
        start, months = self.unlocks[bucket]
        if height < start:
            return 0
        steps = min(months, (height - start) // self.blocks_per_month + 1)
        return total * steps // months

    def still_locked(self, bucket: str, height: int) -> int:
        return self.allocation_amount(bucket) - self.unlocked(bucket, height)

    # ---- community payouts ----
    def short_epoch(self):
        """(fork_height, length) of the testnet short-epoch rule, or None (mainnet)."""
        if not is_dev_chain(self.chain_id):
            return None
        rule = TESTNET_SHORT_EPOCHS.get(self.chain_id)
        if not rule:
            return None
        fork, length = int(rule[0]), int(rule[1])
        return (fork, length) if fork > 0 and length > 0 else None

    def epoch_length_at(self, height: int) -> int:
        se = self.short_epoch()
        return se[1] if se and height >= se[0] else self.epoch_blocks

    def epoch_id(self, height: int) -> int:
        """Epoch number of the block at `height` (used for payouts and the pair cap)."""
        se = self.short_epoch()
        if not se or height < se[0]:
            return height // self.epoch_blocks
        fork, length = se
        return fork // self.epoch_blocks + 1 + (height - fork) // length

    @property
    def vesting_blocks(self) -> int:
        return self.vesting_epochs * self.epoch_blocks

    def vesting_blocks_at(self, height: int) -> int:
        return self.vesting_epochs * self.epoch_length_at(height)

    def is_epoch_block(self, height: int) -> bool:
        se = self.short_epoch()
        if se and height >= se[0]:
            return height > se[0] and (height - se[0]) % se[1] == 0
        return height > 0 and height % self.epoch_blocks == 0

    def next_epoch_height(self, height: int) -> int:
        """First epoch (payout) block strictly after `height`."""
        se = self.short_epoch()
        if se:
            fork, length = se
            if height < fork:
                cand = (height // self.epoch_blocks + 1) * self.epoch_blocks
                return cand if cand < fork else fork + length
            return fork + ((height - fork) // length + 1) * length
        return (height // self.epoch_blocks + 1) * self.epoch_blocks

    def allocation_amount(self, bucket: str) -> int:
        return self.max_supply * BUCKET_PERCENT[bucket] // 100

    @property
    def locked_addresses(self) -> frozenset:
        return frozenset(self.allocations.values())

    def scheduled_reward(self, height: int) -> int:
        if height < 1:
            return 0
        era = (height - 1) // self.halving_interval
        return self.initial_reward >> era if era < 64 else 0

    def block_reward(self, height: int, rewards_paid_before: int) -> int:
        """Reward for the block at `height`, never more than what's left in the miner bucket."""
        remaining = max(0, self.miner_bucket - rewards_paid_before)
        return min(self.scheduled_reward(height), remaining)


def load_economics(path: str = None) -> Economics:
    path = path or CONFIG_PATH
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    if int(cfg.get("decimals", -1)) != DECIMALS:
        raise ValueError(f"genesis config decimals must be {DECIMALS}")
    if int(cfg.get("max_supply_nlt", -1)) != MAX_SUPPLY_NLT:
        raise ValueError(f"genesis config max_supply_nlt must be {MAX_SUPPLY_NLT}")

    allocs = cfg.get("allocations", {})
    if {k: int(v.get("percent", -1)) for k, v in allocs.items()} != {
        b: BUCKET_PERCENT[b] for b in GENESIS_BUCKETS
    }:
        raise ValueError("genesis config allocations must be community 60, treasury 15, reserve 5")

    addresses, placeholders = {}, []
    for bucket in GENESIS_BUCKETS:
        entry = allocs[bucket]
        addr = str(entry.get("address", "")).strip().lower()
        if entry.get("placeholder"):
            if addr != placeholder_address(bucket):
                raise ValueError(f"{bucket}: placeholder flag set but address is not the standard placeholder")
            placeholders.append(bucket)
        if len(addr) != 32 or any(c not in "0123456789abcdef" for c in addr):
            raise ValueError(f"{bucket}: invalid address")
        addresses[bucket] = addr
    if len(set(addresses.values())) != len(addresses):
        raise ValueError("allocation addresses must be distinct")

    rewards = cfg.get("miner_rewards", {})
    if (int(rewards.get("initial_reward_nlt", -1)) * BASE_UNITS_PER_NLT != INITIAL_BLOCK_REWARD
            or int(rewards.get("halving_interval_blocks", -1)) != HALVING_INTERVAL_BLOCKS):
        raise ValueError("miner_rewards in config do not match the schedule in code")

    bpm = int(cfg.get("time", {}).get("blocks_per_month", BLOCKS_PER_MONTH))
    unlocks = {}
    for bucket in GENESIS_BUCKETS:
        u = allocs[bucket].get("unlock", {})
        start = int(u.get("start_month", DEFAULT_UNLOCKS[bucket][0] // BLOCKS_PER_MONTH)) * bpm
        months = int(u.get("months", DEFAULT_UNLOCKS[bucket][1]))
        if start < 0 or months < 1:
            raise ValueError(f"{bucket}: invalid unlock schedule")
        unlocks[bucket] = (start, months)

    cp = cfg.get("community_payouts", {})
    epoch_blocks = int(cp.get("epoch_blocks", DEFAULT_EPOCH_BLOCKS))
    cap = int(cp.get("per_address_cap_nlt", DEFAULT_PAYOUT_CAP_NLT)) * BASE_UNITS_PER_NLT
    vesting_epochs = int(cp.get("vesting_epochs", DEFAULT_VESTING_EPOCHS))
    if bpm < 1 or epoch_blocks < 1 or cap < 1 or vesting_epochs < 1:
        raise ValueError("invalid time/community_payouts settings")

    chain_id = cfg.get("chain_id")
    if not isinstance(chain_id, str) or not chain_id.strip():
        raise ValueError("genesis config must set chain_id")
    chain_id = chain_id.strip()
    min_fee = int(cfg.get("fees", {}).get("min_fee_base_units", DEFAULT_MIN_FEE))
    if min_fee < 0:
        raise ValueError("fees.min_fee_base_units must be >= 0")
    dev = is_dev_chain(chain_id)

    approval = cfg.get("treasury_spend_approval", {})
    mode = approval.get("mode", "none")
    if mode not in APPROVAL_MODES:
        raise ValueError(f"treasury_spend_approval.mode must be one of {APPROVAL_MODES}")
    if mode == "single_key_dev" and not dev:
        raise ValueError(f"single_key_dev approval is only allowed on dev chains ({DEV_CHAIN_PREFIX}*)")

    threshold = int(approval.get("threshold", DEFAULT_MULTISIG_THRESHOLD))
    keyholders = []
    holders = approval.get("keyholders", [])
    if holders and mode != "multisig":
        raise ValueError("keyholders are listed but mode is not multisig; remove them or set mode")
    if mode == "multisig":
        non_founders = 0
        for i, kh in enumerate(holders, start=1):
            pub = str(kh.get("pubkey", "")).strip().lower()
            if len(pub) != 64 or any(ch not in "0123456789abcdef" for ch in pub):
                raise ValueError(f"keyholder {i}: invalid pubkey")
            if kh.get("test_key") and not dev:
                raise ValueError(f"keyholder {i}: test keys are only allowed on dev chains ({DEV_CHAIN_PREFIX}*)")
            if not kh.get("founder"):
                non_founders += 1
            keyholders.append(pub)
        if len(set(keyholders)) != len(keyholders):
            raise ValueError("keyholder pubkeys must be distinct")
        if not 1 <= threshold <= len(keyholders):
            raise ValueError("multisig threshold must be between 1 and the number of keyholders")
        if non_founders < 2:
            raise ValueError("at least two keyholders must not be the founder")

    pts = cfg.get("contribution_points", {})
    per_block = int(pts.get("per_block_mined_tenths", DEFAULT_POINTS_PER_BLOCK_TENTHS))
    per_tx = int(pts.get("per_tx_sent_tenths", DEFAULT_POINTS_PER_TX_TENTHS))
    if per_block < 0 or per_tx < 0:
        raise ValueError("contribution point weights must be >= 0")
    min_tx = int(pts.get("min_transfer_base_units", MIN_POINT_TRANSFER))
    pair_cap = int(pts.get("pair_cap_per_epoch", PAIR_CAP_PER_EPOCH))
    if min_tx < 1 or pair_cap < 0:
        raise ValueError("min_transfer_base_units must be >= 1 and pair_cap_per_epoch >= 0")

    return Economics(
        allocations=addresses,
        placeholders=tuple(placeholders),
        genesis_timestamp=float(cfg.get("genesis_timestamp", 1700000000.0)),
        blocks_per_month=bpm,
        unlocks=unlocks,
        epoch_blocks=epoch_blocks,
        payout_cap=cap,
        vesting_epochs=vesting_epochs,
        spend_approval=mode,
        multisig_threshold=threshold,
        keyholders=tuple(keyholders),
        points_per_block=per_block,
        points_per_tx=per_tx,
        min_point_transfer=min_tx,
        point_pair_cap=pair_cap,
        chain_id=chain_id,
        min_fee=min_fee,
    )


def to_nlt(base_units: int) -> str:
    """Format base units as an exact NLT string, e.g. 1050000000 -> '10.5'."""
    sign = "-" if base_units < 0 else ""
    whole, frac = divmod(abs(int(base_units)), BASE_UNITS_PER_NLT)
    s = str(whole)
    if frac:
        s += "." + str(frac).rjust(DECIMALS, "0").rstrip("0")
    return sign + s


def parse_nlt(text: str) -> int:
    """Parse an NLT amount like '4' or '0.25' into base units (max 8 decimals)."""
    try:
        d = Decimal(str(text).strip())
    except InvalidOperation:
        raise ValueError(f"Not a number: {text!r}")
    units = d * BASE_UNITS_PER_NLT
    if units != units.to_integral_value() or d <= 0:
        raise ValueError(f"Amount must be positive with at most {DECIMALS} decimals: {text!r}")
    return int(units)
