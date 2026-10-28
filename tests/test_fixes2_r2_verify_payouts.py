"""Fixes round 2 (branch points-v4r-fixes2): R2, ops/verify_payouts.py complete validation by default, --quick named and labelled weak. Written to FAIL on points-v4r-fixes (eb4e3d5). Throwaway keys only."""
import copy
import os
import sys
import time

import pytest

import core
import economics as eco

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ops"))
import verify_payouts as vp  # noqa: E402

NLT = eco.BASE_UNITS_PER_NLT

def _fabricated_chain(n=7000):
    """A coherent history with NO work behind it: difficulty 0/1, nonce 0, every reward to one address."""
    bc = core.Blockchain.__new__(core.Blockchain)
    scratch_dir = os.getcwd()
    genesis, _ = vp._scratch_chain()
    chain = [genesis]
    t0 = time.time()
    for i in range(1, n):
        txs = [core.make_reward_tx("aa" * 16, 10 * NLT)]
        d = 0 if not core.is_post_fork(i) else 1
        h = bc.calculate_hash(index=i, timestamp=t0 + i, transactions=txs, previous_hash=chain[-1]["hash"], nonce=0,
                              difficulty=d)
        chain.append(core.Block(index=i, timestamp=t0 + i, transactions=txs, previous_hash=chain[-1]["hash"],
                                nonce=0, difficulty=d, hash=h).to_dict())
    return chain


def test_r2_a_fabricated_7000_block_chain_is_refused_by_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    chain = _fabricated_chain()
    bad = vp.check_chain(chain)                                  # the DEFAULT
    assert bad and "checkpoint" in bad[0]
    # even with no shipped checkpoints to lean on, the default (complete) check refuses it: there is no real work behind it
    monkeypatch.setattr(core, "_checkpoint_cache", {})
    bad = vp.check_chain(chain)
    assert bad and "validator refuses" in bad[0]
    # --quick cannot tell (that is exactly why it is labelled weak)
    assert vp.check_chain(chain, full=False) == []


def test_r2_a_genesis_with_a_10x_allocation_is_refused_in_both_modes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    chain = _fabricated_chain(50)
    g = copy.deepcopy(chain)
    for tx in g[0]["transactions"]:
        if tx.get("type") == "allocation":
            tx["amount"] = int(tx["amount"]) * 10
            break
    else:
        pytest.fail("no allocation in genesis")
    for full in (True, False):
        bad = vp.check_chain(g, full=full)
        assert bad and "genesis" in bad[0]


def test_r2_quick_mode_is_named_and_can_never_say_nothing_odd(monkeypatch, capsys):
    seen = {}

    def fake_run(chain, status, node_prev, reg_info, period, full=True):
        seen["full"] = full
        return (["x"], [] if full else ["QUICK MODE: not fully validated"], 0, {})
    monkeypatch.setattr(vp, "run_checks", fake_run)
    monkeypatch.setattr(vp, "_get", lambda url, timeout=60: {"chain": []} if url.endswith("/chain") else {})
    assert vp.main(["--node", "http://127.0.0.1:1"]) == 0 and seen["full"] is True            # default = complete
    assert vp.main(["--node", "http://127.0.0.1:1", "--full"]) == 0 and seen["full"] is True   # old flag still works
    assert vp.main(["--node", "http://127.0.0.1:1", "--quick"]) == 1 and seen["full"] is False
    out = capsys.readouterr().out
    assert "QUICK MODE" in out
    with pytest.raises(SystemExit):
        vp.main(["--node", "http://127.0.0.1:1", "--quick", "--full"])


def test_r2_run_checks_adds_the_quick_warning_and_the_limits_text_does_not_overstate(monkeypatch):
    monkeypatch.setattr(vp, "check_chain", lambda *a, **k: [])
    monkeypatch.setattr(vp, "local_preview", lambda chain, period: ({"status": "none"}, "rules"))
    monkeypatch.setattr(vp, "independent_period", lambda *a, **k: {"status": "none"})
    monkeypatch.setattr(vp, "analyse", lambda *a, **k: ([], []))
    monkeypatch.setattr(vp, "independent_checks", lambda *a, **k: ([], []))
    lines, warns, floor, prev = vp.run_checks([{"index": 0}], {"points_rules": "rules"}, None, None, None, full=False)
    assert floor == 0 and warns and "QUICK MODE" in warns[0] and any("QUICK MODE" in l for l in lines)
    lines, warns, floor, prev = vp.run_checks([{"index": 0}], {"points_rules": "rules"}, None, None, None)
    assert not any("QUICK" in w for w in warns) and any("complete" in l for l in lines)
    text = " ".join(vp.LIMITS).lower()
    assert "--quick does not prove" in text and "cannot prove that this checkout's rules" in text
    assert "proof of work" not in " ".join(l for l in vp.LIMITS if "quick" not in l.lower() and "genesis" not in l.lower()).lower()


