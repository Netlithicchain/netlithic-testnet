"""Pause-switch practice drill (a local practice run).

Everything here uses THROWAWAY keys generated inside the test (never a real key,
never wallets.json of the repo: the `chain`/`wallets` fixtures run in tmp_path).

Part 1  uses the REAL constants (fork 5000, 1440-block payout period, 10,080-block
        expiry) on a synthetic chain that is placed directly into a Blockchain
        object, then runs the real consensus functions on top of it.
Part 2  uses scaled-down constants and several in-process nodes that pass blocks
        to each other through add_block / replace_chain.
Part 3  (tests/test_pause_drill_http.py) real node processes talking HTTP on localhost.
"""
import hashlib
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import contributions as c
import core
import economics as eco
import pause
from conftest import build_block
from core import Block, Blockchain

NLT = eco.BASE_UNITS_PER_NLT
MINER_A = "a1" * 16
MINER_B = "b2" * 16


def throwaway_key() -> str:
    return Ed25519PrivateKey.generate().private_bytes_raw().hex()


def set_pause_key(monkeypatch, key_hex: str) -> str:
    pub = pause.make_pause_tx(key_hex, True, "x", 1, "c")["pubkey"]
    monkeypatch.setattr(pause, "PAUSE_PUBKEY", pub)
    return pub


def _fake_hash(i: int) -> str:
    return hashlib.sha256(f"synthetic-{i}".encode()).hexdigest()


def synth_chain(bc, upto: int, transfer, miner_for, pause_at=None, step=60.0):
    """Blocks 1..upto appended to bc.chain directly (no PoW, no validation).

    transfer: a signed transfer dict reused in every block so blocks count as
              non-empty (points only read sender/recipient/amount).
    miner_for(i): reward address for block i.
    pause_at: {height: pause tx dict}
    Timestamps run 60 s apart and end about 'now' so that real blocks can be
    mined on top.
    """
    pause_at = pause_at or {}
    chain = [bc.chain[0]]
    ts0 = time.time() - (upto + 1) * step
    for i in range(1, upto + 1):
        txs = [core.make_reward_tx(miner_for(i), 10 * NLT), transfer]
        if i in pause_at:
            txs.append(pause.normalize_pause_tx(pause_at[i]))
        diff = 2 if i < core.LWMA_FORK_HEIGHT else core.LWMA_MIN_WORK
        chain.append(Block(index=i, timestamp=ts0 + i * step, transactions=txs,
                           previous_hash=chain[-1].hash, nonce=0, difficulty=diff,
                           hash=_fake_hash(i)))
    bc.chain = chain
    return chain


@pytest.fixture
def real_bc(tmp_path, monkeypatch, wallets):
    """Blockchain with the real shipped economics (chain id netlithic-dev-1)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Blockchain, "save_chain", lambda self: None)
    alice, bob = wallets
    bc = Blockchain()
    assert core.SECURITY_FORK_HEIGHT == 5000 and core.LWMA_FORK_HEIGHT == 5000
    assert bc.econ.epoch_length_at(5000) == 1440 and pause.PAUSE_EXPIRY_BLOCKS == 10080
    transfer = alice.create_transaction(bob.address, 2 * NLT, nonce=1)
    return bc, transfer


# ------------------------------------------------------------------ Part 1

def test_real_constants_first_payout_height_is_7880_with_the_one_day_lag(real_bc):
    """Points v4: block 6440 is still an epoch block but pays nothing (the first period is paid one period
    later, at 7880)."""
    bc, tr = real_bc
    synth_chain(bc, 7879, tr, lambda i: MINER_A)
    assert [h for h in range(5000, 7881) if bc.econ.is_epoch_block(h)] == [6440, 7880]
    assert bc.expected_payouts(bc.chain[:6440], 6440) == []   # lag: nothing to pay yet
    assert bc.expected_payouts(bc.chain, 7880)                # unpaused: pays period 1
    assert bc.expected_payouts(bc.chain, 7880)[0]["recipient"] == MINER_A


def test_real_constants_pause_blocks_every_payout_until_expiry_then_resumes_without_back_pay(real_bc, monkeypatch):
    bc, tr = real_bc
    k = throwaway_key()
    set_pause_key(monkeypatch, k)
    cid = bc.econ.chain_id
    ptx = pause.make_pause_tx(k, True, "Drill: halt payouts", 1, cid)
    PAUSE_H = 6001
    # miner A works up to 15,079, miner B only after that
    synth_chain(bc, 17000, tr, lambda i: MINER_A if i < 15080 else MINER_B, pause_at={PAUSE_H: ptx})
    epochs = [h for h in range(5000, 17001) if bc.econ.is_epoch_block(h)]
    assert epochs[:3] == [6440, 7880, 9320]
    expires_after = PAUSE_H + 10080                       # last height still paused
    assert expires_after == 16081
    for h in epochs:
        paid = bc.expected_payouts(bc.chain[:h], h)
        if PAUSE_H < h <= expires_after:
            assert paid == [], f"payout at {h} should be paused"
        else:
            assert paid, f"payout at {h} should be paid"
    # epoch 5000 itself is not a payout block; 6000 is before the pause; first paused = 6440
    assert epochs[0] > PAUSE_H
    # first epoch after expiry is 5000 + 1440*8 = 16520. With the one-day lag it pays the window
    # [13640, 15080), which is all miner A's. Periods whose payout block fell INSIDE the pause (6440 .. 16081)
    # are never back-paid: their coins stay in the pool.
    first_after = [h for h in epochs if h > expires_after][0]
    assert first_after == 16520
    paid = bc.expected_payouts(bc.chain[:first_after], first_after)
    assert {p["recipient"] for p in paid} == {MINER_A}
    # the next payout (17960) would pay [15080, 16520): miner B only


def test_real_constants_boundary_exactly_at_expiry(real_bc, monkeypatch):
    """Pause tx mined at height P is in force for heights P+1 .. P+10080 (inclusive)."""
    bc, tr = real_bc
    k = throwaway_key()
    set_pause_key(monkeypatch, k)
    ptx = pause.make_pause_tx(k, True, "edge", 1, bc.econ.chain_id)
    synth_chain(bc, 6000, tr, lambda i: MINER_A, pause_at={6000: ptx})
    assert not pause.state(bc.chain, 6000)["paused"]
    assert pause.state(bc.chain, 6001)["paused"]
    assert pause.state(bc.chain, 6000 + 10080)["paused"]
    st = pause.state(bc.chain, 6000 + 10081)
    assert not st["paused"] and st["expired"] and st["expires_at"] == 16080


def test_pause_in_the_payout_block_takes_effect_next_period(real_bc, monkeypatch):
    """A pause mined IN the payout block 7880 does not stop that block's payout,
    but it does stop the next one (9320). Documented timing gap: to stop a payout
    the pause must be mined at least one block BEFORE the payout block."""
    bc, tr = real_bc
    k = throwaway_key()
    set_pause_key(monkeypatch, k)
    synth_chain(bc, 7879, tr, lambda i: MINER_A)
    ptx = pause.make_pause_tx(k, True, "too late for this one", 1, bc.econ.chain_id)
    blk = bc.mine_block(MINER_B, [ptx])                    # block 7880 with payouts AND the pause
    assert blk.index == 7880
    assert any(t.get("type") == "payout" for t in blk.transactions)
    assert any(pause.is_pause_tx(t) for t in blk.transactions)
    assert bc.add_block(blk.to_dict()), bc.last_reject_reason
    assert pause.state(bc.chain)["paused"]
    assert bc.expected_payouts(bc.chain, 9320) == []


def test_pause_one_block_before_payout_stops_it(real_bc, monkeypatch):
    bc, tr = real_bc
    k = throwaway_key()
    set_pause_key(monkeypatch, k)
    synth_chain(bc, 7878, tr, lambda i: MINER_A)
    ptx = pause.make_pause_tx(k, True, "just in time", 1, bc.econ.chain_id)
    b = bc.mine_block(MINER_B, [ptx])                      # block 7879 carries the pause
    assert b.index == 7879 and bc.add_block(b.to_dict()), bc.last_reject_reason
    b = bc.mine_block(MINER_B, [])                         # block 7880: paused
    assert b.index == 7880
    assert not any(t.get("type") == "payout" for t in b.transactions)
    assert bc.add_block(b.to_dict()), bc.last_reject_reason
    # and a block that DOES include payouts at 6440 is invalid while paused
    # (rebuild on the 7879 tip)
    prior = bc.chain[:7880]
    forced = core.make_reward_tx(MINER_B, bc.expected_reward(prior))
    fake_payout = {"type": "payout", "epoch": 2, "recipient": MINER_A, "amount": NLT,
                   "vest_start": 7880, "vest_end": 7880 + 8640}
    bad = build_block(bc, [forced, fake_payout], chain=prior)
    assert not bc.add_block(bad.to_dict())


def test_fork_boundary_pause_rejected_at_4999_accepted_at_5000(real_bc, monkeypatch):
    bc, tr = real_bc
    k = throwaway_key()
    set_pause_key(monkeypatch, k)
    ptx = pause.make_pause_tx(k, True, "fork edge", 1, bc.econ.chain_id)
    synth_chain(bc, 4998, tr, lambda i: MINER_A)           # next block index = 4999
    assert not bc.validate_candidate_tx_against_chain_and_mempool(ptx, [])
    pre = build_block(bc, [core.make_reward_tx(MINER_B, bc.expected_reward(bc.chain)),
                           pause.normalize_pause_tx(ptx)])
    assert pre.index == 4999 and not bc.add_block(pre.to_dict())
    # an honest block 4999 (no pause) is fine
    ok = bc.mine_block(MINER_B, [ptx])
    assert ok.index == 4999 and not any(pause.is_pause_tx(t) for t in ok.transactions)
    assert bc.add_block(ok.to_dict()), bc.last_reject_reason
    # now the next block is 5000: pause accepted in mempool and in the block
    assert bc.validate_candidate_tx_against_chain_and_mempool(ptx, [])
    b5000 = bc.mine_block(MINER_B, [ptx])
    assert b5000.index == 5000 and any(pause.is_pause_tx(t) for t in b5000.transactions)
    assert bc.add_block(b5000.to_dict()), bc.last_reject_reason
    assert pause.state(bc.chain)["paused"]


def test_old_node_without_fork_rules_rejects_block_with_pause(real_bc, monkeypatch):
    """Why the version gate at block 4900 matters: a node that does not know the
    pause tx rejects the block that carries it and falls onto its own branch."""
    bc, tr = real_bc
    k = throwaway_key()
    set_pause_key(monkeypatch, k)
    synth_chain(bc, 5000, tr, lambda i: MINER_A)
    ptx = pause.make_pause_tx(k, True, "old nodes cannot follow", 1, bc.econ.chain_id)
    b = bc.mine_block(MINER_B, [ptx])
    assert any(pause.is_pause_tx(t) for t in b.transactions)
    old = Blockchain()
    old.chain = list(bc.chain)
    assert bc.add_block(b.to_dict())
    # simulate an old node: pause txs are not valid for it (fork rules never active)
    monkeypatch.setattr(core, "SECURITY_FORK_HEIGHT", 10 ** 9)
    assert not old.add_block(b.to_dict())
    assert len(old.chain) == 5001 and len(bc.chain) == 5002


def test_stored_block_cannot_be_malleated_into_a_state_bricking_pause(real_bc, monkeypatch):
    """A miner who edits a pause tx (nonce '1' as text, 1.0 float, upper-case key,
    extra fields) cannot change what is stored/hashed: blocks are normalized, so
    pause.state() never sees a non-int nonce."""
    bc, tr = real_bc
    k = throwaway_key()
    set_pause_key(monkeypatch, k)
    synth_chain(bc, 5000, tr, lambda i: MINER_A)
    good = pause.make_pause_tx(k, True, "malleability", 1, bc.econ.chain_id)
    mal = dict(good, nonce="1", pubkey=good["pubkey"].upper(), junk="x" * 1000)
    b = build_block(bc, [core.make_reward_tx(MINER_B, bc.expected_reward(bc.chain)), mal])
    assert bc.add_block({**b.to_dict()}), bc.last_reject_reason
    st = pause.state(bc.chain)
    assert st["nonce"] == 1 and isinstance(st["nonce"], int)
    assert "junk" not in bc.chain[-1].transactions[1]
    # a follow-up resume still works
    resume = pause.make_pause_tx(k, False, "ok", 2, bc.econ.chain_id)
    assert bc.validate_candidate_tx_against_chain_and_mempool(resume, [])


# ------------------------------------------------------------------ Part 2
# Several in-process nodes, scaled constants: security fork at 3, payout period 4
# blocks, pause expiry 6 blocks.

from dataclasses import replace


def _econ():
    base = dict(blocks_per_month=10, epoch_blocks=100, vesting_epochs=2, chain_id="netlithic-dev-1",
                unlocks={"community": (0, 240), "treasury": (0, 48), "reserve": (20, 72)})
    return replace(eco.load_economics(), **base)


class Net:
    """N nodes with identical genesis; blocks mined on one are offered to all."""

    def __init__(self, n=3):
        self.nodes = [Blockchain(econ=_econ()) for _ in range(n)]

    def mine(self, miner, txs=(), via=0, expect_all=True):
        blk = self.nodes[via].mine_block(miner, list(txs))
        d = blk.to_dict()
        res = [nd.add_block(d) for nd in self.nodes]
        if expect_all:
            assert all(res), [nd.last_reject_reason for nd in self.nodes]
        return blk, res

    def states(self):
        return [pause.state(nd.chain) for nd in self.nodes]

    def agree(self):
        tips = {nd.chain[-1].hash for nd in self.nodes}
        pst = {(s["paused"], s["nonce"]) for s in self.states()}
        assert len(tips) == 1 and len(pst) == 1
        return pst.pop()


@pytest.fixture
def net(tmp_path, monkeypatch, wallets):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(eco.TESTNET_SHORT_EPOCHS, "netlithic-dev-1", (3, 4))
    monkeypatch.setattr(core, "SECURITY_FORK_HEIGHT", 3)
    monkeypatch.setattr(c, "MIN_ADDRESS_AGE_BLOCKS", 0)
    monkeypatch.setattr(Blockchain, "save_chain", lambda self: None)
    k = throwaway_key()
    pub = set_pause_key(monkeypatch, k)
    return Net(3), k, pub, wallets


def test_drill_valid_pause_all_nodes_agree_and_skip_payout(net):
    n, k, _, (alice, bob) = net
    cid = n.nodes[0].econ.chain_id
    n.mine(alice.address)                                                           # 1
    n.mine(alice.address)                                                           # 2
    n.mine(bob.address, [pause.make_pause_tx(k, True, "drill", 1, cid)], via=1)    # 3 (first fork block)
    for i in (1, 2, 3):
        n.mine(bob.address, [alice.create_transaction("ab" * 16, NLT, nonce=i)], via=i % 3)   # 4..6
    assert n.agree() == (True, 1)
    blk, _ = n.mine(alice.address, via=2)                                           # 7 is a payout height
    assert not any(t.get("type") == "payout" for t in blk.transactions)
    # a miner that ignores the pause and adds a payout is rejected by every node
    n.nodes[0].chain  # unchanged tip
    prior = n.nodes[0].chain
    fake = {"type": "payout", "epoch": 1, "recipient": "cd" * 16, "amount": 5 * NLT,
            "vest_start": 8, "vest_end": 20}
    bad = build_block(n.nodes[0], [core.make_reward_tx(alice.address, n.nodes[0].expected_reward(prior)), fake])
    assert [nd.add_block(bad.to_dict()) for nd in n.nodes] == [False, False, False]
    n.agree()


def test_drill_forged_and_malformed_pauses_rejected_by_every_node(net):
    n, k, pub, _ = net
    cid = n.nodes[0].econ.chain_id
    n.mine("a1" * 16); n.mine("a1" * 16); n.mine("a1" * 16)                          # to height 3
    attacker = throwaway_key()
    good = pause.make_pause_tx(k, True, "real", 1, cid)
    forged = [
        pause.make_pause_tx(attacker, True, "attacker key", 1, cid),
        pause.make_pause_tx(attacker, False, "attacker resume", 1, cid),
        dict(good, reason="edited after signing"),
        dict(good, paused=False),
        dict(good, nonce=2),
        dict(good, chain_id="netlithic-other") if "chain_id" in good else pause.make_pause_tx(k, True, "wc", 1, "netlithic-other"),
        dict(good, sig=("0" * len(good["sig"]))) if "sig" in good else dict(good, signature="0" * 128),
        dict(good, pubkey=pause.make_pause_tx(attacker, True, "x", 1, cid)["pubkey"]),   # honest sig, other key
        {"type": "pause"},
        {"type": "pause", "paused": "yes", "reason": "x", "nonce": 1, "pubkey": pub, "signature": "00"},
        pause.make_pause_tx(k, True, "x" * 5000, 1, cid),                            # oversize reason
        pause.make_pause_tx(k, True, "", 1, cid),                                    # empty reason
    ]
    for f in forged:
        for nd in n.nodes:
            assert not nd.validate_candidate_tx_against_chain_and_mempool(f, []), f
        try:
            nrm = pause.normalize_pause_tx(f)
        except Exception:
            continue                                                                    # cannot even be encoded in a block
        b = build_block(n.nodes[0], [core.make_reward_tx("b2" * 16, n.nodes[0].expected_reward(n.nodes[0].chain)), nrm])
        assert [nd.add_block(b.to_dict()) for nd in n.nodes] == [False] * 3, f
    assert n.agree() == (False, 0)
    # the genuine tx still works afterwards: forged attempts burned nothing
    n.mine("b2" * 16, [good])
    assert n.agree() == (True, 1)


def test_drill_replay_in_later_block_and_after_resume_rejected(net):
    n, k, _, _ = net
    cid = n.nodes[0].econ.chain_id
    for _ in range(3):
        n.mine("a1" * 16)
    p1 = pause.make_pause_tx(k, True, "first", 1, cid)
    r2 = pause.make_pause_tx(k, False, "resume", 2, cid)
    n.mine("a1" * 16, [p1])
    n.mine("a1" * 16, [r2])
    assert n.agree() == (False, 2)
    for old in (p1, r2):                                                            # replay of already-mined txs
        for nd in n.nodes:
            assert not nd.validate_candidate_tx_against_chain_and_mempool(old, [])
        b = build_block(n.nodes[0], [core.make_reward_tx("a1" * 16, n.nodes[0].expected_reward(n.nodes[0].chain)),
                                     pause.normalize_pause_tx(old)])
        assert [nd.add_block(b.to_dict()) for nd in n.nodes] == [False] * 3
    # a fresh pause with the next nonce is fine
    n.mine("a1" * 16, [pause.make_pause_tx(k, True, "second", 3, cid)])
    assert n.agree() == (True, 3)


def test_drill_replay_across_chain_ids_rejected(net, monkeypatch):
    """A tx signed for another network id cannot be replayed here."""
    n, k, _, _ = net
    for _ in range(3):
        n.mine("a1" * 16)
    other = pause.make_pause_tx(k, True, "testnet copy", 1, "netlithic-testnet-9")
    assert not n.nodes[0].validate_candidate_tx_against_chain_and_mempool(other, [])


def test_drill_expiry_renew_and_late_renew(net, monkeypatch):
    n, k, _, _ = net
    monkeypatch.setattr(pause, "PAUSE_EXPIRY_BLOCKS", 6)
    cid = n.nodes[0].econ.chain_id
    for _ in range(3):
        n.mine("a1" * 16)                                                            # 1..3
    n.mine("a1" * 16, [pause.make_pause_tx(k, True, "p", 1, cid)])                  # 4 -> active 5..10
    n.mine("a1" * 16)                                                                # 5
    n.mine("a1" * 16, [pause.make_pause_tx(k, True, "renew", 2, cid)])              # 6 -> active 7..12
    st = pause.state(n.nodes[0].chain)
    assert st["paused"] and st["expires_at"] == 12
    for _ in range(5):
        n.mine("a1" * 16)                                                            # 7..11
    assert pause.state(n.nodes[0].chain)["paused"]       # next height 12 is still covered
    n.mine("a1" * 16)                                                                # 12
    st = pause.state(n.nodes[0].chain)                   # next height 13 is past expires_at
    assert not st["paused"] and st["expired"]
    n.agree()
    # late "renew" after lapse is a brand-new pause (new log entry, new 6-block window)
    n.mine("a1" * 16, [pause.make_pause_tx(k, True, "late", 3, cid)])               # 13 -> active 14..19
    st = pause.state(n.nodes[0].chain)
    assert st["paused"] and st["expires_at"] == 19 and len(st["log"]) == 3


def test_drill_resume_ends_early_and_unneeded_resume(net):
    n, k, _, _ = net
    cid = n.nodes[0].econ.chain_id
    for _ in range(3):
        n.mine("a1" * 16)
    n.mine("a1" * 16, [pause.make_pause_tx(k, True, "p", 1, cid)])
    n.mine("a1" * 16, [pause.make_pause_tx(k, False, "all clear", 2, cid)])
    assert n.agree() == (False, 2)
    # a second resume while not paused is harmless but still consumes a nonce (note for runbook)
    r = pause.make_pause_tx(k, False, "again", 3, cid)
    ok = n.nodes[0].validate_candidate_tx_against_chain_and_mempool(r, [])
    if ok:
        n.mine("a1" * 16, [r])
        assert n.agree() == (False, 3)


def test_drill_pause_and_resume_in_same_block_orders_by_position(net):
    n, k, _, _ = net
    cid = n.nodes[0].econ.chain_id
    for _ in range(3):
        n.mine("a1" * 16)
    n.mine("a1" * 16, [pause.make_pause_tx(k, True, "p", 1, cid), pause.make_pause_tx(k, False, "r", 2, cid)])
    st = pause.state(n.nodes[0].chain)
    n.agree()
    # both txs are valid in sequence; the last one wins and the pause is gone
    assert st["nonce"] == 2 and not st["paused"]


def test_drill_reorg_removes_pause_on_nodes_that_switch(net):
    """Node 0 mined a pause on a short fork; the honest majority chain has none and more work.
    Node 0 follows the heavier chain and un-pauses automatically; payouts resume."""
    n, k, _, _ = net
    cid = n.nodes[0].econ.chain_id
    for _ in range(3):
        n.mine("a1" * 16)
    base = list(n.nodes[0].chain)
    # node 0 alone adds the pause block
    b = n.nodes[0].mine_block("c3" * 16, [pause.make_pause_tx(k, True, "fork pause", 1, cid)])
    assert n.nodes[0].add_block(b.to_dict())
    assert pause.state(n.nodes[0].chain)["paused"]
    # nodes 1 and 2 mine two blocks without it
    for _ in range(2):
        blk = n.nodes[1].mine_block("d4" * 16, [])
        assert n.nodes[1].add_block(blk.to_dict()) and n.nodes[2].add_block(blk.to_dict())
    heavy = n.nodes[1].to_list()
    assert n.nodes[0].replace_chain(heavy), n.nodes[0].last_reject_reason
    assert not pause.state(n.nodes[0].chain)["paused"]
    assert n.agree() == (False, 0)


def test_drill_pause_key_unset_means_inert_after_fork(net, monkeypatch):
    n, k, _, _ = net
    for _ in range(3):
        n.mine("a1" * 16)
    monkeypatch.setattr(pause, "PAUSE_PUBKEY", "")
    assert not n.nodes[0].validate_candidate_tx_against_chain_and_mempool(
        pause.make_pause_tx(k, True, "no key configured", 1, n.nodes[0].econ.chain_id), [])


# ------------------------------------------------------------------ Part 2b: gaps found by the drill

def test_gap_node_without_pause_key_splits_from_nodes_with_it(net, monkeypatch):
    """The shipped tag v0.5.0 has PAUSE_PUBKEY = "". If the key is added in a later
    release, any node still on an empty key rejects the first block that carries a
    pause tx and falls onto its own branch. The 4900 version gate (min peer version
    0.5.0) does NOT protect against this: a 0.5.0 node is still 'allowed'."""
    n, k, pub, _ = net
    for _ in range(3):
        n.mine("a1" * 16)
    blk = n.nodes[0].mine_block("b2" * 16, [pause.make_pause_tx(k, True, "first ever pause", 1, n.nodes[0].econ.chain_id)])
    assert n.nodes[0].add_block(blk.to_dict()) and n.nodes[1].add_block(blk.to_dict())
    monkeypatch.setattr(pause, "PAUSE_PUBKEY", "")          # node 2 runs a build with no key
    assert not n.nodes[2].add_block(blk.to_dict())
    assert len(n.nodes[2].chain) == 4 and len(n.nodes[0].chain) == 5


def test_gap_rotating_the_pause_key_invalidates_history_with_old_key_pauses(net, monkeypatch):
    """PAUSE_PUBKEY is a single constant, so a release that rotates it makes every
    block that already contains a pause tx signed by the OLD key invalid when a new
    node replays the chain from genesis (initial sync / restart validation)."""
    n, k, pub, _ = net
    cid = n.nodes[0].econ.chain_id
    for _ in range(3):
        n.mine("a1" * 16)
    n.mine("a1" * 16, [pause.make_pause_tx(k, True, "old key pause", 1, cid)])
    n.mine("a1" * 16)
    wire = n.nodes[0].to_list()
    assert Blockchain(econ=_econ()).is_valid_chain(wire)
    new_key = throwaway_key()
    set_pause_key(monkeypatch, new_key)                      # "release with rotated key"
    assert not Blockchain(econ=_econ()).is_valid_chain(wire)


def test_gap_renewals_are_unlimited_one_key_can_pause_forever():
    nonce = 0
    events = {i * 10000: True for i in range(1, 8)}          # 7 renewals, each just before lapse
    blocks = []
    for height in range(0, 80001):
        txs = []
        if height in events:
            nonce += 1
            txs.append({"type": "pause", "paused": True, "reason": "r", "nonce": nonce})
        blocks.append({"index": height, "transactions": txs})
    assert pause.state(blocks)["paused"]                     # still paused ~55 days later


def test_gap_orphaned_pause_is_not_rebroadcast_but_can_be_resubmitted(net):
    """After a reorg removes the block that carried the pause, the pause is gone and
    the tx is not put back in the mempool (node code only revalidates what was already
    pending). The same signed tx is valid again, so the operator can resubmit it."""
    n, k, _, _ = net
    cid = n.nodes[0].econ.chain_id
    for _ in range(3):
        n.mine("a1" * 16)
    ptx = pause.make_pause_tx(k, True, "fork pause", 1, cid)
    b = n.nodes[0].mine_block("c3" * 16, [ptx])
    assert n.nodes[0].add_block(b.to_dict())
    for _ in range(2):
        blk = n.nodes[1].mine_block("d4" * 16, [])
        assert n.nodes[1].add_block(blk.to_dict())
    assert n.nodes[0].replace_chain(n.nodes[1].to_list())
    assert not pause.state(n.nodes[0].chain)["paused"]
    assert n.nodes[0].validate_candidate_tx_against_chain_and_mempool(ptx, [])      # resubmit works
