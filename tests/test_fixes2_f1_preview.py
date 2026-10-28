"""Fixes round 2 (branch points-v4r-fixes2): F1, /payout_preview single flight, no junk cache, bounded limiter, IPv6 /64. Written to FAIL on points-v4r-fixes (eb4e3d5). Throwaway keys only."""
import threading
import time
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

import core

def _bare_chain(tip="t1", n=10):
    bc = core.Blockchain.__new__(core.Blockchain)
    return bc, [SimpleNamespace(hash="h%d" % i) for i in range(n - 1)] + [SimpleNamespace(hash=tip)]


def test_f1_forty_concurrent_identical_requests_run_one_computation(monkeypatch):
    bc, chain = _bare_chain()
    calls, gate = [], threading.Event()

    def slow(period=None, c=None):
        calls.append(period)
        gate.wait(5)
        return {"status": "proposed", "period": period}
    monkeypatch.setattr(bc, "payout_preview", slow)
    out = []
    ts = [threading.Thread(target=lambda: out.append(bc.payout_preview_cached(2, chain))) for _ in range(40)]
    for t in ts:
        t.start()
    time.sleep(0.3)
    gate.set()
    for t in ts:
        t.join(10)
    assert len(calls) == 1 and len(out) == 40 and all(o is out[0] for o in out)


def test_f1_junk_periods_are_not_cached_and_cannot_evict_real_entries(monkeypatch):
    bc, chain = _bare_chain()
    calls = []

    def fake(period=None, c=None):
        calls.append(period)
        if period in (None, 1, 2):
            return {"status": "proposed", "period": period}
        return {"status": "none", "error": "Unknown period."}
    monkeypatch.setattr(bc, "payout_preview", fake)
    real_latest, real_2 = bc.payout_preview_cached(None, chain), bc.payout_preview_cached(2, chain)
    for p in [-1, 0, 10 ** 300] + list(range(1000, 1000 + 5 * bc.PREVIEW_CACHE_SIZE)):
        bc.payout_preview_cached(p, chain)
    assert len(bc._preview_cache) <= 1
    n = len(calls)
    assert bc.payout_preview_cached(None, chain) is real_latest and bc.payout_preview_cached(2, chain) is real_2
    assert len(calls) == n                       # both still cached
    bc.payout_preview_cached(-1, chain)
    assert len(calls) == n + 1                   # a junk period is recomputed (cheap), never stored


def test_f1_many_real_periods_cannot_evict_the_pinned_latest_answer_and_the_lru_is_bounded(monkeypatch):
    bc, chain = _bare_chain()
    monkeypatch.setattr(bc, "payout_preview", lambda period=None, c=None: {"status": "paid", "period": period})
    latest = bc.payout_preview_cached(None, chain)
    for p in range(1, 200):
        bc.payout_preview_cached(p, chain)
    assert len(bc._preview_cache) == bc.PREVIEW_CACHE_SIZE
    assert bc.payout_preview_cached(None, chain) is latest
    _, chain2 = _bare_chain(tip="t2")            # the chain moved: the pinned answer is not stale
    assert bc.payout_preview_cached(None, chain2) is not latest


def test_f1_too_many_different_computations_get_busy_not_a_pile_up(monkeypatch):
    bc, chain = _bare_chain()
    gate, started = threading.Event(), []

    def slow(period=None, c=None):
        started.append(period)
        gate.wait(5)
        return {"status": "paid"}
    monkeypatch.setattr(bc, "payout_preview", slow)
    ts = [threading.Thread(target=lambda p=p: bc.payout_preview_cached(p, chain)) for p in (1, 2)]
    for t in ts:
        t.start()
    time.sleep(0.2)
    with pytest.raises(core.PreviewBusy):
        bc.payout_preview_cached(3, chain)
    gate.set()
    for t in ts:
        t.join(10)
    assert bc.payout_preview_cached(3, chain)["status"] == "paid"    # slots were released


def test_f1_a_failed_leader_does_not_wedge_waiters(monkeypatch):
    bc, chain = _bare_chain()
    n = {"c": 0}

    def flaky(period=None, c=None):
        n["c"] += 1
        if n["c"] == 1:
            raise RuntimeError("boom")
        return {"status": "paid"}
    monkeypatch.setattr(bc, "payout_preview", flaky)
    with pytest.raises(RuntimeError):
        bc.payout_preview_cached(2, chain)
    assert bc.payout_preview_cached(2, chain)["status"] == "paid"


def test_f1_endpoint_answers_503_when_busy(node, monkeypatch):
    import p2p_http
    monkeypatch.setattr(p2p_http, "RATE_LIMIT_LOOPBACK", False)

    def busy(period=None, chain=None):
        raise core.PreviewBusy("x")
    monkeypatch.setattr(p2p_http.blockchain, "payout_preview_cached", busy)
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(f"{node.self_url()}/payout_preview?period=1", timeout=5)
    assert e.value.code == 503


def test_f1_rate_limiter_is_a_bounded_lru_that_does_not_reset_past_the_cap(monkeypatch):
    import p2p_http as node
    monkeypatch.setattr(node, "MAX_TRACKED_IPS", 50)
    monkeypatch.setattr(node, "PREVIEW_RATE_BURST", 2.0)
    monkeypatch.setattr(node, "PREVIEW_RATE_PER_SECOND", 0.0)
    node._preview_buckets.clear()
    victim = "203.0.113.9"
    assert not node.preview_rate_limited(victim, now=1.0) and not node.preview_rate_limited(victim, now=1.0)
    assert node.preview_rate_limited(victim, now=1.0)                        # victim's budget is used up
    for i in range(49):                                                       # fill the table to the cap, victim stays recent
        node.preview_rate_limited(f"198.51.100.{i}", now=1.0)
        node.preview_rate_limited(victim, now=1.0)
    assert len(node._preview_buckets) <= 50
    assert node.preview_rate_limited(victim, now=1.0)                        # NOT reset to a fresh budget
    for i in range(200):
        node.preview_rate_limited(f"192.0.2.{i}", now=1.0)
    assert len(node._preview_buckets) <= 50                                   # bounded, evicts one at a time


def test_f1_ipv6_is_counted_per_slash_64(monkeypatch):
    import p2p_http as node
    monkeypatch.setattr(node, "RATE_BURST", 3.0)
    monkeypatch.setattr(node, "RATE_PER_SECOND", 0.0)
    node._rate_buckets.clear()
    res = [node.rate_limited(f"2001:db8:1:2:{i:x}::{i:x}", now=1.0) for i in range(1, 8)]
    assert res == [False] * 3 + [True] * 4                                    # one /64, one budget
    assert not node.rate_limited("2001:db8:1:3::1", now=1.0)                  # a different /64
    assert node._bucket_key("::ffff:203.0.113.5") == "203.0.113.5"
    assert node._bucket_key("fe80::1%eth0") == node._bucket_key("fe80::2")
    assert node._bucket_key("not-an-ip") == "not-an-ip"
