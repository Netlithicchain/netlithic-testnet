"""Security fork rules: reorg depth limit, checkpoints, stall-rule hardening,
anti-farming v2 and the payout pause switch."""
import json
import time
from dataclasses import replace

import pytest

import contributions as c
import core
import economics as eco
import pause
from conftest import build_block
from core import Blockchain

NLT = eco.BASE_UNITS_PER_NLT
A, B, C, D = "a" * 32, "b" * 32, "c" * 32, "d" * 32


def small_fork(monkeypatch, height=3, min_work=16, sec=None):
    monkeypatch.setattr(core, "LWMA_FORK_HEIGHT", height)
    monkeypatch.setattr(core, "LWMA_MIN_WORK", min_work)
    monkeypatch.setattr(core, "SECURITY_FORK_HEIGHT", height if sec is None else sec)


def grow(bc, chain, n, step=60.0, miner=B):
    """Extend `chain` (a list of Blocks) by n blocks; return the new list."""
    chain = list(chain)
    for _ in range(n):
        txs = [core.make_reward_tx(miner, bc.expected_reward(chain))]
        b = build_block(bc, txs, chain=chain, timestamp=chain[-1].timestamp + step)
        assert bc.block_reject_reason(b, chain[-1], chain[:-1]) is None
        chain.append(b)
    return chain


# ---------------- reorg depth limit ----------------

def test_deep_reorg_rejected_shallow_accepted(chain, monkeypatch):
    small_fork(monkeypatch)
    monkeypatch.setattr(core, "MAX_REORG_DEPTH", 3)
    base = time.time() - 5000
    ours = grow(chain, chain.chain, 10, step=1.0)
    ours = [ours[0]] + [replace(b) for b in ours[1:]]
    chain.chain = ours
    # attacker forks 6 blocks back with more blocks (more work)
    deep = grow(chain, ours[:-6], 9, step=1.0, miner=D)
    assert chain.chain_work(deep) > chain.chain_work(ours)
    assert not chain.replace_chain([b.to_dict() for b in deep])
    assert "exceeds limit" in chain.last_reject_reason
    # 2-block reorg is fine
    shallow = grow(chain, ours[:-2], 4, step=1.0, miner=D)
    assert chain.replace_chain([b.to_dict() for b in shallow])
    assert chain.chain[-1].hash == shallow[-1].hash


def test_reorg_limit_off_before_fork(chain, monkeypatch):
    small_fork(monkeypatch, sec=10_000)
    monkeypatch.setattr(core, "MAX_REORG_DEPTH", 1)
    ours = grow(chain, chain.chain, 6, step=1.0)
    chain.chain = ours
    other = grow(chain, ours[:2], 7, step=1.0, miner=D)
    assert chain.replace_chain([b.to_dict() for b in other])


def test_reorg_depth_math(chain, monkeypatch):
    small_fork(monkeypatch)
    ours = grow(chain, chain.chain, 5, step=1.0)
    chain.chain = ours
    assert chain.reorg_depth(ours) == 0
    assert chain.reorg_depth(grow(chain, ours[:3], 4, step=1.0, miner=D)) == 3


def test_checkpoint_mismatch_rejects_chain(chain, monkeypatch, tmp_path):
    small_fork(monkeypatch)
    ours = grow(chain, chain.chain, 4, step=1.0)
    cp = tmp_path / "cp.json"
    monkeypatch.setattr(core, "CHECKPOINTS_FILE", str(cp))
    cp.write_text(json.dumps({"2": ours[2].hash}))
    assert chain.is_valid_chain([b.to_dict() for b in ours])
    cp.write_text(json.dumps({"2": "0" * 64}))
    core.reset_checkpoints()  # cached since R5; a node restart re-reads
    assert not chain.is_valid_chain([b.to_dict() for b in ours])


def test_bad_checkpoint_file_fails_loudly(tmp_path):
    p = tmp_path / "x.json"
    p.write_text("not json")
    with pytest.raises(core.CheckpointError):
        core.load_checkpoints(str(p))
    p.write_text(json.dumps({"5": "nothex"}))
    with pytest.raises(core.CheckpointError):
        core.load_checkpoints(str(p))
    assert core.load_checkpoints(str(tmp_path / "missing.json")) == {}


# ---------------- stall rule (1/8 work after 10 min) ----------------

def test_backdated_block_cannot_fake_a_stall(chain, monkeypatch):
    """Attack: miner backdates block N (allowed, it's > MTP), then claims the
    next block is 'late' relative to it and uses 1/8 work. Old rule allowed it."""
    for sec, expect_cheap in ((10_000, True), (3, False)):
        small_fork(monkeypatch, min_work=64, sec=sec)
        monkeypatch.setattr(core, "STALL_SECONDS", 600)
        now = time.time()
        hist = [chain.chain[0]]
        t0 = now - 25 * 60
        for i in range(12):
            txs = [core.make_reward_tx(B, chain.expected_reward(hist))]
            hist.append(build_block(chain, txs, chain=hist, timestamp=t0 + 120 * i))
        # backdated block: just above MTP, ~15 minutes in the past
        mtp = core.median_time_past(hist)
        txs = [core.make_reward_tx(D, chain.expected_reward(hist))]
        back = build_block(chain, txs, chain=hist, timestamp=mtp + 1)
        assert chain.block_reject_reason(back, hist[-1], hist[:-1]) is None
        hist.append(back)
        ts = min(now, back.timestamp + 600)  # 'late' vs the backdated block, not in the future
        allowed = chain.allowed_difficulties(hist, ts)
        assert (len(allowed) == 2) == expect_cheap, (sec, allowed)


def test_stall_rule_is_clock_free_and_future_claim_refused_on_receive(chain, monkeypatch):
    """F2: allowed_difficulties() must not read the wall clock (consensus is
    the same whenever a node validates). The 'future stall' trick is refused
    at the receive path instead."""
    small_fork(monkeypatch, min_work=64)
    monkeypatch.setattr(core, "STALL_SECONDS", 600)
    now = time.time()
    hist = [chain.chain[0]]
    for i in range(12):
        txs = [core.make_reward_tx(B, chain.expected_reward(hist))]
        hist.append(build_block(chain, txs, chain=hist, timestamp=now - 600 - 60 * (11 - i) + 60))
    newest = max(b.timestamp for b in hist[-core.MTP_WINDOW:])
    # same answer regardless of the local clock
    a1 = chain.allowed_difficulties(hist, newest + 600)
    with monkeypatch.context() as m:
        m.setattr(core.time, "time", lambda: 0.0)
        a2 = chain.allowed_difficulties(hist, newest + 600)
    assert a1 == a2 and len(a1) == 2
    # receive policy: emergency-work block from the future is refused...
    ts = newest + 600
    fake = core.Block(index=hist[-1].index + 1, timestamp=ts, transactions=[], previous_hash=hist[-1].hash,
                      nonce=0, hash="", difficulty=a1[1])
    assert chain.receive_time_reason(fake, hist, now=ts - 30) is not None
    # ...but accepted once that time has really come
    assert chain.receive_time_reason(fake, hist, now=ts + 1) is None
    # normal-work blocks are untouched by the policy
    fake.difficulty = a1[0]
    assert chain.receive_time_reason(fake, hist, now=ts - 30) is None


def test_genuine_stall_still_allowed(chain, monkeypatch):
    small_fork(monkeypatch, min_work=64)
    monkeypatch.setattr(core, "STALL_SECONDS", 600)
    now = time.time()
    hist = [chain.chain[0]]
    for i in range(12):
        txs = [core.make_reward_tx(B, chain.expected_reward(hist))]
        hist.append(build_block(chain, txs, chain=hist, timestamp=now - 2000 + 60 * i))
    assert len(chain.allowed_difficulties(hist, now)) == 2


# ---------------- anti-farming v2 ----------------

def blk(i, txs, miner=C):
    return {"index": i, "transactions": [{"type": "reward", "recipient": miner}] + txs}


def t(s, r, amt=NLT):
    return {"payload": {"sender": s, "recipient": r, "amount": amt, "nonce": 1}}


def test_reciprocal_ping_pong_earns_once():
    blocks = [blk(1, [t(A, B)]), blk(2, [t(B, A)])]
    old = c.points_from_blocks(blocks, epoch_blocks=1000)
    new = c.points_from_blocks(blocks, epoch_blocks=1000, rules_height=0)
    assert old[B]["point_tx_count"] == 1
    assert new[A]["point_tx_count"] == 1 and new[B]["point_tx_count"] == 0


def test_sender_cap_per_epoch():
    recips = [("%02x" % i) * 16 for i in range(60)]
    blocks = [blk(i + 1, [t(A, r)]) for i, r in enumerate(recips)]
    old = c.points_from_blocks(blocks, epoch_blocks=1000)
    new = c.points_from_blocks(blocks, epoch_blocks=1000, rules_height=0)
    assert old[A]["point_tx_count"] == 60
    assert new[A]["point_tx_count"] == c.SENDER_TX_CAP_PER_EPOCH


def test_new_address_earns_nothing_until_aged():
    first = {A: 100}
    blocks = [blk(1500, [t(A, B)]), blk(1600, [t(A, D)])]
    u = c.points_from_blocks(blocks, epoch_blocks=10000, rules_height=0, first_seen=first)[A]
    assert u["point_tx_count"] == 1  # only the one at 1600 (age 1500 >= 1440; age 1400 too young)


def test_rules_inactive_before_height():
    blocks = [blk(1, [t(A, B)]), blk(2, [t(B, A)])]
    u = c.points_from_blocks(blocks, epoch_blocks=1000, rules_height=5000)
    assert u[B]["point_tx_count"] == 1


def test_first_seen_heights():
    blocks = [blk(0, []), blk(1, [t(A, B)], miner=A), blk(2, [t(B, D)])]
    fs = c.first_seen_heights(blocks)
    assert fs[C] == 0 and fs[A] == 1 and fs[B] == 1 and fs[D] == 2


# ---------------- payout pause switch ----------------

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402


def _key():
    sk = Ed25519PrivateKey.generate()
    return sk.private_bytes_raw().hex()


def test_pause_disabled_by_default(chain, monkeypatch):
    monkeypatch.setattr(core, "SECURITY_FORK_HEIGHT", 0)
    monkeypatch.setattr(pause, "PAUSE_PUBKEY", "")
    tx = pause.make_pause_tx(_key(), True, "test", 1, chain.econ.chain_id)
    assert not chain.validate_candidate_tx_against_chain_and_mempool(tx, [])


def test_pause_signature_nonce_and_chain_rules(chain, monkeypatch):
    k = _key()
    other = _key()
    monkeypatch.setattr(core, "SECURITY_FORK_HEIGHT", 0)
    monkeypatch.setattr(pause, "PAUSE_PUBKEY", pause.make_pause_tx(k, True, "x", 1, "c")["pubkey"])
    cid = chain.econ.chain_id
    good = pause.make_pause_tx(k, True, "Investigating payout bug", 1, cid)
    assert chain.validate_candidate_tx_against_chain_and_mempool(good, [])
    assert not chain.validate_candidate_tx_against_chain_and_mempool(good, [good])  # one at a time
    assert not chain.validate_candidate_tx_against_chain_and_mempool(
        pause.make_pause_tx(other, True, "not aric", 1, cid), [])
    assert not chain.validate_candidate_tx_against_chain_and_mempool(
        pause.make_pause_tx(k, True, "wrong nonce", 2, cid), [])
    assert not chain.validate_candidate_tx_against_chain_and_mempool(
        pause.make_pause_tx(k, True, "other chain", 1, "netlithic-other"), [])
    tampered = dict(good, reason="changed after signing")
    assert not chain.validate_candidate_tx_against_chain_and_mempool(tampered, [])
    assert not chain.validate_candidate_tx_against_chain_and_mempool(
        pause.make_pause_tx(k, True, "", 1, cid), [])
    # mined into a block, it's public and logged; replaying it fails
    b = chain.mine_block(B, [good])
    assert chain.add_block(b.to_dict()), chain.last_reject_reason
    st = pause.state(chain.chain)
    assert st["paused"] and st["log"][0]["reason"] == "Investigating payout bug"
    assert not chain.validate_candidate_tx_against_chain_and_mempool(good, [])
    # a block carrying a replayed pause is rejected
    bad = build_block(chain, [core.make_reward_tx(B, chain.expected_reward(chain.chain)),
                              pause.normalize_pause_tx(good)])
    assert not chain.add_block(bad.to_dict())


def test_pause_tx_rejected_before_fork(chain, monkeypatch):
    k = _key()
    monkeypatch.setattr(core, "SECURITY_FORK_HEIGHT", 10_000)
    monkeypatch.setattr(pause, "PAUSE_PUBKEY", pause.make_pause_tx(k, True, "x", 1, "c")["pubkey"])
    tx = pause.make_pause_tx(k, True, "early", 1, chain.econ.chain_id)
    assert not chain.validate_candidate_tx_against_chain_and_mempool(tx, [])
    bad = build_block(chain, [core.make_reward_tx(B, chain.expected_reward(chain.chain)),
                              pause.normalize_pause_tx(tx)])
    assert not chain.add_block(bad.to_dict())


def _econ():
    base = dict(blocks_per_month=10, epoch_blocks=100, vesting_epochs=2, chain_id="netlithic-dev-1",
                unlocks={"community": (0, 240), "treasury": (0, 48), "reserve": (20, 72)})
    return replace(eco.load_economics(), **base)


def test_pause_skips_payouts_and_resume_restores(tmp_path, monkeypatch, wallets):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(eco.TESTNET_SHORT_EPOCHS, "netlithic-dev-1", (3, 4))
    monkeypatch.setattr(core, "SECURITY_FORK_HEIGHT", 0)
    monkeypatch.setattr(c, "MIN_ADDRESS_AGE_BLOCKS", 0)
    k = _key()
    monkeypatch.setattr(pause, "PAUSE_PUBKEY", pause.make_pause_tx(k, True, "x", 1, "c")["pubkey"])
    alice, bob = wallets
    bc = Blockchain(econ=_econ())
    cid = bc.econ.chain_id

    def mine(addr, txs=()):
        assert bc.add_block(bc.mine_block(addr, list(txs)).to_dict()), bc.last_reject_reason

    mine(alice.address)
    mine(alice.address, [pause.make_pause_tx(k, True, "Checking epoch math", 1, cid)])   # 2
    for n in (1, 2, 3):
        mine(bob.address, [alice.create_transaction("ab" * 16, NLT, nonce=n)])            # 3,4,5
    mine(bob.address)                                                                    # 6
    assert bc.expected_payouts(bc.chain, 7) == []
    mine(alice.address)                                                                  # 7: no payouts
    assert not any(t.get("type") == "payout" for t in bc.chain[7].transactions)
    mine(alice.address, [pause.make_pause_tx(k, False, "Fixed, resuming", 2, cid)])      # 8
    for n in (4, 5):
        mine(bob.address, [alice.create_transaction("ab" * 16, NLT, nonce=n)])            # 9,10
    assert bc.expected_payouts(bc.chain, 11)
    assert [e["paused"] for e in pause.state(bc.chain)["log"]] == [True, False]
    assert bc.is_valid_chain(bc.to_list())


def test_pause_over_http_end_to_end(node, monkeypatch, wallets):
    """Submit a pause tx to a live node, mine it, read /pause_status."""
    import json as _json
    import urllib.request
    from conftest import post
    monkeypatch.setattr(core, "SECURITY_FORK_HEIGHT", 0)
    k = _key()
    monkeypatch.setattr(pause, "PAUSE_PUBKEY", pause.make_pause_tx(k, True, "x", 1, "c")["pubkey"])
    alice, _ = wallets
    cid = node.chain_id()
    ptx = pause.make_pause_tx(k, True, "Drill: payouts paused", 1, cid)
    assert post(node, "/submit_tx", {"tx": ptx})[0] == 200
    assert post(node, "/submit_tx", {"tx": pause.make_pause_tx(k, True, "dup", 2, cid)})[0] == 400
    status, body = post(node, "/mine", {"miner_address": alice.address})
    assert status == 200, body
    assert node.mempool == []
    with urllib.request.urlopen(f"{node.self_url()}/pause_status", timeout=5) as r:
        st = _json.loads(r.read())
    assert st["paused"] and st["log"][0]["reason"] == "Drill: payouts paused"
    forged = pause.make_pause_tx(_key(), False, "attacker resumes", 2, cid)
    assert post(node, "/submit_tx", {"tx": forged})[0] == 400


# ---- pause auto-expiry (10080 blocks) and renewals ----

def _blocks(events, tip):
    """events: {height: paused_bool}; returns dict blocks 0..tip."""
    out, nonce = [], 0
    for h in range(tip + 1):
        txs = []
        if h in events:
            nonce += 1
            txs.append({"type": "pause", "paused": events[h], "reason": f"r{h}", "nonce": nonce})
        out.append({"index": h, "transactions": txs})
    return out


def test_default_expiry_is_10080():
    assert pause.PAUSE_EXPIRY_BLOCKS == 10080


def test_pause_expires_after_10080_blocks():
    bl = _blocks({10: True}, 20)
    assert not pause.state(bl, 10)["paused"]           # takes effect after its block
    assert pause.state(bl, 11)["paused"]
    assert pause.state(bl, 10 + 10080)["paused"]
    st = pause.state(bl, 10 + 10081)
    assert not st["paused"] and st["expired"] and st["expires_at"] == 10090


def test_renewal_extends_and_is_logged():
    bl = _blocks({10: True, 900: True}, 950)
    assert pause.state(bl, 1100)["paused"]
    assert pause.state(bl, 900 + 10080)["paused"] and not pause.state(bl, 900 + 10081)["paused"]
    assert [e["action"] for e in pause.pause_log(bl)] == ["pause", "renew"]
    assert pause.state(bl)["nonce"] == 2


def test_late_renewal_is_a_new_pause():
    bl = _blocks({10: True, 20000: True}, 20001)
    assert [e["action"] for e in pause.pause_log(bl)] == ["pause", "pause"]
    assert not pause.state(bl, 15000)["paused"] and pause.state(bl, 20001)["paused"]


def test_resume_ends_pause_early():
    bl = _blocks({10: True, 50: False}, 60)
    assert pause.state(bl[:50], 50)["paused"] and not pause.state(bl[:51], 51)["paused"]
    assert pause.pause_log(bl)[-1]["action"] == "resume"
    assert not pause.state(bl)["expired"]


def test_expired_pause_lets_payouts_resume_without_back_pay(tmp_path, monkeypatch, wallets):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(eco.TESTNET_SHORT_EPOCHS, "netlithic-dev-1", (3, 4))
    monkeypatch.setattr(core, "SECURITY_FORK_HEIGHT", 0)
    monkeypatch.setattr(c, "MIN_ADDRESS_AGE_BLOCKS", 0)
    monkeypatch.setattr(pause, "PAUSE_EXPIRY_BLOCKS", 6)
    k = _key()
    monkeypatch.setattr(pause, "PAUSE_PUBKEY", pause.make_pause_tx(k, True, "x", 1, "c")["pubkey"])
    alice, bob = wallets
    bc = Blockchain(econ=_econ())
    cid = bc.econ.chain_id

    def mine(addr, txs=()):
        assert bc.add_block(bc.mine_block(addr, list(txs)).to_dict()), bc.last_reject_reason

    mine(alice.address)
    mine(alice.address, [pause.make_pause_tx(k, True, "Checking", 1, cid)])   # 2, active 3..8
    for n in (1, 2, 3, 4):
        mine(bob.address, [alice.create_transaction("ab" * 16, NLT, nonce=n)])  # 3..6
    assert bc.expected_payouts(bc.chain, 7) == []                              # paused epoch skipped
    mine(alice.address)                                                        # 7
    for n in (5, 6, 7):
        mine(bob.address, [alice.create_transaction("ab" * 16, NLT, nonce=n)])  # 8..10
    st = pause.state(bc.chain)
    assert not st["paused"] and st["expired"]                                  # lapsed, no resume tx
    paid = bc.expected_payouts(bc.chain, 11)
    assert paid
    # no back pay: only points from the current window [height-epoch, height) count
    start = 11 - bc.econ.epoch_length_at(11)
    assert start > 3
    assert bc.is_valid_chain(bc.to_list())
