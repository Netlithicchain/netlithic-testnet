"""Review round 2: the docs say what the code now does (F5, R1, R3) and no placeholder repo URL is left."""
import glob
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(name):
    return open(os.path.join(ROOT, "docs", name), encoding="utf-8").read()


def test_f5_registry_enabled_false_is_a_hard_fork_after_launch():
    d = _read("POINTS_V4_REGISTRY.md")
    assert "hard fork" in d and "REGISTRY_SUNSET_HEIGHT" in d
    assert "After launch it is not a safe switch" in d
    assert "After launch that is a\n  hard fork" in d
    assert "(or `REGISTRY_ENABLED = False` to open the pot" not in d      # the old, wrong advice


def test_r1_r3_documented():
    d = _read("POINTS_V4_REGISTRY.md")
    assert "co-sign" in d and "--as-new-key" in d and "--new-key-signature" in d
    assert "10,080" in d and "REGISTRY_MAX_VALID_BLOCKS" in d


def test_no_placeholder_repo_url_is_left_in_the_docs():
    for path in glob.glob(os.path.join(ROOT, "docs", "*.md")):
        assert "<netlithic repo url>" not in open(path, encoding="utf-8").read(), path
