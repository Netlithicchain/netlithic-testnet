"""PROPOSAL tests: time-based block counts, RandomX prototype, new alert helpers."""
import importlib
import pytest
import core, pause, contributions, economics, time_params, randomx_pow
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ops"))
import healthcheck  # noqa: E402


def test_target_matches_lwma():
    assert time_params.TARGET_BLOCK_SECONDS == core.LWMA_TARGET_BLOCK_TIME


def test_proposed_values():
    assert time_params.PROPOSED_MAX_REORG_DEPTH == 720
    assert time_params.PROPOSED_PAUSE_EXPIRY_BLOCKS == 10080
    assert time_params.PROPOSED_PAYOUT_EPOCH_BLOCKS == 1440
    assert time_params.PROPOSED_MIN_ADDRESS_AGE_BLOCKS == 1440
    assert time_params.blocks_for(1) == 1 and time_params.blocks_for(61) == 2


def test_code_constants_match_approved_times():
    assert time_params.PROPOSAL_ACTIVE is True
    assert core.MAX_REORG_DEPTH == time_params.PROPOSED_MAX_REORG_DEPTH == 720
    assert pause.PAUSE_EXPIRY_BLOCKS == time_params.PROPOSED_PAUSE_EXPIRY_BLOCKS == 10080
    assert economics.TESTNET_SHORT_EPOCHS["netlithic-dev-1"] == (5000, 1440)
    assert contributions.MIN_ADDRESS_AGE_BLOCKS == 1440


def test_apply_patches_and_restores(monkeypatch):
    for mod, name in [(core, "MAX_REORG_DEPTH"), (pause, "PAUSE_EXPIRY_BLOCKS"),
                      (contributions, "MIN_ADDRESS_AGE_BLOCKS"), (economics, "TESTNET_SHORT_EPOCHS")]:
        monkeypatch.setattr(mod, name, getattr(mod, name))
    time_params.apply()
    assert core.MAX_REORG_DEPTH == 720
    assert pause.PAUSE_EXPIRY_BLOCKS == 10080
    assert economics.TESTNET_SHORT_EPOCHS["netlithic-dev-1"] == (5000, 1440)


def test_randomx_off_and_no_python_fallback(monkeypatch):
    assert randomx_pow.RANDOMX_FORK_HEIGHT is None
    monkeypatch.setattr(randomx_pow, "_rx", None)
    with pytest.raises(RuntimeError):
        randomx_pow.pow_hash(b"x", b"seed")


def test_seed_height():
    assert randomx_pow.seed_height(10) == 0
    assert randomx_pow.seed_height(2048 + 64) == 2048
    assert randomx_pow.seed_height(2048 + 63) == 0


@pytest.mark.skipif(not randomx_pow.available(), reason="RandomX C binding not installed")
def test_randomx_deterministic_and_target():
    a = randomx_pow.pow_hash(b"header", b"seed")
    assert a == randomx_pow.pow_hash(b"header", b"seed") and len(a) == 64
    assert a != randomx_pow.pow_hash(b"header2", b"seed")
    assert randomx_pow.meets_work("0" * 64, 10**9)
    assert not randomx_pow.meets_work("f" * 64, 2)


def test_reorg_alert():
    prev = {10: "a", 11: "b", 12: "c"}
    assert healthcheck.reorg_problem(prev, {10: "a", 11: "b", 12: "c", 13: "d"}) is None
    assert healthcheck.reorg_problem(prev, {10: "x", 11: "y", 12: "z"}) is not None
    assert healthcheck.reorg_problem(prev, {10: "a", 11: "b", 12: "z"}) is None


def test_miner_share_alert():
    assert healthcheck.miner_share_problems(["a"] * 10) == {}
    out = healthcheck.miner_share_problems(["a"] * 60 + ["b"] * 40)
    assert list(out) == ["miner_share:a"]
