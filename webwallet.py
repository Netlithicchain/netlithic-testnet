"""Netlithic TESTNET web wallet: serves a static single-page wallet and a tiny
same-origin API proxy, so browsers never need CORS on the node or faucet.

    python webwallet.py --node http://127.0.0.1:8000 --faucet http://127.0.0.1:8090 --port 8082

Keys are created and kept in the user's browser (encrypted with their password).
This server never receives a private key. It only forwards:
  GET  /api/info             -> chain id + minimum fee (checked against the node)
  GET  /api/account?address= -> node /account + points from /contributions
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
from urllib.request import Request, urlopen

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import wallet as wallet_mod  # noqa: E402

STATIC = os.path.join(ROOT, "webwallet", "static")
FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/wallet.css": ("wallet.css", "text/css; charset=utf-8"),
    "/wallet.js": ("wallet.js", "text/javascript; charset=utf-8"),
    "/wallet-core.js": ("wallet-core.js", "text/javascript; charset=utf-8"),
    "/nacl-fast.min.js": ("nacl-fast.min.js", "text/javascript; charset=utf-8"),
    "/logo.png": ("logo.png", "image/png"),
    "/LICENSE-tweetnacl.txt": ("LICENSE-tweetnacl.txt", "text/plain; charset=utf-8"),
}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
       "connect-src 'self'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'")
ADDRESS_RE = re.compile(r"^[0-9a-f]{32}$")
MAX_BODY = 4096


class Upstream:
    def __init__(self, node, faucet, timeout=15):
        self.node, self.faucet, self.timeout = node.rstrip("/"), (faucet or "").rstrip("/"), timeout

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


def _error_text(body, fallback):
    err = body.get("error") if isinstance(body, dict) else None
    return str(err)[:200] if isinstance(err, str) else fallback


def make_handler(up, trust_proxy=False):
    class Handler(BaseHTTPRequestHandler):
        server_version = "NetlithicWebWallet"
        sys_version = ""

        def log_message(self, fmt, *args):
            pass

        def client_ip(self):
            if trust_proxy:
                fwd = self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
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
            except Exception:
                return self.json(502, {"error": "The network didn't answer. Try again in a minute."})
            return self.json(404, {"error": "Not found"})

        def api_info(self):
            code, st = up.call(up.node + "/status")
            cid = wallet_mod.current_chain_id()
            if code != 200 or st.get("chain_id") != cid:
                return self.json(503, {"error": "The test network is on a different version right now. Try again later."})
            return self.json(200, {"chain_id": cid, "min_fee": wallet_mod.current_min_fee(),
                                   "height": st.get("height"), "testnet": True})

        def api_account(self, addr):
            code, acct = up.call(up.node + "/account?address=" + addr)
            if code != 200:
                return self.json(502, {"error": "The network didn't answer. Try again in a minute."})
            points = "0"
            c2, contrib = up.call(up.node + "/contributions")
            if c2 == 200:
                for row in contrib.get("contributors", []):
                    if isinstance(row, dict) and row.get("address") == addr:
                        points = str(row.get("cp", "0"))[:20]
            return self.json(200, {"address": addr, "balance": int(acct.get("balance", 0)),
                                   "next_nonce": int(acct.get("next_nonce", 1)), "points": points})

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
                    code, res = up.call(up.node + "/receive_tx", {"tx": tx})
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
    p.add_argument("--node", default=os.environ.get("NETLITHIC_WALLET_NODE", "http://127.0.0.1:8000"))
    p.add_argument("--faucet", default=os.environ.get("NETLITHIC_WALLET_FAUCET", "http://127.0.0.1:8090"))
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8082)
    p.add_argument("--trust-proxy", action="store_true",
                   help="use X-Forwarded-For (only behind your own TLS reverse proxy)")
    a = p.parse_args(argv)
    print(f"Web wallet on http://{a.host}:{a.port} (node {a.node}, faucet {a.faucet or 'off'})")
    ThreadingHTTPServer((a.host, a.port), make_handler(Upstream(a.node, a.faucet), a.trust_proxy)).serve_forever()


if __name__ == "__main__":
    main()
