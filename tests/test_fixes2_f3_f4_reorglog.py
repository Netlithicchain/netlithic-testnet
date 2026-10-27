"""Review round 2, F3 and F4: the reorg alert log (off-chain; consensus untouched).
F3: a chain is only counted as a 'refused deep reorg' (and only written to disk) once it has been fully validated,
    and repeats are de-duplicated and the writes throttled.
F4: the sidecar loader never crashes node start-up whatever is in the file, and the temp file is written safely."""
import json
import os
import stat
import sys
import time
from dataclasses import replace

import pytest

import core
from test_security_fork import grow, small_fork
from test_reorg_tripwire import _fork_of_depth

D = "d" * 32


def _sidecar():
    return core.Blockchain._reorg_log_path()


def _garbage_chain(chain, n=12):
    ours = grow(chain, chain.chain, n, step=1.0)
    chain.chain = ours
    junk = [b.to_dict() for b in ours]
    for b in junk[2:]:
        b["hash"] = "00" * 32            # not our blocks, not valid blocks
    return junk


# ---- F3 ----------------------------------------------------------------------------------------

def test_f3_garbage_hash_chains_are_not_recorded_and_write_nothing(chain, monkeypatch, tmp_path):
    small_fork(monkeypatch)
    monkeypatch.setattr(core, "MAX_REORG_DEPTH", 3)
    monkeypatch.chdir(tmp_path)
    junk = _garbage_chain(chain)
    writes = []
    monkeypatch.setattr(core.Blockchain, "_save_reorg_log", lambda self: writes.append(1))
    for _ in range(50):
        assert chain.replace_chain(junk) is False
    info = chain.reorg_info()
    assert info["rejected_deep_reorgs_24h"] == 0 and info["max_rejected_reorg_depth_24h"] == 0
    assert writes == []
    assert not os.path.exists(_sidecar())


def test_f3_a_really_valid_but_too_deep_reorg_is_still_recorded(chain, monkeypatch, tmp_path):
    small_fork(monkeypatch)
    monkeypatch.setattr(core, "MAX_REORG_DEPTH", 3)
    monkeypatch.chdir(tmp_path)
    theirs = _fork_of_depth(chain, 6, extra=3)
    assert chain.replace_chain([b.to_dict() for b in theirs]) is False
    info = chain.reorg_info()
    assert info["rejected_deep_reorgs_24h"] == 1 and info["max_rejected_reorg_depth_24h"] == 6
    assert os.path.exists(_sidecar())


def test_f3_the_same_refused_chain_offered_again_counts_once_and_writes_once(chain, monkeypatch, tmp_path):
    small_fork(monkeypatch)
    monkeypatch.setattr(core, "MAX_REORG_DEPTH", 3)
    monkeypatch.chdir(tmp_path)
    theirs = [b.to_dict() for b in _fork_of_depth(chain, 6, extra=3)]
    writes = []
    real = core.Blockchain._save_reorg_log
    monkeypatch.setattr(core.Blockchain, "_save_reorg_log", lambda self: (writes.append(1), real(self)))
    for _ in range(30):
        assert chain.replace_chain(theirs) is False
    assert chain.reorg_info()["rejected_deep_reorgs_24h"] == 1
    assert len(writes) == 1


def test_f3_the_disk_write_for_refused_reorgs_is_throttled(chain, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    writes = []
    monkeypatch.setattr(core.Blockchain, "_save_reorg_log", lambda self: writes.append(1))
    for i in range(40):
        chain.record_rejected_reorg(900 + i)          # distinct events, all within a few milliseconds
    assert len(chain.rejected_reorg_events) == 40      # the in-memory count (what /status shows) is complete
    assert len(writes) <= 2


def test_f3_validation_work_for_refused_chains_is_budgeted(chain, monkeypatch, tmp_path):
    """Many different junk chains must not make the node run a full validation for each one."""
    small_fork(monkeypatch)
    monkeypatch.setattr(core, "MAX_REORG_DEPTH", 3)
    monkeypatch.chdir(tmp_path)
    junk = _garbage_chain(chain)
    calls = []
    real = core.Blockchain.is_valid_chain
    monkeypatch.setattr(core.Blockchain, "is_valid_chain", lambda self, c: (calls.append(1), real(self, c))[1])
    for i in range(40):
        j = [dict(b) for b in junk]
        j[-1]["hash"] = f"{i:064x}"
        chain.replace_chain(j)
    assert len(calls) <= 3


# ---- F4 ----------------------------------------------------------------------------------------

def _write(tmp_path, text, mode="w"):
    with open(tmp_path / f"{core.CHAIN_FILE}.reorgs.json", mode) as f:
        f.write(text)


@pytest.mark.parametrize("payload", [
    "[" * 200000,                                          # deep nesting -> RecursionError
    '{"a":' * 100000,
    '{"accepted":[{"time":NaN,"height":1,"depth":2}],"rejected":[]}',
    '{"accepted":[{"time":Infinity,"height":1,"depth":2}],"rejected":[{"time":1,"depth":-Infinity}]}',
    '{"accepted":[{"time":1e999,"height":1,"depth":2}]}',
    '{"accepted":[{"time":' + "9" * 500 + ',"height":1,"depth":2}]}',
    '{"accepted":"not a list","rejected":7}',
    '[1,2,3]', 'null', '"str"', '{', '', '\x00\x00\x00', '{"accepted":[{"time":1}]}',
])
def test_f4_a_hostile_or_broken_sidecar_never_stops_the_node(chain, tmp_path, monkeypatch, payload):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, payload)
    b = core.Blockchain()                                  # must not raise
    info = b.reorg_info()                                  # and must be usable afterwards
    assert info["max_reorg_depth_24h"] == 0 and info["rejected_deep_reorgs_24h"] == 0
    json.dumps(info, allow_nan=False)                      # /status must stay valid JSON


def test_f4_binary_garbage_is_survived(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, os.urandom(4096), mode="wb") if False else open(tmp_path / f"{core.CHAIN_FILE}.reorgs.json", "wb").write(os.urandom(4096))
    core.Blockchain().reorg_info()


def test_f4_an_oversized_file_is_ignored_without_being_parsed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    good = {"accepted": [{"time": time.time(), "height": 5, "depth": 4}], "rejected": []}
    pad = " " * (core.Blockchain.REORG_LOG_MAX_BYTES + 10)
    _write(tmp_path, json.dumps(good) + pad)
    parsed = []
    import types
    real_json = core.json

    def spy(name):
        def f(x, *a, **k):
            if "accepted" in (x.decode() if isinstance(x, bytes) else str(x)):
                parsed.append(1)
            return getattr(real_json, name)(x, *a, **k)
        return f
    ns = {n: getattr(real_json, n) for n in dir(real_json) if not n.startswith("__")}
    ns.update(loads=spy("loads"), load=spy("load"))
    monkeypatch.setattr(core, "json", types.SimpleNamespace(**ns))
    assert core.Blockchain().reorg_info()["max_reorg_depth_24h"] == 0
    assert parsed == []


def test_f4_a_normal_file_still_loads(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    b = core.Blockchain()
    b.record_reorg(4, 12)
    b.record_rejected_reorg(800)
    again = core.Blockchain().reorg_info()
    assert again["max_reorg_depth_24h"] == 4 and again["max_rejected_reorg_depth_24h"] == 800


def test_f4_the_sidecar_may_not_be_a_symlink_or_a_fifo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps({"accepted": [{"time": time.time(), "height": 5, "depth": 9}]}))
    os.symlink(target, tmp_path / f"{core.CHAIN_FILE}.reorgs.json")
    assert core.Blockchain().reorg_info()["max_reorg_depth_24h"] == 0
    os.unlink(tmp_path / f"{core.CHAIN_FILE}.reorgs.json")
    os.mkfifo(tmp_path / f"{core.CHAIN_FILE}.reorgs.json")
    t0 = time.time()
    assert core.Blockchain().reorg_info()["max_reorg_depth_24h"] == 0     # must not hang on a pipe
    assert time.time() - t0 < 5


def test_f4_the_temp_file_is_created_exclusively_and_never_follows_a_symlink(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    tmp = tmp_path / f"{core.CHAIN_FILE}.reorgs.json.tmp"
    os.symlink(victim, tmp)                                # an attacker-planted temp name
    b = core.Blockchain()
    b.record_reorg(4, 12)
    assert victim.read_text() == "keep me"
    assert core.Blockchain().reorg_info()["max_reorg_depth_24h"] == 4   # the real file was still written
    mode = stat.S_IMODE(os.stat(_sidecar()).st_mode)
    assert mode & 0o077 == 0


def test_f4_the_source_uses_exclusive_nofollow_creation():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core.py")).read()
    assert "O_EXCL" in src and "O_NOFOLLOW" in src
