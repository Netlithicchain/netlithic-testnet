"""Replies are written after the node's locks are released, and each write has a deadline."""
import json
import socket
import threading
import time

import pytest

BIG = b"[" + b"0," * (16 * 1024 * 1024) + b"0]"     # ~32 MiB: far more than socket buffers hold


def _stalled_chain_reader(port):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    s.connect(("127.0.0.1", port))
    s.sendall(b"GET /chain HTTP/1.0\r\nHost: x\r\n\r\n")
    return s        # never read from


def _request(port, raw, t=5):
    s = socket.create_connection(("127.0.0.1", port), timeout=t)
    try:
        s.sendall(raw)
        data = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    finally:
        s.close()
    return int(data.split(b" ", 2)[1])


@pytest.fixture
def serve(node):
    started = []

    def make(**kw):
        srv = node.P2PServer(("127.0.0.1", 0), node.P2PHandler)
        for k, v in kw.items():
            setattr(srv, k, v)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        started.append(srv)
        return srv, srv.server_address[1]
    yield make
    for srv in started:
        srv.shutdown()
        srv.server_close()


def _wait_sending(node, port):
    time.sleep(0.5)     # handler has built the body and is blocked writing it


def test_chain_reader_that_never_reads_does_not_block_status_or_receive_block(node, serve, monkeypatch):
    monkeypatch.setattr(node, "_chain_body", lambda: BIG)
    srv, port = serve()
    stalled = [_stalled_chain_reader(port) for _ in range(3)]
    try:
        _wait_sending(node, port)
        assert node.state_lock.acquire(timeout=2), "state_lock held during a reply write"
        node.state_lock.release()
        t0 = time.monotonic()
        assert _request(port, b"GET /status HTTP/1.0\r\nHost: x\r\n\r\n") == 200
        body = json.dumps({"index": 1, "bogus": True}).encode()
        code = _request(port, b"POST /receive_block HTTP/1.0\r\nHost: x\r\nContent-Type: application/json\r\n"
                        b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        assert 200 <= code < 600
        assert time.monotonic() - t0 < 5
    finally:
        for s in stalled:
            s.close()


def test_reply_write_deadline_frees_the_handler_thread(node, serve, monkeypatch):
    monkeypatch.setattr(node, "_chain_body", lambda: BIG)
    srv, port = serve(reply_deadline_base=1, reply_min_rate=10 ** 12)
    s = _stalled_chain_reader(port)
    try:
        time.sleep(0.3)
        assert srv._per_ip, "the stalled request should hold a slot while sending"
        deadline = time.monotonic() + 6
        while srv._per_ip and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not srv._per_ip, "stalled reply was not cut off by the write deadline"
    finally:
        s.close()


def test_chain_body_cached_per_tip(node):
    with node.state_lock:
        a = node._chain_body()
        b = node._chain_body()
    assert a is b
    d = json.loads(a)
    assert d["length"] == len(node.blockchain.chain) and "chain" in d
    node._chain_cache.clear()
    with node.state_lock:
        assert node._chain_body() == a


def test_normal_requests_never_reach_handle_error(node, serve, monkeypatch):
    srv, port = serve()
    errors = []
    monkeypatch.setattr(srv, "handle_error", lambda req, addr: errors.append(addr))
    for raw in (b"GET /status HTTP/1.0\r\nHost: x\r\n\r\n", b"GET /chain HTTP/1.0\r\nHost: x\r\n\r\n",
                b"GET /nope HTTP/1.0\r\nHost: x\r\n\r\n"):
        _request(port, raw)
    time.sleep(0.3)
    assert errors == []


def test_flush_after_raw_closed_is_quiet():
    import io
    import netutil

    class H:
        server = None
        def _arm_deadline(self, s): pass
        def _cancel_deadline(self): pass
    raw = io.BytesIO()
    w = netutil._DeadlineWriter(H(), raw)
    raw.close()
    assert w.flush() is None
    w.close()


def test_one_deadline_per_reply(node, serve, monkeypatch):
    import netutil
    srv, port = serve()
    armed = []
    orig = netutil._DeadlineHandler._arm_deadline
    monkeypatch.setattr(netutil._DeadlineHandler, "_arm_deadline", lambda self, s: armed.append(s) or orig(self, s))
    _request(port, b"GET /chain HTTP/1.0\r\nHost: x\r\n\r\n")
    assert len(armed) == 2      # header deadline + one reply deadline
