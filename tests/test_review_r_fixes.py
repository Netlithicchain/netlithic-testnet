"""Second review round: R1 (peer address gate before any fetch), R2 (DNS
rebinding: connected address re-checked), R3 (no redirects), LRU trigger
table, R4 (missing checkpoint file warns), R5 (checkpoints cached)."""
import importlib
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.request import Request

import pytest

import core
import netutil


@pytest.fixture
def strict_p2p(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NETLITHIC_ALLOW_PRIVATE_PEERS", raising=False)
    import p2p_http
    p = importlib.reload(p2p_http)
    yield p
    monkeypatch.setenv("NETLITHIC_ALLOW_PRIVATE_PEERS", "1")
    importlib.reload(p2p_http)


class _Counting(BaseHTTPRequestHandler):
    hits = 0

    def do_GET(self):
        type(self).hits += 1
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.end_headers()
            return
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def local_server():
    _Counting.hits = 0
    srv = HTTPServer(("127.0.0.1", 0), _Counting)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


# R1 -------------------------------------------------------------------------

def test_try_add_peer_refuses_private(strict_p2p):
    for bad in ("http://127.0.0.1:8123", "http://10.0.0.5:8000", "http://169.254.169.254:80",
                "http://[::1]:8123", "http://192.168.1.1:8000"):
        with pytest.raises(ValueError, match="not allowed"):
            strict_p2p._try_add_peer(bad)
    assert strict_p2p.peers == set()


def test_hello_from_private_url_never_fetched(strict_p2p, local_server):
    # Drive the real handler with a fake request object.
    sent = {}

    class H:
        headers = {}
    h = H()
    orig = strict_p2p._json_response
    strict_p2p._json_response = lambda s, code, body: sent.update(code=code, body=body)
    try:
        strict_p2p.P2PHandler.handle_hello(h, {"url": local_server, "version": "9.9.9"})
    finally:
        strict_p2p._json_response = orig
    assert sent["code"] == 400 and "not allowed" in sent["body"]["error"]
    assert _Counting.hits == 0  # gate ran before any fetch
    assert local_server not in strict_p2p.peers


def test_add_peer_admin_refuses_private_before_fetch(strict_p2p, local_server):
    sent = {}
    orig = strict_p2p._json_response
    strict_p2p._json_response = lambda s, code, body: sent.update(code=code, body=body)
    try:
        strict_p2p.P2PHandler.handle_add_peer(object(), {"peer": local_server})
    finally:
        strict_p2p._json_response = orig
    assert sent["code"] == 400 and _Counting.hits == 0


def test_public_peer_still_accepted(strict_p2p, monkeypatch):
    monkeypatch.setattr(strict_p2p, "_resolve_host_ips", lambda h: ["8.8.8.8"])
    assert strict_p2p._try_add_peer("http://seed.example:8000") == "added"


# R2 -------------------------------------------------------------------------

def test_connected_address_rechecked(local_server):
    # Simulates rebinding: whatever the name resolved to at check time, the
    # socket landed on loopback, and the guard refuses before sending.
    with pytest.raises(netutil.AddressNotAllowed):
        netutil.urlopen(Request(local_server + "/x"), timeout=5, allow_ip=netutil.is_public_ip)
    assert _Counting.hits == 0


def test_rebinding_hostname_refused(local_server, monkeypatch):
    port = local_server.rsplit(":", 1)[1]
    with pytest.raises(netutil.AddressNotAllowed):
        netutil.urlopen(f"http://localhost:{port}/x", timeout=5, allow_ip=netutil.is_public_ip)
    assert _Counting.hits == 0


def test_p2p_fetch_uses_guard(strict_p2p, local_server):
    with pytest.raises(netutil.AddressNotAllowed):
        strict_p2p._get_json(local_server + "/status")
    with pytest.raises(netutil.AddressNotAllowed):
        strict_p2p._post_json(local_server + "/receive_block", {})
    assert _Counting.hits == 0


def test_guard_allows_when_policy_allows(local_server):
    with netutil.urlopen(local_server + "/x", timeout=5, allow_ip=lambda ip: True) as r:
        assert json.loads(r.read()) == {"ok": True}


# R3 -------------------------------------------------------------------------

def test_redirect_not_followed(local_server):
    import urllib.error
    with pytest.raises(urllib.error.HTTPError) as e:
        netutil.urlopen(local_server + "/redirect", timeout=5)
    assert e.value.code == 302
    assert _Counting.hits == 1  # only the original request, never the target


def test_scheme_still_checked():
    with pytest.raises(ValueError):
        netutil.urlopen("file:///etc/passwd")


# LRU ------------------------------------------------------------------------

def test_sync_trigger_table_is_lru(strict_p2p, monkeypatch):
    p = strict_p2p
    monkeypatch.setattr(p, "_resolve_host_ips", lambda h: ["8.8.8.8"])
    cap = p.MAX_PEERS * 4
    urls = [f"http://p{i}.example:8000" for i in range(cap + 5)]
    p.peers.update(urls)
    assert p.sync_trigger_allowed(urls[0], now=1000.0)[0]
    for i, u in enumerate(urls[1:], 1):
        assert p.sync_trigger_allowed(u, now=1000.0 + i * 0.001)[0]
    assert len(p._sync_trigger_last) == cap
    # a recently seen peer is still rate-limited (no full reset any more)
    assert not p.sync_trigger_allowed(urls[-1], now=1000.5)[0]
    assert urls[0] not in p._sync_trigger_last  # oldest evicted


# R4 / R5 --------------------------------------------------------------------

def test_missing_checkpoint_file_warns(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="netlithic.core"):
        assert core.load_checkpoints(str(tmp_path / "nope.json")) == {}
    assert "MISSING" in caplog.text


def test_checkpoints_cached_not_reread(tmp_path, monkeypatch):
    cp = tmp_path / "cp.json"
    cp.write_text(json.dumps({"2": "a" * 64}))
    monkeypatch.setattr(core, "CHECKPOINTS_FILE", str(cp))
    assert core.init_checkpoints() == {2: "a" * 64}
    cp.write_text("{broken")
    calls = []
    monkeypatch.setattr(core, "load_checkpoints", lambda *a: calls.append(1) or {})
    assert core.get_checkpoints() == {2: "a" * 64}
    assert calls == []
