"""Reserved connection slots go only to configured addresses, and trusted
addresses are still capped (adapted from the R1e review checks)."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import netutil
from tests.test_inbound_slow_clients import _conn, _status, serve  # noqa: F401


def _free(srv):
    return srv._slots._value, (srv._reserved._value if srv._reserved else 0)


def test_a_peer_added_through_hello_gets_no_reserved_slots(node, serve, monkeypatch):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            b = json.dumps({"height": 1}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def log_message(self, *a):
            pass
    other = HTTPServer(("127.0.0.2", 0), H)
    threading.Thread(target=other.serve_forever, daemon=True).start()
    monkeypatch.setattr(node, "peer_address_allowed", lambda u: True)
    monkeypatch.setattr(node, "_gate_check_url", lambda u, strict=False: None)
    monkeypatch.setattr(node, "_introduce_to", lambda u: None)
    monkeypatch.setattr(node, "_builtin_seeds", lambda: [])
    try:
        with node.state_lock:
            assert node._try_add_peer(f"http://127.0.0.2:{other.server_address[1]}", address_ok=True) == "added"
        ips = node._reserved_ips()
        assert "127.0.0.2" not in ips
        srv, port = serve(max_connections=64, max_per_ip=12, reserved_slots=8, reserved_ips=ips)
        held = []
        for _ in range(80):
            try:
                s = _conn(port, "127.0.0.2")
                s.sendall(b"GET /status HTTP/1.0\r\n")
                held.append(s)
            except OSError:
                pass
        time.sleep(0.5)
        try:
            assert srv._per_ip.get("127.0.0.2", 0) <= 12
            assert _free(srv)[1] == 8                   # reserved pool untouched
            assert _status(port, "127.0.0.3") == 200
        finally:
            for s in held:
                s.close()
    finally:
        other.shutdown()
        other.server_close()


def test_configured_peers_and_seeds_are_reserved(node, monkeypatch):
    monkeypatch.setattr(node, "CONFIGURED_PEER_URLS", {"http://192.0.2.20:8000"})
    monkeypatch.setattr(node, "_builtin_seeds", lambda: ["http://198.51.100.30:8000"])
    ips = node._reserved_ips()
    assert {"192.0.2.20", "198.51.100.30", "127.0.0.1"} <= ips


def test_builtin_seed_list_is_used():
    import easy_node
    import p2p_http
    assert p2p_http._builtin_seeds() == list(easy_node.SEEDS)


def _wait(cond, limit=3.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < limit:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def test_trusted_address_is_capped_and_uses_reserved_only_past_its_share(node, serve):
    assert node.MAX_CONNECTIONS_PER_TRUSTED_IP == 16
    srv, port = serve(max_connections=64, max_per_ip=12, reserved_slots=8, reserved_ips={"127.0.0.5"})
    held = []
    try:
        for i in range(30):
            try:
                s = _conn(port, "127.0.0.5")
                s.sendall(b"GET /status HTTP/1.0\r\n")
                held.append(s)
            except OSError:
                pass
            # pace: wait until the server has counted this one (or refused it past the cap)
            _wait(lambda: srv._per_ip.get("127.0.0.5", 0) >= min(i + 1, 16), 1.0)
        assert _wait(lambda: srv._per_ip.get("127.0.0.5", 0) == 16)
        general_free, reserved_free = _free(srv)
        assert (56 - general_free) + (8 - reserved_free) == 16     # every held slot is accounted for
        assert 8 - reserved_free <= 4                               # at most its share past 12
        assert _status(port, "127.0.0.6") == 200
    finally:
        for s in held:
            s.close()


def test_trusted_address_falls_back_to_reserved_when_the_open_pool_is_full(node, serve):
    srv, port = serve(max_connections=8, max_per_ip=12, reserved_slots=2, reserved_ips={"127.0.0.5"})
    held = []
    for _ in range(6):
        s = _conn(port, "127.0.0.1")
        s.sendall(b"GET /status HTTP/1.0\r\n")
        held.append(s)
    time.sleep(0.3)
    try:
        assert _status(port, "127.0.0.5") == 200
    finally:
        for s in held:
            s.close()


def test_ipv4_mapped_addresses_count_as_ipv4():
    assert netutil.normalize_ip("::ffff:1.2.3.4") == "1.2.3.4"
    assert netutil.normalize_ip("1.2.3.4") == "1.2.3.4"
    assert netutil.normalize_ip("2001:db8::1") == "2001:db8::1"
    assert netutil.normalize_ip("junk") == "junk"


def test_ipv4_mapped_shares_one_counter_and_reserved_set_is_normalized(node):
    srv = netutil.CappedThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler,
                                            max_connections=8, max_per_ip=2, reserved_ips={"::ffff:9.9.9.9"})
    try:
        assert "9.9.9.9" in srv.reserved_ips
        assert srv._take("1.2.3.4") and srv._take("::ffff:1.2.3.4")
        assert list(srv._per_ip) == ["1.2.3.4"] and srv._per_ip["1.2.3.4"] == 2
        assert srv._take("::ffff:1.2.3.4") is None
    finally:
        srv.server_close()
