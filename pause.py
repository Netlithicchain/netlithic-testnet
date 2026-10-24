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

from wallet import canonical, pubkey_from_private, verify_signature

PAUSE_PUBKEY = ""          # The project key holder's pause public key (hex). Empty = feature disabled.
MAX_REASON = 280
PAUSE_EXPIRY_BLOCKS = 10080  # ~7 days at 60 s (set 2026-10-23); a pause lapses this many blocks after its last pause/renew


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


def valid_pause_tx(tx: dict, expected_nonce: int, chain_id: str, pubkey: Optional[str] = None) -> bool:
    pubkey = PAUSE_PUBKEY if pubkey is None else pubkey
    try:
        tx = normalize_pause_tx(tx)
    except (ValueError, TypeError):
        return False
    if not pubkey or tx["pubkey"] != pubkey.lower() or tx["nonce"] != expected_nonce:
        return False
    msg = pause_message(pause_payload(tx["paused"], tx["reason"], tx["nonce"], chain_id))
    return verify_signature(tx["pubkey"], tx["sig"], msg)


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
