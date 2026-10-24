"""Network abuse limits: URL schemes, per-IP rate limit, mempool cap, per-host peer cap."""
import pytest

from conftest import post


# --- netutil: only http/https --------------------------------------------------

@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://x/y", "data:text/plain,hi",
                                 "gopher://x", "/relative/path", "jar:http://x!/"])
def test_netutil_rejects_non_http_schemes(url):
    import netutil
    with pytest.raises(ValueError):
        netutil.urlopen(url, timeout=1)


@pytest.mark.parametrize("url", ["http://127.0.0.1:1/", "HTTPS://example.invalid/"])
def test_netutil_accepts_http_schemes(url):
    import netutil
    assert netutil.check_url(url) == url


def test_netutil_checks_request_objects():
    import netutil
    from urllib.request import Request
    with pytest.raises(ValueError):
        netutil.urlopen(Request("file:///etc/hosts"))


# --- per-IP token bucket ----------------------------------------------------------

def test_rate_limit_burst_then_refill(node, monkeypatch):
    monkeypatch.setattr(node, "RATE_BURST", 5.0)
    monkeypatch.setattr(node, "RATE_PER_SECOND", 1.0)
    node._rate_buckets.clear()
    ip = "203.0.113.7"
    assert not any(node.rate_limited(ip, now=1000.0) for _ in range(5))
    assert node.rate_limited(ip, now=1000.0)              # burst used up
    assert not node.rate_limited("203.0.113.8", now=1000.0)  # other IPs unaffected
    assert not node.rate_limited(ip, now=1002.0)           # refilled
    assert not node.rate_limited(ip, now=1002.0)
    assert node.rate_limited(ip, now=1002.0)


def test_loopback_exempt_unless_flag(node, monkeypatch):
    monkeypatch.setattr(node, "RATE_BURST", 1.0)
    node._rate_buckets.clear()
    assert not any(node.rate_limited("127.0.0.1", now=1.0) for _ in range(50))
    monkeypatch.setattr(node, "RATE_LIMIT_LOOPBACK", True)
    assert not node.rate_limited("127.0.0.1", now=1.0)
    assert node.rate_limited("127.0.0.1", now=1.0)


def test_rate_limit_returns_429_over_http(node, monkeypatch):
    monkeypatch.setattr(node, "RATE_LIMIT_LOOPBACK", True)
    monkeypatch.setattr(node, "RATE_BURST", 3.0)
    monkeypatch.setattr(node, "RATE_PER_SECOND", 0.001)
    node._rate_buckets.clear()
    codes = [post(node, "/submit_tx", {"tx": {}})[0] for _ in range(6)]
    assert codes[:3] != [429] * 3 and codes[-1] == 429


def test_tracked_ip_table_is_bounded(node, monkeypatch):
    monkeypatch.setattr(node, "MAX_TRACKED_IPS", 100)
    node._rate_buckets.clear()
    for i in range(500):
        node.rate_limited(f"198.51.{i // 256}.{i % 256}", now=1.0)
    assert len(node._rate_buckets) <= 101


# --- mempool cap --------------------------------------------------------------------

def test_mempool_full_returns_503(node, wallets, monkeypatch):
    alice, bob = wallets
    monkeypatch.setattr(node, "MAX_MEMPOOL", 2)
    node.mempool[:] = [alice.create_transaction(bob.address, 1, nonce=n) for n in (1, 2)]
    tx = alice.create_transaction(bob.address, 1, nonce=3)
    for path in ("/submit_tx", "/receive_tx"):
        status, body = post(node, path, {"tx": tx})
        assert status == 503, (path, status, body)
    assert len(node.mempool) == 2


# --- per-host peer cap ----------------------------------------------------------------

def test_one_host_cannot_fill_peer_list(node):
    with node.state_lock:
        node.peers.clear()
        assert node._try_add_peer("http://10.1.1.1:8000") == "added"
        assert node._try_add_peer("http://10.1.1.1:8001") == "added"
        with pytest.raises(ValueError, match="one host"):
            node._try_add_peer("http://10.1.1.1:8002")
        assert node._try_add_peer("http://10.1.1.2:8000") == "added"
        # loopback (local testnets) is exempt
        for port in range(9100, 9105):
            node._try_add_peer(f"http://127.0.0.1:{port}")
