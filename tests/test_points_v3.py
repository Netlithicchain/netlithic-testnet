"""Points v3 (POINTS_V4_HEIGHT): funding families, slow release, two pots.

Unit tests use the real shipped constants on hand-built blocks; the consensus test mines real
blocks with scaled constants and checks that v3 payouts validate end to end.
"""
import hashlib
from dataclasses import replace

import pytest

import contributions as c
import core
import economics as eco
import pause  # noqa: F401  (imported so monkeypatched forks behave as in the other fork tests)
from core import Blockchain

NLT = eco.BASE_UNITS_PER_NLT
E = eco.load_economics()
FORK, PERIOD = 5000, 1440
MINER = "ff" * 16


def A(i):
    return hashlib.sha256(f"v3-{i}".encode()).hexdigest()[:32]


def xfer(s, r, amt=NLT):
    return {"payload": {"sender": s, "recipient": r, "amount": amt, "nonce": 1, "fee": 10_000}}


def rew(a):
    return {"type": "reward", "recipient": a, "amount": 0, "fees": 0}


def blk(i, *txs, miner=MINER):
    return {"index": i, "transactions": [rew(miner), *txs]}


def pts(blocks, v3=True, issuers=(), history=None):
    hist = list(history if history is not None else blocks)
    return c.points_from_blocks(blocks, E.points_per_block, E.points_per_tx, E.min_point_transfer,
                                E.point_pair_cap, E.epoch_blocks, epoch_fn=E.epoch_id, rules_height=FORK,
                                first_seen=c.first_seen_heights(hist), v3=v3,
                                roots=c.family_roots(hist, issuers=issuers) if v3 else None)


# ---- activation ----

def test_fork_height_is_before_the_first_payout_with_room_for_notice():
    # Review L1: same height as the security fork, so the live leaderboard (which switches at POINTS_V4_HEIGHT)
    # shows the rules that pay block 6440, and no payout can fall between the two heights.
    assert core.POINTS_V4_HEIGHT == core.SECURITY_FORK_HEIGHT == FORK
    assert core.POINTS_V4_HEIGHT < FORK + PERIOD                                   # first epoch block (6440) is after the fork; first payout is 7880 (v4 lag)
    assert not core.is_points_v3(4999) and core.is_points_v3(5000)


# ---- family_roots ----

def test_chain_reward_and_issuer_funding_start_a_family_transfer_funding_joins_one():
    hub, kid, grand, faucet, drip = A(1), A(2), A(3), A(4), A(5)
    h = [blk(1, miner=hub), blk(2, xfer(hub, kid)), blk(3, xfer(kid, grand)), blk(4, miner=faucet), blk(5, xfer(faucet, drip))]
    r = c.family_roots(h)
    assert r[hub] == hub and r[kid] == hub and r[grand] == hub                    # grandchild is in the hub's family
    assert r[drip] == faucet                                                      # no issuers configured: joins the faucet's
    assert c.family_roots(h, issuers=(faucet,))[drip] == drip                     # configured issuer: a family of its own


def test_only_the_first_real_funding_decides_and_dust_never_does():
    a, b, c_, d = A(1), A(2), A(3), A(4)
    h = [blk(1, miner=a), blk(2, miner=b), blk(3, xfer(a, c_, NLT - 1)), blk(4, xfer(b, c_)), blk(5, xfer(a, c_, 9 * NLT))]
    r = c.family_roots(h)
    assert r[c_] == b                                                              # dust from a was ignored, later gifts too
    assert d not in r


def test_funding_from_a_sender_with_no_family_does_not_root_the_receiver():
    """Review H1: a sender that never had a traceable funding cannot found a family for anyone."""
    r = c.family_roots([blk(1, xfer(A(1), A(2)))])
    assert A(1) not in r and A(2) not in r


# ---- the family rule on points ----

def test_same_family_transfers_earn_nothing_and_do_not_use_up_the_caps():
    hub, f1, f2, friend = A(1), A(2), A(3), A(4)
    hist = [blk(1, miner=hub), blk(2, miner=friend), blk(3, xfer(hub, f1), xfer(hub, f2))]
    live = [blk(FORK + i, xfer(f1, f2)) for i in range(40)] + [blk(FORK + 50, xfer(f1, friend))]
    u = pts(hist + live, history=hist)
    assert u[f1]["point_tx_count"] == 1 and u[f1]["tx_count"] == 41                # only the transfer to the outsider counts
    assert pts(hist + live, v3=False)[f1]["point_tx_count"] == 11                  # v2: 10 (pair cap) + 1, the old behaviour


def test_hub_funded_ring_of_any_size_earns_zero_but_a_real_customer_earns():
    hub, shop = A(1), A(2)
    ring = [A(100 + i) for i in range(60)]
    hist = [blk(1, miner=hub), blk(2, miner=shop), blk(3, *[xfer(hub, a) for a in ring])]
    live = [blk(FORK + j, xfer(ring[i], ring[(i + o) % 60]))
            for j, (o, i) in enumerate((o, i) for o in (1, 2, 3) for i in range(60))]
    live.append(blk(FORK + 300, xfer(ring[0], shop)))
    u = pts(hist + live, history=hist)
    assert sum(u[a]["point_tx_count"] for a in ring) == 1                          # only ring[0] -> shop
    assert pts(hist + live, v3=False)[ring[5]]["point_tx_count"] == 3              # v2 gave every ring member points


def test_family_rule_is_off_without_the_v3_flag():
    a, b = A(1), A(2)
    hist = [blk(1, miner=a), blk(2, xfer(a, b))]
    live = [blk(FORK, xfer(b, a))]
    assert pts(hist + live, v3=False, history=hist)[b]["point_tx_count"] == 1
    assert pts(hist + live, v3=True, history=hist).get(b, {"point_tx_count": 0})["point_tx_count"] == 0


# ---- the payout split ----

def user(mined=0, empty=0, ptx=0):
    return {"blocks_mined": mined, "empty_blocks": empty, "point_tx_count": ptx}


def test_two_pots_pro_rata_and_unspent_budget_stays_in_the_pool():
    users = {"a": user(mined=10, ptx=0), "b": user(mined=30), "t": user(ptx=5), "u": user(ptx=15)}
    out = c.v3_amounts(users, 25, 10, budget=1000, tx_percent=30, cap=10 ** 9)
    assert out == {"a": 175, "b": 525, "t": 75, "u": 225}                          # 700 mining pot 1:3, 300 transfer pot 1:3
    assert c.v3_amounts({"a": user(mined=1)}, 25, 10, 1000, 30, 10 ** 9) == {"a": 700}   # empty transfer pot is not paid out
    assert c.v3_amounts({"t": user(ptx=1)}, 25, 10, 1000, 30, 10 ** 9) == {"t": 300}


def test_empty_blocks_do_not_count_cap_and_exclusion_apply_and_total_never_exceeds_budget():
    users = {"a": user(mined=10, empty=10), "b": user(mined=100), "c": user(ptx=3), "locked": user(mined=100, ptx=5)}
    out = c.v3_amounts(users, 25, 10, 1000, 30, cap=500, excluded={"locked"})
    assert "a" not in out and "locked" not in out
    assert out["b"] == 500                                                          # capped (700 would be due)
    assert sum(out.values()) <= 1000


def test_nothing_is_paid_without_points_and_order_is_sorted():
    assert c.v3_amounts({}, 25, 10, 1000, 30, 10 ** 9) == {}
    out = c.v3_amounts({k: user(mined=1) for k in "cab"}, 25, 10, 900, 30, 10 ** 9)
    assert list(out) == ["a", "b", "c"]


# ---- consensus: real mined blocks, scaled constants ----

def _econ():
    base = dict(blocks_per_month=10, epoch_blocks=100, vesting_epochs=2, chain_id="netlithic-dev-1",
                unlocks={"community": (0, 240), "treasury": (0, 48), "reserve": (20, 72)})
    return replace(eco.load_economics(), **base)


def test_v3_payout_blocks_validate_and_pay_only_the_slice(tmp_path, monkeypatch, wallets):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(eco.TESTNET_SHORT_EPOCHS, "netlithic-dev-1", (3, 4))
    monkeypatch.setattr(core, "SECURITY_FORK_HEIGHT", 0)
    monkeypatch.setattr(core, "POINTS_V4_HEIGHT", 0)
    monkeypatch.setattr(c, "MIN_ADDRESS_AGE_BLOCKS", 0)
    monkeypatch.setattr(Blockchain, "save_chain", lambda self: None)
    alice, bob = wallets
    bc = Blockchain(econ=_econ())

    def mine(addr, txs=()):
        assert bc.add_block(bc.mine_block(addr, list(txs)).to_dict()), bc.last_reject_reason

    mine(alice.address); mine(bob.address)                                          # 1, 2: own families
    for n in (1, 2, 3):
        mine(bob.address, [alice.create_transaction("ab" * 16, NLT, nonce=n)])      # 3..5: transfers to an outsider
    h = len(bc.chain)
    for n in (4, 5, 6):
        mine(bob.address, [alice.create_transaction("ab" * 16, NLT, nonce=n)])
    paid_height = next(i for i in range(h, h + 12) if bc.expected_payouts(bc.chain[:i], i))
    pays = bc.expected_payouts(bc.chain[:paid_height], paid_height)
    pool = bc.community_pool_available(bc.chain[:paid_height], paid_height)
    assert pays and sum(p["amount"] for p in pays) <= pool * bc.econ.epoch_length_at(paid_height) // bc.econ.blocks_per_month
    assert bc.is_valid_chain(bc.to_list())
