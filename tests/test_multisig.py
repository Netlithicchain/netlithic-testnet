import copy
import json
import time
from dataclasses import replace

import pytest

import economics as eco
from conftest import build_block
from core import Blockchain, make_reward_tx
from wallet import CHAIN_ID, Wallet

NLT = eco.BASE_UNITS_PER_NLT


@pytest.fixture
def keys(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return [Wallet.create(f"k{i}") for i in range(1, 6)]


def ms_econ(keys, **kw):
    base = dict(blocks_per_month=10, epoch_blocks=1000, spend_approval="multisig",
                multisig_threshold=3, keyholders=tuple(k.pubkey for k in keys),
                unlocks={"community": (0, 240), "treasury": (0, 48), "reserve": (20, 72)})
    base.update(kw)
    return replace(eco.load_economics(), **base)


@pytest.fixture
def bc(keys):
    return Blockchain(econ=ms_econ(keys))


def spend(signers, to, amount, nonce=1, bucket="treasury", chain_id=CHAIN_ID):
    payload = {"bucket": bucket, "recipient": to, "amount": amount, "nonce": nonce,
               "timestamp": time.time(), "chain_id": chain_id}
    return {"type": "multisig_spend", "payload": payload, "sigs": [k.sign_multisig(payload) for k in signers]}


def ok(bc, tx, mempool=()):
    return bc.validate_candidate_tx_against_chain_and_mempool(tx, list(mempool))


def mine(bc, addr, txs=()):
    assert bc.add_block(bc.mine_block(addr, list(txs)).to_dict()), bc.last_reject_reason


def in_block(bc, miner, tx):
    return bc.add_block(build_block(bc, [make_reward_tx(miner, bc.expected_reward(bc.chain)), tx]).to_dict())


UNLOCKED_T1 = 150_000_000 * NLT // 48   # treasury unlocked at height 1


def test_valid_3_of_5_accepted_and_mined(bc, keys, wallets):
    _, bob = wallets
    tx = spend(keys[:3], bob.address, 1000 * NLT)
    assert ok(bc, tx)
    block = bc.mine_block(bob.address, [tx])
    assert block.transactions[1]["type"] == "multisig_spend"
    assert bc.add_block(block.to_dict())
    assert bc.get_balance(bob.address) == 1000 * NLT + 10 * NLT
    assert bc.get_balance(bc.econ.allocations["treasury"]) == 150_000_000 * NLT - 1000 * NLT


def test_any_3_of_the_5_work(bc, keys, wallets):
    _, bob = wallets
    assert ok(bc, spend([keys[4], keys[1], keys[3]], bob.address, NLT))
    assert ok(bc, spend(keys, bob.address, NLT))          # more than M is fine


def test_m_minus_1_signatures_rejected(bc, keys, wallets):
    _, bob = wallets
    tx = spend(keys[:2], bob.address, NLT)
    assert not ok(bc, tx)
    assert not in_block(bc, bob.address, tx)


def test_duplicate_signer_rejected(bc, keys, wallets):
    _, bob = wallets
    tx = spend(keys[:2], bob.address, NLT)
    tx["sigs"].append(dict(tx["sigs"][0]))
    assert not ok(bc, tx)
    assert not in_block(bc, bob.address, tx)


def test_non_keyholder_signature_rejected(bc, keys, wallets):
    alice, bob = wallets
    tx = spend(keys[:2] + [alice], bob.address, NLT)
    assert not ok(bc, tx)
    assert not in_block(bc, bob.address, tx)


def test_signature_over_different_tx_rejected(bc, keys, wallets):
    _, bob = wallets
    tx = spend(keys[:3], bob.address, NLT)
    tx["payload"]["amount"] = 2 * NLT                    # changed after signing
    assert not ok(bc, tx)
    tx2 = spend(keys[:3], bob.address, NLT)
    tx2["payload"]["recipient"] = "ab" * 16
    assert not ok(bc, tx2)


def test_over_unlocked_amount_rejected(bc, keys, wallets):
    _, bob = wallets
    assert not ok(bc, spend(keys[:3], bob.address, UNLOCKED_T1 + 1))
    assert not in_block(bc, bob.address, spend(keys[:3], bob.address, UNLOCKED_T1 + 1))
    assert ok(bc, spend(keys[:3], bob.address, UNLOCKED_T1))


def test_two_spends_together_cannot_exceed_unlocked(bc, keys, wallets):
    _, bob = wallets
    first = spend(keys[:3], bob.address, UNLOCKED_T1, nonce=1)
    second = spend(keys[:3], bob.address, 1, nonce=2)
    assert ok(bc, first)
    assert not ok(bc, second, [first])
    block = bc.mine_block(bob.address, [first, second])
    assert [t.get("type") for t in block.transactions] == ["reward", "multisig_spend"]


def test_replay_rejected(bc, keys, wallets):
    _, bob = wallets
    tx = spend(keys[:3], bob.address, NLT)
    mine(bc, bob.address, [tx])
    assert not ok(bc, copy.deepcopy(tx))
    assert not in_block(bc, bob.address, copy.deepcopy(tx))
    assert ok(bc, spend(keys[:3], bob.address, NLT, nonce=2))
    assert not ok(bc, spend(keys[:3], bob.address, NLT, nonce=3))   # gaps not allowed


def test_duplicate_nonce_in_mempool_rejected(bc, keys, wallets):
    _, bob = wallets
    a = spend(keys[:3], bob.address, NLT, nonce=1)
    b = spend(keys[1:4], bob.address, 2 * NLT, nonce=1)
    assert not ok(bc, b, [a])


def test_wrong_chain_id_rejected(bc, keys, wallets):
    _, bob = wallets
    assert not ok(bc, spend(keys[:3], bob.address, NLT, chain_id="netlithic-other"))


def test_reserve_before_cliff_rejected_even_with_all_signatures(bc, keys, wallets):
    _, bob = wallets
    assert not ok(bc, spend(keys, bob.address, NLT, bucket="reserve"))


def test_single_signer_transfer_from_treasury_still_rejected(tmp_path, monkeypatch, wallets, keys):
    alice, bob = wallets
    econ = ms_econ(keys, allocations={"community": "c" * 32, "treasury": alice.address, "reserve": "e" * 32})
    bc = Blockchain(econ=econ)
    assert not ok(bc, alice.create_transaction(bob.address, NLT, nonce=1))


def test_mode_none_rejects_multisig(keys, wallets, tmp_path, monkeypatch):
    _, bob = wallets
    (tmp_path / "n").mkdir(); monkeypatch.chdir(tmp_path / "n")
    bc = Blockchain(econ=ms_econ(keys, spend_approval="none"))
    assert not ok(bc, spend(keys, bob.address, NLT))


def test_default_config_has_no_keyholders_and_no_spends(tmp_path, monkeypatch, keys, wallets):
    _, bob = wallets
    (tmp_path / "d").mkdir(); monkeypatch.chdir(tmp_path / "d")
    bc = Blockchain()                                          # repo config
    assert bc.econ.spend_approval == "none"
    assert bc.econ.keyholders == ()
    assert not ok(bc, spend(keys, bob.address, NLT))


# --- config loader -------------------------------------------------------------------

def write_cfg(tmp_path, mutate):
    cfg = json.load(open(eco.CONFIG_PATH))
    mutate(cfg)
    p = tmp_path / "cfg.json"; p.write_text(json.dumps(cfg))
    return str(p)


def real_holders(cfg, keys, **flags):
    cfg["treasury_spend_approval"].update(mode="multisig", threshold=3)
    cfg["treasury_spend_approval"]["keyholders"] = [
        {"name": k.name, "pubkey": k.pubkey, **flags} for k in keys]


@pytest.mark.parametrize("mutate, msg", [
    (lambda c, k: c.update(chain_id="netlithic-testnet-1") or
        c["treasury_spend_approval"].update(mode="single_key_dev"), "dev chains"),
    (lambda c, k: c.update(chain_id="netlithic-testnet-1") or real_holders(c, k, test_key=True), "test keys"),
    (lambda c, k: real_holders(c, k) or c["treasury_spend_approval"].update(threshold=6), "threshold"),
    (lambda c, k: real_holders(c, k) or c["treasury_spend_approval"].update(threshold=0), "threshold"),
    (lambda c, k: c["treasury_spend_approval"].update(mode="multisig"), "threshold"),   # no keyholders
    (lambda c, k: real_holders(c, k[:2]) or c["treasury_spend_approval"].update(threshold=1) or
        [h.update(founder=True) for h in c["treasury_spend_approval"]["keyholders"]], "founder"),
    (lambda c, k: real_holders(c, k) or c["treasury_spend_approval"].update(mode="none"), "not multisig"),
    (lambda c, k: real_holders(c, k) or c["treasury_spend_approval"]["keyholders"][0].update(pubkey="xyz"), "invalid pubkey"),
    (lambda c, k: real_holders(c, [k[0]] * 5), "distinct"),
])
def test_bad_multisig_config_rejected(tmp_path, keys, mutate, msg):
    with pytest.raises(ValueError, match=msg):
        eco.load_economics(write_cfg(tmp_path, lambda c: mutate(c, keys)))


def test_dev_chain_allows_test_keys_and_single_key_dev(tmp_path, keys):
    e = eco.load_economics(write_cfg(tmp_path, lambda c: real_holders(c, keys, test_key=True)))
    assert e.keyholders == tuple(k.pubkey for k in keys)
    e = eco.load_economics(write_cfg(tmp_path, lambda c: c["treasury_spend_approval"].update(mode="single_key_dev")))
    assert e.spend_approval == "single_key_dev"


def test_non_dev_chain_accepts_real_keyholders(tmp_path, keys):
    def m(c):
        c["chain_id"] = "netlithic-testnet-1"
        real_holders(c, keys)
        c["treasury_spend_approval"]["keyholders"][0]["founder"] = True
    assert eco.load_economics(write_cfg(tmp_path, m)).multisig_threshold == 3


def test_keyholders_change_genesis(tmp_path, keys):
    a = eco.load_economics()
    b = eco.load_economics(write_cfg(tmp_path, lambda c: real_holders(c, keys, test_key=True)))
    assert a.params_hash() != b.params_hash()


# --- contribution point weights --------------------------------------------------------

def test_mining_weight_and_empty_block_exclusion(chain, wallets):
    alice, bob = wallets
    mine(chain, alice.address)                                   # empty: 0
    mine(chain, alice.address, [alice.create_transaction(bob.address, NLT, nonce=1)])
    pts = chain.contribution_points()[alice.address]
    assert (pts["cp_tenths"], pts["cp"], pts["blocks_mined"], pts["empty_blocks"]) == (35, "3.5", 2, 1)


def test_self_transfer_only_block_is_empty():
    import contributions
    m = "aa" * 16
    block = {"transactions": [{"type": "reward", "recipient": m, "amount": 1},
                              {"payload": {"sender": m, "recipient": m}}]}
    assert contributions.is_empty_self_mined(block)
    block["transactions"][1]["payload"]["recipient"] = "bb" * 16
    assert not contributions.is_empty_self_mined(block)


def test_mining_weight_configurable(tmp_path, monkeypatch, wallets):
    alice, bob = wallets
    monkeypatch.chdir(tmp_path)
    bc = Blockchain(econ=replace(eco.load_economics(), points_per_block=40, points_per_tx=5))
    mine(bc, alice.address)
    mine(bc, bob.address, [alice.create_transaction(bob.address, NLT, nonce=1)])
    pts = bc.contribution_points()
    assert (pts[bob.address]["cp"], pts[alice.address]["cp"]) == ("4", "0.5")


def test_config_point_weights_load(tmp_path):
    e = eco.load_economics(write_cfg(tmp_path, lambda c: c["contribution_points"].update(per_block_mined_tenths=5)))
    assert e.points_per_block == 5 and e.params_hash() != eco.load_economics().params_hash()


def test_payouts_reflect_weights(tmp_path, monkeypatch, wallets):
    alice, bob = wallets
    monkeypatch.chdir(tmp_path)
    bc = Blockchain(econ=replace(eco.load_economics(), epoch_blocks=4, payout_cap=eco.MAX_SUPPLY))
    mine(bc, alice.address)                                       # empty -> alice 0
    mine(bc, alice.address, [alice.create_transaction(bob.address, NLT, nonce=1)])  # alice 2.5 + 1
    mine(bc, bob.address, [alice.create_transaction(bob.address, NLT, nonce=2)])    # bob 2.5, alice +1
    pool = bc.community_pool_available(bc.chain, 4)
    payouts = {p["recipient"]: p["amount"] for p in bc.expected_payouts(bc.chain, 4)}
    assert payouts == {alice.address: pool * 45 // 70, bob.address: pool * 25 // 70}


# --- CLI flow ---------------------------------------------------------------------------

def test_cli_build_cosign_submit(node, wallets, capsys, monkeypatch):
    import netlithic as cli
    _, bob = wallets
    keyholders = [Wallet.create(f"k{i}") for i in range(1, 6)]
    node.blockchain.econ = replace(node.blockchain.econ, spend_approval="multisig", multisig_threshold=3,
                                   keyholders=tuple(k.pubkey for k in keyholders))
    url = node.self_url()
    assert cli.main(["multisig", "build", "treasury", bob.address, "1234.5", "--node", url]) == 0
    assert cli.main(["multisig", "submit", "--node", url]) == 1          # unsigned: node refuses
    for k in ("k1", "k3"):
        assert cli.main(["multisig", "sign", k]) == 0
    assert cli.main(["multisig", "sign", "k1"]) == 1                     # can't sign twice
    assert cli.main(["multisig", "submit", "--node", url]) == 1          # 2 of 3
    assert cli.main(["multisig", "sign", "k5"]) == 0
    capsys.readouterr()
    assert cli.main(["multisig", "submit", "--node", url]) == 0
    assert "accepted" in capsys.readouterr().out
    bc = node.blockchain
    assert bc.add_block(bc.mine_block(bob.address, list(node.mempool)).to_dict())
    assert bc.get_balance(bob.address) == 12345 * NLT // 10 + 10 * NLT
