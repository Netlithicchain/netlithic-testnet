"""New blocks go to all peers at the same time: a slow or dead peer must not delay the others, and it is all bounded."""
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class _Peer:
    """A tiny local peer. delay=None answers at once; a number holds the answer back that many seconds."""

    def __init__(self, delay=None, gate=None):
        outer = self
        self.received = []          # (monotonic time, block hash)
        self.delay, self.gate = delay, gate

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                outer.received.append((time.monotonic(), body.get("block", {}).get("hash")))
                if outer.gate is not None:
                    outer.gate.wait(30)
                elif outer.delay:
                    time.sleep(outer.delay)
                try:
                    out = b"{}"
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                except OSError:
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def relay(node, monkeypatch):
    p2p = node
    monkeypatch.setattr(p2p, "RELAY_PEER_TIMEOUT", 1)
    monkeypatch.setattr(p2p, "RELAY_DEADLINE", 3)
    made, gates = [], []

    def peer(**kw):
        pr = _Peer(**kw)
        made.append(pr)
        return pr

    p2p.peers.clear()
    yield p2p, peer, gates
    for g in gates:
        g.set()
    for pr in made:
        pr.close()
    p2p.peers.clear()


def _dead_url():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                       # nothing listens here any more
    return f"http://127.0.0.1:{port}"


BLOCK = {"hash": "ab" * 32, "index": 7}


def test_relay_slow_and_dead_peers_do_not_delay_the_rest(relay):
    p2p, peer, gates = relay
    gate = threading.Event()
    gates.append(gate)
    slow = peer(gate=gate)
    good = [peer() for _ in range(4)]
    p2p.peers.update([slow.url, _dead_url()] + [g.url for g in good])
    t0 = time.monotonic()
    p2p._broadcast_block(BLOCK)
    took = time.monotonic() - t0
    for g in good:
        assert g.received, "a healthy peer did not get the block"
        assert g.received[0][0] - t0 < 0.9          # they were served straight away, not after the slow one
    assert slow.received                             # the slow peer was tried too
    assert took < p2p.RELAY_PEER_TIMEOUT + 1.5       # the call ends at the per-peer timeout, not the 30 s gate


def test_relay_excluded_peer_is_skipped_and_block_marked_seen(relay):
    p2p, peer, _ = relay
    a, b = peer(), peer()
    p2p.peers.update([a.url, b.url])
    p2p._broadcast_block(BLOCK, exclude_peer=a.url)
    assert not a.received and b.received
    assert p2p._already_seen_block(BLOCK["hash"])


def test_relay_is_bounded_by_workers_deadline_and_pending_cap(relay, monkeypatch):
    p2p, peer, gates = relay
    monkeypatch.setattr(p2p, "RELAY_WORKERS", 4)
    monkeypatch.setattr(p2p, "_relay_pool", None)           # build a fresh, small pool for this test
    monkeypatch.setattr(p2p, "RELAY_MAX_PENDING", 10)
    monkeypatch.setattr(p2p, "RELAY_PEER_TIMEOUT", 1)
    monkeypatch.setattr(p2p, "RELAY_DEADLINE", 1.5)
    gate = threading.Event()
    gates.append(gate)
    stuck = [peer(gate=gate) for _ in range(30)]
    p2p.peers.update(s.url for s in stuck)
    t0 = time.monotonic()
    p2p._broadcast_block(BLOCK)
    took = time.monotonic() - t0
    assert took < 1.5 + 1.0                                  # never waits longer than the deadline
    workers = [t for t in threading.enumerate() if t.name.startswith("relay")]
    assert 0 < len(workers) <= 4                              # bounded threads
    tried = sum(1 for s in stuck if s.received)
    assert tried <= 10                                        # the cap on queued sends held
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and p2p._relay_pending:
        time.sleep(0.1)
    assert p2p._relay_pending == 0                            # nothing leaks once the timeouts have passed


def test_relay_still_delivers_to_a_normal_peer(relay):
    p2p, peer, _ = relay
    a = peer()
    p2p.peers.add(a.url)
    p2p._broadcast_block(BLOCK)
    assert [h for _, h in a.received] == [BLOCK["hash"]]


def test_relay_logs_how_many_peers_were_skipped_at_the_pending_cap(relay, monkeypatch, capsys):
    p2p, peer, gates = relay
    monkeypatch.setattr(p2p, "RELAY_WORKERS", 2)
    monkeypatch.setattr(p2p, "_relay_pool", None)
    monkeypatch.setattr(p2p, "RELAY_MAX_PENDING", 3)
    monkeypatch.setattr(p2p, "RELAY_PEER_TIMEOUT", 1)
    monkeypatch.setattr(p2p, "RELAY_DEADLINE", 1.2)
    monkeypatch.setattr(p2p, "_relay_skipped_total", 0)
    gate = threading.Event()
    gates.append(gate)
    stuck = [peer(gate=gate) for _ in range(8)]
    p2p.peers.update(s.url for s in stuck)
    p2p._broadcast_block({"hash": "cd" * 32, "index": 9})
    out = capsys.readouterr().out
    assert "skipped 5 of 8 peers for block 9" in out          # 3 sent, 5 left over, and the log says so
    assert p2p._relay_skipped_total >= 5


def test_relay_logs_nothing_about_skips_when_every_peer_was_served(relay, capsys, monkeypatch):
    p2p, peer, _ = relay
    monkeypatch.setattr(p2p, "_relay_skipped_total", 0)
    a, b = peer(), peer()
    p2p.peers.update([a.url, b.url])
    p2p._broadcast_block(BLOCK)
    assert "skipped" not in capsys.readouterr().out
    assert p2p._relay_skipped_total == 0


def test_relay_skip_count_leaves_the_excluded_peer_out(relay, monkeypatch, capsys):
    p2p, peer, gates = relay
    monkeypatch.setattr(p2p, "RELAY_MAX_PENDING", 1)
    monkeypatch.setattr(p2p, "RELAY_DEADLINE", 1.2)
    monkeypatch.setattr(p2p, "_relay_skipped_total", 0)
    gate = threading.Event()
    gates.append(gate)
    stuck = [peer(gate=gate) for _ in range(4)]
    p2p.peers.update(s.url for s in stuck)
    p2p._broadcast_block({"hash": "ef" * 32, "index": 11}, exclude_peer=stuck[0].url)
    assert "skipped 2 of 3 peers for block 11" in capsys.readouterr().out
