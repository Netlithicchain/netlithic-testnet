import threading, urllib.request, urllib.error
import pytest

import explorer as ex_mod
from explorer import Explorer, make_handler

EVIL = '<script>alert("x")</script>'
ADDR = "c" * 32


def signed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from wallet import Wallet
    w = Wallet.create("a")
    return w, w.create_transaction(ADDR, 5 * 10**8, 1)


class FakeReader:
    def __init__(self, tx):
        self.tx = tx
        self.chain_data = [
            {"index": 0, "timestamp": 1700000000, "hash": "0" * 64, "previous_hash": "0" * 64, "nonce": 1,
             "difficulty": 3, "transactions": [{"type": "allocation", "bucket": EVIL, "recipient": "f" * 32,
                                                "amount": 10**16, "locked": True}]},
            {"index": 1, "timestamp": 1700000600, "hash": "1" * 64, "previous_hash": "0" * 64, "nonce": EVIL,
             "difficulty": 3, "transactions": [{"type": "reward", "recipient": ADDR, "amount": 10**9, "fees": 0}, tx]},
        ]

    def status(self):
        return {"height": 1, "chain_id": EVIL, "difficulty": 3, "mempool_size": 0,
                "supply": {"minted": "100", "next_block_reward": "10", "max_supply": EVIL},
                "buckets": {"community": {"address": "f" * 32, "allocation": EVIL, "unlocked": "0",
                                          "locked": "1", "unlock_schedule": EVIL}},
                "community_payouts": {"epoch_blocks": EVIL}}

    def chain(self):
        return self.chain_data

    def balance(self, a):
        return {"balance": 15 * 10**8, "balance_nlt": "15", "spendable_nlt": EVIL}

    def contributions(self):
        return [{"address": ADDR, "cp": "1.5", "blocks_mined": 1}]

    def mempool(self):
        return []


@pytest.fixture
def server(tmp_path, monkeypatch):
    _, tx = signed(tmp_path, monkeypatch)
    ex = Explorer(FakeReader(tx))
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ex))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", tx
    srv.shutdown()


def get(url):
    try:
        with urllib.request.urlopen(url) as r:
            return r.status, r.read().decode(errors="replace"), r.geturl()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), url


def check_safe(body):
    assert "<script>" not in body
    assert "Testnet: coins have no value" in body


def test_pages_render_and_escape(server):
    url, tx = server
    from core import tx_id
    tid = tx_id(tx)
    for path, must in [("/", "Latest blocks"), ("/block/1", "Block 1"), ("/block/" + "1" * 64, "Block 1"),
                       (f"/tx/{tid}", "transfer"), (f"/address/{ADDR}", "Contribution points"),
                       ("/economy", "Allocation buckets")]:
        code, body, _ = get(url + path)
        assert code == 200, path
        assert must in body, path
        check_safe(body)
        if path != "/":
            assert "&lt;script&gt;" in body or path.startswith("/tx") or path.startswith("/address/"), path
    code, body, _ = get(f"{url}/address/{ADDR}")
    assert "15" in body and "1.5" in body and "&lt;script&gt;" in body
    code, body, _ = get(f"{url}/tx/{tid}")
    assert "5" in body and tx["payload"]["sender"][:16] in body


def test_search_redirects(server):
    url, tx = server
    from core import tx_id
    assert get(url + "/search?q=1")[2].endswith("/block/1")
    assert get(url + "/search?q=" + ADDR)[2].endswith("/address/" + ADDR)
    assert get(url + "/search?q=" + tx_id(tx))[2].endswith("/tx/" + tx_id(tx))
    code, body, _ = get(url + "/search?q=%3Cscript%3Ealert(1)%3C/script%3E")
    assert code == 404
    check_safe(body)
    assert "&lt;script&gt;alert(1)" in body


@pytest.mark.parametrize("path", ["/block/99", "/block/<x>", "/tx/abc", "/tx/" + "9" * 64,
                                  "/address/zz", "/nope/%3Cscript%3E"])
def test_not_found_is_escaped(server, path):
    url, _ = server
    code, body, _ = get(url + path)
    assert code == 404
    check_safe(body)


def test_icon_and_headers(server):
    url, _ = server
    with urllib.request.urlopen(url + "/static/netlithic-icon.png") as r:
        assert r.headers["Content-Type"] == "image/png" and r.read(4) == b"\x89PNG"
    with urllib.request.urlopen(url + "/") as r:
        assert "default-src 'none'" in r.headers["Content-Security-Policy"]


def test_node_down_gives_502(tmp_path):
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Explorer(ex_mod.NodeReader("http://127.0.0.1:9"))))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        code, body, _ = get(f"http://127.0.0.1:{srv.server_address[1]}/")
        assert code == 502 and "Could not read from the node" in body
    finally:
        srv.shutdown()


def test_against_real_node(node):
    reader = ex_mod.NodeReader(f"http://127.0.0.1:{node.PORT}", cache_seconds=0)
    title, body = Explorer(reader).home()
    assert "Latest blocks" in body and "netlithic-dev" in body
    assert Explorer(reader).block("0")[0] == "Block 0"


# ---- URL prefix (served at /explorer/ behind Caddy) ---------------------

@pytest.fixture
def prefixed(tmp_path, monkeypatch):
    _, tx = signed(tmp_path, monkeypatch)
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Explorer(FakeReader(tx)), "/explorer/"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", tx
    srv.shutdown()


def _root_links(body):
    import re
    return re.findall(r"""(?:href|src|action)=["'](/[^"']*)""", body)


@pytest.mark.parametrize("pre", ["", "/explorer"])  # stripped (handle_path) or kept (handle)
def test_prefix_links_all_carry_base(prefixed, pre):
    url, tx = prefixed
    from core import tx_id
    for path in ["/", "/economy", "/block/1", "/tx/" + tx_id(tx), "/address/" + ADDR, "/block/99"]:
        code, body, _ = get(url + pre + path)
        links = _root_links(body)
        assert links, path
        assert all(l.startswith("/explorer/") for l in links), (path, links)
    code, _, _ = get(url + pre + "/static/netlithic-icon.png")
    assert code == 200


def test_prefix_search_redirect_and_bare_root(prefixed):
    url, tx = prefixed
    assert get(url + "/search?q=1")[2].endswith("/explorer/block/1")
    assert get(url + "/explorer/search?q=" + ADDR)[2].endswith("/explorer/address/" + ADDR)
    import http.client
    c = http.client.HTTPConnection(url.split("//")[1]); c.request("GET", "/explorer")
    r = c.getresponse()
    assert r.status == 301 and r.getheader("Location") == "/explorer/"


def test_default_has_no_prefix(server):
    url, _ = server
    _, body, _ = get(url + "/")
    assert "/explorer" not in body and "href=\"/\"" in body


@pytest.mark.parametrize("bad", ["/a b", "/x\"y", "//evil.com", "/a/../b?", "http://x"])
def test_base_path_rejects_bad(bad):
    with pytest.raises(ValueError):
        ex_mod.normalize_base_path(bad)


def test_base_path_normalizes():
    assert ex_mod.normalize_base_path("") == ""
    assert ex_mod.normalize_base_path("explorer/") == "/explorer"
    assert ex_mod.with_base("<a href='/x'>", "/e") == "<a href='/e/x'>"
    assert ex_mod.with_base('<a href="//x">', "/e") == '<a href="//x">'


def test_address_page_shows_on_the_way(tmp_path, monkeypatch):
    _, tx = signed(tmp_path, monkeypatch)
    r = FakeReader(tx)
    pend = {"payload": {"sender": "d" * 32, "recipient": ADDR, "amount": 7 * 10**8, "fee": 10000, "nonce": 1}}
    r.mempool = lambda: [pend, {"payload": {"sender": "1" * 32, "recipient": "2" * 32, "amount": 1}}]
    _, body = Explorer(r).address(ADDR)
    assert "On the way" in body and "Receiving 7 test NLT from" in body
    assert body.count("on the way") == 1 and "Test coins have no value" in body
    _, body2 = Explorer(FakeReader(tx)).address(ADDR)
    assert "On the way" not in body2
