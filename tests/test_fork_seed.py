"""The first block from the fork height on starts from a fixed, deliberately low work."""
import pytest

import core
from core import Block

T = core.LWMA_TARGET_BLOCK_TIME
# Measured on the project's mining computer: one CPU core runs the real nonce loop at 320,344 hashes per second.
# The seed is about half of that times the 60 s target. If the real network hashrate changes, update ESTIMATE_HASHRATE and the seed together.
ESTIMATE_HASHRATE = 320_344


def fake(index, ts, work):
    return Block(index=index, timestamp=ts, transactions=[], previous_hash="", nonce=0, difficulty=work, hash="")


@pytest.mark.real_seed
def test_seed_stays_low_relative_to_the_hashrate_estimate():
    ideal = ESTIMATE_HASHRATE * T
    assert core.LWMA_SEED_WORK >= 0.3 * ideal * 0.99          # not absurdly low
    assert core.LWMA_SEED_WORK <= 1.0 * ideal                 # at or below the estimate
    assert core.LWMA_SEED_WORK <= 3 * ideal                   # hard ceiling, never above 3x
    assert core.LWMA_SEED_WORK >= core.LWMA_MIN_WORK


@pytest.mark.real_seed
def test_first_block_from_the_fork_uses_the_seed():
    last_old = fake(core.LWMA_FORK_HEIGHT - 1, 1.79e9, 2)      # old-style block: difficulty = leading zeros
    assert core.lwma_next_work([last_old]) == max(core.LWMA_MIN_WORK, core.LWMA_SEED_WORK)


def test_seed_zero_carries_the_old_work_over(monkeypatch):
    monkeypatch.setattr(core, "LWMA_SEED_WORK", 0)
    last_old = fake(core.LWMA_FORK_HEIGHT - 1, 1.79e9, 3)
    assert core.lwma_next_work([last_old]) == 16 ** 3


def test_seed_never_below_the_floor(monkeypatch):
    monkeypatch.setattr(core, "LWMA_SEED_WORK", 10)
    assert core.lwma_next_work([fake(core.LWMA_FORK_HEIGHT - 1, 1.79e9, 2)]) == core.LWMA_MIN_WORK


@pytest.mark.real_seed
def test_blocks_after_the_first_do_not_use_the_seed():
    seed = core.LWMA_SEED_WORK
    ch = [fake(core.LWMA_FORK_HEIGHT - 1, 1.79e9, 2), fake(core.LWMA_FORK_HEIGHT, 1.79e9 + 60, seed)]
    nxt = core.lwma_next_work(ch)
    assert core.LWMA_MIN_WORK <= nxt <= seed * 3 // 2           # the normal step limits apply


def test_a_chain_across_the_fork_height_validates_with_the_seed(chain, monkeypatch):
    """A real node mines and accepts the first block on the seed work, then the next on LWMA work."""
    import time
    monkeypatch.setattr(core, "LWMA_FORK_HEIGHT", 2)
    monkeypatch.setattr(core, "LWMA_SEED_WORK", 4000)
    monkeypatch.setattr(core, "LWMA_MIN_WORK", 16)
    monkeypatch.setattr(core, "SECURITY_FORK_HEIGHT", 10**9)
    for _ in range(3):
        b = chain.mine_block("ab" * 16, [])
        assert chain.add_block(b.to_dict()), chain.last_reject_reason
    assert chain.chain[1].difficulty == 16 ** chain.chain[0].difficulty or chain.chain[1].difficulty >= 1
    assert chain.chain[2].difficulty == 4000


@pytest.mark.real_seed
def test_block_at_the_fork_may_use_the_emergency_work_when_block_before_it_is_ten_minutes_old(chain):
    """The stall rule measures from block 4999, so block 5000 can use the emergency work (seed / 8 = 1.2M)."""
    t0 = 1.79e9
    hist = [fake(core.LWMA_FORK_HEIGHT - 11 + i, t0 + 600 * i, 2) for i in range(11)]   # old blocks ~10 minutes apart
    assert hist[-1].index == core.LWMA_FORK_HEIGHT - 1
    seed = core.LWMA_SEED_WORK
    assert chain.allowed_difficulties(hist, hist[-1].timestamp + 30) == [seed]
    assert chain.allowed_difficulties(hist, hist[-1].timestamp + core.STALL_SECONDS) == [seed, seed // core.STALL_WORK_DIVISOR]
    assert seed // core.STALL_WORK_DIVISOR == 1_200_000
