import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ops"))
from founder_miner import decide, FOUNDER_ADDRESS

NOW = 1_000_000.0


def test_mines_normally_before_5000():
    assert decide(4998, NOW, NOW, 0)[0] is True


def test_stops_at_5000_when_others_are_mining():
    assert decide(4999, NOW - 60, NOW, 0)[0] is False
    assert decide(6000, NOW - 29 * 60, NOW, 0)[0] is False


def test_backstop_after_30_minutes_idle():
    assert decide(6000, NOW - 31 * 60, NOW, 0)[0] is True


def test_backstop_capped_one_per_30_minutes():
    assert decide(6000, NOW - 40 * 60, NOW, NOW - 10 * 60)[0] is False
    assert decide(6000, NOW - 40 * 60, NOW, NOW - 31 * 60)[0] is True


def test_backstop_can_be_off():
    assert decide(6000, NOW - 999 * 60, NOW, 0, backstop=False)[0] is False


def test_public_address():
    assert FOUNDER_ADDRESS == "e924523b2614a39b9bb4ff8bfc05e747"
