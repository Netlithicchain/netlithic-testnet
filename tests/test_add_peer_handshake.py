"""/add_peer keeps only peers that actually answered."""
import socket

from conftest import free_port, post


def test_add_peer_that_never_answers_is_not_kept(node, monkeypatch):
    monkeypatch.setattr(node, "HELLO_TIMEOUT_SECONDS", 1)
    hang = socket.socket()
    hang.bind(("127.0.0.1", 0))
    hang.listen(4)  # accepts the connection but never replies
    url = f"http://127.0.0.1:{hang.getsockname()[1]}"
    try:
        code, body = post(node, "/add_peer", {"peer": url})
    finally:
        hang.close()
    assert code == 400 and url not in node.peers


def test_add_peer_that_is_down_is_not_kept(node):
    url = f"http://127.0.0.1:{free_port()}"
    code, _ = post(node, "/add_peer", {"peer": url})
    assert code == 400 and url not in node.peers


def _drip_server(gap, slow_headers=False):
    """Answers with HTTP headers, then sends the body one byte every `gap` seconds.
    With slow_headers the status line and headers also come one byte at a time."""
    import threading
    import time
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)

    def serve():
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        try:
            conn.recv(4096)
            body = b'{"height": 1, "chain_id": "x", "version": "0.5.4"}'
            head = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n" % len(body)
            if not slow_headers:
                conn.sendall(head)
            for b in (head + body if slow_headers else body):
                conn.sendall(bytes([b]))
                time.sleep(gap)
        except OSError:
            pass
        finally:
            conn.close()
    threading.Thread(target=serve, daemon=True).start()
    return srv


def test_add_peer_slow_drip_is_bounded_by_the_whole_call(node, monkeypatch):
    import time
    monkeypatch.setattr(node, "HELLO_TIMEOUT_SECONDS", 1)
    srv = _drip_server(0.3)  # each byte well inside the per-read limit, the whole body far beyond it
    url = f"http://127.0.0.1:{srv.getsockname()[1]}"
    t0 = time.monotonic()
    try:
        code, _ = post(node, "/add_peer", {"peer": url})
    finally:
        srv.close()
    assert time.monotonic() - t0 < 4
    assert code == 400 and url not in node.peers


def test_get_json_deadline_allows_a_normal_reply(node):
    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            b = _json.dumps({"ok": 1}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    try:
        assert node._get_json(f"http://127.0.0.1:{srv.server_address[1]}/x", timeout=2, deadline_seconds=2) == {"ok": 1}
    finally:
        srv.server_close()


def test_add_peer_slow_headers_are_bounded_by_the_whole_call(node, monkeypatch):
    import time
    monkeypatch.setattr(node, "HELLO_TIMEOUT_SECONDS", 1)
    srv = _drip_server(0.3, slow_headers=True)  # the status line and headers alone would take ~25 s
    url = f"http://127.0.0.1:{srv.getsockname()[1]}"
    t0 = time.monotonic()
    try:
        code, _ = post(node, "/add_peer", {"peer": url})
    finally:
        srv.close()
    assert time.monotonic() - t0 < 4
    assert code == 400 and url not in node.peers


def test_get_json_deadline_covers_response_headers(node):
    import time

    import pytest
    srv = _drip_server(0.3, slow_headers=True)
    url = f"http://127.0.0.1:{srv.getsockname()[1]}/status"
    t0 = time.monotonic()
    try:
        with pytest.raises(Exception):
            node._get_json(url, timeout=2, deadline_seconds=1)
    finally:
        srv.close()
    assert time.monotonic() - t0 < 2.5


def test_get_json_without_deadline_is_unchanged(node):
    srv = _drip_server(0.0)
    try:
        assert node._get_json(f"http://127.0.0.1:{srv.getsockname()[1]}/status", timeout=2)["height"] == 1
    finally:
        srv.close()


def _timed_hello(node, monkeypatch, slow_headers):
    import time
    monkeypatch.setattr(node, "HELLO_TIMEOUT_SECONDS", 1)
    srv = _drip_server(0.3, slow_headers=slow_headers)
    url = f"http://127.0.0.1:{srv.getsockname()[1]}"
    t0 = time.monotonic()
    try:
        code, _ = post(node, "/hello", {"url": url})
    finally:
        srv.close()
    return time.monotonic() - t0, code, url


def test_hello_slow_body_is_bounded_by_the_whole_call(node, monkeypatch):
    took, code, url = _timed_hello(node, monkeypatch, slow_headers=False)
    assert took < 4 and code == 400 and url not in node.peers


def test_hello_slow_headers_are_bounded_by_the_whole_call(node, monkeypatch):
    took, code, url = _timed_hello(node, monkeypatch, slow_headers=True)
    assert took < 4 and code == 400 and url not in node.peers


def test_sync_and_discovery_fetches_have_a_whole_request_limit(node, monkeypatch):
    seen = {}

    def fake(url, timeout=5, deadline_seconds=None):
        seen[url.rsplit("/", 1)[1]] = deadline_seconds
        return {}
    monkeypatch.setattr(node, "_get_json", fake)
    node._sync_with_peer("http://peer.example:8000")
    node._discover_peers_from("http://peer.example:8000")
    assert seen["chain"] == node.CHAIN_FETCH_DEADLINE_SECONDS >= 60
    assert seen["peers"] == node.PEERS_FETCH_DEADLINE_SECONDS >= 10


def test_large_chain_reply_fits_in_the_chain_limit(node):
    import json as _json
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, HTTPServer
    payload = _json.dumps({"chain": [{"index": i, "pad": "x" * 900} for i in range(15000)]}).encode()
    assert len(payload) > 13 * 1024 * 1024 or len(payload) > 13_000_000

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    t0 = time.monotonic()
    try:
        got = node._get_json(f"http://127.0.0.1:{srv.server_address[1]}/chain",
                             deadline_seconds=node.CHAIN_FETCH_DEADLINE_SECONDS)
    finally:
        srv.server_close()
    assert len(got["chain"]) == 15000 and time.monotonic() - t0 < node.CHAIN_FETCH_DEADLINE_SECONDS
