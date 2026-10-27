"""Fixes round 2 (branch points-v4r-fixes2): F2, the 0.5.3 peer gate is tightened at PEER level only (block validity is
untouched). Written to FAIL on points-v4r-fixes (eb4e3d5)."""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import core
import netlithic_version as nv
from conftest import post, read_private_doc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
pytestmark = pytest.mark.registry


class FakePeer:
    """A tiny node: /status (self-reported version and rules), /peers (a gossip list). Fields can be changed live."""

    def __init__(self, version="0.5.3", rules="same", gossip=()):
        self.version, self.rules, self.gossip = version, rules, list(gossip)
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                port = self.server.server_address[1]
                if self.path.startswith("/peers"):
                    body = {"peers": outer.gossip}
                else:
                    body = {"node": f"http://127.0.0.1:{port}", "chain_id": "netlithic-dev-1", "version": outer.version}
                    rules = core.points_v3_rules_hash() if outer.rules == "same" else outer.rules
                    if rules is not None:
                        body["points_v3_rules"] = rules
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
def fakes():
    made = []

    def make(**kw):
        p = FakePeer(**kw); made.append(p); return p
    yield make
    for p in made:
        p.stop()


@pytest.fixture
def gate_on(monkeypatch):
    """The gate is enforced (we are past block 4900)."""
    monkeypatch.setattr(nv, "UPGRADE_DEADLINE_HEIGHT", 0)
    monkeypatch.setattr(nv, "RULES_FIELD_REQUIRED_HEIGHT", 0)


@pytest.fixture
def gate_off(monkeypatch):
    monkeypatch.setattr(nv, "UPGRADE_DEADLINE_HEIGHT", 10 ** 9)
    monkeypatch.setattr(nv, "RULES_FIELD_REQUIRED_HEIGHT", 10 ** 9)


# ---- version strings -------------------------------------------------------------------------------
@pytest.mark.parametrize("v,outdated", [
    ("0.5.3", False), ("0.5.4", False), ("0.6.0", False), ("1.0.0", False), ("0.5.10", False),
    ("0.5.2", True), ("0.5.2-rc1", True), ("0.5.2-dev", True),                                # round 3, N3: 0.5.2 dev builds are unsupported
    ("0.5.3-rc1", True), ("0.5.3-rc.1", True), ("0.5.3-beta", True), ("0.5.3-0", True),     # a pre-release is older
    ("0.5.4-rc1", False),                                                                     # but older than 0.5.4 only
    ("0.5.1", True), ("0.5.1-rc1", True), ("0.5", True), ("v0.5.3", True), ("0.5.3+x", True), ("", True),
    (None, True), (5, True), ("0.5.3 ", False), ("0.5.3.1", True), ("garbage", True), ("9" * 40 + ".0.0", True),
])
def test_f2_prerelease_and_garbled_versions_do_not_satisfy_the_minimum(v, outdated):
    assert nv.is_outdated(v) is outdated


def test_f2_hello_refuses_a_release_candidate_after_the_deadline(node, fakes, gate_on):
    rc = fakes(version="0.5.3-rc1")
    status, body = post(node, "/hello", {"url": rc.url})
    assert status == 400 and body["upgrade_needed"] and rc.url not in node.peers


def test_f2_below_the_deadline_behaviour_is_unchanged_old_builds_are_let_in_and_flagged(node, fakes, gate_off):
    old = fakes(version="0.5.1", rules=None)
    status, body = post(node, "/hello", {"url": old.url})
    assert status == 200 and body["upgrade_needed"] is True and old.url in node.peers


# ---- peers we already know ----------------------------------------------------------------------------
def test_f2_a_known_peer_is_checked_again_on_hello_and_dropped_if_it_fails(node, fakes, monkeypatch):
    p = fakes()
    monkeypatch.setattr(nv, "UPGRADE_DEADLINE_HEIGHT", 10 ** 9)
    monkeypatch.setattr(nv, "RULES_FIELD_REQUIRED_HEIGHT", 10 ** 9)
    assert post(node, "/hello", {"url": p.url})[1]["message"] == "Peer added"
    assert post(node, "/hello", {"url": p.url})[1]["message"] == "Peer already known"
    p.version = "0.5.1"                                 # it downgraded, or was only ever let in below 4900
    status, body = post(node, "/hello", {"url": p.url})
    assert status == 200 and p.url in node.peers        # still below the deadline: tolerated, as documented
    monkeypatch.setattr(nv, "UPGRADE_DEADLINE_HEIGHT", 0)
    status, body = post(node, "/hello", {"url": p.url})
    assert status == 400 and body["upgrade_needed"] and p.url not in node.peers
    status, body = post(node, "/hello", {"url": p.url})   # and it cannot walk back in
    assert status == 400 and p.url not in node.peers


def test_f2_a_known_peer_with_a_different_rules_fingerprint_is_dropped(node, fakes, gate_on):
    p = fakes()
    node.peers.add(p.url)
    p.rules = "0" * 64
    status, body = post(node, "/hello", {"url": p.url})
    assert status == 400 and "rules" in body["error"].lower() and p.url not in node.peers


def test_f2_a_known_peer_that_is_briefly_unreachable_is_not_dropped(node, fakes, gate_on):
    p = fakes()
    node.peers.add(p.url)
    p.stop()
    status, body = post(node, "/hello", {"url": p.url})
    assert status == 200 and p.url in node.peers


def test_f2_periodic_recheck_drops_failing_peers_only_from_the_deadline(node, fakes, monkeypatch):
    bad, good, dead = fakes(version="0.5.1"), fakes(), fakes()
    node.peers.update({bad.url, good.url, dead.url})
    dead.stop()
    monkeypatch.setattr(nv, "UPGRADE_DEADLINE_HEIGHT", 10 ** 9)
    node._recheck_known_peers()
    assert node.peers == {bad.url, good.url, dead.url}          # below the deadline nothing is enforced
    monkeypatch.setattr(nv, "UPGRADE_DEADLINE_HEIGHT", 0)
    monkeypatch.setattr(nv, "RULES_FIELD_REQUIRED_HEIGHT", 0)
    node._gate_checked_at.clear()
    node._recheck_known_peers()
    assert node.peers == {good.url, dead.url}
    # throttled: an immediate second pass does not fetch again
    good.version = "0.5.1"
    node._recheck_known_peers()
    assert good.url in node.peers
    node._gate_checked_at.clear()
    node._recheck_known_peers()
    assert good.url not in node.peers


# ---- gossip and --peer ------------------------------------------------------------------------------
def test_f2_gossip_discovered_peers_go_through_the_gate(node, fakes, gate_on):
    good, old, rc, badrules, nofield, dead = (fakes(), fakes(version="0.5.1"), fakes(version="0.5.3-rc1"),
                                              fakes(rules="0" * 64), fakes(rules=None), fakes())
    dead_url = dead.url
    dead.stop()
    source = fakes(gossip=[good.url, old.url, rc.url, badrules.url, nofield.url, dead_url])
    node._discover_peers_from(source.url)
    assert node.peers == {good.url}
    # a failing candidate is remembered, not fetched again every round
    assert all(node._gate_recently_rejected(u) for u in (old.url, rc.url, badrules.url, nofield.url, dead_url))


def test_f2_gossip_below_the_deadline_still_adds_old_builds(node, fakes, gate_off):
    old = fakes(version="0.5.1", rules=None)
    source = fakes(gossip=[old.url])
    node._discover_peers_from(source.url)
    assert old.url in node.peers


def test_f2_gossip_verifies_a_bounded_number_of_candidates_per_round(node, fakes, gate_on, monkeypatch):
    monkeypatch.setattr(node, "GATE_MAX_VERIFY_PER_DISCOVERY", 3)
    cands = [fakes() for _ in range(6)]
    source = fakes(gossip=[c.url for c in cands])
    node._discover_peers_from(source.url)
    assert len(node.peers) == 3


def test_f2_peer_flag_peers_go_through_the_gate_but_an_unreachable_one_is_allowed(node, fakes, gate_on):
    good, old, dead = fakes(), fakes(version="0.5.1"), fakes()
    dead_url = dead.url
    dead.stop()
    node._add_extra_peers([good.url, old.url, dead_url])
    assert node.peers == {good.url, dead_url}


def test_f2_admin_add_peer_goes_through_the_gate(node, fakes, gate_on):
    old = fakes(version="0.5.1")
    status, body = post(node, "/add_peer", {"peer": old.url})
    assert status == 400 and "upgrade" in body["error"].lower() and old.url not in node.peers


# ---- peer level only -------------------------------------------------------------------------------------
def test_f2_block_validity_does_not_depend_on_the_version_gate():
    src = open(os.path.join(ROOT, "core.py")).read()
    assert "netlithic_version" not in src and "is_outdated" not in src


def test_f2_docs_say_what_the_gate_is_and_the_6440_resync_warning():
    rb = read_private_doc("docs", "UPGRADE_RUNBOOK_0.5.1.md")
    assert "before block 6440" in rb and "720" in rb and "wipe" in rb.lower()
    assert "below block 4900" in rb.lower() or "below 4900" in rb.lower()
