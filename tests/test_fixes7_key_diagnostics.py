"""0.5.3 reviewer extras: (a) startup warning + /status flag for an unusable pause key (information only; consensus
and the rules fingerprint are untouched); (b) healthcheck expected_pause_key_id (unset = silent, mismatch = alert)."""
import json
import os
import sys
import urllib.request

import pytest

import core
import pause

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ops"))
import healthcheck as H  # noqa: E402

GOOD = "054a1d9948c3325059835edb1d4fc3f74f40ce2f12bf2417b2ac180eb2737029"
GOOD_ID = "2a83f37a4ce7a269"
TYPO = "054a1d9948c3325059835edb1d4fc3f74f40c32f12bf2417b2ac180eb2737029"   # public; on the curve, fails subgroup check
A, B = "http://127.0.0.1:8000", "http://127.0.0.1:8001"


def _status(p2p):
    with urllib.request.urlopen(f"{p2p.self_url()}/status", timeout=10) as r:
        return json.loads(r.read())


@pytest.mark.real_valid_cap
@pytest.mark.registry     # the shipped build (registry slot empty = fail-safe) is what carries the 4f2922c0 fingerprint
def test_configured_key_is_the_right_one_and_good():
    assert pause.PAUSE_PUBKEY == GOOD and pause.key_id() == GOOD_ID
    assert pause.key_problem() == ""
    assert core.points_v3_rules_hash().startswith("4f2922c0")


@pytest.mark.parametrize("bad", [TYPO, "zz" * 32, "ab" * 31, GOOD.upper(), "00" * 32, " " + GOOD])
def test_key_problem_flags_unusable_keys_without_raising(bad):
    assert pause.key_problem(bad) not in ("", "empty")
    pause.key_id(bad)          # must not raise either


def test_empty_key_is_empty_not_invalid():
    assert pause.key_problem("") == "empty"


def test_typo_key_status_flag_and_fingerprint_unchanged(node, monkeypatch):
    before = core.points_v3_rules_hash()
    st = _status(node)
    assert st["pause_key_valid"] is True and st["pause_key_problem"] == "" and st["pause_key_id"] == GOOD_ID
    monkeypatch.setattr(pause, "PAUSE_PUBKEY", TYPO)
    st = _status(node)                          # does not crash
    assert st["pause_key_valid"] is False and "not a valid Ed25519" in st["pause_key_problem"]
    assert st["pause_enabled"] is True
    monkeypatch.setattr(pause, "PAUSE_PUBKEY", "not-hex-at-all")
    st = _status(node)                          # still does not crash
    assert st["pause_key_valid"] is False and st["pause_key_id"] == "invalid"
    monkeypatch.setattr(pause, "PAUSE_PUBKEY", GOOD)
    assert core.points_v3_rules_hash() == before


def test_startup_warning(monkeypatch, capsys, node):
    import p2p_http
    assert p2p_http._warn_pause_key() == "" and capsys.readouterr().out == ""
    monkeypatch.setattr(pause, "PAUSE_PUBKEY", TYPO)
    assert p2p_http._warn_pause_key() != ""     # returns, never raises or exits
    out = capsys.readouterr().out
    assert "WARNING" in out and "PAUSE_PUBKEY" in out and TYPO not in out
    monkeypatch.setattr(pause, "PAUSE_PUBKEY", "")
    p2p_http._warn_pause_key()
    out = capsys.readouterr().out
    assert "WARNING" not in out and "OFF" in out


_CP = {}


@pytest.fixture(autouse=True)
def _cp_file(tmp_path):
    f = tmp_path / "cp.json"
    f.write_text(json.dumps({"_readme": "x", "400": "x400"}))
    _CP["f"] = str(f)


def _fetch(url):
    if url.endswith("/chain"):
        return {"chain": [{"index": i, "hash": f"x{i}"} for i in range(500)]}
    if url.endswith("/pause_status"):
        return {"paused": False, "expired": False}
    raise OSError("x")


def _chk(cfg_over, statuses):
    c = H.load_config(None)
    c.update(cfg_over, checkpoints_file=_CP["f"])
    return H.v050_checks(c, {}, statuses, 1000.0, _fetch, journal_fn=lambda u, s: [])[0]


def _st(**kw):
    return dict({"height": 10, "version": "0.5.3", "fork_ready": True, "latest_hash": "x10", "peers": [],
                 "upgrade_deadline_height": 4900}, **kw)


def test_healthcheck_expected_pause_key_id_unset_warns_once():
    assert H.DEFAULTS["expected_pause_key_id"] == ""
    for node_st in (_st(pause_key_id=GOOD_ID), _st(pause_key_id=""), _st(pause_key_id="ffffffffffffffff"), _st()):
        for over in ({}, {"expected_pause_key_id": "   "}, {"expected_pause_key_id": None}):
            assert list(_chk(over, {A: node_st, B: node_st})) == ["pause_key_check_off"]   # warning only, never per node


def test_healthcheck_expected_pause_key_id_mismatch_alerts():
    cfg = {"expected_pause_key_id": GOOD_ID}
    assert _chk(cfg, {A: _st(pause_key_id=GOOD_ID), B: _st(pause_key_id=GOOD_ID)}) == {}
    assert _chk({"expected_pause_key_id": GOOD_ID.upper() + " "}, {A: _st(pause_key_id=GOOD_ID)}) == {}
    p = _chk(cfg, {A: _st(pause_key_id=GOOD_ID), B: _st(pause_key_id="ffffffffffffffff")})
    assert list(p) == ["pause_key:" + B]
    p = _chk(cfg, {A: _st(pause_key_id="")})
    assert "EMPTY" in p["pause_key:" + A]
    p = _chk(cfg, {A: _st()})                   # field missing entirely
    assert "pause_key:" + A in p


def test_healthcheck_flags_unusable_key_but_not_an_empty_one():
    cfg = {"expected_pause_key_id": GOOD_ID}
    p = _chk(cfg, {A: _st(pause_key_id="invalid", pause_key_valid=False, pause_key_problem="PAUSE_PUBKEY is not a valid x")})
    assert "pause_key_invalid:" + A in p
    p = _chk(cfg, {A: _st(pause_key_id=GOOD_ID, pause_key_valid=False, pause_key_problem="empty")})
    assert "pause_key_invalid:" + A not in p
    assert _chk(cfg, {A: _st(pause_key_id=GOOD_ID, pause_key_valid=True, pause_key_problem="")}) == {}


def test_example_config_documents_the_real_key_id():
    with open(os.path.join(ROOT, "ops", "healthcheck.example.json"), encoding="utf-8") as f:
        assert json.load(f)["expected_pause_key_id"] == GOOD_ID == "2a83f37a4ce7a269"


@pytest.mark.parametrize("slot", [123, 1.5, ["ab"], {"a": 1}, b"\x01\x02", True])
def test_key_id_never_raises_on_a_non_text_slot(slot, monkeypatch):
    assert pause.key_id(slot) == "invalid"
    monkeypatch.setattr(pause, "PAUSE_PUBKEY", slot)
    assert pause.key_id() == "invalid"
    assert pause.key_problem() != ""            # the /status diagnostics agree it is unusable


def test_key_id_empty_slots_and_good_key_unchanged(monkeypatch):
    assert pause.key_id("") == "" and pause.key_id(GOOD) == GOOD_ID
    monkeypatch.setattr(pause, "PAUSE_PUBKEY", None)
    assert pause.key_id() == ""


def test_status_survives_a_non_text_pause_slot(node, monkeypatch):
    monkeypatch.setattr(pause, "PAUSE_PUBKEY", 12345)
    st = _status(node)                          # used to raise
    assert st["pause_key_id"] == "invalid" and st["pause_key_valid"] is False
