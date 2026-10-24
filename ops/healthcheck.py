"""Netlithic testnet health check. Read-only: it only calls GET endpoints.

    python ops/healthcheck.py --config /etc/netlithic/healthcheck.json

Checks: each node answers /status, heights keep advancing, nodes agree (same
tip within a few blocks), faucet balance, HTTPS certificate expiry, free disk.
Problems are printed; an alert is SENT only when "enabled": true in the config
(off by default). Alerts are de-duplicated: the same problem alerts again only
after "repeat_minutes", and a "recovered" note is sent when it clears.
Exit code: 0 healthy, 1 problems found, 2 bad config.
"""
import argparse, json, os, shutil, smtplib, socket, ssl, sys, time
from datetime import datetime, timezone
from email.message import EmailMessage
from urllib.request import Request, urlopen


def _safe_urlopen(req, *a, **kw):
    """urlopen limited to http/https (bandit B310)."""
    from urllib.parse import urlparse as _up
    url = req.full_url if isinstance(req, Request) else str(req)
    if _up(url).scheme.lower() not in ("http", "https"):
        raise ValueError("URL scheme not allowed")
    return urlopen(req, *a, **kw)  # nosec B310 - scheme checked above


DEFAULTS = {
    "enabled": False,                  # send nothing until switched on
    "nodes": ["http://127.0.0.1:8000", "http://127.0.0.1:8001"],
    "faucet_info_url": "http://127.0.0.1:8090/api/info",
    "faucet_min_nlt": 200,
    "stall_minutes": 30,               # no new block for this long -> alert (timer mines every 10)
    "max_height_gap": 3,               # nodes further apart than this -> alert
    "cert_hosts": ["testnet.netlithicchain.com"],
    "cert_min_days": 14,
    "disk_paths": ["/"],
    "disk_min_free_pct": 15,
    "state_file": "/var/lib/netlithic/healthcheck_state.json",
    "repeat_minutes": 360,
    "webhook_url": "",                 # Slack/Discord/ntfy-style URL; keep it in the config file, not git
    "email": {"smtp_host": "", "smtp_port": 587, "username": "", "password_file": "",
              "from": "", "to": []},
}


def http_json(url, timeout=8):
    with _safe_urlopen(Request(url, headers={"Accept": "application/json"}), timeout=timeout) as r:
        return json.loads(r.read(1 << 20).decode() or "{}")


def cert_days_left(host, port=443, timeout=8):
    ctx = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as s:
            not_after = s.getpeercert()["notAfter"]
    exp = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    return (exp - datetime.now(timezone.utc)).total_seconds() / 86400


def run_checks(cfg, state, now=None, fetch=http_json, cert_fn=cert_days_left, disk_fn=shutil.disk_usage):
    """Returns (problems: {key: message}, new_state). Pure apart from the injected I/O."""
    now = time.time() if now is None else now
    problems = {}
    heights = {}
    seen = dict(state.get("heights", {}))
    for node in cfg["nodes"]:
        try:
            st = fetch(node.rstrip("/") + "/status")
            h = int(st["height"])
            heights[node] = h
        except Exception as e:
            problems[f"down:{node}"] = f"Node {node} is not answering ({type(e).__name__})."
            continue
        last = seen.get(node)
        if last is None or h > last["height"]:
            seen[node] = {"height": h, "since": now}
        elif now - last["since"] > cfg["stall_minutes"] * 60:
            mins = int((now - last["since"]) / 60)
            problems[f"stalled:{node}"] = f"Node {node} has been stuck at block {h} for {mins} minutes."
    if len(heights) >= 2:
        gap = max(heights.values()) - min(heights.values())
        if gap > cfg["max_height_gap"]:
            problems["out_of_sync"] = "Nodes disagree on height: " + ", ".join(
                f"{n} at {h}" for n, h in sorted(heights.items())) + "."
    if cfg.get("faucet_info_url"):
        try:
            bal = float(fetch(cfg["faucet_info_url"])["balance_nlt"])
            if bal < cfg["faucet_min_nlt"]:
                problems["faucet_low"] = f"Faucet balance is low: {bal:g} test NLT (alert below {cfg['faucet_min_nlt']})."
        except Exception as e:
            problems["faucet_down"] = f"Faucet info is not answering ({type(e).__name__})."
    for host in cfg.get("cert_hosts", []):
        try:
            days = cert_fn(host)
            if days < cfg["cert_min_days"]:
                problems[f"cert:{host}"] = f"HTTPS certificate for {host} expires in {days:.0f} days."
        except Exception as e:
            problems[f"cert:{host}"] = f"Could not check the HTTPS certificate for {host} ({type(e).__name__})."
    for path in cfg.get("disk_paths", []):
        try:
            u = disk_fn(path)
            free = 100.0 * u.free / u.total
            if free < cfg["disk_min_free_pct"]:
                problems[f"disk:{path}"] = f"Disk {path} is {100 - free:.0f}% full."
        except Exception as e:
            problems[f"disk:{path}"] = f"Could not check disk {path} ({type(e).__name__})."
    return problems, {**state, "heights": seen}


# ---- PROPOSAL (not wired into run yet): reorg and miner-share alerts ----
def reorg_problem(prev_hashes, now_hashes, alert_depth=3):
    """prev_hashes/now_hashes = {height: hash} of recent blocks from the last run and now.
    A reorg replaced every height whose hash changed; alert when that many >= alert_depth."""
    changed = [h for h, x in prev_hashes.items() if h in now_hashes and now_hashes[h] != x]
    if len(changed) >= alert_depth:
        return f"Chain rewrite: {len(changed)} blocks replaced (from block {min(changed)})."
    return None


def miner_share_problems(miners, threshold_pct=40, min_blocks=50):
    """miners = reward addresses of the most recent blocks. Alert when one address
    mined more than threshold_pct of them (founder address excluded by the caller)."""
    if len(miners) < min_blocks:
        return {}
    counts = {}
    for m in miners:
        counts[m] = counts.get(m, 0) + 1
    out = {}
    for m, c in counts.items():
        pct = 100 * c / len(miners)
        if pct > threshold_pct:
            out[f"miner_share:{m}"] = f"One miner ({m[:12]}...) made {pct:.0f}% of the last {len(miners)} blocks."
    return out


def plan_alerts(problems, state, cfg, now):
    """Which messages to send now (new, repeating, recovered) and the updated alert state."""
    alerted = dict(state.get("alerted", {}))
    out = []
    for key, msg in problems.items():
        if key not in alerted or now - alerted[key] >= cfg["repeat_minutes"] * 60:
            out.append(msg)
            alerted[key] = now
    for key in [k for k in alerted if k not in problems]:
        out.append(f"Recovered: {key.split(':', 1)[0].replace('_', ' ')} {key.split(':', 1)[1] if ':' in key else ''}".strip() + ".")
        del alerted[key]
    return out, {**state, "alerted": alerted}


def send_webhook(url, text):
    payload = {"content": text} if "discord" in url else {"text": text, "content": text}
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json",
               "User-Agent": "NetlithicHealthcheck/1.0 (+https://testnet.netlithicchain.com)"}
    with _safe_urlopen(Request(url, data=body, headers=headers), timeout=10):
        pass


def send_email(ecfg, text):
    msg = EmailMessage()
    msg["Subject"] = "Netlithic testnet alert"
    msg["From"], msg["To"] = ecfg["from"], ", ".join(ecfg["to"])
    msg.set_content(text)
    with smtplib.SMTP(ecfg["smtp_host"], int(ecfg.get("smtp_port", 587)), timeout=15) as s:
        s.starttls()
        if ecfg.get("username"):
            with open(ecfg["password_file"], encoding="utf-8") as f:
                s.login(ecfg["username"], f.read().strip())
        s.send_message(msg)


def deliver(cfg, messages):
    if not messages:
        return
    text = "Netlithic testnet:\n- " + "\n- ".join(messages)
    print(text)
    if not cfg.get("enabled"):
        print("(alerts disabled: nothing sent)")
        return
    if cfg.get("webhook_url"):
        send_webhook(cfg["webhook_url"], text)
    e = cfg.get("email") or {}
    if e.get("smtp_host") and e.get("to"):
        send_email(e, text)


def load_config(path):
    cfg = json.loads(json.dumps(DEFAULTS))
    if path:
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    return cfg


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--config", default=os.environ.get("NETLITHIC_HEALTH_CONFIG"))
    p.add_argument("--dry-run", action="store_true", help="never send, even if enabled")
    a = p.parse_args(argv)
    try:
        cfg = load_config(a.config)
    except Exception as e:
        print(f"bad config: {e}")
        return 2
    if a.dry_run:
        cfg["enabled"] = False
    try:
        with open(cfg["state_file"], encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        state = {}
    now = time.time()
    problems, state = run_checks(cfg, state, now)
    messages, state = plan_alerts(problems, state, cfg, now)
    deliver(cfg, messages)
    try:
        os.makedirs(os.path.dirname(cfg["state_file"]) or ".", exist_ok=True)
        with open(cfg["state_file"], "w", encoding="utf-8") as f:
            json.dump(state, f)
    except OSError as e:
        print(f"could not save state: {e}")
    if not problems:
        print("healthy")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
