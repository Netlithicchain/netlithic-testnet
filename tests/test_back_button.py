"""Browser Back/Forward stays inside the web wallet (History API)."""
import json
import os
import re
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import wallet as wallet_mod
import webwallet

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webwallet", "static")
JS = open(os.path.join(STATIC, "wallet.js"), encoding="utf-8").read()


def test_views_push_history_entries():
    assert "history[replace ? \"replaceState\" : \"pushState\"]" in JS
    assert 'addEventListener("popstate", onPop)' in JS
    for sub in ('openSub("review")', 'openSub("qr")', 'closeSub("review")', 'closeSub("qr")', '"help:" + i'):
        assert sub in JS
    # the /send?to= cleanup must keep our state, not wipe it
    assert 'history.replaceState(null' not in JS
    assert 'history.replaceState(history.state, "", "/")' in JS


def test_back_never_locks_or_resends():
    on_pop = JS[JS.index("function onPop"):JS.index("function startNav")]
    assert "lockNow" not in on_pop and "api(" not in on_pop and "b-send" not in on_pop
    allowed = JS[JS.index("function allowedView"):JS.index("function onPop")]
    assert 'if (key) return v === "v-home" || v === "v-save" ? v : "v-home";' in allowed
    apply = JS[JS.index("function applySub"):JS.index("function allowedView")]
    assert "pending = null" in apply and "s-amount" not in apply and "s-to" not in apply


POSTS = []


def _fake_node():
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_POST(self):
            POSTS.append(self.path); self.send_response(500); self.end_headers()
        def do_GET(self):
            p = self.path
            if p == "/status": obj = {"chain_id": wallet_mod.current_chain_id(), "height": 5}
            elif p.startswith("/account"): obj = {"balance": 100 * 10**8, "next_nonce": 1}
            elif p == "/mempool": obj = {"mempool": []}
            elif p.startswith("/history"): obj = {"items": [], "next_offset": None, "total": 0}
            elif p == "/contributions": obj = {"contributors": []}
            else: obj = {}
            b = json.dumps(obj).encode(); self.send_response(200)
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    s = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


def _browser():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, None
    chrome = shutil.which("google-chrome") or "/usr/bin/google-chrome"
    if not os.path.exists(chrome):
        return None, None
    return sync_playwright, chrome


def test_headless_back_button():
    sp, chrome = _browser()
    if sp is None:
        pytest.skip("playwright or chrome not installed")
    up = _fake_node()
    url = f"http://127.0.0.1:{up.server_address[1]}"
    srv = ThreadingHTTPServer(("127.0.0.1", 0), webwallet.make_handler(webwallet.Upstream(url, url)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    friend = "3f9a" * 8
    vis = lambda pg, sel: pg.is_visible(sel)
    try:
        with sp() as p:
            br = p.chromium.launch(executable_path=chrome, args=["--no-sandbox"])
            pg = br.new_page()
            pg.goto("about:blank")
            pg.goto(base + "/send?to=" + friend)
            pg.click("#b-create")
            pg.fill("#c-pass", "correct horse battery"); pg.fill("#c-pass2", "correct horse battery")
            pg.click("#b-create-go")
            pg.wait_for_selector("#v-backup", state="visible")
            pg.check("#c-wrote"); pg.click("#b-backup-done")
            pg.wait_for_selector("#v-home", state="visible")
            # share-link prefill still works
            assert pg.input_value("#s-to") == friend
            pg.fill("#s-amount", "2")
            pg.click("#b-review")
            try:
                pg.wait_for_selector("#s-confirm", state="visible", timeout=8000)
            except Exception:
                raise AssertionError("review did not open: " + pg.text_content("#msg"))
            # Back from review -> form, values kept, nothing sent
            pg.go_back(); pg.wait_for_selector("#s-confirm", state="hidden")
            assert vis(pg, "#v-home") and pg.input_value("#s-to") == friend and pg.input_value("#s-amount") == "2"
            # Forward lands on the form (review needs a fresh sign), still nothing sent
            pg.go_forward(); pg.wait_for_timeout(300)
            assert vis(pg, "#v-home") and not vis(pg, "#s-confirm")
            # QR step
            pg.click("#b-qr"); pg.wait_for_selector("#h-qr-box", state="visible")
            pg.go_back(); pg.wait_for_selector("#h-qr-box", state="hidden")
            # Help section
            pg.click("details >> nth=0 >> summary"); pg.wait_for_timeout(200)
            assert pg.eval_on_selector("details", "d => d.open")
            pg.go_back(); pg.wait_for_timeout(300)
            assert vis(pg, "#v-home") and not pg.eval_on_selector("details", "d => d.open")
            # Back from the main view stays on the wallet and stays unlocked, as many times as there are entries
            for _ in range(6):
                pg.go_back(); pg.wait_for_timeout(250)
                if not pg.url.startswith(base):
                    break
                assert vis(pg, "#v-home"), "Back from the main view must not lock or leave"
            # It only leaves when Back is pressed again from the very first entry
            assert pg.url.startswith(base) or pg.url == "about:blank"
            left = False
            for _ in range(4):
                if not pg.url.startswith(base):
                    left = True; break
                pg.go_back(); pg.wait_for_timeout(250)
            assert left and pg.url == "about:blank"
            assert POSTS == [], "nothing may be sent to the network"
            br.close()
    finally:
        srv.shutdown(); up.shutdown()
