"""Founder/faucet server miner policy (TEST NETWORK). Run by a systemd timer instead of
calling `netlithic.py mine` directly.

Plain rule:
- Before block STOP_HEIGHT (default 5000): mine as today (one block per timer run).
- From STOP_HEIGHT on: the founder server does NOT mine. It only runs a node that relays
  blocks and transactions.
- Optional backstop (on by default, can be switched off): if NOBODY has made a block for
  IDLE_MINUTES (default 30), the server may make one block, and never more than one per
  MIN_GAP_MINUTES (default 30). So the founder server's share is capped at
  max 1 block per 30 minutes, and only while no one else is mining.
- Mined rewards go to the public founder/faucet address (default below), so anyone can
  check its share in the explorer.

This file changes no consensus rules. It reads /status and asks the local node to /mine.
Settings (env): NETLITHIC_NODE, NETLITHIC_MINER_ADDRESS, NETLITHIC_FOUNDER_STOP_HEIGHT,
NETLITHIC_FOUNDER_BACKSTOP (1/0), NETLITHIC_FOUNDER_IDLE_MINUTES, NETLITHIC_FOUNDER_MIN_GAP_MINUTES,
NETLITHIC_FOUNDER_STATE (file holding the time of the server's last block),
NETLITHIC_ADMIN_TOKEN (read from env only, never printed).
"""
import argparse
import json
import os
import sys
import time
from urllib.request import Request, urlopen


def _safe_urlopen(req, *a, **kw):
    """urlopen limited to http/https (bandit B310)."""
    from urllib.parse import urlparse as _up
    url = req.full_url if isinstance(req, Request) else str(req)
    if _up(url).scheme.lower() not in ("http", "https"):
        raise ValueError("URL scheme not allowed")
    return urlopen(req, *a, **kw)  # nosec B310 - scheme checked above


FOUNDER_ADDRESS = "e924523b2614a39b9bb4ff8bfc05e747"  # public: founder/faucet mining address
DEFAULTS = {"stop_height": 5000, "backstop": True, "idle_minutes": 30, "min_gap_minutes": 30}


def decide(height, latest_timestamp, now, last_own_mine, stop_height=5000, backstop=True,
           idle_minutes=30, min_gap_minutes=30):
    """Return (should_mine, reason in plain words). `height` is the latest block's index;
    the block we would make is height + 1."""
    if height + 1 < stop_height:
        return True, f"before block {stop_height}: normal mining"
    if not backstop:
        return False, f"block {stop_height} reached: founder server only relays"
    idle = now - float(latest_timestamp or 0)
    if idle < idle_minutes * 60:
        return False, f"relay only: last block {int(idle // 60)} min ago (backstop after {idle_minutes} min)"
    if last_own_mine and now - last_own_mine < min_gap_minutes * 60:
        return False, f"relay only: server already made a block in the last {min_gap_minutes} min"
    return True, f"backstop: no block from anyone for {int(idle // 60)} min"


def _get(url, timeout=15):
    with _safe_urlopen(Request(url, headers={"Accept": "application/json"}), timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def main(argv=None):
    env = os.environ.get
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--node", default=env("NETLITHIC_NODE", "http://127.0.0.1:8000"))
    p.add_argument("--miner", default=env("NETLITHIC_MINER_ADDRESS", FOUNDER_ADDRESS))
    p.add_argument("--stop-height", type=int, default=int(env("NETLITHIC_FOUNDER_STOP_HEIGHT", DEFAULTS["stop_height"])))
    p.add_argument("--backstop", type=int, choices=(0, 1), default=int(env("NETLITHIC_FOUNDER_BACKSTOP", "1")))
    p.add_argument("--idle-minutes", type=float, default=float(env("NETLITHIC_FOUNDER_IDLE_MINUTES", DEFAULTS["idle_minutes"])))
    p.add_argument("--min-gap-minutes", type=float, default=float(env("NETLITHIC_FOUNDER_MIN_GAP_MINUTES", DEFAULTS["min_gap_minutes"])))
    p.add_argument("--state", default=env("NETLITHIC_FOUNDER_STATE", "/var/lib/netlithic/founder_miner.json"))
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    node = a.node.rstrip("/")
    st = _get(node + "/status")
    if "latest_timestamp" not in st:
        ts = _get(node + "/chain")["chain"][-1]["timestamp"]  # older node without the field
    else:
        ts = st["latest_timestamp"]
    try:
        with open(a.state) as f:
            last_own = float(json.load(f).get("last_mine", 0))
    except (OSError, ValueError):
        last_own = 0.0
    now = time.time()
    ok, why = decide(int(st["height"]), ts, now, last_own, a.stop_height, bool(a.backstop),
                     a.idle_minutes, a.min_gap_minutes)
    print(f"height {st['height']}: {why}")
    if not ok or a.dry_run:
        return 0
    token = env("NETLITHIC_ADMIN_TOKEN", "")
    req = Request(node + "/mine", data=json.dumps({"miner_address": a.miner}).encode(), method="POST",
                  headers={"Content-Type": "application/json", **({"X-Admin-Token": token} if token else {})})
    with _safe_urlopen(req, timeout=600) as r:
        b = json.loads(r.read().decode() or "{}").get("block", {})
    print(f"made block {b.get('index')} for {a.miner}")
    if int(st["height"]) + 1 >= a.stop_height:
        try:
            os.makedirs(os.path.dirname(a.state) or ".", exist_ok=True)
            with open(a.state, "w") as f:
                json.dump({"last_mine": now}, f)
        except OSError as e:
            print(f"warning: could not save state: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
