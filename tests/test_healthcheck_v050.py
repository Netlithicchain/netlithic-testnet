"""0.5.0 healthcheck alerts: each fake failure is detected; healthy stays quiet."""
import json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops"))
import healthcheck as H

A, B, P = "http://127.0.0.1:8000", "http://127.0.0.1:8001", "http://203.0.113.5:8000"
CHAIN = [{"index": i, "hash": f"x{i}"} for i in range(500)]


def st(h=480, v="0.5.3", tip=None, peers=(), ready=True, node=None):
    return {"pause_key_id": "2a83f37a4ce7a269", "height": h, "version": v, "fork_ready": ready, "latest_hash": tip or f"x{h}",
            "peers": list(peers), "min_peer_version": "0.5.3", "upgrade_deadline_height": 4900, "node": node}


def make_fetch(peer_v="0.5.3", chain=CHAIN, pause=None):
    def f(url):
        if url.endswith("/chain"):
            return {"chain": chain}
        if url.endswith("/pause_status"):
            return pause or {"paused": False, "expired": False}
        if url.startswith(P):
            return {"version": peer_v}
        raise OSError("x")
    return f


def cfg(tmp_path, cps=None, **kw):
    c = H.load_config(None)
    path = tmp_path / "cp.json"
    path.write_text(json.dumps(cps if cps is not None else {"_readme": "x", "400": "x400"}))
    c.update(checkpoints_file=str(path), **dict({"expected_pause_key_id": "2a83f37a4ce7a269"}, **kw))
    return c


def chk(c, statuses, state=None, **fk):
    return H.v050_checks(c, state or {}, statuses, 1000.0, make_fetch(**fk), journal_fn=lambda u, s: [])


def test_healthy_is_quiet(tmp_path):
    p, _ = chk(cfg(tmp_path), {A: st(peers=[P]), B: st(peers=[P])})
    assert p == {}


def test_old_node_and_not_ready(tmp_path):
    p, _ = chk(cfg(tmp_path), {A: st(v=None), B: st(ready=False)})
    assert "node_version:" + A in p and "fork_not_ready:" + B in p


def test_old_peer_and_urgent_near_deadline(tmp_path):
    p, _ = chk(cfg(tmp_path), {A: st(peers=[P])}, peer_v=None)
    assert "old_peer:" + P in p and "4420 blocks left" in p["old_peer:" + P]
    p, _ = chk(cfg(tmp_path), {A: st(h=4500, peers=[P])}, peer_v="0.4.0", chain=CHAIN)
    assert "old_peer_urgent:" + P in p


def test_tip_split(tmp_path):
    p, _ = chk(cfg(tmp_path), {A: st(tip="aa"), B: st(tip="bb")})
    assert "tip_split" in p


def test_checkpoint_mismatch_and_missing_file(tmp_path):
    p, _ = chk(cfg(tmp_path, cps={"400": "WRONG"}), {A: st()})
    assert "checkpoint:400" in p
    c = cfg(tmp_path); c["checkpoints_file"] = str(tmp_path / "nope.json")
    p, _ = chk(c, {A: st()})
    assert "checkpoints_missing" in p


def test_chain_rewrite_detected(tmp_path):
    c = cfg(tmp_path)
    _, s = chk(c, {A: st()})
    forked = CHAIN[:495] + [{"index": i, "hash": f"y{i}"} for i in range(495, 500)]
    p, _ = chk(c, {A: st()}, state=s, chain=forked)
    assert "chain_rewrite" in p


def test_pause_on_and_expired(tmp_path):
    p, _ = chk(cfg(tmp_path), {A: st()}, pause={"paused": True, "expired": True})
    assert "paused:" + A in p and "pause_expired:" + A in p


def test_journal_patterns_and_unreadable(tmp_path):
    c = cfg(tmp_path, journal_units=["netlithic-node-a"])
    lines = ["Rejected chain: reorg of 900 blocks exceeds limit 720 (peer advertises...)",
             "Rejected chain: block 400 does not match checkpoint", "Skipping peer 'http://127.0.0.1:8001': private"]
    p, s = H.v050_checks(c, {}, {A: st()}, 1000.0, make_fetch(), journal_fn=lambda u, since: lines)
    assert {"log_reorg_refused", "log_checkpoint_reject", "log_peer_skipped"} <= set(p) and s["journal_since"] == 1000.0
    p, _ = H.v050_checks(c, {}, {A: st()}, 1000.0, make_fetch(), journal_fn=lambda u, since: None)
    assert "journal_unreadable" in p


def test_real_checkpoint_file_loads():
    cps = H._load_checkpoints(H.DEFAULTS["checkpoints_file"])
    assert 400 in cps and len(cps[400]) == 64


def test_pause_key_id_mismatch_alerts_and_match_is_quiet(tmp_path):
    want = "0123456789abcdef"
    c = cfg(tmp_path, expected_pause_key_id=want)
    ok = dict(st(), pause_key_id=want, pause_enabled=True)
    p, _ = chk(c, {A: ok, B: dict(ok)})
    assert p == {}
    p, _ = chk(c, {A: ok, B: dict(ok, pause_key_id="", pause_enabled=False)})
    assert list(p) == ["pause_key:" + B] and "EMPTY" in p["pause_key:" + B]
    p, _ = chk(c, {A: dict(ok, pause_key_id="ffffffffffffffff")})
    assert "pause_key:" + A in p


def test_blank_expected_pause_key_id_warns_once_not_per_node(tmp_path):
    c = cfg(tmp_path, expected_pause_key_id="")
    p, _ = chk(c, {A: st(), B: st()})
    assert list(p) == ["pause_key_check_off"]                   # one warning for the config, not one per node
    assert "2a83f37a4ce7a269" in p["pause_key_check_off"] and "Warning" in p["pause_key_check_off"]
    # no node answered with a version -> nothing to say yet (the "down" alerts cover that)
    assert "pause_key_check_off" not in chk(c, {A: {"height": 1}})[0]


def test_blank_pause_key_warning_repeats_only_every_repeat_minutes(tmp_path):
    c = cfg(tmp_path, expected_pause_key_id="")
    c["repeat_minutes"] = 360
    problems, _ = chk(c, {A: st()})
    out, state = H.plan_alerts(problems, {}, c, 1000.0)
    assert len(out) == 1
    out, state = H.plan_alerts(problems, state, c, 1000.0 + 3600)       # an hour later: quiet
    assert out == []
    out, state = H.plan_alerts(problems, state, c, 1000.0 + 360 * 60)   # after repeat_minutes: once more
    assert len(out) == 1
