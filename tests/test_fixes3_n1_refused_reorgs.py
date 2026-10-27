"""Review round 3, N1: a junk chain must not be able to hide the alert for a REAL deep reorg.
Both attacks from the Security Reviewer's report (budget starvation, tip poisoning) are reproduced here, plus the
neighbours of the same idea. The refusal itself (a too-deep chain is never adopted) must stay exactly as it was."""
import copy
import json
import os
import time as _time
import urllib.request

import pytest

import core
from conftest import build_block
from test_security_fork import grow, small_fork
from test_reorg_tripwire import _fork_of_depth

D = "d" * 32
LIMIT = 3


class Clock:
    """A fake clock for core only (everything but time()/monotonic() still comes from the real module)."""
    def __init__(self, start):
        self.now = start

    def time(self):
        return self.now

    def monotonic(self):
        return self.now

    def __getattr__(self, name):
        return getattr(_time, name)


@pytest.fixture
def setup(chain, monkeypatch, tmp_path):
    small_fork(monkeypatch)
    monkeypatch.setattr(core, "MAX_REORG_DEPTH", LIMIT)
    monkeypatch.chdir(tmp_path)
    clock = Clock(_time.time())
    monkeypatch.setattr(core, "time", clock)
    theirs = _fork_of_depth(chain, 6, extra=3)               # a REAL, valid, heavier chain that forks 6 blocks back
    real = [b.to_dict() for b in theirs]
    return chain, real, clock


def rejected(chain):
    return chain.reorg_info()["rejected_deep_reorgs_24h"]


def unvalidated(chain):
    return chain.reorg_info()["refused_unvalidated_deep_reorgs_24h"]


_n = [0]


def junk_with_fresh_tip(chain, real):
    """Looks like a deep fork (our prefix, then made-up blocks) and has a different tip hash every time."""
    _n[0] += 1
    j = copy.deepcopy(real)
    fork = chain.reorg_depth_raw(real)
    start = len(chain.chain) - fork
    for b in j[start:]:
        b["hash"] = f"{_n[0]:032x}" + "ee" * 16
    return j


# ---- the two attacks, as the reviewer measured them -------------------------------------------------------------

def test_n1_budget_starvation_junk_every_29s_cannot_hide_the_real_chain(setup):
    chain, real, clock = setup
    t0 = clock.now
    last_junk = None
    while clock.now - t0 < 300:
        if last_junk is None or clock.now - last_junk >= 29:
            assert chain.replace_chain(junk_with_fresh_tip(chain, real)) is False
            last_junk = clock.now
        clock.now += 1
        assert chain.replace_chain(real) is False         # the refusal itself is unchanged
        clock.now += 4
    assert rejected(chain) == 1                             # reviewer: 0 with the attacker, 1 without


def test_n1_no_attacker_gives_the_same_count(setup):
    chain, real, clock = setup
    for _ in range(60):
        chain.replace_chain(real)
        clock.now += 5
    assert rejected(chain) == 1


def test_n1_tip_poisoning_a_junk_body_with_the_real_tip_hash_does_not_hide_the_real_chain(setup):
    chain, real, clock = setup
    start = len(chain.chain) - chain.reorg_depth_raw(real)
    poison = copy.deepcopy(real)
    for b in poison[start:-1]:
        b["hash"] = "ab" * 32                               # garbage middle, same claimed tip as the real chain
    assert poison[-1]["hash"] == real[-1]["hash"]
    assert chain.replace_chain(poison) is False
    clock.now += 31
    assert chain.replace_chain(real) is False
    assert rejected(chain) == 1                             # reviewer: 0 (the real chain was skipped for 600 s)


def test_n1_poisoning_through_corrupted_shared_prefix_bodies_does_not_hide_the_real_chain(setup):
    """Same idea without touching the divergent blocks: our shared prefix is sent with altered bodies (hashes kept)."""
    chain, real, clock = setup
    start = len(chain.chain) - chain.reorg_depth_raw(real)
    poison = copy.deepcopy(real)
    poison[1]["transactions"] = []                          # same claimed hash as our block 1, different content
    assert poison[1]["hash"] == chain.chain[1].hash and start > 1
    chain.replace_chain(poison)
    clock.now += 31
    chain.replace_chain(real)
    assert rejected(chain) == 1


def test_n1_a_chain_with_real_work_but_a_bad_last_block_cannot_keep_taking_the_slot(setup):
    """It passes the cheap checks (real hashes and work) and fails full validation. It may take ONE slot, then it is
    remembered as judged, so it cannot starve the real chain by being offered again and again."""
    chain, real, clock = setup
    start = len(chain.chain) - chain.reorg_depth_raw(real)
    prefix = list(chain.chain[:start])
    bad = grow(chain, prefix, 8, step=1.0, miner=D)
    greedy = core.make_reward_tx(D, chain.expected_reward(bad) * 1000)       # not allowed by the reward rule
    bad.append(build_block(chain, [greedy], chain=bad, timestamp=bad[-1].timestamp + 1))
    bad_d = [b.to_dict() for b in bad]
    calls = []
    orig = core.Blockchain.is_valid_chain
    core.Blockchain.is_valid_chain = lambda self, c: (calls.append(1), orig(self, c))[1]
    try:
        for _ in range(20):
            chain.replace_chain(bad_d)
            clock.now += 31                                  # the budget is open again every time
        assert len(calls) == 1
        chain.replace_chain(real)
        assert rejected(chain) == 1
    finally:
        core.Blockchain.is_valid_chain = orig


# ---- budget and tip bookkeeping ---------------------------------------------------------------------------------

def test_n1_junk_with_garbage_hashes_costs_no_full_validation(setup):
    chain, real, clock = setup
    calls = []
    orig = core.Blockchain.is_valid_chain
    core.Blockchain.is_valid_chain = lambda self, c: (calls.append(1), orig(self, c))[1]
    try:
        for _ in range(100):
            chain.replace_chain(junk_with_fresh_tip(chain, real))
    finally:
        core.Blockchain.is_valid_chain = orig
    assert calls == []
    assert rejected(chain) == 0


def test_n1_junk_does_not_write_the_sidecar_file(setup, tmp_path):
    chain, real, clock = setup
    for _ in range(30):
        chain.replace_chain(junk_with_fresh_tip(chain, real))
    assert not os.path.exists(core.Blockchain._reorg_log_path())


def test_n1_the_real_chain_is_still_counted_once_per_tip(setup):
    chain, real, clock = setup
    for _ in range(10):
        chain.replace_chain(real)
        clock.now += 40                                     # 400 s in all: inside the 600 s de-duplication window
    assert rejected(chain) == 1


# ---- the new "refused, not validated" counter -----------------------------------------------------------------

def test_n1_unvalidated_counter_counts_junk_and_is_not_an_alert_input(setup):
    chain, real, clock = setup
    for _ in range(25):
        chain.replace_chain(junk_with_fresh_tip(chain, real))
    assert unvalidated(chain) == 25
    info = chain.reorg_info()
    assert info["refused_unvalidated_deep_reorgs_total"] == 25
    assert info["rejected_deep_reorgs_24h"] == 0 and info["max_rejected_reorg_depth_24h"] == 0
    import ops.healthcheck as H
    assert H.deep_reorg_problem(info) is None               # volume only, never an alert


def test_n1_unvalidated_counter_forgets_after_24h_but_total_stays(setup):
    chain, real, clock = setup
    for _ in range(5):
        chain.replace_chain(junk_with_fresh_tip(chain, real))
    clock.now += 25 * 3600
    info = chain.reorg_info()
    assert info["refused_unvalidated_deep_reorgs_24h"] == 0 and info["refused_unvalidated_deep_reorgs_total"] == 5


def test_n1_unvalidated_counter_is_memory_bounded(setup, monkeypatch):
    chain, real, clock = setup
    monkeypatch.setattr(core.Blockchain, "REFUSED_UNVALIDATED_KEEP", 40)
    for _ in range(chain.REFUSED_UNVALIDATED_KEEP + 200):
        chain.replace_chain(junk_with_fresh_tip(chain, real))
    assert len(chain.__dict__["_refused_unvalidated"]) <= chain.REFUSED_UNVALIDATED_KEEP
    assert chain.reorg_info()["refused_unvalidated_deep_reorgs_total"] == chain.REFUSED_UNVALIDATED_KEEP + 200


def test_n1_a_chain_that_ran_out_of_budget_counts_as_unvalidated_not_as_an_alert(setup):
    chain, real, clock = setup
    start = len(chain.chain) - chain.reorg_depth_raw(real)
    other = grow(chain, list(chain.chain[:start]), 9, step=1.0, miner="e" * 32)       # a second REAL deep chain
    other_d = [b.to_dict() for b in other]
    chain.replace_chain(real)
    chain.replace_chain(other_d)                                                     # same second: out of budget
    assert rejected(chain) == 1 and unvalidated(chain) == 1
    clock.now += 31
    chain.replace_chain(other_d)
    assert rejected(chain) == 2 and unvalidated(chain) == 1


def test_n1_status_endpoint_and_healthcheck_carry_the_counter(node):
    with urllib.request.urlopen(f"{node.self_url()}/status") as r:
        st = json.loads(r.read())
    assert st["refused_unvalidated_deep_reorgs_24h"] == 0
    assert st["refused_unvalidated_deep_reorgs_total"] == 0
    import ops.healthcheck as H
    assert H.unvalidated_refusals_note({"refused_unvalidated_deep_reorgs_24h": 0}) is None
    assert H.unvalidated_refusals_note({}) is None
    assert H.unvalidated_refusals_note({"refused_unvalidated_deep_reorgs_24h": "x"}) is None
    note = H.unvalidated_refusals_note({"refused_unvalidated_deep_reorgs_24h": 12})
    assert "12" in note and "not an alert" in note


def test_n1_healthcheck_run_reports_the_counter_without_raising_an_alert():
    import ops.healthcheck as H
    cfg = H.load_config(None)
    node = "http://127.0.0.1:8000"
    cfg["nodes"] = [node]
    cfg["faucet_info_url"] = ""
    cfg["cert_hosts"] = []
    cfg["disk_paths"] = []
    status = {"height": 10, "version": H.DEFAULTS.get("expected_version") or "0.5.3", "fork_ready": True,
              "refused_unvalidated_deep_reorgs_24h": 7, "latest_hash": "x"}
    problems, state = H.run_checks(cfg, {}, now=1000.0, fetch=lambda url: status)
    assert not [k for k in problems if k.startswith("deep_reorg")]
    assert state["info"][node]["refused_unvalidated_deep_reorgs_24h"] == 7
