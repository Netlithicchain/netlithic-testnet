"""The difficulty rules are worked out once per block template, not once per nonce, and give the same answers as before."""
import random

import core
from core import Block

MINER = "ab" * 16


def fake(index, ts, work):
    return Block(index=index, timestamp=ts, transactions=[], previous_hash="", nonce=0, difficulty=work, hash="")


def reference_allowed(bc, previous_chain, timestamp):
    """The pre-0.5.4 allowed_difficulties, copied verbatim, as the oracle."""
    normal = bc.expected_block_difficulty(previous_chain)
    height = previous_chain[-1].index + 1
    if not core.is_post_fork(height):
        return [normal]
    if core.is_security_fork(height):
        newest = max(b.timestamp for b in previous_chain[-core.MTP_WINDOW:])
        stalled = timestamp - newest >= core.STALL_SECONDS
    else:
        stalled = timestamp - previous_chain[-1].timestamp >= core.STALL_SECONDS
    return [normal, core.stall_work(normal)] if stalled else [normal]


def random_chain(rng, n):
    t = 1.79e9 + rng.random()
    chain = [fake(4999, t, 2)]
    for i in range(n):
        t += rng.expovariate(1 / 60)
        chain.append(fake(5000 + i, t, rng.choice([256, 10**3, 10**6, 10**9])))
    return chain


def test_split_form_gives_the_same_answers_as_the_old_function(chain):
    rng = random.Random(11)
    for _ in range(400):
        ch = random_chain(rng, rng.choice([0, 1, 5, 20, 80]))
        for dt in (1, 59.9, 599.999, 600, 600.001, 5000):
            ts = max(b.timestamp for b in ch[-core.MTP_WINDOW:]) + dt
            assert chain.allowed_difficulties(ch, ts) == reference_allowed(chain, ch, ts)
            ctx = chain.difficulty_context(ch)
            assert chain.allowed_from_context(ctx, ts) == reference_allowed(chain, ch, ts)


def test_pre_fork_and_the_gap_before_the_security_fork_are_unchanged(chain, monkeypatch):
    pre = [fake(i, 1e9 + 600 * i, 2) for i in range(10)]
    assert chain.allowed_difficulties(pre, pre[-1].timestamp + 9999) == reference_allowed(chain, pre, pre[-1].timestamp + 9999)
    # LWMA on, security fork not yet: the gap is measured from the previous block only
    monkeypatch.setattr(core, "LWMA_FORK_HEIGHT", 100)
    ch = [fake(99, 1e9, 2), fake(100, 1e9 + 100, 300), fake(101, 1e9 + 200, 300)]
    for ts in (1e9 + 200 + 599, 1e9 + 200 + 600):
        assert chain.allowed_difficulties(ch, ts) == reference_allowed(chain, ch, ts)


def test_the_nonce_search_does_not_recompute_difficulty(chain, monkeypatch):
    """Post-fork mining: thousands of nonces must not call the LWMA again."""
    monkeypatch.setattr(core, "LWMA_FORK_HEIGHT", 1)       # block 1 is already on the new difficulty rule
    monkeypatch.setattr(core, "LWMA_MIN_WORK", 16)
    calls = {"lwma": 0, "expected": 0}
    real_lwma, real_exp = core.lwma_next_work, chain.expected_block_difficulty

    def count_lwma(ch):
        calls["lwma"] += 1
        return real_lwma(ch)

    def count_exp(ch):
        calls["expected"] += 1
        return real_exp(ch)

    monkeypatch.setattr(core, "lwma_next_work", count_lwma)
    monkeypatch.setattr(chain, "expected_block_difficulty", count_exp)
    template = chain.build_block_template(MINER, [])
    built = dict(calls)
    assert built["lwma"] >= 1                              # the rule was really in play

    real_meets = core.meets_pow
    tried = {"n": 0}

    def slow_meets(h, d, i):
        tried["n"] += 1
        return tried["n"] > 3000 and real_meets(h, d, i)

    monkeypatch.setattr(core, "meets_pow", slow_meets)
    block = chain.solve_block_template(template)
    assert tried["n"] > 3000
    assert calls == built, "difficulty was recomputed during the nonce search"
    assert chain.add_block(block.to_dict())
