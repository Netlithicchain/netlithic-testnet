import json
import urllib.request
from dataclasses import replace

import pytest

import economics as eco
from conftest import build_block
from core import Blockchain, make_reward_tx

NLT = eco.BASE_UNITS_PER_NLT
BIG_CAP = eco.MAX_SUPPLY


def small_econ(**kw):
    """Tiny test parameters: 10-block months, 5-block epochs, vesting over 2 epochs."""
    base = dict(blocks_per_month=10, epoch_blocks=5, vesting_epochs=2, payout_cap=BIG_CAP,
                unlocks={"community": (0, 240), "treasury": (0, 48), "reserve": (20, 72)})
    base.update(kw)
    return replace(eco.load_economics(), **base)


@pytest.fixture
def mk(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def make(**kw):
        return Blockchain(econ=small_econ(**kw))
    return make


def mine(bc, addr, txs=()):
    assert bc.add_block(bc.mine_block(addr, list(txs)).to_dict()), bc.last_reject_reason


OTHER = "ab" * 16


def to_epoch(bc, alice, bob):
    """Epoch 1 (blocks 1-4): alice mines an empty block (0 points), then two blocks each
    with one of her transfers (2.5 + 1 each), bob mines a block with alice's third
    transfer (bob 2.5, alice +1). Points in tenths: alice 80, bob 25. Block 5 pays out."""
    mine(bc, alice.address)
    for n, miner in ((1, alice), (2, alice), (3, bob)):
        mine(bc, miner.address, [alice.create_transaction(OTHER, NLT, nonce=n)])
    assert len(bc.chain) == 5
    pts = bc.contribution_points()
    assert (pts[alice.address]["cp_tenths"], pts[bob.address]["cp_tenths"]) == (80, 25)


# --- default numbers ------------------------------------------------------------------

def test_default_unlock_schedule_numbers():
    e = eco.load_economics()
    m = eco.BLOCKS_PER_MONTH
    assert e.unlocked("community", 0) == 2_500_000 * NLT                # 1/240 at start
    assert e.unlocked("treasury", 0) == 3_125_000 * NLT                 # 1/48 at start
    assert e.unlocked("treasury", m) == 6_250_000 * NLT
    assert e.unlocked("treasury", 48 * m) == 150_000_000 * NLT
    assert e.unlocked("reserve", 12 * m - 1) == 0                       # 1-year cliff
    assert e.unlocked("reserve", 12 * m) == 50_000_000 * NLT // 72
    assert e.unlocked("reserve", 84 * m) == 50_000_000 * NLT
    assert e.unlocked("community", 10 ** 9) == 600_000_000 * NLT
    assert (e.epoch_blocks, e.payout_cap, e.vesting_epochs, e.spend_approval) == (m, 25_000 * NLT, 6, "none")
    assert e.keyholders == ()                                   # multisig off by default
    assert (e.points_per_block, e.points_per_tx) == (25, 10)


def test_config_rejects_bad_approval_mode(tmp_path):
    cfg = json.load(open(eco.CONFIG_PATH))
    cfg["treasury_spend_approval"]["mode"] = "anyone"
    p = tmp_path / "c.json"; p.write_text(json.dumps(cfg))
    with pytest.raises(ValueError, match="mode"):
        eco.load_economics(str(p))


def test_different_economic_params_mean_different_chain(mk, tmp_path, monkeypatch):
    a = mk()
    with pytest.raises(RuntimeError):            # a node with other rules refuses this chain.json
        Blockchain(econ=small_econ(payout_cap=7 * NLT))
    (tmp_path / "other").mkdir(); monkeypatch.chdir(tmp_path / "other")
    b = Blockchain(econ=small_econ(payout_cap=7 * NLT))
    assert a.chain[0].hash != b.chain[0].hash
    assert not b.is_valid_chain(a.to_list())


# --- community payouts ------------------------------------------------------------------

def test_epoch_payout_pro_rata(mk, wallets):
    alice, bob = wallets
    bc = mk()
    to_epoch(bc, alice, bob)
    pool = bc.community_pool_available(bc.chain, 5)
    assert pool == 2_500_000 * NLT
    mine(bc, bob.address)
    payouts = [t for t in bc.chain[5].transactions if t["type"] == "payout"]
    assert [(p["recipient"], p["amount"]) for p in payouts] == sorted(
        [(alice.address, pool * 80 // 105), (bob.address, pool * 25 // 105)])
    assert all(p["vest_start"] == 5 and p["vest_end"] == 15 and p["epoch"] == 1 for p in payouts)
    paid = sum(p["amount"] for p in payouts)
    assert pool - paid < 2                       # only rounding dust stays behind
    assert bc.get_balance(bc.econ.allocations["community"]) == 600_000_000 * NLT - paid


def test_payouts_are_deterministic_across_nodes(mk, wallets, tmp_path, monkeypatch):
    alice, bob = wallets
    bc = mk()
    to_epoch(bc, alice, bob)
    mine(bc, bob.address)
    monkeypatch.chdir(tmp_path / ".." )
    other = Blockchain(econ=small_econ())  # a second node, same rules, fresh chain
    assert other.replace_chain(bc.to_list())
    assert other.expected_payouts(other.chain[:5], 5) == bc.expected_payouts(bc.chain[:5], 5)


def test_payout_capped_and_excess_stays_in_pool(mk, wallets):
    alice, bob = wallets
    bc = mk(payout_cap=3 * NLT)
    to_epoch(bc, alice, bob)
    mine(bc, bob.address)
    payouts = [t for t in bc.chain[5].transactions if t["type"] == "payout"]
    assert sorted(p["amount"] for p in payouts) == [3 * NLT, 3 * NLT]
    assert bc.community_pool_available(bc.chain, 6) == 2_500_000 * NLT - 6 * NLT


@pytest.mark.parametrize("tweak", ["inflate", "drop", "extra_in_normal_block"])
def test_wrong_payouts_rejected(mk, wallets, tweak):
    alice, bob = wallets
    bc = mk()
    to_epoch(bc, alice, bob)
    block = bc.mine_block(bob.address, [])
    txs = [dict(t) for t in block.transactions]
    if tweak == "inflate":
        txs[1]["amount"] += 1
        bad = build_block(bc, txs)
    elif tweak == "drop":
        bad = build_block(bc, txs[:1] + txs[2:])
    else:
        mine(bc, bob.address)  # the real epoch block
        fake = dict(txs[1], vest_start=6, vest_end=16)
        bad = build_block(bc, [make_reward_tx(bob.address, bc.expected_reward(bc.chain)), fake])
    assert not bc.add_block(bad.to_dict())


def test_no_points_no_payout(mk, wallets):
    bc = mk()
    assert bc.expected_payouts(bc.chain, 5) == []  # nobody earned points in the window


# --- vesting ------------------------------------------------------------------------

def test_payouts_vest_linearly_and_are_enforced(mk, wallets):
    alice, bob = wallets
    bc = mk()
    to_epoch(bc, alice, bob)
    mine(bc, alice.address)                          # block 5: payouts, alice mines -> +10 NLT
    paid = next(t["amount"] for t in bc.chain[5].transactions
                if t["type"] == "payout" and t["recipient"] == bob.address)
    # vests 5 -> 15: at height 6 (next block) 1/10 is vested; bob also has 10 NLT mined
    vested = paid // 10
    fees = sum(b.transactions[0]["fees"] for b in bc.chain[1:] if b.transactions[0]["recipient"] == bob.address)
    assert bc.account_info(bob.address)["spendable_nlt"] == eco.to_nlt(10 * NLT + vested + fees)
    assert bc.account_info(bob.address)["locked_or_unvested_nlt"] == eco.to_nlt(paid - vested)
    too_much = bob.create_transaction(alice.address, 10 * NLT + vested + fees + 1, nonce=1)
    assert not bc.validate_candidate_tx_against_chain_and_mempool(too_much, [])
    block = build_block(bc, [make_reward_tx(alice.address, bc.expected_reward(bc.chain)), too_much])
    assert not bc.add_block(block.to_dict())
    # mine to height 10 (payout half vested at the next block's height)
    while len(bc.chain) < 10:
        mine(bc, alice.address)
    half = paid - bc.unvested({"amount": paid, "vest_start": 5, "vest_end": 15}, 10)
    assert half == paid // 2
    fee = bc.econ.min_fee
    ok = bob.create_transaction(alice.address, 10 * NLT + half + fees - fee, nonce=1)
    over = bob.create_transaction(alice.address, 10 * NLT + half + fees - fee + 1, nonce=1)
    assert not bc.validate_candidate_tx_against_chain_and_mempool(over, [])
    assert bc.validate_candidate_tx_against_chain_and_mempool(ok, [])
    mine(bc, alice.address, [ok])
    assert bc.get_balance(bob.address) == paid - half


# --- allocations: community, treasury, reserve -------------------------------------------

def holder_econ(alice_addr, bucket, **kw):
    allocs = {"community": "c" * 32, "treasury": "d" * 32, "reserve": "e" * 32}
    allocs[bucket] = alice_addr
    return small_econ(allocations=allocs, **kw)


def test_community_pool_never_spendable_by_key(tmp_path, monkeypatch, wallets):
    alice, bob = wallets
    monkeypatch.chdir(tmp_path)
    bc = Blockchain(econ=holder_econ(alice.address, "community", spend_approval="single_key_dev"))
    tx = alice.create_transaction(bob.address, NLT, nonce=1)
    assert not bc.validate_candidate_tx_against_chain_and_mempool(tx, [])


def test_treasury_spend_rejected_without_approval(tmp_path, monkeypatch, wallets):
    alice, bob = wallets
    monkeypatch.chdir(tmp_path)
    bc = Blockchain(econ=holder_econ(alice.address, "treasury", spend_approval="none"))
    assert bc.econ.unlocked("treasury", 1) > 0       # unlocked, but no approval mechanism
    tx = alice.create_transaction(bob.address, NLT, nonce=1)
    assert not bc.validate_candidate_tx_against_chain_and_mempool(tx, [])
    block = build_block(bc, [make_reward_tx(bob.address, bc.expected_reward(bc.chain)), tx])
    assert not bc.add_block(block.to_dict())


def test_treasury_cannot_overspend_unlocked_amount(tmp_path, monkeypatch, wallets):
    alice, bob = wallets
    monkeypatch.chdir(tmp_path)
    bc = Blockchain(econ=holder_econ(alice.address, "treasury", spend_approval="single_key_dev"))
    unlocked = bc.econ.unlocked("treasury", 1)
    assert unlocked == 3_125_000 * NLT
    over = alice.create_transaction(bob.address, unlocked + 1, nonce=1)
    assert not bc.validate_candidate_tx_against_chain_and_mempool(over, [])
    block = build_block(bc, [make_reward_tx(bob.address, bc.expected_reward(bc.chain)), over])
    assert not bc.add_block(block.to_dict())
    ok = alice.create_transaction(bob.address, unlocked - bc.econ.min_fee, nonce=1)  # + fee = all unlocked
    mine(bc, bob.address, [ok])
    assert bc.get_balance(bob.address) == unlocked + 10 * NLT
    # next month's step unlocks more
    while len(bc.chain) <= 10:
        mine(bc, bob.address)
    more = alice.create_transaction(bob.address, unlocked - bc.econ.min_fee, nonce=2)
    assert bc.validate_candidate_tx_against_chain_and_mempool(more, [])


def test_reserve_cliff(tmp_path, monkeypatch, wallets):
    alice, bob = wallets
    monkeypatch.chdir(tmp_path)
    bc = Blockchain(econ=holder_econ(alice.address, "reserve", spend_approval="single_key_dev"))
    tx = alice.create_transaction(bob.address, NLT, nonce=1)
    assert not bc.validate_candidate_tx_against_chain_and_mempool(tx, [])   # before height 20
    while len(bc.chain) < 20:
        mine(bc, bob.address)
    assert bc.validate_candidate_tx_against_chain_and_mempool(tx, [])


# --- API / CLI -------------------------------------------------------------------------

def test_status_and_balance_show_locks_and_vesting(node, wallets):
    alice, _ = wallets
    bc = node.blockchain
    for _ in range(2):
        assert bc.add_block(bc.mine_block(alice.address, []).to_dict())
    with urllib.request.urlopen(f"{node.self_url()}/status") as r:
        s = json.loads(r.read())
    assert s["buckets"]["treasury"]["unlocked"] == "3125000"
    assert s["buckets"]["treasury"]["locked"] == "146875000"
    assert s["buckets"]["reserve"]["unlocked"] == "0"
    assert s["community_payouts"]["next_epoch_height"] == bc.econ.next_epoch_height(2)
    assert s["community_payouts"]["per_address_cap"] == "25000"
    assert s["community_payouts"]["treasury_reserve_spend_approval"] == "none"
    with urllib.request.urlopen(f"{node.self_url()}/balance?address={alice.address}") as r:
        b = json.loads(r.read())
    assert b["spendable_nlt"] == "20" and b["locked_or_unvested_nlt"] == "0"


def test_cli_economy(node, capsys):
    import netlithic as cli
    assert cli.main(["economy", "--node", node.self_url()]) == 0
    out = capsys.readouterr().out
    assert "treasury" in out and "Next community payout" in out and "approval: none" in out
