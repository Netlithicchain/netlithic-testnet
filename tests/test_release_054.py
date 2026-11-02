"""0.5.4 release: version gate and the separate mining-difficulty fingerprint."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import core
import netlithic_version as nv
from conftest import post

pytestmark = pytest.mark.registry


class Peer:
    def __init__(self, version="0.5.4", difficulty="same"):
        self.version, self.difficulty = version, difficulty
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/peers"):
                    body = {"peers": []}
                else:
                    body = {"node": outer.url, "chain_id": "netlithic-dev-1", "version": outer.version,
                            "points_v3_rules": core.points_v3_rules_hash()}
                    d = core.difficulty_rules_hash() if outer.difficulty == "same" else outer.difficulty
                    if d is not None:
                        body["difficulty_rules"] = d
                b = json.dumps(body).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(b)))
                self.end_headers(); self.wfile.write(b)

            def log_message(self, *a):
                pass
        self.srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def stop(self):
        self.srv.shutdown(); self.srv.server_close()


@pytest.fixture
def peer():
    made = []

    def make(**kw):
        p = Peer(**kw); made.append(p); return p
    yield make
    for p in made:
        p.stop()


@pytest.fixture
def gate_on(monkeypatch):
    monkeypatch.setattr(nv, "UPGRADE_DEADLINE_HEIGHT", 0)
    monkeypatch.setattr(nv, "RULES_FIELD_REQUIRED_HEIGHT", 0)


def test_release_version_is_0_5_4_and_the_gate_height_is_unchanged():
    assert nv.VERSION == nv.MIN_PEER_VERSION == "0.5.4"
    assert nv.UPGRADE_DEADLINE_HEIGHT == 4900 and nv.RULES_FIELD_REQUIRED_HEIGHT == 4900
    assert nv.is_outdated("0.5.3") and not nv.is_outdated("0.5.4")


def test_release_difficulty_fingerprint_is_stable_and_covers_the_new_constants(monkeypatch):
    h = core.difficulty_rules_hash()
    assert len(h) == 64 and h == core.difficulty_rules_hash()
    for name, value in (("LWMA_WINDOW", 45), ("LWMA_TARGET_BLOCK_TIME", 30), ("LWMA_SEED_WORK", 7),
                        ("LWMA_MIN_WORK", 512), ("LWMA_MAX_SOLVETIME_FACTOR", 5), ("MTP_WINDOW", 9),
                        ("LWMA_MAX_STEP_UP", (2, 1)), ("LWMA_MAX_STEP_DOWN", (1, 2)),
                        # the fork height, the timestamp rule and the constants used below block 5000
                        ("SECURITY_FORK_HEIGHT", 5001), ("TIMESTAMP_RULE_TAG", "something-else"),
                        ("TARGET_BLOCK_TIME", 16), ("DIFFICULTY_ADJUSTMENT_INTERVAL", 11),
                        ("DEFAULT_DIFFICULTY", 4), ("MIN_DIFFICULTY", 3), ("MAX_DIFFICULTY_STEP", 2)):
        with monkeypatch.context() as m:
            m.setattr(core, name, value)
            assert core.difficulty_rules_hash() != h, name


def test_release_the_existing_points_fingerprint_is_not_changed_by_the_difficulty_rules(monkeypatch):
    """Mixed-version peering below block 4900 depends on this: the shared fingerprint ignores the new rules."""
    before = core.points_v3_rules_hash()
    monkeypatch.setattr(core, "LWMA_WINDOW", 45)
    monkeypatch.setattr(core, "LWMA_SEED_WORK", 7)
    assert core.points_v3_rules_hash() == before
    import subprocess, sys
    from conftest import ROOT
    out = subprocess.run([sys.executable, "-c", "import core; print(core.points_v3_rules_hash())"], cwd=ROOT,
                         capture_output=True, text=True, timeout=60).stdout.strip()
    assert out.startswith("4f2922c0")           # same production fingerprint as 0.5.3


def test_release_node_status_reports_the_difficulty_fingerprint(node):
    import urllib.request
    with urllib.request.urlopen(f"{node.self_url()}/status", timeout=5) as r:
        st = json.loads(r.read())
    assert st["difficulty_rules"] == core.difficulty_rules_hash()
    assert st["points_rules"] == core.points_v3_rules_hash()


def test_release_a_peer_with_matching_rules_is_accepted(node, peer, gate_on):
    p = peer()
    status, body = post(node, "/hello", {"url": p.url})
    assert status == 200 and p.url in node.peers


def test_release_a_peer_with_different_difficulty_rules_is_refused_at_any_height(node, peer):
    p = peer(difficulty="0" * 64)               # gate not forced on: this check is not height-dependent
    status, body = post(node, "/hello", {"url": p.url})
    assert status == 400 and "difficulty rules" in body["error"].lower() and p.url not in node.peers


def test_release_a_peer_that_does_not_report_the_field_is_left_to_the_version_gate(node, peer, gate_on):
    old = peer(version="0.5.3", difficulty=None)
    status, body = post(node, "/hello", {"url": old.url})
    assert status == 400 and body["upgrade_needed"] and old.url not in node.peers
    new = peer(version="0.5.4", difficulty=None)
    assert post(node, "/hello", {"url": new.url})[0] == 200


def test_release_a_known_peer_whose_difficulty_rules_change_is_dropped(node, peer):
    p = peer()
    node.peers.add(p.url)
    p.difficulty = "f" * 64
    status, body = post(node, "/hello", {"url": p.url})
    assert status == 400 and p.url not in node.peers


def test_release_difficulty_fingerprint_names_the_fork_height_timestamp_rule_and_pre_fork_constants():
    assert core.TIMESTAMP_RULE_TAG == "finite-all-heights"
    import hashlib
    h = core.difficulty_rules_hash()
    # The input is a '|' joined list; rebuild it from the public constants to prove each field is really in it.
    wanted = [f"security_fork={core.SECURITY_FORK_HEIGHT}", f"timestamp_rule={core.TIMESTAMP_RULE_TAG}",
              f"pre_fork_target={core.TARGET_BLOCK_TIME}", f"pre_fork_interval={core.DIFFICULTY_ADJUSTMENT_INTERVAL}",
              f"pre_fork_default={core.DEFAULT_DIFFICULTY}", f"pre_fork_min={core.MIN_DIFFICULTY}",
              f"pre_fork_max_step={core.MAX_DIFFICULTY_STEP}"]
    captured = []
    real = hashlib.sha256
    import unittest.mock as um
    with um.patch("hashlib.sha256", side_effect=lambda data=b"": (captured.append(data), real(data))[1]):
        core.difficulty_rules_hash()
    text = captured[0].decode()
    for w in wanted:
        assert w in text.split("|"), w
    assert len(h) == 64
