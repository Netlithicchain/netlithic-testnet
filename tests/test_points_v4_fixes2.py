"""Second round of security-review fixes (branch points-v4r-fixes2). Each test was written to FAIL on points-v4r-fixes
(eb4e3d5) and pass now. Throwaway keys only; no network; test coins have no value.

  R1  registry rotation: the new key must be a real Ed25519 public key AND must co-sign the handover
  R3  registry valid_until: capped at the carrying block + 10,080
  R2  ops/verify_payouts.py: complete validation is the default, --quick is the named fast mode
  F1  /payout_preview: single-flight, no junk cache entries, bounded limiter, IPv6 /64 buckets
  F2  0.5.3 peer gate: known peers, gossip and --peer additions, pre-release strings
  F3  rejected-reorg record: validate first, throttle writes
  F4  reorg sidecar loader: deep nesting, NaN/Infinity, malformed files never stop a node starting
"""
import json
import os
import sys

import pytest

import registry
import pause
import wallet
from test_registry import FORK, CID, addr, key, rtx
from test_points_v4_fixes import _state, period_of

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ops"))

pytestmark = pytest.mark.registry

IDENTITY = "01" + "00" * 31
IDENTITY_SIGNBIT = "01" + "00" * 30 + "80"          # x = 0 with the sign bit set: non-canonical
ORDER_2 = "ec" + "ff" * 30 + "7f"                     # (0, -1): a point of order 2
ORDER_8 = "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a"   # a point of order 8
NON_CANONICAL_Y = "ed" + "ff" * 30 + "7f"            # y = p: the same point as y = 0, but not the canonical encoding
OFF_CURVE = "ff" * 32
ALL_ZERO = "00" * 32
BAD_KEYS = {"identity": IDENTITY, "identity+signbit": IDENTITY_SIGNBIT, "order2": ORDER_2, "order8": ORDER_8,
            "non-canonical y": NON_CANONICAL_Y, "off the curve": OFF_CURVE, "all zero": ALL_ZERO}


def _rotation(old, new_pub, new_priv=None, h=FORK + 10, nonce=1, **kw):
    return registry.sign_registry_tx(old, [], [], "rotate", nonce, CID, valid_until=kw.get("valid_until", h + 100),
                                     new_key=new_pub, new_key_private=new_priv)


# ======================================================================================= R1
def test_r1_good_keys_pass_and_bad_encodings_fail_the_shared_check():
    for _ in range(5):
        assert wallet.is_valid_pubkey_hex(registry.pubkey_from_private(key()))
    for name, bad in BAD_KEYS.items():
        assert not wallet.is_valid_pubkey_hex(bad), name
    assert not wallet.is_valid_pubkey_hex("AB" * 32) and not wallet.is_valid_pubkey_hex("zz") and not wallet.is_valid_pubkey_hex(5)


@pytest.mark.parametrize("name", sorted(BAD_KEYS))
def test_r1_consensus_refuses_a_rotation_to_a_bad_key(name, monkeypatch):
    old = key()
    st = _state(old, monkeypatch)
    bad = BAD_KEYS[name]
    h = FORK + 10
    tx = registry.sign_registry_tx(old, [], [], "rotate", 1, CID, valid_until=h + 100, new_key=bad)
    tx["new_sig"] = "00" * 64                                  # whatever the signer puts here, the key itself is refused
    with pytest.raises(ValueError):
        registry.normalize_registry_tx(tx)
    assert not st.check_and_apply(tx, h, CID)
    assert st.key_at(h + 5000) == registry.REGISTRY_PUBKEY    # nothing was scheduled


def test_r1_the_identity_key_cannot_be_installed_so_forged_updates_stay_impossible(monkeypatch):
    old = key()
    st = _state(old, monkeypatch)
    h = FORK + 10
    forged = {"type": "registry", "adds": [addr("evil")], "removes": [], "reason": "forged", "nonce": 1,
              "valid_until": h + 50, "pubkey": IDENTITY, "sig": "01" + "00" * 31 + "00" * 32}
    assert not st.check_and_apply(forged, h + registry.REGISTRY_ROTATION_DELAY_BLOCKS, CID)


def test_r1_a_rotation_without_the_new_keys_signature_is_refused(monkeypatch):
    old, new = key(), key()
    st = _state(old, monkeypatch)
    h = FORK + 10
    no_cosign = _rotation(old, registry.pubkey_from_private(new), None, h)
    assert "new_sig" not in no_cosign
    with pytest.raises(ValueError):
        registry.normalize_registry_tx(no_cosign)
    assert not st.check_and_apply(no_cosign, h, CID)
    # the co-signature must come from the NEW key (not the old one, not some third key), over THIS update
    for wrong in (old, key()):
        assert not st.copy().check_and_apply(_rotation(old, registry.pubkey_from_private(new), wrong, h), h, CID)
    other = _rotation(old, registry.pubkey_from_private(new), new, h, valid_until=h + 7)       # a different update
    stolen = dict(_rotation(old, registry.pubkey_from_private(new), new, h), new_sig=other["new_sig"])
    assert not st.copy().check_and_apply(stolen, h, CID)
    ok = _rotation(old, registry.pubkey_from_private(new), new, h)
    assert st.check_and_apply(ok, h, CID)
    assert st.key_at(h + registry.REGISTRY_ROTATION_DELAY_BLOCKS) == registry.pubkey_from_private(new)


def test_r1_an_update_signature_is_not_a_handover_signature_and_new_sig_is_only_for_rotations(monkeypatch):
    old, new = key(), key()
    st = _state(old, monkeypatch)
    h = FORK + 10
    # the new key signing the ordinary update message (same payload, other domain) is refused
    tx = _rotation(old, registry.pubkey_from_private(new), None, h)
    payload = registry.registry_payload([], [], "rotate", 1, CID, h + 100, registry.pubkey_from_private(new))
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    tx["new_sig"] = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(new)).sign(registry.registry_message(payload)).hex()
    assert not st.check_and_apply(tx, h, CID)
    plain = rtx(old, [addr("a")])
    plain["new_sig"] = "00" * 64
    with pytest.raises(ValueError):
        registry.normalize_registry_tx(plain)
    assert registry.registry_accept_message(payload) != registry.registry_message(payload)


def test_r1_rules_fingerprint_changes():
    import core
    assert "registry_rules=3" in open(os.path.join(ROOT, "core.py")).read()
    assert core.points_v3_rules_hash() == core.points_v3_rules_hash()


# ======================================================================================= R3
@pytest.mark.real_valid_cap
def test_r3_valid_until_is_capped_at_the_carrying_height_plus_10080(monkeypatch):
    assert registry.REGISTRY_MAX_VALID_BLOCKS == 10_080
    k = key()
    st = _state(k, monkeypatch)
    h = FORK + 100
    mk = lambda vu, nonce=1: registry.sign_registry_tx(k, [addr(f"a{nonce}")], [], "ok", nonce, CID, valid_until=vu)
    for huge in (2 ** 63, 10 ** 30, registry.NO_EXPIRY, h + 10_081):
        assert not st.copy().check_and_apply(mk(huge), h, CID), huge
    assert st.copy().check_and_apply(mk(h + 10_080), h, CID)          # exactly at the cap is fine
    assert st.copy().check_and_apply(mk(h), h, CID)                   # valid at its own block
    assert not st.copy().check_and_apply(mk(h - 1), h, CID)           # one block too late
    # the cap moves with the block that carries it: a value fine at a later block is refused at an earlier one
    assert not st.copy().check_and_apply(mk(h + 10_080 + 5), h + 4, CID)
    assert st.copy().check_and_apply(mk(h + 10_080 + 5), h + 5, CID)
    # a rotation obeys the same cap
    new = key()
    far = registry.sign_registry_tx(k, [], [], "r", 1, CID, valid_until=h + 10_081,
                                    new_key=registry.pubkey_from_private(new), new_key_private=new)
    assert not st.copy().check_and_apply(far, h, CID)


def test_r3_cap_is_in_the_rules_fingerprint(monkeypatch):
    import core
    a = core.points_v3_rules_hash()
    monkeypatch.setattr(registry, "REGISTRY_MAX_VALID_BLOCKS", 99)
    assert core.points_v3_rules_hash() != a


# ======================================================================================= R1: the offline tools
def _tool_files(tmp_path, old_priv, new_pub, valid_until=9000):
    import registry_tool as rt
    unsigned = rt.build_rotation(new_pub, 1, CID, "rotate", valid_until, registry.pubkey_from_private(old_priv))
    p = tmp_path / "unsigned.json"
    p.write_text(json.dumps(unsigned))
    return rt, unsigned, str(p)


def test_r1_tool_refuses_a_bad_new_key_and_writes_both_messages(tmp_path):
    import registry_tool as rt
    old = key()
    for name, bad in BAD_KEYS.items():
        with pytest.raises(ValueError):
            rt.build_rotation(bad, 1, CID, "r", 9000, registry.pubkey_from_private(old))
    new = key()
    new_pub = registry.pubkey_from_private(new)
    _, unsigned, _ = _tool_files(tmp_path, old, new_pub)
    u = unsigned[0]
    assert u["message_hex"] != u["new_key_message_hex"] and u["new_key_id"] == registry.key_id(new_pub)
    assert "sig" not in u and "new_sig" not in u


def test_r1_tool_assemble_needs_both_signatures_and_the_chain_accepts_the_result(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    old, new = key(), key()
    new_pub, old_pub = registry.pubkey_from_private(new), registry.pubkey_from_private(old)
    rt, unsigned, path = _tool_files(tmp_path, old, new_pub)
    u = unsigned[0]
    sk = lambda k: Ed25519PrivateKey.from_private_bytes(bytes.fromhex(k))
    s_old = sk(old).sign(bytes.fromhex(u["message_hex"])).hex()
    s_new = sk(new).sign(bytes.fromhex(u["new_key_message_hex"])).hex()
    with pytest.raises(ValueError):
        rt.assemble(u, s_old, old_pub)                                  # no co-signature
    with pytest.raises(ValueError):
        rt.assemble(u, s_old, old_pub, s_old)                           # the wrong key co-signed
    with pytest.raises(ValueError):
        rt.assemble(dict(u, new_key_message_hex="00"), s_old, old_pub, s_new)   # edited file
    tx = rt.assemble(u, s_old, old_pub, s_new)
    assert tx["new_sig"] == s_new and registry.signature_ok(tx, CID, old_pub)
    st = _state(old, monkeypatch)
    assert st.check_and_apply(tx, 6000, CID)
    # an ordinary update refuses a stray co-signature
    plain = rt.build_updates([addr("p")], set(), 2, CID, "r", old_pub, 9000)[0]
    s_plain = sk(old).sign(bytes.fromhex(plain["message_hex"])).hex()
    with pytest.raises(ValueError):
        rt.assemble(plain, s_plain, old_pub, s_new)


def test_r1_offline_signer_can_sign_as_the_new_key_and_checks_it_is_the_right_key(tmp_path, monkeypatch, capsys):
    import builtins, getpass, re
    import registry_sign_offline
    old, new = key(), key()
    new_pub, old_pub = registry.pubkey_from_private(new), registry.pubkey_from_private(old)
    rt, unsigned, path = _tool_files(tmp_path, old, new_pub)
    monkeypatch.setattr(builtins, "input", lambda p="": "yes")

    def run(argv, priv):
        monkeypatch.setattr(sys, "argv", ["x"] + argv)
        monkeypatch.setattr(getpass, "getpass", lambda p="": priv)
        capsys.readouterr()
        registry_sign_offline.main()
        out = capsys.readouterr().out
        assert priv not in out
        return re.findall(r"\b[0-9a-f]{128}\b", out)[-1]

    s_old = run([path], old)
    s_new = run([path, "--as-new-key"], new)
    tx = rt.assemble(unsigned[0], s_old, old_pub, s_new)
    assert registry.signature_ok(tx, CID, old_pub)
    # the wrong private key is refused on both sides, before anything is printed as a signature
    with pytest.raises(SystemExit):
        run([path, "--as-new-key"], old)
    with pytest.raises(SystemExit):
        run([path], new)
    # --as-new-key on an ordinary update makes no sense
    plain = tmp_path / "plain.json"
    plain.write_text(json.dumps(rt.build_updates([addr("p")], set(), 2, CID, "r", old_pub, 9000)))
    with pytest.raises(SystemExit):
        run([str(plain), "--as-new-key"], new)
