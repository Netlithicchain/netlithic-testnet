import hashlib
import json
import os
import re
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)


WALLET_FILE = "wallets.json"
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
ADDRESS_RE = re.compile(r"^[0-9a-f]{32}$")
SIG_RE = re.compile(r"^[0-9a-f]{128}$")  # Ed25519 signature = 64 bytes

GENESIS_CONFIG_ENV = "NETLITHIC_GENESIS_CONFIG"
DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "genesis_config.json")


def _config_path() -> str:
    return os.environ.get(GENESIS_CONFIG_ENV, DEFAULT_CONFIG_PATH)


def _read_config() -> dict:
    with open(_config_path(), "r", encoding="utf-8") as f:
        return json.load(f)


def current_chain_id() -> str:
    """The chain id from config/genesis_config.json (the single source of truth).
    Every signed tx commits to it, so a signature made for another Netlithic
    network (or a future chain reset) can't be replayed here."""
    chain_id = _read_config().get("chain_id")
    if not isinstance(chain_id, str) or not chain_id.strip():
        raise ValueError("genesis config has no chain_id")
    return chain_id.strip()


def current_min_fee() -> int:
    """Minimum tx fee in base units, from the genesis config (proposal)."""
    return int(_read_config().get("fees", {}).get("min_fee_base_units", 0))


# Read once at import for convenience; code that must follow the config
# (signing, verification) calls current_chain_id() / uses Economics.chain_id.
CHAIN_ID = current_chain_id()


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def pubkey_to_address(pubkey_hex: str) -> str:
    if not isinstance(pubkey_hex, str):
        raise ValueError("Pubkey must be a string")

    pubkey_hex = pubkey_hex.strip().lower()
    if not HEX64_RE.fullmatch(pubkey_hex):
        raise ValueError("Invalid pubkey")

    return sha256_text(pubkey_hex)[:32]


def pubkey_from_private(private_key_hex: str) -> str:
    sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_key_hex))
    return sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


def signing_message(payload: dict, pubkey: str) -> bytes:
    """Exact bytes that get signed: sorted-key JSON of the tx minus 'sig'."""
    return canonical({"payload": payload, "pubkey": pubkey}).encode("utf-8")


def normalize_address(address: str) -> str:
    if not isinstance(address, str):
        raise ValueError("Address must be a string")

    address = address.strip().lower()
    if not ADDRESS_RE.fullmatch(address):
        raise ValueError("Invalid address")

    return address


def verify_signed_tx(signed_tx: dict, chain_id: str = None) -> bool:
    try:
        if not isinstance(signed_tx, dict):
            return False

        payload = signed_tx.get("payload")
        pubkey = signed_tx.get("pubkey")
        sig = signed_tx.get("sig")

        if not isinstance(payload, dict):
            return False

        sender = normalize_address(payload.get("sender"))
        recipient = normalize_address(payload.get("recipient"))
        amount = int(payload.get("amount", 0))
        nonce = int(payload.get("nonce", 0))
        timestamp = float(payload.get("timestamp", 0))
        fee = int(payload.get("fee", -1))
        chain_id = chain_id or current_chain_id()

        if payload.get("chain_id") != chain_id:
            return False
        if fee < 0:
            return False
        if sender == recipient:
            return False
        if amount <= 0 or nonce <= 0 or timestamp <= 0:
            return False

        if not isinstance(pubkey, str) or not HEX64_RE.fullmatch(pubkey.strip().lower()):
            return False

        if not isinstance(sig, str) or not SIG_RE.fullmatch(sig.strip().lower()):
            return False

        pubkey = pubkey.strip().lower()
        sig = sig.strip().lower()

        if pubkey_to_address(pubkey) != sender:
            return False

        normalized_payload = {
            "sender": sender,
            "recipient": recipient,
            "amount": amount,
            "nonce": nonce,
            "timestamp": timestamp,
            "fee": fee,
            "chain_id": chain_id,
        }

        Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey)).verify(
            bytes.fromhex(sig), signing_message(normalized_payload, pubkey)
        )
        return True

    except InvalidSignature:
        return False
    except Exception:
        return False


def multisig_message(payload: dict) -> bytes:
    """Exact bytes each keyholder signs for a treasury/reserve multisig spend."""
    return canonical({"multisig_spend": payload}).encode("utf-8")


def verify_signature(pubkey_hex: str, sig_hex: str, message: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey_hex)).verify(
            bytes.fromhex(sig_hex), message)
        return True
    except (InvalidSignature, ValueError):
        return False


# --- Ed25519 public key sanity check (shared by consensus in registry.py and by the ops tools) ---
# The cryptography library accepts ANY 32 bytes as a public key. Some encodings are not real keys at all
# (not on the curve, not canonical) and some are "small order" points (the identity point, for one) for which
# a signature can be forged by anybody. Everything that installs a key must refuse those.
# It also refuses points that carry a torsion part (L*P != identity): an honestly generated key A = a*B always has
# L*A = identity, so such a point cannot be anybody's real key (the 0.5.3 candidate shipped one by a typo).
# This function is used ONLY for the pause/registry key slots, the registry rotation key and their ops tools; it is not
# used on wallet addresses, miner addresses or transaction senders, so ordinary accounts are not affected.
_ED_P = 2 ** 255 - 19
_ED_L = 2 ** 252 + 27742317777372353535851937790883648493
_ED_D = -121665 * pow(121666, _ED_P - 2, _ED_P) % _ED_P


def is_good_ed25519_point(raw: bytes) -> bool:
    """True only for a canonical encoding of a point that is on the curve, is not of small order, and lies in the
    prime-order subgroup (L * P is the identity)."""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) != 32:
        return False
    P, D = _ED_P, _ED_D
    y = int.from_bytes(raw, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    if y >= P:
        return False                      # non-canonical encoding of y
    x2 = (y * y - 1) * pow(D * y * y + 1, P - 2, P) % P
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P:
        x = x * pow(2, (P - 1) // 4, P) % P
    if (x * x - x2) % P:
        return False                      # no square root: not on the curve
    if x == 0 and sign:
        return False                      # non-canonical encoding of x = 0
    if (x & 1) != sign:
        x = P - x

    def add(p1, p2):                      # twisted Edwards addition, affine
        (x1, y1), (x2_, y2) = p1, p2
        t = D * x1 * x2_ * y1 * y2 % P
        return ((x1 * y2 + x2_ * y1) * pow(1 + t, P - 2, P) % P,
                (y1 * y2 + x1 * x2_) * pow(1 - t, P - 2, P) % P)
    pt = (x, y)
    for _ in range(3):                    # 8 * P
        pt = add(pt, pt)
    if pt == (0, 1):
        return False                      # 8P = identity means a low-order (weak) key
    q, k, base = (0, 1), _ED_L, (x, y)    # L * P by double-and-add; identity means "no torsion part"
    while k:
        if k & 1:
            q = add(q, base)
        base = add(base, base)
        k >>= 1
    return q == (0, 1)


def is_valid_pubkey_hex(key: str) -> bool:
    """64 lowercase hex characters that are a good Ed25519 public key (see is_good_ed25519_point)."""
    if not isinstance(key, str) or len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
        return False
    return is_good_ed25519_point(bytes.fromhex(key))


class Wallet:
    def __init__(self, name: str, private_key: str, pubkey: str):
        self.name = str(name)
        self.private_key = str(private_key).strip().lower()
        self.pubkey = str(pubkey).strip().lower()
        if pubkey_from_private(self.private_key) != self.pubkey:
            raise ValueError("Private key does not match pubkey (old-style wallet?)")
        self.address = pubkey_to_address(self.pubkey)

    @staticmethod
    def _load_wallets():
        if not os.path.exists(WALLET_FILE):
            return {}

        with open(WALLET_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return {}

        normalized = {}
        for address, wallet_data in data.items():
            try:
                clean_address = normalize_address(address)
                if not isinstance(wallet_data, dict):
                    continue

                pubkey = str(wallet_data["pubkey"]).strip().lower()
                private_key = str(wallet_data["private_key"]).strip().lower()
                name = str(wallet_data["name"])

                if pubkey_to_address(pubkey) != clean_address:
                    continue
                # Old pre-Ed25519 wallets (pubkey = sha256(private)) are skipped:
                # they can no longer sign. See MIGRATION.md.
                if pubkey_from_private(private_key) != pubkey:
                    continue

                normalized[clean_address] = {
                    "name": name,
                    "address": clean_address,
                    "pubkey": pubkey,
                    "private_key": private_key,
                    "created_at": float(wallet_data.get("created_at", time.time())),
                }
            except Exception:
                continue

        return normalized

    @staticmethod
    def _save_wallets(wallets):
        # Owner-only (0600): this file holds private keys.
        fd = os.open(WALLET_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(fd, 0o600)
        except (AttributeError, OSError):
            pass  # e.g. Windows; rely on folder permissions / encryption there
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(wallets, f, indent=2)

    @classmethod
    def create(cls, name: str):
        wallets = cls._load_wallets()

        sk = Ed25519PrivateKey.generate()
        private_key = sk.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()).hex()
        pubkey = pubkey_from_private(private_key)

        wallet = cls(name=name, private_key=private_key, pubkey=pubkey)

        wallets[wallet.address] = {
            "name": wallet.name,
            "address": wallet.address,
            "pubkey": wallet.pubkey,
            "private_key": wallet.private_key,
            "created_at": time.time(),
        }

        cls._save_wallets(wallets)
        return wallet

    @classmethod
    def load(cls, address: str):
        address = normalize_address(address)
        wallets = cls._load_wallets()
        data = wallets.get(address)

        if not data:
            raise ValueError(f"Wallet not found: {address}")

        return cls(
            name=data["name"],
            private_key=data["private_key"],
            pubkey=data["pubkey"],
        )

    def sign_multisig(self, payload: dict) -> dict:
        """This wallet's co-signature for a multisig spend payload."""
        sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(self.private_key))
        return {"pubkey": self.pubkey, "sig": sk.sign(multisig_message(payload)).hex()}

    def create_transaction(self, to_address: str, amount: int, nonce: int,
                           fee: int = None, chain_id: str = None) -> dict:
        to_address = normalize_address(to_address)
        amount = int(amount)
        nonce = int(nonce)
        fee = current_min_fee() if fee is None else int(fee)
        if fee < 0:
            raise ValueError("Fee must be >= 0")

        if amount <= 0:
            raise ValueError("Amount must be > 0")
        if nonce <= 0:
            raise ValueError("Nonce must be > 0")
        if to_address == self.address:
            raise ValueError("Cannot send to self")

        payload = {
            "sender": self.address,
            "recipient": to_address,
            "amount": amount,
            "nonce": nonce,
            "timestamp": time.time(),
            "fee": fee,
            "chain_id": chain_id or current_chain_id(),
        }

        sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(self.private_key))
        sig = sk.sign(signing_message(payload, self.pubkey)).hex()

        return {
            "payload": payload,
            "pubkey": self.pubkey,
            "sig": sig,
        }



if __name__ == "__main__":
    # Creates a wallet in wallets.json (0600) and prints ONLY the address.
    # Private keys are never printed.
    import sys
    w = Wallet.create(sys.argv[1] if len(sys.argv) > 1 else "New Wallet")
    print(f"Address: {w.address}")
    print(f"Private key saved in {os.path.abspath(WALLET_FILE)} (owner-only). Keep that file private.")
