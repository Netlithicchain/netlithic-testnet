"""ops/verify_drill_tx.py: OK for a drill-chain tx, FAIL for junk, DANGER (exit 2) for a tx valid on the REAL chain.
Only THROWAWAY keys made inside the test are used (patched in as PAUSE_PUBKEY)."""
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ops"))

import pause  # noqa: E402
import verify_drill_tx as V  # noqa: E402
from wallet import pubkey_from_private  # noqa: E402

SEED = "11" * 32
REAL, DRILL = "netlithic-dev-1", "netlithic-drill"


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setattr(pause, "PAUSE_PUBKEY", pubkey_from_private(SEED))
    return SEED


def run(tmp_path, capsys, tx, *extra):
    f = tmp_path / "tx.json"
    f.write_text(json.dumps(tx))
    code = _call(f, extra)
    return code, capsys.readouterr().out


def _call(f, extra):
    old = sys.argv
    sys.argv = ["verify_drill_tx.py", str(f), "--repo", ROOT, *extra]
    try:
        return V.main()
    finally:
        sys.argv = old


def test_drill_chain_tx_is_ok_and_says_not_valid_on_real_chain(tmp_path, capsys, key):
    tx = pause.make_pause_tx(key, True, "drill", 1, DRILL)
    code, out = run(tmp_path, capsys, tx)
    assert code == 0 and "OK:" in out and "DANGER" not in out and REAL in out


def test_real_chain_tx_is_danger_exit_2(tmp_path, capsys, key):
    tx = pause.make_pause_tx(key, True, "drill", 1, REAL)       # what a drill without --chain-id used to produce
    code, out = run(tmp_path, capsys, tx)
    assert code == 2 and "DANGER" in out and "OK:" not in out and "Do NOT photograph" in out
    code, out = run(tmp_path, capsys, tx, "--chain-id", REAL)   # even when asked about the real chain directly
    assert code == 2 and "DANGER" in out


def test_other_chain_or_tampered_or_other_key_is_fail_not_danger(tmp_path, capsys, key):
    tx = pause.make_pause_tx(key, True, "drill", 1, "netlithic-other")
    code, out = run(tmp_path, capsys, tx)
    assert code == 1 and "FAIL" in out and "DANGER" not in out
    good = pause.make_pause_tx(key, True, "drill", 1, DRILL)
    code, out = run(tmp_path, capsys, dict(good, reason="changed"))
    assert code == 1 and "FAIL" in out
    code, out = run(tmp_path, capsys, dict(good, sig=("0" if good["sig"][0] != "0" else "1") + good["sig"][1:]))
    assert code == 1 and "FAIL" in out
    other = pause.make_pause_tx("22" * 32, True, "drill", 1, REAL)
    code, out = run(tmp_path, capsys, other)
    assert code == 1 and "different key" in out and "DANGER" not in out   # not the founder's key: cannot pause anything


def test_shipped_default_repo_key_never_validates_a_throwaway_tx(tmp_path, capsys):
    tx = pause.make_pause_tx(SEED, True, "drill", 1, REAL)
    code, out = run(tmp_path, capsys, tx)
    assert code == 1 and "different key" in out


def test_cli_exit_codes_as_a_real_process(tmp_path):
    f = tmp_path / "tx.json"
    f.write_text("not json")
    p = subprocess.run([sys.executable, os.path.join(ROOT, "ops", "verify_drill_tx.py"), str(f)], capture_output=True, text=True)
    assert p.returncode == 1 and "FAIL" in p.stdout
