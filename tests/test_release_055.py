"""0.5.5: version bump stays compatible with 0.5.4, deep-fork offers are judged outside the lock with backoff,
the deep-fork map is bounded and pruned, and the founder miner is more patient."""
import os
import socket
import sys

import pytest

import netlithic_version as v

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ops"))
import founder_miner  # noqa: E402


def test_version_is_055_and_still_peers_with_054():
    assert v.VERSION == "0.5.5"
    assert v.MIN_PEER_VERSION == "0.5.4"
    assert not v.is_outdated("0.5.4") and not v.is_outdated("0.5.5")
    assert v.UPGRADE_DEADLINE_HEIGHT == 4900 and v.FORK_READY_HEIGHT == 5000


def test_installer_version_matches():
    iss = open(os.path.join(os.path.dirname(__file__), "..", "desktop", "netlithic.iss")).read()
    assert "AppVersion=0.5.5" in iss


class _Lock:
    def __init__(self, real):
        self.real, self.n = real, 0
    def __enter__(self):
        self.n += 1
        return self.real.__enter__()
    def __exit__(self, *a):
        return self.real.__exit__(*a)
    def locked(self):
        return self.real.locked()


def _fresh(node, monkeypatch):
    monkeypatch.setattr(node, "_sync_bad_tips", type(node._sync_bad_tips)())
    monkeypatch.setattr(node, "_sync_stats", {k: 0 for k in node._sync_stats})
    monkeypatch.setattr(node, "_sync_last_deep_fork", type(node._sync_last_deep_fork)())


def _deep_setup(node, monkeypatch):
    _fresh(node, monkeypatch)
    monkeypatch.setattr(node, "is_security_fork", lambda h: True)
    monkeypatch.setattr(node, "MAX_REORG_DEPTH", 0)   # any divergence counts as "too deep"
    ours = node.blockchain.to_list()
    fork = [dict(b) for b in ours]
    fork[-1] = dict(fork[-1], hash="ff" * 32)
    fork.append(dict(fork[-1], index=fork[-1]["index"] + 1, hash="ee" * 32))
    monkeypatch.setattr(node, "_get_json", lambda url, **k: {"chain_id": node.chain_id(), "chain": fork})
    return fork


def test_deep_fork_never_takes_lock_and_backs_off(node, monkeypatch):
    _deep_setup(node, monkeypatch)
    replace = []
    monkeypatch.setattr(node.blockchain, "replace_chain", lambda *a, **k: replace.append(1) or False)
    notes = []
    def note(chain, depth, ours=None, record=None):
        notes.append(node.state_lock.locked())
    monkeypatch.setattr(node.blockchain, "_note_refused_deep_reorg", note)
    lock = _Lock(node.state_lock)
    monkeypatch.setattr(node, "state_lock", lock)
    peer = "http://127.0.0.1:1"
    node._sync_with_peer(peer)
    node._sync_with_peer(peer)          # same tip again: backoff, not judged twice
    assert replace == [] and lock.n == 0
    assert notes == [False]
    info = node._sync_info()
    assert info["sync_skipped_deep_refused"] == 1 and info["sync_skipped_backoff"] == 1
    assert info["sync_peers_on_deep_lower_work_fork"] == 1


def test_deep_fork_record_goes_through_short_lock(node, monkeypatch):
    _fresh(node, monkeypatch)
    lock = _Lock(node.state_lock)
    monkeypatch.setattr(node, "state_lock", lock)
    got = []
    monkeypatch.setattr(node.blockchain, "record_rejected_reorg", lambda d, now=None: got.append((d, lock.real.locked())))
    node._record_rejected_reorg_locked(800, 1.0)
    assert got == [(800, True)] and lock.n == 1


def test_note_refused_deep_reorg_uses_record_callback(node, monkeypatch):
    bc = node.blockchain
    monkeypatch.setattr(bc, "_refused_chain_precheck", lambda c, d, ours=None: True)
    monkeypatch.setattr(bc, "is_valid_chain", lambda c: True)
    bc.__dict__.pop("_refused_last_validation", None)
    bc.__dict__.pop("_refused_seen", None)
    got = []
    bc._note_refused_deep_reorg([{"hash": "aa" * 32}], 900, ours=list(bc.chain), record=lambda d, now: got.append(d))
    assert got == [900]


def test_deep_fork_map_is_bounded_and_pruned(node, monkeypatch):
    _fresh(node, monkeypatch)
    for i in range(node.SYNC_DEEP_FORK_KEEP + 50):
        node._sync_note_deep_fork(f"http://p{i}", 800)
    assert len(node._sync_last_deep_fork) == node.SYNC_DEEP_FORK_KEEP
    assert "http://p0" not in node._sync_last_deep_fork
    keep = f"http://p{node.SYNC_DEEP_FORK_KEEP + 49}"
    node._sync_prune_deep_forks([keep])
    assert list(node._sync_last_deep_fork) == [keep]


class _Resp:
    def __init__(self, body):
        self.body = body
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def read(self):
        return self.body


def test_miner_timeout_is_60s():
    assert founder_miner.HTTP_TIMEOUT_SECONDS == 60
    seen = {}
    def fake(req, timeout=None):
        seen["t"] = timeout
        return _Resp(b'{"height": 1}')
    orig = founder_miner._safe_urlopen
    founder_miner._safe_urlopen = fake
    try:
        assert founder_miner._get("http://x/status") == {"height": 1}
    finally:
        founder_miner._safe_urlopen = orig
    assert seen["t"] == 60


def test_miner_retries_with_backoff_then_succeeds(monkeypatch):
    calls, sleeps = [], []
    def fake(req, timeout=None):
        calls.append(1)
        if len(calls) < 3:
            raise socket.timeout("slow")
        return _Resp(b'{"ok": true}')
    monkeypatch.setattr(founder_miner, "_safe_urlopen", fake)
    assert founder_miner._get("http://x/status", sleep=sleeps.append) == {"ok": True}
    assert len(calls) == 3 and sleeps == [2.0, 4.0]


def test_miner_gives_up_after_retries(monkeypatch):
    calls, sleeps = [], []
    def fake(req, timeout=None):
        calls.append(1)
        raise OSError("down")
    monkeypatch.setattr(founder_miner, "_safe_urlopen", fake)
    with pytest.raises(OSError):
        founder_miner._get("http://x/status", sleep=sleeps.append)
    assert len(calls) == 4 and sleeps == [2.0, 4.0, 8.0]


def test_miner_does_not_retry_bad_json(monkeypatch):
    calls = []
    def fake(req, timeout=None):
        calls.append(1)
        return _Resp(b'{"x": NaN}')
    monkeypatch.setattr(founder_miner, "_safe_urlopen", fake)
    with pytest.raises(ValueError):
        founder_miner._get("http://x/status", sleep=lambda s: None)
    assert len(calls) == 1
