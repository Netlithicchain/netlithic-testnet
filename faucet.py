"""Netlithic TESTNET faucet: hands out a small fixed amount of test NLT.

    NETLITHIC_FAUCET_KEY_FILE=/etc/netlithic/faucet_wallet.json \
        python faucet.py --node http://127.0.0.1:8000 --port 8090

* Test coins have NO value. The faucet refuses to run (and refuses every
  request) unless the node's chain_id starts with "netlithic-dev".
* The key file is a normal wallets.json holding exactly one wallet. It must be
  owner-only (0600) on Linux/macOS. Its private key is never printed or logged.
* Limits: one drip per address and per IP every --cooldown-hours (24), and at
  most --daily-cap-nlt (500) per rolling 24 hours in total.
* GET / is an HTML form; POST /api/drip {"address": "..."} is the JSON API;
  GET /api/info shows the faucet address, balance and limits.
"""
import argparse
import html
import json
import os
import stat
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import economics  # noqa: E402
import wallet as wallet_mod  # noqa: E402

KEY_FILE_ENV = "NETLITHIC_FAUCET_KEY_FILE"
DAY = 24 * 3600
MAX_BODY = 4096


class FaucetError(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


def load_faucet_wallet(path):
    """Load the single wallet from a 0600 key file. Errors never include key material."""
    if not path:
        raise FaucetError(f"Set {KEY_FILE_ENV} to the faucet wallet file", 500)
    if not os.path.isfile(path):
        raise FaucetError("Faucet key file not found", 500)
    if os.name != "nt":
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if mode & 0o077:
            raise FaucetError(f"Faucet key file must be owner-only (chmod 600), it is {oct(mode)}", 500)
    old = wallet_mod.WALLET_FILE
    try:
        wallet_mod.WALLET_FILE = path
        wallets = wallet_mod.Wallet._load_wallets()
    finally:
        wallet_mod.WALLET_FILE = old
    if len(wallets) != 1:
        raise FaucetError("Faucet key file must hold exactly one valid wallet", 500)
    w = next(iter(wallets.values()))
    return wallet_mod.Wallet(w["name"], w["private_key"], w["pubkey"])


class NodeClient:
    def __init__(self, url):
        self.url = url.rstrip("/")

    def _req(self, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        req = Request(self.url + path, data=data, method="GET" if data is None else "POST",
                      headers={"Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode() or "{}")
        except HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode() or "{}")
            except Exception:
                return e.code, {}

    def status(self):
        return self._req("/status")[1]

    def account(self, address):
        return self._req(f"/account?address={address}")[1]

    def submit(self, tx):
        return self._req("/submit_tx", {"tx": tx})


class RateLimiter:
    """Per-address, per-IP cooldowns and a rolling daily total, saved to a JSON file."""

    def __init__(self, path=None, cooldown=DAY, daily_cap=0, clock=time.time):
        self.path, self.cooldown, self.daily_cap, self.clock = path, cooldown, daily_cap, clock
        self.addr, self.ip, self.drips = {}, {}, []  # drips: [ts, amount]
        self.lock = threading.Lock()
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    d = json.load(f)
                self.addr, self.ip, self.drips = d.get("addr", {}), d.get("ip", {}), d.get("drips", [])
            except Exception:
                pass

    def _prune(self, now):
        self.addr = {k: v for k, v in self.addr.items() if now - v < self.cooldown}
        self.ip = {k: v for k, v in self.ip.items() if now - v < self.cooldown}
        self.drips = [d for d in self.drips if now - d[0] < DAY]

    def check(self, address, ip, amount):
        now = self.clock()
        self._prune(now)
        if address in self.addr:
            raise FaucetError(f"This address already got test NLT; try again in "
                              f"{_hours(self.cooldown - (now - self.addr[address]))}", 429)
        if ip in self.ip:
            raise FaucetError(f"Your IP already got test NLT; try again in "
                              f"{_hours(self.cooldown - (now - self.ip[ip]))}", 429)
        if self.daily_cap and sum(d[1] for d in self.drips) + amount > self.daily_cap:
            raise FaucetError("The faucet hit its daily limit; try again tomorrow", 429)

    def record(self, address, ip, amount):
        now = self.clock()
        self.addr[address], self.ip[ip] = now, now
        self.drips.append([now, amount])
        if self.path:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"addr": self.addr, "ip": self.ip, "drips": self.drips}, f)
            os.replace(tmp, self.path)


def _hours(seconds):
    h = max(1, int(seconds // 3600 + (1 if seconds % 3600 else 0)))
    return f"{h} hour" + ("s" if h != 1 else "")


class Faucet:
    def __init__(self, wallet, node, limiter, amount, fee=None):
        self.wallet, self.node, self.limiter = wallet, node, limiter
        self.amount = int(amount)
        self.fee = wallet_mod.current_min_fee() if fee is None else int(fee)

    def chain_id(self):
        cid = self.node.status().get("chain_id")
        if not isinstance(cid, str) or not economics.is_dev_chain(cid):
            raise FaucetError("Faucet is testnet-only and this node is not on a test chain", 503)
        if cid != wallet_mod.current_chain_id():
            raise FaucetError("Node chain_id does not match this faucet's config", 503)
        return cid

    def drip(self, address, ip):
        try:
            address = wallet_mod.normalize_address(str(address or ""))
        except Exception:
            raise FaucetError("Enter a valid Netlithic address (32 hex characters)")
        if address == self.wallet.address:
            raise FaucetError("That's the faucet's own address")
        cid = self.chain_id()
        with self.limiter.lock:
            self.limiter.check(address, ip, self.amount)
            acct = self.node.account(self.wallet.address)
            if int(acct.get("balance", 0)) < self.amount + self.fee:
                raise FaucetError("The faucet is empty right now; please try later", 503)
            tx = self.wallet.create_transaction(address, self.amount, int(acct["next_nonce"]),
                                                fee=self.fee, chain_id=cid)
            code, body = self.node.submit(tx)
            if code != 200:
                raise FaucetError("The node did not accept the transfer; try again later", 502)
            self.limiter.record(address, ip, self.amount)
        return {"ok": True, "address": address, "amount_nlt": economics.to_nlt(self.amount),
                "tx_id": body.get("tx_id"),
                "note": "Test NLT has no value. It arrives when the next block is mined."}

    def info(self):
        acct = self.node.account(self.wallet.address)
        return {"faucet_address": self.wallet.address,
                "balance_nlt": economics.to_nlt(int(acct.get("balance", 0))),
                "amount_nlt": economics.to_nlt(self.amount),
                "cooldown_hours": self.limiter.cooldown / 3600,
                "daily_cap_nlt": economics.to_nlt(self.limiter.daily_cap),
                "testnet": True, "note": "Test NLT has no value."}


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Netlithic Testnet Faucet</title><style>
body{{margin:0;background:#0a1430;color:#e6ecff;font:16px/1.5 system-ui,sans-serif}}
.banner{{background:#f5b400;color:#1a1300;text-align:center;padding:8px;font-weight:600}}
main{{max-width:560px;margin:40px auto;padding:0 16px}}
.card{{background:#111f47;border:1px solid #23397a;border-radius:12px;padding:24px}}
h1{{color:#7fd3ff;margin-top:0}} input{{width:100%;box-sizing:border-box;padding:10px;border-radius:8px;
border:1px solid #2f4b9a;background:#0a1430;color:#e6ecff;font-family:monospace}}
button{{margin-top:12px;padding:10px 18px;border:0;border-radius:8px;background:#2f6bff;color:#fff;font-weight:600}}
.msg{{margin-top:16px;padding:12px;border-radius:8px;background:#0d2a5c}} .err{{background:#5c1020}}
small{{color:#9fb0dd}} code{{color:#7fd3ff}}</style></head><body>
<div class="banner">TESTNET &mdash; test coins have no value and may be wiped at any reset</div>
<main><div class="card"><h1>Netlithic testnet faucet</h1>
<p>Get {amount} test NLT to try transfers. One drip per address and per IP every {hours} hours.</p>
<form method="post" action="/"><label for="a">Your address</label>
<input id="a" name="address" maxlength="64" placeholder="32 hex characters" value="{value}" required>
<button type="submit">Send me test NLT</button></form>{message}
<p><small>API: <code>POST /api/drip {{"address": "..."}}</code></small></p></div></main></body></html>"""


def render_page(faucet, message="", error=False, value=""):
    box = ""
    if message:
        box = f'<div class="msg{" err" if error else ""}">{html.escape(message)}</div>'
    return PAGE.format(amount=html.escape(economics.to_nlt(faucet.amount)),
                       hours=html.escape(f"{faucet.limiter.cooldown / 3600:g}"),
                       value=html.escape(value, quote=True), message=box)


def make_handler(faucet, trust_proxy=False):
    class Handler(BaseHTTPRequestHandler):
        server_version = "NetlithicFaucet"

        def log_message(self, fmt, *args):
            return

        def client_ip(self):
            if trust_proxy:
                fwd = self.headers.get("X-Forwarded-For", "")
                if fwd.strip():
                    return fwd.split(",")[-1].strip()
            return self.client_address[0]

        def send(self, code, body, ctype):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'")
            self.end_headers()
            self.wfile.write(data)

        def json(self, code, obj):
            self.send(code, json.dumps(obj), "application/json")

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/":
                return self.send(200, render_page(faucet), "text/html; charset=utf-8")
            if path == "/health":
                return self.json(200, {"ok": True})
            if path == "/api/info":
                try:
                    faucet.chain_id()
                    return self.json(200, faucet.info())
                except FaucetError as e:
                    return self.json(e.status, {"error": str(e)})
                except Exception:
                    return self.json(502, {"error": "Node unreachable"})
            return self.json(404, {"error": "Not found"})

        def do_POST(self):
            path = urlparse(self.path).path
            if path not in ("/", "/api/drip"):
                return self.json(404, {"error": "Not found"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n < 0 or n > MAX_BODY:
                return self.json(413, {"error": "Request too large"})
            raw = self.rfile.read(n).decode("utf-8", "replace")
            if path == "/api/drip":
                try:
                    address = json.loads(raw or "{}").get("address")
                except Exception:
                    return self.json(400, {"error": "Invalid JSON"})
            else:
                address = (parse_qs(raw).get("address") or [""])[0]
            try:
                result = faucet.drip(address, self.client_ip())
                code, msg, err = 200, f"Sent {result['amount_nlt']} test NLT. {result['note']}", False
            except FaucetError as e:
                result, code, msg, err = {"error": str(e)}, e.status, str(e), True
            except Exception:
                result, code, msg, err = {"error": "Faucet error"}, 502, "Faucet error; try later", True
            if path == "/api/drip":
                return self.json(code, result)
            return self.send(code, render_page(faucet, msg, err, str(address or "")[:64]),
                             "text/html; charset=utf-8")

    return Handler


def main(argv=None):
    p = argparse.ArgumentParser(description="Netlithic TESTNET faucet (test coins have no value)")
    p.add_argument("--node", default=os.environ.get("NETLITHIC_FAUCET_NODE", "http://127.0.0.1:8000"))
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8090)
    p.add_argument("--amount-nlt", default="10")
    p.add_argument("--cooldown-hours", type=float, default=24)
    p.add_argument("--daily-cap-nlt", default="500")
    p.add_argument("--state-file", default="faucet_state.json")
    p.add_argument("--trust-proxy", action="store_true",
                   help="use X-Forwarded-For (only behind your own reverse proxy)")
    args = p.parse_args(argv)
    try:
        w = load_faucet_wallet(os.environ.get(KEY_FILE_ENV))
    except FaucetError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    limiter = RateLimiter(args.state_file, int(args.cooldown_hours * 3600),
                          economics.parse_nlt(args.daily_cap_nlt))
    faucet = Faucet(w, NodeClient(args.node), limiter, economics.parse_nlt(args.amount_nlt))
    try:
        cid = faucet.chain_id()
    except FaucetError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception:
        print(f"Error: cannot reach node {args.node}", file=sys.stderr)
        return 1
    print(f"Netlithic TESTNET faucet on {cid}; faucet address {w.address}")
    print(f"Listening on http://{args.host}:{args.port} (test coins have no value)")
    ThreadingHTTPServer((args.host, args.port), make_handler(faucet, args.trust_proxy)).serve_forever()


if __name__ == "__main__":
    sys.exit(main())
