"""Reviewer fixes for the 0.5.3 candidate: P1 (prime-order subgroup check for key slots), P2 (pause key in the rules
fingerprint), P3 (cheap checks before the full-chain pause scan). Only throwaway keys and public keys are used."""
import inspect
import os
import subprocess
import sys

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import core
import pause
import registry
import wallet
from wallet import is_good_ed25519_point, is_valid_pubkey_hex

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ops"))
import set_pause_pubkey as spk  # noqa: E402
import registry_tool  # noqa: E402

OLD_TYPO_KEY = "054a1d9948c3325059835edb1d4fc3f74f40c32f12bf2417b2ac180eb2737029"   # public; one wrong character
NEW_KEY = "054a1d9948c3325059835edb1d4fc3f74f40ce2f12bf2417b2ac180eb2737029"        # public; the real one
NEW_KEY_ID = "2a83f37a4ce7a269"

P = 2 ** 255 - 19
L = 2 ** 252 + 27742317777372353535851937790883648493
D = -121665 * pow(121666, P - 2, P) % P


# ---- independent point arithmetic (not the code under test) ----
def _decode(raw):
    y = int.from_bytes(raw, "little"); s = y >> 255; y &= (1 << 255) - 1
    x2 = (y * y - 1) * pow(D * y * y + 1, P - 2, P) % P
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P:
        x = x * pow(2, (P - 1) // 4, P) % P
    assert (x * x - x2) % P == 0
    return (P - x if (x & 1) != s else x, y)


def _encode(pt):
    x, y = pt
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _add(a, b):
    (x1, y1), (x2, y2) = a, b
    t = D * x1 * x2 * y1 * y2 % P
    return ((x1 * y2 + x2 * y1) * pow(1 + t, P - 2, P) % P, (y1 * y2 + x1 * x2) * pow(1 - t, P - 2, P) % P)


def _mul(k, pt):
    r = (0, 1)
    while k:
        if k & 1:
            r = _add(r, pt)
        pt = _add(pt, pt); k >>= 1
    return r


def _throwaway_pub():
    return Ed25519PrivateKey.generate().public_key().public_bytes_raw()


ORDER_2 = (0, P - 1)


# ============================== P1 ==============================
def test_p1_new_key_accepted_old_typo_key_rejected():
    assert is_valid_pubkey_hex(NEW_KEY)
    assert not is_valid_pubkey_hex(OLD_TYPO_KEY)
    # the independent check agrees on both
    assert _mul(L, _decode(bytes.fromhex(NEW_KEY))) == (0, 1)
    assert _mul(L, _decode(bytes.fromhex(OLD_TYPO_KEY))) != (0, 1)


def test_p1_the_key_in_pause_py_is_a_real_subgroup_key():
    assert pause.PAUSE_PUBKEY == NEW_KEY and pause.key_id() == NEW_KEY_ID
    assert is_valid_pubkey_hex(pause.PAUSE_PUBKEY)


def test_p1_honest_keys_still_pass_and_key_plus_torsion_fails():
    for _ in range(8):
        raw = _throwaway_pub()
        assert is_good_ed25519_point(raw)
        tainted = _encode(_add(_decode(raw), ORDER_2))           # A + T: on the curve, not small order, has a torsion part
        assert not is_good_ed25519_point(tainted)
        assert _mul(L, _decode(tainted)) != (0, 1)


def test_p1_set_pause_pubkey_tool_refuses_the_typo_key_and_takes_the_new_one(tmp_path):
    with pytest.raises(ValueError):
        spk.validate(OLD_TYPO_KEY)
    assert spk.validate(NEW_KEY) == NEW_KEY
    f = tmp_path / "pause.py"
    f.write_text('PAUSE_PUBKEY = ""  # PAUSE-PUBKEY-SLOT: test\n')

    def run(k):
        return subprocess.run([sys.executable, os.path.join(ROOT, "ops", "set_pause_pubkey.py"), k, "--file", str(f)],
                              capture_output=True, text=True)
    r = run(OLD_TYPO_KEY)
    assert r.returncode == 2 and "Refused" in r.stdout and f.read_text().startswith('PAUSE_PUBKEY = ""')
    r = run(NEW_KEY)
    assert r.returncode == 0 and NEW_KEY in f.read_text()


def test_p1_registry_key_checks_use_the_same_strict_check():
    assert registry.is_valid_pubkey_hex is wallet.is_valid_pubkey_hex
    assert registry_tool._validate is spk.validate
    with pytest.raises(ValueError):
        registry_tool._validate(OLD_TYPO_KEY)
    with pytest.raises(ValueError):                              # building a rotation to the typo key is refused
        registry_tool.build_rotation(OLD_TYPO_KEY, 1, "netlithic-dev-1", "r", 100)


def test_p1_ordinary_account_paths_do_not_use_the_strict_check():
    """Addresses and transaction senders never call the strict check, so no live account is affected."""
    for fn in (wallet.verify_signed_tx, wallet.verify_signature, wallet.pubkey_to_address):
        src = inspect.getsource(fn)
        assert "is_good_ed25519_point" not in src and "is_valid_pubkey_hex" not in src
    assert "is_good_ed25519_point" not in open(os.path.join(ROOT, "core.py")).read()
    assert "is_valid_pubkey_hex" not in open(os.path.join(ROOT, "core.py")).read()


# ============================== P2 ==============================
def test_p2_pause_key_and_limits_are_in_the_rules_fingerprint(monkeypatch):
    base = core.points_v3_rules_hash()
    monkeypatch.setattr(pause, "PAUSE_PUBKEY", OLD_TYPO_KEY)
    assert core.points_v3_rules_hash() != base                   # a build with another pause key cannot pass /hello
    monkeypatch.setattr(pause, "PAUSE_PUBKEY", "")
    assert core.points_v3_rules_hash() != base
    monkeypatch.setattr(pause, "PAUSE_PUBKEY", NEW_KEY)
    assert core.points_v3_rules_hash() == base
    monkeypatch.setattr(pause, "PAUSE_EXPIRY_BLOCKS", pause.PAUSE_EXPIRY_BLOCKS + 1)
    assert core.points_v3_rules_hash() != base
    monkeypatch.setattr(pause, "PAUSE_EXPIRY_BLOCKS", pause.PAUSE_EXPIRY_BLOCKS - 1)
    assert core.points_v3_rules_hash() == base
    monkeypatch.setattr(pause, "MAX_REASON", pause.MAX_REASON + 1)
    assert core.points_v3_rules_hash() != base


# ============================== P3 ==============================
def _key():
    return Ed25519PrivateKey.generate().private_bytes_raw().hex()


@pytest.fixture
def scan_counter(monkeypatch):
    calls = {"n": 0}
    real = pause._scan

    def counting(blocks, expiry):
        calls["n"] += 1
        return real(blocks, expiry)
    monkeypatch.setattr(pause, "_scan", counting)
    return calls


def test_p3_junk_pause_tx_never_scans_the_chain(chain, monkeypatch, scan_counter):
    k = _key()
    monkeypatch.setattr(core, "SECURITY_FORK_HEIGHT", 0)
    monkeypatch.setattr(pause, "PAUSE_PUBKEY", pause.make_pause_tx(k, True, "x", 1, "c")["pubkey"])
    cid = chain.econ.chain_id
    good = pause.make_pause_tx(k, True, "real", 1, cid)
    junk = [
        pause.make_pause_tx(_key(), True, "wrong key", 1, cid),                      # not the pause key
        dict(good, reason="edited after signing"),                                   # bad signature
        dict(good, sig="00" * 64),                                                   # junk signature
        pause.make_pause_tx(k, True, "other chain", 1, "netlithic-other"),           # signed for another chain
        {"type": "pause"},                                                           # malformed
    ]
    for tx in junk:
        assert not chain.validate_candidate_tx_against_chain_and_mempool(tx, [])
    assert scan_counter["n"] == 0
    # a correctly signed tx does reach the nonce check, once
    assert chain.validate_candidate_tx_against_chain_and_mempool(good, [])
    assert scan_counter["n"] == 1
    # and the answer is cached for the same tip: repeats do not scan again
    for _ in range(5):
        assert chain.validate_candidate_tx_against_chain_and_mempool(good, [])
    assert not chain.validate_candidate_tx_against_chain_and_mempool(pause.make_pause_tx(k, True, "n2", 2, cid), [])
    assert scan_counter["n"] == 1


def test_p3_cache_follows_the_tip_and_nonces_still_work(chain, monkeypatch, scan_counter):
    k = _key()
    monkeypatch.setattr(core, "SECURITY_FORK_HEIGHT", 0)
    monkeypatch.setattr(pause, "PAUSE_PUBKEY", pause.make_pause_tx(k, True, "x", 1, "c")["pubkey"])
    cid = chain.econ.chain_id
    first = pause.make_pause_tx(k, True, "first", 1, cid)
    second = pause.make_pause_tx(k, False, "second", 2, cid)
    assert chain.validate_candidate_tx_against_chain_and_mempool(first, [])
    assert not chain.validate_candidate_tx_against_chain_and_mempool(second, [])
    b = chain.mine_block("b" * 32, [first])
    assert chain.add_block(b.to_dict()), chain.last_reject_reason
    # new tip: the cache must not serve the old nonce
    assert not chain.validate_candidate_tx_against_chain_and_mempool(first, [])
    assert chain.validate_candidate_tx_against_chain_and_mempool(second, [])
    assert chain.pause_nonce_at_tip() == pause.state(chain.chain)["nonce"] == 1


def test_p3_signed_by_pause_key_matches_valid_pause_tx(monkeypatch):
    k = _key()
    pub = pause.make_pause_tx(k, True, "x", 1, "c")["pubkey"]
    monkeypatch.setattr(pause, "PAUSE_PUBKEY", pub)
    tx = pause.make_pause_tx(k, True, "r", 3, "c")
    assert pause.signed_by_pause_key(tx, "c")["nonce"] == 3
    assert pause.valid_pause_tx(tx, 3, "c") and not pause.valid_pause_tx(tx, 2, "c")
    assert pause.signed_by_pause_key(tx, "other") is None
    monkeypatch.setattr(pause, "PAUSE_PUBKEY", "")
    assert pause.signed_by_pause_key(tx, "c") is None
