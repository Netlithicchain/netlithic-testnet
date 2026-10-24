"""PROPOSAL pending founder approval -- NOT ACTIVE. RandomX proof-of-work prototype.

Uses the C library tevador/RandomX through the `RandomX` PyPI binding (or the
`openssl-encrypt-randomx` binding, module randomx_native). There is NO pure-Python
fallback: if no binding is installed, available() is False and nothing changes.
RANDOMX_FORK_HEIGHT is None, so consensus never calls this. See docs/RANDOMX_PROPOSAL.md.
"""
import hashlib

RANDOMX_FORK_HEIGHT = None      # proposal: a height chosen by the founder; None = off
SEED_EPOCH = 2048               # Monero-style key rotation
SEED_LAG = 64

try:  # C bindings only
    import randomx as _rx
    def _make(key, full):
        return _rx.RandomX(key, full_mem=full, secure=True, large_pages=False)
    def _hash(vm, data):
        return vm(data)
    BINDING = "RandomX"
except ImportError:  # pragma: no cover
    try:
        import randomx_native as _rx
        def _make(key, full):
            return _rx.RandomX(key, full_mem=full)
        def _hash(vm, data):
            return vm.calculate_hash(data)
        BINDING = "openssl-encrypt-randomx"
    except ImportError:
        _rx = None
        BINDING = None

_vms = {}


def available() -> bool:
    return _rx is not None


def seed_height(height: int) -> int:
    """Block whose hash keys the RandomX VM for `height`."""
    h = max(0, int(height) - SEED_LAG)
    return (h // SEED_EPOCH) * SEED_EPOCH


def pow_hash(header: bytes, seed: bytes, full: bool = False) -> str:
    if _rx is None:
        raise RuntimeError("RandomX C binding not installed; refusing pure-Python fallback")
    key = (hashlib.sha256(seed).digest(), full)
    vm = _vms.get(key)
    if vm is None:
        _vms.clear()                    # keep at most one VM (256 MB light / ~2.3 GB full)
        vm = _vms[key] = _make(key[0], full)
    return _hash(vm, header).hex()


def meets_work(pow_hex: str, work: int) -> bool:
    """Same target rule as post-LWMA SHA-256: int(hash) * work < 2**256."""
    return int(pow_hex, 16) * int(work) < 2 ** 256
