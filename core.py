import copy
import hashlib
import json
import logging
import math
import os
import re
import stat
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import pause
import registry
from wallet import verify_signed_tx, pubkey_to_address, multisig_message, verify_signature
import contributions
import economics


TARGET_BLOCK_TIME = 15
DIFFICULTY_ADJUSTMENT_INTERVAL = 10
DEFAULT_DIFFICULTY = 3
MIN_DIFFICULTY = 2
MAX_DIFFICULTY_STEP = 1
BLOCK_REWARD = economics.INITIAL_BLOCK_REWARD  # base units; see economics.py for the schedule
MAX_SUPPLY = economics.MAX_SUPPLY
CHAIN_FILE = "chain.json"
MAX_FUTURE_BLOCK_SECONDS = 120
MAX_BLOCK_TXS = 1000  # signed transfers per block; miners fill it highest-fee first  # reject blocks timestamped >2 min ahead of our clock

# ---- Difficulty fork (LWMA) ----
# Blocks below LWMA_FORK_HEIGHT keep the original rule: `difficulty` = number of
# leading hex zeros, retargeted every 10 blocks. From LWMA_FORK_HEIGHT on,
# `difficulty` means expected hashes per block ("work"): a block is valid when
# int(hash, 16) * work < 2**256, and work is retargeted every block with a
# linearly weighted moving average (LWMA) of recent solve times. These are code
# constants, not genesis settings, so the genesis economics_sha256 is unchanged.
LWMA_FORK_HEIGHT = 5000
LWMA_TARGET_BLOCK_TIME = 60        # seconds per block after the fork
LWMA_WINDOW = 45                   # blocks in the average
LWMA_MIN_WORK = 256                # = old difficulty 2 (two leading hex zeros)
LWMA_MAX_STEP_UP = (3, 2)          # next work <= 1.5x previous
LWMA_MAX_STEP_DOWN = (2, 3)        # next work >= 2/3 previous
LWMA_MAX_SOLVETIME_FACTOR = 6      # one solve time counts as at most 6 targets
MTP_WINDOW = 11                    # median-time-past window (after the fork)
STALL_SECONDS = 10 * LWMA_TARGET_BLOCK_TIME  # a block this late may use emergency work
STALL_WORK_DIVISOR = 8
POW_LIMIT = 2 ** 256

# ---- Security fork (same height as LWMA; every node must upgrade before it) ----
# From SECURITY_FORK_HEIGHT: reorg depth limit, checkpoints, stricter stall rule,
# anti-farming v2 for points. See docs/THREAT_MODEL.md and docs/CONFIRMATIONS.md.
SECURITY_FORK_HEIGHT = 5000
POINTS_V4_HEIGHT = SECURITY_FORK_HEIGHT   # THE announced fork height for the points rules: slow release, 20/80 pots, 2% per-address cap, one-day payout lag.
# (v3 never shipped; v4 replaces it at the same height.)
# Same height as the security fork on purpose: the live leaderboard then shows the rules that will actually pay. Under v4 the
# first payout is at 7880 (6440 pays nothing); a node on plain 0.5.1 rules would pay at 6440 and split there.
MAX_REORG_DEPTH = 720              # node policy from 5000: never roll back more than this (~12 h at 60 s; set 2026-10-23)
# {"height": "hash"}; shipped with the release (may be empty). Resolved next to
# this file so it works whatever the node's working directory (desktop app).
CHECKPOINTS_FILE = os.environ.get(
    "NETLITHIC_CHECKPOINTS_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "checkpoints.json"),
)


def points_v3_rules_hash() -> str:
    """Fingerprint of every constant that decides who earns what under points v3. Two nodes with different values
    would compute different payouts (at 6440 for a plain 0.5.1 node, at 7880 for other v4 constant sets) and fork, so /status shows it and /hello refuses a mismatch."""
    import hashlib
    parts = [
        "rules=points-v4",
        "block_points=flat",   # H1 fix: every mined block earns block points from the fork on (no empty-block discount)
        f"fork_height={POINTS_V4_HEIGHT}",
        f"tx_pot_percent={contributions.TX_POT_PERCENT}",
        f"pot_cap_percent={contributions.POT_CAP_PERCENT}",
        f"payout_lag_periods={contributions.PAYOUT_LAG_PERIODS}",
        f"family_rule={int(contributions.FAMILY_RULE_ENABLED)}",
        f"family_pair_cap={contributions.FAMILY_PAIR_CAP}",
        f"max_base_per_point={contributions.MAX_BASE_PER_POINT}",
        f"min_point_transfer={contributions.MIN_POINT_TRANSFER}",
        # The issuer list only matters while the family rule is on; with it off, an edit there changes no payout.
        "issuers=" + (",".join(sorted(contributions.FAMILY_ISSUERS)) if contributions.FAMILY_RULE_ENABLED else "-"),
        # The payout pause switch: a node with a different pause key (or expiry / reason limit) would reject the block
        # that carries the first pause tx and fork off, so it must not pass /hello or the 4900 gate.
        f"pause_key={str(pause.PAUSE_PUBKEY or '').lower() or '-'}",
        f"pause_expiry={pause.PAUSE_EXPIRY_BLOCKS}",
        f"pause_max_reason={pause.MAX_REASON}",
    ]
    # Registered-contributors option (TEMPORARY, centralized): who may sign the list and every limit on it. With the
    # registry off these lines are absent, so a registry-disabled build has exactly the points-v4 fingerprint.
    if registry.REGISTRY_ENABLED:
        parts += [
            "registry=1",
            f"registry_key={registry.REGISTRY_PUBKEY.lower() or '-'}",
            f"registry_delay={registry.REGISTRY_DELAY_BLOCKS}",
            "registry_rules=3",   # rolling add/remove limits, prospective removals, key rotation (key checked + co-signed), valid_until (capped)
            f"registry_max_valid={registry.REGISTRY_MAX_VALID_BLOCKS}",
            f"registry_remove_delay={registry.REGISTRY_REMOVE_DELAY_BLOCKS}",
            f"registry_window={registry.REGISTRY_LIMIT_WINDOW}",
            f"registry_max_adds={registry.MAX_ADDS_PER_WINDOW}",
            f"registry_max_removes={registry.MAX_REMOVES_PER_WINDOW}",
            f"registry_rotation_delay={registry.REGISTRY_ROTATION_DELAY_BLOCKS}",
            f"registry_max_entries={registry.MAX_ENTRIES_PER_TX}/{registry.MAX_ACTIVE_ENTRIES}",
            f"registry_sunset={registry.REGISTRY_SUNSET_HEIGHT if registry.REGISTRY_SUNSET_HEIGHT is not None else '-'}",
        ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def is_points_v4(height: int) -> bool:
    return int(height) >= POINTS_V4_HEIGHT


is_points_v3 = is_points_v4   # old name, kept so older tests and tools keep working


def is_security_fork(index: int) -> bool:
    return int(index) >= SECURITY_FORK_HEIGHT


class CheckpointError(ValueError):
    """config/checkpoints.json exists but is unreadable or malformed."""


def load_checkpoints(path: str = None) -> Dict[int, str]:
    """Read {"height": "hash"} checkpoints. A missing file means "no
    checkpoints". A file that exists but can't be parsed is an operator error
    and raises CheckpointError (review F4): silently running with no
    checkpoints would hide a broken or tampered file."""
    path = path or CHECKPOINTS_FILE
    if not os.path.exists(path):
        # Review R4: warn, don't fail. The file ships with every release, so
        # a missing one means a damaged/partial install; refusing to start
        # would take nodes offline over a file that is currently empty
        # anyway. Revisit (fail hard) once signed checkpoints exist.
        log.warning("Checkpoint file %s is MISSING: running with NO checkpoints. "
                    "Reinstall the release or restore config/checkpoints.json.", path)
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise ValueError("top level must be an object")
        out = {}
        for k, v in raw.items():
            if str(k).startswith("_"):
                continue  # comment keys like "_readme"
            h = str(v)
            if not re.fullmatch(r"[0-9a-f]{64}", h):
                raise ValueError(f"checkpoint {k}: not a 64-hex block hash")
            out[int(k)] = h
        return out
    except (OSError, ValueError, TypeError) as e:
        log.error("Checkpoint file %s is invalid: %s", path, e)
        raise CheckpointError(f"invalid checkpoint file {path}: {e}") from e

log = logging.getLogger("netlithic.core")

_checkpoint_cache: Optional[Dict[int, str]] = None


def init_checkpoints(path: str = None) -> Dict[int, str]:
    """Load checkpoints once (node startup) and cache them (review R5).
    Raises CheckpointError on a malformed file, so the node won't start."""
    global _checkpoint_cache
    _checkpoint_cache = load_checkpoints(path)
    return _checkpoint_cache


def get_checkpoints() -> Dict[int, str]:
    """Cached checkpoints; loaded on first use if init_checkpoints() wasn't called."""
    if _checkpoint_cache is None:
        return init_checkpoints()
    return _checkpoint_cache


class PreviewBusy(RuntimeError):
    """Too many different /payout_preview computations are running; the HTTP layer answers 503."""


class CheckpointMismatch(RuntimeError):
    """The local chain disagrees with a shipped checkpoint (review F10)."""


CHECKPOINT_RESYNC_ENV = "NETLITHIC_CHECKPOINT_RESYNC"


def checkpoint_mismatches(chain, checkpoints: Optional[Dict[int, str]] = None) -> List[int]:
    """Heights at or below the local tip whose block hash differs from the
    checkpoint. `chain` is a list of Blocks or block dicts."""
    cps = get_checkpoints() if checkpoints is None else checkpoints
    bad = []
    for h, cp_hash in sorted(cps.items()):
        if h < len(chain):
            b = chain[h]
            got = b.get("hash") if isinstance(b, dict) else getattr(b, "hash", None)
            if got != cp_hash:
                bad.append(h)
    return bad


def quarantine_chain_file(reason_heights) -> str:
    """Move chain.json aside (never deleted) so the node resyncs from genesis,
    as in docs/ECLIPSE_RECOVERY.md. Wallet files are not touched."""
    import time as _t
    base = f"{CHAIN_FILE}.checkpoint-mismatch-{_t.strftime('%Y%m%d-%H%M%S')}"
    dest, n = base, 1
    while os.path.exists(dest):  # P5-3: never overwrite an earlier quarantine
        dest = f"{base}-{n}"
        n += 1
    os.replace(CHAIN_FILE, dest)
    log.error("Quarantined %s -> %s (checkpoint mismatch at %s); resyncing from genesis.",
              CHAIN_FILE, dest, reason_heights)
    return dest


def reset_checkpoints() -> None:
    global _checkpoint_cache
    _checkpoint_cache = None

ADDRESS_RE = re.compile(r"^[0-9a-f]{32}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
SIG_RE = re.compile(r"^[0-9a-f]{128}$")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def normalize_address(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Address must be a string")
    address = value.strip().lower()
    if not ADDRESS_RE.fullmatch(address):
        raise ValueError(f"Invalid address: {value!r}")
    return address


def is_valid_address(value: Any) -> bool:
    try:
        normalize_address(value)
        return True
    except Exception:
        return False


def normalize_pubkey(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Pubkey must be a string")
    pubkey = value.strip().lower()
    if not HEX64_RE.fullmatch(pubkey):
        raise ValueError("Invalid pubkey")
    return pubkey


def normalize_sig(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Signature must be a string")
    sig = value.strip().lower()
    if not SIG_RE.fullmatch(sig):
        raise ValueError("Invalid signature")
    return sig


def tx_id(tx: dict) -> str:
    return sha256_text(canonical(normalize_tx(tx)))


def is_reward_tx(tx: dict) -> bool:
    return isinstance(tx, dict) and tx.get("type") == "reward"


def is_allocation_tx(tx: dict) -> bool:
    """Genesis-only output that mints a fixed allocation to a bucket address."""
    return isinstance(tx, dict) and tx.get("type") == "allocation"


def is_payout_tx(tx: dict) -> bool:
    """Protocol-generated community payout (only in epoch blocks, right after the reward)."""
    return isinstance(tx, dict) and tx.get("type") == "payout"


def is_params_tx(tx: dict) -> bool:
    """Genesis-only record of the hash of all economic rules."""
    return isinstance(tx, dict) and tx.get("type") == "params"


def is_multisig_tx(tx: dict) -> bool:
    """Treasury/reserve spend approved by M-of-N keyholder signatures."""
    return isinstance(tx, dict) and tx.get("type") == "multisig_spend"


def is_signed_tx(tx: dict) -> bool:
    """A normal single-signer transfer."""
    return isinstance(tx, dict) and "payload" in tx and "type" not in tx


def normalize_multisig_tx(tx: dict) -> dict:
    p = tx.get("payload")
    if not isinstance(p, dict):
        raise ValueError("Missing payload")
    amount, nonce = int(p.get("amount", 0)), int(p.get("nonce", 0))
    if amount <= 0 or nonce <= 0:
        raise ValueError("Amount and nonce must be > 0")
    if p.get("bucket") not in ("treasury", "reserve"):
        raise ValueError("bucket must be treasury or reserve")
    if not isinstance(p.get("chain_id"), str):
        raise ValueError("Missing chain_id")
    sigs = tx.get("sigs")
    if not isinstance(sigs, list) or len(sigs) > 64:
        raise ValueError("sigs must be a list")
    return {
        "type": "multisig_spend",
        "payload": {
            "bucket": p["bucket"],
            "recipient": normalize_address(p.get("recipient")),
            "amount": amount,
            "nonce": nonce,
            "timestamp": float(p.get("timestamp", 0)),
            "chain_id": p["chain_id"],
        },
        "sigs": [{"pubkey": normalize_pubkey(s.get("pubkey")), "sig": normalize_sig(s.get("sig"))}
                 for s in sigs],
    }


def make_reward_tx(recipient: str, amount: int, fees: int = 0) -> dict:
    """Coinbase: `amount` is newly minted (from the miner bucket); `fees` are the
    block's transfer fees, moved from senders to the miner (not minted)."""
    return {
        "type": "reward",
        "recipient": normalize_address(recipient),
        "amount": int(amount),
        "fees": int(fees),
    }


def normalize_reward_tx(tx: dict) -> dict:
    if not isinstance(tx, dict):
        raise ValueError("Reward tx must be a dict")

    if tx.get("type") != "reward":
        raise ValueError("Not a reward tx")

    amount = int(tx.get("amount", -1))
    if amount < 0:  # 0 is valid once the miner bucket is used up
        raise ValueError("Invalid reward amount")
    fees = int(tx.get("fees", 0))
    if fees < 0:
        raise ValueError("Invalid reward fees")

    return {
        "type": "reward",
        "recipient": normalize_address(tx.get("recipient")),
        "amount": amount,
        "fees": fees,
    }


def normalize_signed_tx(tx: dict) -> dict:
    if not isinstance(tx, dict):
        raise ValueError("Signed tx must be a dict")

    payload = tx.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("Missing payload")

    amount = int(payload.get("amount", 0))
    nonce = int(payload.get("nonce", 0))
    timestamp = float(payload.get("timestamp", 0))
    if "fee" not in payload:
        raise ValueError("Missing fee")
    fee = int(payload.get("fee"))

    if amount <= 0:
        raise ValueError("Amount must be > 0")
    if fee < 0:
        raise ValueError("Fee must be >= 0")
    if nonce <= 0:
        raise ValueError("Nonce must be > 0")
    if timestamp <= 0:
        raise ValueError("Timestamp must be > 0")
    if not isinstance(payload.get("chain_id"), str):
        raise ValueError("Missing chain_id")

    normalized = {
        "payload": {
            "sender": normalize_address(payload.get("sender")),
            "recipient": normalize_address(payload.get("recipient")),
            "amount": amount,
            "nonce": nonce,
            "timestamp": timestamp,
            "fee": fee,
            "chain_id": payload["chain_id"],
        },
        "pubkey": normalize_pubkey(tx.get("pubkey")),
        "sig": normalize_sig(tx.get("sig")),
    }

    return normalized


def normalize_allocation_tx(tx: dict) -> dict:
    amount = int(tx.get("amount", 0))
    if amount <= 0:
        raise ValueError("Invalid allocation amount")
    return {
        "type": "allocation",
        "bucket": str(tx.get("bucket")),
        "recipient": normalize_address(tx.get("recipient")),
        "amount": amount,
        "locked": bool(tx.get("locked", True)),
    }


def normalize_payout_tx(tx: dict) -> dict:
    amount = int(tx.get("amount", 0))
    if amount <= 0:
        raise ValueError("Invalid payout amount")
    return {
        "type": "payout",
        "epoch": int(tx["epoch"]),
        "recipient": normalize_address(tx.get("recipient")),
        "amount": amount,
        "vest_start": int(tx["vest_start"]),
        "vest_end": int(tx["vest_end"]),
    }


def normalize_tx(tx: dict) -> dict:
    if pause.is_pause_tx(tx):
        return pause.normalize_pause_tx(tx)
    if registry.is_registry_tx(tx):
        return registry.normalize_registry_tx(tx)
    if is_reward_tx(tx):
        return normalize_reward_tx(tx)
    if is_payout_tx(tx):
        return normalize_payout_tx(tx)
    if is_multisig_tx(tx):
        return normalize_multisig_tx(tx)
    if is_params_tx(tx):  # only valid inside the genesis block
        return {"type": "params", "economics_sha256": str(tx.get("economics_sha256"))}
    if is_allocation_tx(tx):  # only valid inside the genesis block
        return normalize_allocation_tx(tx)
    return normalize_signed_tx(tx)


@dataclass
class Block:
    index: int
    timestamp: float
    transactions: List[dict]
    previous_hash: str
    nonce: int
    difficulty: int
    hash: str

    def to_dict(self) -> dict:
        return {
            "index": int(self.index),
            "timestamp": float(self.timestamp),
            "transactions": [normalize_tx(tx) for tx in self.transactions],
            "previous_hash": str(self.previous_hash),
            "nonce": int(self.nonce),
            "difficulty": int(self.difficulty),
            "hash": str(self.hash),
        }

    @staticmethod
    def from_dict(data: dict) -> "Block":
        if not isinstance(data, dict):
            raise ValueError("Block must be a dict")

        txs = data.get("transactions")
        if not isinstance(txs, list):
            raise ValueError("Block transactions must be a list")

        return Block(
            index=int(data["index"]),
            timestamp=float(data["timestamp"]),
            transactions=[normalize_tx(tx) for tx in txs],
            previous_hash=str(data["previous_hash"]),
            nonce=int(data["nonce"]),
            difficulty=int(data["difficulty"]),
            hash=str(data["hash"]),
        )


def is_post_fork(index: int) -> bool:
    return int(index) >= LWMA_FORK_HEIGHT


def meets_pow(block_hash: str, difficulty: int, index: int) -> bool:
    if not is_post_fork(index):
        return block_hash.startswith("0" * int(difficulty))
    work = int(difficulty)
    return work >= 1 and int(block_hash, 16) * work < POW_LIMIT


def block_work(block: "Block") -> int:
    """Expected hashes for a block (old blocks: 16 per leading hex zero)."""
    if is_post_fork(block.index):
        return int(block.difficulty)
    return 16 ** int(block.difficulty)


def median_time_past(chain: List["Block"], window: int = MTP_WINDOW) -> float:
    ts = sorted(b.timestamp for b in chain[-window:])
    return ts[len(ts) // 2]


def lwma_next_work(chain: List["Block"]) -> int:
    """LWMA work for the block after chain[-1], using only post-fork blocks.
    Timestamps are viewed monotonically (each at least 1s after the one before)
    and each solve time is capped, so one lying timestamp moves work only a little."""
    T = LWMA_TARGET_BLOCK_TIME
    last = chain[-1]
    if not is_post_fork(last.index):
        return max(LWMA_MIN_WORK, block_work(last))  # first post-fork block: same work as before
    post = [b for b in chain[-LWMA_WINDOW:] if is_post_fork(b.index)]
    start = chain[len(chain) - len(post) - 1] if len(chain) > len(post) else None
    blocks = ([start] if start is not None else []) + post
    if len(blocks) < 2:
        return max(LWMA_MIN_WORK, int(last.difficulty))
    weighted = 0
    sum_work = 0
    prev_t = blocks[0].timestamp
    for i, b in enumerate(blocks[1:], start=1):
        t = max(b.timestamp, prev_t + 1)
        st = min(t - prev_t, LWMA_MAX_SOLVETIME_FACTOR * T)
        prev_t = t
        weighted += st * i
        sum_work += block_work(b)
    n = len(blocks) - 1
    nxt = int(sum_work * T * (n + 1) / (2 * weighted)) if weighted > 0 else block_work(last)
    prev_work = block_work(last)
    hi = prev_work * LWMA_MAX_STEP_UP[0] // LWMA_MAX_STEP_UP[1]
    lo = prev_work * LWMA_MAX_STEP_DOWN[0] // LWMA_MAX_STEP_DOWN[1]
    return max(LWMA_MIN_WORK, min(hi, max(lo, nxt)))


def stall_work(normal_work: int) -> int:
    return max(LWMA_MIN_WORK, normal_work // STALL_WORK_DIVISOR)


class Blockchain:
    def __init__(self, econ: Optional["economics.Economics"] = None):
        self.chain: List[Block] = []
        self.last_reject_reason: Optional[str] = None
        self.econ = econ or economics.load_economics()
        contributions.validate_family_issuers()      # review H2: only checks the list while the family rule is ON
        self.reorg_events: List[dict] = []           # off-chain tripwire: {"time", "height", "depth"} per reorg
        self.rejected_reorg_events: List[dict] = []  # deep reorgs we REFUSED (over MAX_REORG_DEPTH): {"time", "depth"}
        self._load_reorg_log()                       # both lists survive a restart (sidecar file next to chain.json)

        if not self.load_chain():
            if os.path.exists(CHAIN_FILE) and self._handle_checkpoint_mismatch_on_load():
                return  # quarantined and restarted from genesis (review F10)
            if os.path.exists(CHAIN_FILE):
                # Never silently overwrite an existing chain (e.g. a pre-Ed25519
                # chain that no longer validates). See MIGRATION.md.
                raise RuntimeError(
                    f"{CHAIN_FILE} exists but is not a valid chain. Back it up and run "
                    "`python new_genesis.py` to start a fresh chain."
                )
            self.chain = [self.create_genesis_block()]
            self.save_chain()

    # ----------------------------
    # Persistence
    # ----------------------------

    def _handle_checkpoint_mismatch_on_load(self) -> bool:
        """Review F10: if chain.json failed to load *because* it contradicts a
        checkpoint, say so loudly; refuse to start, or (opt-in via
        NETLITHIC_CHECKPOINT_RESYNC=1) quarantine it and start from genesis.
        Returns True if the chain was quarantined and reset."""
        try:
            with open(CHAIN_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            bad = checkpoint_mismatches(data) if isinstance(data, list) else []
        except Exception:
            return False
        if not bad:
            return False
        self.handle_checkpoint_mismatch(bad)  # raises unless resync is enabled
        return True

    def handle_checkpoint_mismatch(self, bad) -> None:
        log.error("CHECKPOINT MISMATCH: local chain disagrees with config/checkpoints.json at block(s) %s. "
                  "This node may have been eclipsed or given a bad data folder. See docs/ECLIPSE_RECOVERY.md.", bad)
        if os.environ.get(CHECKPOINT_RESYNC_ENV, "") == "1":
            quarantine_chain_file(bad)
            self.chain = [self.create_genesis_block()]
            self.save_chain()
            return
        raise CheckpointMismatch(
            f"Refusing to start: local chain does not match checkpoint(s) at block(s) {bad}. "
            f"Follow docs/ECLIPSE_RECOVERY.md, or set {CHECKPOINT_RESYNC_ENV}=1 to move "
            f"{CHAIN_FILE} aside and resync from genesis.")

    def checkpoints_ok(self) -> bool:
        return not checkpoint_mismatches(self.chain)

    def save_chain(self) -> None:
        with open(CHAIN_FILE, "w", encoding="utf-8") as f:
            json.dump([block.to_dict() for block in self.chain], f, indent=2)
        self.save_contributions_cache()

    # ----------------------------
    # Contribution points (derived from the chain, never stored separately)
    # ----------------------------

    def contribution_points(self, chain: Optional[List[Block]] = None,
                            explain: Optional[list] = None) -> Dict[str, Dict[str, int]]:
        chain = self.chain if chain is None else chain
        return contributions.points_from_blocks(
            (block.to_dict() for block in chain), self.econ.points_per_block, self.econ.points_per_tx,
            self.econ.min_point_transfer, self.econ.point_pair_cap, self.econ.epoch_blocks,
            epoch_fn=self.econ.epoch_id, rules_height=SECURITY_FORK_HEIGHT,
            first_seen=contributions.first_seen_heights(b.to_dict() for b in self.chain),
            explain=explain, registry=self._registry_fn(self.chain),
            flat_block_points_height=POINTS_V4_HEIGHT,
            **self._v3_args(self.chain, len(self.chain)))

    # ---- registered contributors (registry.py): the list is derived from the chain, like the pause state ----

    def registry_state(self, chain: Optional[List[Block]] = None) -> "registry.RegistryState":
        """The registry as seen after every block of `chain` (default: the whole chain)."""
        return registry.scan(self.chain if chain is None else chain, self.econ.epoch_id,
                             fork_height=POINTS_V4_HEIGHT, chain_id=self.econ.chain_id)

    def registry_info(self, address: Optional[str] = None) -> dict:
        """Read-only view of the registered-contributors list for /registry and the explorer."""
        height = len(self.chain)                      # the next block
        st = self.registry_state()
        on = registry.REGISTRY_ENABLED
        live = on and is_points_v4(height) and registry.active_at(height)
        now = st.registered_now()
        entries = []
        for a, h in sorted(now.items(), key=lambda kv: (kv[1], kv[0])):
            start = h + st.delay
            entries.append({"address": a, "added_at_height": h, "earns_from_height": start,
                            "status": "active" if height >= start else "pending",
                            "blocks_until_active": max(0, start - height)})
        active = [x for x in entries if x["status"] == "active"]
        pend = [x for x in entries if x["status"] == "pending"]
        leaving = st.leaving(height)
        removed = [{"address": a, "removed_at_height": e[-1][1],
                    "still_earning_until_height": leaving.get(a)} for a, e in sorted(st.events.items())
                   if e and e[-1][0] == "remove"]
        pend_key = st.pending_key(height)
        P = self.econ.epoch_length_at(height)
        if not on:
            mode = "off"
            note = "The registry is switched off in this build: the transfer pot is open to everyone (points v4)."
        elif not registry.REGISTRY_PUBKEY:
            mode = "no_key"
            note = ("TEMPORARY, CENTRALIZED: the registry is on but no signing key is set, so nobody earns transfer "
                    "points. The transfer pot stays unspent in the pool. Mining points are open to everyone.")
        elif not is_points_v4(height):
            mode = "before_fork"
            note = f"The registry starts at block {POINTS_V4_HEIGHT}."
        elif not live:
            mode = "sunset"
            note = f"The registry ended at block {registry.REGISTRY_SUNSET_HEIGHT}: the transfer pot is open again."
        elif not active:
            mode = "empty"
            note = ("TEMPORARY, CENTRALIZED: the list has nobody active yet, so nobody earns transfer points and the "
                    "transfer pot stays unspent in the pool. Mining points are open to everyone.")
        else:
            mode = "on"
            note = ("TEMPORARY, CENTRALIZED: only addresses on this list earn transfer points (the sender must be "
                    "listed, and counts one day after being added). Mining points are open to everyone.")
        out = {
            "enabled": on, "mode": mode, "note": note, "test_coins_note": "Test coins have no value.",
            "temporary": True, "centralized": True,
            "key_set": bool(registry.REGISTRY_PUBKEY), "key_id": registry.key_id(),
            "delay_blocks": st.delay, "remove_delay_blocks": st.remove_delay,
            "max_adds_per_period": registry.MAX_ADDS_PER_WINDOW,
            "max_adds_per_window": registry.MAX_ADDS_PER_WINDOW,
            "max_removes_per_window": registry.MAX_REMOVES_PER_WINDOW,
            "limit_window_blocks": registry.REGISTRY_LIMIT_WINDOW,
            "adds_in_window": st.adds_in_window(height), "removes_in_window": st.removes_in_window(height),
            "active_key_id": registry.key_id(st.key_at(height)),
            "pending_key_id": registry.key_id(pend_key[1]) if pend_key else None,
            "pending_key_from_height": pend_key[0] if pend_key else None,
            "rotation_delay_blocks": registry.REGISTRY_ROTATION_DELAY_BLOCKS,
            "max_active_entries": registry.MAX_ACTIVE_ENTRIES, "sunset_height": registry.REGISTRY_SUNSET_HEIGHT,
            "fork_height": POINTS_V4_HEIGHT, "next_height": height, "nonce": st.nonce,
            "adds_this_period": st.adds_in_window(height), "period_blocks": P,
            "active_count": len(active), "pending_count": len(pend),
            "active": active, "pending": pend, "removed": removed, "log": st.log[-50:],
        }
        if address is not None:
            address = normalize_address(address)
            e = next((x for x in entries if x["address"] == address), None)
            if not on or not live:
                out["address"] = {"address": address, "earns_transfer_points": True,
                                  "status": "open", "why": "The transfer pot is open to everyone right now."}
            elif e is None:
                out["address"] = {"address": address, "earns_transfer_points": False, "status": "not_listed",
                                  "why": "Not on the list. Transfers you send earn no points; mining points still count. "
                                         "Ask the project founder for a place on the list."}
            elif e["status"] == "pending":
                out["address"] = {"address": address, "earns_transfer_points": False, "status": "pending",
                                  "earns_from_height": e["earns_from_height"],
                                  "why": f"Listed, and starts counting at block {e['earns_from_height']} "
                                         f"({e['blocks_until_active']} blocks from now)."}
            else:
                out["address"] = {"address": address, "earns_transfer_points": True, "status": "active",
                                  "why": "Listed: transfers you send earn transfer points (normal caps apply)."}
        return out

    def _registry_fn(self, view: List[Block]):
        """(sender, height) -> may this transfer earn transfer points? None = open (registry off or sunset passed
        everywhere). `view` is the chain the decision is made on (for payouts: the blocks before the paying block)."""
        if not registry.REGISTRY_ENABLED:
            return None
        return registry.resolver(self.registry_state(view), POINTS_V4_HEIGHT)

    @staticmethod
    def _v3_args(prior_chain: List[Block], height: int) -> dict:
        if not is_points_v3(height):
            return {}
        if not contributions.FAMILY_RULE_ENABLED:
            return {"v3": True, "roots": None, "family_pair_cap": None}
        # roots is None (no family rule, design A only) if the issuer list matches no real funder on this chain
        return {"v3": True, "roots": contributions.active_family_roots(b.to_dict() for b in prior_chain),
                "family_pair_cap": contributions.FAMILY_PAIR_CAP}

    def explain_points(self, address: str, limit: int = 50) -> dict:
        """Why did this address's transfers in the current reward period earn (or not earn) points?"""
        address = normalize_address(address)
        e = self.econ
        cur = e.epoch_id(len(self.chain))
        window = [b for b in self.chain if e.epoch_id(b.index) == cur]
        log: list = []
        users = self.contribution_points(window, explain=log)
        mine = [x for x in log if x["sender"] == address]
        counts: Dict[str, int] = {}
        for x in mine:
            counts[x["reason"]] = counts.get(x["reason"], 0) + 1
        u = users.get(address, {})
        v3 = is_points_v3(len(self.chain))
        roots = self._v3_args(self.chain, len(self.chain)).get("roots") if v3 else None
        lines = []
        for reason, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            if reason != "earned":
                lines.append(f"{n} transfer{'s' if n != 1 else ''} earned 0: {contributions.REASON_TEXT[reason]}")
        if u.get("empty_blocks"):
            lines.append(f"{u['empty_blocks']} of your {u['blocks_mined']} mined blocks earned 0: no one else's "
                         "transfer was in them.")
        return {
            "address": address, "epoch": cur, "points": u.get("cp", "0"),
            "transfers_earned": counts.get("earned", 0),
            "family_rule_active": roots is not None,
            "funding_family": (roots.get(address) if roots else None),
            "summary": counts, "why_zero": lines,
            "transfers": [dict(x, why=contributions.REASON_TEXT[x["reason"]]) for x in reversed(mine[-limit:])],
        }

    def contribution_points_this_epoch(self) -> Dict[str, Dict[str, int]]:
        """Points earned so far in the current (not yet paid) epoch."""
        cur = self.econ.epoch_id(len(self.chain))
        window = [b for b in self.chain if self.econ.epoch_id(b.index) == cur]
        return self.contribution_points(window)

    def save_contributions_cache(self) -> None:
        try:
            contributions.save_cache(self.contribution_points(), len(self.chain) - 1)
        except OSError as e:
            log.warning("Could not write contributions cache: %s", e)

    def load_chain(self) -> bool:
        if not os.path.exists(CHAIN_FILE):
            return False

        try:
            with open(CHAIN_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

            if not isinstance(data, list) or not data:
                return False

            loaded_chain = [Block.from_dict(block_data) for block_data in data]

            if not self.is_valid_chain([block.to_dict() for block in loaded_chain]):
                return False

            self.chain = loaded_chain
            self.save_contributions_cache()  # rebuild cache from the loaded chain
            return True
        except Exception:
            return False

    # ----------------------------
    # Genesis
    # ----------------------------

    def create_genesis_block(self) -> Block:
        index = 0
        timestamp = self.econ.genesis_timestamp
        transactions: List[dict] = [
            {
                "type": "allocation",
                "bucket": bucket,
                "recipient": self.econ.allocations[bucket],
                "amount": self.econ.allocation_amount(bucket),
                "locked": True,
            }
            for bucket in economics.GENESIS_BUCKETS
        ] + [{"type": "params", "economics_sha256": self.econ.params_hash()}]
        previous_hash = "0" * 64
        difficulty = DEFAULT_DIFFICULTY
        nonce = 0

        while True:
            block_hash = self.calculate_hash(
                index=index,
                timestamp=timestamp,
                transactions=transactions,
                previous_hash=previous_hash,
                nonce=nonce,
                difficulty=difficulty,
            )
            if block_hash.startswith("0" * difficulty):
                return Block(
                    index=index,
                    timestamp=timestamp,
                    transactions=transactions,
                    previous_hash=previous_hash,
                    nonce=nonce,
                    difficulty=difficulty,
                    hash=block_hash,
                )
            nonce += 1

    def get_latest_block(self) -> Block:
        return self.chain[-1]

    # ----------------------------
    # Hashing / Difficulty
    # ----------------------------

    def calculate_hash(
        self,
        index: int,
        timestamp: float,
        transactions: List[dict],
        previous_hash: str,
        nonce: int,
        difficulty: int,
    ) -> str:
        block_data = {
            "index": int(index),
            "timestamp": float(timestamp),
            "transactions": [normalize_tx(tx) for tx in transactions],
            "previous_hash": str(previous_hash),
            "nonce": int(nonce),
            "difficulty": int(difficulty),
        }
        return sha256_text(canonical(block_data))

    def calculate_next_difficulty(self, chain: Optional[List[Block]] = None) -> int:
        if chain is None:
            chain = self.chain

        if len(chain) == 1:
            return DEFAULT_DIFFICULTY

        last_block = chain[-1]
        if last_block.index == 0:
            return DEFAULT_DIFFICULTY

        if last_block.index % DIFFICULTY_ADJUSTMENT_INTERVAL != 0:
            return last_block.difficulty

        start_idx = max(0, len(chain) - DIFFICULTY_ADJUSTMENT_INTERVAL)
        window = chain[start_idx:]

        if len(window) < 2:
            return last_block.difficulty

        actual_time = window[-1].timestamp - window[0].timestamp
        expected_time = TARGET_BLOCK_TIME * (len(window) - 1)
        difficulty = last_block.difficulty

        if actual_time < expected_time * 0.5:
            return difficulty + MAX_DIFFICULTY_STEP
        if actual_time > expected_time * 2.0:
            return max(MIN_DIFFICULTY, difficulty - MAX_DIFFICULTY_STEP)

        return difficulty

    def expected_block_difficulty(self, previous_chain: List[Block]) -> int:
        if is_post_fork(previous_chain[-1].index + 1):
            return lwma_next_work(previous_chain)
        return self.calculate_next_difficulty(previous_chain)

    def allowed_difficulties(self, previous_chain: List[Block], timestamp: float) -> List[int]:
        """Work values a new block may carry. After the fork, a block arriving
        STALL_SECONDS or more after the previous one may use a reduced
        "emergency" work so a hashrate drop can't freeze the chain."""
        normal = self.expected_block_difficulty(previous_chain)
        height = previous_chain[-1].index + 1
        if not is_post_fork(height):
            return [normal]
        if is_security_fork(height):
            # Measure the gap from the LATEST recent timestamp (not just the
            # previous block, which a miner can backdate). This is a pure
            # function of chain data: no wall-clock read here, so every node
            # reaches the same verdict whenever it validates (review F2).
            # Future-dated timestamps are bounded at block acceptance
            # (MAX_FUTURE_BLOCK_SECONDS drift bound in block_reject_reason,
            # applied when a block is received or mined, not here).
            newest = max(b.timestamp for b in previous_chain[-MTP_WINDOW:])
            stalled = timestamp - newest >= STALL_SECONDS
        else:
            stalled = timestamp - previous_chain[-1].timestamp >= STALL_SECONDS
        return [normal, stall_work(normal)] if stalled else [normal]

    # ----------------------------
    # Chain Views
    # ----------------------------

    def to_list(self) -> List[dict]:
        return [block.to_dict() for block in self.chain]

    def chain_work(self, chain: Optional[List[Block]] = None) -> int:
        if chain is None:
            chain = self.chain
        return sum(block_work(block) for block in chain)

    # ----------------------------
    # Transactions / State
    # ----------------------------

    def get_balances(self, chain: Optional[List[Block]] = None) -> Dict[str, int]:
        if chain is None:
            chain = self.chain

        balances: Dict[str, int] = {}

        for block in chain:
            for tx in block.transactions:
                if is_params_tx(tx) or pause.is_pause_tx(tx) or registry.is_registry_tx(tx):
                    continue
                if is_reward_tx(tx) or is_allocation_tx(tx):
                    recipient = normalize_address(tx["recipient"])
                    amount = int(tx["amount"]) + int(tx.get("fees", 0))
                    balances[recipient] = balances.get(recipient, 0) + amount
                elif is_multisig_tx(tx):
                    p = tx["payload"]
                    src = self.econ.allocations[p["bucket"]]
                    balances[src] = balances.get(src, 0) - int(p["amount"])
                    balances[p["recipient"]] = balances.get(p["recipient"], 0) + int(p["amount"])
                elif is_payout_tx(tx):
                    pool = self.econ.allocations["community"]
                    amount = int(tx["amount"])
                    balances[pool] = balances.get(pool, 0) - amount
                    balances[tx["recipient"]] = balances.get(tx["recipient"], 0) + amount
                else:
                    payload = tx["payload"]
                    sender = normalize_address(payload["sender"])
                    recipient = normalize_address(payload["recipient"])
                    amount = int(payload["amount"])
                    fee = int(payload.get("fee", 0))

                    balances[sender] = balances.get(sender, 0) - amount - fee
                    balances[recipient] = balances.get(recipient, 0) + amount

        return balances

    def rewards_paid(self, chain: Optional[List[Block]] = None) -> int:
        chain = self.chain if chain is None else chain
        return sum(int(tx["amount"]) for b in chain for tx in b.transactions if is_reward_tx(tx))

    def total_minted(self, chain: Optional[List[Block]] = None) -> int:
        chain = self.chain if chain is None else chain
        allocated = sum(int(tx["amount"]) for b in chain for tx in b.transactions if is_allocation_tx(tx))
        return allocated + self.rewards_paid(chain)

    def expected_reward(self, prior_chain: List[Block]) -> int:
        """Reward for the block that would come after prior_chain."""
        return self.econ.block_reward(len(prior_chain), self.rewards_paid(prior_chain))

    # ----------------------------
    # Time locks, vesting and community payouts
    # ----------------------------

    @staticmethod
    def unvested(payout: dict, height: int) -> int:
        amount, start, end = int(payout["amount"]), int(payout["vest_start"]), int(payout["vest_end"])
        if height >= end:
            return 0
        if height <= start:
            return amount
        return amount - amount * (height - start) // (end - start)

    def restricted_amounts(self, prior_chain: List[Block], height: int) -> Dict[str, int]:
        """Per address, how much of its balance can NOT be spent at `height`:
        the still-locked part of a genesis allocation, plus unvested payouts."""
        restricted = {
            addr: self.econ.still_locked(bucket, height)
            for bucket, addr in self.econ.allocations.items()
        }
        for block in prior_chain:
            for tx in block.transactions:
                if is_payout_tx(tx):
                    left = self.unvested(tx, height)
                    if left:
                        restricted[tx["recipient"]] = restricted.get(tx["recipient"], 0) + left
        return restricted

    def community_pool_available(self, prior_chain: List[Block], height: int) -> int:
        """Unlocked community allocation not yet paid out (plus any donations)."""
        pool = self.econ.allocations["community"]
        return max(0, self.get_balance(pool, prior_chain) - self.econ.still_locked("community", height))

    def period_bounds(self, period: int):
        """(start, end) block range [start, end) of payout period number `period` (1 = the first period, which starts
        at the fork), or None when the chain has no such period (not a short-epoch chain, or period < 1)."""
        se = self.econ.short_epoch()
        if not se or int(period) < 1:
            return None
        fork, length = se
        start = fork + (int(period) - 1) * length
        return start, start + length

    def expected_payouts(self, prior_chain: List[Block], height: int) -> List[dict]:
        """The exact payout outputs the block at `height` must contain (deterministic).

        Legacy (before POINTS_V4_HEIGHT): at each epoch block the whole available pool is split pro rata by the points
        of the epoch that just ended, each address capped at payout_cap.
        Points v4 (from POINTS_V4_HEIGHT): at each epoch block X, pay the period that ended PAYOUT_LAG_PERIODS periods
        earlier, i.e. the points of blocks [X - (lag+1)P, X - lag*P). Nothing is paid while that window would reach
        back before the fork. A pause active at X pays nothing and the coins stay in the pool."""
        e = self.econ
        if not e.is_epoch_block(height):
            return []
        if is_security_fork(height) and pause.state(prior_chain, height)["paused"]:
            return []  # payouts paused (public, on-chain); the pool keeps the coins
        if is_points_v4(height):
            period = e.epoch_length_at(height)
            end = height - contributions.PAYOUT_LAG_PERIODS * period
            start = end - period
            if start < POINTS_V4_HEIGHT:
                return []  # the first payout waits one period: its window starts at the fork
            return self._v4_period_payouts(prior_chain, start, end, height)
        start = height - e.epoch_length_at(height)
        window = [b for b in prior_chain if start <= b.index < height]
        points = contributions.points_from_blocks(
            (b.to_dict() for b in window), e.points_per_block, e.points_per_tx,
            e.min_point_transfer, e.point_pair_cap, e.epoch_blocks, epoch_fn=e.epoch_id,
            rules_height=SECURITY_FORK_HEIGHT,
            first_seen=contributions.first_seen_heights(b.to_dict() for b in prior_chain))
        cp = {a: u["cp_tenths"] for a, u in points.items()
              if a not in e.locked_addresses and u["cp_tenths"] > 0}
        total_cp = sum(cp.values())
        pool = self.community_pool_available(prior_chain, height)
        if total_cp == 0 or pool == 0:
            return []
        payouts = []
        for addr in sorted(cp):
            amount = min(pool * cp[addr] // total_cp, e.payout_cap)
            if amount > 0:
                payouts.append({
                    "type": "payout",
                    "epoch": e.epoch_id(height),
                    "recipient": addr,
                    "amount": amount,
                    "vest_start": height,
                    "vest_end": height + e.vesting_blocks_at(height),
                })
        return payouts

    def _v4_period_inputs(self, chain: List[Block], start: int, end: int):
        """(points, budget) for the period [start, end): everything the payout list depends on.
        Only blocks up to and including block `end` are used (see _v4_period_payouts), INCLUDING the registry list:
        a registry update can only change who earns for transfers at or after its own height (plus the delays), so
        the list as of block `end` gives exactly the answer the paying block would get from its whole prior chain."""
        e = self.econ
        known = [b for b in chain if b.index <= end]
        window = [b for b in known if start <= b.index < end]
        points = contributions.points_from_blocks(
            (b.to_dict() for b in window), e.points_per_block, e.points_per_tx,
            e.min_point_transfer, e.point_pair_cap, e.epoch_blocks, epoch_fn=e.epoch_id,
            rules_height=SECURITY_FORK_HEIGHT,
            first_seen=contributions.first_seen_heights(b.to_dict() for b in known),
            registry=self._registry_fn(known),
            flat_block_points_height=POINTS_V4_HEIGHT,
            **self._v3_args(known, end))
        pool = self.community_pool_available(known, end + 1)
        return points, pool * (end - start) // e.blocks_per_month

    def _v4_amounts(self, points: dict, budget: int) -> Dict[str, int]:
        e = self.econ
        return contributions.v3_amounts(points, e.points_per_block, e.points_per_tx, budget,
                                        contributions.TX_POT_PERCENT, e.payout_cap, e.locked_addresses,
                                        max_per_point=contributions.MAX_BASE_PER_POINT,
                                        pot_cap_percent=contributions.POT_CAP_PERCENT)

    def _v4_period_payouts(self, chain: List[Block], start: int, end: int, pay_height: int) -> List[dict]:
        """Payout list for the period of blocks [start, end), to be credited in the block at `pay_height`.

        Everything is fixed by blocks up to and including block `end`: the points come from [start, end), the registry
        list is read as of block `end` (updates mined later cannot change an earlier transfer's answer, see
        registry.py), and the release budget is the pool measured right after block `end`. Block `end` is one full
        period (1,440 blocks) older than the paying block, which is deeper than a node's own reorg limit
        (MAX_REORG_DEPTH = 720, a node policy and not a consensus rule), so in practice the list is settled by then."""
        e = self.econ
        points, budget = self._v4_period_inputs(chain, start, end)
        amounts = self._v4_amounts(points, budget)
        return [{"type": "payout", "epoch": e.epoch_id(start), "recipient": a, "amount": amt,
                 "vest_start": pay_height, "vest_end": pay_height + e.vesting_blocks_at(pay_height)}
                for a, amt in amounts.items()]

    def payout_preview(self, period: Optional[int] = None, chain: Optional[List[Block]] = None) -> dict:
        """Read-only: the payout list for one period, as the chain will pay it one period after the period ends.
        period=None means the latest period that has already ended. Never changes anything.
        `chain` is a snapshot (a list of blocks): the preview then never reads self.chain, so a caller can compute it
        without holding the node's state lock. The registry list is read as of block `end` (see _v4_period_inputs)."""
        e = self.econ
        chain = self.chain if chain is None else chain
        height = len(chain)
        P = e.epoch_length_at(height)
        if period is None:
            se = e.short_epoch()
            if not se or height - 1 < se[0] + P:
                return {"error": "No payout period has ended yet.", "status": "none"}
            period = (height - 1 - se[0]) // P          # last period that is complete
        bounds = self.period_bounds(period)
        if bounds is None:
            return {"error": "Unknown period.", "status": "none"}
        start, end = bounds
        pay_height = end + contributions.PAYOUT_LAG_PERIODS * P
        out = {"period": int(period), "window_start": start, "window_end": end, "pays_at_height": pay_height,
               "payout_lag_blocks": contributions.PAYOUT_LAG_PERIODS * P,
               "note": "Test coins have no value. This list is computed from chain data only."}
        if height - 1 < end:
            out.update(status="period_not_finished", blocks_until_period_ends=end - (height - 1), payouts=[])
            return out
        points, budget = self._v4_period_inputs(chain, start, end)
        amounts = self._v4_amounts(points, budget)
        pays = [{"recipient": a, "amount": amt} for a, amt in amounts.items()]
        tx_budget = budget * contributions.TX_POT_PERCENT // 100
        mine_budget = budget - tx_budget
        cap_pct = contributions.POT_CAP_PERCENT
        out["pots"] = {
            "release_budget_nlt": economics.to_nlt(budget),
            "mining_pot_nlt": economics.to_nlt(mine_budget),
            "transfer_pot_nlt": economics.to_nlt(tx_budget),
            "per_address_cap_percent": cap_pct,
            "per_address_cap_mining_nlt": None if cap_pct is None else economics.to_nlt(mine_budget * cap_pct // 100),
            "per_address_cap_transfer_nlt": None if cap_pct is None else economics.to_nlt(tx_budget * cap_pct // 100),
            "family_rule": bool(contributions.FAMILY_RULE_ENABLED),
        }
        paid = [t for b in chain if b.index == pay_height for t in b.transactions if is_payout_tx(t)]
        if height - 1 >= pay_height:
            status = "paid" if paid else "skipped_or_empty"
        else:
            status = "proposed"
        total = sum(p["amount"] for p in pays)
        out.update(status=status, total_nlt=economics.to_nlt(total),
                   payouts=[{"address": p["recipient"], "amount_nlt": economics.to_nlt(p["amount"]),
                             "amount_base_units": p["amount"],
                             "mining_points": ((points[p["recipient"]]["blocks_mined"]
                                                - points[p["recipient"]]["empty_blocks"]) * e.points_per_block) / 10,
                             "transfer_points": (points[p["recipient"]]["point_tx_count"] * e.points_per_tx) / 10}
                            for p in pays],
                   paused_now=bool(pause.state(chain)["paused"]) if is_security_fork(height) else False)
        return out

    PREVIEW_CACHE_SIZE = 16             # explicit periods kept (least recently used goes first)
    PREVIEW_MAX_COMPUTING = 2           # at most this many DIFFERENT previews computed at once; more get PreviewBusy (503)
    PREVIEW_CACHEABLE = ("proposed", "paid", "skipped_or_empty")

    def payout_preview_cached(self, period: Optional[int] = None, chain: Optional[List[Block]] = None) -> dict:
        """payout_preview() with a small cache keyed by (period, tip hash, height): the same answer is handed out until
        the chain moves. The (slow) computation runs outside any lock, on the snapshot.
        - Single flight: concurrent requests for the same key share ONE computation (the others wait for it).
        - Only real answers are cached (status proposed / paid / skipped_or_empty). Junk periods (-1, 0, 10**300) and
          "not finished yet" answers are cheap to recompute and are never stored, so they cannot push anything out.
        - The answer for period=None (the latest period) is pinned in its own slot: explicit-period requests, however
          many, cannot evict it. Explicit periods live in a small LRU.
        - At most PREVIEW_MAX_COMPUTING different keys compute at once; beyond that PreviewBusy is raised."""
        chain = list(self.chain) if chain is None else chain
        tip = (len(chain), chain[-1].hash if chain else "")
        d = self.__dict__
        lock = d.setdefault("_preview_lock", threading.Lock())
        cache = d.setdefault("_preview_cache", OrderedDict())     # (period, tip) -> answer, explicit periods only
        pinned = d.setdefault("_preview_pinned", {})              # "latest" -> (tip, answer)
        inflight = d.setdefault("_preview_inflight", {})
        slots = d.setdefault("_preview_slots", threading.BoundedSemaphore(self.PREVIEW_MAX_COMPUTING))
        key = (period, tip)
        while True:
            with lock:
                if period is None:
                    hit = pinned.get("latest")
                    hit = hit[1] if hit and hit[0] == tip else None
                else:
                    hit = cache.get(key)
                    if hit is not None:
                        cache.move_to_end(key)
                if hit is not None:
                    return hit
                flight = inflight.get(key)
                leader = flight is None
                if leader:
                    flight = inflight[key] = {"event": threading.Event(), "result": None}
            if not leader:
                flight["event"].wait(300)
                if flight["result"] is not None:
                    return flight["result"]
                continue                                           # the leader failed or was busy: try to lead
            res = None
            took = slots.acquire(blocking=False)
            try:
                if not took:
                    raise PreviewBusy("too many payout previews are being computed")
                res = self.payout_preview(period, chain)
                return res
            finally:
                if took:
                    slots.release()
                with lock:
                    inflight.pop(key, None)
                    if isinstance(res, dict) and res.get("status") in self.PREVIEW_CACHEABLE:
                        if period is None:
                            pinned["latest"] = (tip, res)
                        else:
                            cache[key] = res
                            cache.move_to_end(key)
                            while len(cache) > self.PREVIEW_CACHE_SIZE:
                                cache.popitem(last=False)
                flight["result"] = res
                flight["event"].set()

    def payout_account(self, address: str) -> dict:
        """Community payouts for one address, all computed from the chain:
        earned = every payout received; vesting = the part not yet unlocked;
        claimable = the vested part (already spendable, no claim step needed)."""
        address = normalize_address(address)
        height = len(self.chain)
        earned = vesting = 0
        history = []
        for b in self.chain:
            for tx in b.transactions:
                if is_payout_tx(tx) and tx["recipient"] == address:
                    left = self.unvested(tx, height)
                    earned += int(tx["amount"])
                    vesting += left
                    history.append({"epoch": int(tx["epoch"]), "height": b.index,
                                    "amount": economics.to_nlt(int(tx["amount"])),
                                    "vests_fully_at": int(tx["vest_end"]),
                                    "still_vesting": economics.to_nlt(left)})
        cur = self.contribution_points_this_epoch().get(address, {})
        nxt = self.econ.next_epoch_height(height - 1)
        return {
            "address": address,
            "earned_nlt": economics.to_nlt(earned),
            "vesting_nlt": economics.to_nlt(vesting),
            "claimable_nlt": economics.to_nlt(earned - vesting),
            "points_this_epoch": economics.format_points(cur.get("cp_tenths", 0)),
            "next_payout_height": nxt,
            "blocks_until_next_payout": nxt - (height - 1),
            "payouts": history[-50:],
            "note": "Test coins have no value. Vested payouts are already in your spendable balance.",
        }

    def account_info(self, address: str) -> dict:
        address = normalize_address(address)
        height = len(self.chain)  # rules for the next block
        balance = self.get_balance(address)
        locked = min(balance, self.restricted_amounts(self.chain, height).get(address, 0))
        return {
            "address": address,
            "balance_nlt": economics.to_nlt(balance),
            "spendable_nlt": economics.to_nlt(balance - locked),
            "locked_or_unvested_nlt": economics.to_nlt(locked),
        }

    def buckets_info(self) -> dict:
        height = len(self.chain)
        out = {}
        for bucket, addr in self.econ.allocations.items():
            start, months = self.econ.unlocks[bucket]
            out[bucket] = {
                "address": addr,
                "allocation": economics.to_nlt(self.econ.allocation_amount(bucket)),
                "unlocked": economics.to_nlt(self.econ.unlocked(bucket, height)),
                "locked": economics.to_nlt(self.econ.still_locked(bucket, height)),
                "balance": economics.to_nlt(self.get_balance(addr)),
                "unlock_schedule": f"{months} monthly steps from height {start}",
            }
        out["community"]["paid_out"] = economics.to_nlt(sum(
            int(tx["amount"]) for b in self.chain for tx in b.transactions if is_payout_tx(tx)))
        return out

    def payout_info(self) -> dict:
        e = self.econ
        nxt = e.next_epoch_height(len(self.chain) - 1)
        preview = self.expected_payouts(self.chain, nxt) if nxt == len(self.chain) else None
        se = e.short_epoch()
        return {
            "epoch_blocks": e.epoch_length_at(len(self.chain)),
            "current_epoch": e.epoch_id(len(self.chain)),
            "testnet_short_epoch": None if not se else {"from_height": se[0], "epoch_blocks": se[1]},
            "next_epoch_height": nxt,
            "blocks_until": nxt - (len(self.chain) - 1),
            "pool_available_at_next_epoch": economics.to_nlt(self.community_pool_available(self.chain, nxt)),
            "per_address_cap": economics.to_nlt(e.payout_cap),
            "vesting_blocks": e.vesting_blocks_at(nxt),
            "next_block_payouts": None if preview is None else [
                {"recipient": p["recipient"], "amount": economics.to_nlt(p["amount"])} for p in preview],
            "treasury_reserve_spend_approval": e.spend_approval,
            **self._v4_info(nxt),
        }

    def _v4_info(self, nxt: int) -> dict:
        """Plain-words fields for the points v4 rules (empty before the fork)."""
        if not is_points_v4(nxt):
            return {}
        e = self.econ
        P = e.epoch_length_at(nxt)
        end = nxt - contributions.PAYOUT_LAG_PERIODS * P
        return {
            "points_rules": {
                "payout_lag_blocks": contributions.PAYOUT_LAG_PERIODS * P,
                "transfer_pot_percent": contributions.TX_POT_PERCENT,
                "mining_pot_percent": 100 - contributions.TX_POT_PERCENT,
                "per_address_cap_percent_of_each_pot": contributions.POT_CAP_PERCENT,
                "family_rule": bool(contributions.FAMILY_RULE_ENABLED),
                "mining_pot_note": ("The mining pot follows block share: every block counts the same, and the "
                                    "per-address cap does not stop one miner who spreads blocks over many addresses. "
                                    "It is a spreading rule, not protection for the pool."),
            },
            "next_payout_pays_window": ({"start": end - P, "end": end} if end - P >= POINTS_V4_HEIGHT else None),
        }

    def multisig_info(self, mempool: Optional[List[dict]] = None) -> dict:
        e = self.econ
        out = {"mode": e.spend_approval, "threshold": e.multisig_threshold,
               "keyholders": list(e.keyholders), "next_nonce": {}}
        for b in ("treasury", "reserve"):
            pending = [int(t["payload"]["nonce"]) for t in (mempool or [])
                       if is_multisig_tx(t) and t["payload"].get("bucket") == b]
            out["next_nonce"][b] = max([self.multisig_confirmed_nonce(b, self.chain)] + pending) + 1
        return out

    def supply_info(self) -> dict:
        minted = self.total_minted()
        paid = self.rewards_paid()
        nxt = self.expected_reward(self.chain)
        return {
            "decimals": economics.DECIMALS,
            "max_supply": economics.to_nlt(MAX_SUPPLY),
            "minted": economics.to_nlt(minted),
            "remaining": economics.to_nlt(MAX_SUPPLY - minted),
            "miner_rewards_paid": economics.to_nlt(paid),
            "miner_rewards_remaining": economics.to_nlt(self.econ.miner_bucket - paid),
            "next_block_reward": economics.to_nlt(nxt),
            "locked_allocations": {b: a for b, a in self.econ.allocations.items()},
            "placeholder_addresses": list(self.econ.placeholders),
        }

    def get_balance(self, address: str, chain: Optional[List[Block]] = None) -> int:
        address = normalize_address(address)
        balances = self.get_balances(chain)
        return balances.get(address, 0)

    def get_confirmed_nonce(self, address: str, chain: Optional[List[Block]] = None) -> int:
        if chain is None:
            chain = self.chain

        address = normalize_address(address)
        highest = 0

        for block in chain:
            for tx in block.transactions:
                if not is_signed_tx(tx):
                    continue
                payload = tx["payload"]
                if normalize_address(payload.get("sender")) == address:
                    highest = max(highest, int(payload.get("nonce", 0)))

        return highest

    def get_pending_nonce(self, address: str, mempool: List[dict]) -> int:
        address = normalize_address(address)
        highest = 0

        for tx in mempool:
            if is_reward_tx(tx):
                continue
            payload = tx.get("payload", {})
            sender = payload.get("sender")
            if isinstance(sender, str) and normalize_address(sender) == address:
                highest = max(highest, int(payload.get("nonce", 0)))

        return highest

    def get_next_nonce(self, address: str, mempool: Optional[List[dict]] = None) -> int:
        address = normalize_address(address)
        confirmed = self.get_confirmed_nonce(address)
        pending = self.get_pending_nonce(address, mempool or [])
        return max(confirmed, pending) + 1

    def validate_reward_tx(self, tx: dict, expected_recipient: str) -> bool:
        try:
            normalized = normalize_reward_tx(tx)
            return (
                normalized["recipient"] == normalize_address(expected_recipient)
                and int(normalized["amount"]) == BLOCK_REWARD
            )
        except Exception:
            return False

    def validate_signed_tx(
        self,
        tx: dict,
        temp_balances: Dict[str, int],
        temp_nonces: Dict[str, int],
        restricted: Optional[Dict[str, int]] = None,
    ) -> bool:
        try:
            tx = normalize_signed_tx(tx)

            if tx["payload"]["chain_id"] != self.econ.chain_id:
                return False  # signed for a different network
            if not verify_signed_tx(tx, chain_id=self.econ.chain_id):
                return False
            fee = int(tx["payload"]["fee"])
            if fee < self.econ.min_fee:
                return False

            payload = tx["payload"]
            sender = payload["sender"]
            recipient = payload["recipient"]
            amount = int(payload["amount"])
            nonce = int(payload["nonce"])
            pubkey = tx["pubkey"]

            if sender == recipient:
                return False

            if sender == self.econ.allocations["community"]:
                return False  # the community pool only pays out via protocol epochs
            if sender in self.econ.locked_addresses and self.econ.spend_approval != "single_key_dev":
                return False  # treasury/reserve only spend via multisig_spend (dev mode aside)

            derived_sender = pubkey_to_address(pubkey)
            if derived_sender != sender:
                return False

            expected_nonce = temp_nonces.get(sender, 0) + 1
            if nonce != expected_nonce:
                return False

            sender_balance = temp_balances.get(sender, 0)
            if restricted is None:
                restricted = {}
            # locked allocation parts and unvested payouts can't be spent
            if sender_balance - restricted.get(sender, 0) < amount + fee:
                return False

            temp_balances[sender] = sender_balance - amount - fee
            temp_balances[recipient] = temp_balances.get(recipient, 0) + amount
            temp_nonces[sender] = nonce

            return True
        except Exception:
            return False

    def multisig_confirmed_nonce(self, bucket: str, chain: List[Block]) -> int:
        return max([int(t["payload"]["nonce"]) for b in chain for t in b.transactions
                    if is_multisig_tx(t) and t["payload"]["bucket"] == bucket] or [0])

    def multisig_signers(self, tx: dict) -> int:
        """Number of distinct listed keyholders with a valid signature (0 if any sig is bad
        or any signer is duplicated or not a keyholder)."""
        msg = multisig_message(tx["payload"])
        seen = set()
        for s in tx["sigs"]:
            if s["pubkey"] in seen or s["pubkey"] not in self.econ.keyholders:
                return 0
            if not verify_signature(s["pubkey"], s["sig"], msg):
                return 0
            seen.add(s["pubkey"])
        return len(seen)

    def validate_multisig_tx(self, tx: dict, temp_balances: Dict[str, int],
                             ms_nonces: Dict[str, int], restricted: Dict[str, int]) -> bool:
        try:
            if self.econ.spend_approval != "multisig":
                return False
            tx = normalize_multisig_tx(tx)
            p = tx["payload"]
            if p["chain_id"] != self.econ.chain_id:
                return False
            src = self.econ.allocations[p["bucket"]]
            if p["recipient"] == src:
                return False
            if p["nonce"] != ms_nonces.get(p["bucket"], 0) + 1:
                return False  # replay or gap
            if self.multisig_signers(tx) < self.econ.multisig_threshold:
                return False
            if temp_balances.get(src, 0) - restricted.get(src, 0) < p["amount"]:
                return False  # never more than the unlocked amount
            temp_balances[src] = temp_balances.get(src, 0) - p["amount"]
            temp_balances[p["recipient"]] = temp_balances.get(p["recipient"], 0) + p["amount"]
            ms_nonces[p["bucket"]] = p["nonce"]
            return True
        except Exception:
            return False

    def validate_block_transactions(self, prior_chain: List[Block], block: Block) -> bool:
        try:
            if not isinstance(block.transactions, list):
                return False

            if len(block.transactions) == 0:
                return False

            reward_txs = [tx for tx in block.transactions if is_reward_tx(tx)]
            if len(reward_txs) != 1:
                return False

            if not is_reward_tx(block.transactions[0]):
                return False

            expected = self.expected_payouts(prior_chain, block.index)
            k = len(expected)
            got = [normalize_payout_tx(t) for t in block.transactions[1:1 + k] if is_payout_tx(t)]
            if got != expected:
                return False
            rest = block.transactions[1 + k:]
            p = 0
            while p < len(rest) and pause.is_pause_tx(rest[p]):
                p += 1
            if p:
                if not is_security_fork(block.index):
                    return False
                nonce = pause.state(prior_chain)["nonce"]
                for t in rest[:p]:
                    nonce += 1
                    if not pause.valid_pause_tx(t, nonce, self.econ.chain_id):
                        return False
                rest = rest[p:]
            r = 0
            while r < len(rest) and registry.is_registry_tx(rest[r]):
                r += 1
            if r:
                # registry updates sit right after any pause updates; the list is only a thing from the fork on
                if not is_points_v4(block.index) or not registry.REGISTRY_ENABLED:
                    return False
                rstate = self.registry_state(prior_chain)
                for t in rest[:r]:
                    if not rstate.check_and_apply(t, block.index, self.econ.chain_id):
                        return False
                rest = rest[r:]
            m = 0
            while m < len(rest) and is_multisig_tx(rest[m]):
                m += 1
            multisig_part, signed_part = rest[:m], rest[m:]
            if any(not is_signed_tx(t) for t in signed_part):
                return False
            if len(signed_part) > MAX_BLOCK_TXS:
                return False
            restricted = self.restricted_amounts(prior_chain, block.index)

            reward_tx = normalize_reward_tx(block.transactions[0])
            block_reward = self.expected_reward(prior_chain)
            if int(reward_tx["amount"]) != block_reward:
                return False

            temp_balances = self.get_balances(prior_chain)
            temp_nonces: Dict[str, int] = {}

            all_senders = set()
            seen_ids = set()

            for tx in signed_part:
                normalized_tx = normalize_signed_tx(tx)
                tid = tx_id(normalized_tx)
                if tid in seen_ids:
                    return False
                seen_ids.add(tid)

                sender = normalized_tx["payload"]["sender"]
                all_senders.add(sender)

            for sender in all_senders:
                temp_nonces[sender] = self.get_confirmed_nonce(sender, prior_chain)

            reward_recipient = reward_tx["recipient"]
            temp_balances[reward_recipient] = temp_balances.get(reward_recipient, 0) + block_reward

            ms_nonces = {b: self.multisig_confirmed_nonce(b, prior_chain) for b in ("treasury", "reserve")}
            for tx in multisig_part:
                if not self.validate_multisig_tx(tx, temp_balances, ms_nonces, restricted):
                    return False

            txs_by_sender: Dict[str, List[dict]] = {}
            for tx in signed_part:
                normalized_tx = normalize_signed_tx(tx)
                sender = normalized_tx["payload"]["sender"]
                txs_by_sender.setdefault(sender, []).append(normalized_tx)

            ordered_txs: List[dict] = []
            for sender in sorted(txs_by_sender.keys()):
                sender_txs = sorted(
                    txs_by_sender[sender],
                    key=lambda x: (x["payload"]["nonce"], tx_id(x)),
                )
                ordered_txs.extend(sender_txs)

            for tx in ordered_txs:
                if not self.validate_signed_tx(tx, temp_balances, temp_nonces, restricted):
                    return False

            # the miner collects exactly the block's fees (moved, not minted)
            if int(reward_tx["fees"]) != sum(int(t["payload"]["fee"]) for t in ordered_txs):
                return False

            return True
        except Exception:
            return False

    def pause_nonce_at_tip(self) -> int:
        """Last pause nonce on the chain, cached per tip (height + tip hash). A new block or a reorg changes the tip,
        so the cache can never go stale; it only saves repeating the full-chain scan for the same tip."""
        tip = (len(self.chain), self.chain[-1].hash if self.chain else "")
        cached = getattr(self, "_pause_nonce_cache", None)
        if cached is not None and cached[0] == tip:
            return cached[1]
        nonce = pause.state(self.chain)["nonce"]
        self._pause_nonce_cache = (tip, nonce)
        return nonce

    def validate_candidate_tx_against_chain_and_mempool(self, tx: dict, mempool: List[dict]) -> bool:
        try:
            if is_reward_tx(tx):
                return False

            if pause.is_pause_tx(tx):
                if not is_security_fork(len(self.chain)):
                    return False
                if any(pause.is_pause_tx(t) for t in mempool):
                    return False  # one pending pause action at a time
                # Cheap checks first (key + signature, no chain scan), so a junk pause tx cannot make a node
                # scan the whole chain. Only a correctly signed tx reaches the nonce check.
                signed = pause.signed_by_pause_key(tx, self.econ.chain_id)
                if signed is None:
                    return False
                return signed["nonce"] == self.pause_nonce_at_tip() + 1

            if registry.is_registry_tx(tx):
                if not is_points_v4(len(self.chain)) or not registry.REGISTRY_ENABLED:
                    return False
                if any(registry.is_registry_tx(t) for t in mempool):
                    return False  # one pending registry update at a time (the nonce is sequential anyway)
                return self.registry_state().check_and_apply(tx, len(self.chain), self.econ.chain_id)

            if is_multisig_tx(tx):
                tx = normalize_multisig_tx(tx)
                bucket = tx["payload"]["bucket"]
                pending = [normalize_multisig_tx(t) for t in mempool if is_multisig_tx(t)]
                pending = [t for t in pending if t["payload"]["bucket"] == bucket]
                ms_nonces = {bucket: self.multisig_confirmed_nonce(bucket, self.chain)}
                temp_balances = self.get_balances()
                restricted = self.restricted_amounts(self.chain, len(self.chain))
                for t in sorted(pending, key=lambda t: t["payload"]["nonce"]):
                    if t["payload"]["nonce"] == tx["payload"]["nonce"]:
                        return False
                    self.validate_multisig_tx(t, temp_balances, ms_nonces, restricted)
                return self.validate_multisig_tx(tx, temp_balances, ms_nonces, restricted)

            tx = normalize_signed_tx(tx)
            sender = tx["payload"]["sender"]
            nonce = int(tx["payload"]["nonce"])

            for existing in mempool:
                if not is_signed_tx(existing):
                    continue
                existing = normalize_signed_tx(existing)
                existing_payload = existing["payload"]
                if (
                    existing_payload["sender"] == sender
                    and int(existing_payload["nonce"]) == nonce
                ):
                    return False

            temp_balances = self.get_balances()
            temp_nonces = {
                sender: max(
                    self.get_confirmed_nonce(sender),
                    self.get_pending_nonce(sender, mempool),
                )
            }

            restricted = self.restricted_amounts(self.chain, len(self.chain))
            return self.validate_signed_tx(tx, temp_balances, temp_nonces, restricted)
        except Exception:
            return False

    # ----------------------------
    # Mining
    # ----------------------------

    def mine_block(self, miner_address: str, mempool: List[dict]) -> Block:
        miner_address = normalize_address(miner_address)

        latest = self.get_latest_block()
        next_index = latest.index + 1
        difficulty = self.expected_block_difficulty(self.chain)

        block_reward = self.expected_reward(self.chain)
        reward_tx = make_reward_tx(miner_address, block_reward)
        payouts = self.expected_payouts(self.chain, next_index)
        restricted = self.restricted_amounts(self.chain, next_index)

        temp_balances = self.get_balances()
        temp_balances[miner_address] = temp_balances.get(miner_address, 0) + block_reward
        temp_nonces: Dict[str, int] = {}

        pause_txs: List[dict] = []
        if is_security_fork(next_index):
            pnonce = self.pause_nonce_at_tip()
            for tx in mempool:
                if pause.is_pause_tx(tx) and pause.valid_pause_tx(tx, pnonce + 1, self.econ.chain_id):
                    pause_txs.append(pause.normalize_pause_tx(tx))
                    pnonce += 1

        registry_txs: List[dict] = []
        rstate = self.registry_state() if is_points_v4(next_index) and registry.REGISTRY_ENABLED else None
        if rstate is not None and rstate.key_at(next_index):
            for tx in sorted((t for t in mempool if registry.is_registry_tx(t)),
                             key=lambda t: t.get("nonce") if isinstance(t.get("nonce"), int) else 0):
                try:
                    n = registry.normalize_registry_tx(tx)
                except (ValueError, TypeError):
                    continue
                if rstate.check_and_apply(n, next_index, self.econ.chain_id):
                    registry_txs.append(n)

        multisig_txs: List[dict] = []
        ms_nonces = {b: self.multisig_confirmed_nonce(b, self.chain) for b in ("treasury", "reserve")}
        candidates = []
        for tx in mempool:
            try:
                if is_multisig_tx(tx):
                    candidates.append(normalize_multisig_tx(tx))
            except Exception:
                continue
        for tx in sorted(candidates, key=lambda t: (t["payload"]["bucket"], t["payload"]["nonce"])):
            if self.validate_multisig_tx(copy.deepcopy(tx), temp_balances, ms_nonces, restricted):
                multisig_txs.append(tx)

        clean_mempool: List[dict] = []
        for tx in mempool:
            try:
                if not is_signed_tx(tx):
                    continue
                clean_mempool.append(normalize_signed_tx(tx))
            except Exception:
                continue

        grouped: Dict[str, List[dict]] = {}
        for tx in clean_mempool:
            sender = tx["payload"]["sender"]
            grouped.setdefault(sender, []).append(tx)

        valid_txs: List[dict] = []

        # Highest fee first: senders whose next pending tx pays the most go first;
        # each sender's txs still go in nonce order. Stop at MAX_BLOCK_TXS.
        for sender in grouped:
            grouped[sender].sort(key=lambda x: (x["payload"]["nonce"], tx_id(x)))
        sender_order = sorted(grouped.keys(),
                              key=lambda a: (-int(grouped[a][0]["payload"]["fee"]), a))

        for sender in sender_order:
            temp_nonces[sender] = self.get_confirmed_nonce(sender)
            for tx in grouped[sender]:
                if len(valid_txs) >= MAX_BLOCK_TXS:
                    break
                tx_copy = copy.deepcopy(tx)
                if self.validate_signed_tx(tx_copy, temp_balances, temp_nonces, restricted):
                    valid_txs.append(tx)

        total_fees = sum(int(t["payload"]["fee"]) for t in valid_txs)
        reward_tx = make_reward_tx(miner_address, block_reward, total_fees)

        transactions = [reward_tx] + payouts + pause_txs + registry_txs + multisig_txs + valid_txs
        previous_hash = latest.hash
        nonce = 0

        post_fork = is_post_fork(next_index)
        min_ts = median_time_past(self.chain) if post_fork else latest.timestamp
        while True:
            timestamp = max(time.time(), min_ts + 0.001)
            if post_fork:
                difficulty = self.allowed_difficulties(self.chain, timestamp)[-1]
            block_hash = self.calculate_hash(
                index=next_index,
                timestamp=timestamp,
                transactions=transactions,
                previous_hash=previous_hash,
                nonce=nonce,
                difficulty=difficulty,
            )

            if meets_pow(block_hash, difficulty, next_index):
                return Block(
                    index=next_index,
                    timestamp=timestamp,
                    transactions=transactions,
                    previous_hash=previous_hash,
                    nonce=nonce,
                    difficulty=difficulty,
                    hash=block_hash,
                )

            nonce += 1

    # ----------------------------
    # Block Validation / Chain Validation
    # ----------------------------

    def is_valid_new_block(
        self,
        new_block: Block,
        previous_block: Block,
        prior_chain: List[Block],
    ) -> bool:
        return self.block_reject_reason(new_block, previous_block, prior_chain) is None

    def block_reject_reason(
        self,
        new_block: Block,
        previous_block: Block,
        prior_chain: List[Block],
    ) -> Optional[str]:
        """None if the block is valid, otherwise a short human-readable reason."""
        try:
            if new_block.index != previous_block.index + 1:
                return f"wrong index {new_block.index}, expected {previous_block.index + 1}"

            if new_block.previous_hash != previous_block.hash:
                return "previous_hash does not match our latest block"

            history = prior_chain + [previous_block]
            allowed = self.allowed_difficulties(history, new_block.timestamp)
            if new_block.difficulty not in allowed:
                return f"wrong difficulty {new_block.difficulty}, expected {allowed[0]}"

            if is_post_fork(new_block.index):
                if new_block.timestamp <= median_time_past(history):
                    return f"timestamp not after median of last {MTP_WINDOW} blocks"
            elif new_block.timestamp <= previous_block.timestamp:
                return "timestamp not after previous block"

            if new_block.timestamp > time.time() + MAX_FUTURE_BLOCK_SECONDS:
                return f"timestamp more than {MAX_FUTURE_BLOCK_SECONDS}s in the future"

            recalculated_hash = self.calculate_hash(
                index=new_block.index,
                timestamp=new_block.timestamp,
                transactions=new_block.transactions,
                previous_hash=new_block.previous_hash,
                nonce=new_block.nonce,
                difficulty=new_block.difficulty,
            )

            if new_block.hash != recalculated_hash:
                return "hash does not match block contents"

            if not meets_pow(new_block.hash, new_block.difficulty, new_block.index):
                return "insufficient proof of work"

            if not self.validate_block_transactions(prior_chain + [previous_block], new_block):
                return "invalid transactions (reward, signature, nonce or balance)"

            return None
        except Exception as e:
            return f"malformed block: {e}"

    def is_valid_chain(self, chain_data: List[dict]) -> bool:
        try:
            blocks = [Block.from_dict(x) for x in chain_data]
            if not blocks:
                return False

            local_genesis = self.create_genesis_block().to_dict()
            if blocks[0].to_dict() != local_genesis:
                log.warning("Rejected chain: genesis does not match config/genesis_config.json")
                return False

            for h, cp_hash in get_checkpoints().items():
                if h < len(blocks) and blocks[h].hash != cp_hash:
                    log.warning("Rejected chain: block %s does not match checkpoint", h)
                    return False

            temp_chain = [blocks[0]]

            for i in range(1, len(blocks)):
                current_block = blocks[i]
                previous_block = temp_chain[-1]
                prior_chain = temp_chain[:-1]

                if not self.is_valid_new_block(current_block, previous_block, prior_chain):
                    return False

                temp_chain.append(current_block)

            if self.total_minted(temp_chain) > MAX_SUPPLY:
                log.warning("Rejected chain: total minted exceeds MAX_SUPPLY")
                return False

            return True
        except Exception:
            return False

    def add_block(self, block_data: dict) -> bool:
        """Append a block. Returns False on rejection; the reason is logged
        and kept in self.last_reject_reason."""
        self.last_reject_reason = None
        try:
            new_block = Block.from_dict(block_data)
            previous_block = self.get_latest_block()
            prior_chain = self.chain[:-1]

            reason = self.block_reject_reason(new_block, previous_block, prior_chain)
            if not reason:
                reason = self.receive_time_reason(new_block, prior_chain + [previous_block])
            if reason:
                self.last_reject_reason = reason
                log.warning("Rejected block %s: %s", block_data.get("index"), reason)
                return False

            self.chain.append(new_block)
            self.save_chain()

            return True
        except Exception as e:
            self.last_reject_reason = f"malformed block: {e}"
            log.warning("Rejected block: %s", self.last_reject_reason)
            return False

    def receive_time_reason(self, new_block: "Block", history: List["Block"], now: float = None) -> Optional[str]:
        """Clock policy at the RECEIVE path only (review F2), never inside
        is_valid_chain: a block claiming the stall rule's emergency (1/8) work
        must not be timestamped in our future, so a miner can't pull the stall
        forward with a future timestamp. A node that refuses it here still
        accepts the same block later via normal sync once it is no longer in
        the future, so nodes can't permanently disagree on validity."""
        if not is_security_fork(new_block.index) or len(history) < 1:
            return None
        now = time.time() if now is None else now
        allowed = self.allowed_difficulties(history, new_block.timestamp)
        emergency = len(allowed) == 2 and new_block.difficulty == allowed[1] and allowed[0] != allowed[1]
        if emergency and new_block.timestamp > now:
            return "emergency-work block timestamped in the future (receive policy)"
        return None

    def reorg_depth(self, incoming: List[Block]) -> int:
        """How many of OUR blocks would be dropped by switching to `incoming`."""
        n = min(len(self.chain), len(incoming))
        common = 0
        while common < n and self.chain[common].hash == incoming[common].hash:
            common += 1
        return len(self.chain) - common

    def reorg_depth_raw(self, chain_data: List[dict]) -> int:
        """reorg_depth() from raw block dicts, using only their claimed
        hashes (cheap; no parsing or validation)."""
        n = min(len(self.chain), len(chain_data))
        common = 0
        while common < n and isinstance(chain_data[common], dict) \
                and self.chain[common].hash == chain_data[common].get("hash"):
            common += 1
        return len(self.chain) - common

    def replace_chain(self, chain_data: List[dict]) -> bool:
        # Cheap checks FIRST (review F4): refuse a too-deep reorg before
        # spending CPU on full validation of the incoming chain. A lying
        # "hash" field can only make depth look larger (refuse) or match our
        # own blocks, which full validation re-checks below.
        if not isinstance(chain_data, list) or not chain_data:
            return False
        if is_security_fork(self.chain[-1].index):
            depth = self.reorg_depth_raw(chain_data)
            if depth > MAX_REORG_DEPTH:
                self.last_reject_reason = f"reorg of {depth} blocks exceeds limit {MAX_REORG_DEPTH}"
                # review F3: the refusal itself stays cheap, but it only becomes an ALERT (and a disk write) once
                # the offered chain has been fully validated. A made-up chain with garbage hashes proves nothing.
                self._note_refused_deep_reorg(chain_data, depth)
                log.warning("Rejected chain: %s (peer advertises a chain we refuse; see docs/ECLIPSE_RECOVERY.md)",
                            self.last_reject_reason)
                return False

        if not self.is_valid_chain(chain_data):
            return False

        incoming_chain = [Block.from_dict(x) for x in chain_data]
        current_work = self.chain_work(self.chain)
        incoming_work = self.chain_work(incoming_chain)

        if incoming_work <= current_work:
            return False

        depth = self.reorg_depth(incoming_chain)
        if is_security_fork(self.chain[-1].index) and depth > MAX_REORG_DEPTH:
            self.last_reject_reason = f"reorg of {depth} blocks exceeds limit {MAX_REORG_DEPTH}"
            self.record_rejected_reorg(depth)
            log.warning("Rejected chain: %s", self.last_reject_reason)
            return False

        self.record_reorg(depth, incoming_chain[-1].index)
        self.chain = incoming_chain
        self.save_chain()
        return True

    # review F3 / N1: how often a refused deep reorg may cost us a full validation / a disk write
    REFUSED_VALIDATE_MIN_INTERVAL = 30.0      # seconds between full validations spent on refused chains
    REFUSED_SEEN_KEEP = 64                    # tips (of chains already judged) remembered for de-duplication
    REFUSED_SEEN_TTL = 600.0
    REFUSED_SAVE_MIN_INTERVAL = 30.0          # seconds between sidecar writes caused by refused chains
    REFUSED_UNVALIDATED_KEEP = 2000           # timestamps kept for the "refused, not validated" volume counter

    def _refused_chain_precheck(self, chain_data: List[dict], depth: int) -> bool:
        """review N1: a CHEAP structural test (no signatures, no balances, no full validation) that a chain offered
        as a deep reorg has to pass before it may use the validation budget or be remembered. It checks that the
        part we share with the offer really is our own blocks, and that every divergent block has the right index,
        links to the block before it, has a hash that matches its own contents, meets its proof-of-work target
        and carries a difficulty we would allow. Made-up hashes or a body that merely copies the real tip hash fail
        on the first bad block, in time linear in the offered chain and with no real validation. Only a chain that
        passes this has a tip hash that really identifies its contents, which is what makes remembering it safe.
        Passing proves nothing about the chain being valid: full validation still decides that."""
        try:
            common = len(self.chain) - depth
            if common < 1 or common >= len(chain_data):
                return False
            ours = self.chain
            for i in range(common):
                x = chain_data[i]
                if not isinstance(x, dict):
                    return False
                if x != ours[i].to_dict():
                    # same claimed hash as our block but a different dict: accept only if its contents really hash to it
                    b = Block.from_dict(x)
                    if b.hash != ours[i].hash or self.calculate_hash(
                            b.index, b.timestamp, b.transactions, b.previous_hash, b.nonce, b.difficulty) != b.hash:
                        return False
            history = list(ours[:common])
            for i in range(common, len(chain_data)):
                blk = Block.from_dict(chain_data[i])
                prev = history[-1]
                if blk.index != prev.index + 1 or blk.previous_hash != prev.hash:
                    return False
                if self.calculate_hash(blk.index, blk.timestamp, blk.transactions, blk.previous_hash,
                                       blk.nonce, blk.difficulty) != blk.hash:
                    return False
                if not meets_pow(blk.hash, blk.difficulty, blk.index):
                    return False
                if blk.difficulty not in self.allowed_difficulties(history, blk.timestamp):
                    return False
                history.append(blk)
            return True
        except Exception:
            return False

    def _count_refused_unvalidated(self, now: float) -> None:
        """review N1: volume of deep-reorg offers we refused WITHOUT reaching a full-validation verdict (failed the
        cheap precheck, or arrived while the validation budget was spent). Shown in /status and the healthcheck as
        information only; it never raises an alert and is never written to disk."""
        dq = self.__dict__.setdefault("_refused_unvalidated", deque(maxlen=self.REFUSED_UNVALIDATED_KEEP))
        dq.append(now)
        self.__dict__["_refused_unvalidated_total"] = self.__dict__.get("_refused_unvalidated_total", 0) + 1

    def _note_refused_deep_reorg(self, chain_data: List[dict], depth: int) -> None:
        """Called for an offered chain we refuse for depth alone (the refusal itself has already been decided).
        It only decides whether to COUNT it as a real refused deep reorg (an alert input):
          1. cheap structural precheck; a chain that fails it is junk: counted as 'not validated', nothing else
             (no budget used, nothing remembered);
          2. a tip already judged (valid or invalid) is not judged again for REFUSED_SEEN_TTL;
          3. at most one full validation per REFUSED_VALIDATE_MIN_INTERVAL; a chain that arrives while the budget is
             spent is counted as 'not validated' and may be offered again;
          4. the tip is remembered only after the verdict, and only chains that passed step 1 get here, so a junk body
             cannot claim somebody else's tip hash."""
        try:
            now = time.time()
            if not self._refused_chain_precheck(chain_data, depth):
                self._count_refused_unvalidated(now)
                return
            tip = chain_data[-1].get("hash")
            seen = self.__dict__.setdefault("_refused_seen", OrderedDict())          # tips judged VALID (already counted)
            bad = self.__dict__.setdefault("_refused_bad", OrderedDict())            # tips judged INVALID after step 1
            for book in (seen, bad):
                t = book.get(tip)
                if t is not None and now - t < self.REFUSED_SEEN_TTL:
                    return
            if now - self.__dict__.get("_refused_last_validation", 0.0) < self.REFUSED_VALIDATE_MIN_INTERVAL:
                self._count_refused_unvalidated(now)
                return
            self.__dict__["_refused_last_validation"] = now
            valid = self.is_valid_chain(chain_data)
            book = seen if valid else bad
            book[tip] = now
            book.move_to_end(tip)
            while len(book) > self.REFUSED_SEEN_KEEP:
                book.popitem(last=False)
            if valid:
                self.record_rejected_reorg(depth, now=now)
        except Exception:
            return

    # ---- reorg tripwire (off-chain: log + /status fields; consensus is untouched) ----
    REORG_ALERT_DEPTH = 3          # a reorg of THIS many blocks or more is logged as a WARNING and counts as unusual
    REORG_WINDOW_SECONDS = 24 * 3600
    REORG_LOG_KEEP = 200

    @staticmethod
    def _reorg_log_path() -> str:
        return f"{CHAIN_FILE}.reorgs.json"

    REORG_LOG_MAX_BYTES = 256 * 1024   # review F4: the real file is a few KB; refuse to parse anything bigger

    @staticmethod
    def _log_number_ok(v, hi: float) -> bool:
        """A plain finite number in range. Rejects bool, NaN, +-Infinity and absurdly large values (review F4)."""
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return False
        try:
            return math.isfinite(v) and 0 <= v <= hi
        except (OverflowError, ValueError):
            return False

    def _load_reorg_log(self) -> None:
        """Read the sidecar written by _save_reorg_log. A missing, hostile or damaged file just means an empty log:
        the tripwire is a warning light, never a reason to refuse to start (review F4: any failure, including
        RecursionError from deep nesting, NaN/Infinity, a huge file, a symlink or a pipe, is survived)."""
        fd = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
            fd = os.open(self._reorg_log_path(), flags)
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_size > self.REORG_LOG_MAX_BYTES:
                return
            with os.fdopen(fd, "rb") as f:
                fd = None
                data = f.read(self.REORG_LOG_MAX_BYTES + 1)
            if len(data) > self.REORG_LOG_MAX_BYTES:
                return

            def refuse_constant(name):
                raise ValueError(f"non-finite number {name}")
            raw = json.loads(data.decode("utf-8"), parse_constant=refuse_constant)
            ok = self._log_number_ok
            limits = {"time": 1e12, "height": 1e12, "depth": 1e12}

            def clean(items, keys):
                out = []
                for x in items if isinstance(items, list) else []:
                    if isinstance(x, dict) and all(ok(x.get(k), limits[k]) for k in keys):
                        out.append({k: x[k] for k in keys})
                return out[-self.REORG_LOG_KEEP:]
            self.reorg_events = clean(raw.get("accepted"), ("time", "height", "depth"))
            self.rejected_reorg_events = clean(raw.get("rejected"), ("time", "depth"))
        except (OSError, ValueError, AttributeError, TypeError, RecursionError, MemoryError, OverflowError):
            pass
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def _save_reorg_log(self) -> None:
        try:
            path = self._reorg_log_path()
            tmp = f"{path}.tmp"
            try:
                os.unlink(tmp)                  # a stale or planted temp name; O_EXCL below refuses anything that reappears
            except FileNotFoundError:
                pass
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                         | getattr(os, "O_CLOEXEC", 0), 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"accepted": self.reorg_events[-self.REORG_LOG_KEEP:],
                           "rejected": self.rejected_reorg_events[-self.REORG_LOG_KEEP:]}, f, allow_nan=False)
            os.replace(tmp, path)
        except (OSError, ValueError) as e:
            log.warning("Could not save the reorg log (%s); the tripwire still works until restart.", e)

    def record_reorg(self, depth: int, new_height: int, now: Optional[float] = None) -> None:
        """Remember an accepted reorg that dropped `depth` of our blocks (saved to the sidecar file)."""
        if depth <= 0:
            return
        now = time.time() if now is None else now
        self.reorg_events.append({"time": now, "height": int(new_height), "depth": int(depth)})
        del self.reorg_events[:-self.REORG_LOG_KEEP]
        self._save_reorg_log()
        if depth >= self.REORG_ALERT_DEPTH:
            log.warning("REORG of %d blocks accepted (new tip %d). %d or more is unusual: "
                        "check docs/INCIDENT_PLAN.md.", depth, new_height, self.REORG_ALERT_DEPTH)

    def record_rejected_reorg(self, depth: int, now: Optional[float] = None) -> None:
        """Count a chain we REFUSED because it would rewrite more than MAX_REORG_DEPTH blocks. A peer that keeps
        offering one is either badly out of sync or attacking; the count shows up in /status."""
        now = time.time() if now is None else now
        self.rejected_reorg_events.append({"time": now, "depth": int(depth)})
        del self.rejected_reorg_events[:-self.REORG_LOG_KEEP]
        # review F3: the in-memory count (what /status shows) is always exact; the disk copy is refreshed at most
        # once per REFUSED_SAVE_MIN_INTERVAL so a stream of refused chains cannot become a stream of file writes.
        mono = time.monotonic()
        last = self.__dict__.get("_refused_last_save")
        if last is None or mono - last >= self.REFUSED_SAVE_MIN_INTERVAL:
            self.__dict__["_refused_last_save"] = mono
            self._save_reorg_log()

    def reorg_info(self, now: Optional[float] = None) -> dict:
        now = time.time() if now is None else now
        recent = [x for x in self.reorg_events if now - x["time"] <= self.REORG_WINDOW_SECONDS]
        rejected = [x for x in self.rejected_reorg_events if now - x["time"] <= self.REORG_WINDOW_SECONDS]
        last = self.reorg_events[-1] if self.reorg_events else None
        return {"last_reorg_depth": last["depth"] if last else 0,
                "last_reorg_height": last["height"] if last else None,
                "last_reorg_time": last["time"] if last else None,
                "max_reorg_depth_24h": max((x["depth"] for x in recent), default=0),
                "reorgs_24h": len(recent),
                "rejected_deep_reorgs_24h": len(rejected),
                "refused_unvalidated_deep_reorgs_24h": sum(1 for t in self.__dict__.get("_refused_unvalidated", ())
                                                           if now - t <= self.REORG_WINDOW_SECONDS),
                "refused_unvalidated_deep_reorgs_total": self.__dict__.get("_refused_unvalidated_total", 0),
                "max_rejected_reorg_depth_24h": max((x["depth"] for x in rejected), default=0),
                "reorg_alert_depth": self.REORG_ALERT_DEPTH,
                "reorg_limit": MAX_REORG_DEPTH}
