"""Sync checks a peer's chain without the node lock and only takes the lock for the final swap."""
import copy
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler

import core
import netutil


class _CountingLock:
    def __init__(self, real):
        self.real, self.n = real, 0
    def __enter__(self):
        self.n += 1
        return self.real.__enter__()
    def __exit__(self, *a):
        return self.real.__exit__(*a)
    def acquire(self, *a, **k):
        self.n += 1
        return self.real.acquire(*a, **k)
    def release(self):
        return self.real.release()
    def locked(self):
        return self.real.locked()


PEER = "http://127.0.0.1:1"


def _offer(node, monkeypatch, chain_or_map):
    def get(url, **k):
        c = chain_or_map[url.rsplit("/chain", 1)[0]] if isinstance(chain_or_map, dict) else chain_or_map
        return {"chain_id": node.chain_id(), "chain": c}
    monkeypatch.setattr(node, "_get_json", get)


def _fresh_state(node, monkeypatch):
    monkeypatch.setattr(node, "_sync_bad_tips", type(node._sync_bad_tips)())
    monkeypatch.setattr(node, "_sync_stats", {k: 0 for k in node._sync_stats})
    monkeypatch.setattr(node, "_sync_last_deep_fork", type(node._sync_last_deep_fork)())


def _mined(bc, n, addr="ab" * 16):
    other = copy.deepcopy(bc)
    for _ in range(n):
        b = other.mine_block(addr, [])
        if other.chain[-1].hash != b.hash:
            other.chain.append(b)
    return other


def _spy_validation(node, monkeypatch):
    seen = []
    real = node.blockchain.is_valid_chain
    def spy(c):
        seen.append(node.state_lock.locked())
        return real(c)
    monkeypatch.setattr(node.blockchain, "is_valid_chain", spy)
    return seen


def test_identical_peer_chain_skips_lock_and_validation(node, monkeypatch):
    _fresh_state(node, monkeypatch)
    calls = []
    monkeypatch.setattr(node.blockchain, "replace_chain", lambda *a, **k: calls.append(1) or False)
    seen = _spy_validation(node, monkeypatch)
    lock = _CountingLock(node.state_lock)
    monkeypatch.setattr(node, "state_lock", lock)
    _offer(node, monkeypatch, node.blockchain.to_list())
    node._sync_with_peer(PEER)
    assert calls == [] and seen == [] and lock.n == 0
    assert node._sync_info()["sync_skipped_identical"] == 1


def test_better_chain_validated_outside_lock_then_adopted(node, monkeypatch):
    _fresh_state(node, monkeypatch)
    better = _mined(node.blockchain, 2).to_list()
    seen = _spy_validation(node, monkeypatch)
    _offer(node, monkeypatch, better)
    node._sync_with_peer(PEER)
    assert node.blockchain.chain[-1].hash == better[-1]["hash"]
    assert seen == [False], "validated once, without the node lock"
    assert node._sync_info()["sync_adopted"] == 1


def test_swap_rechecks_work_under_lock(node, monkeypatch):
    """Our chain grows between the lockless validation and the swap: the swap must re-check work."""
    _fresh_state(node, monkeypatch)
    offer = _mined(node.blockchain, 1, addr="cd" * 16).to_list()
    ahead = _mined(node.blockchain, 2)
    real = node.blockchain.is_valid_chain
    def validate_then_grow(c):
        ok = real(c)
        node.blockchain.chain = list(ahead.chain)    # we moved ahead meanwhile
        return ok
    monkeypatch.setattr(node.blockchain, "is_valid_chain", validate_then_grow)
    _offer(node, monkeypatch, offer)
    node._sync_with_peer(PEER)
    assert node.blockchain.chain[-1].hash == ahead.chain[-1].hash


def _huge_difficulty_offer(node, d):
    ours = node.blockchain.to_list()
    return ours + [dict(ours[-1], index=ours[-1]["index"] + 1, previous_hash=ours[-1]["hash"],
                        difficulty=d, hash="00" * 32)]


def test_huge_claimed_difficulty_returns_fast(node, monkeypatch):
    for d in (10 ** 10, 10 ** 30):
        _fresh_state(node, monkeypatch)
        calls = []
        monkeypatch.setattr(node.blockchain, "replace_chain", lambda *a, **k: calls.append(1) or False)
        _offer(node, monkeypatch, _huge_difficulty_offer(node, d))
        t0 = time.monotonic()
        node._sync_with_peer(PEER)
        assert time.monotonic() - t0 < 2, d
        assert calls == []
        assert node._sync_info()["sync_skipped_precheck_failed"] == 1


def test_block_work_is_clamped():
    pre = core.Block.from_dict(dict(index=1, timestamp=0.0, transactions=[], previous_hash="0", nonce=0,
                                    difficulty=10 ** 30, hash="0"))
    t0 = time.monotonic()
    assert core.block_work(pre) == 16 ** core.MAX_PRE_FORK_DIFFICULTY
    post = core.Block.from_dict(dict(pre.to_dict(), index=core.LWMA_FORK_HEIGHT, difficulty=10 ** 100))
    assert core.block_work(post) == core.POW_LIMIT
    neg = core.Block.from_dict(dict(pre.to_dict(), difficulty=-5))
    assert core.block_work(neg) == 1
    assert time.monotonic() - t0 < 0.5


def test_peer_claiming_more_work_is_rejected_cheaply_and_backed_off(node, monkeypatch):
    _fresh_state(node, monkeypatch)
    ours = node.blockchain.to_list()
    fake = ours + [dict(ours[-1], index=ours[-1]["index"] + 1, previous_hash=ours[-1]["hash"], hash="ff" * 32)]
    seen = _spy_validation(node, monkeypatch)
    lock = _CountingLock(node.state_lock)
    monkeypatch.setattr(node, "state_lock", lock)
    _offer(node, monkeypatch, fake)
    for _ in range(3):
        node._sync_with_peer(PEER)
    info = node._sync_info()
    assert lock.n == 0 and seen == []
    assert info["sync_skipped_precheck_failed"] == 1 and info["sync_skipped_backoff"] == 2


def test_valid_structure_bad_body_is_validated_once_then_backed_off(node, monkeypatch):
    _fresh_state(node, monkeypatch)
    better = _mined(node.blockchain, 1).to_list()
    monkeypatch.setattr(node.blockchain, "is_valid_chain", lambda c: False)   # e.g. a bad signature
    lock = _CountingLock(node.state_lock)
    monkeypatch.setattr(node, "state_lock", lock)
    _offer(node, monkeypatch, better)
    for _ in range(3):
        node._sync_with_peer(PEER)
    info = node._sync_info()
    assert lock.n == 0 and info["sync_skipped_invalid"] == 1 and info["sync_skipped_backoff"] == 2


def test_lying_peers_cannot_block_honest_peer(node, monkeypatch):
    _fresh_state(node, monkeypatch)
    ours = node.blockchain.to_list()
    low = [dict(b, difficulty=0) for b in ours] + [dict(ours[-1], index=ours[-1]["index"] + 1, difficulty=0)]
    high = _huge_difficulty_offer(node, 10 ** 30)
    honest = _mined(node.blockchain, 1).to_list()
    _offer(node, monkeypatch, {"http://low:1": low, "http://high:1": high, "http://honest:1": honest})
    for p in ("http://low:1", "http://high:1", "http://honest:1"):
        node._sync_with_peer(p)
    assert node.blockchain.chain[-1].hash == honest[-1]["hash"]


def test_deep_lower_work_fork_is_counted(node, monkeypatch):
    _fresh_state(node, monkeypatch)
    theirs = _mined(node.blockchain, 1, addr="cd" * 16).to_list()
    node.blockchain.chain = list(_mined(node.blockchain, 3).chain)
    monkeypatch.setattr(node, "MAX_REORG_DEPTH", 2)
    monkeypatch.setattr(node, "is_security_fork", lambda i: False)
    _offer(node, monkeypatch, theirs)
    node._sync_with_peer(PEER)
    info = node._sync_info()
    assert info["sync_skipped_lower_work_deep_forks"] == 1
    assert info["sync_peers_on_deep_lower_work_fork"] == 1


# ---- reply deadline sized from Content-Length for every server (not only p2p) ----

class _Plain(BaseHTTPRequestHandler):
    seen = []
    def do_GET(self):
        body = b"x" * 10
        self.send_response(200)
        self.send_header("Content-Length", "5000000")    # declared size drives the deadline
        self.end_headers()
        _Plain.seen.append(self._reply_size_hint)
        self.wfile.write(body)
        self.close_connection = True
    def log_message(self, *a):
        pass


def test_content_length_sizes_reply_deadline():
    srv = netutil.CappedThreadingHTTPServer(("127.0.0.1", 0), _Plain)
    armed = []
    real = srv.RequestHandlerClass._arm_deadline
    def spy(self, seconds):
        armed.append(seconds)
        return real(self, seconds)
    srv.RequestHandlerClass._arm_deadline = spy
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        s = socket.create_connection(srv.server_address, timeout=5)
        s.sendall(b"GET / HTTP/1.0\r\nHost: x\r\n\r\n")
        s.settimeout(2)
        try:
            while s.recv(65536):
                pass
        except OSError:
            pass
        s.close()
        time.sleep(0.2)
    finally:
        srv.shutdown()
        srv.server_close()
    assert _Plain.seen == [5000000]
    base, rate = srv.reply_deadline_base, srv.reply_min_rate
    assert any(abs(a - (base + 5000000 / rate)) < 1e-6 for a in armed), armed
