"""Contribution points, computed from the chain itself.

Points are integers in tenths (POINT_SCALE = 10) so fractional weights stay exact.
Default weights (set in config/genesis_config.json):
  - 2.5 points (25 tenths) to the miner of a block, but 0 for an "empty self-mined"
    block: no transfers at all, or only transfers from the miner to the miner.
  - 1 point (10 tenths) to the sender of each *qualifying* signed transfer.
    Anti-farming (proposals): a transfer qualifies only if amount >= MIN_POINT_TRANSFER
    (1 NLT), the recipient is not the sender, and the sender->recipient pair has
    had fewer than PAIR_CAP_PER_EPOCH (10) qualifying transfers in this epoch
    (epoch = block index // epoch_blocks, default one month). Non-qualifying transfers
    are still valid; they just earn no points. All of this is computed from the chain.
Genesis allocations, community payouts and multisig spends earn nothing.

state_contributions.json is only a cache of this result. It is rebuilt
whenever the chain is loaded or changes (new block or chain replacement), so it
can't drift from the chain. Delete it any time; it comes back.
"""
import json
from typing import Dict, Iterable

from economics import (DEFAULT_EPOCH_BLOCKS, DEFAULT_POINTS_PER_BLOCK_TENTHS,
                       DEFAULT_POINTS_PER_TX_TENTHS, MIN_POINT_TRANSFER,
                       PAIR_CAP_PER_EPOCH, format_points)

STATE_FILE = "state_contributions.json"


def _is_transfer(tx: dict) -> bool:
    return isinstance(tx, dict) and "payload" in tx and "type" not in tx


def is_empty_self_mined(block: dict) -> bool:
    txs = block.get("transactions") or []
    if not txs or txs[0].get("type") != "reward":
        return True
    miner = txs[0]["recipient"]
    transfers = [t["payload"] for t in txs if _is_transfer(t)]
    return all(p["sender"] == miner and p["recipient"] == miner for p in transfers)


# ---- Anti-farming v2 (active from `rules_height`, set by core to SECURITY_FORK_HEIGHT) ----
MIN_ADDRESS_AGE_BLOCKS = 1440     # ~1 day at 60 s (set 2026-10-23); a sender must have first received coins this many blocks earlier
SENDER_TX_CAP_PER_EPOCH = 30      # point-earning transfers per sender per epoch
# Reciprocal rule: if A->B already earned this epoch, B->A earns nothing (ping-pong farming).


def first_seen_heights(blocks: Iterable[dict]) -> Dict[str, int]:
    """Height at which each address first received anything (reward, transfer,
    payout or genesis allocation)."""
    seen: Dict[str, int] = {}
    for pos, block in enumerate(blocks):
        idx = int(block.get("index", pos))
        for tx in block.get("transactions") or []:
            if not isinstance(tx, dict):
                continue
            if "payload" in tx:
                r = tx["payload"].get("recipient")
            else:
                r = tx.get("recipient")
            if isinstance(r, str) and r not in seen:
                seen[r] = idx
    return seen


def points_from_blocks(
    blocks: Iterable[dict],
    per_block: int = DEFAULT_POINTS_PER_BLOCK_TENTHS,
    per_tx: int = DEFAULT_POINTS_PER_TX_TENTHS,
    min_transfer: int = MIN_POINT_TRANSFER,
    pair_cap: int = PAIR_CAP_PER_EPOCH,
    epoch_blocks: int = DEFAULT_EPOCH_BLOCKS,
    epoch_fn=None,
    rules_height: int = None,
    first_seen: Dict[str, int] = None,
) -> Dict[str, Dict]:
    """blocks: block dicts in chain order.
    Returns {address: {cp_tenths, cp, blocks_mined, empty_blocks, tx_count, point_tx_count}}.
    tx_count counts every transfer sent; point_tx_count only those that earned points."""
    users: Dict[str, Dict] = {}
    pair_counts: Dict[tuple, int] = {}
    sender_counts: Dict[tuple, int] = {}

    def user(addr: str) -> Dict:
        return users.setdefault(
            addr, {"cp_tenths": 0, "blocks_mined": 0, "empty_blocks": 0, "tx_count": 0,
                   "point_tx_count": 0})

    for pos, block in enumerate(blocks):
        idx = int(block.get("index", pos))
        epoch = epoch_fn(idx) if epoch_fn else idx // max(1, int(epoch_blocks))
        txs = block.get("transactions") or []
        if txs and txs[0].get("type") == "reward":
            u = user(txs[0]["recipient"])
            u["blocks_mined"] += 1
            if is_empty_self_mined(block):
                u["empty_blocks"] += 1
            else:
                u["cp_tenths"] += per_block
        for tx in txs:
            if _is_transfer(tx):
                p = tx["payload"]
                u = user(p["sender"])
                u["tx_count"] += 1
                if p["sender"] == p["recipient"] or int(p.get("amount", 0)) < min_transfer:
                    continue
                key = (epoch, p["sender"], p["recipient"])
                if pair_counts.get(key, 0) >= pair_cap:
                    continue
                if rules_height is not None and idx >= rules_height:
                    if pair_counts.get((epoch, p["recipient"], p["sender"]), 0) > 0:
                        continue  # reciprocal ping-pong
                    if sender_counts.get((epoch, p["sender"]), 0) >= SENDER_TX_CAP_PER_EPOCH:
                        continue
                    if first_seen is not None and \
                            idx - first_seen.get(p["sender"], idx) < MIN_ADDRESS_AGE_BLOCKS:
                        continue  # brand-new address
                    sender_counts[(epoch, p["sender"])] = sender_counts.get((epoch, p["sender"]), 0) + 1
                pair_counts[key] = pair_counts.get(key, 0) + 1
                u["cp_tenths"] += per_tx
                u["point_tx_count"] += 1

    for u in users.values():
        u["cp"] = format_points(u["cp_tenths"])
    return users


def save_cache(users: Dict[str, Dict], height: int) -> None:
    state = {
        "note": "Cache only: rebuilt from chain.json on load and on every chain change.",
        "height": height,
        "users": users,
    }
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def load_state() -> Dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"users": {}}
