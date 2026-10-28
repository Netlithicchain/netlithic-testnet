"""Points v4: 20/80 pots, 2% per-address cap, one-day payout lag, family rule off, one fork height.
Real constants on synthetic chains (like tests/test_pause_drill.py) plus scaled constants on really mined blocks."""
import json
import urllib.request
from dataclasses import replace

import pytest

import contributions as c
import core
import economics as eco
import pause
from core import Block, Blockchain
from test_pause_drill import MINER_A, MINER_B, real_bc, set_pause_key, synth_chain, throwaway_key  # noqa: F401

NLT = eco.BASE_UNITS_PER_NLT
FORK, P = 5000, 1440


def user(mined=0, empty=0, ptx=0):
    return {"blocks_mined": mined, "empty_blocks": empty, "point_tx_count": ptx}


# ---- constants and the rules fingerprint ----

def test_shipped_v4_constants():
    assert c.TX_POT_PERCENT == 20 and 100 - c.TX_POT_PERCENT == 80
    assert c.POT_CAP_PERCENT == 2 and c.PAYOUT_LAG_PERIODS == 1
    assert c.FAMILY_RULE_ENABLED is False and c.FAMILY_PAIR_CAP is None and c.MAX_BASE_PER_POINT is None
    assert core.POINTS_V4_HEIGHT == core.SECURITY_FORK_HEIGHT == FORK            # one announced fork height
    assert core.is_points_v4(5000) and not core.is_points_v4(4999) and core.is_points_v3 is core.is_points_v4


def test_status_and_handshake_fingerprint_cover_the_new_constants(monkeypatch):
    base = core.points_v3_rules_hash()
    for attr, val in (("TX_POT_PERCENT", 30), ("POT_CAP_PERCENT", 5), ("POT_CAP_PERCENT", None), ("PAYOUT_LAG_PERIODS", 0)):
        with monkeypatch.context() as m:
            m.setattr(c, attr, val)
            assert core.points_v3_rules_hash() != base, attr


def test_status_endpoint_has_points_rules_and_payouts_endpoint_describes_v4(node):
    with urllib.request.urlopen(f"{node.self_url()}/status") as r:
        st = json.loads(r.read())
    assert st["points_rules"] == st["points_v3_rules"] == core.points_v3_rules_hash()
    with urllib.request.urlopen(f"{node.self_url()}/payout_preview") as r:
        assert json.loads(r.read())["status"] == "none"                          # nothing has ended on a new chain
    with urllib.request.urlopen(f"{node.self_url()}/payout_preview?period=1") as r:
        assert json.loads(r.read())["status"] == "period_not_finished"
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(f"{node.self_url()}/payout_preview?period=abc")
    assert e.value.code == 400


# ---- 20 / 80 pots ----

def test_pots_are_20_percent_transfers_and_80_percent_mining():
    users = {"m": user(mined=10), "t": user(ptx=5)}
    out = c.v3_amounts(users, 25, 10, 1000, c.TX_POT_PERCENT, 10 ** 9)
    assert out == {"m": 800, "t": 200}


# ---- 2% per-address cap ----

def test_two_percent_cap_applies_to_each_pot_separately():
    users = {"m": user(mined=1000, ptx=1000), "x": user(mined=1), "y": user(ptx=1)}
    out = c.v3_amounts(users, 25, 10, 100_000, 20, 10 ** 9, pot_cap_percent=2)
    assert out["m"] == 1600 + 400                      # 2% of the 80,000 mining pot + 2% of the 20,000 transfer pot
    assert out["x"] < 1600 and out["y"] < 400          # small users are not touched by the cap


def test_cap_holds_back_coins_that_stay_in_the_pool_and_are_not_re_shared():
    users = {"a": user(mined=100), "b": user(mined=100)}
    out = c.v3_amounts(users, 25, 10, 10_000, 20, 10 ** 9, pot_cap_percent=2)
    assert out == {"a": 160, "b": 160}                 # 8,000 mining pot, 2% = 160 each; 7,680 stays in the pool
    assert sum(out.values()) == 320


def test_cap_rounds_down_and_never_overspends():
    for budget in (0, 1, 49, 50, 51, 99, 100, 101, 12_345_678_901):
        users = {chr(97 + i): user(mined=i + 1, ptx=(i * 7) % 5) for i in range(60)}
        out = c.v3_amounts(users, 25, 10, budget, 20, 10 ** 12, pot_cap_percent=2)
        mine_cap = (budget - budget * 20 // 100) * 2 // 100
        tx_cap = budget * 20 // 100 * 2 // 100
        assert sum(out.values()) <= budget
        assert all(0 < v <= mine_cap + tx_cap for v in out.values())


def test_tiny_budget_where_the_cap_rounds_to_zero_pays_nothing_not_dust_to_one_address():
    assert c.v3_amounts({"a": user(mined=5)}, 25, 10, 49, 20, 10 ** 9, pot_cap_percent=2) == {}


def test_cap_off_reproduces_v3_amounts():
    users = {"a": user(mined=7, ptx=3), "b": user(mined=1), "c": user(ptx=9)}
    assert c.v3_amounts(users, 25, 10, 5000, 30, 10 ** 9) == c.v3_amounts(users, 25, 10, 5000, 30, 10 ** 9, pot_cap_percent=None)


def test_old_25000_cap_is_a_backstop_that_the_two_percent_cap_never_reaches_at_real_sizes():
    e = eco.load_economics()
    budget = 2_500_000 * NLT * P // e.blocks_per_month
    most_one_address_can_get = budget * 2 // 100          # (80% + 20%) x 2%
    assert most_one_address_can_get < e.payout_cap // 50 and e.payout_cap == 25_000 * NLT


# ---- payout lag, real constants ----

def _real_period_one(bc, tr, upto=7879, miner=lambda i: MINER_A):
    return synth_chain(bc, upto, tr, miner)


def test_nothing_is_paid_at_the_first_epoch_block_and_period_one_is_paid_at_7880(real_bc):
    bc, tr = real_bc
    _real_period_one(bc, tr)
    assert [h for h in range(5000, 7881) if bc.econ.is_epoch_block(h)] == [6440, 7880]
    assert bc.expected_payouts(bc.chain[:6440], 6440) == []
    pays = bc.expected_payouts(bc.chain, 7880)
    assert {p["recipient"] for p in pays} == {MINER_A}
    assert all(p["vest_start"] == 7880 for p in pays)
    for h in range(5001, 7880):
        if not bc.econ.is_epoch_block(h):
            assert bc.expected_payouts(bc.chain[:h], h) == []


def test_boundary_blocks_6439_counts_and_6440_does_not_for_period_one(real_bc):
    bc, tr = real_bc
    _real_period_one(bc, tr, miner=lambda i: MINER_A if i < 6439 else MINER_B)
    # block 6439 mined by B is in the window [5000, 6440): B is paid; a block at 6440 or later is not in it
    paid = {p["recipient"] for p in bc.expected_payouts(bc.chain, 7880)}
    assert paid == {MINER_A, MINER_B}
    _real_period_one(bc, tr, miner=lambda i: MINER_A if i < 6440 else MINER_B)
    assert {p["recipient"] for p in bc.expected_payouts(bc.chain, 7880)} == {MINER_A}


def test_period_two_is_paid_at_9320_and_uses_its_own_window(real_bc):
    bc, tr = real_bc
    synth_chain(bc, 9319, tr, lambda i: MINER_A if i < 6440 else MINER_B)
    assert {p["recipient"] for p in bc.expected_payouts(bc.chain, 7880)} == {MINER_A}
    assert {p["recipient"] for p in bc.expected_payouts(bc.chain, 9320)} == {MINER_B}


def test_the_payout_list_is_already_fixed_longer_than_the_reorg_limit_before_it_is_paid(real_bc):
    """Anything a legal reorg (<= 720 blocks) could change at payout time is outside the window and the budget."""
    bc, tr = real_bc
    _real_period_one(bc, tr)
    honest = bc.expected_payouts(bc.chain, 7880)
    keep = 7880 - core.MAX_REORG_DEPTH
    assert keep > 6440 + 1                                   # newest block the list reads is 6440
    alt = synth_chain(bc, 7879, tr, lambda i: MINER_A if i < keep else MINER_B)
    assert bc.expected_payouts(alt, 7880) == honest


def test_a_pause_at_the_payout_block_skips_that_period_and_nothing_is_back_paid(real_bc, monkeypatch):
    bc, tr = real_bc
    k = throwaway_key()
    set_pause_key(monkeypatch, k)
    ptx = pause.make_pause_tx(k, True, "test", 1, bc.econ.chain_id)
    synth_chain(bc, 9319, tr, lambda i: MINER_A if i < 6440 else MINER_B, pause_at={7000: ptx})
    assert bc.expected_payouts(bc.chain[:7880], 7880) == []                    # period 1 skipped
    assert bc.expected_payouts(bc.chain[:9320], 9320) == []                    # still paused (10,080 blocks)
    after = 7000 + pause.PAUSE_EXPIRY_BLOCKS
    nxt = next(h for h in range(after + 1, after + 3000) if bc.econ.is_epoch_block(h))
    synth_chain(bc, nxt - 1, tr, lambda i: MINER_B, pause_at={7000: ptx})
    pays = bc.expected_payouts(bc.chain, nxt)
    assert {p["recipient"] for p in pays} == {MINER_B}                         # only that one period, no catch-up
    pool = bc.community_pool_available(bc.chain[:nxt - P + 1], nxt - P + 1)
    assert sum(p["amount"] for p in pays) <= pool * P // bc.econ.blocks_per_month   # still the slow, one-period slice


def test_pause_that_ends_before_the_payout_block_does_not_stop_it(real_bc, monkeypatch):
    """The pause is checked at the paying block (X), not at the end of the period."""
    bc, tr = real_bc
    k = throwaway_key()
    set_pause_key(monkeypatch, k)
    on = pause.make_pause_tx(k, True, "on", 1, bc.econ.chain_id)
    off = pause.make_pause_tx(k, False, "off", 2, bc.econ.chain_id)
    synth_chain(bc, 7879, tr, lambda i: MINER_A, pause_at={6500: on, 7000: off})
    assert bc.expected_payouts(bc.chain, 7880)


def test_payout_preview_matches_expected_payouts_and_reports_status(real_bc):
    bc, tr = real_bc
    synth_chain(bc, 6000, tr, lambda i: MINER_A)
    assert bc.payout_preview(1)["status"] == "period_not_finished"
    synth_chain(bc, 7000, tr, lambda i: MINER_A)
    pv = bc.payout_preview(1)
    assert pv["status"] == "proposed" and pv["pays_at_height"] == 7880 and pv["window_start"] == 5000
    synth_chain(bc, 7879, tr, lambda i: MINER_A)
    pv = bc.payout_preview()                                    # default: latest finished period = 1
    assert pv["period"] == 1
    exp = {p["recipient"]: p["amount"] for p in bc.expected_payouts(bc.chain, 7880)}
    assert {p["address"]: p["amount_base_units"] for p in pv["payouts"]} == exp
    assert pv["pots"]["per_address_cap_percent"] == 2 and pv["pots"]["family_rule"] is False
    assert bc.payout_preview(0)["status"] == "none"


def test_payouts_endpoint_describes_the_v4_rules(real_bc):
    bc, tr = real_bc
    synth_chain(bc, 5800, tr, lambda i: MINER_A)
    info = bc.payout_info()
    assert info["next_payout_pays_window"] is None            # block 6440 is an epoch block but pays nothing (lag)
    synth_chain(bc, 7000, tr, lambda i: MINER_A)
    info = bc.payout_info()
    note = info["points_rules"].pop("mining_pot_note")
    assert "block share" in note and "not protection for the pool" in note
    assert info["points_rules"] == {"payout_lag_blocks": 1440, "transfer_pot_percent": 20, "mining_pot_percent": 80,
                                    "per_address_cap_percent_of_each_pot": 2, "family_rule": False}
    assert info["next_payout_pays_window"] == {"start": 5000, "end": 6440}      # block 7880 pays period 1


# ---- consensus with really mined blocks, scaled constants (fork 3, period 4, first paying block 11) ----

def _econ():
    base = dict(blocks_per_month=10, epoch_blocks=100, vesting_epochs=2, chain_id="netlithic-dev-1",
                unlocks={"community": (0, 240), "treasury": (0, 48), "reserve": (20, 72)})
    return replace(eco.load_economics(), **base)


@pytest.fixture
def small(tmp_path, monkeypatch, wallets):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(eco.TESTNET_SHORT_EPOCHS, "netlithic-dev-1", (3, 4))
    monkeypatch.setattr(core, "SECURITY_FORK_HEIGHT", 3)
    monkeypatch.setattr(core, "POINTS_V4_HEIGHT", 3)
    monkeypatch.setattr(c, "MIN_ADDRESS_AGE_BLOCKS", 0)
    monkeypatch.setattr(Blockchain, "save_chain", lambda self: None)
    alice, bob = wallets
    bc = Blockchain(econ=_econ())

    def mine(addr, txs=()):
        b = bc.mine_block(addr, list(txs))
        assert bc.add_block(b.to_dict()), bc.last_reject_reason
        return b
    return bc, mine, alice, bob


def test_mined_chain_pays_nothing_at_7_and_period_one_at_11(small):
    bc, mine, alice, bob = small
    mine(alice.address); mine(bob.address)
    for n in range(1, 14):
        mine(bob.address, [alice.create_transaction("ab" * 16, NLT, nonce=n)])
    pays = lambda i: [t for t in bc.chain[i].transactions if t.get("type") == "payout"]
    assert not pays(7)                                          # first epoch block: lag, nothing yet
    assert pays(11)                                             # period 1 = blocks [3, 7)
    assert {t["recipient"] for t in pays(11)} <= {alice.address, bob.address, "ab" * 16}
    assert all(t["vest_start"] == 11 for t in pays(11))
    assert pays(15)                                             # period 2
    assert bc.is_valid_chain(bc.to_list())


def test_block_with_a_missing_or_early_payout_is_rejected(small):
    bc, mine, alice, bob = small
    from conftest import build_block
    mine(alice.address); mine(bob.address)
    for n in range(1, 9):
        mine(bob.address, [alice.create_transaction("ab" * 16, NLT, nonce=n)])
    assert len(bc.chain) == 11                                  # next block is 11, the first paying block
    exp = bc.expected_payouts(bc.chain, 11)
    assert exp
    reward = core.make_reward_tx(bob.address, bc.expected_reward(bc.chain))
    assert not bc.add_block(build_block(bc, [reward]).to_dict())                       # payouts missing
    assert not bc.add_block(build_block(bc, [reward, dict(exp[0], amount=exp[0]["amount"] + 1), *exp[1:]]).to_dict())
    ok = bc.mine_block(bob.address, [])
    assert ok.index == 11 and [t for t in ok.transactions if t.get("type") == "payout"] == exp
    assert bc.add_block(ok.to_dict())
    # block 7 (old v3 timing: pay the period that just ended) must not carry payouts either
    early = bc.expected_payouts(bc.chain[:7], 7)
    assert early == []


def test_small_legal_reorg_cannot_change_the_list_paid_at_11(small, monkeypatch):
    bc, mine, alice, bob = small
    mine(alice.address); mine(bob.address)
    for n in range(1, 9):
        mine(bob.address, [alice.create_transaction("ab" * 16, NLT, nonce=n)])
    monkeypatch.setattr(core, "MAX_REORG_DEPTH", 3)
    prior = list(bc.chain[:11])
    honest = bc.expected_payouts(prior, 11)
    assert honest
    alt = prior[:-3]
    for b in prior[-3:]:                                         # blocks 8, 9, 10 rewritten by someone else
        alt.append(Block(index=b.index, timestamp=b.timestamp, transactions=[core.make_reward_tx("cd" * 16, 1)],
                         previous_hash=alt[-1].hash, nonce=b.nonce, difficulty=b.difficulty,
                         hash=f"{b.index:064x}"))
    assert bc.expected_payouts(alt, 11) == honest                # window [3,7) and budget block 7 are older than 3 blocks
