"""Wallet backup check, activity history, address book, desktop helping stats,
opt-in notifications. Test coins have no value."""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import economics as eco
import webwallet
from history import address_history

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "desktop"))
import netlithic_desktop as D  # noqa: E402

NLT = eco.BASE_UNITS_PER_NLT
STATIC = webwallet.STATIC
NODE = shutil.which("node")
A, B = "ab" * 16, "cd" * 16


def read(name, base=STATIC):
    return open(os.path.join(base, name), encoding="utf-8").read()


def jget(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


# ---------------------------------------------------------------- history (node side)
def mine(bc, addr, txs=()):
    assert bc.add_block(bc.mine_block(addr, list(txs)).to_dict()), bc.last_reject_reason


def test_node_history_endpoint(node, wallets):
    alice, bob = wallets
    bc = node.blockchain
    mine(bc, alice.address)
    mine(bc, bob.address, [alice.create_transaction(bob.address, 5 * NLT, nonce=1)])
    code, h = jget(f"{node.self_url()}/history?address={bob.address}")
    assert code == 200
    kinds = [i["kind"] for i in h["items"]]
    assert kinds[:2] == ["received", "mined"] or kinds[:2] == ["mined", "received"]
    got = next(i for i in h["items"] if i["kind"] == "received")
    assert got["from"] == alice.address and got["amount"] == 5 * NLT and re.fullmatch(r"[0-9a-f]{64}", got["tx_id"])
    code, ha = jget(f"{node.self_url()}/history?address={alice.address}")
    sent = next(i for i in ha["items"] if i["kind"] == "sent")
    assert sent["to"] == bob.address and sent["fee"] > 0
    assert jget(f"{node.self_url()}/history?address=nope")[0] == 400


def test_history_pagination_newest_first():
    blocks = [{"index": i, "timestamp": 1000.0 + i,
               "transactions": [{"type": "reward", "recipient": A, "amount": 10 * NLT, "fees": 0}]}
              for i in range(1, 46)]
    p1 = address_history(blocks, A, 0, 20)
    assert [i["height"] for i in p1["items"]][:3] == [45, 44, 43] and p1["next_offset"] == 20 and p1["total"] == 45
    p3 = address_history(blocks, A, 40, 20)
    assert len(p3["items"]) == 5 and p3["next_offset"] is None
    assert len(address_history(blocks, A, 0, 9999)["items"]) == 45  # capped at 50
    assert address_history(blocks, B, 0, 20)["items"] == []


def test_history_kinds():
    blocks = [{"index": 3, "timestamp": 5.0, "transactions": [
        {"type": "payout", "recipient": A, "amount": 7, "epoch": 2, "vest_start": 1, "vest_end": 2},
        {"type": "allocation", "recipient": A, "amount": 3},
        {"type": "reward", "recipient": B, "amount": 10, "fees": 1}]}]
    kinds = {i["kind"] for i in address_history(blocks, A)["items"]}
    assert kinds == {"reward_share", "allocation"}
    assert address_history(blocks, B)["items"][0] == {"kind": "mined", "amount": 11, "height": 3, "timestamp": 5.0}


# ---------------------------------------------------------------- history (web wallet side)
class Up:
    history = {}
    mempool = []


def fake_node():
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def reply(self, code, obj):
            b = json.dumps(obj).encode(); self.send_response(code)
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        def do_GET(self):
            if self.path.startswith("/history"):
                Up.last = self.path
                return self.reply(200, Up.history) if Up.history is not None else self.reply(404, {})
            if self.path == "/mempool":
                return self.reply(200, {"mempool": Up.mempool})
            self.reply(404, {})
    s = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


@pytest.fixture
def ww():
    up = fake_node()
    url = f"http://127.0.0.1:{up.server_address[1]}"
    s = ThreadingHTTPServer(("127.0.0.1", 0), webwallet.make_handler(webwallet.Upstream(url, url)))
    threading.Thread(target=s.serve_forever, daemon=True).start()
    Up.history, Up.mempool = {}, []
    yield f"http://127.0.0.1:{s.server_address[1]}"
    s.shutdown(); up.shutdown()


def test_api_history_cleans_and_adds_pending(ww, wallets):
    alice, bob = wallets
    Up.history = {"items": [
        {"kind": "received", "amount": 5 * NLT, "from": B, "height": 9, "timestamp": 1.5, "tx_id": "e" * 64, "evil": "<b>"},
        {"kind": "<script>", "amount": 1, "height": 1, "timestamp": 1},
        {"kind": "sent", "amount": 1, "to": "not-an-address", "height": 1, "timestamp": 1},
        {"kind": "mined", "amount": "x", "height": 1, "timestamp": 1}], "next_offset": 20}
    Up.mempool = [alice.create_transaction(A, 2 * NLT, nonce=1)]
    code, h = jget(ww + "/api/history?address=" + A)
    assert code == 200
    assert h["items"] == [{"kind": "received", "amount": 5 * NLT, "from": B, "height": 9, "timestamp": 1.5, "tx_id": "e" * 64}]
    assert h["next_offset"] == 20
    assert h["pending"][0]["kind"] == "received" and h["pending"][0]["from"] == alice.address
    code, h2 = jget(ww + "/api/history?address=" + A + "&offset=20&limit=500")
    assert h2["pending"] == [] and "limit=50" in Up.last and "offset=20" in Up.last


def test_api_history_validates(ww):
    assert jget(ww + "/api/history?address=../x")[0] == 400
    assert jget(ww + "/api/history?address=" + A + "&offset=abc")[0] == 400
    Up.history = None
    assert jget(ww + "/api/history?address=" + A)[0] == 503


def test_history_is_get_only_and_takes_no_key(ww):
    req = urllib.request.Request(ww + "/api/history?address=" + A, data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(req, timeout=5)


# ---------------------------------------------------------------- page / JS rules
def test_page_has_all_five_features():
    html = read("index.html")
    for id_ in ("v-save", "b-save-file", "b-save-print", "b-check", "h-backup-nag", "print-sheet",
                "h-activity", "b-more", "h-contacts", "s-contact", "b-prefill-save", "b-notify", "toast", "b-print-new"):
        assert f'id="{id_}"' in html
    assert "never sent to our server" in html and "Test coins have no value" in html
    assert "style=" not in html and "onclick" not in html.lower() and "<script>" not in html


def test_js_stays_safe():
    js = read("wallet.js") + read("wallet-core.js")
    for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "console.log"):
        assert bad not in js
    w = read("wallet.js")
    # contacts and notification choice stay in localStorage; the only api() calls are the existing ones + history
    calls = set(re.findall(r'api\("([a-z]+)', w))
    assert calls <= {"info", "account", "history", "drip", "send"}
    assert "CONTACTS" in w and "localStorage.setItem(CONTACTS" in w
    # the backup check only stores a flag, never the seed
    assert re.search(r'setItem\(CONFIRM \+ address, "yes"\)', w)
    for line in w.splitlines():
        if "setItem(" in line and "seed" in line.lower():
            assert "encryptSeed(" in line, line  # only the password-locked blob is ever stored
    # notifications are off by default and asked for only on a click
    assert w.count("Notification.requestPermission") == 1 and "toggleNotify" in w
    assert "Notification.requestPermission" in w.split("async function toggleNotify")[1].split("async function openWallet")[0]


def test_print_css_shows_only_the_sheet():
    css = read("wallet.css")
    assert "@media print" in css and ".print-sheet" in css


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_core_helpers_in_node(tmp_path):
    script = tmp_path / "t.js"
    script.write_text("""
const C = require(%r);
const A = "ab".repeat(16), B = "cd".repeat(16);
let l = C.addContact([], "  Mum <3 ", A);
if (l[0].name !== "Mum 3") throw "clean " + l[0].name;
l = C.addContact(l, "Mum again", A); if (l.length !== 1 || l[0].name !== "Mum again") throw "dedupe";
let bad = 0; try { C.addContact(l, "x", "zz"); } catch (e) { bad++; } try { C.addContact(l, "  ", B); } catch (e) { bad++; }
if (bad !== 2) throw "validation";
if (C.parseContacts("not json").length !== 0 || C.parseContacts('[{"name":"a","address":"x"}]').length !== 0) throw "parse";
const nameOf = a => C.contactName(l, a);
const r = C.describeActivity({kind: "received", amount: 500000000, from: A}, nameOf);
if (r !== "Received 5 test NLT from Mum again") throw r;
if (C.describeActivity({kind: "sent", amount: 100000000, to: B}, nameOf) !== "Sent 1 test NLT to cdcd…cdcd") throw "sent";
if (!/^Reward share/.test(C.describeActivity({kind: "reward_share", amount: 1}))) throw "rs";
if (!/^Mined a block/.test(C.describeActivity({kind: "mined", amount: 1000000000}))) throw "mined";
if (!/on the way/.test(C.describeActivity({kind: "received", amount: 1, from: A, pending: true}))) throw "pend";
const seen = {}, items = [{kind: "received", amount: 1, from: A, height: 2, tx_id: "e"}, {kind: "mined", amount: 1, height: 1}];
if (C.newArrivals(seen, items, true).length !== 0) throw "first look is quiet";
const more = [{kind: "reward_share", amount: 9, height: 3, epoch: 1}, {kind: "mined", amount: 1, height: 4}].concat(items);
const f = C.newArrivals(seen, more, false);
if (f.length !== 1 || f[0].kind !== "reward_share") throw "arrivals";
if (C.newArrivals(seen, [{kind: "received", amount: 1, from: A, pending: true}], false).length) throw "pending never notifies";
if (!/no value/.test(C.notifyText(f[0]))) throw "words";
if (!/^Today/.test(C.whenText(Date.now() / 1000, Date.now()))) throw "today";
console.log("ok");
""" % os.path.join(STATIC, "wallet-core.js"))
    out = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=30)
    assert out.stdout.strip() == "ok", out.stderr


# ---------------------------------------------------------------- desktop
def test_help_stats_count_time_and_persist(tmp_path):
    t = [1_000_000.0]
    path = str(tmp_path / "helping-stats.json")
    st = D.HelpStats(path, clock=lambda: t[0])
    st.tick(True)
    for _ in range(10):
        t[0] += 60; st.tick(True)
    t[0] += 3600; st.tick(True)        # a sleep gap isn't counted
    t[0] += 30; st.tick(False)         # stopped: this last bit counts, then nothing
    t[0] += 600; st.tick(False)
    st.add_block()
    snap = st.snapshot()
    assert snap["total_seconds"] == 630 and snap["blocks_found_total"] == 1 and snap["total"] == "10 minutes"
    st.flush()
    data = json.load(open(path))
    assert set(data) == {"total_seconds", "blocks_found", "days"}
    again = D.HelpStats(path, clock=lambda: t[0])
    assert again.snapshot()["total_seconds"] == 630 and again.snapshot()["blocks_found_total"] == 1


def test_help_stats_survive_a_broken_file(tmp_path):
    p = tmp_path / "s.json"; p.write_text("{not json")
    assert D.HelpStats(str(p)).snapshot()["total_seconds"] == 0
    p.write_text(json.dumps({"total_seconds": -5, "blocks_found": "x", "days": {"../x": 9}}))
    assert D.HelpStats(str(p)).snapshot()["total_seconds"] == 0


def test_duration_words():
    assert D.duration_words(0) == "less than a minute"
    assert D.duration_words(61) == "1 minute"
    assert D.duration_words(7260) == "2 hours 1 minute"


def test_earner_status_includes_stats(tmp_path):
    def call(url, payload=None, headers=None):
        return 200, {"height": 5}
    e = D.Earner("http://127.0.0.1:1", "http://seed:1", call, stats=D.HelpStats(str(tmp_path / "s.json")))
    s = e.status()
    assert s["stats"]["blocks_found_total"] == 0 and "today" in s["stats"]


def test_desktop_page_has_stats_and_notify_hook():
    html = D.build_index(read("index.html"))
    for id_ in ("e-today", "e-total", "e-blocks-all", "e-points", "e-next", "h-backup-nag", "h-activity"):
        assert f'id="{id_}"' in html
    dj = read("desktop.js", D.DESKTOP_STATIC)
    assert "NetlithicNotify" in dj and "api().notify" in dj


def test_desktop_notify_is_sanitised_and_falls_back():
    seen = []
    api = D.WindowApi(show_notification=lambda t, b: seen.append((t, b)) or True)
    assert api.notify('Hi"\n', 'You got "5"\x00 test NLT\\') is True
    assert seen == [("Hi", "You got 5 test NLT")]
    assert D.WindowApi(show_notification=lambda t, b: False).notify("a", "b") is False
    def boom(t, b): raise OSError("no notifier")
    assert D.WindowApi(show_notification=boom).notify("a", "b") is False
    assert D.WindowApi(show_notification=lambda t, b: True).notify("a", "") is False
