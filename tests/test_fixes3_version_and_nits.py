"""Review round 3: N3 (the version is 0.5.3 everywhere and builds before it are declared unsupported), N4 (the key slots
stay empty and the release checks keep refusing) and the N5 nits."""
import os
import re
import subprocess
import sys

import pytest

import netlithic_version as nv
import registry
from registry import normalize_registry_tx, sign_registry_tx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


# ---- N3 ----------------------------------------------------------------------------------------------------------

def test_n3_version_and_minimum_are_0_5_3():
    assert nv.VERSION == nv.MIN_PEER_VERSION == "0.5.3"


@pytest.mark.parametrize("v", ["0.5.0", "0.5.1", "0.5.2", "0.5.2-rc1", "0.5.3-rc1"])
def test_n3_builds_before_0_5_3_are_outdated(v):
    assert nv.is_outdated(v)


def test_n3_installer_version_matches():
    iss = _read("desktop", "netlithic.iss")
    assert re.search(r"^AppVersion=0\.5\.3\s*$", iss, re.M)


def test_n3_healthcheck_expects_0_5_3():
    import ops.healthcheck as H
    assert H.DEFAULTS["expected_version"] == "0.5.3"
    assert H._outdated("0.5.2", "0.5.3") and not H._outdated("0.5.3", "0.5.3")


def test_n3_no_stale_current_version_left_in_code_or_runbooks():
    """0.5.2 may only appear as history (what changed, what is unsupported), never as a version a node must report."""
    for rel in ("netlithic_version.py", "ops/healthcheck.py", "desktop/netlithic.iss"):
        text = _read(*rel.split("/"))
        for ln in text.splitlines():
            if "0.5.2" in ln:
                assert re.search(r"older|before|unsupported|0\.5\.1 =|0\.5\.2 =|0\.5\.3 =|dev build|plus the|raised to", ln), (rel, ln)


@pytest.mark.parametrize("doc", ["docs/UPGRADE_RUNBOOK_0.5.1.md", "docs/RELEASE_PLAN.md", "docs/POINTS_V4.md",
                                 "docs/DESKTOP_UPGRADE_PLAN.md"])
def test_n3_docs_say_builds_before_0_5_3_are_unsupported(doc):
    text = _read(*doc.split("/"))
    assert "0.5.3" in text
    low = " ".join(text.split())
    assert "before 0.5.3" in low and "unsupported" in low
    assert "before block 4900" in low


def test_n3_runbook_names_the_dev_builds_and_says_upgrade_together():
    low = " ".join(_read("docs", "UPGRADE_RUNBOOK_0.5.1.md").split())
    assert "eb4e3d5" in low and "4080424" in low
    assert "All nodes must upgrade together" in low


# ---- N4 ----------------------------------------------------------------------------------------------------------

def test_n4_pause_key_is_set_and_registry_slot_is_still_empty():
    """0.5.3 candidate: the pause slot holds the founder's offline-made PUBLIC key; the registry slot stays empty."""
    assert registry.REGISTRY_PUBKEY == ""
    import pause
    assert pause.PAUSE_PUBKEY == "054a1d9948c3325059835edb1d4fc3f74f40ce2f12bf2417b2ac180eb2737029"
    r = subprocess.run([sys.executable, os.path.join(ROOT, "ops", "set_pause_pubkey.py"), "--check"],
                       capture_output=True, text=True, cwd=ROOT, timeout=60)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "Do NOT release" not in r.stdout
    r = subprocess.run([sys.executable, os.path.join(ROOT, "ops", "registry_tool.py"), "--check"],
                       capture_output=True, text=True, cwd=ROOT, timeout=60)
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "Do NOT release" in r.stdout


# ---- N5 ----------------------------------------------------------------------------------------------------------

@pytest.mark.real_valid_cap
@pytest.mark.registry
def test_n5_none_valid_until_is_the_placeholder_and_consensus_always_refuses_it(monkeypatch):
    key = "11" * 32
    monkeypatch.setattr(registry, "REGISTRY_PUBKEY", registry.pubkey_from_private(key))
    tx = sign_registry_tx(key, ["a" * 32], [], "test", 1, "chain-x", valid_until=None)
    assert tx["valid_until"] == registry.NO_EXPIRY
    doc = sign_registry_tx.__doc__
    assert "ALWAYS refuses" in doc and "any real height" in doc and "no practical limit" not in doc
    for height in (0, 1, 10 ** 6, 10 ** 9):
        st = registry.RegistryState(lambda h: h // 100, initial_key=tx["pubkey"])
        assert st.check_and_apply(normalize_registry_tx(tx), height, "chain-x") is False
    # control: the same update with a real expiry is accepted at the same call
    ok = sign_registry_tx(key, ["a" * 32], [], "test", 1, "chain-x", valid_until=500)
    st = registry.RegistryState(lambda h: h // 100, initial_key=ok["pubkey"])
    assert st.check_and_apply(normalize_registry_tx(ok), 100, "chain-x") is True


def test_n5_negative_valid_until_gets_its_own_error_message():
    base = {"type": "registry", "adds": ["a" * 32], "removes": [], "reason": "r", "nonce": 1, "valid_until": -5,
            "pubkey": "00" * 32, "sig": "00" * 64}
    with pytest.raises(ValueError) as e:
        normalize_registry_tx(base)
    assert "valid_until" in str(e.value) and "Nonce" not in str(e.value)
    with pytest.raises(ValueError) as e:
        normalize_registry_tx({**base, "valid_until": 5, "nonce": 0})
    assert "Nonce must be > 0" in str(e.value)
