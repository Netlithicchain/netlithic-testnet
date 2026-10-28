import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from conftest import TEST_ADMIN_TOKEN, free_port, post


# Subprocess nodes run the shipped defaults (registry on), so the in-process node
# must too, or the handshake rightly refuses a different rules fingerprint.
pytestmark = pytest.mark.registry


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return json.loads(r.read())


def test_hello_adds_a_verified_peer_without_admin(node, node_procs, monkeypatch):
    b = node_procs()
    monkeypatch.setattr(node, "is_admin_request", lambda ip, h: False)  # caller is "remote"
    status, body = post(node, "/hello", {"url": f"http://127.0.0.1:{b}"})
    assert status == 200 and body["message"] == "Peer added"
    assert f"http://127.0.0.1:{b}" in node.peers


def test_hello_rejects_unreachable_url(node):
    status, body = post(node, "/hello", {"url": f"http://127.0.0.1:{free_port()}"})
    assert status == 400 and "unreachable" in body["error"]
    assert not node.peers


def test_hello_rejects_url_whose_status_names_someone_else(node):
    class Liar(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"node": "http://10.9.9.9:8000"}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Liar)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        status, body = post(node, "/hello", {"url": f"http://127.0.0.1:{srv.server_address[1]}"})
        assert status == 400 and "different URL" in body["error"]
        assert not node.peers
    finally:
        srv.shutdown()


@pytest.mark.parametrize("bad", ["ftp://x", "http://u:p@x:1", None, 5])
def test_hello_rejects_bad_urls(node, bad):
    assert post(node, "/hello", {"url": bad})[0] == 400


def test_hello_rejects_self(node):
    status, body = post(node, "/hello", {"url": node.self_url()})
    assert status == 400 and "self" in body["error"]


def test_hello_respects_peer_cap(node, node_procs, monkeypatch):
    b = node_procs()
    monkeypatch.setattr(node, "MAX_PEERS", 0)
    status, body = post(node, "/hello", {"url": f"http://127.0.0.1:{b}"})
    assert status == 400 and "limit" in body["error"]


def test_add_peer_triggers_add_back_on_the_other_node(node_procs):
    a, b = node_procs(), node_procs()
    req = urllib.request.Request(f"http://127.0.0.1:{a}/add_peer", method="POST",
                                 headers={"X-Admin-Token": TEST_ADMIN_TOKEN},
                                 data=json.dumps({"peer": f"http://127.0.0.1:{b}"}).encode())
    urllib.request.urlopen(req, timeout=10).read()
    for _ in range(50):
        if f"http://127.0.0.1:{a}" in get(b, "/peers")["peers"]:
            break
        time.sleep(0.1)
    assert f"http://127.0.0.1:{a}" in get(b, "/peers")["peers"]


def test_peer_flag_and_advertise_url(node_procs):
    b = node_procs()
    a = node_procs("--peer", f"http://127.0.0.1:{b}", "--advertise-url", "http://localhost:1")
    st = get(a, "/status")
    assert st["peers"] == [f"http://127.0.0.1:{b}"]
    assert st["node"] == "http://localhost:1"


def test_peer_flag_at_startup_makes_both_sides_peers(node_procs):
    b = node_procs()
    a = node_procs("--peer", f"http://127.0.0.1:{b}")
    for _ in range(50):
        if f"http://127.0.0.1:{a}" in get(b, "/peers")["peers"]:
            break
        time.sleep(0.1)
    assert f"http://127.0.0.1:{a}" in get(b, "/peers")["peers"]
