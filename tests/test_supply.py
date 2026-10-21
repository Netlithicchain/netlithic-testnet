import copy
import json
import urllib.request
from dataclasses import replace

import pytest

import economics as eco
from conftest import build_block
from core import Blockchain, make_reward_tx

NLT = eco.BASE_UNITS_PER_NLT


def mine(bc, addr, txs=()):
    assert bc.add_block(bc.mine_block(addr, list(txs)).to_dict()), bc.last_reject_reason


# --- constants & config -----------------------------------------------------------

def test_max_supply_and_decimals():
    assert eco.DECIMALS == 8
    assert eco.MAX_SUPPLY == 1_000_000_000 * 10 ** 8
    assert sum(eco.BUCKET_PERCENT.values()) == 100
    assert eco.MINER_BUCKET == 200_000_000 * NLT


REAL = {"treasury": "e26742c7bcdb96fa0ca9446073b04a35",
        "reserve": "65ca7858ba65ee7a27d8b2879197d973",
        "community": "f953a95407a5ae8bba986101d1f9b272"}


def test_repo_config_uses_real_addresses():
    e = eco.load_economics()
    assert set(e.placeholders) == set()
    assert e.allocations == REAL


@pytest.mark.parametrize("mutate, msg", [
    (lambda c: c.update(decimals=6), "decimals"),
    (lambda c: c.update(max_supply_nlt=2_000_000_000), "max_supply"),
    (lambda c: c["allocations"]["treasury"].update(percent=20), "allocations"),
    (lambda c: c["allocations"]["reserve"].update(placeholder=True), "placeholder"),
    (lambda c: [c["allocations"][b].update(placeholder=False, address="ab" * 16) for b in ("treasury", "reserve")], "distinct"),
    (lambda c: c["miner_rewards"].update(initial_reward_nlt=50), "schedule"),
])
def test_bad_config_rejected(tmp_path, mutate, msg):
    cfg = json.load(open(eco.CONFIG_PATH))
    mutate(cfg)
    p = tmp_path / "cfg.json"; p.write_text(json.dumps(cfg))
    with pytest.raises(ValueError, match=msg):
        eco.load_economics(str(p))


def test_nlt_formatting_and_parsing():
    assert eco.to_nlt(1_050_000_000) == "10.5"
    assert eco.to_nlt(eco.MAX_SUPPLY) == "1000000000"
    assert eco.parse_nlt("0.25") == 25_000_000
    for bad in ("0", "-1", "0.000000001", "abc"):
        with pytest.raises(ValueError):
            eco.parse_nlt(bad)


# --- genesis ------------------------------------------------------------------------

def test_genesis_has_locked_allocations_and_no_miner_premint(chain):
    g = [t for t in chain.chain[0].transactions if t["type"] == "allocation"]
    params = [t for t in chain.chain[0].transactions if t["type"] == "params"]
    assert params == [{"type": "params", "economics_sha256": chain.econ.params_hash()}]
    assert [(t["bucket"], t["amount"]) for t in g] == [
        ("community", 600_000_000 * NLT), ("treasury", 150_000_000 * NLT), ("reserve", 50_000_000 * NLT)]
    assert all(t["type"] == "allocation" and t["locked"] for t in g)
    assert chain.total_minted() == 800_000_000 * NLT
    assert chain.rewards_paid() == 0
    assert chain.get_balance(chain.econ.allocations["community"]) == 600_000_000 * NLT


def test_allocation_tx_outside_genesis_rejected(chain, wallets):
    alice, _ = wallets
    sneaky = {"type": "allocation", "bucket": "community", "recipient": alice.address,
              "amount": NLT, "locked": True}
    block = build_block(chain, [make_reward_tx(alice.address, chain.expected_reward(chain.chain)), sneaky])
    assert not chain.add_block(block.to_dict())


def test_chain_with_different_genesis_config_rejected(tmp_path, monkeypatch, wallets):
    alice, bob = wallets
    monkeypatch.chdir(tmp_path)
    other = replace(Blockchain().econ, allocations={"community": alice.address,
                                                    "treasury": bob.address, "reserve": "c" * 32})
    (tmp_path / "chain.json").unlink()
    foreign = Blockchain(econ=other)            # writes a chain with a different genesis
    data = foreign.to_list()
    with pytest.raises(RuntimeError):
        Blockchain()                             # node with repo config refuses to load it
    (tmp_path / "chain.json").unlink()
    assert not Blockchain().replace_chain(data + data[1:])  # and never switches to it


# --- reward schedule ----------------------------------------------------------------

def test_schedule_numbers():
    e = eco.load_economics()
    assert e.scheduled_reward(1) == 10 * NLT
    assert e.scheduled_reward(10_000_000) == 10 * NLT
    assert e.scheduled_reward(10_000_001) == 5 * NLT
    assert e.scheduled_reward(20_000_001) == 25 * NLT // 10
    total = sum((e.initial_reward >> era) * e.halving_interval for era in range(64))
    assert total <= e.miner_bucket
    assert e.miner_bucket - total < NLT * 2   # only rounding dust is never paid


def test_first_block_reward_is_10_nlt(chain, wallets):
    alice, _ = wallets
    mine(chain, alice.address)
    assert chain.get_balance(alice.address) == 10 * NLT


def test_wrong_reward_amount_rejected(chain, wallets):
    alice, _ = wallets
    block = build_block(chain, [make_reward_tx(alice.address, 11 * NLT)])
    assert not chain.add_block(block.to_dict())


def test_reward_stops_at_miner_bucket_cap(chain, wallets):
    alice, _ = wallets
    chain.econ = replace(chain.econ, miner_bucket=25 * NLT)
    for _ in range(4):
        mine(chain, alice.address)
    rewards = [b.transactions[0]["amount"] for b in chain.chain[1:]]
    assert rewards == [10 * NLT, 10 * NLT, 5 * NLT, 0]
    assert chain.rewards_paid() == 25 * NLT
    # a block that tries to pay itself anyway is rejected
    greedy = build_block(chain, [make_reward_tx(alice.address, 10 * NLT)])
    assert not chain.add_block(greedy.to_dict())
    assert chain.supply_info()["miner_rewards_remaining"] == "0"


def test_chain_minting_beyond_max_supply_rejected(chain, wallets):
    alice, _ = wallets
    # a misconfigured schedule that would mint 300M per block on top of 800M genesis
    chain.econ = replace(chain.econ, miner_bucket=eco.MAX_SUPPLY, initial_reward=300_000_000 * NLT)
    bad = list(chain.chain)
    bad.append(build_block(chain, [make_reward_tx(alice.address, 300_000_000 * NLT)], chain=bad))
    assert chain.is_valid_new_block(bad[1], bad[0], [])   # per-block rule alone would allow it
    assert not chain.is_valid_chain([b.to_dict() for b in bad])


# --- locked allocations -------------------------------------------------------------------

@pytest.fixture
def alice_holds_treasury(tmp_path, monkeypatch, wallets):
    alice, bob = wallets
    monkeypatch.chdir(tmp_path)
    econ = replace(eco.load_economics(),
                   allocations={"community": "c" * 32, "treasury": alice.address, "reserve": "e" * 32})
    return Blockchain(econ=econ), alice, bob


def test_locked_address_cannot_spend_in_block(alice_holds_treasury):
    bc, alice, bob = alice_holds_treasury
    assert bc.get_balance(alice.address) == 150_000_000 * NLT
    tx = alice.create_transaction(bob.address, NLT, nonce=1)
    block = build_block(bc, [make_reward_tx(bob.address, bc.expected_reward(bc.chain)), tx])
    assert not bc.add_block(block.to_dict())


def test_locked_address_cannot_enter_mempool_or_be_mined(alice_holds_treasury):
    bc, alice, bob = alice_holds_treasury
    tx = alice.create_transaction(bob.address, NLT, nonce=1)
    assert not bc.validate_candidate_tx_against_chain_and_mempool(tx, [])
    assert len(bc.mine_block(bob.address, [tx]).transactions) == 1


def test_anyone_can_send_to_locked_address(alice_holds_treasury):
    bc, alice, bob = alice_holds_treasury
    mine(bc, bob.address)
    tx = bob.create_transaction(alice.address, 2 * NLT, nonce=1)
    mine(bc, bob.address, [tx])
    assert bc.get_balance(alice.address) == 150_000_002 * NLT


# --- API / CLI ------------------------------------------------------------------------

def test_status_shows_supply(node, wallets):
    alice, _ = wallets
    mine(node.blockchain, alice.address)
    with urllib.request.urlopen(f"{node.self_url()}/status") as r:
        s = json.loads(r.read())["supply"]
    assert s["max_supply"] == "1000000000"
    assert s["minted"] == "800000010"
    assert s["remaining"] == "199999990"
    assert s["miner_rewards_remaining"] == "199999990"
    assert s["next_block_reward"] == "10"
    assert s["placeholder_addresses"] == []


def test_cli_send_fractional_amount(node, wallets, capsys):
    import netlithic as cli
    alice, bob = wallets
    mine(node.blockchain, alice.address)
    assert cli.main(["send", alice.address, bob.address, "0.25", "--node", node.self_url()]) == 0
    assert "Sent 0.25 NLT" in capsys.readouterr().out
    mine(node.blockchain, alice.address, list(node.mempool))
    assert node.blockchain.get_balance(bob.address) == 25_000_000
