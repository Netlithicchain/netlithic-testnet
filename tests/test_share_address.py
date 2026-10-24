"""One-tap address sharing in the web wallet: /send?to= pre-fill, Share button, QR code."""
import json
import os
import re
import shutil
import subprocess
import threading
from http.server import ThreadingHTTPServer
from urllib.request import urlopen

import pytest

import webwallet
from tests.test_webwallet import fake_upstream

STATIC = webwallet.STATIC
NODE = shutil.which("node")


def read(name):
    return open(os.path.join(STATIC, name), encoding="utf-8").read()


def serve(share_base=""):
    up = fake_upstream()
    url = f"http://127.0.0.1:{up.server_address[1]}"
    s = ThreadingHTTPServer(("127.0.0.1", 0), webwallet.make_handler(webwallet.Upstream(url, url), False, "", share_base))
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s, up, f"http://127.0.0.1:{s.server_address[1]}"


def test_send_route_and_qr_script_served():
    s, up, base = serve()
    try:
        with urlopen(base + "/send?to=" + "ab" * 16) as r:
            body = r.read().decode()
            assert r.status == 200 and "Netlithic Wallet" in body and 'id="b-share"' in body
            assert "script-src 'self'" in r.headers["Content-Security-Policy"]
        with urlopen(base + "/qrcode.js") as r:
            assert r.status == 200 and b"qrcode" in r.read()
        assert json.loads(urlopen(base + "/api/info").read())["share_base"] == ""
    finally:
        s.shutdown(); up.shutdown()


def test_share_base_configured():
    s, up, base = serve("https://testnet.netlithicchain.com/")
    try:
        assert json.loads(urlopen(base + "/api/info").read())["share_base"] == "https://testnet.netlithicchain.com"
    finally:
        s.shutdown(); up.shutdown()


@pytest.mark.parametrize("bad", ["http://x.com", "javascript:alert(1)", "https://x.com/<b>", "//x.com", "ftp://x"])
def test_share_base_rejects_bad(bad):
    with pytest.raises(ValueError):
        webwallet.normalize_share_base(bad)


def test_desktop_uses_public_site_for_share_links():
    src = open(os.path.join(webwallet.ROOT, "desktop", "netlithic_desktop.py"), encoding="utf-8").read()
    assert "share_base=PUBLIC_SITE" in src and '"/send"' in src


def test_share_ui_and_wording():
    html, js = read("index.html"), read("wallet.js")
    for i in ('id="b-copy"', 'id="b-share"', 'id="b-qr"', 'id="h-qr"', 'id="s-prefill"'):
        assert i in html
    assert '<script src="/qrcode.js"></script>' in html
    assert "Send me test NLT on Netlithic (test coins have no value): " in js
    assert "navigator.share" in js and "Link copied" in js
    assert not re.search(r"https?://", html + js)  # no external URLs / CDNs
    for bad in ("price", "invest", "profit"):
        assert bad not in (html + js).lower()


def test_prefill_validates_and_never_sends():
    js = read("wallet.js")
    m = re.search(r"var shareTo = \(function \(\) \{(.*?)\}\)\(\);", js, re.S)
    assert m and "ADDR_RE.test(to)" in m.group(1)
    apply_fn = re.search(r"function applyShareTo\(\) \{(.*?)\n  \}", js, re.S).group(1)
    for bad in ("api(", "pending", "b-send", "b-review", "signTransfer", ".click("):
        assert bad not in apply_fn
    assert '$("s-amount").value = ""' in apply_fn  # never pre-fills an amount


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_vendored_qr_encodes_share_link():
    code = ("const q = require('./qrcode.js')(0, 'M'); q.addData('https://testnet.netlithicchain.com/send?to=" + "ab" * 16
            + "'); q.make(); console.log(JSON.stringify({n: q.getModuleCount(), d: q.isDark(0, 0)}));")
    out = subprocess.run([NODE, "-e", code], cwd=STATIC, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout)
    assert r["n"] >= 21 and r["d"] is True  # finder pattern corner is dark
