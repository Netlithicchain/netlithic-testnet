import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import explorer
import faucet
import webwallet
from node_pool import NodePool, NoNodeReachable, parse_nodes


def fake_node(name, status=200, height=10):
    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body):
            b = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            seen.append(("GET", self.path))
            self._send(status, {"node": name, "height": height, "chain_id": "x"})

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            seen.append(("POST", self.path, json.loads(self.rfile.read(n) or b"{}")))
            if self.path == "/reject":
                return self._send(400, {"error": "bad tx"})
            self._send(status, {"ok": True, "node": name})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}", seen


def dead_url():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


@pytest.fixture
def nodes():
    a, ua, sa = fake_node("A")
    b, ub, sb = fake_node("B")
    yield ua, sa, ub, sb
    a.shutdown(); b.shutdown()


def test_parse_nodes_dedupes_and_skips_blank():
    assert parse_nodes("http://a/, http://b", "", None, "http://a") == ["http://a", "http://b"]


def test_primary_used_when_up(nodes):
    ua, sa, ub, sb = nodes
    code, body = NodePool([ua, ub]).request("/status")
    assert (code, body["node"]) == (200, "A") and not sb


def test_falls_back_when_primary_down(nodes):
    ua, sa, ub, sb = nodes
    pool = NodePool([dead_url(), ub], timeout=2)
    code, body = pool.request("/status")
    assert body["node"] == "B" and pool.last_used == ub


def test_falls_back_on_5xx():
    s1, u1, _ = fake_node("A", status=503)
    s2, u2, _ = fake_node("B")
    try:
        assert NodePool([u1, u2]).request("/status")[1]["node"] == "B"
    finally:
        s1.shutdown(); s2.shutdown()


def test_4xx_is_not_retried_elsewhere(nodes):
    ua, sa, ub, sb = nodes
    code, body = NodePool([ua, ub]).request("/reject", {"tx": 1})
    assert code == 400 and body["error"] == "bad tx" and not sb


def test_broadcast_goes_to_reachable_node(nodes):
    ua, sa, ub, sb = nodes
    code, body = NodePool([dead_url(), ub], timeout=2).request("/receive_tx", {"tx": {"n": 1}})
    assert code == 200 and sb[-1] == ("POST", "/receive_tx", {"tx": {"n": 1}})


def test_all_down_raises():
    with pytest.raises(NoNodeReachable):
        NodePool([dead_url(), dead_url()], timeout=1).request("/status")


def test_down_node_rests_then_primary_tried_again(nodes):
    ua, sa, ub, sb = nodes
    t = [1000.0]
    pool = NodePool([ua, ub], cooldown=30, clock=lambda: t[0])
    pool._mark(ua, False)
    assert pool.request("/status")[1]["node"] == "B"
    t[0] += 31
    assert pool.request("/status")[1]["node"] == "A"


def test_webwallet_upstream_falls_back(nodes):
    ua, sa, ub, sb = nodes
    up = webwallet.Upstream(dead_url(), "", timeout=2, fallback=ub)
    assert up.node_call("/status")[1]["node"] == "B"
    assert up.node_call("/receive_tx", {"tx": {}})[1]["node"] == "B"


def test_faucet_client_falls_back(nodes):
    ua, sa, ub, sb = nodes
    c = faucet.NodeClient(dead_url(), ub)
    assert c.status()["node"] == "B"
    assert c.submit({"a": 1})[0] == 200 and sb[-1][1] == "/submit_tx"


def test_explorer_reader_falls_back(nodes):
    ua, sa, ub, sb = nodes
    r = explorer.NodeReader(dead_url(), cache_seconds=0, fallback=ub)
    assert r.status()["node"] == "B"


def test_cli_default_fallback_is_node2():
    import node_pool
    assert node_pool.DEFAULT_FALLBACK == "http://157.230.63.97:8000"
