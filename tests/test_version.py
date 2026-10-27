"""Single version constant, exposed everywhere; explorer counts fork-ready nodes."""
import json
import subprocess
import sys
import urllib.request

import pytest

import core
import explorer as ex_mod
import netlithic_version as nv
from conftest import ROOT


def test_version_and_fork_height_agree():
    assert nv.VERSION == "0.5.3"
    assert nv.FORK_READY_HEIGHT == core.SECURITY_FORK_HEIGHT


@pytest.mark.parametrize("script", ["netlithic.py", "webwallet.py"])
def test_cli_version_flag(script):
    out = subprocess.run([sys.executable, script, "--version"], cwd=ROOT,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and nv.VERSION in out.stdout


def test_node_status_reports_version(node):
    with urllib.request.urlopen(f"{node.self_url()}/status", timeout=5) as r:
        st = json.loads(r.read())
    assert st["version"] == nv.VERSION and st["fork_ready"] is True
    assert st["security_fork_height"] == core.SECURITY_FORK_HEIGHT


def test_fork_readiness_counts():
    good = {"version": "0.5.0", "fork_ready": True, "security_fork_height": 5000}
    statuses = {
        "a": good, "b": dict(good),
        "c": {"height": 10},                                            # old node, no fields
        "d": {"version": "0.5.0", "fork_ready": True, "security_fork_height": 6000},  # wrong fork
        "e": {"fork_ready": True, "security_fork_height": "junk"},
        "f": None,                                                       # unreachable
    }
    fr = ex_mod.fork_readiness(statuses)
    assert (fr["ready"], fr["not_ready"], fr["unreachable"], fr["reachable"]) == (2, 3, 1, 5)
    assert fr["versions"]["0.5.0"] == 3


class _Reader:
    def __init__(self, statuses):
        self._s = statuses

    def status(self):
        return {"height": 1, "chain_id": "x", "supply": {}}

    def chain(self):
        return []

    def node_statuses(self):
        return self._s


def test_explorer_home_shows_count_and_version():
    st = {"n1": {"version": "<b>0.5.0</b>", "fork_ready": True, "security_fork_height": 5000},
          "n2": {}, "n3": None}
    _, body = ex_mod.Explorer(_Reader(st)).home()
    assert "1 of 2" in body and "1 unreachable" in body
    assert nv.VERSION in ex_mod.page("t", "")


def test_explorer_polls_real_node_and_peers(node, monkeypatch):
    reader = ex_mod.NodeReader(f"http://127.0.0.1:{node.PORT}", cache_seconds=0)
    with node.state_lock:
        node.peers.add("http://127.0.0.1:9")       # dead peer
    sts = reader.node_statuses(timeout=1)
    fr = ex_mod.fork_readiness(sts)
    assert fr["ready"] >= 1


def test_webwallet_info_and_desktop_page_show_version():
    sys.path.insert(0, str(ROOT) + "/desktop")
    import netlithic_desktop as d
    import os
    with open(os.path.join(d.webwallet.STATIC, "index.html"), encoding="utf-8") as fh:
        page = d.build_index(fh.read())
    assert f"Netlithic {nv.VERSION}" in page
    src = open(f"{ROOT}/webwallet.py").read()
    assert '"version": netlithic_version.VERSION' in src
