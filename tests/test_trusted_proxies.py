"""Reverse proxies listed in NETLITHIC_TRUSTED_PROXIES skip the per-address caps but get
no reserved slots; loopback leaves the reserve when a proxy is set; optional health listener."""
import json
import socket
import time
import urllib.error
import urllib.request

import netutil
from tests.test_inbound_slow_clients import _conn, _status, serve  # noqa: F401


def _free(srv):
    return srv._slots._value, (srv._reserved._value if srv._reserved else 0)


def _hold(port, src, n):
    out = []
    for _ in range(n):
        try:
            s = _conn(port, src)
            s.sendall(b"GET /status HTTP/1.0\r\n")
            out.append(s)
        except OSError:
            pass
        time.sleep(0.01)
    return out


def test_env_parsing_takes_ip_literals_only():
    env = {"NETLITHIC_TRUSTED_PROXIES": " 10.0.0.5, ::ffff:10.0.0.6 ,proxy.example.com,, "}
    assert netutil.trusted_proxies_from_env(env) == {"10.0.0.5", "10.0.0.6"}
    assert netutil.trusted_proxies_from_env({}) == frozenset()


def test_proxy_is_exempt_from_the_per_address_cap_but_gets_no_reserve(node, serve):
    srv, port = serve(max_connections=40, max_per_ip=12, reserved_slots=8,
                      reserved_ips={"127.0.0.7"})
    srv.trusted_proxies = {"127.0.0.7"}       # listed as both: proxy treatment wins
    held = _hold(port, "127.0.0.7", 40)
    try:
        time.sleep(0.5)
        assert srv._per_ip.get("127.0.0.7") == 16     # beyond 12, held to half the open pool
        assert _free(srv) == (16, 8)                   # rest of the pool and reserve untouched
    finally:
        for s in held:
            s.close()


def test_other_addresses_keep_their_caps_with_a_proxy_set(node, serve):
    srv, port = serve(max_connections=64, max_per_ip=12, reserved_slots=0)
    srv.trusted_proxies = {"127.0.0.7"}
    held = _hold(port, "127.0.0.8", 20)
    try:
        time.sleep(0.5)
        assert srv._per_ip.get("127.0.0.8") == 12
    finally:
        for s in held:
            s.close()


def test_loopback_leaves_the_reserve_when_a_proxy_is_configured(node, monkeypatch):
    monkeypatch.setattr(node, "_builtin_seeds", lambda: [])
    monkeypatch.delenv("NETLITHIC_TRUSTED_PROXIES", raising=False)
    assert "127.0.0.1" in node._reserved_ips()
    monkeypatch.setenv("NETLITHIC_TRUSTED_PROXIES", "127.0.0.1")
    ips = node._reserved_ips()
    assert "127.0.0.1" not in ips and "::1" not in ips


def test_changes_in_the_trusted_set_are_logged(node, serve, monkeypatch, capsys):
    srv, _ = serve(max_connections=8, reserved_slots=2, reserved_ips={"127.0.0.1"})
    monkeypatch.setattr(node, "_reserved_ips", lambda: {"127.0.0.1", "192.0.2.9"})
    node._refresh_reserved_ips(srv)
    assert "added ['192.0.2.9']" in capsys.readouterr().out
    node._refresh_reserved_ips(srv)
    assert "changed" not in capsys.readouterr().out


def test_health_listener_answers_on_loopback(node):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = node._start_health_listener(port)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=10) as r:  # nosec B310
            assert r.status == 200 and "height" in json.loads(r.read())
        assert srv.server_address[0] == "127.0.0.1"
    finally:
        srv.shutdown()
        srv.server_close()


def test_one_proxy_cannot_take_more_than_half_the_open_pool(node, serve):
    srv, port = serve(max_connections=40, max_per_ip=12, reserved_slots=8)
    srv.trusted_proxies = {"127.0.0.7"}
    assert srv.max_per_proxy == 16
    held = _hold(port, "127.0.0.7", 30)
    try:
        time.sleep(0.5)
        assert srv._per_ip.get("127.0.0.7") == 16
        others = _hold(port, "127.0.0.8", 12)      # a direct client still gets its full share
        try:
            time.sleep(0.5)
            assert srv._per_ip.get("127.0.0.8") == 12
        finally:
            for s in others:
                s.close()
    finally:
        for s in held:
            s.close()


def test_proxy_share_is_configurable_and_at_least_one(node, serve):
    srv, _ = serve(max_connections=10, reserved_slots=0, proxy_share=0.25)
    assert srv.max_per_proxy == 2
    srv2, _ = serve(max_connections=2, reserved_slots=1, proxy_share=0.1)
    assert srv2.max_per_proxy == 1


def test_node_warns_when_trusted_proxies_is_set(node, serve, capsys):
    srv, _ = serve(max_connections=8, reserved_slots=0)
    srv.trusted_proxies = set()
    node._report_trusted_proxies(srv)
    assert capsys.readouterr().err == ""
    srv.trusted_proxies = {"127.0.0.7"}
    node._report_trusted_proxies(srv)
    out = capsys.readouterr()
    assert "WARNING: NETLITHIC_TRUSTED_PROXIES is set on a node" in out.err
    assert "up to 4 connections each" in out.out


def _health(node):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return node._start_health_listener(port), port


def _get(port, path, data=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # nosec B310
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_health_listener_serves_status_and_health_only(node):
    srv, port = _health(node)
    try:
        for path in ("/status", "/health", "/health?x=1"):
            code, body = _get(port, path)
            assert code == 200 and "height" in body, path
        for path in ("/chain", "/peers", "/mempool", "/block?index=0", "/", "/status/../chain",
                     "/admin/pause"):
            assert _get(port, path)[0] == 404, path
        for path in ("/add_peer", "/status", "/transactions/new"):
            assert _get(port, path, data=b"{}")[0] == 404, path
    finally:
        srv.shutdown()
        srv.server_close()


def test_public_port_still_serves_the_full_api(node, serve):
    _, port = serve(max_connections=8, reserved_slots=0)
    assert _get(port, "/peers")[0] == 200
