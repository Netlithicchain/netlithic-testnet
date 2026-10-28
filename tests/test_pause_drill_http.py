"""Pause drill part 3: real node PROCESSES talking HTTP on 127.0.0.1.

Throwaway pause key and wallets are generated inside the test (tmp_path). The
env var NETLITHIC_ALLOW_PRIVATE_PEERS=1 is set only for these child processes.
Constants are scaled by ops/drill/pause_drill_node.py (fork 3, payout period 4,
pause expiry 6).
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import economics as eco
import pause
from conftest import ROOT, free_port

NLT = eco.BASE_UNITS_PER_NLT
TOKEN = "drill-admin-token-not-a-secret"
LAUNCHER = os.path.join(ROOT, "ops", "drill", "pause_drill_node.py")
CID = "netlithic-dev-1"


def _req(port, path, body=None, admin=False, timeout=15):
    url = f"http://127.0.0.1:{port}{path}"
    if body is None:
        r = urllib.request.Request(url)
    else:
        h = {"Content-Type": "application/json"}
        if admin:
            h["X-Admin-Token"] = TOKEN
        r = urllib.request.Request(url, data=json.dumps(body).encode(), headers=h, method="POST")
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _wait(pred, timeout=40, what="condition"):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return
        time.sleep(0.3)
    raise AssertionError(f"timed out waiting for {what}")


class Cluster:
    def __init__(self, tmp_path, pub, n_new=3, n_old=0):
        self.tmp, self.pub, self.procs, self.ports = tmp_path, pub, {}, []
        self.old = []
        for _ in range(n_new):
            self.ports.append(self.start())
        for _ in range(n_old):
            p = self.start(old=True)
            self.ports.append(p)
            self.old.append(p)
        # fully connect (admin add_peer both ways)
        for a in self.ports:
            for b in self.ports:
                if a != b:
                    _req(a, "/add_peer", {"peer": f"http://127.0.0.1:{b}"}, admin=True)

    def start(self, old=False, port=None, data=None):
        port = port or free_port()
        data = data or str(self.tmp / f"node{port}")
        env = {**os.environ, "NETLITHIC_ADMIN_TOKEN": TOKEN, "NETLITHIC_ALLOW_PRIVATE_PEERS": "1",
               "DRILL_PAUSE_PUBKEY": self.pub, "DRILL_SEC_FORK": str(10 ** 9 if old else 3),
               "NETLITHIC_SYNC_TRIGGER_SECONDS": "1", "NETLITHIC_RATE_LIMIT_LOOPBACK": ""}
        self.procs[port] = subprocess.Popen(
            [sys.executable, LAUNCHER, str(port), "--data-dir", data],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.data = getattr(self, "data", {}); self.data[port] = data
        _wait(lambda: self.alive(port), 20, f"node {port}")
        return port

    def alive(self, port):
        try:
            _req(port, "/status", timeout=1)
            return True
        except Exception:
            return False

    def stop(self, port):
        self.procs[port].terminate(); self.procs[port].wait(timeout=10)

    def close(self):
        for p in self.procs.values():
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=10)
                except Exception:
                    p.kill()

    def height(self, port):
        return _req(port, "/status")[1]["height"]

    def mine(self, port, addr):
        s, b = _req(port, "/mine", {"miner_address": addr}, admin=True, timeout=60)
        assert s == 200, b
        return b["block"]

    def wait_height(self, h, ports=None):
        ports = ports or [p for p in self.ports if p not in self.old]
        _wait(lambda: all(self.height(p) >= h for p in ports), 40, f"height {h} on {ports}")

    def pstate(self, port):
        return _req(port, "/pause_status")[1]


@pytest.fixture
def drill(tmp_path, monkeypatch, wallets):
    key = Ed25519PrivateKey.generate().private_bytes_raw().hex()
    pub = pause.make_pause_tx(key, True, "x", 1, "c")["pubkey"]
    cl = Cluster(tmp_path, pub)
    try:
        yield cl, key, wallets
    finally:
        cl.close()


def test_http_drill_pause_propagates_blocks_payouts_and_survives_restart(drill):
    cl, key, (alice, bob) = drill
    a, b, c = cl.ports
    M = "ab" * 16
    cl.mine(a, alice.address)                       # 1
    cl.wait_height(1)
    cl.mine(b, alice.address)                       # 2
    cl.wait_height(2)
    # --- forged pauses at every node: refused over the wire ---
    attacker = Ed25519PrivateKey.generate().private_bytes_raw().hex()
    for port in cl.ports:
        s, body = _req(port, "/submit_tx", {"tx": pause.make_pause_tx(attacker, True, "attack", 1, CID)})
        assert s == 400, body
        s, _ = _req(port, "/receive_tx", {"tx": pause.make_pause_tx(attacker, True, "attack", 1, CID)})
        assert s != 200
        s, _ = _req(port, "/submit_tx", {"tx": pause.make_pause_tx(key, True, "wrong net", 1, "netlithic-other")})
        assert s == 400
    # --- the real pause, submitted to node A, gossiped to the others ---
    ptx = pause.make_pause_tx(key, True, "DRILL: stop payouts", 1, CID)
    s, body = _req(a, "/submit_tx", {"tx": ptx})
    assert s == 200, body
    _wait(lambda: all(_req(p, "/mempool")[1]["count"] == 1 for p in cl.ports), 20, "mempool gossip")
    # a second pause while one is pending is refused (one at a time)
    s, _ = _req(a, "/submit_tx", {"tx": pause.make_pause_tx(key, True, "dup", 2, CID)})
    assert s == 400
    cl.mine(c, alice.address)                       # 3: first fork block, carries the pause
    cl.wait_height(3)
    for p in cl.ports:
        st = cl.pstate(p)
        assert st["paused"] and st["nonce"] == 1 and st["log"][0]["reason"] == "DRILL: stop payouts"
    # replay of the mined pause is refused everywhere
    for p in cl.ports:
        assert _req(p, "/submit_tx", {"tx": ptx})[0] == 400
    # --- build points, reach the payout heights 7 and 11 ---
    for n in (1, 2, 3):
        s, body = _req(a, "/submit_tx", {"tx": alice.create_transaction(M, NLT, nonce=n)})
        assert s == 200, body
        _wait(lambda: _req(b, "/mempool")[1]["count"] == 1, 15, "tx gossip")
        blk = cl.mine(b, bob.address)               # 4,5,6
        cl.wait_height(blk["index"])
    blk7 = cl.mine(c, bob.address)                  # 7 payout height, paused
    assert blk7["index"] == 7 and not any(t.get("type") == "payout" for t in blk7["transactions"])
    cl.wait_height(7)
    # --- resume ---
    s, body = _req(a, "/submit_tx", {"tx": pause.make_pause_tx(key, False, "DRILL over", 2, CID)})
    assert s == 200, body
    _wait(lambda: _req(c, "/mempool")[1]["count"] == 1, 15, "resume gossip")
    cl.mine(c, alice.address)                       # 8
    cl.wait_height(8)
    assert all(not cl.pstate(p)["paused"] and cl.pstate(p)["nonce"] == 2 for p in cl.ports)
    # --- restart node B: pause log is rebuilt from its own chain file ---
    cl.stop(b)
    nb = cl.start(port=b, data=cl.data[b])
    assert nb == b
    assert cl.pstate(b)["nonce"] == 2 and [e["paused"] for e in cl.pstate(b)["log"]] == [True, False]
    assert cl.height(b) == 8
    # --- payouts come back at height 11 ---
    for n in (4, 5):
        assert _req(a, "/submit_tx", {"tx": alice.create_transaction(M, NLT, nonce=n)})[0] == 200
        _wait(lambda: _req(a, "/mempool")[1]["count"] >= 1, 5, "own mempool")
        blk = cl.mine(a, bob.address)               # 9, 10
        cl.wait_height(blk["index"])
    blk11 = cl.mine(a, bob.address)
    assert blk11["index"] == 11 and any(t.get("type") == "payout" for t in blk11["transactions"])
    cl.wait_height(11)
    tips = {_req(p, "/status")[1]["latest_hash"] for p in cl.ports}
    assert len(tips) == 1


def test_http_drill_old_version_node_splits_off_at_the_pause_block(tmp_path, wallets):
    """A node that runs pre-fork rules (does not know the pause tx) refuses the
    block that carries the pause and stays on the old tip. This is the reason for
    the 4900 version gate; the drill shows what an un-upgraded node looks like."""
    key = Ed25519PrivateKey.generate().private_bytes_raw().hex()
    pub = pause.make_pause_tx(key, True, "x", 1, "c")["pubkey"]
    cl = Cluster(tmp_path, pub, n_new=2, n_old=1)
    try:
        alice, _ = wallets
        a, b = cl.ports[:2]
        old = cl.old[0]
        cl.mine(a, alice.address); cl.mine(a, alice.address)         # 1, 2 : both rule sets agree
        _wait(lambda: cl.height(old) == 2 and cl.height(b) == 2, 20, "all at 2")
        assert _req(a, "/submit_tx", {"tx": pause.make_pause_tx(key, True, "split drill", 1, CID)})[0] == 200
        cl.mine(a, alice.address)                                    # 3 with pause
        cl.wait_height(3)
        time.sleep(8)                                                # give the old node two sync rounds
        assert cl.height(old) == 2, "old node must not accept the pause block"
        assert cl.pstate(a)["paused"] and not cl.pstate(old)["paused"]
        # the old node's own blocks are also refused by the upgraded majority once they diverge
        cl.mine(old, alice.address)                                  # old-rules block 3'
        time.sleep(3)
        assert cl.height(a) == 3 and cl.height(old) == 3
        assert _req(a, "/status")[1]["latest_hash"] != _req(old, "/status")[1]["latest_hash"]
    finally:
        cl.close()
