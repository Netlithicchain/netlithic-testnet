"""The nonce search must not run while the node's state lock is held, so a mining node keeps answering peers."""
import json
import threading
import time
import urllib.error
import urllib.request

import core
from conftest import post

MINER = "ab" * 16
OTHER = "cd" * 16


def _get(p2p, path, timeout=3):
    with urllib.request.urlopen(f"{p2p.self_url()}{path}", timeout=timeout) as r:
        return r.status, json.loads(r.read())


def test_solver_stops_when_told_to_abort(chain, monkeypatch):
    tpl = chain.build_block_template(MINER, [])
    monkeypatch.setattr(core, "meets_pow", lambda *a, **k: False)      # never solvable: only the abort can end it
    polls = []

    def should_abort():
        polls.append(1)
        return len(polls) >= 3

    assert chain.solve_block_template(tpl, should_abort=should_abort, abort_every=10) is None
    assert len(polls) == 3


def test_solver_does_not_read_the_chain(chain):
    """The template carries everything the search needs; clearing the chain afterwards must not matter."""
    tpl = chain.build_block_template(MINER, [])
    saved = chain.chain
    chain.chain = None
    try:
        blk = chain.solve_block_template(tpl)
    finally:
        chain.chain = saved
    assert blk is not None and blk.index == 1
    assert chain.add_block(blk.to_dict())


def test_mine_block_still_returns_a_valid_block(chain):
    assert chain.add_block(chain.mine_block(MINER, []).to_dict())


def test_status_and_receive_block_answer_during_the_nonce_search(node):
    p2p = node
    bc = p2p.blockchain
    searching, release = threading.Event(), threading.Event()
    real_solve = bc.solve_block_template
    lock_was_free = {}

    def slow_solve(tpl, should_abort=None, abort_every=4096):
        got = p2p.state_lock.acquire(timeout=2)            # the old code held this lock for the whole search
        lock_was_free["v"] = got
        if got:
            p2p.state_lock.release()
        searching.set()
        release.wait(10)
        return real_solve(tpl, should_abort, abort_every)

    bc.solve_block_template = slow_solve
    result = {}
    t = threading.Thread(target=lambda: result.update(zip(("code", "body"), post(p2p, "/mine", {"miner_address": MINER}))))
    t.start()
    try:
        assert searching.wait(10)
        t0 = time.time()
        code, status = _get(p2p, "/status")
        assert code == 200 and time.time() - t0 < 2
        # a competing block from a peer is accepted while we search
        rival = real_solve(bc.build_block_template(OTHER, []))
        code, _ = post(p2p, "/receive_block", {"block": rival.to_dict()})
        assert code == 200
        assert bc.get_latest_block().hash == rival.hash
    finally:
        release.set()
        t.join(15)
    assert lock_was_free["v"] is True
    assert result["code"] == 409                                # our block was for the old tip: dropped, not added
    assert bc.get_latest_block().hash == rival.hash


def test_mine_endpoint_adds_the_block_when_the_tip_is_unchanged(node):
    code, body = post(node, "/mine", {"miner_address": MINER})
    assert code == 200 and body["block"]["index"] == 1
    assert node.blockchain.get_latest_block().index == 1


def test_two_mine_requests_are_served_one_after_the_other(node):
    out = []
    ts = [threading.Thread(target=lambda: out.append(post(node, "/mine", {"miner_address": MINER})[0])) for _ in range(2)]
    [t.start() for t in ts]
    [t.join(30) for t in ts]
    assert sorted(out) == [200, 200]
    assert node.blockchain.get_latest_block().index == 2
