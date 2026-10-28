"""ops/verify_payouts.py: read-only payout checker. No network: the node is faked."""
import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops"))
import verify_payouts as vp  # noqa: E402
from test_points_abuse import bc, sybil_world  # noqa: E402,F401

NLT = 10 ** 8


def _preview(pays, budget="1000", mine_cap="10", tx_cap="2", status="proposed"):
    return {"status": status, "period": 1, "window_start": 5000, "window_end": 6440, "pays_at_height": 7880,
            "total_nlt": "0", "payouts": pays,
            "pots": {"release_budget_nlt": budget, "mining_pot_nlt": "800", "transfer_pot_nlt": "200",
                     "per_address_cap_percent": 2, "per_address_cap_mining_nlt": mine_cap,
                     "per_address_cap_transfer_nlt": tx_cap, "family_rule": False}}


def _p(addr, nlt, mine=0, tx=0):
    return {"address": addr, "amount_nlt": str(nlt), "amount_base_units": int(nlt * NLT),
            "mining_points": mine, "transfer_points": tx}


def test_clean_period_has_no_warnings():
    pays = [_p(f"a{i}", 5, mine=10) for i in range(40)]
    lines, warns = vp.analyse(_preview(pays))
    assert warns == [] or all("unspent" in w for w in warns)
    assert any("Nobody hit it" in l for l in lines)


def test_address_over_the_two_percent_limit_is_flagged_in_plain_words():
    pays = [_p(f"a{i}", 5) for i in range(40)] + [_p("greedy", 13)]
    _, warns = vp.analyse(_preview(pays))
    assert any("greedy" in w and "more than the 2% limit" in w for w in warns)


def test_a_few_addresses_taking_most_of_the_pot_is_flagged():
    pays = [_p(f"a{i}", 1) for i in range(30)] + [_p(f"big{i}", 12) for i in range(5)]
    _, warns = vp.analyse(_preview(pays))
    assert any("biggest addresses take" in w for w in warns)


def test_ring_look_is_flagged():
    pays = [_p(f"r{i}", 1, tx=30) for i in range(25)]
    _, warns = vp.analyse(_preview(pays))
    assert any("ring of fake addresses" in w for w in warns)


def test_node_that_disagrees_is_flagged_and_one_that_agrees_is_not():
    pays = [_p(f"a{i}", 5, mine=10) for i in range(40)]
    prev = _preview(pays)
    lines, warns = vp.analyse(prev, node_prev=copy.deepcopy(prev))
    assert any("matches this recomputation exactly" in l for l in lines)
    other = copy.deepcopy(prev)
    other["payouts"][3]["amount_base_units"] += 1
    _, warns = vp.analyse(prev, node_prev=other)
    assert any("DIFFER" in w for w in warns)


def test_different_rules_fingerprint_is_flagged():
    _, warns = vp.analyse(_preview([_p("a", 1)]), node_rules="x", local_rules="y")
    assert any("fingerprint" in w for w in warns)


def test_nothing_to_check_yet_is_not_an_error():
    lines, warns = vp.analyse({"status": "none", "error": "No finished period yet."})
    assert lines == ["No finished period yet."] and warns == []


def test_money_text_helpers_round_trip():
    assert vp._nlt_base("328.767123") == 32876712300 and vp._fmt(32876712300) == "328.767123"
    assert vp._nlt_base("5") == 5 * NLT and vp._fmt(5 * NLT) == "5"


def test_local_recomputation_matches_the_node_for_a_synthetic_world(bc, wallets):
    sybil_world(bc, wallets, n_sybil=30, n_honest=5)
    here = os.getcwd()
    prev, rules = vp.local_preview(bc.to_list(), 1)
    assert os.getcwd() == here                                           # no stray directory change
    assert prev["payouts"] == bc.payout_preview(1)["payouts"]
    assert prev["status"] == "proposed" and rules


def test_a_chain_from_another_network_is_refused():
    with pytest.raises(ValueError):
        vp.local_preview([{"hash": "00" * 32}], 1)


def test_main_reads_with_get_only_and_exit_codes(bc, wallets, monkeypatch, capsys):
    sybil_world(bc, wallets, n_sybil=30, n_honest=5)
    chain = bc.to_list()
    node_prev = bc.payout_preview(1)
    import core
    urls = []

    def fake_get(url, timeout=60):
        urls.append(url)
        if url.endswith("/status"):
            return {"points_rules": core.points_v3_rules_hash()}
        if url.endswith("/chain"):
            return {"chain": chain}
        if "/payout_preview" in url:
            return node_prev
        raise AssertionError(url)

    monkeypatch.setattr(vp, "_get", fake_get)
    monkeypatch.setattr(vp, "check_chain", lambda *a, **k: [])      # this synthetic world has no real hashes or proof of work
    rc = vp.main(["--node", "http://node.invalid:1", "--period", "1"])
    out = capsys.readouterr().out
    assert rc in (0, 1) and "matches this recomputation exactly" in out and "no value" in out
    assert all(u.startswith("http://node.invalid:1/") for u in urls)

    def dead(url, timeout=60):
        raise OSError("no route")
    monkeypatch.setattr(vp, "_get", dead)
    assert vp.main(["--node", "http://node.invalid:1"]) == 2
    assert "Could not check" in capsys.readouterr().out
