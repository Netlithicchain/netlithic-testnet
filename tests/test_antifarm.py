"""Anti-farming rules for transfer points (proposals): minimum amount,
no self-transfers, and a per sender->recipient pair cap per epoch."""
import pytest

import contributions as c
import economics as eco

NLT = eco.BASE_UNITS_PER_NLT
A, B, C = "a" * 32, "b" * 32, "c" * 32


def tx(sender, recipient, amount):
    return {"payload": {"sender": sender, "recipient": recipient, "amount": amount, "nonce": 1}}


def block(index, txs, miner=C):
    return {"index": index, "transactions": [{"type": "reward", "recipient": miner}] + txs}


def pts(blocks, **kw):
    return c.points_from_blocks(blocks, **kw)


def test_below_minimum_earns_nothing_but_is_counted():
    u = pts([block(1, [tx(A, B, NLT - 1)])])[A]
    assert u["cp_tenths"] == 0 and u["tx_count"] == 1 and u["point_tx_count"] == 0


def test_minimum_amount_earns_point():
    assert pts([block(1, [tx(A, B, NLT)])])[A]["cp_tenths"] == 10


def test_self_transfer_earns_nothing():
    assert pts([block(1, [tx(A, A, 50 * NLT)])])[A]["cp_tenths"] == 0


def test_pair_cap_per_epoch():
    blocks = [block(i, [tx(A, B, NLT)]) for i in range(1, 16)]
    u = pts(blocks, epoch_blocks=1000)[A]
    assert u["point_tx_count"] == eco.PAIR_CAP_PER_EPOCH == 10
    assert u["cp_tenths"] == 100 and u["tx_count"] == 15


def test_cap_is_per_pair_not_per_sender():
    blocks = [block(i, [tx(A, B, NLT), tx(A, C, NLT)]) for i in range(1, 13)]
    assert pts(blocks, epoch_blocks=1000)[A]["point_tx_count"] == 20


def test_cap_resets_next_epoch():
    blocks = [block(i, [tx(A, B, NLT)]) for i in range(1, 25)]  # epochs of 12 blocks
    # blocks 1..11 in epoch 0 (10 count), 12..23 epoch 1 (10 count), 24 epoch 2 (1)
    assert pts(blocks, epoch_blocks=12)[A]["point_tx_count"] == 21


def test_custom_limits_and_determinism():
    blocks = [block(i, [tx(A, B, 5)]) for i in range(1, 6)]
    r1 = pts(blocks, min_transfer=5, pair_cap=2, epoch_blocks=100)
    r2 = pts(list(blocks), min_transfer=5, pair_cap=2, epoch_blocks=100)
    assert r1 == r2 and r1[A]["point_tx_count"] == 2


def test_config_parsing_and_validation(tmp_path):
    import json, os
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "config", "genesis_config.json")
    cfg = json.load(open(src))
    e = eco.load_economics(src)
    assert e.min_point_transfer == NLT and e.point_pair_cap == 10
    cfg["contribution_points"]["min_transfer_base_units"] = 0
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(cfg))
    with pytest.raises(ValueError):
        eco.load_economics(str(bad))


def test_chain_uses_rules(chain, wallets):
    alice, bob = wallets
    assert chain.add_block(chain.mine_block(alice.address, []).to_dict())
    small = alice.create_transaction(bob.address, 1, nonce=1)
    assert chain.add_block(chain.mine_block(bob.address, [small]).to_dict())
    assert chain.contribution_points()[alice.address]["cp_tenths"] == 0
    big = alice.create_transaction(bob.address, NLT, nonce=2)
    assert chain.add_block(chain.mine_block(bob.address, [big]).to_dict())
    assert chain.contribution_points()[alice.address]["cp_tenths"] == 10
