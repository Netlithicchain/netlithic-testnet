"""The LWMA difficulty rule uses integers only, so it stays exact for very large work (above 2**53, about 9e15)."""
import ast
import inspect
import random
import textwrap
from fractions import Fraction

import core
from core import Block

T = core.LWMA_TARGET_BLOCK_TIME


def fake(index, ts, work):
    return Block(index=index, timestamp=ts, transactions=[], previous_hash="", nonce=0, difficulty=work, hash="")


def exact_reference(chain):
    """The same rule written with exact rational numbers (no rounding anywhere)."""
    last = chain[-1]
    post = [b for b in chain[-core.LWMA_WINDOW:] if core.is_post_fork(b.index)]
    start = chain[len(chain) - len(post) - 1] if len(chain) > len(post) else None
    blocks = ([start] if start is not None else []) + post
    weighted, sum_work, prev_t = Fraction(0), 0, Fraction(blocks[0].timestamp)
    for i, b in enumerate(blocks[1:], start=1):
        t = max(Fraction(b.timestamp), prev_t + 1)
        st = min(t - prev_t, core.LWMA_MAX_SOLVETIME_FACTOR * T)
        prev_t = t
        weighted += st * i
        sum_work += b.difficulty
    n = len(blocks) - 1
    nxt = int(Fraction(sum_work * T * (n + 1)) / (2 * weighted))
    prev = last.difficulty
    hi = prev * core.LWMA_MAX_STEP_UP[0] // core.LWMA_MAX_STEP_UP[1]
    lo = prev * core.LWMA_MAX_STEP_DOWN[0] // core.LWMA_MAX_STEP_DOWN[1]
    return max(core.LWMA_MIN_WORK, min(hi, max(lo, nxt)))


def old_float_rule(chain):
    """The 0.5.3 arithmetic, kept here only to show where it went wrong."""
    post = [b for b in chain[-core.LWMA_WINDOW:] if core.is_post_fork(b.index)]
    start = chain[len(chain) - len(post) - 1] if len(chain) > len(post) else None
    blocks = ([start] if start is not None else []) + post
    weighted, sum_work, prev_t = 0, 0, blocks[0].timestamp
    for i, b in enumerate(blocks[1:], start=1):
        t = max(b.timestamp, prev_t + 1)
        st = min(t - prev_t, core.LWMA_MAX_SOLVETIME_FACTOR * T)
        prev_t = t
        weighted += st * i
        sum_work += b.difficulty
    n = len(blocks) - 1
    nxt = int(sum_work * T * (n + 1) / (2 * weighted))
    prev = chain[-1].difficulty
    return max(core.LWMA_MIN_WORK, min(prev * 3 // 2, max(prev * 2 // 3, nxt)))


def make_chain(rng, base_work, n=50, spread=0.02, quarter=False):
    t = 1.79e9
    chain = [fake(4999, t, 2)]
    for i in range(n):
        step = T * (1 + rng.uniform(-spread, spread))
        t += round(step * 4) / 4 if quarter else step      # quarter seconds are exact in binary
        chain.append(fake(5000 + i, t, base_work + rng.randrange(0, 1000)))
    return chain


def test_lwma_next_work_has_no_floating_point_in_it():
    tree = ast.parse(textwrap.dedent(inspect.getsource(core.lwma_next_work)))
    for node in ast.walk(tree):
        assert not isinstance(node, ast.Div), "true division found"
        assert not (isinstance(node, ast.Constant) and isinstance(node.value, float)), "float literal found"
        assert not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "float"), "float() found"


def test_exact_above_nine_e15():
    rng = random.Random(3)
    for base in (10**15, 9 * 10**15, 2**53, 2**53 + 1, 10**18, 10**21, 7 * 10**30):
        for _ in range(40):
            ch = make_chain(rng, base, n=rng.choice([2, 10, 45, 60, 80]), spread=0.3, quarter=True)
            got = core.lwma_next_work(ch)
            assert isinstance(got, int)
            assert got == exact_reference(ch), base


def test_the_old_float_rule_did_lose_exactness_there():
    """Evidence the change matters: with huge work the 0.5.3 float arithmetic misses the exact integer answer."""
    rng = random.Random(5)
    misses = 0
    for _ in range(60):
        ch = make_chain(rng, 10**21 + 7)
        # whole-second spacing so the exact answer is unambiguous
        t = 1.79e9
        for i, b in enumerate(ch[1:], start=1):
            b.timestamp = t + 61 * i - (i % 7)
        if old_float_rule(ch) != core.lwma_next_work(ch):
            misses += 1
    assert misses > 0


def test_exactly_the_integer_answer_with_whole_second_timestamps():
    """Constant 60s blocks at work W give back W (up to the step limits), exactly, at any size."""
    for w in (10**6, 2**53 + 1, 10**21 + 12345):
        ch = [fake(4999, 1.79e9, 2)] + [fake(5000 + i, 1.79e9 + 60 * (i + 1), w) for i in range(50)]
        assert core.lwma_next_work(ch) == w


def test_matches_the_old_rule_at_normal_sizes():
    """Below 2**53 the new rule agrees with the 0.5.3 float rule to within one unit in a million."""
    rng = random.Random(9)
    for _ in range(300):
        ch = make_chain(rng, rng.choice([256, 5000, 10**6, 10**9]), n=rng.choice([2, 10, 45, 70]), spread=0.4)
        new, old = core.lwma_next_work(ch), old_float_rule(ch)
        assert abs(new - old) <= max(2, old // 10**5), (new, old)


def test_timestamp_ms_is_exact_floor():
    assert core.timestamp_ms(0.0) == 0
    assert core.timestamp_ms(1.5) == 1500
    assert core.timestamp_ms(1790000000.123) == 1790000000122 or core.timestamp_ms(1790000000.123) == 1790000000123
    assert core.timestamp_ms(1.0009999999) == 1000
