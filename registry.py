"""Registered contributors for the transfer pot (TEMPORARY and CENTRALIZED).

Test coins have no value. This module decides ONE thing: which senders may earn *transfer* points. Mining points
are open to everybody and are never touched by anything in here.

How it works (all of it is consensus: every node derives the same answer from the chain):

  * The list lives ON THE CHAIN as signed "registry" transactions, exactly like the pause switch:

        {"type": "registry", "adds": [addr, ...], "removes": [addr, ...], "reason": "...", "nonce": n,
         "pubkey": <REGISTRY_PUBKEY>, "sig": <Ed25519 over registry_message(...)>}

  * Only REGISTRY_PUBKEY may sign. Nonces go 1, 2, 3 (no replay). The chain id is signed (no cross-chain replay).
    The message is domain-separated from the pause message, so a pause signature can never be used here.
  * An ADDITION made in block h counts for transfers mined at heights >= h + REGISTRY_DELAY_BLOCKS (1,440 = ~1 day),
    so everybody can see "pending" additions for a day before they can earn anything.
  * A REMOVAL made in block h stops the address from earning for transfers mined at heights >= h + REGISTRY_REMOVE_DELAY_BLOCKS
    (1,440). It is PROSPECTIVE ONLY: a transfer that was mined while the sender was listed keeps its points whatever
    happens later, so a removal (or a stolen key) can never erase coins somebody already earned and that are waiting for
    their payout. Removing an address that is still waiting for its one-day delay cancels the addition. Whether a
    transfer earns depends only on registry updates mined at or before the transfer's own height (plus the delays), so the
    list that pays a period is fixed by the period's own blocks. A re-added address starts again from its new addition.
  * An address earns transfer points for a transfer only if the SENDER is registered at the block that carries it.
    (Points go to the sender only, so the receiver is not checked; see the docs for why.)
  * Limits that protect against a bad or stolen key, counted over ANY rolling window of REGISTRY_LIMIT_WINDOW blocks (not a
    calendar period, so there is no double allowance across a period boundary): at most MAX_ADDS_PER_WINDOW additions and at
    most MAX_REMOVES_PER_WINDOW removals; at most MAX_ENTRIES_PER_TX addresses per transaction; at most MAX_ACTIVE_ENTRIES
    registered at any time.
  * KEY ROTATION without a fork: the key in REGISTRY_PUBKEY is only the FIRST key. The current key may sign a rotation
    ("new_key"); the new key takes over REGISTRY_ROTATION_DELAY_BLOCKS (1,440) blocks later. A pending rotation can be
    replaced by another one signed by the still-current key. The chain, not a code constant, says which key is current.
    A rotation also needs the NEW key's own signature ("new_sig") over the same update, so a key nobody holds (a typo,
    a copy mistake) can never be installed, and the new key must be a real Ed25519 public key (on the curve, canonical,
    not of small order; all-zero and the identity point are refused).
    A rotation cannot protect against a thief who holds the current key as well (there is no second authority on chain):
    for that, the pause switch stops payouts and the break-glass plan is a coordinated fork.
  * Every update carries "valid_until" (a block height). A block above it cannot carry the update, so a miner cannot sit
    on a signed update and mine it much later. valid_until may be at most REGISTRY_MAX_VALID_BLOCKS (10,080) above the
    height of the block that carries the update, so a huge value cannot switch the expiry off.
  * FAIL SAFE: with the registry on and no key set (or an empty list) nobody earns transfer points: the transfer pot
    stays unspent in the pool. It never turns into an open pot by accident.
  * Switches: REGISTRY_ENABLED = False gives exactly points v4 (open transfer pot). REGISTRY_SUNSET_HEIGHT (None =
    never) opens the pot again at an announced height.

Everything here is a pure function of the blocks it is given: no clock, no files, no network.
"""
import re
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from wallet import canonical, is_valid_pubkey_hex, pubkey_from_private, verify_signature

# ---------------------------------------------------------------------------------------------
# REGISTRY KEY SLOT. The ONLY place the registry PUBLIC key goes (64 lowercase hex characters).
#   * EMPTY on purpose until the founder hands over a PUBLIC key from an offline ceremony (never a private key).
#     Empty + REGISTRY_ENABLED = nobody earns transfer points (fail safe), and no registry tx is valid.
#   * Fill it with the tool, never by hand:  python ops/registry_tool.py set-key <public key hex>
#   * Release gate:                          python ops/registry_tool.py --check   (exit 1 while empty)
#   * Use a DIFFERENT key from the pause key. Every node must carry the SAME value (it is in the rules fingerprint).
#   * This is only the FIRST key. Later keys are chosen on chain with a signed rotation (no fork). Changing THIS constant
#     afterwards invalidates every registry transaction on the chain and is a fork: set it once.
# ---------------------------------------------------------------------------------------------
REGISTRY_PUBKEY = ""  # REGISTRY-PUBKEY-SLOT: empty = no valid registry tx; set only via ops/registry_tool.py set-key

REGISTRY_ENABLED = True            # False = exactly points v4 (open transfer pot); consensus, in the rules fingerprint
REGISTRY_DELAY_BLOCKS = 1440       # an addition counts for transfers mined this many blocks after the block that adds it
REGISTRY_REMOVE_DELAY_BLOCKS = 1440  # a removal stops earning for transfers mined this many blocks after the removal; never retroactive
REGISTRY_LIMIT_WINDOW = 1440       # rolling window (blocks) for the add and remove limits
MAX_ADDS_PER_WINDOW = 50           # additions in any 1,440 consecutive blocks: caps what a bad or stolen key can add per day
MAX_REMOVES_PER_WINDOW = 50        # removals in any 1,440 consecutive blocks: caps what a stolen key can switch off per day
MAX_ADDS_PER_PERIOD = MAX_ADDS_PER_WINDOW   # old name, kept for tools and tests
REGISTRY_MAX_VALID_BLOCKS = 10_080          # an update may not claim to stay valid longer than this many blocks past the block that carries it (~7 days)
REGISTRY_ROTATION_DELAY_BLOCKS = 1440       # a rotation to a new key takes over this many blocks after the block that carries it
MAX_ENTRIES_PER_TX = 50            # adds + removes in one transaction
MAX_ACTIVE_ENTRIES = 500           # registered (including pending) addresses at any time
MAX_REASON = 280
REGISTRY_SUNSET_HEIGHT: Optional[int] = None   # None = never; else from this height the transfer pot is open again (v4)

_ADDR_RE = re.compile(r"[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_SIG = re.compile(r"[0-9a-f]{128}")
NO_EXPIRY = 10 ** 12   # valid_until used by the test helper when none is given


def key_id(pubkey: Optional[str] = None) -> str:
    """Short public fingerprint of the registry key (first 16 hex of SHA-256); "" while the slot is empty."""
    import hashlib
    pubkey = REGISTRY_PUBKEY if pubkey is None else pubkey
    if not pubkey:
        return ""
    try:
        return hashlib.sha256(bytes.fromhex(pubkey.lower())).hexdigest()[:16]
    except (ValueError, TypeError, AttributeError):
        return "invalid"      # a malformed slot must never break /status or /registry


def is_registry_tx(tx) -> bool:
    return isinstance(tx, dict) and tx.get("type") == "registry"


def registry_payload(adds: List[str], removes: List[str], reason: str, nonce: int, chain_id: str,
                     valid_until: Optional[int] = None, new_key: Optional[str] = None) -> dict:
    p = {"adds": sorted(adds), "removes": sorted(removes), "reason": str(reason), "nonce": int(nonce),
         "chain_id": chain_id}
    if valid_until is not None:
        p["valid_until"] = int(valid_until)
    if new_key is not None:
        p["new_key"] = str(new_key).lower()
    return p


def registry_message(payload: dict) -> bytes:
    """What the key signs. A different top-level name from the pause message keeps the two signatures apart."""
    return canonical({"registry_update": payload}).encode("utf-8")


def registry_accept_message(payload: dict) -> bytes:
    """What the NEW key signs in a rotation ("I accept this handover"). Same payload, its own top-level name, so this
    signature can never be mistaken for an update signature (or the other way round)."""
    return canonical({"registry_accept": payload}).encode("utf-8")


def _addr_list(value, what: str) -> List[str]:
    if not isinstance(value, list):
        raise ValueError(f"{what} must be a list")
    out = []
    for a in value:
        if not isinstance(a, str) or not _ADDR_RE.fullmatch(a):
            raise ValueError(f"{what} has a bad address (need 32 lowercase hex characters)")
        out.append(a)
    if len(set(out)) != len(out):
        raise ValueError(f"{what} has a duplicate address")
    return out


def normalize_registry_tx(tx: dict) -> dict:
    """Strict shape check. Lists are sorted so there is exactly one canonical form (and one signed message).
    `valid_until` is required. `new_key` (a key rotation) makes the update stand alone: no adds or removes with it."""
    if not is_registry_tx(tx):
        raise ValueError("Not a registry tx")
    adds, removes = _addr_list(tx.get("adds", []), "adds"), _addr_list(tx.get("removes", []), "removes")
    rotating = "new_key" in tx
    if not adds and not removes and not rotating:
        raise ValueError("A registry update must add, remove or rotate")
    if len(adds) + len(removes) > MAX_ENTRIES_PER_TX:
        raise ValueError(f"At most {MAX_ENTRIES_PER_TX} addresses per registry update")
    if set(adds) & set(removes):
        raise ValueError("An address cannot be added and removed in the same update")
    reason = tx.get("reason")
    if not isinstance(reason, str) or not 1 <= len(reason) <= MAX_REASON:
        raise ValueError("Registry reason must be 1-280 chars")
    nonce = tx.get("nonce", 0)
    valid_until = tx.get("valid_until")
    if isinstance(nonce, bool) or isinstance(valid_until, bool) or not isinstance(valid_until, int):
        raise ValueError("A registry update needs a whole-number valid_until height")
    nonce = int(nonce)
    if nonce <= 0:
        raise ValueError("Nonce must be > 0")
    if valid_until < 0:
        raise ValueError("valid_until must be a block height of 0 or more")
    out = {"type": "registry", "adds": sorted(adds), "removes": sorted(removes), "reason": reason,
           "nonce": nonce, "valid_until": valid_until,
           "pubkey": str(tx.get("pubkey", "")).lower(), "sig": str(tx.get("sig", "")).lower()}
    if rotating:
        nk = tx.get("new_key")
        if adds or removes:
            raise ValueError("A key rotation must stand alone (no adds or removes)")
        if not isinstance(nk, str) or not _HEX64.fullmatch(nk):
            raise ValueError("new_key must be 64 lowercase hex characters")
        if not is_valid_pubkey_hex(nk):
            raise ValueError("new_key is not a valid Ed25519 public key (off the curve, not canonical, or a weak point)")
        ns = tx.get("new_sig")
        if not isinstance(ns, str) or not _SIG.fullmatch(ns):
            raise ValueError("A rotation needs new_sig: 128 lowercase hex characters signed by the new key")
        out["new_key"] = nk
        out["new_sig"] = ns
    elif "new_sig" in tx:
        raise ValueError("new_sig only belongs to a key rotation")
    return out


def sign_registry_tx(private_key_hex: str, adds: List[str], removes: List[str], reason: str, nonce: int,
                     chain_id: str, valid_until: Optional[int] = None, new_key: Optional[str] = None,
                     new_key_private: Optional[str] = None) -> dict:
    """Sign an update. Run ONLY on the key holder's own offline machine (or with a throwaway key in tests), never on a server.
    valid_until=None signs the placeholder NO_EXPIRY (10**12). Consensus ALWAYS refuses that at any real height (the
    cap is the carrying block's height + REGISTRY_MAX_VALID_BLOCKS; 10**12 is unreachably far), so it
    exists only for tests (which lift the cap) and for tests of the refusal; real updates must pass a height.
    For a rotation, `new_key_private` (TESTS ONLY, a throwaway key) makes the new key's co-signature; real rotations
    get that signature from the new key's holder on their own machine (ops/registry_sign_offline.py --as-new-key)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_key_hex))
    valid_until = NO_EXPIRY if valid_until is None else int(valid_until)
    payload = registry_payload(adds, removes, reason, nonce, chain_id, valid_until, new_key)
    tx = {"type": "registry", "adds": payload["adds"], "removes": payload["removes"], "reason": reason,
          "nonce": int(nonce), "valid_until": valid_until, "pubkey": pubkey_from_private(private_key_hex),
          "sig": sk.sign(registry_message(payload)).hex()}
    if new_key is not None:
        tx["new_key"] = str(new_key).lower()
        if new_key_private is not None:
            nsk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(new_key_private))
            tx["new_sig"] = nsk.sign(registry_accept_message(payload)).hex()
    return tx


def _tx_payload(tx: dict, chain_id: str) -> dict:
    return registry_payload(tx["adds"], tx["removes"], tx["reason"], tx["nonce"], chain_id,
                            tx["valid_until"], tx.get("new_key"))


def _tx_message(tx: dict, chain_id: str) -> bytes:
    return registry_message(registry_payload(tx["adds"], tx["removes"], tx["reason"], tx["nonce"], chain_id,
                                             tx["valid_until"], tx.get("new_key")))


def signature_ok(tx: dict, chain_id: str, pubkey: Optional[str] = None) -> bool:
    """Key + signature check only (no chain state)."""
    pubkey = REGISTRY_PUBKEY if pubkey is None else pubkey
    try:
        tx = normalize_registry_tx(tx)
    except (ValueError, TypeError):
        return False
    if not pubkey or not _HEX64.fullmatch(pubkey.lower()) or tx["pubkey"] != pubkey.lower():
        return False
    if not _SIG.fullmatch(tx["sig"]):
        return False
    if not verify_signature(tx["pubkey"], tx["sig"], _tx_message(tx, chain_id)):
        return False
    if "new_key" in tx:        # the new key must have signed the handover too (proof that somebody holds it)
        if not verify_signature(tx["new_key"], tx["new_sig"], registry_accept_message(_tx_payload(tx, chain_id))):
            return False
    return True


def active_at(height: int) -> bool:
    """Is the registry rule in force for a transfer mined at `height`? (fork height is decided by core.)"""
    if not REGISTRY_ENABLED:
        return False
    return REGISTRY_SUNSET_HEIGHT is None or int(height) < REGISTRY_SUNSET_HEIGHT


class RegistryState:
    """The list as seen after some number of blocks. Built by scan(); check_and_apply() checks and applies one update."""

    def __init__(self, period_fn: Callable[[int], int], delay: Optional[int] = None,
                 initial_key: Optional[str] = None, remove_delay: Optional[int] = None):
        self.period_fn = period_fn
        self.delay = REGISTRY_DELAY_BLOCKS if delay is None else delay
        self.remove_delay = REGISTRY_REMOVE_DELAY_BLOCKS if remove_delay is None else remove_delay
        self.initial_key = (REGISTRY_PUBKEY if initial_key is None else initial_key).lower()
        self.keys: List[Tuple[int, str]] = [(0, self.initial_key)]   # (first height the key may sign, key), ascending
        self.nonce = 0
        self.events: Dict[str, List[Tuple[str, int]]] = {}     # address -> [("add"|"remove", height), ...] in order
        self.add_log: List[Tuple[int, int]] = []               # (height, how many) for the rolling add limit
        self.remove_log: List[Tuple[int, int]] = []            # same for removals
        self.log: List[dict] = []
        self.tip = -1

    def copy(self) -> "RegistryState":
        o = RegistryState(self.period_fn, self.delay, self.initial_key, self.remove_delay)
        o.nonce, o.tip = self.nonce, self.tip
        o.keys = list(self.keys)
        o.events = {a: list(e) for a, e in self.events.items()}
        o.add_log, o.remove_log = list(self.add_log), list(self.remove_log)
        o.log = list(self.log)
        return o

    # ---- keys ----
    def key_at(self, height: int) -> str:
        """The key allowed to sign an update carried by the block at `height` ("" = none: nobody can sign)."""
        k = ""
        for eff, key in self.keys:
            if eff <= height:
                k = key
        return k

    def pending_key(self, height: int) -> Optional[Tuple[int, str]]:
        """(first height, key) of a rotation that has been made but has not taken over yet at `height`."""
        later = [(eff, key) for eff, key in self.keys if eff > height]
        return later[-1] if later else None

    # ---- reading ----
    def registered_now(self) -> Dict[str, int]:
        """{address: height of the block that added it} for every address whose latest event is an addition
        (pending ones included)."""
        return {a: e[-1][1] for a, e in self.events.items() if e and e[-1][0] == "add"}

    def intervals(self, address: str) -> List[Tuple[int, Optional[int]]]:
        """[(first, last_exclusive_or_None)] heights at which a transfer by this address earns. Prospective only:
        a removal at r ends an interval at r + remove_delay and never reaches back; removing an address that was
        still waiting for its delay (r < add + delay) cancels it."""
        out: List[Tuple[int, Optional[int]]] = []
        start: Optional[int] = None
        for kind, h in self.events.get(address, []):
            if kind == "add":
                start = h + self.delay
            elif start is not None:
                end = h + self.remove_delay
                if h >= start and end > start:
                    out.append((start, end))
                start = None
        if start is not None:
            out.append((start, None))
        return out

    def eligible(self, address: str, height: int) -> bool:
        """May a transfer mined at `height` by this sender earn transfer points? Depends only on updates mined at
        heights <= height (an update at r cannot change an answer for a height below r, or below r + the delays)."""
        return any(s <= height and (e is None or height < e) for s, e in self.intervals(address))

    def pending(self, height: int) -> Dict[str, int]:
        """Registered but not yet earning at `height`: {address: first height at which transfers count}."""
        return {a: h + self.delay for a, h in self.registered_now().items() if height < h + self.delay}

    def leaving(self, height: int) -> Dict[str, int]:
        """Removed but still earning at `height`: {address: first height at which transfers stop counting}."""
        out = {}
        for a, e in self.events.items():
            if e and e[-1][0] == "remove" and self.eligible(a, height):
                out[a] = e[-1][1] + self.remove_delay
        return out

    def _in_window(self, log: List[Tuple[int, int]], height: int) -> int:
        return sum(n for h, n in log if height - h < REGISTRY_LIMIT_WINDOW)

    def adds_in_window(self, height: int) -> int:
        return self._in_window(self.add_log, height)

    def removes_in_window(self, height: int) -> int:
        return self._in_window(self.remove_log, height)

    # ---- writing ----
    def check_and_apply(self, tx: dict, height: int, chain_id: str, pubkey: Optional[str] = None) -> bool:
        """Validate one registry transaction carried by the block at `height` and, if valid, apply it.
        Returns False and changes nothing if anything is wrong. `pubkey` (tests) overrides the scheduled key."""
        try:
            n = normalize_registry_tx(tx)
        except (ValueError, TypeError):
            return False
        if n != tx:
            return False  # exactly the canonical form: no extra fields, sorted lists, lowercase hex
        key = self.key_at(height) if pubkey is None else pubkey
        if not REGISTRY_ENABLED or n["nonce"] != self.nonce + 1 or not signature_ok(n, chain_id, key):
            return False
        if height > n["valid_until"]:
            return False                      # the update expired: it cannot be mined late
        if n["valid_until"] > height + REGISTRY_MAX_VALID_BLOCKS:
            return False                      # the expiry must be real: at most ~7 days past the carrying block
        rotating = "new_key" in n
        if rotating:
            import pause
            if n["new_key"] == self.key_at(height) or n["new_key"] == (pause.PAUSE_PUBKEY or "").lower():
                return False                  # same key, or the pause key (one key, one job)
        now = self.registered_now()
        if any(a in now for a in n["adds"]) or any(a not in now for a in n["removes"]):
            return False
        if self.adds_in_window(height) + len(n["adds"]) > MAX_ADDS_PER_WINDOW:
            return False
        if self.removes_in_window(height) + len(n["removes"]) > MAX_REMOVES_PER_WINDOW:
            return False
        if len(now) + len(n["adds"]) - len(n["removes"]) > MAX_ACTIVE_ENTRIES:
            return False
        for a in n["removes"]:
            self.events[a].append(("remove", height))
        for a in n["adds"]:
            self.events.setdefault(a, []).append(("add", height))
        if n["adds"]:
            self.add_log.append((height, len(n["adds"])))
        if n["removes"]:
            self.remove_log.append((height, len(n["removes"])))
        if rotating:                           # a newer rotation replaces one that has not taken over yet
            self.keys = [(eff, k) for eff, k in self.keys if eff <= height]
            self.keys.append((height + REGISTRY_ROTATION_DELAY_BLOCKS, n["new_key"]))
        self.nonce = n["nonce"]
        entry = {"height": height, "adds": n["adds"], "removes": n["removes"], "reason": n["reason"],
                 "nonce": n["nonce"]}
        if rotating:
            entry["new_key_id"] = key_id(n["new_key"])
            entry["new_key_from_height"] = height + REGISTRY_ROTATION_DELAY_BLOCKS
        self.log.append(entry)
        return True


def scan(blocks: Iterable, period_fn: Callable[[int], int], upto: Optional[int] = None, fork_height: int = 0,
         chain_id: str = "") -> RegistryState:
    """Replay every registry transaction in `blocks` (Block objects or dicts) up to and including height `upto`.
    The chain is already validated when this is used, so a transaction that fails the checks is simply skipped
    (it cannot be on a valid chain); it can never change the state."""
    st = RegistryState(period_fn)
    for b in blocks:
        if isinstance(b, dict):
            h, txs = int(b["index"]), b.get("transactions") or []
        else:
            h, txs = int(b.index), b.transactions or []
        st.tip = h
        if upto is not None and h > upto:
            break
        if h < fork_height:
            continue
        for tx in txs:
            if is_registry_tx(tx):
                st.check_and_apply(tx, h, chain_id)
    return st


def resolver(state: RegistryState, fork_height: int) -> Callable[[str, int], bool]:
    """(sender, height) -> may this transfer earn transfer points? Open (True) before the fork height and from the
    sunset height; otherwise only registered senders. An empty list fails closed: nobody earns."""
    def ok(address: str, height: int) -> bool:
        if height < fork_height or not active_at(height):
            return True
        return state.eligible(address, height)
    return ok
