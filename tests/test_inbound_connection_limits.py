"""Inbound HTTP connections: idle clients are dropped and concurrency is capped."""
import json
import socket
import threading
import time
import urllib.request

import pytest


@pytest.fixture
def small_server(node, monkeypatch):
    monkeypatch.setattr(node.P2PHandler, "timeout", 1)
    srv = node.P2PServer(("127.0.0.1", 0), node.P2PHandler, max_connections=3, reserved_slots=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def _status(port):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=5) as r:
        return r.status, json.loads(r.read())


def test_handler_has_a_read_timeout(node):
    assert node.P2PHandler.timeout == node.INBOUND_READ_TIMEOUT_SECONDS
    assert 0 < node.INBOUND_READ_TIMEOUT_SECONDS <= 60
    assert node.MAX_INBOUND_CONNECTIONS >= 64


def test_silent_client_is_dropped(small_server):
    _, port = small_server
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    s.sendall(b"GET /status HTTP/1.1\r\nHost: x\r\n")   # never finishes headers
    t0 = time.monotonic()
    try:
        data = s.recv(100)
    except OSError:
        data = b""
    s.close()
    assert time.monotonic() - t0 < 5 and not data.startswith(b"HTTP/1.1 200")


def test_extra_connections_are_closed_and_slots_come_back(small_server):
    _, port = small_server
    held = [socket.create_connection(("127.0.0.1", port), timeout=10) for _ in range(3)]
    time.sleep(0.2)
    extra = socket.create_connection(("127.0.0.1", port), timeout=10)
    extra.sendall(b"GET /status HTTP/1.0\r\n\r\n")
    try:
        got = extra.recv(100)
    except OSError:
        got = b""
    extra.close()
    assert got == b""                       # refused while all slots are busy
    for s in held:
        s.close()
    srv = small_server[0]
    end = time.monotonic() + 5
    while srv._slots._value < 3 and time.monotonic() < end:
        time.sleep(0.05)
    assert srv._slots._value == 3           # slots are released
    assert _status(port)[0] == 200
    for _ in range(10):                     # and stay balanced over many requests
        assert _status(port)[0] == 200


def test_normal_requests_still_work_on_the_node(node):
    with urllib.request.urlopen(f"{node.self_url()}/status", timeout=5) as r:
        assert r.status == 200
