"""F10 (checkpoint self-check at startup, /status checkpoints_ok) and
F9 (explorer peer fetch guard, bounded /hello DNS)."""
import importlib
import json
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import core

BAD = "f" * 64


# F10 ------------------------------------------------------------------------

def test_status_reports_checkpoints_ok(node):
    port = node.PORT
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=5) as r:
        st = json.loads(r.read())
    assert st["checkpoints_ok"] is True


def test_checkpoint_mismatches_helper():
    chain = [{"hash": "a"}, {"hash": "b"}]
    assert core.checkpoint_mismatches(chain, {0: "a", 1: "b", 400: "z"}) == []  # above tip: not checked yet
    assert core.checkpoint_mismatches(chain, {1: "x"}) == [1]


def test_mismatching_chain_file_refuses_to_start(tmp_path, monkeypatch, caplog):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(core.CHECKPOINT_RESYNC_ENV, raising=False)
    core.Blockchain()  # writes a valid chain.json (genesis)
    core._checkpoint_cache = {0: BAD}  # pretend the release pins a different block
    with pytest.raises(core.CheckpointMismatch, match="Refusing to start"):
        core.Blockchain()
    assert "CHECKPOINT MISMATCH" in caplog.text
    assert os.path.exists("chain.json")  # nothing deleted


def test_mismatching_chain_file_quarantined_with_resync(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    core.Blockchain()
    original = open("chain.json").read()
    core._checkpoint_cache = {0: BAD}
    monkeypatch.setenv(core.CHECKPOINT_RESYNC_ENV, "1")
    bc = core.Blockchain()  # moved aside, restarted from genesis
    assert len(bc.chain) == 1
    q = [f for f in os.listdir(".") if f.startswith("chain.json.checkpoint-mismatch-")]
    assert len(q) == 1 and open(q[0]).read() == original


def test_startup_self_check_refuses_in_memory_mismatch(node, monkeypatch):
    monkeypatch.delenv(core.CHECKPOINT_RESYNC_ENV, raising=False)
    assert node.startup_checkpoint_check() is True
    core._checkpoint_cache = {0: BAD}
    with pytest.raises(core.CheckpointMismatch):
        node.startup_checkpoint_check()


def test_real_checkpoints_do_not_affect_short_chain(node):
    core.reset_checkpoints()
    core.init_checkpoints()
    assert node.startup_checkpoint_check() is True


# F9 explorer -----------------------------------------------------------------

class _Hit(BaseHTTPRequestHandler):
    hits = 0

    def do_GET(self):
        type(self).hits += 1
        body = b'{"version": "0.5.0"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def loop_peer():
    _Hit.hits = 0
    srv = HTTPServer(("127.0.0.1", 0), _Hit)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _reader(explorer, peer):
    r = explorer.NodeReader("http://127.0.0.1:1")
    r.status = lambda: {"node": "http://own:8000", "peers": [peer]}
    return r


def test_explorer_refuses_loopback_peer(loop_peer, monkeypatch):
    monkeypatch.delenv("NETLITHIC_ALLOW_PRIVATE_PEERS", raising=False)
    import explorer
    explorer = importlib.reload(explorer)
    out = _reader(explorer, loop_peer).node_statuses()
    assert out[loop_peer] is None and _Hit.hits == 0


def test_explorer_still_reads_allowed_peer(loop_peer, monkeypatch):
    import explorer
    explorer = importlib.reload(explorer)
    monkeypatch.setattr(explorer, "_peer_ip_allowed", lambda ip: True)  # stands in for a public IP
    out = _reader(explorer, loop_peer).node_statuses()
    assert out[loop_peer] == {"version": "0.5.0"} and _Hit.hits == 1


# F9 /hello DNS -----------------------------------------------------------------

@pytest.fixture
def strict(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NETLITHIC_ALLOW_PRIVATE_PEERS", raising=False)
    import p2p_http
    p = importlib.reload(p2p_http)
    yield p
    monkeypatch.setenv("NETLITHIC_ALLOW_PRIVATE_PEERS", "1")
    importlib.reload(p2p_http)


def test_slow_resolver_bounded(strict):
    release = threading.Event()

    def slow(host):
        release.wait(10)
        return []
    try:
        t0 = time.time()
        ok, why = strict.hello_address_allowed("http://slow.example:8000", "198.51.100.1",
                                               resolver=slow, timeout=0.3)
        assert not ok and "timed out" in why and time.time() - t0 < 1.5
        # second lookup from the same client still in flight: allowed up to the cap
        strict.hello_address_allowed("http://slow2.example:8000", "198.51.100.1", resolver=slow, timeout=0.1)
        t0 = time.time()
        ok, why = strict.hello_address_allowed("http://slow3.example:8000", "198.51.100.1", resolver=slow)
        assert not ok and "Too many" in why and time.time() - t0 < 0.2  # rejected at once
        # another client is not blocked by the first one's slow names
        ok, _ = strict.hello_address_allowed("http://good.example:8000", "203.0.113.9",
                                             resolver=lambda h: [__import__("ipaddress").ip_address("8.8.8.8")])
        assert ok
    finally:
        release.set()


def test_hello_ip_literals_skip_pool_and_valid_public_names_pass(strict):
    assert strict.hello_address_allowed("http://8.8.8.8:8000")[0]
    assert not strict.hello_address_allowed("http://127.0.0.1:8000")[0]
    import ipaddress
    assert strict.hello_address_allowed("http://seed.example:8000", "x",
                                        resolver=lambda h: [ipaddress.ip_address("8.8.4.4")])[0]


# PR #5 review fixes (P5-1, P5-2, P5-3) -----------------------------------------

import ipaddress  # noqa: E402

PUBLIC = [ipaddress.ip_address("8.8.8.8")]


def _wait(pred, t=5):
    end = time.time() + t
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def test_per_client_limit_two_and_ipv6_grouped_by_64(strict):
    release = threading.Event()
    slow = lambda h: (release.wait(10), [])[1]  # noqa: E731
    try:
        for i in range(2):
            ok, why = strict.hello_address_allowed(f"http://s{i}.example:8000", "2001:db8:1:2::1",
                                                   resolver=slow, timeout=0.05)
            assert "timed out" in why
        # same /64, different address: still the same client
        ok, why = strict.hello_address_allowed("http://s9.example:8000", "2001:db8:1:2::ffff", resolver=slow)
        assert not ok and "Too many" in why
        # a different /64 is a different client
        ok, _ = strict.hello_address_allowed("http://p.example:8000", "2001:db8:1:3::1", resolver=lambda h: PUBLIC)
        assert ok
        assert strict._client_key("198.51.100.7") == "198.51.100.7"
    finally:
        release.set()


def test_total_limit_sixteen(strict, monkeypatch):
    monkeypatch.setattr(strict, "HELLO_DNS_PER_CLIENT", 100)
    release = threading.Event()
    slow = lambda h: (release.wait(10), [])[1]  # noqa: E731
    threads = [threading.Thread(target=strict.hello_address_allowed,
                                args=(f"http://t{i}.example:8000", f"198.51.100.{i}", slow, 5))
               for i in range(strict.HELLO_DNS_MAX_PENDING)]
    try:
        for t in threads:
            t.start()
        assert _wait(lambda: sum(strict._hello_dns_inflight.values()) == strict.HELLO_DNS_MAX_PENDING)
        ok, why = strict.hello_address_allowed("http://x.example:8000", "203.0.113.50", resolver=slow)
        assert not ok and "busy" in why
    finally:
        release.set()
        for t in threads:
            t.join(10)


def test_slots_released_after_timeout_and_queued_lookups_cancelled(strict, monkeypatch):
    monkeypatch.setattr(strict, "HELLO_DNS_PER_CLIENT", 100)
    release = threading.Event()
    calls = []

    def slow(h):
        calls.append(h)
        release.wait(10)
        return []
    n = strict.HELLO_DNS_WORKERS + 4
    for i in range(n):
        ok, why = strict.hello_address_allowed(f"http://c{i}.example:8000", f"198.51.100.{i}",
                                               resolver=slow, timeout=0.05)
        assert "timed out" in why
    # queued (not yet running) lookups were cancelled, so only the running ones hold slots
    assert _wait(lambda: sum(strict._hello_dns_inflight.values()) == strict.HELLO_DNS_WORKERS)
    release.set()
    assert _wait(lambda: not strict._hello_dns_inflight)
    assert strict._hello_dns_slots._value == strict.HELLO_DNS_MAX_PENDING
    assert len(calls) == strict.HELLO_DNS_WORKERS  # cancelled lookups never ran


def test_failed_lookup_briefly_cached(strict):
    calls = []

    def fail(h):
        calls.append(h)
        raise OSError("nxdomain")
    assert not strict.hello_address_allowed("http://bad.example:8000", "198.51.100.1", resolver=fail)[0]
    ok, why = strict.hello_address_allowed("http://BAD.example:8000", "198.51.100.2", resolver=fail)
    assert not ok and "recently failed" in why and len(calls) == 1
    strict._hello_dns_neg["bad.example"] -= strict.HELLO_DNS_NEG_CACHE_SECONDS + 1  # expire
    strict.hello_address_allowed("http://bad.example:8000", "198.51.100.3", resolver=fail)
    assert len(calls) == 2


def _lock_spy(strict, monkeypatch):
    seen = []

    def resolve(host):
        seen.append(strict.state_lock.locked())
        return PUBLIC
    monkeypatch.setattr(strict, "_resolve_host_ips", resolve)
    return seen


def test_no_dns_under_state_lock_hello_and_add_peer(strict, monkeypatch):
    seen = _lock_spy(strict, monkeypatch)
    sent = {}
    monkeypatch.setattr(strict, "_json_response", lambda s, code, body: sent.update(code=code, body=body))
    monkeypatch.setattr(strict, "_get_json", lambda url, timeout=None: {
        "node": "http://seed.example:8000", "chain_id": strict.chain_id(), "version": "0.5.0"})

    class H:
        headers = {}
        client_address = ("198.51.100.9", 1)
    strict.P2PHandler.handle_hello(H(), {"url": "http://seed.example:8000"})
    assert sent["code"] == 200 and "http://seed.example:8000" in strict.peers
    strict.P2PHandler.handle_add_peer(H(), {"peer": "http://seed2.example:8000"})
    assert "http://seed2.example:8000" in strict.peers
    assert seen and not any(seen)


def test_no_dns_under_state_lock_discovery(strict, monkeypatch):
    seen = _lock_spy(strict, monkeypatch)
    # review F2: a gossiped candidate must now answer /status with the right chain, rules and version
    def fake_get(url, timeout=None):
        if url.endswith("/status"):
            return {"chain_id": strict.chain_id(), "points_v3_rules": strict.points_v3_rules_hash(),
                    "version": strict.netlithic_version.VERSION}
        return {"peers": ["http://a.example:8000", "http://b.example:8000", 7]}
    monkeypatch.setattr(strict, "_get_json", fake_get)
    strict._discover_peers_from("http://seed.example:8000")
    assert {"http://a.example:8000", "http://b.example:8000"} <= strict.peers
    assert seen and not any(seen)


def test_quarantine_names_never_collide(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    names = []
    for _ in range(3):
        open("chain.json", "w").write("[]")
        names.append(core.quarantine_chain_file([400]))
    assert len(set(names)) == 3 and all(os.path.exists(n) for n in names)


# P5-4 and review gaps ------------------------------------------------------------

def test_timeout_is_not_negative_cached(strict):
    release = threading.Event()
    slow = lambda h: (release.wait(10), PUBLIC)[1]  # noqa: E731  slow but legitimate
    try:
        ok, why = strict.hello_address_allowed("http://legit.example:8000", "198.51.100.1",
                                               resolver=slow, timeout=0.05)
        assert not ok and "timed out" in why
        assert "legit.example" not in strict._hello_dns_neg
    finally:
        release.set()
    assert _wait(lambda: not strict._hello_dns_inflight)
    assert "legit.example" not in strict._hello_dns_neg  # finished OK later: still not cached
    ok, _ = strict.hello_address_allowed("http://legit.example:8000", "198.51.100.2",
                                         resolver=lambda h: PUBLIC)
    assert ok


def test_cancelled_queued_lookup_is_not_cached(strict, monkeypatch):
    monkeypatch.setattr(strict, "HELLO_DNS_PER_CLIENT", 100)
    release = threading.Event()
    slow = lambda h: (release.wait(10), [])[1]  # noqa: E731
    try:
        for i in range(strict.HELLO_DNS_WORKERS):  # fill every worker
            strict.hello_address_allowed(f"http://busy{i}.example:8000", f"198.51.100.{i}",
                                         resolver=slow, timeout=0.05)
        ok, why = strict.hello_address_allowed("http://victim.example:8000", "203.0.113.7",
                                               resolver=lambda h: PUBLIC, timeout=0.05)
        assert not ok and "timed out" in why
        assert "victim.example" not in strict._hello_dns_neg
    finally:
        release.set()
    assert _wait(lambda: not strict._hello_dns_inflight)
    assert "victim.example" not in strict._hello_dns_neg
    assert strict.hello_address_allowed("http://victim.example:8000", "203.0.113.7",
                                        resolver=lambda h: PUBLIC)[0]


def test_non_public_answer_is_cached(strict):
    private = [ipaddress.ip_address("10.0.0.5")]
    assert not strict.hello_address_allowed("http://inside.example:8000", "198.51.100.1",
                                            resolver=lambda h: private)[0]
    assert "inside.example" in strict._hello_dns_neg


def test_ipv4_mapped_client_key():
    import p2p_http as p
    assert p._client_key("::ffff:198.51.100.7") == "198.51.100.7"
    assert p._client_key("::ffff:198.51.100.7") == p._client_key("198.51.100.7")
    assert p._client_key("2001:db8:1:2::5") == "2001:db8:1:2::/64"
    assert p._client_key("not-an-ip") == "not-an-ip"


def test_mapped_and_plain_ipv4_share_the_per_client_limit(strict):
    release = threading.Event()
    slow = lambda h: (release.wait(10), [])[1]  # noqa: E731
    try:
        strict.hello_address_allowed("http://m1.example:8000", "198.51.100.8", resolver=slow, timeout=0.05)
        strict.hello_address_allowed("http://m2.example:8000", "::ffff:198.51.100.8", resolver=slow, timeout=0.05)
        ok, why = strict.hello_address_allowed("http://m3.example:8000", "198.51.100.8", resolver=slow)
        assert not ok and "Too many" in why
    finally:
        release.set()


def test_extra_peers_cli_resolved_outside_lock_and_private_skipped(strict, monkeypatch, capsys):
    seen = []

    def resolve(host):
        seen.append(strict.state_lock.locked())
        try:
            return [ipaddress.ip_address(host)]
        except ValueError:
            return PUBLIC
    monkeypatch.setattr(strict, "_resolve_host_ips", resolve)
    strict._add_extra_peers(["http://seed.example:8000", "http://127.0.0.1:8001", "http://10.1.2.3:8000"])
    assert strict.peers == {"http://seed.example:8000"}
    assert seen and not any(seen)
    out = capsys.readouterr().out
    assert out.count("Skipping peer") == 2
    assert json.load(open(strict.PEERS_FILE)) == ["http://seed.example:8000"]


def test_load_peers_keeps_public_and_drops_private_or_junk(strict, monkeypatch):
    monkeypatch.setattr(strict, "_resolve_host_ips",
                        lambda h: {"seed.example": PUBLIC, "8.8.8.8": PUBLIC, "127.0.0.1": [ipaddress.ip_address("127.0.0.1")]}
                        .get(h, [ipaddress.ip_address("192.168.1.9")]))
    json.dump(["http://seed.example:8000", "http://127.0.0.1:8001", "http://lan.example:8000",
               "ftp://x", 42, "http://8.8.8.8:8000"], open(strict.PEERS_FILE, "w"))
    strict._load_peers()
    assert strict.peers == {"http://seed.example:8000", "http://8.8.8.8:8000"}


def test_load_peers_tolerates_bad_file(strict):
    open(strict.PEERS_FILE, "w").write("{not json")
    strict._load_peers()
    assert strict.peers == set()
