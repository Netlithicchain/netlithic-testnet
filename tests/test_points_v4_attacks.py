"""Attack tests for points v4 (shipped settings: family rule OFF, 20/80 pots, 2% cap, one-day lag).

These pin what v4 does and does not stop. In plain words: v4 limits the damage of a ring of fake addresses to the
20% transfer pot and a small share per address; it does NOT stop the ring from taking that pot.
"""
import pytest

import contributions as c
import core
import economics as eco
import pause
from core import Block
from test_pause_drill import MINER_A, MINER_B, real_bc, set_pause_key, synth_chain, throwaway_key  # noqa: F401
from test_points_abuse import (FAUCET, NLT, PERIOD, addr, blk, bc, real_signed, sybil_world, synth,  # noqa: F401
                               tx_from_template, xfer)

E = eco.load_economics()
FORK = 5000


def _shares(bc, period=1):
    pv = bc.payout_preview(period)
    return pv, {p["address"]: p["amount_base_units"] for p in pv["payouts"]}


def _nlt(s):
    whole, _, frac = str(s).partition(".")
    return int(whole) * NLT + int((frac + "0" * 8)[:8])


# ---- ring attack ----

def test_ring_of_400_fakes_takes_at_most_the_transfer_pot_and_never_the_mining_pot(bc, wallets):
    sy, honest, miners = sybil_world(bc, wallets, n_sybil=400, n_honest=20)
    assert c.FAMILY_RULE_ENABLED is False                               # shipped setting
    pv, pays = _shares(bc)
    tx_pot = _nlt(pv["pots"]["transfer_pot_nlt"])
    ring = sum(pays.get(a, 0) for a in sy)
    assert 0 < ring <= tx_pot                                           # the ring DOES get paid: v4 does not stop it ...
    assert ring < 0.2 * _nlt(pv["pots"]["release_budget_nlt"]) + 1       # ... but it can never reach past 20% of a period
    assert max(pays[a] for a in sy) <= _nlt(pv["pots"]["per_address_cap_transfer_nlt"])
    # honest mining pot is untouched by the ring: miners get their mining share (capped at 2% each), honest users get theirs
    assert all(pays[a] > 0 for a in miners) and all(pays[a] > 0 for a in honest)
    assert sum(pays.values()) <= _nlt(pv["pots"]["release_budget_nlt"])


def test_ring_gets_nothing_if_the_family_rule_is_switched_on(bc, wallets, monkeypatch):
    """Best-effort rule, disabled in the shipped build. Shown here only so nobody thinks it was deleted."""
    monkeypatch.setattr(c, "FAMILY_ISSUERS", (FAUCET,))
    monkeypatch.setattr(c, "FAMILY_RULE_ENABLED", True)
    sy, honest, miners = sybil_world(bc, wallets, n_sybil=400, n_honest=20)
    _, pays = _shares(bc)
    assert sum(pays.get(a, 0) for a in sy) == 0


def test_the_ring_hits_the_per_address_cap_only_when_it_is_small(bc, wallets):
    """With few ring addresses each one earns more than 2% of the transfer pot and is held at the limit."""
    sy, honest, miners = sybil_world(bc, wallets, n_sybil=8, n_honest=0)
    pv, pays = _shares(bc)
    cap = _nlt(pv["pots"]["per_address_cap_transfer_nlt"])
    assert all(pays.get(a, 0) <= cap for a in sy)
    assert sum(pays.get(a, 0) for a in sy) < 8 * cap + 1


# ---- dust staging ----

def test_dust_transfers_earn_no_points_and_dust_staging_stays_inside_the_same_limits():
    ring = [addr(i) for i in range(400)]
    seen = {a: 1 for a in ring}                                          # staged with dust long before
    dust = [blk(FORK + i, MINER, [xfer(ring[i], ring[(i + 1) % 400], E.min_point_transfer - 1)]) for i in range(400)]
    users = c.points_from_blocks(dust, E.points_per_block, E.points_per_tx, E.min_point_transfer, E.point_pair_cap,
                                 E.epoch_blocks, epoch_fn=E.epoch_id, rules_height=FORK, first_seen=seen)
    assert all(u["point_tx_count"] == 0 for a, u in users.items() if a in set(ring))
    real = [blk(FORK + i, MINER, [xfer(ring[i], ring[(i + 1) % 400], E.min_point_transfer)]) for i in range(400)]
    users = c.points_from_blocks(real, E.points_per_block, E.points_per_tx, E.min_point_transfer, E.point_pair_cap,
                                 E.epoch_blocks, epoch_fn=E.epoch_id, rules_height=FORK, first_seen=seen)
    budget = 20_000 * NLT
    out = c.v3_amounts(users, E.points_per_block, E.points_per_tx, budget, c.TX_POT_PERCENT, E.payout_cap,
                       pot_cap_percent=c.POT_CAP_PERCENT)
    ring_total = sum(v for a, v in out.items() if a in set(ring))
    assert 0 < ring_total <= budget * c.TX_POT_PERCENT // 100             # dust staging buys no extra pot
    assert max(v for a, v in out.items() if a in set(ring)) <= budget * 20 // 100 * 2 // 100


# ---- paused period ----

def test_attack_period_that_falls_in_a_pause_is_not_paid_later(real_bc, monkeypatch):
    bc, tr = real_bc
    k = throwaway_key()
    set_pause_key(monkeypatch, k)
    ptx = pause.make_pause_tx(k, True, "attack seen", 1, bc.econ.chain_id)
    # attacker (MINER_B) is active only in period 1; pause is mined before the paying block 7880 and lasts 10,080 blocks
    synth_chain(bc, 20_000, tr, lambda i: MINER_B if i < 6440 else MINER_A, pause_at={7500: ptx})
    assert bc.expected_payouts(bc.chain[:7880], 7880) == []
    expires = 7500 + pause.PAUSE_EXPIRY_BLOCKS
    later = [h for h in range(expires + 1, 20_000) if bc.econ.is_epoch_block(h)]
    for h in later:
        got = {p["recipient"] for p in bc.expected_payouts(bc.chain[:h], h)}
        assert MINER_B not in got                                         # no catch-up for the skipped period


# ---- payout lag boundary ----

def test_points_in_the_last_block_of_a_period_count_for_the_next_period_not_this_one(bc, wallets):
    tpl = real_signed(wallets)
    a, b = addr(1), addr(2)

    def txs(i):
        return [tx_from_template(tpl, a, b)] if i == 6440 else []          # 6440 = first block of period 2

    synth(bc, 9319, txs, lambda i: MINER)
    first = {p["address"] for p in bc.payout_preview(1)["payouts"]}
    assert a not in first                                                   # block 6440 belongs to period 2, not period 1


MINER = "ff" * 16


# ---- cap rounding ----

@pytest.mark.parametrize("budget", [2_054_794_520_547, 2_054_794_520_548, 2_054_794_520_549, 99, 100, 4_999])
def test_cap_is_floor_of_two_percent_per_pot_and_the_sum_never_exceeds_the_budget(budget):
    users = {addr(i): {"blocks_mined": 1 + i, "empty_blocks": 0, "point_tx_count": i % 7} for i in range(80)}
    out = c.v3_amounts(users, E.points_per_block, E.points_per_tx, budget, 20, E.payout_cap, pot_cap_percent=2)
    tx_budget = budget * 20 // 100
    mine_budget = budget - tx_budget
    assert sum(out.values()) <= budget
    assert all(v <= mine_budget * 2 // 100 + tx_budget * 2 // 100 for v in out.values())


# ---- self-stuffing: deliberately NOT fixed ----

def test_self_stuffing_between_own_addresses_still_earns_transfer_points_but_only_inside_the_20_percent_pot():
    me, mine2 = addr(7), addr(8)
    seen = {me: 1, mine2: 1}
    blocks = [blk(FORK + i, me, [xfer(me, mine2)]) for i in range(10)]
    u = c.points_from_blocks(blocks, E.points_per_block, E.points_per_tx, E.min_point_transfer, E.point_pair_cap,
                             E.epoch_blocks, epoch_fn=E.epoch_id, rules_height=FORK, first_seen=seen)
    assert u[me]["point_tx_count"] == 10                                  # the pair cap (10) is the only limit
    out = c.v3_amounts(u, E.points_per_block, E.points_per_tx, 1000, 20, 10 ** 9, pot_cap_percent=2)
    assert out[me] <= 1000 * 20 // 100 * 2 // 100 + 1000 * 80 // 100 * 2 // 100
