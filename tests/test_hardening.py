import importlib
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from conftest import ROOT, build_block, post
from core import BLOCK_REWARD, MAX_FUTURE_BLOCK_SECONDS, make_reward_tx


# --- helpers ----------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path, monkeypatch):
    # p2p_http builds a Blockchain() on import; keep its files out of the repo.
    monkeypatch.chdir(tmp_path)

class Headers(dict):
    def get(self, k, d=None):
        return super().get(k, d)


# --- (1) admin endpoints ----------------------------------------------------------

def test_loopback_without_token_is_rejected(monkeypatch):
    import p2p_http as p
    monkeypatch.setenv(p.ADMIN_TOKEN_ENV, "s3cret-test-token")
    monkeypatch.delenv(p.DEV_LOCAL_ADMIN_ENV, raising=False)
    assert not p.is_admin_request("127.0.0.1", Headers())
    assert not p.is_admin_request("::1", Headers({p.ADMIN_TOKEN_HEADER: "wrong"}))
    assert p.is_admin_request("127.0.0.1", Headers({p.ADMIN_TOKEN_HEADER: "s3cret-test-token"}))


def test_no_token_disables_admin_even_for_loopback(monkeypatch):
    import p2p_http as p
    monkeypatch.delenv(p.ADMIN_TOKEN_ENV, raising=False)
    monkeypatch.delenv(p.DEV_LOCAL_ADMIN_ENV, raising=False)
    assert not p.is_admin_request("127.0.0.1", Headers())
    assert not p.is_admin_request("10.0.0.5", Headers())
    # an empty configured token must never match an empty header
    monkeypatch.setenv(p.ADMIN_TOKEN_ENV, "")
    assert not p.is_admin_request("127.0.0.1", Headers({p.ADMIN_TOKEN_HEADER: ""}))


def test_dev_local_admin_flag_only_on_dev_chain_and_loopback(monkeypatch):
    import p2p_http as p
    monkeypatch.delenv(p.ADMIN_TOKEN_ENV, raising=False)
    monkeypatch.setenv(p.DEV_LOCAL_ADMIN_ENV, "1")
    monkeypatch.setattr(p, "chain_id", lambda: "netlithic-dev-1")
    assert p.is_admin_request("127.0.0.1", Headers())
    assert not p.is_admin_request("10.0.0.5", Headers())
    monkeypatch.setattr(p, "chain_id", lambda: "netlithic-testnet-1")
    assert not p.is_admin_request("127.0.0.1", Headers())


def test_admin_remote_needs_correct_token(monkeypatch):
    import p2p_http as p
    monkeypatch.setenv(p.ADMIN_TOKEN_ENV, "s3cret-test-token")
    assert p.is_admin_request("10.0.0.5", Headers({p.ADMIN_TOKEN_HEADER: "s3cret-test-token"}))
    assert not p.is_admin_request("10.0.0.5", Headers({p.ADMIN_TOKEN_HEADER: "wrong"}))


def test_mine_over_http_needs_token(node, wallets):
    alice, _ = wallets
    assert post(node, "/mine", {"miner_address": alice.address},
                headers={"X-Admin-Token": "wrong"})[0] == 403
    assert post(node, "/add_peer", {"peer": "http://127.0.0.1:9"},
                headers={"X-Admin-Token": ""})[0] == 403
    assert len(node.blockchain.chain) == 1


def test_loopback_peers_hidden_from_remote_callers(node, monkeypatch):
    node.peers.update({"http://127.0.0.1:8001", "http://localhost:8002", "http://[::1]:8003",
                       "http://203.0.113.7:8000"})
    assert node.shared_peers("127.0.0.1") == sorted(node.peers)
    assert node.shared_peers("198.51.100.2") == ["http://203.0.113.7:8000"]


def test_mine_and_add_peer_forbidden_for_non_admin(node, monkeypatch, wallets):
    alice, _ = wallets
    monkeypatch.setattr(node, "is_admin_request", lambda ip, h: False)  # simulate remote caller
    assert post(node, "/mine", {"miner_address": alice.address})[0] == 403
    assert post(node, "/add_peer", {"peer": "http://127.0.0.1:9"})[0] == 403
    assert len(node.blockchain.chain) == 1 and not node.peers


def test_mine_works_with_token(node, wallets):
    alice, _ = wallets
    status, body = post(node, "/mine", {"miner_address": alice.address})
    assert status == 200 and body["block"]["index"] == 1


# --- (2) peer URL validation ---------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "ftp://example.com", "file:///etc/passwd", "javascript:alert(1)",
    "http://user:pw@example.com", "http://example.com/path", "http://example.com:99999",
    "http://", "", "http://example.com?x=1",
])
def test_bad_peer_urls_rejected(bad):
    import p2p_http as p
    with pytest.raises(ValueError):
        p._normalize_peer(bad)


def test_good_peer_urls_normalized():
    import p2p_http as p
    assert p._normalize_peer("127.0.0.1:8001") == "http://127.0.0.1:8001"
    assert p._normalize_peer("HTTPS://Node.Example.com/") == "https://node.example.com"


def test_self_peer_rejected_in_all_spellings(node):
    port = node.PORT
    for me in (f"http://127.0.0.1:{port}", f"localhost:{port}", f"http://0.0.0.0:{port}"):
        status, body = post(node, "/add_peer", {"peer": me})
        assert status == 400 and "self" in body["error"]


def test_peer_count_capped(node, monkeypatch):
    monkeypatch.setattr(node, "MAX_PEERS", 3)
    with node.state_lock:
        for i in range(3):
            assert node._try_add_peer(f"http://10.0.0.{i + 1}:8000") == "added"
        with pytest.raises(ValueError, match="limit"):
            node._try_add_peer("http://10.0.0.99:8000")
        assert node._try_add_peer("http://10.0.0.1:8000") == "known"
    assert len(node.peers) == 3


def test_loaded_peers_file_is_validated_and_capped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "peers.json").write_text(json.dumps(
        ["ftp://bad", "http://10.0.0.1:1"] + [f"http://10.1.0.{i}:1" for i in range(100)]))
    import p2p_http
    monkeypatch.delenv("NETLITHIC_ALLOW_PRIVATE_PEERS", raising=False)
    p = importlib.reload(p2p_http)
    p._load_peers()
    assert p.peers == set()  # R1: private addresses refused from peers.json too
    monkeypatch.setenv("NETLITHIC_ALLOW_PRIVATE_PEERS", "1")
    p = importlib.reload(p2p_http)
    p._load_peers()
    assert "ftp://bad" not in p.peers
    assert len(p.peers) == p.MAX_PEERS


# --- (3) future timestamps and body caps -------------------------------------------------

def test_block_too_far_in_future_rejected(chain, wallets):
    alice, _ = wallets
    future = time.time() + MAX_FUTURE_BLOCK_SECONDS + 60
    block = build_block(chain, [make_reward_tx(alice.address, BLOCK_REWARD)], timestamp=future)
    assert not chain.add_block(block.to_dict())
    assert "future" in chain.last_reject_reason


def test_block_slightly_in_future_ok(chain, wallets):
    alice, _ = wallets
    block = build_block(chain, [make_reward_tx(alice.address, BLOCK_REWARD)], timestamp=time.time() + 30)
    assert chain.add_block(block.to_dict())


def test_future_block_rejected_over_http_with_reason(node, wallets):
    alice, _ = wallets
    bc = node.blockchain
    block = build_block(bc, [make_reward_tx(alice.address, BLOCK_REWARD)], timestamp=time.time() + 3600)
    status, body = post(node, "/receive_block", {"block": block.to_dict()})
    assert status == 400 and "future" in body["reason"]


def test_oversized_post_rejected(node, monkeypatch):
    monkeypatch.setattr(node, "MAX_POST_BYTES", 1000)
    big = json.dumps({"tx": {"pad": "x" * 2000}}).encode()
    status, body = post(node, "/submit_tx", raw=big)
    assert status == 413


def test_oversized_peer_response_rejected(node, monkeypatch):
    monkeypatch.setattr(node, "MAX_FETCH_BYTES", 100)  # our own /chain is bigger than this
    with pytest.raises(ValueError, match="over"):
        node._get_json(f"{node.self_url()}/chain")


# --- (4) add_block gives a reason ---------------------------------------------------------

@pytest.mark.parametrize("mutate, expect", [
    (lambda d: d.update(nonce=d["nonce"] + 1), "hash does not match"),
    (lambda d: d.update(index=99), "wrong index"),
    (lambda d: d.update(previous_hash="0" * 64), "previous_hash"),
    (lambda d: d.update(transactions="nope"), "malformed"),
])
def test_add_block_reports_reason(chain, wallets, mutate, expect):
    alice, _ = wallets
    d = chain.mine_block(alice.address, []).to_dict()
    mutate(d)
    assert chain.add_block(d) is False
    assert expect in chain.last_reject_reason


def test_add_block_logs_reason(chain, wallets, caplog):
    alice, _ = wallets
    d = chain.mine_block(alice.address, []).to_dict()
    d["nonce"] += 1
    with caplog.at_level("WARNING", logger="netlithic.core"):
        chain.add_block(d)
    assert "hash does not match" in caplog.text


def test_reason_cleared_after_success(chain, wallets):
    alice, _ = wallets
    d = chain.mine_block(alice.address, []).to_dict()
    bad = dict(d, nonce=d["nonce"] + 1)
    assert not chain.add_block(bad)
    assert chain.add_block(d) and chain.last_reject_reason is None


# --- (5) --data-dir ---------------------------------------------------------------------

def test_node_parse_args():
    import node
    a = node.parse_args(["8123", "--data-dir", "/tmp/x"])
    assert a.port == 8123 and a.data_dir == "/tmp/x"
    assert node.parse_args([]).data_dir == "."


def test_node_with_data_dir_writes_files_there(tmp_path):
    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    data = tmp_path / "nodeA"
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "node.py"), str(port),
                             "--data-dir", str(data)], cwd=tmp_path,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=1) as r:
                    assert json.loads(r.read())["height"] == 0
                    break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("node did not start")
        assert (data / "chain.json").exists()
        assert not (tmp_path / "chain.json").exists()
    finally:
        proc.terminate()
        proc.wait(timeout=10)
