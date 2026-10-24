"""Testnet-only short payout epochs (economics.TESTNET_SHORT_EPOCHS)."""
import json
import urllib.request
from dataclasses import replace

import pytest

import economics as eco
from core import Blockchain

NLT = eco.BASE_UNITS_PER_NLT


def econ(chain_id="netlithic-dev-1", **kw):
    base = dict(blocks_per_month=10, epoch_blocks=100, vesting_epochs=2, chain_id=chain_id,
                unlocks={"community": (0, 240), "treasury": (0, 48), "reserve": (20, 72)})
    base.update(kw)
    return replace(eco.load_economics(), **base)


@pytest.fixture
def short(monkeypatch):
    monkeypatch.setitem(eco.TESTNET_SHORT_EPOCHS, "netlithic-dev-1", (3, 4))


def mine(bc, addr, txs=()):
    assert bc.add_block(bc.mine_block(addr, list(txs)).to_dict()), bc.last_reject_reason


OTHER = "ab" * 16


def test_rule_does_not_change_genesis_hash(monkeypatch):
    before = eco.load_economics().params_hash()
    monkeypatch.setitem(eco.TESTNET_SHORT_EPOCHS, "netlithic-dev-1", (3, 4))
    assert eco.load_economics().params_hash() == before
    assert "short" not in json.dumps(sorted(eco.Economics.__dataclass_fields__))


def test_mainnet_chain_ids_never_get_short_epochs(monkeypatch):
    monkeypatch.setitem(eco.TESTNET_SHORT_EPOCHS, "netlithic-main-1", (3, 4))
    e = econ(chain_id="netlithic-main-1", epoch_blocks=eco.BLOCKS_PER_MONTH)
    assert e.short_epoch() is None
    assert e.next_epoch_height(5000) == eco.BLOCKS_PER_MONTH
    assert e.epoch_length_at(10 ** 6) == eco.BLOCKS_PER_MONTH


def test_default_testnet_rule_and_real_mainnet_numbers():
    e = eco.load_economics()
    assert e.short_epoch() == eco.TESTNET_SHORT_EPOCHS["netlithic-dev-1"]
    fork, length = e.short_epoch()
    assert not e.is_epoch_block(fork) and e.is_epoch_block(fork + length)
    assert e.next_epoch_height(fork - 1) == fork + length
    assert e.epoch_id(fork - 1) == 0 and e.epoch_id(fork) == 1 and e.epoch_id(fork + length) == 2
    assert e.epoch_length_at(0) == eco.BLOCKS_PER_MONTH


def test_schedule_math(short):
    e = econ()
    assert [h for h in range(0, 20) if e.is_epoch_block(h)] == [7, 11, 15, 19]
    assert e.next_epoch_height(0) == 7 and e.next_epoch_height(7) == 11
    assert e.vesting_blocks_at(7) == 8 and e.vesting_blocks_at(2) == 200


def test_short_epoch_pays_and_vests(short, tmp_path, monkeypatch, wallets):
    monkeypatch.chdir(tmp_path)
    alice, bob = wallets
    bc = Blockchain(econ=econ())
    mine(bc, alice.address)                                   # 1
    mine(bc, alice.address)                                   # 2 (pre-fork, empty)
    for n in (1, 2, 3):                                       # 3,4,5: epoch window [3,7)
        mine(bc, bob.address, [alice.create_transaction(OTHER, NLT, nonce=n)])
    mine(bc, bob.address)                                     # 6 (empty: 0 points)
    pa = bc.payout_info()
    assert pa["next_epoch_height"] == 7 and pa["next_block_payouts"]
    mine(bc, alice.address)                                   # 7 = payout block
    pays = [t for t in bc.chain[7].transactions if t["type"] == "payout"]
    got = {p["recipient"]: p for p in pays}
    # bob 3 blocks with transfers = 7.5 pts; alice 3 transfers = 3 pts; both capped at 25k
    assert set(got) == {alice.address, bob.address}
    assert all(p["amount"] == 25_000 * NLT and p["vest_end"] == 15 and p["epoch"] == 2 for p in pays)
    acct = bc.payout_account(bob.address)
    assert acct["earned_nlt"] == "25000"
    assert acct["vesting_nlt"] == "21875" and acct["claimable_nlt"] == "3125"  # 1 of 8 blocks
    for _ in range(8):
        mine(bc, OTHER)
    acct = bc.payout_account(bob.address)
    assert acct["vesting_nlt"] == "0" and acct["claimable_nlt"] == "25000"


def test_short_epoch_deterministic_across_nodes(short, tmp_path, monkeypatch, wallets):
    monkeypatch.chdir(tmp_path)
    alice, bob = wallets
    bc = Blockchain(econ=econ())
    mine(bc, alice.address)                                   # 1: alice gets coins
    for n in range(1, 7):
        mine(bc, bob.address, [alice.create_transaction(OTHER, NLT, nonce=n)])
    (tmp_path / "n2").mkdir()
    monkeypatch.chdir(tmp_path / "n2")
    other = Blockchain(econ=econ())
    assert other.replace_chain(bc.to_list())
    assert other.to_list() == bc.to_list()
    assert other.payout_account(bob.address) == bc.payout_account(bob.address)


def test_node_without_rule_rejects_short_payout_block(short, tmp_path, monkeypatch, wallets):
    """An old node (no short-epoch rule) refuses the payout block: so all nodes
    must upgrade before the fork height."""
    monkeypatch.chdir(tmp_path)
    alice, bob = wallets
    bc = Blockchain(econ=econ())
    mine(bc, alice.address)                                   # 1: alice gets coins
    for n in range(1, 7):
        mine(bc, bob.address, [alice.create_transaction(OTHER, NLT, nonce=n)])
    assert any(t["type"] == "payout" for t in bc.chain[7].transactions)
    monkeypatch.delitem(eco.TESTNET_SHORT_EPOCHS, "netlithic-dev-1")
    (tmp_path / "old").mkdir()
    monkeypatch.chdir(tmp_path / "old")
    old = Blockchain(econ=econ())
    assert not old.replace_chain(bc.to_list())


def test_api_shows_payouts(node, wallets):
    alice, _ = wallets
    with urllib.request.urlopen(f"{node.self_url()}/payouts?address={alice.address}") as r:
        p = json.loads(r.read())
    assert p["earned_nlt"] == "0" and p["claimable_nlt"] == "0" and "no value" in p["note"]
    with urllib.request.urlopen(f"{node.self_url()}/account?address={alice.address}") as r:
        a = json.loads(r.read())
    assert a["payouts"]["vesting_nlt"] == "0"


def test_real_testnet_first_post_fork_payout_is_6440():
    """Approved 2026-10-23: 1440-block periods from 5000 -> first payout at 6440, then 7880."""
    e = econ(epoch_blocks=eco.DEFAULT_EPOCH_BLOCKS)
    assert e.short_epoch() == (5000, 1440)
    assert e.epoch_length_at(4999) == eco.DEFAULT_EPOCH_BLOCKS   # pre-5000 unchanged
    assert e.next_epoch_height(4999) == 6440
    assert e.next_epoch_height(6440) == 7880
    assert not e.is_epoch_block(5144) and e.is_epoch_block(6440)
