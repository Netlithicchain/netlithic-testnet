"""Tooling for the registered-contributors list: ops/registry_tool.py, ops/registry_sign_offline.py and the registry lines in
ops/verify_payouts.py. Only THROWAWAY keys made inside these tests are used; the tool itself never makes or reads one."""
import builtins
import getpass
import json
import os
import re
import shutil
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ops"))

import registry  # noqa: E402
import registry_sign_offline  # noqa: E402
import registry_tool as rt  # noqa: E402
import verify_payouts  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

CID = "netlithic-dev-1"
pytestmark = pytest.mark.registry


def a(i):
    return f"{i:032x}"


@pytest.fixture
def keypair():
    sk = Ed25519PrivateKey.generate()           # throwaway, lives only in this test
    from cryptography.hazmat.primitives import serialization as s
    priv = sk.private_bytes(s.Encoding.Raw, s.PrivateFormat.Raw, s.NoEncryption()).hex()
    pub = sk.public_key().public_bytes(s.Encoding.Raw, s.PublicFormat.Raw).hex()
    return sk, priv, pub


@pytest.fixture
def regfile(tmp_path):
    p = tmp_path / "registry.py"
    shutil.copy(os.path.join(ROOT, "registry.py"), p)
    return str(p)


def write(tmp_path, name, obj):
    p = tmp_path / name
    p.write_text(json.dumps(obj))
    return str(p)


# ---------------------------------------------------------------- the key slot
def test_shipped_slot_is_empty_and_check_fails(capsys):
    assert registry.REGISTRY_PUBKEY == "" or len(registry.REGISTRY_PUBKEY) == 64
    rc = rt.main(["--check"])
    assert rc == (1 if registry.REGISTRY_PUBKEY == "" else 0)
    assert rt.main(["--status"]) == 0
    if registry.REGISTRY_PUBKEY == "":
        assert "EMPTY" in capsys.readouterr().out


def test_check_fails_while_empty_then_passes_after_set_key(regfile, keypair, capsys):
    _, _, pub = keypair
    assert rt.main(["--check", "--file", regfile]) == 1
    assert rt.main(["set-key", pub, "--file", regfile]) == 0
    assert rt.read_slot(regfile) == pub
    assert rt.main(["--check", "--file", regfile]) == 0
    out = capsys.readouterr().out
    assert "key id" in out and " ".join(pub[i:i + 8] for i in range(0, 64, 8)) in out   # public key shown in groups to compare
    text = open(regfile).read()
    assert text.count("REGISTRY-PUBKEY-SLOT") >= 1 and f'REGISTRY_PUBKEY = "{pub}"' in text


def test_set_key_refuses_bad_values_a_second_key_and_the_pause_key(regfile, keypair, capsys):
    _, _, pub = keypair
    assert rt.main(["set-key", "abc", "--file", regfile]) == 2
    assert rt.main(["set-key", "00" * 32, "--file", regfile]) == 2
    assert rt.main(["set-key", "zz" * 32, "--file", regfile]) == 2
    assert rt.read_slot(regfile) == ""
    assert rt.main(["set-key", pub, "--file", regfile]) == 0
    other = Ed25519PrivateKey.generate().public_key().public_bytes_raw().hex()
    assert rt.main(["set-key", other, "--file", regfile]) == 2               # no silent replacement
    assert rt.read_slot(regfile) == pub
    assert rt.main(["set-key", other, "--force", "--file", regfile]) == 0
    assert rt.read_slot(regfile) == other


def test_pause_key_cannot_double_as_registry_key(regfile, monkeypatch):
    k = Ed25519PrivateKey.generate().public_key().public_bytes_raw().hex()
    monkeypatch.setattr(rt, "pause_key", lambda path=None: k)
    assert rt.main(["set-key", k, "--file", regfile]) == 2
    assert rt.read_slot(regfile) == ""


def test_slot_line_must_be_unique(tmp_path):
    p = tmp_path / "registry.py"
    p.write_text('REGISTRY_PUBKEY = ""  # REGISTRY-PUBKEY-SLOT\nREGISTRY_PUBKEY = ""  # REGISTRY-PUBKEY-SLOT\n')
    with pytest.raises(SystemExit):
        rt.read_slot(str(p))


# ---------------------------------------------------------------- list files
def test_verify_list(tmp_path, capsys):
    ok = write(tmp_path, "ok.json", {"entries": [{"address": a(1), "label": "tester one"}, {"address": a(2)}]})
    assert rt.main(["verify", ok]) == 0
    assert rt.main(["verify", os.path.join(ROOT, "ops/registry/registry_list.json")]) == 0   # the shipped list is empty
    bad = write(tmp_path, "bad.json", {"entries": [{"address": a(1)}, {"address": a(1)}, {"address": "XYZ"},
                                                   {"address": a(3), "private_key": "oops"}, "str"]})
    assert rt.main(["verify", bad]) == 1
    out = capsys.readouterr().out
    assert "appears twice" in out and "32 lowercase hex" in out and "secret" in out and "must be an object" in out
    assert rt.verify_list([]) and rt.verify_list({"entries": "x"})
    big = {"entries": [{"address": a(i)} for i in range(registry.MAX_ACTIVE_ENTRIES + 1)]}
    assert any("more than the chain accepts" in p for p in rt.verify_list(big))


# ---------------------------------------------------------------- build -> sign elsewhere -> assemble
def test_full_offline_flow_produces_a_tx_the_chain_accepts(tmp_path, keypair, regfile, capsys):
    sk, priv, pub = keypair
    assert rt.main(["set-key", pub, "--file", regfile]) == 0
    lst = write(tmp_path, "list.json", {"entries": [{"address": a(i)} for i in (1, 2, 3)]})
    reg = write(tmp_path, "reg.json", {"active": [{"address": a(9)}], "pending": []})
    capsys.readouterr()
    assert rt.main(["build", lst, "--nonce", "1", "--registered", reg, "--file", regfile, "--reason", "first testers", "--valid-until", "9000"]) == 0
    unsigned = json.loads(capsys.readouterr().out)
    assert len(unsigned) == 1 and unsigned[0]["adds"] == [a(1), a(2), a(3)] and unsigned[0]["removes"] == [a(9)]
    assert "sig" not in unsigned[0] and "pubkey" not in unsigned[0]            # nothing signed, no key anywhere
    assert unsigned[0]["valid_until"] == 9000
    u = write(tmp_path, "unsigned.json", unsigned)
    sig = sk.sign(bytes.fromhex(unsigned[0]["message_hex"])).hex()             # "somewhere else": only the signature comes back
    assert rt.main(["assemble", u, "--signature", sig, "--file", regfile]) == 0
    tx = json.loads(capsys.readouterr().out)
    assert tx["pubkey"] == pub and registry.signature_ok(tx, CID, pub)
    # the chain's own check accepts it (nonce 1, nothing listed yet except 9, which it removes)
    st = registry.RegistryState(lambda h: h // 1440)
    st.events[a(9)] = [("add", 5000)]
    assert st.check_and_apply(tx, 6500, CID, pub)
    assert st.eligible(a(1), 6500 + registry.REGISTRY_DELAY_BLOCKS) and not st.eligible(a(1), 6500)
    # check-tx agrees
    t = write(tmp_path, "tx.json", tx)
    assert rt.main(["check-tx", t, "--file", regfile]) == 0


def test_assemble_refuses_wrong_signature_edited_file_and_empty_slot(tmp_path, keypair, regfile, capsys):
    sk, priv, pub = keypair
    rt.main(["set-key", pub, "--file", regfile])
    unsigned = rt.build_updates([a(1)], set(), 1, CID, "r", pub, 9000)
    good = sk.sign(bytes.fromhex(unsigned[0]["message_hex"])).hex()
    u = write(tmp_path, "u.json", unsigned)
    capsys.readouterr()
    other_sig = Ed25519PrivateKey.generate().sign(bytes.fromhex(unsigned[0]["message_hex"])).hex()
    assert rt.main(["assemble", u, "--signature", other_sig, "--file", regfile]) == 1       # not the registry key
    assert rt.main(["assemble", u, "--signature", "12", "--file", regfile]) == 1
    edited = [dict(unsigned[0], adds=[a(2)])]
    assert rt.main(["assemble", write(tmp_path, "e.json", edited), "--signature", good, "--file", regfile]) == 1
    assert rt.main(["assemble", u, "--signature", good, "--file", regfile]) == 0
    empty = str(tmp_path / "empty.py")
    shutil.copy(os.path.join(ROOT, "registry.py"), empty)
    if rt.read_slot(empty):
        rt.write_slot(empty, "")
    assert rt.main(["assemble", u, "--signature", good, "--file", empty]) == 1


def test_build_respects_the_chain_limits(tmp_path):
    many = {a(i) for i in range(1, registry.MAX_ADDS_PER_WINDOW + 2)}
    with pytest.raises(ValueError, match="additions are more than the chain allows"):
        rt.build_updates(sorted(many), set(), 1, CID, "r", None, 9000)
    with pytest.raises(ValueError, match="removals are more than the chain allows"):
        rt.build_updates([], {a(i) for i in range(1, registry.MAX_REMOVES_PER_WINDOW + 2)}, 1, CID, "r", None, 9000)
    with pytest.raises(ValueError, match="valid-until"):
        rt.build_updates([a(1)], set(), 1, CID, "r")
    ok = rt.build_updates(sorted(many)[:registry.MAX_ADDS_PER_WINDOW], set(), 7, CID, "r", None, 9000)
    assert [u["nonce"] for u in ok] == [7]
    # 50 adds + 30 removes = 80 entries = two transactions with consecutive nonces, each within the per-tx limit
    reg = {a(1000 + i) for i in range(30)}
    two = rt.build_updates(sorted(many)[:50], reg, 7, CID, "r", None, 9000)
    assert [u["nonce"] for u in two] == [7, 8]
    assert all(len(u["adds"]) + len(u["removes"]) <= registry.MAX_ENTRIES_PER_TX for u in two)
    assert len(two[0]["removes"]) == 30 and not two[1]["removes"]               # removals go first
    assert rt.build_updates([a(1)], {a(1)}, 1, CID, "r", None, 9000) == []                  # nothing to do
    with pytest.raises(ValueError, match="longer than the chain accepts"):
        rt.build_updates([a(i) for i in range(1, 11)] + [a(1000 + i) for i in range(495)], {a(1000 + i) for i in range(495)}, 1, CID, "r", None, 9000)


def test_build_cli_needs_to_know_what_the_chain_has(tmp_path, capsys):
    lst = write(tmp_path, "l.json", {"entries": [{"address": a(1)}]})
    with pytest.raises(SystemExit):
        rt.main(["build", lst, "--nonce", "1"])


def test_registered_from_info_counts_active_and_pending_but_not_removed():
    info = {"active": [{"address": a(1)}], "pending": [{"address": a(2)}], "removed": [{"address": a(3)}]}
    assert rt.registered_from_info(info) == {a(1), a(2)}


# ---------------------------------------------------------------- the offline signer
def test_offline_signer_signs_only_what_it_shows_and_asks_first(tmp_path, keypair, monkeypatch, capsys):
    sk, priv, pub = keypair
    unsigned = rt.build_updates([a(1)], set(), 1, CID, "r", pub, 9000)
    u = write(tmp_path, "u.json", unsigned)
    monkeypatch.setattr(sys, "argv", ["x", u])
    monkeypatch.setattr(builtins, "input", lambda p="": "yes")
    monkeypatch.setattr(getpass, "getpass", lambda p="": priv)
    registry_sign_offline.main()
    out = capsys.readouterr().out
    sig = re.findall(r"\b[0-9a-f]{128}\b", out)[-1]
    assert priv not in out
    assert rt.assemble(unsigned[0], sig, pub)["sig"] == sig
    # declining signs nothing
    monkeypatch.setattr(builtins, "input", lambda p="": "no")
    with pytest.raises(SystemExit):
        registry_sign_offline.main()
    # a file edited after build is refused before any key is asked for
    bad = write(tmp_path, "bad.json", [dict(unsigned[0], adds=[a(2)])])
    monkeypatch.setattr(sys, "argv", ["x", bad])
    monkeypatch.setattr(getpass, "getpass", lambda p="": pytest.fail("asked for a key for an edited file"))
    with pytest.raises(SystemExit):
        registry_sign_offline.main()


def test_the_build_box_tool_never_makes_or_reads_private_keys():
    src = open(os.path.join(ROOT, "ops", "registry_tool.py"), encoding="utf-8").read()
    for bad in ("Ed25519PrivateKey", ".generate(", "getpass", "from_private_bytes", "sign_registry_tx", "wallets.json",
                "miner_wallet", "faucet_wallet", "genesis_wallets"):
        assert bad not in src, bad
    assert "private key" in src.lower()      # it only talks about them


# ---------------------------------------------------------------- verify_payouts registry lines
PREV = {"status": "paid", "period": 3, "window_start": 6440, "window_end": 7880, "pays_at_height": 9320,
        "pots": {"release_budget_nlt": "100", "mining_pot_nlt": "20", "transfer_pot_nlt": "80"},
        "payouts": [{"address": a(1), "amount_base_units": 10 ** 8, "amount_nlt": "1", "transfer_points": 5, "mining_points": 0},
                    {"address": a(2), "amount_base_units": 10 ** 8, "amount_nlt": "1", "transfer_points": 0, "mining_points": 3}],
        "total_nlt": "2"}


def test_verify_payouts_explains_an_empty_or_keyless_registry():
    for mode in ("no_key", "empty"):
        lines, warns = verify_payouts.registry_notes(PREV, {"mode": mode, "active_count": 0, "pending_count": 0, "key_id": ""})
        assert any("Nobody can earn transfer points" in x for x in lines) and not warns
    assert verify_payouts.registry_notes(PREV, None) == ([], [])


def test_verify_payouts_flags_transfer_earners_who_are_not_on_the_list():
    info = {"mode": "on", "active_count": 1, "pending_count": 0, "key_id": "k", "active": [{"address": a(7)}]}
    lines, warns = verify_payouts.registry_notes(PREV, info)
    assert any("not on the list" in w and a(1) in w for w in warns)
    info["active"].append({"address": a(1)})
    assert not verify_payouts.registry_notes(PREV, info)[1]
    # a removed address is explained by the log, still reported only if absent from every list
    info["active"] = []
    info["removed"] = [{"address": a(1)}]
    assert not verify_payouts.registry_notes(PREV, info)[1]


def test_verify_payouts_flags_a_key_at_its_addition_limit():
    info = {"mode": "on", "active": [{"address": a(1)}], "adds_this_period": 50, "max_adds_per_period": 50}
    assert any("used up" in w for w in verify_payouts.registry_notes(PREV, info)[1])
    out_lines, out_warns = verify_payouts.analyse(PREV, registry_info=info)
    assert any("Registered-contributors list" in x for x in out_lines)
