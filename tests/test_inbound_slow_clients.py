"""Inbound HTTP: per-address cap, reserved slots, and total time limits for
headers and body (adapted from the R1d review checks)."""
import json
import socket
import threading
import time
import urllib.request

import pytest


def _conn(port, src="127.0.0.1"):
    return socket.create_connection(("127.0.0.1", port), timeout=10, source_address=(src, 0))


def _status(port, src="127.0.0.1", t=3):
    s = _conn(port, src)
    s.settimeout(t)
    try:
        s.sendall(b"GET /status HTTP/1.0\r\nHost: x\r\n\r\n")
        data = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    except OSError as e:
        return type(e).__name__
    finally:
        s.close()
    return int(data.split(b" ", 2)[1]) if data.startswith(b"HTTP/") else "closed"


def _closed_within(sock, limit, drip=None, gap=0.3):
    """Drip bytes until the server closes the connection; return seconds taken (or None)."""
    sock.settimeout(gap)
    t0 = time.monotonic()
    while time.monotonic() - t0 < limit:
        try:
            if drip is not None:
                sock.sendall(drip)
            if sock.recv(4096) == b"":
                return time.monotonic() - t0
        except socket.timeout:
            continue
        except OSError:
            return time.monotonic() - t0
    return None


@pytest.fixture
def serve(node):
    started = []

    class Server(node.P2PServer):
        request_queue_size = 512        # used by listen() during bind, so set on the class

    def make(**kw):
        srv = Server(("127.0.0.1", 0), node.P2PHandler, **kw)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        started.append(srv)
        return srv, srv.server_address[1]
    yield make
    for srv in started:
        srv.shutdown()
        srv.server_close()


def test_trickle_from_one_address_does_not_lock_out_others(node, serve, monkeypatch):
    monkeypatch.setattr(node.P2PHandler, "timeout", 4)
    srv, port = serve(max_connections=32, reserved_slots=0)
    socks = []
    for _ in range(40):
        try:
            c = _conn(port)
            c.sendall(b"GET /status HTTP/1.0\r\nX-a: ")
            socks.append(c)
        except OSError:
            pass
    stop = time.monotonic() + 8

    def drip():
        while time.monotonic() < stop:
            for c in socks:
                try:
                    c.sendall(b"a")
                except OSError:
                    pass
            time.sleep(1.5)
    threading.Thread(target=drip, daemon=True).start()
    time.sleep(1)
    try:
        results = [_status(port, src="127.0.0.2") for _ in range(4) if not time.sleep(1)]
        assert results == [200] * 4
        assert srv._per_ip.get("127.0.0.1", 0) <= node.MAX_CONNECTIONS_PER_IP
    finally:
        for c in socks:
            c.close()


def test_per_address_cap_is_12_and_allows_a_busy_nat(node, serve):
    assert node.MAX_CONNECTIONS_PER_IP == 12
    srv, port = serve()
    held = [_conn(port) for _ in range(11)]
    for c in held:
        c.sendall(b"GET /status HTTP/1.0\r\n")      # still sending headers
    time.sleep(0.3)
    try:
        assert _status(port) == 200                  # the 12th from the same address is served
        extra = [_conn(port) for _ in range(2)]      # 12 open + 1 more is refused
        time.sleep(0.3)
        assert any(_closed_within(c, 2) is not None for c in extra)
        for c in extra:
            c.close()
    finally:
        for c in held:
            c.close()
    time.sleep(0.5)
    assert _status(port) == 200


def test_per_address_cap_is_configurable(node, monkeypatch):
    import importlib
    monkeypatch.setenv("NETLITHIC_MAX_CONNECTIONS_PER_IP", "20")
    assert node._env_int("NETLITHIC_MAX_CONNECTIONS_PER_IP", 12) == 20
    monkeypatch.setenv("NETLITHIC_MAX_CONNECTIONS_PER_IP", "junk")
    assert node._env_int("NETLITHIC_MAX_CONNECTIONS_PER_IP", 12) == 12
    srv = node.P2PServer(("127.0.0.1", 0), node.P2PHandler, max_per_ip=3)
    assert srv.max_per_ip == 3
    srv.server_close()


def test_reserved_slots_keep_a_trusted_peer_in(node, serve):
    srv, port = serve(max_connections=8, reserved_slots=2, reserved_ips={"127.0.0.3"})
    held = []
    for src in ("127.0.0.1", "127.0.0.2"):
        for _ in range(3):
            c = _conn(port, src)
            c.sendall(b"GET /status HTTP/1.0\r\n")
            held.append(c)
    time.sleep(0.3)
    try:
        assert _status(port, src="127.0.0.4") != 200     # the open pool is full
        assert _status(port, src="127.0.0.3") == 200     # a configured peer still gets in
    finally:
        for c in held:
            c.close()


def test_reserved_ips_include_loopback_env_and_peers(node, monkeypatch):
    monkeypatch.setenv("NETLITHIC_RESERVED_IPS", "203.0.113.7, 198.51.100.2")
    monkeypatch.setattr(node, "CONFIGURED_PEER_URLS", {"http://192.0.2.10:8000"})
    monkeypatch.setattr(node, "peers", {"http://192.0.2.10:8000", "http://192.0.2.99:8000"})
    ips = node._reserved_ips()
    assert {"127.0.0.1", "::1", "203.0.113.7", "198.51.100.2", "192.0.2.10"} <= ips
    assert "192.0.2.99" not in ips                     # learned at run time: not reserved


def test_header_trickle_is_cut_at_the_header_deadline(node, serve, monkeypatch):
    monkeypatch.setattr(node, "HEADER_DEADLINE_SECONDS", 1.5)
    srv, port = serve()
    c = _conn(port)
    c.sendall(b"GET /status HTTP/1.0\r\nX-a: ")
    took = _closed_within(c, 8, drip=b"a")           # well inside the 20 s idle limit
    c.close()
    assert took is not None and took < 4


def test_body_trickle_is_cut_at_the_body_deadline(node, serve, monkeypatch):
    monkeypatch.setattr(node, "BODY_DEADLINE_BASE_SECONDS", 1.5)
    srv, port = serve()
    c = _conn(port)
    c.sendall(b"POST /receive_block HTTP/1.0\r\nContent-Type: application/json\r\nContent-Length: 5000\r\n\r\n{")
    took = _closed_within(c, 8, drip=b" ")
    c.close()
    assert took is not None and took < 4


def test_body_deadline_grows_with_size(node):
    base, rate = node.BODY_DEADLINE_BASE_SECONDS, node.BODY_MIN_BYTES_PER_SECOND
    assert node.HEADER_DEADLINE_SECONDS == 10 and base == 10 and rate == 64 * 1024
    assert base + node.MAX_POST_BYTES / rate == 26     # a full 1 MiB body may take 26 s


def test_a_large_body_at_normal_speed_is_handled(node, serve):
    srv, port = serve()
    body = json.dumps({"block": {"pad": "x" * 900_000}}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/receive_block", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    assert code in (200, 400)                         # a normal answer, not a dropped connection


def test_slow_reply_download_is_not_cut_by_the_body_deadline(node, serve, monkeypatch):
    monkeypatch.setattr(node, "BODY_DEADLINE_BASE_SECONDS", 0.5)

    def slow_status(url, timeout=5, deadline_seconds=None):
        time.sleep(2)                                 # the handler works longer than the body limit
        raise OSError("down")
    monkeypatch.setattr(node, "_get_json", slow_status)
    monkeypatch.setattr(node, "hello_address_allowed", lambda url, ip: (True, ""), raising=False)
    srv, port = serve()
    body = json.dumps({"url": "http://127.0.0.1:1"}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/hello", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    assert code == 400                                # the handler finished and answered


def test_slots_and_counts_return_after_abusive_connections(node, serve, monkeypatch):
    monkeypatch.setattr(node.P2PHandler, "timeout", 1)
    srv, port = serve(max_connections=8, reserved_slots=0, max_per_ip=8)
    for i in range(40):
        c = _conn(port)
        kind = i % 6
        if kind == 0:
            c.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\0\0\0\0\0\0\0")
        elif kind == 1:
            c.sendall(b"\x00\xff garbage\r\n\r\n")
        elif kind == 2:
            c.sendall(b"GET /status HTTP/1.0\r\nX: y\r\n")
        elif kind == 3:
            c.sendall(b"POST /receive_block HTTP/1.0\r\nContent-Length: 5\r\n\r\n{bad}")
        elif kind == 4:
            c.sendall(b"POST /receive_block HTTP/1.0\r\nContent-Length: 999999999\r\n\r\nx")
        else:
            c.sendall(b"GET /block?index=" + b"9" * 5000 + b" HTTP/1.0\r\n\r\n")
        c.close()
    time.sleep(2.5)
    assert srv._slots._value == 8 and srv._per_ip == {} and srv._held == {}
    assert _status(port) == 200
