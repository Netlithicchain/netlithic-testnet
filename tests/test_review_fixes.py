"""Fixes for the PR #1 security review (F1-F4 + explicit short-epoch chain_id)."""
import ast
import ipaddress
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from conftest import ROOT, post
import core
import economics as eco
import netlithic_version as nv
from test_security_fork import small_fork, grow, D


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # p2p_http builds a Blockchain() on import


def ips(*a):
    return lambda host: [ipaddress.ip_address(x) for x in a]


# ---------------- F1: SSRF / forced sync ----------------

@pytest.mark.parametrize("addr", ["10.0.0.5", "127.0.0.1", "169.254.169.254", "192.168.1.1",
                                  "172.16.0.1", "::1", "fd00::1", "fe80::1", "0.0.0.0",
                                  "::ffff:127.0.0.1", "100.64.0.1", "224.0.0.1"])
def test_private_addresses_blocked(addr, monkeypatch):
    import p2p_http as p
    monkeypatch.setattr(p, "ALLOW_PRIVATE_PEERS", False)
    assert not p.peer_address_allowed("http://x.example:8000", resolver=ips(addr))


def test_public_ok_mixed_or_unresolvable_blocked(monkeypatch):
    import p2p_http as p
    monkeypatch.setattr(p, "ALLOW_PRIVATE_PEERS", False)
    assert p.peer_address_allowed("http://seed.example:8000", resolver=ips("8.8.8.8"))
    # DNS rebinding style: one public + one private answer -> refused
    assert not p.peer_address_allowed("http://seed.example:8000", resolver=ips("8.8.8.8", "10.0.0.1"))

    def boom(host):
        raise OSError("nxdomain")
    assert not p.peer_address_allowed("http://nope.example:8000", resolver=boom)


def test_sync_trigger_requires_known_peer_and_is_rate_limited(monkeypatch):
    import p2p_http as p
    monkeypatch.setattr(p, "ALLOW_PRIVATE_PEERS", False)
    monkeypatch.setattr(p, "peers", {"http://seed.example:8000"})
    p._sync_trigger_last.clear()
    pub = ips("8.8.8.8")
    ok, why = p.sync_trigger_allowed("http://evil.example:8000", now=1000, resolver=pub)
    assert not ok and "known" in why
    assert p.sync_trigger_allowed("http://seed.example:8000", now=1000, resolver=pub)[0]
    ok, why = p.sync_trigger_allowed("http://seed.example:8000", now=1001, resolver=pub)
    assert not ok and "rate" in why
    assert p.sync_trigger_allowed("http://seed.example:8000", now=1000 + p.SYNC_TRIGGER_MIN_SECONDS, resolver=pub)[0]
    assert not p.sync_trigger_allowed(None)[0]


def test_receive_block_from_unknown_peer_does_not_sync(node, monkeypatch):
    calls = []
    monkeypatch.setattr(node, "_sync_with_peer", lambda peer: calls.append(peer))
    fake = {"index": 50, "hash": "ab" * 32, "previous_hash": "cd" * 32, "timestamp": time.time(),
            "transactions": [], "nonce": 0, "difficulty": 1}
    status, body = post(node, "/receive_block", {"block": fake, "from_peer": "http://169.254.169.254"})
    assert status == 200 and calls == []
    assert "no sync triggered" in body["message"]


# ---------------- F2: stall rule has no wall clock ----------------

def test_allowed_difficulties_source_has_no_wall_clock():
    import inspect
    src = inspect.getsource(core.Blockchain.allowed_difficulties)
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    assert "time.time" not in code


# ---------------- F3: version gate + desktop bundling ----------------

def test_version_parsing():
    assert nv.parse_version("0.5.0") == (0, 5, 0)
    assert nv.parse_version("0.5") == (0, 5, 0)
    assert nv.parse_version(None) == (0, 0, 0)
    assert nv.parse_version("garbage") == (0, 0, 0)
    assert nv.is_outdated(None) and nv.is_outdated("0.4.9")
    assert not nv.is_outdated("0.5.0") and not nv.is_outdated("1.0.0")
    assert nv.UPGRADE_DEADLINE_HEIGHT == core.SECURITY_FORK_HEIGHT - 100


def _fake_peer(version):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = {"node": f"http://127.0.0.1:{self.server.server_address[1]}", "chain_id": "netlithic-dev-1"}
            if version is not None:
                body["version"] = version
            b = json.dumps(body).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(b)))
            self.end_headers(); self.wfile.write(b)

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_hello_warns_old_peer_before_deadline(node):
    srv = _fake_peer(None)  # pre-0.5.0 builds report no version
    try:
        status, body = post(node, "/hello", {"url": f"http://127.0.0.1:{srv.server_address[1]}"})
        assert status == 200 and body["upgrade_needed"] and "Upgrade needed" in body["warning"]
    finally:
        srv.shutdown()


def test_hello_refuses_old_peer_from_deadline(node, monkeypatch):
    monkeypatch.setattr(node.netlithic_version, "UPGRADE_DEADLINE_HEIGHT", 0)
    srv = _fake_peer("0.4.0")
    try:
        status, body = post(node, "/hello", {"url": f"http://127.0.0.1:{srv.server_address[1]}"})
        assert status == 400 and body["upgrade_needed"]
        assert not node.peers
    finally:
        srv.shutdown()
    srv = _fake_peer("0.5.0")
    try:
        status, body = post(node, "/hello", {"url": f"http://127.0.0.1:{srv.server_address[1]}"})
        assert status == 200 and "upgrade_needed" not in body
    finally:
        srv.shutdown()


def _local_imports(mod, seen):
    path = os.path.join(ROOT, mod + ".py")
    if mod in seen or not os.path.exists(path):
        return
    seen.add(mod)
    tree = ast.parse(open(path, encoding="utf-8").read())
    for n in ast.walk(tree):
        names = [a.name for a in n.names] if isinstance(n, ast.Import) else \
            [n.module] if isinstance(n, ast.ImportFrom) and n.module and n.level == 0 else []
        for name in names:
            _local_imports(name.split(".")[0], seen)


@pytest.mark.parametrize("script", ["build_linux.sh", "build_macos.sh", "build_windows.ps1"])
def test_desktop_build_bundles_every_runtime_module(script):
    """The desktop build must ship THIS code (pause, reorg limit, anti-farming
    v2, version gate). PyInstaller only follows static imports from the entry
    script, so every local module the node needs must be a --hidden-import."""
    text = open(os.path.join(ROOT, "desktop", script), encoding="utf-8").read()
    bundled = set(re.findall(r"--hidden-import\s+(\w+)", text))
    need = set()
    for m in ("p2p_http", "node", "core", "webwallet", "node_pool"):
        _local_imports(m, need)
    missing = need - bundled
    assert not missing, f"{script} misses {sorted(missing)}"
    assert "config:config" in text or "config;config" in text  # ships checkpoints.json + genesis


def test_desktop_upgrade_notice():
    sys.path.insert(0, os.path.join(ROOT, "desktop"))
    import netlithic_desktop as d
    assert d.upgrade_notice(None) == ""
    assert d.upgrade_notice({"min_peer_version": "0.5.0"}, own_version="0.5.0") == ""
    msg = d.upgrade_notice({"min_peer_version": "0.6.0", "upgrade_deadline_height": 9900}, own_version="0.5.0")
    assert "0.6.0" in msg and "9900" in msg


# ---------------- F4: checkpoints + depth before validation ----------------

def test_checkpoints_file_shipped_and_valid():
    path = os.path.join(ROOT, "config", "checkpoints.json")
    assert os.path.exists(path)
    assert core.load_checkpoints(path) == {
        400: "0041d10e973ffedfa721c0be089e1a1f74dce05424cccb7281d822b0e59d16c9"}
    assert os.path.isabs(core.CHECKPOINTS_FILE)


def test_reorg_depth_checked_before_full_validation(chain, monkeypatch):
    small_fork(monkeypatch)
    monkeypatch.setattr(core, "MAX_REORG_DEPTH", 3)
    ours = grow(chain, chain.chain, 10, step=1.0)
    chain.chain = ours
    deep = grow(chain, ours[:-6], 9, step=1.0, miner=D)
    called = []
    monkeypatch.setattr(chain, "is_valid_chain", lambda data: called.append(1) or True)
    assert not chain.replace_chain([b.to_dict() for b in deep])
    assert "exceeds limit" in chain.last_reject_reason and called == []


def test_node_refuses_to_start_with_broken_checkpoints(tmp_path, monkeypatch):
    p = tmp_path / "cp.json"
    p.write_text("{broken")
    monkeypatch.setattr(core, "CHECKPOINTS_FILE", str(p))
    with pytest.raises(core.CheckpointError):
        core.load_checkpoints()


# ---------------- short-epoch chain_id is explicit ----------------

def test_short_epoch_bound_to_shipped_chain_id():
    shipped = json.load(open(os.path.join(ROOT, "config", "genesis_config.json")))["chain_id"]
    assert shipped == eco.SHORT_EPOCH_CHAIN_ID == "netlithic-dev-1"
    assert eco.TESTNET_SHORT_EPOCHS[eco.SHORT_EPOCH_CHAIN_ID] == (5000, 1440)
    from dataclasses import replace
    base = eco.load_economics()
    assert replace(base, chain_id="netlithic-dev-1").epoch_length_at(5000) == 1440
    # any other chain_id (even another dev chain) keeps the normal epoch length
    other = replace(base, chain_id="netlithic-dev-2")
    assert other.short_epoch() is None and other.epoch_length_at(5000) == base.epoch_blocks


def test_version_locked_everywhere():
    import netlithic_version
    assert netlithic_version.VERSION == "0.5.0"
    iss = open(os.path.join(ROOT, "desktop", "netlithic.iss"), encoding="utf-8").read()
    assert f"AppVersion={netlithic_version.VERSION}\n" in iss
