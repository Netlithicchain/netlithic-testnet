import json, os, socket, subprocess, sys, time, urllib.request

from conftest import ROOT, TEST_ADMIN_TOKEN


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def wait(url, t=30):
    end = time.time() + t
    while time.time() < end:
        try:
            return json.loads(urllib.request.urlopen(url, timeout=2).read())
        except Exception:
            time.sleep(0.3)
    raise AssertionError("timeout " + url)


def test_join_script_syncs_with_local_seed(tmp_path):
    seed_port, join_port = free_port(), free_port()
    env = {**os.environ, "NETLITHIC_ADMIN_TOKEN": TEST_ADMIN_TOKEN}
    env.pop("NETLITHIC_DEV_LOCAL_ADMIN", None)
    seed = subprocess.Popen([sys.executable, os.path.join(ROOT, "netlithic.py"), "node", "start", str(seed_port),
                             "--data-dir", str(tmp_path / "seed")], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        seed_url = f"http://127.0.0.1:{seed_port}"
        wait(seed_url + "/status")
        for _ in range(2):
            req = urllib.request.Request(seed_url + "/mine", data=json.dumps({"miner_address": "d" * 32}).encode(),
                                         headers={"Content-Type": "application/json", "X-Admin-Token": TEST_ADMIN_TOKEN})
            urllib.request.urlopen(req, timeout=120).read()
        data = tmp_path / "me"
        jenv = {k: v for k, v in os.environ.items() if k != "NETLITHIC_ADMIN_TOKEN"}
        r = subprocess.run([sys.executable, os.path.join(ROOT, "join_testnet.py"), "--no-venv",
                            "--seed", seed_url, "--port", str(join_port), "--data-dir", str(data),
                            "--advertise-url", f"http://127.0.0.1:{join_port}",
                            "--sync-timeout", "60", "--exit-after-sync"],
                           capture_output=True, text=True, timeout=180, env=jenv)
        out = r.stdout + r.stderr
        assert r.returncode == 0, out
        assert "Synced: your node is at height 2" in out
        assert "NO value" in out and "netlithic.py mine" in out
        wallets = json.loads((data / "wallets.json").read_text())
        (addr, w), = wallets.items()
        assert addr in out and w["private_key"] not in out
        token = (data / "admin_token.txt").read_text().strip()
        assert token and token not in out
        if os.name != "nt":
            assert oct(os.stat(data / "wallets.json").st_mode & 0o777) == "0o600"
            assert oct(os.stat(data / "admin_token.txt").st_mode & 0o777) == "0o600"
        peers = wait(seed_url + "/peers")["peers"]
        assert f"http://127.0.0.1:{join_port}" in peers or "Seed knows you now" in out
    finally:
        seed.terminate(); seed.wait(10)


def test_join_refuses_wrong_chain(tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            b = json.dumps({"chain_id": "netlithic-main-1", "height": 0}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = subprocess.run([sys.executable, os.path.join(ROOT, "join_testnet.py"), "--no-venv", "--seed",
                            f"http://127.0.0.1:{srv.server_address[1]}", "--data-dir", str(tmp_path / "x"),
                            "--exit-after-sync"], capture_output=True, text=True, timeout=60)
        assert r.returncode == 2 and "netlithic-main-1" in r.stdout
        assert not (tmp_path / "x" / "wallets.json").exists()
    finally:
        srv.shutdown()
