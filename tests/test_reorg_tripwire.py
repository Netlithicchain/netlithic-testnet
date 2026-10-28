"""Points v4 item 7: the off-chain reorg tripwire (log + /status fields + healthcheck alert).
Nothing here is consensus: the 720-block cap and the checkpoints are unchanged."""
import json
import logging
import os
import sys
import urllib.request
from dataclasses import replace

import core
from test_security_fork import grow, small_fork

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops"))
import healthcheck as H

D = "d" * 32


def _fork_of_depth(chain, depth, extra=2):
    ours = grow(chain, chain.chain, 10, step=1.0)
    ours = [ours[0]] + [replace(b) for b in ours[1:]]
    chain.chain = ours
    return grow(chain, ours[:-depth], depth + extra, step=1.0, miner=D)


def test_shallow_reorg_is_recorded_but_quiet(chain, monkeypatch, caplog):
    small_fork(monkeypatch)
    theirs = _fork_of_depth(chain, 2)
    with caplog.at_level(logging.WARNING, logger="netlithic.core"):
        assert chain.replace_chain([b.to_dict() for b in theirs])
    info = chain.reorg_info()
    assert info["last_reorg_depth"] == 2 and info["max_reorg_depth_24h"] == 2 and info["reorgs_24h"] == 1
    assert not [r for r in caplog.records if "REORG" in r.getMessage()]


def test_deep_reorg_is_logged_and_reported(chain, monkeypatch, caplog):
    small_fork(monkeypatch)
    monkeypatch.setattr(core, "MAX_REORG_DEPTH", 10)
    theirs = _fork_of_depth(chain, 5)
    with caplog.at_level(logging.WARNING, logger="netlithic.core"):
        assert chain.replace_chain([b.to_dict() for b in theirs])
    info = chain.reorg_info()
    assert info["last_reorg_depth"] == 5 and info["max_reorg_depth_24h"] == 5
    assert info["reorg_alert_depth"] == 3 and info["reorg_limit"] == 10
    assert any("REORG of 5 blocks" in r.getMessage() for r in caplog.records)


def test_a_refused_reorg_is_not_recorded_and_the_cap_still_holds(chain, monkeypatch):
    small_fork(monkeypatch)
    monkeypatch.setattr(core, "MAX_REORG_DEPTH", 3)
    theirs = _fork_of_depth(chain, 6, extra=3)
    assert not chain.replace_chain([b.to_dict() for b in theirs])
    assert chain.reorg_info()["max_reorg_depth_24h"] == 0 and chain.reorg_info()["last_reorg_depth"] == 0
    monkeypatch.undo()
    assert core.MAX_REORG_DEPTH == 720                                    # the shipped cap is unchanged


def test_24h_window_forgets_old_reorgs_but_last_one_is_kept(chain):
    chain.record_reorg(7, 100, now=1_000.0)
    assert chain.reorg_info(now=1_000.0 + 3600)["max_reorg_depth_24h"] == 7
    later = chain.reorg_info(now=1_000.0 + 25 * 3600)
    assert later["max_reorg_depth_24h"] == 0 and later["last_reorg_depth"] == 7 and later["reorgs_24h"] == 0


def test_zero_depth_is_not_a_reorg(chain):
    chain.record_reorg(0, 5)
    assert chain.reorg_info()["reorgs_24h"] == 0


def test_status_endpoint_carries_the_fields(node):
    with urllib.request.urlopen(f"{node.self_url()}/status") as r:
        st = json.loads(r.read())
    for k in ("last_reorg_depth", "max_reorg_depth_24h", "reorgs_24h", "reorg_alert_depth", "reorg_limit"):
        assert k in st
    assert st["last_reorg_depth"] == 0 and st["reorg_limit"] == 720


def test_healthcheck_alerts_from_depth_three_like_the_node_does():
    assert H.deep_reorg_problem({"max_reorg_depth_24h": 2}) is None                 # 1 or 2 blocks is normal
    assert "3 blocks" in H.deep_reorg_problem({"max_reorg_depth_24h": 3})          # depth 3 alerts (it was 4 before 0.5.2)
    assert H.deep_reorg_problem({"max_reorg_depth_24h": 0}) is None
    assert H.deep_reorg_problem({}) is None                       # older node: no field, no alert
    assert H.deep_reorg_problem({"max_reorg_depth_24h": "x"}) is None
    msg = H.deep_reorg_problem({"max_reorg_depth_24h": 4})
    assert msg and "4 blocks" in msg


def test_healthcheck_run_raises_deep_reorg_alert(tmp_path):
    cfg = H.load_config(None)
    node = "http://127.0.0.1:8000"
    st = {"height": 480, "version": "0.5.1", "fork_ready": True, "latest_hash": "x480", "peers": [],
          "min_peer_version": "0.5.1", "upgrade_deadline_height": 4900, "node": node, "max_reorg_depth_24h": 9}
    quiet = dict(st, max_reorg_depth_24h=1)
    fetch = lambda url: {"chain": [{"index": i, "hash": f"x{i}"} for i in range(500)]}
    p, _ = H.v050_checks(cfg, {}, {node: st}, 1000.0, fetch, journal_fn=lambda u, s: [])
    assert f"deep_reorg:{node}" in p
    p, _ = H.v050_checks(cfg, {}, {node: quiet}, 1000.0, fetch, journal_fn=lambda u, s: [])
    assert f"deep_reorg:{node}" not in p
