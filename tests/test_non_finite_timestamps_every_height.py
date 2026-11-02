"""Timestamp validation hardening, part 2: a block must carry a real (finite) time at EVERY height, on every path
that validates a block (add_block, is_valid_chain, replace_chain), whatever way the bad value is written."""
import math

import pytest

import core
from conftest import build_block

ADDR = "ab" * 16
REASON = "timestamp is not a valid time"
# how a non-finite time can arrive in a block: as a float, or as text that float() turns into one
WIRE_VALUES = [math.inf - math.inf, math.inf, -math.inf, "nan", "-nan", "NaN", "inf", "-inf", "Infinity", "1e999", "-1e999"]
WIRE_IDS = [f"wire{i}" for i in range(len(WIRE_VALUES))]
REPLACE_VALUES = ["nan", math.inf, -math.inf, "1e999"]
REPLACE_IDS = [f"wire{i}" for i in range(len(REPLACE_VALUES))]


def _mined(bc, ts=None):
    reward = core.make_reward_tx(ADDR, bc.expected_reward(bc.chain))
    return build_block(bc, [reward], timestamp=ts)


def _bad_dict(bc, wire):
    d = _mined(bc).to_dict()
    d["timestamp"] = wire
    return d


def _spy_reasons(bc, monkeypatch):
    seen = []
    real = bc.block_reject_reason

    def spy(*a, **k):
        r = real(*a, **k)
        seen.append(r)
        return r

    monkeypatch.setattr(bc, "block_reject_reason", spy)
    return seen


def _spy_parse_errors(monkeypatch):
    """Records why Block.from_dict refused a block (is_valid_chain turns that into a plain False)."""
    errors = []
    real = core.Block.from_dict

    def spy(data):
        try:
            return real(data)
        except Exception as e:
            errors.append(str(e))
            raise

    monkeypatch.setattr(core.Block, "from_dict", staticmethod(spy))
    return errors


def _rehashed(bc, d):
    """Give the edited block a hash that matches its own header, so only the time itself can make it fail."""
    try:
        h = bc.calculate_hash(d["index"], d["timestamp"], d["transactions"], d["previous_hash"], d["nonce"],
                              d["difficulty"])
    except ValueError:
        return d  # the header cannot even be hashed; the time is still the only edited field
    return dict(d, hash=h)


@pytest.fixture(params=["pre_fork", "post_fork"])
def at_height(request, chain, monkeypatch):
    """The first new block sits below the fork height (pre_fork) or at it (post_fork)."""
    if request.param == "post_fork":
        monkeypatch.setattr(core, "LWMA_FORK_HEIGHT", 1)
        monkeypatch.setattr(core, "LWMA_MIN_WORK", 16)
    else:
        assert not core.is_post_fork(1)
    assert core.is_post_fork(1) == (request.param == "post_fork")
    return chain


@pytest.mark.parametrize("wire", WIRE_VALUES, ids=WIRE_IDS)
def test_non_finite_timestamp_is_rejected_by_add_block_at_every_height(at_height, wire):
    bc = at_height
    assert bc.add_block(_bad_dict(bc, wire)) is False
    assert bc.last_reject_reason.endswith(REASON)  # now refused while reading the block
    assert len(bc.chain) == 1


@pytest.mark.parametrize("wire", WIRE_VALUES, ids=WIRE_IDS)
def test_non_finite_timestamp_is_rejected_by_is_valid_chain_at_every_height(at_height, monkeypatch, wire):
    bc = at_height
    seen = _spy_reasons(bc, monkeypatch)
    errors = _spy_parse_errors(monkeypatch)
    data = [b.to_dict() for b in bc.chain] + [_rehashed(bc, _bad_dict(bc, wire))]
    assert bc.is_valid_chain(data) is False
    assert seen == []  # refused while reading the block, before the per-block checks
    assert any(REASON in e for e in errors)


@pytest.mark.parametrize("wire", REPLACE_VALUES, ids=REPLACE_IDS)
def test_non_finite_timestamp_is_rejected_by_replace_chain_at_every_height(at_height, monkeypatch, wire):
    bc = at_height
    good = _mined(bc)
    seen = _spy_reasons(bc, monkeypatch)
    errors = _spy_parse_errors(monkeypatch)
    longer = [b.to_dict() for b in bc.chain] + [good.to_dict()]
    # a chain with one extra block would win; change only its time (and rehash) and it must not
    longer[-1] = _rehashed(bc, dict(longer[-1], timestamp=wire))
    assert bc.replace_chain(longer) is False
    assert len(bc.chain) == 1
    assert REASON not in seen  # refused while reading the block, before the per-block checks
    assert any(REASON in e for e in errors)


def test_non_finite_timestamp_two_in_a_row_cannot_lower_the_work_before_the_fork(chain):
    """Two such blocks in a row used to be able to pull the pre-fork work down; both are now refused."""
    bc = chain
    assert not core.is_post_fork(1)
    before = bc.expected_block_difficulty(bc.chain)
    for wire in ("nan", "-inf"):
        assert bc.add_block(_bad_dict(bc, wire)) is False
    assert bc.expected_block_difficulty(bc.chain) == before
    assert len(bc.chain) == 1


def test_non_finite_timestamp_check_does_not_touch_a_normal_block_at_any_height(at_height):
    bc = at_height
    blk = _mined(bc)
    assert bc.block_reject_reason(blk, bc.chain[-1], bc.chain[:-1]) is None
    assert bc.add_block(blk.to_dict()) is True


def test_non_finite_timestamp_refused_chain_precheck_agrees(at_height, monkeypatch):
    """The cheap precheck for deep-reorg offers also turns such a block away."""
    bc = at_height
    blk = bc.chain[-1]
    good = _mined(bc)
    bad = good.to_dict()
    bad["timestamp"] = "inf"
    # let the hash and work checks pass, so only the time itself can make the precheck say no
    monkeypatch.setattr(bc, "calculate_hash", lambda *a, **k: bad["hash"])
    assert bc._refused_chain_precheck([blk.to_dict(), bad], 0) is False


@pytest.mark.parametrize("wire", WIRE_VALUES, ids=WIRE_IDS)
def test_block_from_dict_header_validation(chain, wire):
    with pytest.raises(ValueError, match=REASON):
        core.Block.from_dict(_bad_dict(chain, wire))
