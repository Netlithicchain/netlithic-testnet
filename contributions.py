"""Contribution points, computed from the chain itself.

Points are integers in tenths (POINT_SCALE = 10) so fractional weights stay exact.
Default weights (set in config/genesis_config.json):
  - 2.5 points (25 tenths) to the miner of a block. Before the points-v4 fork height an "empty self-mined"
    block (no transfers at all, or only miner-to-miner transfers) earned 0; from the fork on EVERY block counts,
    because that discount was free to dodge (see flat_block_points_height). Mining-pot share = block share.
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
from typing import Callable, Dict, Iterable, List, Optional

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


# ---- Points v3 (applies to payouts at height >= core.POINTS_V4_HEIGHT) ----
MAX_BASE_PER_POINT: Optional[int] = None   # review M2: optional ceiling on NLT paid per whole point (off)
# Review M1 decision: the family rule is OFF in consensus (design A: slow release + two pots only). The code, the
# issuer list and the tests stay so a later announced fork can switch it on.
FAMILY_RULE_ENABLED = False
FAMILY_PAIR_CAP: Optional[int] = None      # transfer points per (sender family, recipient family) pair per period; None = off
# ---- Points v4 (same fork height as v3; v3 never shipped, v4 replaces it) ----
TX_POT_PERCENT = 20        # share of each period's release paid for transfers; the other 80% is the mining pot (v3 proposed 30)
POT_CAP_PERCENT = 2        # one address can take at most this % of EACH pot per period (floor, in base units); None = off
PAYOUT_LAG_PERIODS = 1     # payout for period N is paid in the block that ends period N+1 (1,440 blocks later); 0 = old v3 timing
# Addresses that hand out coins to many unrelated people. An address first funded by one of these starts
# a family of its own instead of joining the issuer's. All from public sources (the explorer /founder page and the genesis file):
FAMILY_ISSUERS: tuple = (
    "e924523b2614a39b9bb4ff8bfc05e747",  # founder server miner + faucet: explorer /founder page, 6 x 10 NLT drips on chain
    "f953a95407a5ae8bba986101d1f9b272",  # genesis allocation: community (locked, pays only by protocol payout)
    "e26742c7bcdb96fa0ca9446073b04a35",  # genesis allocation: treasury (locked)
    "65ca7858ba65ee7a27d8b2879197d973",  # genesis allocation: reserve (locked)
)


class FamilyIssuerError(RuntimeError):
    """FAMILY_ISSUERS is empty or malformed: refuse to start rather than run with a rule nobody can fix later."""


def validate_family_issuers(issuers: tuple = None) -> tuple:
    """The list is consensus data ONLY while FAMILY_RULE_ENABLED is on. With the rule off (the shipped v4 build) the
    list is never read for payouts, so an empty or odd list can neither stop the node nor change who earns: this
    returns the list untouched. With the rule on, a bad list must stop the node at startup.
    Valid = non-empty tuple of distinct 32-char lowercase hex addresses."""
    if not FAMILY_RULE_ENABLED and issuers is None:
        return FAMILY_ISSUERS
    issuers = FAMILY_ISSUERS if issuers is None else issuers
    if not isinstance(issuers, tuple) or not issuers:
        raise FamilyIssuerError("FAMILY_ISSUERS is empty: this build must ship the public issuer addresses "
                                "Refusing to start.")
    for a in issuers:
        if not (isinstance(a, str) and len(a) == 32 and all(ch in "0123456789abcdef" for ch in a)):
            raise FamilyIssuerError(f"FAMILY_ISSUERS has a malformed address: {a!r} (need 32 lowercase hex characters).")
    if len(set(issuers)) != len(issuers):
        raise FamilyIssuerError("FAMILY_ISSUERS has a duplicate address.")
    return issuers


def _scan_funding(blocks: Iterable[dict], min_amount: int, issuers: tuple):
    """One pass over the chain. Returns ({address: family root}, issuer_was_seen_funding).

    * Chain-issued coins (reward, payout, allocation, treasury/reserve multisig_spend) make the
      recipient its own root at once.
    * A transfer only counts once the recipient has received at least `min_amount` in total. The
      transfer that takes it over that line decides its family: its own, if the sender is an issuer;
      the sender's family, if the sender has one. A sender with no family (it has only ever received
      dust, or only from such senders) decides nothing: the recipient stays unrooted until a funder
      with a family pays it. Unrooted addresses earn no transfer points (points_from_blocks).
    Every value in the map is itself a root, so families are never chained."""
    root: Dict[str, str] = {}
    received: Dict[str, int] = {}
    issuer_seen = False
    for block in blocks:
        for tx in block.get("transactions") or []:
            if not isinstance(tx, dict):
                continue
            if not _is_transfer(tx):
                r = tx.get("recipient")
                if r is None and isinstance(tx.get("payload"), dict):   # multisig_spend: treasury/reserve grant
                    r = tx["payload"].get("recipient")
                if isinstance(r, str) and r not in root:
                    root[r] = r
                continue
            p = tx["payload"]
            r, s, amt = p.get("recipient"), p.get("sender"), int(p.get("amount", 0))
            if not isinstance(r, str) or r == s:
                continue
            if s in issuers and amt >= min_amount:
                issuer_seen = True
            if r in root:
                continue
            received[r] = received.get(r, 0) + amt
            if received[r] >= min_amount:
                if s in issuers:
                    root[r] = r
                elif s in root:
                    root[r] = root[s]
    return root, issuer_seen


def family_roots(blocks: Iterable[dict], min_amount: int = MIN_POINT_TRANSFER,
                 issuers: tuple = None) -> Dict[str, str]:
    """Funding family of every address: {address: root address}. See _scan_funding.
    Transfers inside one family earn no points in v3: an account farm funded from one
    hub is one family, however many addresses it has."""
    return _scan_funding(blocks, min_amount, FAMILY_ISSUERS if issuers is None else issuers)[0]


def active_family_roots(blocks: Iterable[dict], min_amount: int = MIN_POINT_TRANSFER,
                        issuers: tuple = None) -> Optional[Dict[str, str]]:
    """family_roots, or None (= design A only, no family rule) when the issuer list is empty or none of
    its addresses ever funded anyone on this chain. That is the safe failure: a wrong or missing issuer
    list makes the rule weaker, never makes honest users earn 0."""
    issuers = FAMILY_ISSUERS if issuers is None else issuers
    if not issuers:
        return None
    roots, issuer_seen = _scan_funding(blocks, min_amount, issuers)
    return roots if issuer_seen else None


def unlisted_funders(blocks: Iterable[dict], issuers: tuple = None, min_amount: int = MIN_POINT_TRANSFER,
                     min_recipients: int = 3) -> Dict[str, int]:
    """Release check: addresses NOT in the issuer list that sent >= min_amount to at least `min_recipients`
    different addresses. Each is either a hub (fine) or an issuer that should be listed. {address: recipients}."""
    issuers = FAMILY_ISSUERS if issuers is None else issuers
    sent: Dict[str, set] = {}
    for block in blocks:
        for tx in block.get("transactions") or []:
            if isinstance(tx, dict) and "payload" in tx:
                p = tx["payload"]
                if p.get("sender") not in issuers and p.get("sender") != p.get("recipient") \
                        and int(p.get("amount", 0)) >= min_amount:
                    sent.setdefault(p["sender"], set()).add(p["recipient"])
    return {a: len(r) for a, r in sorted(sent.items()) if len(r) >= min_recipients}


def v3_amounts(users: Dict[str, Dict], per_block: int, per_tx: int, budget: int, tx_percent: int,
               cap: int, excluded=(), max_per_point: Optional[int] = None,
               pot_cap_percent: Optional[int] = None) -> Dict[str, int]:
    """Split one period's release `budget` into a transfer pot and a mining pot, each pro rata
    by that kind of points, each address capped. Unspent budget (empty pot, rounding, cap) is not paid.
    max_per_point (base units per whole point, None = off): a pot never pays more than this per point,
    so a quiet network does not hand its whole pot to one or two people (review M2).
    pot_cap_percent (v4, None = off): one address takes at most this % of EACH pot (rounded down), on top of `cap`.
    Whatever the cap holds back is not shared out again: it stays in the pool."""
    live = {a: u for a, u in users.items() if a not in excluded}
    mine = {a: (u["blocks_mined"] - u["empty_blocks"]) * per_block for a, u in live.items()}
    tx = {a: u["point_tx_count"] * per_tx for a, u in live.items()}
    mine = {a: v for a, v in mine.items() if v > 0}
    tx = {a: v for a, v in tx.items() if v > 0}
    tx_budget = budget * tx_percent // 100
    mine_budget = budget - tx_budget
    total_mine, total_tx = sum(mine.values()), sum(tx.values())
    if max_per_point is not None:
        mine_budget = min(mine_budget, total_mine * max_per_point // 10)
        tx_budget = min(tx_budget, total_tx * max_per_point // 10)
    mine_cap = mine_budget * pot_cap_percent // 100 if pot_cap_percent is not None else None
    tx_cap = tx_budget * pot_cap_percent // 100 if pot_cap_percent is not None else None
    out: Dict[str, int] = {}
    for a in sorted(set(mine) | set(tx)):
        from_mine = mine_budget * mine.get(a, 0) // total_mine if total_mine else 0
        from_tx = tx_budget * tx.get(a, 0) // total_tx if total_tx else 0
        if mine_cap is not None:
            from_mine = min(from_mine, mine_cap)
            from_tx = min(from_tx, tx_cap)
        amount = min(from_mine + from_tx, cap)
        if amount > 0:
            out[a] = amount
    return out


# Why a transfer earned 0 points, in plain words (shown by /points_explain and the explorer address page).
REASON_TEXT = {
    "earned": "Earned 1 point.",
    "self": "You sent it to your own address.",
    "below_minimum": "Under the 1 NLT minimum for a point.",
    "unrooted": "The sender's coins cannot be traced to a real source: it has only ever received amounts under "
                "1 NLT, so it cannot earn transfer points yet. Receiving 1 NLT or more from a normal address fixes this.",
    "family_pair_cap": "These two funding families already earned the points they can share in one reward period.",
    "same_family": "Same funding family as the sender: both addresses trace back to the same first funder, "
                   "so it does not count as a payment between different people.",
    "pair_cap": "This sender and recipient already earned the 10 points they can share in one reward period.",
    "reciprocal": "The recipient already sent to the sender in this reward period (back-and-forth payments do not count).",
    "sender_cap": "The sender already reached 30 point-earning transfers in this reward period.",
    "not_registered": "For now only registered contributors earn transfer points (a temporary rule). Your address is "
                      "not on the list yet, or was added less than a day ago. Mining points are open to everyone.",
    "too_new": "The sender's address is less than 1440 blocks old (the new-address wait).",
}


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
    v3: bool = False,
    roots: Dict[str, str] = None,
    explain: List[dict] = None,
    family_pair_cap: int = None,
    registry: Optional[Callable[[str, int], bool]] = None,
    flat_block_points_height: Optional[int] = None,
) -> Dict[str, Dict]:
    """blocks: block dicts in chain order.
    Returns {address: {cp_tenths, cp, blocks_mined, empty_blocks, tx_count, point_tx_count}}.
    tx_count counts every transfer sent; point_tx_count only those that earned points.
    v3 (with `roots` from family_roots): transfers between addresses of one funding family earn nothing.
    registry (registered-contributors option): a function (sender, height) -> bool. A transfer
    whose sender it refuses earns nothing and uses up none of the caps. None = open (no registry).
    flat_block_points_height (points v4 fix H1): from this height on EVERY mined block earns the block points. The old
    "empty self-mined block earns 0" discount cannot be enforced (one 1-base-unit transfer to another own address
    defeats it for ~zero cost), so after the fork it is gone; blocks below this height keep the old rule so history
    and the pre-fork payouts are unchanged. None = the old rule everywhere (legacy callers and tests).
    explain: if a list is given, one {height, sender, recipient, amount, reason, earned} is appended per transfer."""
    users: Dict[str, Dict] = {}
    pair_counts: Dict[tuple, int] = {}
    sender_counts: Dict[tuple, int] = {}
    family_counts: Dict[tuple, int] = {}

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
            flat = flat_block_points_height is not None and idx >= flat_block_points_height
            if not flat and is_empty_self_mined(block):
                u["empty_blocks"] += 1
            else:
                u["cp_tenths"] += per_block
        for tx in txs:
            if _is_transfer(tx):
                p = tx["payload"]
                u = user(p["sender"])
                u["tx_count"] += 1
                reason = None
                if p["sender"] == p["recipient"]:
                    reason = "self"
                elif int(p.get("amount", 0)) < min_transfer:
                    reason = "below_minimum"
                elif registry is not None and not registry(p["sender"], idx):
                    reason = "not_registered"  # transfer pot is for registered contributors only (temporary)
                elif v3 and roots is not None and (p["sender"] not in roots or p["recipient"] not in roots):
                    reason = "unrooted"       # funded only by dust (or by such addresses): no traceable funding
                elif v3 and roots is not None and roots[p["sender"]] == roots[p["recipient"]]:
                    reason = "same_family"
                else:
                    key = (epoch, p["sender"], p["recipient"])
                    enforced = rules_height is not None and idx >= rules_height
                    if pair_counts.get(key, 0) >= pair_cap:
                        reason = "pair_cap"
                    elif enforced and pair_counts.get((epoch, p["recipient"], p["sender"]), 0) > 0:
                        reason = "reciprocal"
                    elif enforced and sender_counts.get((epoch, p["sender"]), 0) >= SENDER_TX_CAP_PER_EPOCH:
                        reason = "sender_cap"
                    elif enforced and first_seen is not None and \
                            idx - first_seen.get(p["sender"], idx) < MIN_ADDRESS_AGE_BLOCKS:
                        reason = "too_new"
                    elif family_pair_cap and v3 and roots is not None and \
                            family_counts.get((epoch, roots[p["sender"]], roots[p["recipient"]]), 0) >= family_pair_cap:
                        reason = "family_pair_cap"
                    else:
                        if family_pair_cap and v3 and roots is not None:
                            fk = (epoch, roots[p["sender"]], roots[p["recipient"]])
                            family_counts[fk] = family_counts.get(fk, 0) + 1
                        if enforced:
                            sender_counts[(epoch, p["sender"])] = sender_counts.get((epoch, p["sender"]), 0) + 1
                        pair_counts[key] = pair_counts.get(key, 0) + 1
                        u["cp_tenths"] += per_tx
                        u["point_tx_count"] += 1
                        reason = "earned"
                if explain is not None:
                    explain.append({"height": idx, "sender": p["sender"], "recipient": p["recipient"],
                                    "amount": int(p.get("amount", 0)), "reason": reason, "earned": reason == "earned"})

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
