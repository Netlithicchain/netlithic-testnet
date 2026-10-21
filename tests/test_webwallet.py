"""Web wallet: JS signing must match wallet.py byte-for-byte; proxy server checks."""
import json, os, re, shutil, subprocess, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import wallet as W
import webwallet

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "webwallet", "static")
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(not NODE, reason="node.js not installed")

SEEDS = ["00" * 32, "11" * 32, "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
         "c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7"]


def run_js(code):
    out = subprocess.run([NODE, "-e", code], cwd=STATIC, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def py_sign(seed, payload):
    sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed))
    pub = W.pubkey_from_private(seed)
    return pub, sk.sign(W.signing_message(payload, pub)).hex()


@needs_node
def test_js_signatures_match_python_vectors():
    cases = []
    for i, seed in enumerate(SEEDS):
        cases.append({"seed": seed, "recipient": ("ab" * 16) if i % 2 else ("0f" * 16),
                      "amount": [1, 150000000, 999999999999, 10000][i], "nonce": i + 1,
                      "fee": 10000, "timestamp": [1761000000.123, 1700000000.5, 1761076543.001, 1761000000.999][i],
                      "chain_id": "netlithic-dev-1"})
    js = """
    const C = require('./wallet-core.js'); const cases = %s;
    (async () => { const out = [];
      for (const c of cases) { const k = C.keyFromSeed(c.seed);
        const tx = await C.signTransfer(k, c);
        out.push({tx, canon: C.canonical({payload: tx.payload, pubkey: tx.pubkey}), addr: await C.addressOf(k.pubkey)}); }
      console.log(JSON.stringify(out)); })();
    """ % json.dumps(cases)
    for case, res in zip(cases, run_js(js)):
        tx = res["tx"]
        pub = W.pubkey_from_private(case["seed"])
        assert tx["pubkey"] == pub
        assert res["addr"] == W.pubkey_to_address(pub) == tx["payload"]["sender"]
        assert res["canon"].encode() == W.signing_message(tx["payload"], pub)
        assert py_sign(case["seed"], tx["payload"]) == (pub, tx["sig"])  # Ed25519 is deterministic
        # Round-trip through JSON exactly as the node receives it.
        assert W.verify_signed_tx(json.loads(json.dumps(tx)), chain_id="netlithic-dev-1")
        tampered = json.loads(json.dumps(tx)); tampered["payload"]["amount"] += 1
        assert not W.verify_signed_tx(tampered, chain_id="netlithic-dev-1")


@needs_node
def test_js_live_timestamp_and_node_normalization():
    seed = SEEDS[2]
    js = """const C=require('./wallet-core.js');(async()=>{const k=C.keyFromSeed('%s');const out=[];
      for (const ms of [Date.now(), 1761000000000, 1761000000010]) out.push(await C.signTransfer(k,
       {recipient:'ab'.repeat(16),amount:5,nonce:3,fee:10000,chain_id:'netlithic-dev-1',timestamp:C.txTimestamp(ms)}));
      console.log(JSON.stringify(out));})();""" % seed
    from core import normalize_signed_tx, tx_id
    for tx in run_js(js):
        assert W.verify_signed_tx(tx, chain_id="netlithic-dev-1")
        assert tx_id(normalize_signed_tx(tx))


@needs_node
def test_js_backup_code_amounts_and_validation():
    js = """const C=require('./wallet-core.js');(async()=>{const r={};const s='%s';
      r.code=await C.backupCode(s); r.back=await C.seedFromBackupCode(r.code.toUpperCase());
      try{await C.seedFromBackupCode(r.code.slice(0,-1)+(r.code.endsWith('0')?'1':'0'));r.typo='accepted'}catch(e){r.typo=e.message}
      r.units=[C.toBaseUnits('1'),C.toBaseUnits('2.5'),C.toBaseUnits('0.00000001')];
      r.fmt=[C.fromBaseUnits(150000000),C.fromBaseUnits(4999990000),C.fromBaseUnits(1)];
      r.bad=[];for(const a of ['0','-1','1e5','abc','1.123456789','<b>1</b>']){try{C.toBaseUnits(a);r.bad.push('ok')}catch(e){r.bad.push('no')}}
      const k=C.keyFromSeed(s);try{await C.signTransfer(k,{recipient:'<script>',amount:1,nonce:1,fee:0,chain_id:'x'});r.rcp='ok'}catch(e){r.rcp='no'}
      console.log(JSON.stringify(r));})();""" % SEEDS[3]
    r = run_js(js)
    assert re.fullmatch(r"([0-9a-f]{4}-){16}[0-9a-f]{4}", r["code"])
    assert r["back"] == SEEDS[3]
    assert "typo" in r["typo"]
    assert r["units"] == [100000000, 250000000, 1]
    assert r["fmt"] == ["1.5", "49.9999", "0.00000001"]
    assert r["bad"] == ["no"] * 6
    assert r["rcp"] == "no"


def test_static_assets_have_no_unsafe_dom_or_external_urls():
    js = open(os.path.join(STATIC, "wallet.js")).read() + open(os.path.join(STATIC, "wallet-core.js")).read()
    for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert bad not in js
    html = open(os.path.join(STATIC, "index.html")).read()
    assert "<script>" not in html and "onclick" not in html.lower() and "style=" not in html
    assert not re.search(r"https?://", html + js)
    assert "localStorage.setItem(STORE, JSON.stringify(" in js  # only the encrypted blob is stored
    assert "coins have no value" in html


# ---- proxy server -------------------------------------------------------

class Fake:
    seen = []


def fake_upstream():
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def reply(self, code, obj):
            b = json.dumps(obj).encode(); self.send_response(code)
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        def do_GET(self):
            if self.path == "/status":
                return self.reply(200, {"chain_id": W.current_chain_id(), "height": 9})
            if self.path.startswith("/account"):
                return self.reply(200, {"balance": 4999990000, "next_nonce": 2})
            if self.path == "/contributions":
                return self.reply(200, {"contributors": [{"address": "ab" * 16, "cp": "<b>6</b>"}]})
            self.reply(404, {})
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            Fake.seen.append((self.path, body, self.headers.get("X-Forwarded-For")))
            if self.path == "/receive_tx":
                return self.reply(200, {"tx_id": "f" * 64})
            if self.path == "/api/drip":
                if body["address"] == "cd" * 16:
                    return self.reply(429, {"error": "Already claimed today"})
                return self.reply(200, {"amount_nlt": "10"})
            self.reply(404, {})
    s = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


@pytest.fixture
def ww():
    up = fake_upstream()
    url = f"http://127.0.0.1:{up.server_address[1]}"
    s = ThreadingHTTPServer(("127.0.0.1", 0), webwallet.make_handler(webwallet.Upstream(url, url)))
    threading.Thread(target=s.serve_forever, daemon=True).start()
    Fake.seen.clear()
    yield f"http://127.0.0.1:{s.server_address[1]}"
    s.shutdown(); up.shutdown()


def req(url, body=None, raw=None):
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    r = Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urlopen(r, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_static_files_and_security_headers(ww):
    code, h, body = req(ww + "/")
    assert code == 200 and b"Netlithic Wallet" in body
    assert "script-src 'self'" in h["Content-Security-Policy"] and "unsafe" not in h["Content-Security-Policy"]
    assert h["X-Frame-Options"] == "DENY" and h["X-Content-Type-Options"] == "nosniff"
    for p in ("/wallet.js", "/wallet-core.js", "/nacl-fast.min.js", "/wallet.css", "/logo.png"):
        assert req(ww + p)[0] == 200
    assert req(ww + "/../webwallet.py")[0] == 404
    assert req(ww + "/wallets.json")[0] == 404


def test_info_and_account(ww):
    code, _, b = req(ww + "/api/info")
    info = json.loads(b)
    assert code == 200 and info["chain_id"] == W.current_chain_id() and info["min_fee"] == W.current_min_fee()
    code, _, b = req(ww + "/api/account?address=" + "AB" * 16)
    acct = json.loads(b)
    assert acct == {"address": "ab" * 16, "balance": 4999990000, "next_nonce": 2, "points": "<b>6</b>"}
    assert req(ww + "/api/account?address=<script>")[0] == 400


def test_send_only_forwards_valid_signed_transfers(ww, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = W.Wallet.create("a")
    tx = a.create_transaction("ab" * 16, 5, 1)
    code, _, b = req(ww + "/api/send", {"tx": tx})
    assert code == 200 and json.loads(b)["tx_id"] == "f" * 64
    assert Fake.seen[-1][0] == "/receive_tx" and Fake.seen[-1][1] == {"tx": tx}
    bad = json.loads(json.dumps(tx)); bad["payload"]["amount"] = 6
    n = len(Fake.seen)
    assert req(ww + "/api/send", {"tx": bad})[0] == 400
    other = a.create_transaction("ab" * 16, 5, 1, chain_id="some-other-chain")
    assert req(ww + "/api/send", {"tx": other})[0] == 400
    assert req(ww + "/api/send", raw=b"x" * 5000)[0] == 400
    assert req(ww + "/api/send", raw=b"not json")[0] == 400
    assert len(Fake.seen) == n  # nothing bad reached the node


def test_drip_validates_and_passes_errors(ww):
    code, _, b = req(ww + "/api/drip", {"address": "ab" * 16})
    assert code == 200 and json.loads(b)["amount_nlt"] == "10"
    assert Fake.seen[-1][2] == "127.0.0.1"
    code, _, b = req(ww + "/api/drip", {"address": "cd" * 16})
    assert code == 429 and json.loads(b)["error"] == "Already claimed today"
    assert req(ww + "/api/drip", {"address": "nope"})[0] == 400
    assert req(ww + "/api/mine", {})[0] == 404
    assert req(ww + "/api/add_peer", {})[0] == 404
