"""Explorer points leaderboard and the node's /leaderboard endpoint."""
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import economics as eco
import explorer as ex_mod
from explorer import Explorer, make_handler

NLT = eco.BASE_UNITS_PER_NLT
EVIL = '<script>alert("x")</script>'


def jget(url):
    with urllib.request.urlopen(url) as r:
        return json.loads(r.read())


def mine(bc, addr, txs=()):
    assert bc.add_block(bc.mine_block(addr, list(txs)).to_dict()), bc.last_reject_reason


@pytest.fixture
def played(node, wallets):
    """alice mines 2 empty blocks (0 pts), sends 12 transfers to bob
    (only 10 count). bob mines the blocks with transfers (2.5 pts each)."""
    alice, bob = wallets
    bc = node.blockchain
    mine(bc, alice.address)
    mine(bc, alice.address)
    for n in range(1, 13):
        mine(bc, bob.address, [alice.create_transaction(bob.address, NLT, nonce=n)])
    return node, alice, bob


def test_leaderboard_api_rules(played):
    node, alice, bob = played
    lb = jget(f"{node.self_url()}/leaderboard")
    rows = {r["address"]: r for r in lb["leaders"]}
    # bob: 12 blocks with real transfers = 30 pts
    assert rows[bob.address]["points"] == "30" and rows[bob.address]["blocks_with_transfers"] == 12
    # alice: empty blocks 0, pair cap 10 -> 10 pts though 12 transfers sent
    assert rows[alice.address]["points"] == "10" and rows[alice.address]["transfers_sent"] == 12
    assert rows[alice.address]["transfers_that_earned"] == 10
    assert [r["rank"] for r in lb["leaders"]] == [1, 2] and lb["leaders"][0]["address"] == bob.address
    assert lb["rules"]["pair_cap_per_epoch"] == 10
    assert all("balance" not in k for r in lb["leaders"] for k in r)


def test_leaderboard_page(played):
    node, alice, bob = played
    ex = Explorer(ex_mod.NodeReader(node.self_url(), cache_seconds=0))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ex, "/explorer"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}/explorer"
        full = urllib.request.urlopen(base + "/leaderboard").read().decode()
        assert bob.address in full and "test coins have no value" in full.lower()
        assert "yourself earns 0" in full and "every mined block counts" in full and "at most 10" in full
        assert 'href="/explorer/leaderboard"' in full or "href='/explorer/leaderboard" in full
        shortp = urllib.request.urlopen(base + "/leaderboard?short=1").read().decode()
        assert bob.address[:8] + "..." in shortp and f">{bob.address}<" not in shortp
        assert "Balance" not in full
    finally:
        srv.shutdown()


def test_leaderboard_escapes_and_empty():
    class R:
        def __init__(self, leaders):
            self.leaders = leaders

        def get(self, path):
            return {"epoch": EVIL, "height": 1, "leaders": self.leaders, "rules": {"pair_cap_per_epoch": EVIL}}
    _, body = Explorer(R([])).leaderboard()
    assert "No points yet" in body and EVIL not in body
    _, body = Explorer(R([{"rank": 1, "address": EVIL, "points": EVIL}])).leaderboard(True)
    assert "<script>" not in body
