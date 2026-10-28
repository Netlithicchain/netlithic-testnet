"""CI coverage for the pause-key tools: ops/pause_keygen.py, ops/pause_tx.py, ops/set_pause_pubkey.py,
the pause drill, and the /status pause fields. Only THROWAWAY keys made inside the tests are used."""
import builtins
import getpass
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ops"))

import pause  # noqa: E402
import pause_keygen  # noqa: E402
import pause_tx  # noqa: E402
import set_pause_pubkey as spk  # noqa: E402
from wallet import pubkey_from_private  # noqa: E402

PY = sys.executable


def run_keygen(monkeypatch, capsys, answers=("YES", "")):
    it = iter(answers)
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(it))
    pause_keygen.main()
    return capsys.readouterr().out


def make_key(monkeypatch, capsys):
    out = run_keygen(monkeypatch, capsys)
    priv, pub = re.findall(r"\b[0-9a-f]{64}\b", out)
    return priv, pub


def run_tx(monkeypatch, capsys, key, *args):
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": key)
    monkeypatch.setattr(sys, "argv", ["pause_tx.py", *args])
    pause_tx.main()
    return capsys.readouterr()


# ---------------- ops/pause_keygen.py ----------------

def test_keygen_prints_matching_pair_and_writes_nothing(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    out = run_keygen(monkeypatch, capsys)
    keys = re.findall(r"\b[0-9a-f]{64}\b", out)
    assert len(keys) == 2 and keys[0] != keys[1]
    assert pubkey_from_private(keys[0]) == keys[1]           # printed public key belongs to the private key
    assert "PRIVATE KEY" in out and "PUBLIC KEY" in out
    assert list(tmp_path.iterdir()) == []                      # "writes nothing to disk"


def test_keygen_makes_a_new_key_each_time(monkeypatch, capsys):
    assert make_key(monkeypatch, capsys)[0] != make_key(monkeypatch, capsys)[0]


def test_keygen_refuses_unless_you_type_yes(monkeypatch, capsys):
    monkeypatch.setattr(builtins, "input", lambda prompt="": "yes please")
    with pytest.raises(SystemExit) as e:
        pause_keygen.main()
    assert "OFFLINE" in str(e.value) or "Stopped" in str(e.value)
    assert not re.search(r"\b[0-9a-f]{64}\b", capsys.readouterr().out)   # nothing printed


# ---------------- ops/pause_tx.py ----------------

def test_pause_tx_signs_pause_renew_resume(monkeypatch, capsys):
    priv, pub = make_key(monkeypatch, capsys)
    monkeypatch.setattr(pause, "PAUSE_PUBKEY", pub)
    cid = "netlithic-dev-pause-test"
    for action, paused, nonce in (("pause", True, 1), ("renew", True, 2), ("resume", False, 3)):
        cap = run_tx(monkeypatch, capsys, priv, action, f"reason {action}", "--nonce", str(nonce), "--chain-id", cid)
        tx = json.loads(cap.out)
        assert tx["type"] == "pause" and tx["paused"] is paused and tx["nonce"] == nonce
        assert tx["pubkey"] == pub
        assert pause.valid_pause_tx(tx, nonce, cid)              # a node that holds this key accepts it
        assert not pause.valid_pause_tx(tx, nonce, "netlithic-other")  # signed for one chain only
        assert priv not in cap.out and priv not in cap.err       # the private key is never echoed


def test_pause_tx_requires_chain_id_and_never_asks_for_the_key_without_it(monkeypatch, capsys):
    """F-1: no default to the real chain. Without --chain-id: a plain STOP message, no key prompt, no tx."""
    priv, pub = make_key(monkeypatch, capsys)
    monkeypatch.setattr(pause, "PAUSE_PUBKEY", pub)
    asked = []
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": asked.append(1) or priv)
    for extra in ([], ["--chain-id", ""], ["--chain-id", "   "]):
        monkeypatch.setattr(sys, "argv", ["pause_tx.py", "pause", "x", "--nonce", "1", *extra])
        with pytest.raises(SystemExit) as e:
            pause_tx.main()
        assert "--chain-id" in str(e.value) and "netlithic-dev-1" in str(e.value) and "netlithic-drill" in str(e.value)
    assert not asked and priv not in capsys.readouterr().out


def test_pause_tx_real_chain_id_given_explicitly_is_valid_for_dev(monkeypatch, capsys):
    priv, pub = make_key(monkeypatch, capsys)
    monkeypatch.setattr(pause, "PAUSE_PUBKEY", pub)
    tx = json.loads(run_tx(monkeypatch, capsys, priv, "pause", "x", "--nonce", "1", "--chain-id", "netlithic-dev-1").out)
    assert pause.valid_pause_tx(tx, 1, "netlithic-dev-1")
    tx = json.loads(run_tx(monkeypatch, capsys, priv, "pause", "x", "--nonce", "1", "--chain-id", "netlithic-drill").out)
    assert pause.valid_pause_tx(tx, 1, "netlithic-drill") and not pause.valid_pause_tx(tx, 1, "netlithic-dev-1")


def test_pause_tx_refuses_key_that_is_not_the_configured_one(monkeypatch, capsys):
    priv, _ = make_key(monkeypatch, capsys)
    _, other_pub = make_key(monkeypatch, capsys)
    monkeypatch.setattr(pause, "PAUSE_PUBKEY", other_pub)
    with pytest.raises(SystemExit) as e:
        run_tx(monkeypatch, capsys, priv, "pause", "x", "--nonce", "1", "--chain-id", "netlithic-drill")
    assert "not the configured PAUSE_PUBKEY" in str(e.value)


def test_pause_tx_works_with_empty_slot_for_ceremony_test(monkeypatch, capsys):
    """Ceremony step 6 runs before the key is in pause.py, so an empty slot must not block signing."""
    priv, pub = make_key(monkeypatch, capsys)
    monkeypatch.setattr(pause, "PAUSE_PUBKEY", "")
    tx = json.loads(run_tx(monkeypatch, capsys, priv, "pause", "test", "--nonce", "1", "--chain-id", "netlithic-drill").out)
    assert tx["pubkey"] == pub
    assert not pause.valid_pause_tx(tx, 1, "netlithic-dev-1")    # but a keyless node (0.5.0) rejects it


@pytest.mark.parametrize("reason", ["", "x" * 281])
def test_pause_tx_rejects_bad_reason(monkeypatch, capsys, reason):
    priv, _ = make_key(monkeypatch, capsys)
    with pytest.raises(SystemExit) as e:
        run_tx(monkeypatch, capsys, priv, "pause", reason, "--nonce", "1", "--chain-id", "netlithic-drill")
    assert "reason must be" in str(e.value)


def test_pause_tx_requires_nonce_and_known_action(monkeypatch, capsys):
    priv, _ = make_key(monkeypatch, capsys)
    for args in (("pause", "x"), ("explode", "x", "--nonce", "1")):
        with pytest.raises(SystemExit):
            run_tx(monkeypatch, capsys, priv, *args)


def test_pause_tx_bad_key_fails_without_leaking_it(monkeypatch, capsys):
    with pytest.raises(Exception) as e:
        run_tx(monkeypatch, capsys, "zz" * 32, "pause", "x", "--nonce", "1", "--chain-id", "netlithic-drill")
    assert "zz" * 32 not in str(e.value)


@pytest.mark.skipif(os.name != "posix", reason="needs a pseudo-terminal")
def test_pause_tx_reads_key_from_hidden_prompt_on_a_real_tty(monkeypatch, capsys):
    """Run the tool as a real process: the key is typed at the prompt, never passed in argv or env."""
    import pty
    import select
    priv, pub = make_key(monkeypatch, capsys)
    env = {k: v for k, v in os.environ.items() if not k.startswith("NETLITHIC_")}
    pid, fd = pty.fork()
    if pid == 0:  # child
        os.chdir(ROOT)
        # The shipped slot now holds the founder's real key, which this throwaway key is not. Run the real tool
        # on a real tty with the slot emptied in the child only (the ceremony-test situation).
        boot = ("import sys, runpy, pause; pause.PAUSE_PUBKEY = ''; sys.argv = "
                "['ops/pause_tx.py', 'pause', 'tty test', '--nonce', '1', '--chain-id', 'netlithic-drill']; "
                "runpy.run_path('ops/pause_tx.py', run_name='__main__')")
        os.execve(PY, [PY, "-c", boot], dict(env, PYTHONPATH=ROOT))
    buf = b""

    def read_until(marker, limit=20):
        nonlocal buf
        import time
        end = time.time() + limit
        while marker not in buf and time.time() < end:
            if select.select([fd], [], [], 0.2)[0]:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
    read_until(b"hidden): ")
    assert b"hidden): " in buf
    os.write(fd, priv.encode() + b"\n")
    read_until(b'"sig"')
    os.waitpid(pid, 0)
    os.close(fd)
    text = buf.decode(errors="replace")
    assert priv not in text.replace("\r", "")                  # the typed key was not echoed back
    tx = json.loads(re.search(r"\{.*\}", text.replace("\r", ""), re.S).group(0))
    assert tx["pubkey"] == pub and tx["paused"] is True


# ---------------- ops/set_pause_pubkey.py ----------------

@pytest.fixture
def slot_file(tmp_path):
    p = tmp_path / "pause.py"
    shutil.copy(os.path.join(ROOT, "pause.py"), p)
    text = p.read_text()
    # the copy under test always starts EMPTY, whatever the repo ships
    p.write_text(re.sub(r'^PAUSE_PUBKEY = "[0-9a-f]*"', 'PAUSE_PUBKEY = ""', text, flags=re.M))
    return str(p)


def test_repo_slot_is_well_formed_and_empty_or_valid():
    cur = spk.read_slot(os.path.join(ROOT, "pause.py"))
    assert cur == "" or spk.validate(cur) == cur
    assert pause.PAUSE_PUBKEY == cur


def test_empty_slot_fails_the_release_gate(slot_file, capsys):
    assert spk.main(["--check", "--file", slot_file]) == 1
    assert "EMPTY" in capsys.readouterr().out
    assert spk.main(["--status", "--file", slot_file]) == 0


def test_set_then_check_roundtrip(slot_file, monkeypatch, capsys):
    _, pub = make_key(monkeypatch, capsys)
    before = open(slot_file).read()
    assert spk.main([pub.upper(), "--file", slot_file]) == 0          # upper case is normalised
    out = capsys.readouterr().out
    assert " ".join(pub[i:i + 8] for i in range(0, 64, 8)) in out
    assert hashlib.sha256(bytes.fromhex(pub)).hexdigest()[:16] in out
    after = open(slot_file).read()
    changed = [(a, b) for a, b in zip(before.splitlines(), after.splitlines()) if a != b]
    assert len(changed) == 1 and changed[0][1].startswith(f'PAUSE_PUBKEY = "{pub}"')   # exactly one line
    assert len(before.splitlines()) == len(after.splitlines())
    assert spk.main(["--check", "--file", slot_file]) == 0
    assert spk.read_slot(slot_file) == pub
    ns = {}
    exec(compile(after, slot_file, "exec"), ns)                        # still valid Python, key is live
    assert ns["PAUSE_PUBKEY"] == pub and ns["key_id"]() == pause.key_id(pub)


@pytest.mark.parametrize("bad", ["abc", "g" * 64, "0" * 64, "a" * 63, "a" * 65, "not a key", "ff" * 32 + "00",
                                 "ff" * 32,                                    # y >= p: not a curve point
                                 "01" + "00" * 31,                             # the identity (low order)
                                 "ecff" + "ff" * 29 + "7f"])                   # y = p - 1 (order 2)
def test_set_refuses_malformed_or_weak_keys(slot_file, bad):
    before = open(slot_file).read()
    assert spk.main([bad, "--file", slot_file]) == 2
    assert open(slot_file).read() == before


def test_set_accepts_many_real_keys():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    for _ in range(50):
        pub = Ed25519PrivateKey.generate().public_key().public_bytes_raw().hex()
        assert spk.validate(pub) == pub


def test_empty_arg_is_a_usage_error(slot_file):
    with pytest.raises(SystemExit):
        spk.main(["", "--file", slot_file])


def test_set_does_not_overwrite_without_force(slot_file, monkeypatch, capsys):
    _, one = make_key(monkeypatch, capsys)
    _, two = make_key(monkeypatch, capsys)
    assert spk.main([one, "--file", slot_file]) == 0
    assert spk.main([two, "--file", slot_file]) == 2
    assert spk.read_slot(slot_file) == one
    assert spk.main([one, "--file", slot_file]) == 0                   # same key again is a harmless no-op
    assert spk.main([two, "--force", "--file", slot_file]) == 0
    assert spk.read_slot(slot_file) == two


def test_set_keeps_crlf_line_endings(slot_file, monkeypatch, capsys):
    text = open(slot_file, newline="").read().replace("\r\n", "\n").replace("\n", "\r\n")
    open(slot_file, "w", newline="").write(text)
    _, pub = make_key(monkeypatch, capsys)
    assert spk.main([pub, "--file", slot_file]) == 0
    after = open(slot_file, newline="").read()
    assert "\r\n" in after and "\n" not in after.replace("\r\n", "")


def test_missing_or_duplicate_slot_line_is_an_error(tmp_path):
    p = tmp_path / "pause.py"
    p.write_text("x = 1\n")
    with pytest.raises(SystemExit):
        spk.read_slot(str(p))
    p.write_text('PAUSE_PUBKEY = ""  # PAUSE-PUBKEY-SLOT a\nPAUSE_PUBKEY = ""  # PAUSE-PUBKEY-SLOT b\n')
    with pytest.raises(SystemExit):
        spk.read_slot(str(p))


def test_set_tool_cli_exit_codes(slot_file):
    r = subprocess.run([PY, os.path.join(ROOT, "ops", "set_pause_pubkey.py"), "--check", "--file", slot_file],
                       capture_output=True, text=True)
    assert r.returncode == 1 and "EMPTY" in r.stdout


# ---------------- key id, /status, drill ----------------

def test_key_id(monkeypatch, capsys):
    _, pub = make_key(monkeypatch, capsys)
    assert pause.key_id("") == ""
    assert pause.key_id(pub) == hashlib.sha256(bytes.fromhex(pub)).hexdigest()[:16] == pause.key_id(pub.upper())
    monkeypatch.setattr(pause, "PAUSE_PUBKEY", "")
    assert pause.key_id() == ""
    monkeypatch.setattr(pause, "PAUSE_PUBKEY", pub)
    assert pause.key_id() == pause.key_id(pub)


def test_status_reports_pause_fields(node, monkeypatch, capsys):
    import urllib.request

    def status():
        with urllib.request.urlopen(f"{node.self_url()}/status", timeout=5) as r:
            return json.loads(r.read())
    monkeypatch.setattr(pause, "PAUSE_PUBKEY", "")
    st = status()
    assert st["pause_enabled"] is False and st["pause_key_id"] == ""
    _, pub = make_key(monkeypatch, capsys)
    monkeypatch.setattr(pause, "PAUSE_PUBKEY", pub)
    st = status()
    assert st["pause_enabled"] is True and st["pause_key_id"] == pause.key_id(pub)


def test_pause_drill_passes_and_changes_nothing(tmp_path):
    def digest(name):
        return hashlib.sha256(open(os.path.join(ROOT, name), "rb").read()).hexdigest()
    before = {n: digest(n) for n in ("pause.py", "chain.json", "mempool.json", "peers.json", "state_contributions.json")}
    r = subprocess.run([PY, os.path.join(ROOT, "ops", "drill", "pause_drill.py")], cwd=tmp_path,
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PAUSE DRILL PASSED" in r.stdout and "FAIL" not in r.stdout
    assert "keyless node (0.5.0) rejects the BLOCK" in r.stdout
    assert not re.search(r"\b[0-9a-f]{64}\b", r.stdout + r.stderr)    # no key (private or public) is printed
    assert {n: digest(n) for n in before} == before
