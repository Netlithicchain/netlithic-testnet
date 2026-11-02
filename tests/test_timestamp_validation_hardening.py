"""Timestamp validation hardening: blocks from the fork on must carry a real, finite time, and old history with a
malformed time can never make the difficulty or clock rules raise."""
import math

import pytest

import core
from core import Block
from conftest import build_block

NOT_A_TIME = math.inf - math.inf          # a float that is not a real number
BAD_VALUES = [NOT_A_TIME, math.inf, -math.inf]
ADDR = "ab" * 16


def fake(index, ts, work):
    return Block(index=index, timestamp=ts, transactions=[], previous_hash="", nonce=0, difficulty=work, hash="")


@pytest.fixture
def post_fork_chain(chain, monkeypatch):
    monkeypatch.setattr(core, "LWMA_FORK_HEIGHT", 1)
    monkeypatch.setattr(core, "LWMA_MIN_WORK", 16)
    return chain


def _block_with_time(bc, ts):
    reward = core.make_reward_tx(ADDR, bc.expected_reward(bc.chain))
    return build_block(bc, [reward], timestamp=ts)


@pytest.mark.parametrize("bad", BAD_VALUES, ids=["value0", "value1", "value2"])
def test_timestamp_validation_rejects_non_finite_time_after_the_fork(post_fork_chain, bad):
    bc = post_fork_chain
    blk = _block_with_time(bc, bad)
    reason = bc.block_reject_reason(blk, bc.chain[-1], bc.chain[:-1])
    assert reason == "timestamp is not a valid time"
    assert bc.add_block(blk.to_dict()) is False
    assert len(bc.chain) == 1


def test_timestamp_validation_still_accepts_a_normal_block_after_the_fork(post_fork_chain):
    bc = post_fork_chain
    blk = _block_with_time(bc, None)
    assert bc.block_reject_reason(blk, bc.chain[-1], bc.chain[:-1]) is None
    assert bc.add_block(blk.to_dict()) is True


def test_timestamp_validation_rejects_it_before_any_difficulty_work(post_fork_chain, monkeypatch):
    bc = post_fork_chain

    def boom(*a, **k):
        raise AssertionError("difficulty rules ran on an invalid timestamp")

    monkeypatch.setattr(bc, "allowed_difficulties", boom)
    blk = _block_with_time(bc, NOT_A_TIME)
    assert bc.block_reject_reason(blk, bc.chain[-1], bc.chain[:-1]) == "timestamp is not a valid time"


def test_timestamp_validation_old_history_cannot_break_the_rules(chain):
    """A malformed time in blocks from before the fork counts as 0.0 for the median, the LWMA and the stall rule."""
    t0 = 1.79e9
    for bad in BAD_VALUES:
        hist = [fake(4990 + i, t0 + 600 * i, 2) for i in range(9)]
        hist[7] = fake(4997, bad, 2)
        hist += [fake(4999, t0 + 600 * 9, 2)]
        clean = list(hist)
        clean[7] = fake(4997, 0.0, 2)
        assert core.median_time_past(hist) == core.median_time_past(clean)
        assert chain.difficulty_context(hist) == chain.difficulty_context(clean)
        post = hist + [fake(5000 + i, t0 + 6000 + 60 * i, 256) for i in range(5)]
        post_clean = clean + post[len(hist):]
        got = core.lwma_next_work(post)
        assert isinstance(got, int) and got == core.lwma_next_work(post_clean)
        ts = t0 + 7000
        assert chain.allowed_difficulties(post, ts) == chain.allowed_difficulties(post_clean, ts)


def test_timestamp_validation_helper_values():
    assert core.finite_timestamp(12.5) == 12.5
    for bad in BAD_VALUES:
        assert core.finite_timestamp(bad) == 0.0
        assert core.timestamp_ms(bad) == 0
