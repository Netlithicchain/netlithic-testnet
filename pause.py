"""Payout pause switch (key-holder-only, public, logged). OFF until PAUSE_PUBKEY is set.

A pause is an on-chain transaction, so every pause/resume is public and
permanent in the chain (the log is the chain itself):

    {"type": "pause", "paused": true|false, "reason": "...", "nonce": n,
     "pubkey": <PAUSE_PUBKEY>, "sig": <Ed25519 over pause_message(...)>}

Rules (consensus, from core.SECURITY_FORK_HEIGHT):
  * only PAUSE_PUBKEY may sign; nonces go 1, 2, 3... (no replay);
  * signed over chain_id too (no cross-chain replay);
  * reason is required (1..280 chars) so every pause explains itself;
  * while paused, epoch payouts are skipped and the pool simply keeps the
    coins; nothing else (transfers, mining) is affected;
  * a pause/resume in block N takes effect for payouts at heights > N;
  * AUTO-EXPIRY: a pause ends by itself PAUSE_EXPIRY_BLOCKS (10080, ~7 days at 60 s)
    after its latest pause/renew tx. Renewing = another signed paused=true tx
    while the pause is active (public, logged as "renew"). A paused=true tx
    after expiry is a new "pause";
  * NO BACK PAY: epochs skipped while paused are never paid later; their coins
    just stay in the community pool for future epochs.
The switch can only STOP payouts. It can't move coins, change balances,
or touch the treasury.
"""
from typing import Dict, Iterable, List, Optional

from wallet import canonical, is_valid_pubkey_hex, pubkey_from_private, verify_signature

# ---------------------------------------------------------------------------------------------
# PAUSE KEY SLOT. This is the ONLY place the pause PUBLIC key goes (64 lowercase hex characters).
#
#   * It is EMPTY on purpose until the founder hands over his PUBLIC key from the offline ceremony
#     (docs/PAUSE_KEY_CEREMONY.md). Empty = the pause switch is disabled.
#   * Fill it with the tool, never by hand:   python ops/set_pause_pubkey.py <public key hex>
#   * Check it before a release:              python ops/set_pause_pubkey.py --check
#     (exits 1 while the slot is empty, so an empty 0.5.1 can never pass the release gate).
#   * NEVER put a private key here. The private key lives only on paper/metal and an offline computer.
#   * Every node must carry the SAME value. A node with a different or empty value rejects the
#     block that carries a pause tx and forks off. /status shows pause_enabled and pause_key_id.
# ---------------------------------------------------------------------------------------------
PAUSE_PUBKEY = "054a1d9948c3325059835edb1d4fc3f74f40ce2f12bf2417b2ac180eb2737029"  # PAUSE-PUBKEY-SLOT: empty = feature disabled; set only via ops/set_pause_pubkey.py
MAX_REASON = 280
PAUSE_EXPIRY_BLOCKS = 10080  # ~7 days at 60 s (set 2026-10-23); a pause lapses this many blocks after its last pause/renew


def key_id(pubkey: Optional[str] = None) -> str:
    """Short public fingerprint of the pause key (first 16 hex of SHA-256); "" while the slot is empty.

    Shown in /status as pause_key_id so everyone can compare it with the value the founder published."""
    import hashlib
    pubkey = PAUSE_PUBKEY if pubkey is None else pubkey
    if pubkey is None or pubkey == "":
        return ""
    if not isinstance(pubkey, str):
        return "invalid"          # a number, list, bytes...: never crash /status over a bad slot
    try:
        return hashlib.sha256(bytes.fromhex(pubkey.lower())).hexdigest()[:16]
    except (ValueError, TypeError):
        return "invalid"          # not hex at all: never crash /status over it (see key_problem)


_KEY_PROBLEM_CACHE: Dict[str, str] = {}


def key_problem(pubkey: Optional[str] = None) -> str:
    """Why the configured pause key is unusable, or "" when it is fine.

    "" for a good key. "empty" while the slot is empty (the switch is disabled: normal for dev builds, wrong for a
    release). A readable message when the slot holds something that is not a good Ed25519 key (wrong length, not
    lowercase hex, not on the curve, or not in the prime-order subgroup): nobody could ever sign a valid pause, so
    this is a release mistake. Information only: this never changes consensus or the rules fingerprint; it is for the
    startup warning and the /status flag. Cached per value (the subgroup check is a few milliseconds)."""
    pubkey = PAUSE_PUBKEY if pubkey is None else pubkey
    if not pubkey:
        return "empty"
    if not isinstance(pubkey, str):
        return "PAUSE_PUBKEY is not text"
    hit = _KEY_PROBLEM_CACHE.get(pubkey)
    if hit is None:
        try:
            ok = is_valid_pubkey_hex(pubkey)
        except Exception:                     # never let a diagnostic take the node down
            ok = False
        hit = "" if ok else ("PAUSE_PUBKEY is not a valid Ed25519 public key (needs 64 lowercase hex characters that "
                             "pass the curve and prime-order-subgroup check): no pause could ever be accepted")
        if len(_KEY_PROBLEM_CACHE) < 64:
            _KEY_PROBLEM_CACHE[pubkey] = hit
    return hit


def is_pause_tx(tx) -> bool:
    return isinstance(tx, dict) and tx.get("type") == "pause"


def pause_payload(paused: bool, reason: str, nonce: int, chain_id: str) -> dict:
    return {"paused": bool(paused), "reason": str(reason), "nonce": int(nonce), "chain_id": chain_id}


def pause_message(payload: dict) -> bytes:
    return canonical({"payout_pause": payload}).encode("utf-8")


def normalize_pause_tx(tx: dict) -> dict:
    if not is_pause_tx(tx):
        raise ValueError("Not a pause tx")
    reason = tx.get("reason")
    if not isinstance(reason, str) or not 1 <= len(reason) <= MAX_REASON:
        raise ValueError("Pause reason must be 1-280 chars")
    if not isinstance(tx.get("paused"), bool):
        raise ValueError("paused must be true/false")
    nonce = int(tx.get("nonce", 0))
    if nonce <= 0:
        raise ValueError("Nonce must be > 0")
    return {"type": "pause", "paused": tx["paused"], "reason": reason, "nonce": nonce,
            "pubkey": str(tx.get("pubkey", "")).lower(), "sig": str(tx.get("sig", "")).lower()}


def make_pause_tx(private_key_hex: str, paused: bool, reason: str, nonce: int, chain_id: str) -> dict:
    """Sign a pause/resume (run on the key holder's own machine, never on a server)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_key_hex))
    pub = pubkey_from_private(private_key_hex)
    sig = sk.sign(pause_message(pause_payload(paused, reason, nonce, chain_id))).hex()
    return {"type": "pause", "paused": bool(paused), "reason": reason, "nonce": int(nonce),
            "pubkey": pub, "sig": sig}


def signed_by_pause_key(tx: dict, chain_id: str, pubkey: Optional[str] = None) -> Optional[dict]:
    """The normalized tx if it is well formed, carries the pause key and has a valid signature for this chain; else
    None. Looks at no chain data, so it is cheap: check this BEFORE anything that scans the chain."""
    pubkey = PAUSE_PUBKEY if pubkey is None else pubkey
    try:
        tx = normalize_pause_tx(tx)
    except (ValueError, TypeError):
        return None
    if not pubkey or tx["pubkey"] != pubkey.lower():
        return None
    msg = pause_message(pause_payload(tx["paused"], tx["reason"], tx["nonce"], chain_id))
    return tx if verify_signature(tx["pubkey"], tx["sig"], msg) else None


def valid_pause_tx(tx: dict, expected_nonce: int, chain_id: str, pubkey: Optional[str] = None) -> bool:
    signed = signed_by_pause_key(tx, chain_id, pubkey)
    return signed is not None and signed["nonce"] == expected_nonce


def _active(on: Optional[int], height: int, expiry: int) -> bool:
    return on is not None and 0 < height - on <= expiry


def _scan(blocks: Iterable, expiry: int):
    """Return (log, height of last pause/renew still in force or None, tip height)."""
    log: List[Dict] = []
    on: Optional[int] = None
    tip = -1
    for b in blocks:
        d = b if isinstance(b, dict) else b.to_dict()
        tip = d["index"]
        for tx in d.get("transactions") or []:
            if not is_pause_tx(tx):
                continue
            h = d["index"]
            if tx["paused"]:
                action = "renew" if _active(on, h, expiry) else "pause"
                on = h
            else:
                action, on = "resume", None
            log.append({"height": h, "paused": tx["paused"], "action": action,
                        "reason": tx["reason"], "nonce": tx["nonce"],
                        "expires_at": on + expiry if on is not None else None})
    return log, on, tip


def pause_log(blocks: Iterable) -> List[Dict]:
    """Every pause/renew/resume on the chain, oldest first (blocks: Block or dict)."""
    return _scan(blocks, PAUSE_EXPIRY_BLOCKS)[0]


def state(blocks: Iterable, height: Optional[int] = None, expiry: Optional[int] = None) -> Dict:
    """Pause state as seen by a block at `height` (default: next block after the tip)."""
    expiry = PAUSE_EXPIRY_BLOCKS if expiry is None else expiry
    log, on, tip = _scan(blocks, expiry)
    height = tip + 1 if height is None else height
    paused = _active(on, height, expiry)
    return {"paused": paused, "nonce": log[-1]["nonce"] if log else 0,
            "expires_at": on + expiry if on is not None else None,
            "expired": on is not None and not paused,
            "expiry_blocks": expiry, "enabled": bool(PAUSE_PUBKEY), "log": log}
