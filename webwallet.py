"""Netlithic TESTNET web wallet: serves a static single-page wallet and a tiny
same-origin API proxy, so browsers never need CORS on the node or faucet.

    python webwallet.py --node http://127.0.0.1:8000 --faucet http://127.0.0.1:8090 --port 8082

Keys are created and kept in the user's browser (encrypted with their password).
This server never receives a private key. It only forwards:
  GET  /api/info             -> chain id + minimum fee (checked against the node)
  GET  /api/account?address= -> node /account + points from /contributions
                                + pending_in / pending_out (transfers still in the node mempool)
  POST /api/send   {"tx":..} -> node /receive_tx (already signed in the browser)
  POST /api/drip   {"address"} -> faucet /api/drip
Browsers need HTTPS (or localhost) for WebCrypto; put this behind TLS for real users.
"""
import argparse
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request
from netutil import urlopen

from node_pool import DEFAULT_FALLBACK, NodePool, parse_nodes

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import netlithic_version  # noqa: E402
import wallet as wallet_mod  # noqa: E402

STATIC = os.path.join(ROOT, "webwallet", "static")
FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/wallet.css": ("wallet.css", "text/css; charset=utf-8"),
    "/wallet.js": ("wallet.js", "text/javascript; charset=utf-8"),
    "/wallet-core.js": ("wallet-core.js", "text/javascript; charset=utf-8"),
    "/nacl-fast.min.js": ("nacl-fast.min.js", "text/javascript; charset=utf-8"),
    "/send": ("index.html", "text/html; charset=utf-8"),
    "/qrcode.js": ("qrcode.js", "text/javascript; charset=utf-8"),
    "/logo.png": ("logo.png", "image/png"),
    "/LICENSE-tweetnacl.txt": ("LICENSE-tweetnacl.txt", "text/plain; charset=utf-8"),
}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
       "connect-src 'self'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'")
ADDRESS_RE = re.compile(r"^[0-9a-f]{32}$")
MAX_BODY = 4096


class Upstream:
    def __init__(self, node, faucet, timeout=15, fallback=None):
        self.pool = NodePool(parse_nodes(node, fallback), timeout=timeout, max_bytes=524288)
        self.node, self.faucet, self.timeout = self.pool.primary, (faucet or "").rstrip("/"), timeout

    def node_call(self, path, payload=None):
        """Node A first, then node 2 if A is down. Raises if none answer."""
        return self.pool.request(path, payload)

    def call(self, url, payload=None, headers=None):
        """Returns (status, json). Upstream 4xx answers are passed through."""
        data = None if payload is None else json.dumps(payload).encode()
        h = {"Accept": "application/json", **(headers or {})}
        if data is not None:
            h["Content-Type"] = "application/json"
        try:
            with urlopen(Request(url, data=data, headers=h), timeout=self.timeout) as r:
                return r.status, json.loads(r.read(65536).decode() or "{}")
        except HTTPError as e:
            try:
                body = json.loads(e.read(65536).decode() or "{}")
            except Exception:
                body = {}
            return e.code, body


MAX_PENDING = 20


def pending_for(addr, txs):
    """Transfers to/from addr that are waiting in the mempool (not in a block yet).
    Returns (pending_in, pending_out). Amounts are base units (ints)."""
    from core import tx_id
    pin, pout = [], []
    for tx in txs if isinstance(txs, list) else []:
        if not isinstance(tx, dict) or tx.get("type") in ("reward", "allocation"):
            continue
        p = tx.get("payload") if isinstance(tx.get("payload"), dict) else tx
        s, r = str(p.get("sender") or "").lower(), str(p.get("recipient") or "").lower()
        try:
            amt, fee = int(p.get("amount", 0)), int(p.get("fee", 0) or 0)
            tid = tx_id(tx)
        except Exception:
            continue
        if amt <= 0 or not (ADDRESS_RE.fullmatch(s) and ADDRESS_RE.fullmatch(r)):
            continue
        if s == addr and len(pout) < MAX_PENDING:
            pout.append({"tx_id": tid, "to": r, "amount": amt, "fee": fee})
        if r == addr and s != addr and len(pin) < MAX_PENDING:
            pin.append({"tx_id": tid, "from": s, "amount": amt})
    return pin, pout


HISTORY_KINDS = ("mined", "reward_share", "allocation", "sent", "received")
TXID_RE = re.compile(r"^[0-9a-f]{64}$")


def clean_history_item(it):
    """Only pass through known fields with checked types (node answers are not trusted blindly)."""
    if not isinstance(it, dict) or it.get("kind") not in HISTORY_KINDS:
        return None
    try:
        out = {"kind": it["kind"], "amount": int(it.get("amount", 0)), "height": int(it.get("height", 0)),
               "timestamp": float(it.get("timestamp", 0))}
        if "fee" in it:
            out["fee"] = int(it["fee"])
        if "epoch" in it:
            out["epoch"] = int(it["epoch"])
    except (TypeError, ValueError):
        return None
    for k in ("to", "from"):
        v = str(it.get(k) or "")
        if v:
            if not ADDRESS_RE.fullmatch(v):
                return None
            out[k] = v
    tid = str(it.get("tx_id") or "")
    if TXID_RE.fullmatch(tid):
        out["tx_id"] = tid
    return out


def _error_text(body, fallback):
    err = body.get("error") if isinstance(body, dict) else None
    return str(err)[:200] if isinstance(err, str) else fallback


EXPLORER_RE = re.compile(r"^(/[A-Za-z0-9._~-]+)+$")


def normalize_explorer(path):
    """'' (off) or a same-site path such as '/explorer'. Full URLs are refused on purpose."""
    path = (path or "").strip().rstrip("/")
    if path and not EXPLORER_RE.match(path):
        raise ValueError("explorer link must be a same-site path like /explorer")
    return path


def normalize_share_base(url):
    """'' (use the page's own address) or a plain https://host[/path] for share links."""
    url = (url or "").strip().rstrip("/")
    if url and not re.fullmatch(r"https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?", url):
        raise ValueError("share base must look like https://example.com")
    return url


def make_handler(up, trust_proxy=False, explorer="", share_base=""):
    explorer = normalize_explorer(explorer)
    share_base = normalize_share_base(share_base)
    class Handler(BaseHTTPRequestHandler):
        server_version = "NetlithicWebWallet"
        sys_version = ""

        def log_message(self, fmt, *args):
            pass

        def client_ip(self):
            if trust_proxy:
                fwd = self.headers.get("X-Forwarded-For", "").split(",")[-1].strip()
                if fwd:
                    return fwd[:64]
            return self.client_address[0]

        def send(self, code, data, ctype):
            if isinstance(data, str):
                data = data.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def json(self, code, obj):
            self.send(code, json.dumps(obj), "application/json")

        def do_GET(self):
            u = urlparse(self.path)
            if u.path in FILES:
                name, ctype = FILES[u.path]
                with open(os.path.join(STATIC, name), "rb") as f:
                    return self.send(200, f.read(), ctype)
            if u.path == "/health":
                return self.json(200, {"ok": True})
            try:
                if u.path == "/api/info":
                    return self.api_info()
                if u.path == "/api/account":
                    addr = (parse_qs(u.query).get("address") or [""])[0].strip().lower()
                    if not ADDRESS_RE.fullmatch(addr):
                        return self.json(400, {"error": "That address doesn't look right."})
                    return self.api_account(addr)
                if u.path == "/api/history":
                    q = parse_qs(u.query)
                    addr = (q.get("address") or [""])[0].strip().lower()
                    if not ADDRESS_RE.fullmatch(addr):
                        return self.json(400, {"error": "That address doesn't look right."})
                    try:
                        offset = max(0, min(1000000, int((q.get("offset") or ["0"])[0])))
                        limit = max(1, min(50, int((q.get("limit") or ["20"])[0])))
                    except ValueError:
                        return self.json(400, {"error": "Bad request"})
                    return self.api_history(addr, offset, limit)
            except Exception:
                return self.json(502, {"error": "The network didn't answer. Try again in a minute."})
            return self.json(404, {"error": "Not found"})

        def api_info(self):
            code, st = up.node_call("/status")
            cid = wallet_mod.current_chain_id()
            if code != 200 or st.get("chain_id") != cid:
                return self.json(503, {"error": "The test network is on a different version right now. Try again later."})
            return self.json(200, {"chain_id": cid, "min_fee": wallet_mod.current_min_fee(),
                                   "height": st.get("height"), "testnet": True,
                                   "explorer": explorer, "share_base": share_base,
                                   "version": netlithic_version.VERSION})

        def api_account(self, addr):
            code, acct = up.node_call("/account?address=" + addr)
            if code != 200:
                return self.json(502, {"error": "The network didn't answer. Try again in a minute."})
            points = "0"
            c2, contrib = up.node_call("/contributions")
            if c2 == 200:
                for row in contrib.get("contributors", []):
                    if isinstance(row, dict) and row.get("address") == addr:
                        points = str(row.get("cp", "0"))[:20]
            pay = acct.get("payouts") if isinstance(acct.get("payouts"), dict) else {}
            payouts = {k: str(pay.get(k, "0"))[:24] for k in
                       ("earned_nlt", "vesting_nlt", "claimable_nlt", "points_this_epoch")}
            try:
                payouts["blocks_until_next_payout"] = int(pay.get("blocks_until_next_payout"))
            except (TypeError, ValueError):
                payouts["blocks_until_next_payout"] = None
            # Pending transfers come from the mempool. The balance above stays the
            # confirmed one; pending amounts are reported separately, never added in.
            pin, pout, known = [], [], False
            try:
                c3, mp = up.node_call("/mempool")
                if c3 == 200 and isinstance(mp, dict):
                    pin, pout = pending_for(addr, mp.get("mempool", []))
                    known = True
            except Exception:
                pass
            return self.json(200, {"address": addr, "balance": int(acct.get("balance", 0)),
                                   "next_nonce": int(acct.get("next_nonce", 1)), "points": points,
                                   "payouts": payouts, "pending_in": pin, "pending_out": pout,
                                   "pending_known": known})

        def api_history(self, addr, offset, limit):
            code, h = up.node_call(f"/history?address={addr}&offset={offset}&limit={limit}")
            if code != 200 or not isinstance(h, dict):
                return self.json(503, {"error": "Activity history isn't available on the network yet. Try again later."})
            items = []
            for it in h.get("items", [])[:limit] if isinstance(h.get("items"), list) else []:
                clean = clean_history_item(it)
                if clean:
                    items.append(clean)
            nxt = h.get("next_offset")
            out = {"address": addr, "items": items,
                   "next_offset": nxt if isinstance(nxt, int) and nxt > offset else None, "pending": []}
            if offset == 0:  # waiting transfers only on the first page
                try:
                    c3, mp = up.node_call("/mempool")
                    if c3 == 200 and isinstance(mp, dict):
                        pin, pout = pending_for(addr, mp.get("mempool", []))
                        out["pending"] = ([dict(kind="received", **p) for p in pin] +
                                          [dict(kind="sent", **p) for p in pout])
                except Exception:
                    pass
            return self.json(200, out)

        def read_body(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n < 0 or n > MAX_BODY:
                return None
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except Exception:
                return None
            return body if isinstance(body, dict) else None

        def do_POST(self):
            path = urlparse(self.path).path
            if path not in ("/api/send", "/api/drip"):
                return self.json(404, {"error": "Not found"})
            body = self.read_body()
            if body is None:
                return self.json(400, {"error": "Bad request"})
            try:
                if path == "/api/send":
                    tx = body.get("tx")
                    # Reject anything that isn't a well-formed signed transfer for this chain.
                    if not isinstance(tx, dict) or not wallet_mod.verify_signed_tx(tx):
                        return self.json(400, {"error": "That transfer wasn't signed correctly. Please try again."})
                    code, res = up.node_call("/receive_tx", {"tx": tx})
                    if code == 200:
                        return self.json(200, {"ok": True, "tx_id": str(res.get("tx_id", ""))[:64]})
                    return self.json(400, {"error": _error_text(res, "The network refused that transfer.")})
                addr = str(body.get("address", "")).strip().lower()
                if not ADDRESS_RE.fullmatch(addr):
                    return self.json(400, {"error": "That address doesn't look right."})
                if not up.faucet:
                    return self.json(503, {"error": "Free test coins aren't available right now."})
                code, res = up.call(up.faucet + "/api/drip", {"address": addr},
                                    {"X-Forwarded-For": self.client_ip()})
                if code == 200:
                    return self.json(200, {"ok": True, "amount_nlt": str(res.get("amount_nlt", ""))[:20]})
                return self.json(code if 400 <= code < 500 else 502,
                                 {"error": _error_text(res, "Free test coins aren't available right now.")})
            except Exception:
                return self.json(502, {"error": "The network didn't answer. Try again in a minute."})

    return Handler


def main(argv=None):
    p = argparse.ArgumentParser(description="Netlithic TESTNET web wallet (test coins have no value)")
    p.add_argument("--version", action="version", version=netlithic_version.version_string("netlithic-webwallet"))
    p.add_argument("--node", default=os.environ.get("NETLITHIC_WALLET_NODE", "http://127.0.0.1:8000"))
    p.add_argument("--faucet", default=os.environ.get("NETLITHIC_WALLET_FAUCET", "http://127.0.0.1:8090"))
    p.add_argument("--fallback-node", default=os.environ.get("NETLITHIC_FALLBACK_NODES", DEFAULT_FALLBACK),
                   help="comma-separated nodes to use when --node is down ('' = none)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8082)
    p.add_argument("--trust-proxy", action="store_true",
                   help="use X-Forwarded-For (only behind your own TLS reverse proxy)")
    p.add_argument("--explorer", default=os.environ.get("NETLITHIC_WALLET_EXPLORER", ""),
                   help="same-site explorer path to link to, e.g. /explorer (default: off)")
    p.add_argument("--share-base", default=os.environ.get("NETLITHIC_WALLET_SHARE_BASE", ""),
                   help="public https address used in share links (default: the page's own address)")
    a = p.parse_args(argv)
    try:
        a.explorer = normalize_explorer(a.explorer)
        a.share_base = normalize_share_base(a.share_base)
    except ValueError as err:
        p.error(str(err))
    print(f"Web wallet on http://{a.host}:{a.port} (nodes {a.node} then {a.fallback_node or 'none'}, faucet {a.faucet or 'off'})")
    ThreadingHTTPServer((a.host, a.port), make_handler(Upstream(a.node, a.faucet, fallback=a.fallback_node), a.trust_proxy, a.explorer, a.share_base)).serve_forever()


if __name__ == "__main__":
    main()
