"""LWMA difficulty fork: old chain still valid, retargeting, timestamp rules, stalls."""
import json, os, time
import pytest

import core
from core import Block
from conftest import build_block

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE = os.path.join(ROOT, "tests", "data", "live_chain.json")
T = core.LWMA_TARGET_BLOCK_TIME


def fake(index, ts, work):
    return Block(index=index, timestamp=ts, transactions=[], previous_hash="", nonce=0,
                 difficulty=work, hash="")


def simulate(hashrate_fn, blocks=400, start_work=256, fork=100):
    """Deterministic miner: solve time = work / hashrate (expected value)."""
    chain = [fake(fork - 1, 1_000_000.0, 2)]  # last pre-fork block (difficulty 2 = 256 work)
    ts = chain[0].timestamp
    for i in range(blocks):
        idx = fork + i
        work = core.lwma_next_work(chain)
        ts += work / hashrate_fn(i)
        chain.append(fake(idx, ts, work))
    return chain


@pytest.fixture
def fork_at_100(monkeypatch):
    monkeypatch.setattr(core, "LWMA_FORK_HEIGHT", 100)


# ---- replaying the real chain ----

def test_live_chain_replays_under_old_rule(chain):
    data = json.load(open(LIVE))
    assert len(data) >= 40
    assert data[-1]["index"] < core.LWMA_FORK_HEIGHT
    assert chain.is_valid_chain(data)
    genesis = data[0]["transactions"][-1]
    assert genesis["type"] == "params" and genesis["economics_sha256"] == chain.econ.params_hash()


def test_live_chain_valid_with_fork_right_after_it_and_extends(chain, monkeypatch):
    data = json.load(open(LIVE))
    monkeypatch.setattr(core, "LWMA_FORK_HEIGHT", len(data))  # first new block is post-fork
    monkeypatch.setattr(core, "LWMA_MIN_WORK", 16)
    assert chain.is_valid_chain(data)
    blocks = [Block.from_dict(b) for b in data]
    nxt = core.lwma_next_work(blocks)
    assert nxt == 16 ** blocks[-1].difficulty  # work carries over at the fork
    b = build_block(chain, [core.make_reward_tx("a" * 32, chain.expected_reward(blocks))], chain=blocks)
    assert chain.block_reject_reason(b, blocks[-1], blocks[:-1]) is None


def test_genesis_hash_untouched(chain):
    # the fork lives in code, not genesis_config.json, so economics_sha256 is unchanged
    assert chain.econ.params_hash().startswith("de4df9b9")


# ---- retargeting ----

def test_steady_miner_converges_to_target(fork_at_100):
    rate = 50_000.0
    c = simulate(lambda i: rate)
    tail = c[-100:]
    avg = (tail[-1].timestamp - tail[0].timestamp) / (len(tail) - 1)
    assert abs(avg - T) / T < 0.1


def test_fast_miner_cannot_flood(fork_at_100):
    # one PC 1000x faster than the rest arrives: within ~60 blocks we're back near target
    c = simulate(lambda i: 1_000.0 if i < 100 else 1_000_000.0, blocks=300)
    tail = c[-60:]
    avg = (tail[-1].timestamp - tail[0].timestamp) / (len(tail) - 1)
    assert abs(avg - T) / T < 0.2
    # blocks during the flood never come faster than the 1.5x-per-block clamp allows
    assert all(c[i].difficulty <= c[i - 1].difficulty * 3 // 2 for i in range(2, len(c)))


def test_slow_miner_lowers_work_but_not_below_minimum(fork_at_100):
    c = simulate(lambda i: 1_000_000.0 if i < 100 else 1_000.0, blocks=300)
    assert c[-1].difficulty < c[100].difficulty
    tail = c[-60:]
    avg = (tail[-1].timestamp - tail[0].timestamp) / (len(tail) - 1)
    assert abs(avg - T) / T < 0.2
    c2 = simulate(lambda i: 0.5, blocks=50)
    assert all(b.difficulty >= core.LWMA_MIN_WORK for b in c2[1:])


def test_step_clamps(fork_at_100):
    base = [fake(99, 0.0, 2)] + [fake(100 + i, 60.0 * (i + 1), 10_000) for i in range(45)]
    instant = base + [fake(145, base[-1].timestamp + 0.001, 10_000)]
    assert core.lwma_next_work(instant) <= 15_000
    very_late = base + [fake(145, base[-1].timestamp + 10 ** 7, 10_000)]
    assert core.lwma_next_work(very_late) >= 6_666


# ---- timestamp attacks ----

def test_one_lying_timestamp_has_bounded_effect(fork_at_100):
    base = [fake(99, 0.0, 2)] + [fake(100 + i, 60.0 * (i + 1), 10_000) for i in range(45)]
    honest = core.lwma_next_work(base + [fake(145, base[-1].timestamp + 60, 10_000)])
    future = core.lwma_next_work(base + [fake(145, base[-1].timestamp + 60 + core.MAX_FUTURE_BLOCK_SECONDS, 10_000)])
    assert future >= honest * 0.85
    # and the next honest block (timestamp earlier than the liar's) undoes most of it
    lied = base + [fake(145, base[-1].timestamp + 180, 10_000)]
    after = core.lwma_next_work(lied + [fake(146, base[-1].timestamp + 120, 10_000)])
    assert after >= honest * 0.9


def _small_fork(monkeypatch, height=3, min_work=16):
    monkeypatch.setattr(core, "LWMA_FORK_HEIGHT", height)
    monkeypatch.setattr(core, "LWMA_MIN_WORK", min_work)


def _grow(bc, n, step=60.0):
    for _ in range(n):
        prev = bc.chain[-1]
        txs = [core.make_reward_tx("b" * 32, bc.expected_reward(bc.chain))]
        b = build_block(bc, txs, timestamp=prev.timestamp + step)
        assert bc.add_block(b.to_dict()), bc.last_reject_reason


def test_post_fork_timestamp_rules(chain, monkeypatch):
    _small_fork(monkeypatch)
    now = time.time()
    _grow(chain, 14, step=1.0)  # blocks in the past, crossing the fork
    assert core.is_post_fork(chain.chain[-1].index)
    hist, prev = chain.chain, chain.chain[-1]
    mtp = core.median_time_past(hist)
    txs = [core.make_reward_tx("b" * 32, chain.expected_reward(hist))]
    # at or before median-time-past: rejected
    b = build_block(chain, txs, timestamp=mtp)
    assert "median" in chain.block_reject_reason(b, prev, hist[:-1])
    # earlier than the previous block but after MTP: accepted (so a liar can't block honest miners)
    b = build_block(chain, txs, timestamp=prev.timestamp - 0.5)
    assert b.timestamp > mtp
    assert chain.block_reject_reason(b, prev, hist[:-1]) is None
    # too far in the future: rejected
    b = build_block(chain, txs, timestamp=now + core.MAX_FUTURE_BLOCK_SECONDS + 30)
    assert "future" in chain.block_reject_reason(b, prev, hist[:-1])


def test_pre_fork_still_needs_increasing_timestamps(chain, monkeypatch):
    _small_fork(monkeypatch, height=1000)
    _grow(chain, 2, step=1.0)
    prev = chain.chain[-1]
    b = build_block(chain, [core.make_reward_tx("b" * 32, chain.expected_reward(chain.chain))],
                    timestamp=prev.timestamp - 0.5)
    assert "not after previous" in chain.block_reject_reason(b, prev, chain.chain[:-1])


def test_wrong_work_and_weak_pow_rejected(chain, monkeypatch):
    _small_fork(monkeypatch)
    _grow(chain, 5, step=1.0)
    prev, hist = chain.chain[-1], chain.chain
    txs = [core.make_reward_tx("b" * 32, chain.expected_reward(hist))]
    want = chain.expected_block_difficulty(hist)
    b = build_block(chain, txs, difficulty=want + 1, timestamp=prev.timestamp + 1)
    assert "wrong difficulty" in chain.block_reject_reason(b, prev, hist[:-1])
    good = build_block(chain, txs, timestamp=prev.timestamp + 1)
    # find a nonce whose hash fails the work target
    n = good.nonce + 1
    while True:
        h = chain.calculate_hash(index=good.index, timestamp=good.timestamp, transactions=txs,
                                 previous_hash=good.previous_hash, nonce=n, difficulty=good.difficulty)
        if not core.meets_pow(h, good.difficulty, good.index):
            break
        n += 1
    bad = Block(good.index, good.timestamp, txs, good.previous_hash, n, good.difficulty, h)
    assert "proof of work" in chain.block_reject_reason(bad, prev, hist[:-1])


def test_mine_block_post_fork_and_chain_valid(chain, monkeypatch):
    _small_fork(monkeypatch)
    for _ in range(6):
        b = chain.mine_block("c" * 32, [])
        assert chain.add_block(b.to_dict()), chain.last_reject_reason
    assert chain.is_valid_chain(chain.to_list())
    assert chain.chain_work() == sum(core.block_work(b) for b in chain.chain)


# ---- low-hashrate stall ----

def test_stall_allows_emergency_work_only_after_gap(chain, monkeypatch):
    _small_fork(monkeypatch, min_work=16)
    monkeypatch.setattr(core, "STALL_SECONDS", 300)
    _grow(chain, 6, step=1.0)
    hist, prev = chain.chain, chain.chain[-1]
    normal = chain.expected_block_difficulty(hist)
    assert chain.allowed_difficulties(hist, prev.timestamp + 10) == [normal]
    assert chain.allowed_difficulties(hist, prev.timestamp + 300) == [normal, core.stall_work(normal)]
    txs = [core.make_reward_tx("b" * 32, chain.expected_reward(hist))]
    if core.stall_work(normal) != normal:
        early = build_block(chain, txs, difficulty=core.stall_work(normal), timestamp=prev.timestamp + 10)
        assert "wrong difficulty" in chain.block_reject_reason(early, prev, hist[:-1])
    late = build_block(chain, txs, difficulty=core.stall_work(normal), timestamp=prev.timestamp + 301)
    assert chain.block_reject_reason(late, prev, hist[:-1]) is None


def test_stall_work_is_reduced():
    assert core.stall_work(80_000) == 10_000
    assert core.stall_work(100) == core.LWMA_MIN_WORK
