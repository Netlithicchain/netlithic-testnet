"""Desktop app: page building, earning control, explorer redirect, no key handling."""
import json, os, sys, threading
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "desktop"))
import netlithic_desktop as D  # noqa: E402
import webwallet  # noqa: E402

ADDR = "ab" * 16


class FakeNet:
    def __init__(self, local=10, network=10, mine_ok=True):
        self.local, self.network, self.mine_ok = local, network, mine_ok
        self.calls = []

    def __call__(self, url, payload=None, headers=None):
        self.calls.append((url, payload, dict(headers or {})))
        if url.endswith("/status"):
            h = self.local if "local" in url else self.network
            return (200, {"height": h}) if h is not None else (502, {})
        if url.endswith("/mine"):
            return (200, {}) if self.mine_ok else (500, {"error": "stale"})
        return 404, {}


def earner(net, **kw):
    return D.Earner("http://local", "http://seed", net, admin_token="t" * 10, pace=kw.get("pace", 0.01))


def test_page_is_web_wallet_plus_earning_card():
    with open(os.path.join(webwallet.STATIC, "index.html"), encoding="utf-8") as f:
        page = D.build_index(f.read())
    for s in ('id="e-toggle"', "Start helping", "Test coins have no value", "/desktop.js", "/desktop.css", "TEST NETWORK",
              "Write down your backup code", "Bring back my wallet", "kept only on this computer"):
        assert s in page
    assert page.index('id="e-toggle"') < page.index('id="h-balance"') or 'id="v-home"' in page
    with pytest.raises(RuntimeError):
        D.build_index("<html></html>")


def test_sync_words():
    assert "up to date" in earner(FakeNet(10, 10)).status()["sync"]
    s = earner(FakeNet(3, 10)).status()
    assert not s["synced"] and "block 3 of 10" in s["sync"]
    assert "Can't reach" in earner(FakeNet(10, None)).status()["sync"]
    assert "Starting" in earner(FakeNet(None, 10)).status()["sync"]


def test_start_needs_sync_and_valid_address():
    with pytest.raises(ValueError):
        earner(FakeNet(3, 10)).start(ADDR)
    with pytest.raises(ValueError):
        earner(FakeNet()).start("not-an-address")


def test_mining_loop_counts_blocks_and_stops():
    net = FakeNet()
    e = earner(net)
    e.start(ADDR)
    import time
    deadline = time.time() + 5
    while e.blocks_found < 2 and time.time() < deadline:
        time.sleep(0.02)
    e.stop()
    e.thread.join(2)
    assert e.blocks_found >= 2 and not e.running
    mines = [c for c in net.calls if c[0].endswith("/mine")]
    assert mines[0][1] == {"miner_address": ADDR}
    assert mines[0][2]["X-Admin-Token"] == "t" * 10
    assert all(c[0].startswith("http://local") for c in mines)  # only ever our own node


def test_stale_block_is_not_counted():
    e = earner(FakeNet(mine_ok=False))
    e.address = ADDR
    assert e.mine_once() is False and e.blocks_found == 0 and "first" in e.last_error


def test_upstream_never_forwards_client_ip(monkeypatch):
    seen = {}
    def fake(self, url, payload=None, headers=None):
        seen.update(headers or {})
        return 200, {}
    monkeypatch.setattr(webwallet.Upstream, "call", fake)
    D.DesktopUpstream("http://n", "http://f").call("http://f/api/drip", {}, {"X-Forwarded-For": "1.2.3.4", "A": "b"})
    assert "X-Forwarded-For" not in seen and seen["A"] == "b"


def test_node_command_targets_local_only():
    cmd = D.node_command("/tmp/x", 47101, "http://seed:8000")
    assert "--run-node" in cmd and cmd[cmd.index("--host") + 1] == "127.0.0.1"
    assert cmd[cmd.index("--peer") + 1] == "http://seed:8000"


@pytest.fixture
def app():
    net = FakeNet()
    e = earner(net)
    up = webwallet.Upstream("http://local", "")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), D.make_desktop_handler(up, e))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", e
    e.stop(); srv.shutdown()


class NoRedirect(__import__("urllib.request").request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def req(url, body=None, headers=None):
    from urllib.request import build_opener
    data = None if body is None else json.dumps(body).encode()
    r = Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with build_opener(NoRedirect).open(r, timeout=5) as resp:
            return resp.status, resp.headers, resp.read()
    except HTTPError as err:
        return err.code, err.headers, err.read()


def test_server_pages_and_earning(app):
    base, e = app
    code, h, body = req(base + "/")
    assert code == 200 and b"Start helping" in body and "script-src 'self'" in h["Content-Security-Policy"]
    assert req(base + "/desktop.js")[0] == 200 and req(base + "/wallet.js")[0] == 200
    code, _, body = req(base + "/api/earn/status")
    assert code == 200 and json.loads(body)["synced"] is True
    code, _, body = req(base + "/api/earn/start", {"address": ADDR})
    assert code == 200 and json.loads(body)["earning"] is True
    code, _, body = req(base + "/api/earn/stop", {})
    assert code == 200 and json.loads(body)["earning"] is False


def test_other_websites_cannot_control_earning(app):
    base, e = app
    code, _, _ = req(base + "/api/earn/start", {"address": ADDR}, {"Origin": "https://evil.example"})
    assert code == 403 and not e.running


def test_explorer_redirects_to_public_site(app):
    base, _ = app
    code, h, _ = req(base + "/explorer/address/" + ADDR)
    assert code == 302 and h["Location"] == D.PUBLIC_SITE + "/explorer/address/" + ADDR
    assert req(base + "/explorer/<script>")[0] == 404


def test_desktop_code_never_touches_wallet_files():
    for name in ("netlithic_desktop.py", "static/desktop.js"):
        src = open(os.path.join(ROOT, "desktop", name), encoding="utf-8").read()
        for bad in ("wallets.json", "genesis_wallets", "private_key", "localStorage"):
            assert bad not in src, (name, bad)


GOOD = {"format": "netlithic-wallet-backup", "version": 1, "address": ADDR, "kdf": "PBKDF2-SHA256",
        "iterations": 600000, "salt": "00" * 16, "iv": "11" * 12, "ciphertext": "ab" * 80}


def test_save_backup_writes_only_encrypted_backup(tmp_path):
    asked = []
    target = tmp_path / "b.json"
    api = D.WindowApi(ask_save_path=lambda n: asked.append(n) or str(target))
    text = json.dumps(GOOD)
    r = api.save_backup(text, "netlithic-wallet-backup-abababab.json")
    assert r == {"ok": True, "path": str(target)} and target.read_text() == text
    assert asked == ["netlithic-wallet-backup-abababab.json"]
    # a bare key, or anything with extra fields, is refused and no dialog opens
    for bad in ["9d" * 32, json.dumps({**GOOD, "seed": "00" * 32}), json.dumps({**GOOD, "format": "x"}), "{"]:
        asked.clear()
        assert api.save_backup(bad, "x.json")["ok"] is False and asked == []


def test_save_backup_cancel_and_name_cleanup(tmp_path):
    api = D.WindowApi(ask_save_path=lambda n: None)
    assert api.save_backup(json.dumps(GOOD), "a.json") == {"ok": False}
    names = []
    api = D.WindowApi(ask_save_path=lambda n: names.append(n) or None)
    api.save_backup(json.dumps(GOOD), "..\\..\\evil")
    assert names == ["....evil.json"]


def test_copy_only_addresses():
    got = []
    api = D.WindowApi(set_clipboard=got.append)
    assert api.copy_text(ADDR) is True and got == [ADDR]
    assert api.copy_text("9d" * 32) is False and got == [ADDR]


def test_wallet_js_uses_desktop_hooks_but_keeps_web_download():
    js = open(os.path.join(webwallet.STATIC, "wallet.js"), encoding="utf-8").read()
    assert "window.NetlithicSaveBackup" in js and "a.download = name" in js
    assert "window.NetlithicCopy" in js and "navigator.clipboard.writeText" in js
    dj = open(os.path.join(ROOT, "desktop", "static", "desktop.js"), encoding="utf-8").read()
    assert "save_backup" in dj and "pywebviewready" in dj


def test_js_api_exposes_only_plain_methods():
    """pywebview recurses into public attributes of js_api; a public Window ref froze
    the window on Windows. Mirror its walk and require only methods to be public."""
    import inspect
    api = D.WindowApi()
    api._window = object()
    public = [n for n in dir(api) if not n.startswith("_")]
    assert sorted(public) == ["copy_text", "notify", "save_backup"]
    assert all(inspect.ismethod(getattr(api, n)) for n in public)


def test_log_file_has_no_secrets(tmp_path):
    import logging
    h = D.setup_logging(str(tmp_path))
    try:
        api = D.WindowApi(ask_save_path=lambda n: str(tmp_path / "b.json"), set_clipboard=lambda t: None)
        api.save_backup(json.dumps(GOOD), "x.json")
        api.copy_text(ADDR)
        e = earner(FakeNet())
        e.status()
    finally:
        logging.getLogger().removeHandler(h); h.close()
    text = (tmp_path / "netlithic.log").read_text()
    assert "backup file saved" in text
    for secret in (GOOD["ciphertext"], GOOD["salt"], "t" * 10):
        assert secret not in text


def test_app_starts_and_writes_log(tmp_path):
    """Real start-up path (no window): logs to <parent of data dir>/netlithic.log."""
    import subprocess, time, socket
    def free():
        s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p
    ui, nodep = free(), free()
    env = {**os.environ, "NETLITHIC_UI_PORT": str(ui), "NETLITHIC_NODE_PORT": str(nodep)}
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "desktop", "netlithic_desktop.py"), "--no-window",
                             "--data-dir", str(tmp_path / "testnet"), "--seed", "http://127.0.0.1:9"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        ok = False
        for _ in range(60):
            try:
                with urlopen(f"http://127.0.0.1:{ui}/api/earn/status", timeout=2) as r:
                    ok = r.status == 200; break
            except Exception:
                if proc.poll() is not None:
                    break
                time.sleep(0.25)
        assert ok, proc.stdout.read().decode() if proc.poll() is not None else "no answer"
    finally:
        proc.terminate(); proc.wait(15)
    assert "starting 0.5.0 (frozen=False" in (tmp_path / "netlithic.log").read_text()


def test_parent_alive_and_watchdog():
    import subprocess, time
    assert D.parent_alive(os.getppid()) or os.name == "nt"
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    gone = threading.Event()
    D.watch_parent(child.pid, interval=0.05, on_gone=gone.set)
    time.sleep(0.3)
    assert not gone.is_set()
    child.kill(); child.wait()
    assert gone.wait(5)


def test_node_command_passes_parent_pid():
    cmd = D.node_command("/tmp/x", 47101, "http://seed:8000")
    assert cmd[cmd.index("--parent-pid") + 1] == str(os.getpid())


def _start_app(tmp_path):
    import subprocess, time, socket
    def free():
        s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p
    ui, nodep = free(), free()
    env = {**os.environ, "NETLITHIC_UI_PORT": str(ui), "NETLITHIC_NODE_PORT": str(nodep)}
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "desktop", "netlithic_desktop.py"), "--no-window",
                             "--data-dir", str(tmp_path / "testnet"), "--seed", "http://127.0.0.1:9"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    node_up = False
    for _ in range(80):
        try:
            with urlopen(f"http://127.0.0.1:{nodep}/status", timeout=2):
                node_up = True; break
        except Exception:
            time.sleep(0.25)
    assert node_up
    return proc, nodep


def _node_gone(port, wait=8):
    import time
    end = time.time() + wait
    while time.time() < end:
        try:
            urlopen(f"http://127.0.0.1:{port}/status", timeout=1).close()
            time.sleep(0.25)
        except Exception:
            return True
    return False


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
def test_closing_app_stops_node(tmp_path):
    proc, nodep = _start_app(tmp_path)
    proc.terminate()
    assert proc.wait(10) is not None
    assert _node_gone(nodep)
    assert "stopped" in (tmp_path / "netlithic.log").read_text()


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
def test_node_stops_if_app_is_killed(tmp_path):
    proc, nodep = _start_app(tmp_path)
    proc.kill(); proc.wait(5)
    assert _node_gone(nodep)


def test_installer_closes_running_app():
    iss = open(os.path.join(ROOT, "desktop", "netlithic.iss"), encoding="utf-8").read()
    for s in ("CloseApplications=force", "function PrepareToInstall", "function InitializeUninstall",
              "/F /T /IM Netlithic.exe"):
        assert s in iss


def test_seed_list_parsing_and_node_command():
    assert D.parse_seeds("http://a:1/, http://b:2,http://a:1") == ["http://a:1", "http://b:2"]
    assert D.parse_seeds(["http://a:1"]) == ["http://a:1"]
    cmd = D.node_command("/tmp/x", 47101, ["http://a:1", "http://b:2"])
    peers = [cmd[i + 1] for i, v in enumerate(cmd) if v == "--peer"]
    assert peers == ["http://a:1", "http://b:2"]


def test_earner_falls_back_to_next_seed():
    def call(url, payload=None, headers=None):
        if url.startswith("http://down"):
            raise OSError("refused")
        if url.startswith("http://up"):
            return 200, {"height": 7}
        return 200, {"height": 7}
    e = D.Earner("http://local", ["http://down:1", "http://up:2"], call, admin_token="t" * 10)
    assert e.heights() == (7, 7)
    assert e.seed == "http://up:2"
    assert e.status()["synced"]
