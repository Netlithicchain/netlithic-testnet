"""Adversarial and fuzz tests: malformed blocks/txs, replay, timestamp and
difficulty games (incl. the 10-minute 1/8-work stall rule), deep reorgs."""
import copy
import time

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

import core
from conftest import build_block

B, D = "b" * 32, "d" * 32
FUZZ = settings(max_examples=150, deadline=None,
                suppress_health_check=[HealthCheck.function_scoped_fixture])

json_scalar = st.one_of(st.none(), st.booleans(), st.integers(-2**70, 2**70),
                        st.floats(allow_nan=True, allow_infinity=True), st.text(max_size=40))
json_val = st.recursive(json_scalar, lambda kids: st.one_of(
    st.lists(kids, max_size=4), st.dictionaries(st.text(max_size=12), kids, max_size=5)),
    max_leaves=20)


def small_fork(monkeypatch, height=3):
    monkeypatch.setattr(core, "LWMA_FORK_HEIGHT", height)
    monkeypatch.setattr(core, "LWMA_MIN_WORK", 16)
    monkeypatch.setattr(core, "SECURITY_FORK_HEIGHT", height)


def grow(bc, chain, n, step=60.0, miner=B):
    chain = list(chain)
    for _ in range(n):
        txs = [core.make_reward_tx(miner, bc.expected_reward(chain))]
        b = build_block(bc, txs, chain=chain, timestamp=chain[-1].timestamp + step)
        assert bc.block_reject_reason(b, chain[-1], chain[:-1]) is None
        chain.append(b)
    return chain


# ---------------- malformed input never crashes, never gets in ----------------

@FUZZ
@given(data=st.one_of(json_val, st.dictionaries(
    st.sampled_from(["index", "timestamp", "transactions", "previous_hash", "nonce",
                     "difficulty", "hash"]), json_val, max_size=7)))
def test_fuzz_add_block_rejects_garbage(chain, data):
    before = len(chain.chain)
    assert chain.add_block(data) is False if isinstance(data, dict) else True
    assert len(chain.chain) == before


@FUZZ
@given(tx=json_val)
def test_fuzz_candidate_tx_never_crashes(chain, tx):
    assert chain.validate_candidate_tx_against_chain_and_mempool(tx, []) is False


@FUZZ
@given(blocks=st.lists(st.dictionaries(st.text(max_size=10), json_val, max_size=7), max_size=5))
def test_fuzz_replace_chain_rejects_garbage(chain, blocks):
    tip = chain.chain[-1].hash
    assert chain.replace_chain(blocks) is False
    assert chain.chain[-1].hash == tip


@FUZZ
@given(field=st.sampled_from(["index", "timestamp", "previous_hash", "nonce", "difficulty",
                              "hash", "transactions"]), val=json_val)
def test_fuzz_mutated_valid_block_rejected(chain, field, val):
    good = build_block(chain, [core.make_reward_tx(B, chain.expected_reward(chain.chain))])
    d = good.to_dict()
    if d[field] == val:
        return
    d[field] = val
    reason = None
    try:
        reason = chain.block_reject_reason(core.Block.from_dict(d), chain.chain[-1], chain.chain[:-1])
    except Exception:
        return  # from_dict refusing it is a rejection too
    assert reason is not None


# ---------------- replay ----------------

def test_tx_replay_in_later_block_rejected(chain, wallets):
    alice, bob = wallets
    chain.chain = grow(chain, chain.chain, 2, step=1.0, miner=alice.address)
    tx = alice.create_transaction(bob.address, 1, nonce=1)
    assert chain.validate_candidate_tx_against_chain_and_mempool(tx, [])
    b = chain.mine_block(alice.address, [tx])
    assert chain.add_block(b.to_dict())
    assert not chain.validate_candidate_tx_against_chain_and_mempool(tx, [])
    # forcing it into a block by hand is rejected too
    fees = int(tx["payload"]["fee"])
    reward = core.make_reward_tx(B, chain.expected_reward(chain.chain))
    reward["fees"] = fees
    replay = build_block(chain, [reward, tx])
    assert not chain.add_block(replay.to_dict())


def test_cross_chain_replay_rejected(chain, wallets):
    alice, bob = wallets
    tx = alice.create_transaction(bob.address, 1, nonce=1)
    forged = copy.deepcopy(tx)
    forged["payload"]["chain_id"] = "some-other-chain"
    assert not chain.validate_candidate_tx_against_chain_and_mempool(forged, [])


# ---------------- timestamps and difficulty ----------------

def test_timestamp_not_after_mtp_rejected(chain, monkeypatch):
    small_fork(monkeypatch)
    now = time.time()
    hist = grow(chain, chain.chain, 12, step=1.0)
    mtp = core.median_time_past(hist)
    bad = build_block(chain, [core.make_reward_tx(B, chain.expected_reward(hist))],
                      chain=hist, timestamp=mtp)
    assert "median" in chain.block_reject_reason(bad, hist[-1], hist[:-1])
    assert now  # silence


def test_future_timestamp_rejected(chain):
    ts = time.time() + core.MAX_FUTURE_BLOCK_SECONDS + 60
    bad = build_block(chain, [core.make_reward_tx(B, chain.expected_reward(chain.chain))],
                      timestamp=ts)
    assert "future" in chain.block_reject_reason(bad, chain.chain[-1], chain.chain[:-1])


@pytest.mark.parametrize("factor", [0.5, 2, 8])
def test_wrong_difficulty_rejected(chain, factor):
    import dataclasses
    want = chain.expected_block_difficulty(chain.chain)
    good = build_block(chain, [core.make_reward_tx(B, chain.expected_reward(chain.chain))])
    bad = dataclasses.replace(good, difficulty=max(1, int(want * factor)))
    assert bad.difficulty != want
    assert "difficulty" in chain.block_reject_reason(bad, chain.chain[-1], chain.chain[:-1])


def _recent_chain(chain, n=12, gap=60):
    t0 = time.time() - n * gap - 30
    hist = list(chain.chain)
    for i in range(n):
        txs = [core.make_reward_tx(B, chain.expected_reward(hist))]
        hist.append(build_block(chain, txs, chain=hist, timestamp=t0 + gap * i))
    return hist


def test_honest_stall_gets_one_eighth_work(chain, monkeypatch):
    small_fork(monkeypatch)
    hist = _recent_chain(chain, gap=1)
    # real 10+ minute silence after the newest block
    ts = hist[-1].timestamp + core.STALL_SECONDS + 1
    monkeypatch.setattr(core.time, "time", lambda: ts + 1)
    allowed = chain.allowed_difficulties(hist, ts)
    assert len(allowed) == 2 and allowed[1] == core.stall_work(allowed[0])
    assert allowed[1] < allowed[0]


def test_stall_just_under_ten_minutes_is_full_work(chain, monkeypatch):
    small_fork(monkeypatch)
    hist = _recent_chain(chain, gap=1)
    ts = hist[-1].timestamp + core.STALL_SECONDS - 1
    monkeypatch.setattr(core.time, "time", lambda: ts + 1)
    assert len(chain.allowed_difficulties(hist, ts)) == 1


def test_future_dated_stall_claim_rejected(chain, monkeypatch):
    """A miner can't claim a stall by stamping the block up to 120s ahead.
    Since review F2 this is a RECEIVE-path policy (receive_time_reason), not
    part of consensus validation (allowed_difficulties is clock-free)."""
    small_fork(monkeypatch)
    hist = _recent_chain(chain, gap=1)
    now = hist[-1].timestamp + core.STALL_SECONDS - 60
    allowed = chain.allowed_difficulties(hist, now + 90)
    if len(allowed) == 2:
        blk = core.Block(index=hist[-1].index + 1, timestamp=now + 90, transactions=[],
                         previous_hash=hist[-1].hash, nonce=0, hash="", difficulty=allowed[1])
        assert chain.receive_time_reason(blk, hist, now=now) is not None


@FUZZ
@given(offsets=st.lists(st.floats(-3000, 3000), min_size=1, max_size=11),
       ts_off=st.floats(-1000, 3000))
def test_fuzz_stall_never_cheap_without_real_gap(chain, offsets, ts_off):
    """Whatever the timestamps of recent blocks, cheap work needs the block to be
    >= 600s after the newest of the last 11 and not in the future."""
    hist = list(chain.chain)
    base = time.time() - 4000
    import dataclasses
    for i, off in enumerate(offsets):
        hist.append(dataclasses.replace(hist[-1], index=hist[-1].index + 1,
                                        timestamp=base + off))
    old = (core.LWMA_FORK_HEIGHT, core.SECURITY_FORK_HEIGHT)
    core.LWMA_FORK_HEIGHT = core.SECURITY_FORK_HEIGHT = 1
    try:
        ts = base + ts_off
        allowed = chain.allowed_difficulties(hist, ts)
    finally:
        core.LWMA_FORK_HEIGHT, core.SECURITY_FORK_HEIGHT = old
    newest = max(b.timestamp for b in hist[-core.MTP_WINDOW:])
    if len(allowed) == 2:
        assert ts - newest >= core.STALL_SECONDS and ts <= time.time()


# ---------------- deep reorg / 51% ----------------

def test_majority_attacker_cannot_rewrite_past_limit(chain, monkeypatch):
    small_fork(monkeypatch)
    monkeypatch.setattr(core, "MAX_REORG_DEPTH", 5)
    ours = grow(chain, chain.chain, 12, step=60.0)
    chain.chain = ours
    secret = grow(chain, ours[:-8], 12, step=30.0, miner=D)  # much more work
    assert chain.chain_work(secret) > chain.chain_work(ours)
    assert not chain.replace_chain([b.to_dict() for b in secret])
    assert chain.chain[-1].hash == ours[-1].hash


def test_heavier_but_invalid_chain_rejected(chain, monkeypatch):
    small_fork(monkeypatch)
    ours = grow(chain, chain.chain, 5, step=1.0)
    chain.chain = ours
    other = grow(chain, ours[:-2], 6, step=1.0, miner=D)
    data = [b.to_dict() for b in other]
    data[-3]["transactions"][0]["amount"] = 10**18  # inflate a reward
    assert not chain.replace_chain(data)
    assert chain.chain[-1].hash == ours[-1].hash
