"""The simulator's v3-vs-v4 comparison runs and says what the docs say (numbers come from the real functions)."""
import importlib.util
import os

import contributions as c

_p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops", "sim", "points_sim.py")
_spec = importlib.util.spec_from_file_location("points_sim_under_test", _p)
sim = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sim)


def test_v4_beats_v3_on_the_ring_in_absolute_terms_but_does_not_stop_it():
    text, r = sim.v4_tables()
    ring = "400-fake ring funded by one hub, 20 honest users"
    v3, v4 = r[(ring, "v3")], r[(ring, "v4")]
    assert v4["fakes"] < v3["fakes"]
    assert v4["fakes"] > 0                                             # honest statement: the ring still gets paid
    assert v4["fakes"] <= 0.2 * sim.BUDGET / sim.NLT + 1                # never more than the 20% transfer pot
    assert v4["paid"] < v3["paid"]                                      # the rest stays in the pool
    assert "Part 3" in text


def test_family_rule_is_the_only_thing_that_zeroes_the_hub_ring_and_it_misses_mining_born_fakes():
    _, r = sim.v4_tables()
    hub = "400-fake ring funded by one hub, 20 honest users"
    mined = "400 mining-born fakes (each mined 1 block), 20 honest users"
    assert r[(hub, "v3fam")]["fakes"] == 0
    assert r[(mined, "v3fam")]["fakes"] > 0


def test_v4_never_pays_more_than_the_budget_and_miners_are_held_at_the_cap():
    _, r = sim.v4_tables()
    assert all(v["paid"] <= sim.BUDGET / sim.NLT + 1e-6 for v in r.values())
    cap = (sim.BUDGET * (100 - c.TX_POT_PERCENT) // 100) * c.POT_CAP_PERCENT // 100 / sim.NLT
    assert r[("no attack: 20 honest users", "v4")]["miners"] <= 3 * cap + 1e-6


# ---- part 4: v4 against v4 + registered contributors ----
A_RING = "A. 400-fake ring (hub-funded); 20 honest users, all listed"
B_MINED = "B. 400 mining-born fakes (mined before the window); 20 honest listed"
D_TEN = "D. no attack: 20 honest users, only 10 listed (ten testers)"
E_EMPTY = "E. no attack: 20 honest users, NOBODY listed (empty list)"
F_HASH = "F. ring + fakes mine 10% of the window's blocks (144 blocks, 1 each); honest listed"
G_ONE = "G. same 10% hash power on ONE address (no splitting); ring unlisted"
H_TEN = "H. ring of 400, but 10 of the fakes got listed; 20 honest listed"
I_FIFTY = "I. ring of 400, but 50 of the fakes got listed; 20 honest listed"


def test_registry_zeroes_the_unlisted_ring_and_the_mining_born_fakes_without_hurting_listed_honest_users():
    text, r = sim.reg_tables()
    assert "Part 4" in text and "TEMPORARY" in text
    for w in (A_RING, B_MINED):
        assert r[(w, "v4")]["fakes"] > 4000                        # the open v4 pot pays the ring ~4,089 NLT
        assert r[(w, "v4reg")]["fakes"] == 0                        # the registry pays it nothing
        assert r[(w, "v4reg")]["honest"] > 50 * r[(w, "v4")]["honest"]
    no_attack = "C. no attack: 20 honest users, all listed"
    assert r[(no_attack, "v4reg")] == r[(no_attack, "v4")]          # nobody honest loses when all of them are listed


def test_registry_costs_unlisted_honest_users_their_transfer_points_and_empty_list_pays_no_transfers():
    _, r = sim.reg_tables()
    assert r[(D_TEN, "v4reg")]["honest"] * 2 == r[(D_TEN, "v4")]["honest"]    # 10 of 20 listed -> half
    assert r[(E_EMPTY, "v4reg")]["honest"] == 0                               # fails safe, mining still paid
    assert r[(E_EMPTY, "v4reg")]["miners"] == r[(E_EMPTY, "v4")]["miners"] > 0


def test_what_the_registry_does_not_fix_mining_pot_and_bad_registrations():
    _, r = sim.reg_tables()
    pot = sim.BUDGET * (100 - c.TX_POT_PERCENT) // 100 / sim.NLT
    # fakes that really mine 10% of the blocks still get ~10% of the mining pot (open by design)
    assert abs(r[(F_HASH, "v4reg")]["fakes"] - 0.10 * pot) < 0.01 * pot
    assert r[(G_ONE, "v4reg")]["fakes"] < r[(F_HASH, "v4reg")]["fakes"]       # one address hits the per-address limit
    # bad registrations: 50 listed fakes (the per-period maximum) take nearly all of what the open pot paid them
    assert r[(I_FIFTY, "v4reg")]["fakes"] > 0.9 * r[(I_FIFTY, "v4")]["fakes"]
    assert 0 < r[(H_TEN, "v4reg")]["fakes"] < r[(I_FIFTY, "v4reg")]["fakes"]
    assert all(v["paid"] <= sim.BUDGET / sim.NLT + 1e-6 for v in r.values())
