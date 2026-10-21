import copy
import hashlib
import json
import time

import pytest

from conftest import build_block
from core import BLOCK_REWARD, make_reward_tx
from wallet import CHAIN_ID, verify_signed_tx


def fund(bc, w, blocks=1):
    for _ in range(blocks):
        assert bc.add_block(bc.mine_block(w.address, []).to_dict())


# --- valid acceptance ---------------------------------------------------

def test_mined_block_is_accepted_and_pays_reward(chain, wallets):
    alice, _ = wallets
    fund(chain, alice)
    assert len(chain.chain) == 2
    assert chain.get_balance(alice.address) == BLOCK_REWARD


def test_valid_signed_tx_is_included_and_moves_funds(chain, wallets):
    alice, bob = wallets
    fund(chain, alice)
    tx = alice.create_transaction(bob.address, 4, nonce=1)
    block = chain.mine_block(bob.address, [tx])
    assert len(block.transactions) == 2
    assert chain.add_block(block.to_dict())
    fee = tx["payload"]["fee"]
    assert chain.get_balance(alice.address) == BLOCK_REWARD - 4 - fee
    assert chain.get_balance(bob.address) == BLOCK_REWARD + 4 + fee  # bob mined: collects the fee


# --- nonce rules -----------------------------------------------------------

def test_replayed_tx_is_rejected(chain, wallets):
    alice, bob = wallets
    fund(chain, alice)
    tx = alice.create_transaction(bob.address, 1, nonce=1)
    fee = tx["payload"]["fee"]
    assert chain.add_block(build_block(chain, [make_reward_tx(bob.address, BLOCK_REWARD, fee), tx]).to_dict())
    replay = build_block(chain, [make_reward_tx(bob.address, BLOCK_REWARD, fee), copy.deepcopy(tx)])
    assert not chain.add_block(replay.to_dict())


def test_skipped_nonce_is_rejected(chain, wallets):
    alice, bob = wallets
    fund(chain, alice)
    tx = alice.create_transaction(bob.address, 1, nonce=2)
    block = build_block(chain, [make_reward_tx(bob.address, BLOCK_REWARD), tx])
    assert not chain.add_block(block.to_dict())


# --- balances ----------------------------------------------------------------

def test_overspend_is_rejected(chain, wallets):
    alice, bob = wallets
    fund(chain, alice)
    tx = alice.create_transaction(bob.address, BLOCK_REWARD + 1, nonce=1)
    block = build_block(chain, [make_reward_tx(bob.address, BLOCK_REWARD), tx])
    assert not chain.add_block(block.to_dict())
    # and the miner silently drops it from its own block
    assert len(chain.mine_block(bob.address, [tx]).transactions) == 1


# --- block structure -----------------------------------------------------------

def test_two_reward_txs_in_one_block_rejected(chain, wallets):
    alice, bob = wallets
    txs = [make_reward_tx(alice.address, BLOCK_REWARD), make_reward_tx(bob.address, BLOCK_REWARD)]
    assert not chain.add_block(build_block(chain, txs).to_dict())


def test_inflated_reward_rejected(chain, wallets):
    alice, _ = wallets
    block = build_block(chain, [make_reward_tx(alice.address, BLOCK_REWARD * 100)])
    assert not chain.add_block(block.to_dict())


# --- proof of work / difficulty ---------------------------------------------------

def test_bad_pow_rejected(chain, wallets):
    alice, _ = wallets
    block = chain.mine_block(alice.address, []).to_dict()
    block["nonce"] += 1  # hash no longer matches
    assert not chain.add_block(block)


def test_hash_without_enough_zeros_rejected(chain, wallets):
    alice, _ = wallets
    b = chain.mine_block(alice.address, [])
    # find a nonce whose (correctly recomputed) hash fails the zero-prefix target
    n = 0
    while True:
        h = chain.calculate_hash(index=b.index, timestamp=b.timestamp, transactions=b.transactions,
                                 previous_hash=b.previous_hash, nonce=n, difficulty=b.difficulty)
        if not h.startswith("0" * b.difficulty):
            break
        n += 1
    d = b.to_dict(); d["nonce"] = n; d["hash"] = h
    assert not chain.add_block(d)


def test_wrong_difficulty_rejected(chain, wallets):
    alice, _ = wallets
    expected = chain.expected_block_difficulty(chain.chain)
    block = build_block(chain, [make_reward_tx(alice.address, BLOCK_REWARD)], difficulty=expected - 1)
    assert not chain.add_block(block.to_dict())


def test_tampered_tx_breaks_hash(chain, wallets):
    alice, bob = wallets
    block = chain.mine_block(alice.address, []).to_dict()
    block["transactions"][0]["recipient"] = bob.address
    assert not chain.add_block(block)


# --- fork choice ---------------------------------------------------------------

def test_chain_replaced_by_more_work(chain, wallets, tmp_path):
    alice, bob = wallets
    fund(chain, alice, 1)
    longer = list(chain.chain[:1])
    for _ in range(2):
        longer.append(build_block(chain, [make_reward_tx(bob.address, BLOCK_REWARD)], chain=longer))
    assert chain.replace_chain([b.to_dict() for b in longer])
    assert chain.get_balance(bob.address) == 2 * BLOCK_REWARD
    assert chain.get_balance(alice.address) == 0


def test_chain_not_replaced_by_equal_or_less_work(chain, wallets):
    alice, bob = wallets
    fund(chain, alice, 2)
    shorter = list(chain.chain[:1])
    shorter.append(build_block(chain, [make_reward_tx(bob.address, BLOCK_REWARD)], chain=shorter))
    assert not chain.replace_chain([b.to_dict() for b in shorter])
    assert len(chain.chain) == 3


def test_invalid_chain_never_replaces(chain, wallets):
    alice, bob = wallets
    fake = list(chain.chain[:1])
    for _ in range(3):
        fake.append(build_block(chain, [make_reward_tx(bob.address, 999)], chain=fake))
    assert not chain.replace_chain([b.to_dict() for b in fake])


# --- contribution points -----------------------------------------------------------

def _cp():
    with open("state_contributions.json") as f:
        return {k: v["cp"] for k, v in json.load(f)["users"].items()}


def test_contribution_points_per_block(chain, wallets):
    alice, bob = wallets
    fund(chain, alice)
    assert _cp() == {alice.address: "0"}       # empty self-mined block: 0 mining points
    tx = alice.create_transaction(bob.address, 10**8, nonce=1)
    assert chain.add_block(chain.mine_block(bob.address, [tx]).to_dict())
    assert _cp() == {alice.address: "1", bob.address: "2.5"}


def test_contribution_points_follow_chain_after_reorg(chain, wallets):
    alice, bob = wallets
    fund(chain, alice)
    longer = list(chain.chain[:1])
    for _ in range(2):
        longer.append(build_block(chain, [make_reward_tx(bob.address, BLOCK_REWARD)], chain=longer))
    assert chain.replace_chain([b.to_dict() for b in longer])
    assert _cp().get(alice.address, 0) == 0


# --- signatures (Ed25519) ------------------------------------------------------

def _resign_mine_block(bc, miner, tx):
    from core import make_reward_tx as r
    return build_block(bc, [r(miner, BLOCK_REWARD), tx])


def test_tx_cannot_be_forged_without_private_key(chain, wallets):
    """The old scheme: sig = sha256(payload | pubkey). Anyone could compute it."""
    alice, bob = wallets
    fund(chain, alice)
    payload = {"sender": alice.address, "recipient": bob.address, "amount": BLOCK_REWARD // 2,
               "nonce": 1, "fee": 10_000, "timestamp": time.time(), "chain_id": CHAIN_ID}
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    old_style = hashlib.sha256(f"{canon}|{alice.pubkey}".encode()).hexdigest()
    for sig in (old_style, old_style * 2, "00" * 64):
        forged = {"payload": payload, "pubkey": alice.pubkey, "sig": sig}
        assert not verify_signed_tx(forged)
        if len(sig) == 128:  # shape-valid sigs must also be refused inside a block
            assert not chain.add_block(_resign_mine_block(chain, bob.address, forged).to_dict())


def test_genuine_signature_verifies(wallets):
    alice, bob = wallets
    assert verify_signed_tx(alice.create_transaction(bob.address, 1, nonce=1))


def test_tampered_amount_rejected(chain, wallets):
    alice, bob = wallets
    fund(chain, alice)
    tx = alice.create_transaction(bob.address, 1, nonce=1)
    tx["payload"]["amount"] = 9
    assert not verify_signed_tx(tx)
    assert not chain.add_block(_resign_mine_block(chain, bob.address, tx).to_dict())


def test_tampered_recipient_or_chain_id_rejected(wallets):
    alice, bob = wallets
    tx = alice.create_transaction(bob.address, 1, nonce=1)
    t1 = copy.deepcopy(tx); t1["payload"]["recipient"] = "f" * 32
    t2 = copy.deepcopy(tx); t2["payload"]["chain_id"] = "some-other-chain"
    assert not verify_signed_tx(t1)
    assert not verify_signed_tx(t2)


def test_wrong_pubkey_for_address_rejected(wallets):
    alice, bob = wallets
    tx = bob.create_transaction(alice.address, 1, nonce=1)
    # claim the tx is from alice, but it's signed by bob's key / bob's pubkey
    tx["payload"]["sender"] = alice.address
    tx["payload"]["recipient"] = "e" * 32
    assert not verify_signed_tx(tx)
    tx["pubkey"] = alice.pubkey  # now address matches, but sig is bob's
    assert not verify_signed_tx(tx)


def test_signature_from_different_key_rejected(chain, wallets):
    alice, bob = wallets
    from wallet import Wallet
    mallory = Wallet.create("mallory")
    fund(chain, alice)
    real = alice.create_transaction(bob.address, 5, nonce=1)
    stolen = mallory.create_transaction(bob.address, 5, nonce=1)
    forged = copy.deepcopy(real)
    forged["sig"] = stolen["sig"]  # valid Ed25519 sig, but by the wrong key
    assert not verify_signed_tx(forged)
    assert not chain.add_block(_resign_mine_block(chain, bob.address, forged).to_dict())


def test_old_style_wallet_is_refused(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from wallet import Wallet, pubkey_to_address
    priv = "11" * 32
    pub = hashlib.sha256(priv.encode()).hexdigest()  # pre-Ed25519 derivation
    with pytest.raises(ValueError):
        Wallet("old", priv, pub)
    json.dump({pubkey_to_address(pub): {"name": "old", "pubkey": pub, "private_key": priv}},
              open("wallets.json", "w"))
    with pytest.raises(ValueError):
        Wallet.load(pubkey_to_address(pub))


def test_existing_invalid_chain_is_not_overwritten(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "chain.json").write_text('[{"not": "a valid chain"}]')
    from core import Blockchain
    with pytest.raises(RuntimeError):
        Blockchain()
    assert "not" in (tmp_path / "chain.json").read_text()


def test_new_genesis_backs_up_and_resets(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import new_genesis
    (tmp_path / "chain.json").write_text("old chain")
    (tmp_path / "wallets.json").write_text("{}")
    monkeypatch.setattr("sys.argv", ["new_genesis.py", "--yes"])
    assert new_genesis.main() == 0
    from core import Blockchain
    assert len(Blockchain().chain) == 1
    assert list(tmp_path.glob("chain.json.pre-ed25519-*.bak"))[0].read_text() == "old chain"
    assert (tmp_path / "wallets.json").read_text() == "{}"
