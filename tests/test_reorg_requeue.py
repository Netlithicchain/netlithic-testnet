"""After a reorg, txs (including a signed pause) that only lived in the abandoned blocks
go back into the mempool instead of vanishing (overnight review, drill finding)."""
import os

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import contributions as c
import core
import economics as eco
import pause
from core import Blockchain


def _adopt(p2p, other):
    """Make p2p._sync_with_peer see `other` as the peer's chain."""
    p2p._get_json = lambda url, *a, **k: {"chain_id": p2p.chain_id(), "chain": other.to_list()}
    p2p._sync_with_peer("http://127.0.0.1:1")


@pytest.fixture
def setup(node, monkeypatch, tmp_path, wallets):
    monkeypatch.setitem(eco.TESTNET_SHORT_EPOCHS, node.chain_id(), (3, 4))
    monkeypatch.setattr(core, "SECURITY_FORK_HEIGHT", 3)
    monkeypatch.setattr(c, "MIN_ADDRESS_AGE_BLOCKS", 0)
    key = Ed25519PrivateKey.generate().private_bytes_raw().hex()
    pub = pause.make_pause_tx(key, True, "x", 1, "c")["pubkey"]
    monkeypatch.setattr(pause, "PAUSE_PUBKEY", pub)
    cwd = os.getcwd()
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    os.chdir(other_dir)
    try:
        other = Blockchain()
    finally:
        os.chdir(cwd)
    other.save_chain = lambda: None
    return node, other, key, wallets


def _mine(bc, miner, txs=()):
    b = bc.mine_block(miner, list(txs))
    assert bc.add_block(b.to_dict()), bc.last_reject_reason
    return b


def test_orphaned_pause_is_requeued_after_reorg(setup):
    p2p, other, key, (alice, bob) = setup
    local = p2p.blockchain
    assert local.chain[0].hash == other.chain[0].hash
    for _ in range(3):
        _mine(local, alice.address)
    ptx = pause.make_pause_tx(key, True, "drill", 1, p2p.chain_id())
    stx = alice.create_transaction(bob.address, 10**8, nonce=1)
    _mine(local, "c3" * 16, [ptx, stx])
    assert pause.state(local.chain)["paused"]
    # the rest of the network built a longer chain without those two txs
    for i in range(7):
        _mine(other, "d4" * 16)
    _adopt(p2p, other)
    assert local.chain[-1].hash == other.chain[-1].hash
    assert not pause.state(local.chain)["paused"]
    ids = {core.tx_id(t) for t in p2p.mempool}
    assert core.tx_id(ptx) in ids                                  # pause came back
    # (the transfer needs alice's funds, which only existed on the abandoned chain, so it is dropped)


def test_requeue_does_not_resurrect_confirmed_or_reward_txs(setup):
    p2p, other, key, _ = setup
    local = p2p.blockchain
    for _ in range(3):
        _mine(local, "a1" * 16)
    for _ in range(5):
        _mine(other, "d4" * 16)
    _adopt(p2p, other)
    assert p2p.mempool == []                                       # only reward txs were orphaned


def test_requeued_pause_already_in_new_chain_is_not_duplicated(setup):
    p2p, other, key, _ = setup
    local = p2p.blockchain
    for _ in range(3):
        _mine(local, "a1" * 16)
        _mine(other, "d4" * 16)
    ptx = pause.make_pause_tx(key, True, "drill", 1, p2p.chain_id())
    _mine(local, "c3" * 16, [ptx])
    _mine(other, "d4" * 16, [ptx])                                 # same pause got mined on the winning chain
    for _ in range(3):
        _mine(other, "d4" * 16)
    _adopt(p2p, other)
    assert [t for t in p2p.mempool if core.tx_id(t) == core.tx_id(ptx)] == []
    assert pause.state(local.chain)["nonce"] == 1
