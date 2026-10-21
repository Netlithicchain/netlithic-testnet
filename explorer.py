"""Netlithic block explorer: a small read-only web app over a node's public API.

    python explorer.py --node http://127.0.0.1:8000 --port 8081

Pages: / (latest blocks + summary), /block/<height|hash>, /tx/<id>,
/address/<address>, /economy, /search?q=..., /static/netlithic-icon.png.
Only GETs public node endpoints (/status, /chain, /balance, /contributions,
/mempool). Every value is HTML-escaped before it is shown.
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import economics  # noqa: E402
from core import tx_id  # noqa: E402

HEX32 = re.compile(r"^[0-9a-f]{32}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
ICON = os.path.join(ROOT, "static", "netlithic-icon.png")


def e(v):
    return html.escape(str(v), quote=True)


class NodeReader:
    def __init__(self, url, cache_seconds=5):
        self.url, self.ttl = url.rstrip("/"), cache_seconds
        self._cache, self._lock = {}, threading.Lock()

    def get(self, path):
        with self._lock:
            hit = self._cache.get(path)
            if hit and time.time() - hit[0] < self.ttl:
                return hit[1]
        with urlopen(Request(self.url + path, headers={"Accept": "application/json"}), timeout=15) as r:
            data = json.loads(r.read().decode() or "{}")
        with self._lock:
            self._cache[path] = (time.time(), data)
        return data

    def status(self):
        return self.get("/status")

    def chain(self):
        return self.get("/chain").get("chain", [])

    def balance(self, address):
        return self.get(f"/balance?address={quote(address)}")

    def contributions(self):
        return self.get("/contributions").get("contributors", [])

    def mempool(self):
        return self.get("/mempool").get("mempool", [])


# ------------------------------------------------------------ tx helpers

def tx_kind(tx):
    return tx.get("type") or ("transfer" if "payload" in tx else "other")


def tx_parties(tx):
    p = tx.get("payload") if isinstance(tx.get("payload"), dict) else tx
    return p.get("sender"), p.get("recipient"), p.get("amount"), p.get("fee")


def safe_tx_id(tx):
    try:
        return tx_id(tx)
    except Exception:
        return ""


def nlt(v):
    try:
        return economics.to_nlt(int(v))
    except Exception:
        return "-"


def when(ts):
    try:
        return dt.datetime.fromtimestamp(float(ts), dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return "-"


def short(h, n=12):
    h = str(h or "")
    return h if len(h) <= n + 3 else h[:n] + "..."


def addr_link(a):
    if not a:
        return "&mdash;"
    return f'<a class="mono" href="/address/{e(quote(str(a)))}">{e(short(a, 16))}</a>'


# ------------------------------------------------------------ layout

CSS = """
:root{--bg:#0a1430;--panel:#111f47;--line:#23397a;--text:#e6ecff;--muted:#9fb0dd;--accent:#7fd3ff;--blue:#2f6bff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,sans-serif}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.banner{background:#f5b400;color:#1a1300;text-align:center;padding:6px;font-weight:600}
header{display:flex;align-items:center;gap:14px;padding:14px 24px;background:#0d1a3d;border-bottom:1px solid var(--line);flex-wrap:wrap}
header img{height:40px}header .brand{font-size:20px;font-weight:700;color:var(--text)}
header nav a{margin-right:14px}header form{margin-left:auto;display:flex;gap:6px}
input{padding:8px 10px;border-radius:8px;border:1px solid #2f4b9a;background:var(--bg);color:var(--text);width:340px;max-width:60vw}
button{padding:8px 14px;border:0;border-radius:8px;background:var(--blue);color:#fff;font-weight:600}
main{max-width:1100px;margin:24px auto;padding:0 16px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:20px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px}
.card .k{color:var(--muted);font-size:13px}.card .v{font-size:20px;font-weight:700}
table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden;margin-bottom:20px}
th,td{text-align:left;padding:8px 12px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:13px}.mono{font-family:ui-monospace,monospace;word-break:break-all}
h1{font-size:22px}h2{font-size:17px;color:var(--accent)}.muted{color:var(--muted)}
.err{background:#5c1020;padding:12px;border-radius:8px}
"""


def page(title, body):
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(title)} | Netlithic Explorer</title>
<link rel="icon" href="/static/netlithic-icon.png"><style>{CSS}</style></head><body>
<div class="banner">Testnet: coins have no value</div>
<header><a href="/"><img src="/static/netlithic-icon.png" alt="Netlithic"></a>
<a class="brand" href="/">Netlithic Explorer</a><nav><a href="/">Blocks</a><a href="/economy">Economy</a></nav>
<form action="/search" method="get"><input name="q" placeholder="Block height, block hash, tx id or address" maxlength="80">
<button>Search</button></form></header><main>{body}</main></body></html>"""


def kv_table(rows):
    return "<table>" + "".join(f"<tr><th>{e(k)}</th><td>{v}</td></tr>" for k, v in rows) + "</table>"


def tx_rows(txs, height=None):
    out = ["<table><tr><th>Tx</th><th>Type</th><th>From</th><th>To</th><th>Amount (NLT)</th>"
           + ("<th>Block</th>" if height is not None else "") + "</tr>"]
    for item in txs:
        tx, h = (item if isinstance(item, tuple) else (item, None))
        tid = safe_tx_id(tx)
        s, r, amt, _ = tx_parties(tx)
        link = f'<a class="mono" href="/tx/{e(tid)}">{e(short(tid))}</a>' if tid else "&mdash;"
        cells = [link, e(tx_kind(tx)), addr_link(s), addr_link(r), e(nlt(amt))]
        if height is not None:
            cells.append(f'<a href="/block/{e(h)}">{e(h)}</a>')
        out.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
    if len(out) == 1:
        out.append('<tr><td colspan="6" class="muted">No transactions</td></tr>')
    return "".join(out) + "</table>"


# ------------------------------------------------------------ pages

class Explorer:
    def __init__(self, reader):
        self.r = reader

    def home(self):
        st, chain = self.r.status(), self.r.chain()
        sup = st.get("supply", {})
        cards = [("Height", st.get("height")), ("Chain", st.get("chain_id")),
                 ("Difficulty", st.get("difficulty")), ("Mempool", st.get("mempool_size")),
                 ("Minted NLT", sup.get("minted")), ("Next reward", f"{sup.get('next_block_reward')} NLT")]
        body = "<div class='cards'>" + "".join(
            f"<div class='card'><div class='k'>{e(k)}</div><div class='v'>{e(v)}</div></div>" for k, v in cards) + "</div>"
        body += "<h2>Latest blocks</h2><table><tr><th>Height</th><th>Hash</th><th>Time</th><th>Txs</th><th>Miner</th></tr>"
        for b in reversed(chain[-25:]):
            miner = next((t.get("recipient") for t in b.get("transactions", []) if t.get("type") == "reward"), None)
            body += (f"<tr><td><a href='/block/{e(b.get('index'))}'>{e(b.get('index'))}</a></td>"
                     f"<td class='mono'>{e(short(b.get('hash'), 20))}</td><td>{e(when(b.get('timestamp')))}</td>"
                     f"<td>{e(len(b.get('transactions', [])))}</td><td>{addr_link(miner)}</td></tr>")
        return "Latest blocks", body + "</table>"

    def find_block(self, key):
        chain = self.r.chain()
        for b in chain:
            if str(b.get("index")) == key or b.get("hash") == key:
                return b
        return None

    def block(self, key):
        b = self.find_block(key)
        if not b:
            return None
        i = b.get("index", 0)
        nav = []
        if i > 0:
            nav.append(f"<a href='/block/{e(i - 1)}'>&larr; Block {e(i - 1)}</a>")
        nav.append(f"<a href='/block/{e(i + 1)}'>Block {e(i + 1)} &rarr;</a>")
        body = f"<h1>Block {e(i)}</h1>" + kv_table([
            ("Hash", f"<span class='mono'>{e(b.get('hash'))}</span>"),
            ("Previous", f"<a class='mono' href='/block/{e(b.get('previous_hash'))}'>{e(b.get('previous_hash'))}</a>"),
            ("Time", e(when(b.get("timestamp")))), ("Difficulty", e(b.get("difficulty"))),
            ("Nonce", e(b.get("nonce"))), ("Transactions", e(len(b.get("transactions", []))))])
        body += "<h2>Transactions</h2>" + tx_rows(b.get("transactions", [])) + " &nbsp; ".join(nav)
        return f"Block {i}", body

    def find_tx(self, tid):
        for b in self.r.chain():
            for tx in b.get("transactions", []):
                if safe_tx_id(tx) == tid:
                    return tx, b
        for tx in self.r.mempool():
            if safe_tx_id(tx) == tid:
                return tx, None
        return None, None

    def tx(self, tid):
        tx, b = self.find_tx(tid)
        if tx is None:
            return None
        s, r, amt, fee = tx_parties(tx)
        p = tx.get("payload") if isinstance(tx.get("payload"), dict) else {}
        rows = [("Tx id", f"<span class='mono'>{e(tid)}</span>"), ("Type", e(tx_kind(tx))),
                ("Status", f"In block <a href='/block/{e(b.get('index'))}'>{e(b.get('index'))}</a>"
                 if b else "Pending (mempool)"),
                ("From", addr_link(s)), ("To", addr_link(r)), ("Amount", e(nlt(amt)) + " NLT")]
        if fee is not None:
            rows.append(("Fee", e(nlt(fee)) + " NLT"))
        if tx.get("type") == "reward":
            rows.append(("Fees collected", e(nlt(tx.get("fees", 0))) + " NLT"))
        if tx.get("bucket"):
            rows.append(("Bucket", e(tx.get("bucket"))))
        if "nonce" in p:
            rows.append(("Nonce", e(p["nonce"])))
        if p.get("timestamp"):
            rows.append(("Signed at", e(when(p["timestamp"]))))
        return "Transaction", "<h1>Transaction</h1>" + kv_table(rows)

    def address(self, addr):
        bal = self.r.balance(addr)
        cp = next((c for c in self.r.contributions() if c.get("address") == addr), {})
        rows = [("Address", f"<span class='mono'>{e(addr)}</span>"),
                ("Balance", e(bal.get("balance_nlt", nlt(bal.get("balance", 0)))) + " NLT")]
        if "spendable_nlt" in bal:
            rows.append(("Spendable", e(bal["spendable_nlt"]) + " NLT"))
        if bal.get("locked_or_unvested_nlt") not in (None, "0"):
            rows.append(("Locked / vesting", e(bal["locked_or_unvested_nlt"]) + " NLT"))
        rows.append(("Contribution points", e(cp.get("cp", "0"))))
        if cp:
            rows.append(("Blocks mined", e(cp.get("blocks_mined", 0))))
        txs = []
        for b in self.r.chain():
            for tx in b.get("transactions", []):
                s, r, _, _ = tx_parties(tx)
                if addr in (s, r):
                    txs.append((tx, b.get("index")))
        body = f"<h1>Address</h1>{kv_table(rows)}<h2>Activity (latest 100)</h2>" + tx_rows(list(reversed(txs))[:100], height=True)
        return "Address", body

    def economy(self):
        st = self.r.status()
        sup, buckets, pay = st.get("supply", {}), st.get("buckets", {}), st.get("community_payouts", {})
        body = "<h1>Supply and economy</h1>" + kv_table([(k.replace("_", " ").capitalize(), e(v))
                                                         for k, v in sup.items() if not isinstance(v, (dict, list))])
        body += "<h2>Allocation buckets</h2><table><tr><th>Bucket</th><th>Address</th><th>Allocation</th><th>Unlocked</th><th>Locked</th><th>Schedule</th></tr>"
        for name, bk in buckets.items():
            body += (f"<tr><td>{e(name)}</td><td>{addr_link(bk.get('address'))}</td><td>{e(bk.get('allocation'))}</td>"
                     f"<td>{e(bk.get('unlocked'))}</td><td>{e(bk.get('locked'))}</td><td>{e(bk.get('unlock_schedule'))}</td></tr>")
        body += "</table><h2>Community payouts</h2>" + kv_table(
            [(k.replace("_", " ").capitalize(), e(v)) for k, v in pay.items()])
        body += "<h2>Top contributors</h2><table><tr><th>Address</th><th>Points</th><th>Blocks mined</th></tr>"
        for c in self.r.contributions()[:25]:
            body += f"<tr><td>{addr_link(c.get('address'))}</td><td>{e(c.get('cp'))}</td><td>{e(c.get('blocks_mined', 0))}</td></tr>"
        return "Economy", body + "</table>"

    def search(self, q):
        q = (q or "").strip().lower()
        if q.isdigit():
            return f"/block/{q}"
        if HEX32.match(q):
            return f"/address/{q}"
        if HEX64.match(q):
            if self.find_block(q):
                return f"/block/{q}"
            if self.find_tx(q)[0] is not None:
                return f"/tx/{q}"
        return None


def make_handler(explorer):
    class Handler(BaseHTTPRequestHandler):
        server_version = "NetlithicExplorer"

        def log_message(self, fmt, *args):
            return

        def send(self, code, body, ctype="text/html; charset=utf-8", extra=()):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; form-action 'self'")
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def not_found(self, what="Page"):
            return self.send(404, page("Not found", f"<h1>{e(what)} not found</h1><p><a href='/'>Back to blocks</a></p>"))

        def do_GET(self):
            u = urlparse(self.path)
            parts = [p for p in u.path.split("/") if p]
            try:
                if u.path == "/static/netlithic-icon.png":
                    with open(ICON, "rb") as f:
                        return self.send(200, f.read(), "image/png", [("Cache-Control", "max-age=86400")])
                if u.path == "/health":
                    return self.send(200, '{"ok": true}', "application/json")
                if not parts:
                    return self.send(200, page(*explorer.home()))
                if parts == ["economy"]:
                    return self.send(200, page(*explorer.economy()))
                if parts == ["search"]:
                    q = (parse_qs(u.query).get("q") or [""])[0][:80]
                    target = explorer.search(q)
                    if target:
                        return self.send(302, "", extra=[("Location", target)])
                    return self.send(404, page("Not found", f"<h1>Nothing found for <span class='mono'>{e(q)}</span></h1>"
                                               "<p class='muted'>Try a block height, a 64-character block hash or tx id, "
                                               "or a 32-character address.</p>"))
                if len(parts) == 2 and parts[0] == "block":
                    key = parts[1].lower()
                    if not (key.isdigit() or HEX64.match(key)):
                        return self.not_found("Block")
                    r = explorer.block(key)
                    return self.send(200, page(*r)) if r else self.not_found("Block")
                if len(parts) == 2 and parts[0] == "tx":
                    key = parts[1].lower()
                    r = explorer.tx(key) if HEX64.match(key) else None
                    return self.send(200, page(*r)) if r else self.not_found("Transaction")
                if len(parts) == 2 and parts[0] == "address":
                    key = parts[1].lower()
                    if not HEX32.match(key):
                        return self.not_found("Address")
                    return self.send(200, page(*explorer.address(key)))
                return self.not_found()
            except Exception:
                return self.send(502, page("Node unavailable", "<div class='err'>Could not read from the node. Try again shortly.</div>"))

        do_HEAD = do_GET

    return Handler


def main(argv=None):
    p = argparse.ArgumentParser(description="Netlithic block explorer (read-only)")
    p.add_argument("--node", default=os.environ.get("NETLITHIC_EXPLORER_NODE", "http://127.0.0.1:8000"))
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8081)
    p.add_argument("--cache-seconds", type=float, default=5)
    args = p.parse_args(argv)
    print(f"Netlithic explorer on http://{args.host}:{args.port} reading {args.node} (testnet: coins have no value)")
    ThreadingHTTPServer((args.host, args.port), make_handler(Explorer(NodeReader(args.node, args.cache_seconds)))).serve_forever()


if __name__ == "__main__":
    sys.exit(main())
