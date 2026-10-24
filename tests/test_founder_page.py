"""Founder transparency page (/explorer/founder) and founder_report rule check."""
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import explorer as ex_mod
from explorer import Explorer, founder_report, make_handler, FOUNDER_ADDRESS as F

OTHER = "a" * 32


def blk(i, ts, who):
    return {"index": i, "timestamp": ts, "hash": f"h{i}",
            "transactions": [{"type": "reward", "recipient": who, "amount": 1000, "fees": 0}]}


def test_report_normal_and_backstop_ok():
    chain = [blk(4998, 0, F), blk(4999, 60, OTHER), blk(5000, 60 + 1800, F),
             blk(5001, 60 + 3600, F), blk(5002, 60 + 3700, OTHER)]
    r = founder_report(chain)
    assert r["count"] == 3 and r["backstop_count"] == 2 and r["total"] == 3000
    assert [b["backstop"] for b in r["blocks"]] == [False, True, True]
    assert r["rule_followed"] is True and not r["problems"]


def test_report_flags_quick_backstop():
    chain = [blk(4999, 0, OTHER), blk(5000, 600, F)]
    r = founder_report(chain)
    assert r["rule_followed"] is False and "5000" in r["problems"][0]


def test_report_flags_two_founder_blocks_close():
    # second founder block: 30 min after other's block, but founder gap check still fine;
    # make previous-block gap ok yet founder gap < 30 min impossible -> craft via min_gap 60
    chain = [blk(4999, 0, OTHER), blk(5000, 1800, F), blk(5001, 3600, F)]
    assert founder_report(chain)["rule_followed"] is True
    r = founder_report(chain, min_gap_minutes=60)
    assert r["rule_followed"] is False and "within 60 min" in r["problems"][0]


def test_report_pre_5000_never_breaks_rule():
    chain = [blk(i, i * 5, F) for i in range(10)]
    r = founder_report(chain)
    assert r["count"] == 10 and r["backstop_count"] == 0 and r["rule_followed"]


class FakeReader:
    def __init__(self, chain):
        self._c = chain

    def chain(self):
        return self._c


def serve(ex):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ex, "/explorer"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/explorer"


def test_founder_page_html():
    chain = [blk(4999, 0, OTHER), blk(5000, 1800, F)]
    srv, base = serve(Explorer(FakeReader(chain)))
    try:
        html = urllib.request.urlopen(base + "/founder").read().decode()
    finally:
        srv.shutdown()
    assert "Backstop rule followed: yes" in html and F in html
    assert "Test coins have no value" in html and "Backstop" in html
    assert 'href="/explorer/founder"' in html
    for bad in ("price", "invest", "return", "profit"):
        assert bad not in html.lower()


def test_founder_page_live_node(node, wallets):
    alice, _ = wallets
    bc = node.blockchain
    assert bc.add_block(bc.mine_block(F, []).to_dict())
    assert bc.add_block(bc.mine_block(alice.address, []).to_dict())
    srv, base = serve(Explorer(ex_mod.NodeReader(node.self_url(), cache_seconds=0)))
    try:
        html = urllib.request.urlopen(base + "/founder").read().decode()
    finally:
        srv.shutdown()
    assert "Backstop rule followed: yes" in html and "Normal (before 5000)" in html
