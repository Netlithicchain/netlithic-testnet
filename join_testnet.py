"""Join the Netlithic TEST network with one command.

    python join_testnet.py                      (Windows: py join_testnet.py)
    python join_testnet.py --seed http://137.184.199.238:8000 --port 8000

What it does, all on YOUR machine:
  1. creates a virtualenv (.venv-testnet) and installs `cryptography`
  2. creates your own wallet in <data-dir>/wallets.json (owner-only); only the
     ADDRESS is printed, never the private key
  3. creates an admin token for your own node in <data-dir>/admin_token.txt
     (owner-only, never printed) so only you can mine on your node
  4. starts a node with the shared genesis config, peers it with the seed node,
     says /hello (if you give a public --advertise-url) and syncs the chain
  5. prints how to mine test blocks on your own node

TESTNET ONLY: test NLT has NO value and the chain can be wiped at any reset.
"""
import argparse
import json
import os
import secrets
import subprocess
import sys
import time
import venv
from urllib.error import HTTPError, URLError
from urllib.request import Request
from netutil import urlopen

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SEED = os.environ.get("NETLITHIC_SEED_URL", "http://137.184.199.238:8000")
DEV_PREFIX = "netlithic-dev"
IS_WIN = os.name == "nt"

BANNER = ("=" * 66 + "\n NETLITHIC TESTNET - test coins have NO value and may be wiped.\n"
          + "=" * 66)


def say(msg=""):
    print(msg, flush=True)


def http_json(url, payload=None, headers=None, timeout=10):
    data = None if payload is None else json.dumps(payload).encode()
    req = Request(url, data=data, method="GET" if data is None else "POST",
                  headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}


def venv_python(venv_dir):
    return os.path.join(venv_dir, "Scripts" if IS_WIN else "bin", "python.exe" if IS_WIN else "python")


def ensure_venv(venv_dir):
    py = venv_python(venv_dir)
    if not os.path.exists(py):
        say(f"[1/5] Creating virtualenv {venv_dir} ...")
        venv.EnvBuilder(with_pip=True).create(venv_dir)
    else:
        say(f"[1/5] Using virtualenv {venv_dir}")
    if subprocess.run([py, "-c", "import cryptography"], capture_output=True).returncode != 0:
        say("      Installing cryptography ...")
        subprocess.run([py, "-m", "pip", "install", "--quiet", "-r",
                        os.path.join(ROOT, "requirements.txt")], check=True)
    return py


def write_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
    except (AttributeError, OSError):
        pass
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def ensure_wallet(data_dir, name):
    """Create (or reuse) the user's wallet. Returns the address only."""
    sys.path.insert(0, ROOT)
    import wallet as wallet_mod
    wallet_mod.WALLET_FILE = os.path.join(data_dir, "wallets.json")
    existing = wallet_mod.Wallet._load_wallets()
    for addr, w in existing.items():
        if w["name"] == name:
            say(f"[2/5] Using your existing wallet '{name}'")
            return addr
    w = wallet_mod.Wallet.create(name)
    say(f"[2/5] Created your wallet '{name}' (private key kept in "
        f"{wallet_mod.WALLET_FILE}, owner-only; never share it)")
    return w.address


def ensure_admin_token(data_dir):
    path = os.path.join(data_dir, "admin_token.txt")
    if not os.path.exists(path):
        write_private(path, secrets.token_urlsafe(32))
        say(f"[3/5] Created your node's admin token in {path} (owner-only)")
    else:
        say(f"[3/5] Using your node's admin token in {path}")
    with open(path, encoding="utf-8") as f:
        return path, f.read().strip()


def local_chain_id():
    with open(os.environ.get("NETLITHIC_GENESIS_CONFIG",
                             os.path.join(ROOT, "config", "genesis_config.json")), encoding="utf-8") as f:
        return json.load(f)["chain_id"]


def wait_for(url, timeout):
    end = time.time() + timeout
    while time.time() < end:
        try:
            return http_json(url, timeout=3)[1]
        except (URLError, OSError):
            time.sleep(0.5)
    return None


def mining_help(args, data_dir, token_path, address):
    node = f"http://127.0.0.1:{args.port}"
    wallets = os.path.join(data_dir, "wallets.json")
    return f"""
How to mine test blocks on YOUR node (your token, your node only):
  Windows PowerShell:
    $env:NETLITHIC_ADMIN_TOKEN = Get-Content "{token_path}"
    python netlithic.py mine {address} --node {node}
  Linux / macOS:
    export NETLITHIC_ADMIN_TOKEN="$(cat '{token_path}')"
    python3 netlithic.py mine {address} --node {node}
Each block pays you test NLT and is shared with the seed node.
Check your balance:   python netlithic.py balance {address} --node {node}
Your wallet file:     {wallets}  (keep private; it holds your key)
Remember: test NLT has NO value and the testnet may be reset at any time."""


def run(args):
    say(BANNER)
    seed = args.seed.rstrip("/")
    data_dir = os.path.abspath(args.data_dir)
    os.makedirs(data_dir, exist_ok=True)

    status = wait_for(f"{seed}/status", 10)
    if status is None:
        say(f"Could not reach the seed node {seed}. Check your internet or use --seed.")
        return 2
    cid = local_chain_id()
    if status.get("chain_id") != cid:
        say(f"Seed is on chain {status.get('chain_id')!r} but your config says {cid!r}. "
            "Update your code (git pull) and try again.")
        return 2
    if not str(cid).startswith(DEV_PREFIX):
        say(f"Refusing: {cid!r} is not a test network.")
        return 2
    say(f"      Seed {seed} is on {cid}, height {status.get('height')}")

    address = ensure_wallet(data_dir, args.wallet_name)
    say(f"      Your address: {address}")
    token_path, token = ensure_admin_token(data_dir)

    say(f"[4/5] Starting your node on port {args.port} ...")
    cmd = [sys.executable, os.path.join(ROOT, "netlithic.py"), "node", "start", str(args.port),
           "--data-dir", data_dir, "--host", args.host, "--peer", seed]
    if args.advertise_url:
        cmd += ["--advertise-url", args.advertise_url]
    env = {**os.environ, "NETLITHIC_ADMIN_TOKEN": token, "PYTHONUNBUFFERED": "1"}
    log = open(os.path.join(data_dir, "node.log"), "a", encoding="utf-8")
    proc = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT, cwd=ROOT)
    local = f"http://127.0.0.1:{args.port}"
    try:
        if wait_for(f"{local}/status", 20) is None:
            say(f"Your node did not start; see {os.path.join(data_dir, 'node.log')}")
            proc.terminate()
            return 3
        if args.advertise_url:
            code, body = http_json(f"{seed}/hello", {"url": args.advertise_url, "chain_id": cid})
            if code == 200:
                say(f"      Seed knows you now: {body.get('message')}")
            else:
                say(f"      Seed could not reach {args.advertise_url} ({body.get('error')}). "
                    "You still sync and can mine; open the port for others to reach you.")
        else:
            say("      No --advertise-url: you download the chain and send blocks out, "
                "but others can't connect to you (fine behind home routers).")

        say("[5/5] Syncing ...")
        target = int(status.get("height", 0))
        http_json(f"{local}/sync", {}, timeout=60)
        end = time.time() + args.sync_timeout
        height = -1
        while time.time() < end:
            height = int(http_json(f"{local}/status")[1].get("height", -1))
            if height >= target:
                break
            time.sleep(1)
        if height < target:
            say(f"      Still syncing (at {height} of {target}); it keeps going in the background.")
        else:
            say(f"      Synced: your node is at height {height}.")
        say(mining_help(args, data_dir, token_path, address))
        if args.exit_after_sync:
            proc.terminate()
            proc.wait(10)
            return 0 if height >= target else 4
        say(f"\nYour node is running at {local} (log: {data_dir}{os.sep}node.log). Ctrl+C stops it.")
        proc.wait()
        return proc.returncode
    except KeyboardInterrupt:
        say("\nStopping your node ...")
        proc.terminate()
        return 0
    finally:
        log.close()


def main(argv=None):
    p = argparse.ArgumentParser(description="Join the Netlithic testnet (test coins have no value).")
    p.add_argument("--seed", default=DEFAULT_SEED, help=f"seed node URL (default {DEFAULT_SEED}; "
                   "or set NETLITHIC_SEED_URL)")
    p.add_argument("--port", type=int, default=8000, help="your node's port (default 8000)")
    p.add_argument("--host", default="127.0.0.1",
                   help="address your node listens on (0.0.0.0 to accept peers)")
    p.add_argument("--advertise-url", default=None,
                   help="your node's PUBLIC URL, e.g. http://203.0.113.5:8000 (optional)")
    p.add_argument("--data-dir", default=os.path.join(ROOT, "testnet-node"))
    p.add_argument("--wallet-name", default="me")
    p.add_argument("--venv", default=os.path.join(ROOT, ".venv-testnet"))
    p.add_argument("--no-venv", action="store_true", help="use the current Python as is")
    p.add_argument("--sync-timeout", type=int, default=120)
    p.add_argument("--exit-after-sync", action="store_true", help="stop the node after syncing (tests)")
    args = p.parse_args(argv)

    if not args.no_venv and os.environ.get("NETLITHIC_JOIN_IN_VENV") != "1":
        py = ensure_venv(os.path.abspath(args.venv))
        env = {**os.environ, "NETLITHIC_JOIN_IN_VENV": "1"}
        return subprocess.call([py, os.path.abspath(__file__), *(argv if argv is not None else sys.argv[1:])],
                               env=env)
    if args.no_venv:
        say("[1/5] Using the current Python (no virtualenv)")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
