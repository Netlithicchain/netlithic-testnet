"""Netlithic testnet health check. Read-only: it only calls GET endpoints.

    python ops/healthcheck.py --config /etc/netlithic/healthcheck.json

Checks: each node answers /status, heights keep advancing, nodes agree (same
tip within a few blocks), faucet balance, HTTPS certificate expiry, free disk.
0.5.0 checks: node version / fork_ready, outdated peers (urgent near the
block-4900 deadline), tip split at equal height, block checkpoints
(config/checkpoints.json), chain rewrites since the last run, pause state,
and node log lines for refused deep reorgs / checkpoint rejects / skipped peers.
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
    # ---- 0.5.0 checks ----
    "expected_version": "0.5.3",       # nodes we run must report at least this
    "check_peers": True,               # also look at each node's listed peers
    "deadline_warn_blocks": 500,       # outdated peer becomes "urgent" this close to the deadline
    "checkpoints_file": os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                     "config", "checkpoints.json"),
    "reorg_alert_depth": 3,            # rewrite of this many recent blocks -> alert
    "reorg_track_blocks": 50,
    "miner_share_window": 200,         # last N blocks looked at for a dominant miner or funding cluster; 0 = off
    "miner_share_threshold_pct": 40,   # one address or one funding cluster above this share -> alert
    "miner_share_ignore": [],          # the founder's own mining addresses (not counted)
    "miner_share_ignore_funders": [],  # faucet / exchange addresses that fund many people (not used to link addresses)
    "check_pause": True,
    "expected_pause_key_id": "",       # set to the pause_key_id the founder published (2a83f37a4ce7a269); any node showing another value alerts; blank = warning
    "journal_units": [],               # e.g. ["netlithic-node-a"]; or env NETLITHIC_HEALTH_JOURNAL_UNITS
    "journal_patterns": {
        "reorg_refused": "exceeds limit",
        "checkpoint_reject": "does not match checkpoint",
        "checkpoint_file": "Checkpoint file",
        "peer_skipped": "Skipping peer",
    },
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
    statuses = {}
    seen = dict(state.get("heights", {}))
    for node in cfg["nodes"]:
        try:
            st = fetch(node.rstrip("/") + "/status")
            h = int(st["height"])
            heights[node] = h
            statuses[node] = st
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
    extra, state = v050_checks(cfg, {**state, "heights": seen}, statuses, now, fetch)
    problems.update(extra)
    return problems, state


def _vt(v):
    try:
        return tuple(int(x) for x in str(v).split(".")[:3])
    except (TypeError, ValueError):
        return (0, 0, 0)


def _outdated(v, minimum):
    return not v or _vt(v) < _vt(minimum)


def _load_checkpoints(path):
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    out = {}
    for k, v in raw.items():
        if str(k).isdigit():
            out[int(k)] = v if isinstance(v, str) else v.get("hash")
    return out


def journal_lines(units, since_ts, run=None):
    """New node log lines since since_ts (journalctl). Returns list of str, or None if unreadable."""
    import subprocess  # nosec B404 - fixed argv, no shell
    if not units:
        return []
    cmd = ["journalctl", "--no-pager", "-q", "-o", "cat", "--since", f"@{int(since_ts)}"]
    for u in units:
        cmd += ["-u", u]
    try:
        r = (run or subprocess.run)(cmd, capture_output=True, text=True, timeout=20)  # nosec B603
    except Exception:
        return None
    if r.returncode != 0:
        return None
    return r.stdout.splitlines()


def v050_checks(cfg, state, statuses, now, fetch=http_json, journal_fn=journal_lines):
    """0.5.0 alerts. statuses = {node: /status json}. Returns (problems, new_state)."""
    problems = {}
    want = cfg.get("expected_version") or "0.5.3"
    deadline = None
    if not str(cfg.get("expected_pause_key_id") or "").strip() and any(s.get("version") for s in statuses.values()):
        # One warning for the whole config (not one per node); repeats only every repeat_minutes like the others.
        problems["pause_key_check_off"] = ("Warning: expected_pause_key_id is blank in the healthcheck config, so nobody is checking "
                                           "that every node has the right pause key. Set it to 2a83f37a4ce7a269.")
    for node, st in statuses.items():
        if _outdated(st.get("version"), want):
            problems[f"node_version:{node}"] = (f"Node {node} runs {st.get('version') or 'an old version'}, "
                                                f"expected {want} or newer.")
        want_key = str(cfg.get("expected_pause_key_id") or "").strip().lower()   # blank = per-node check off (a single warning is raised above)
        got_key = str(st.get("pause_key_id") or "").strip().lower()
        if want_key and st.get("version") and got_key != want_key:
            problems[f"pause_key:{node}"] = (f"Node {node} has pause key id {got_key or 'EMPTY'}, "
                                             f"expected {want_key}: it would fork off at the first pause.")
        if st.get("version") and st.get("pause_key_valid") is False and st.get("pause_key_problem") not in (None, "", "empty"):
            problems[f"pause_key_invalid:{node}"] = (f"Node {node} says its pause key is unusable: "
                                                     f"{str(st.get('pause_key_problem'))[:160]}")
        if st.get("version") and not st.get("fork_ready"):
            problems[f"fork_not_ready:{node}"] = f"Node {node} says it is not ready for the block-5000 fork."
        if st.get("upgrade_deadline_height"):
            deadline = int(st["upgrade_deadline_height"])
    tip = max((int(s.get("height", 0)) for s in statuses.values()), default=0)
    # tip split: same height, different hash
    by_h = {}
    for node, st in statuses.items():
        by_h.setdefault(st.get("height"), set()).add(st.get("latest_hash"))
    for h, hashes in by_h.items():
        if len(hashes) > 1:
            problems["tip_split"] = f"Nodes at block {h} have different tips: the chain may have split."
    # reorg tripwire: the node itself counts reorgs (see Blockchain.reorg_info); alert on a reorg of reorg_alert_depth blocks or more, and on refused deep reorgs
    for node, st in statuses.items():
        msg = deep_reorg_problem(st, cfg.get("reorg_alert_depth", 3))
        if msg:
            problems[f"deep_reorg:{node}"] = f"{node}: {msg}"
    # information only: refused deep-reorg offers that could not be validated (volume, never an alert)
    info = {}
    for node, st in statuses.items():
        if unvalidated_refusals_note(st):
            info[node] = {"refused_unvalidated_deep_reorgs_24h": int(st["refused_unvalidated_deep_reorgs_24h"]),
                          "note": unvalidated_refusals_note(st)}
    state = {**state, "info": info}
    # outdated peers
    if cfg.get("check_peers"):
        peers = sorted({p for st in statuses.values() for p in (st.get("peers") or [])} - set(statuses))
        mine = {st.get("node") for st in statuses.values()}
        for peer in peers:
            if peer in mine:
                continue
            try:
                pv = fetch(peer.rstrip("/") + "/status").get("version")
            except Exception:
                continue  # unreachable peers are normal on an open network
            min_peer = next((s.get("min_peer_version") for s in statuses.values() if s.get("min_peer_version")), want)
            if _outdated(pv, min_peer):
                left = (deadline - tip) if deadline else None
                urgent = left is not None and left <= cfg.get("deadline_warn_blocks", 500)
                msg = f"Peer {peer} runs {pv or 'an old version'} (needs {min_peer})"
                if left is not None:
                    msg += f"; {max(left, 0)} blocks left before the block-{deadline} cut-off" if left > 0 else \
                           f"; past block {deadline}, it is now cut off"
                problems[f"old_peer{'_urgent' if urgent else ''}:{peer}"] = msg + "."
    # checkpoints + chain rewrites (one /chain fetch from the first healthy node)
    cps = {}
    path = cfg.get("checkpoints_file")
    if path:
        try:
            cps = _load_checkpoints(path)
            if not cps:
                problems["checkpoints_missing"] = f"No checkpoints found in {path}."
        except FileNotFoundError:
            problems["checkpoints_missing"] = f"Checkpoint file {path} is missing."
        except Exception as e:
            problems["checkpoints_missing"] = f"Checkpoint file {path} is unreadable ({type(e).__name__})."
    track = int(cfg.get("reorg_track_blocks", 0) or 0)
    if (cps or track) and statuses:
        node = sorted(statuses)[0]
        try:
            chain = fetch(node.rstrip("/") + "/chain")["chain"]
            hashes = {int(b["index"]): b["hash"] for b in chain}
            for h, want_hash in cps.items():
                if h in hashes and hashes[h] != want_hash:
                    problems[f"checkpoint:{h}"] = f"Block {h} on {node} does not match the checkpoint."
            window = int(cfg.get("miner_share_window", 0) or 0)
            if window:
                tail = chain[-window:]
                problems.update(miner_share_problems(
                    block_miners(tail), cfg.get("miner_share_threshold_pct", 40), min(window, 50),
                    funding_sources(chain, cfg.get("miner_share_ignore_funders")), cfg.get("miner_share_ignore")))
            if track:
                recent = {h: x for h, x in hashes.items() if h > max(hashes) - track}
                prev = {int(k): v for k, v in state.get("recent_hashes", {}).items()}
                msg = reorg_problem(prev, recent, cfg.get("reorg_alert_depth", 3))
                if msg:
                    problems["chain_rewrite"] = msg
                state = {**state, "recent_hashes": {str(k): v for k, v in recent.items()}}
        except Exception as e:
            problems["chain_unreadable"] = f"Could not read the chain from {node} ({type(e).__name__})."
    # pause state
    if cfg.get("check_pause"):
        for node in statuses:
            try:
                ps = fetch(node.rstrip("/") + "/pause_status")
            except Exception:
                continue
            if ps.get("paused"):
                problems[f"paused:{node}"] = f"Payouts are PAUSED on {node} (emergency pause is on)."
            if ps.get("expired"):
                problems[f"pause_expired:{node}"] = f"The emergency pause on {node} has expired."
    # node logs
    units = cfg.get("journal_units") or [u for u in os.environ.get("NETLITHIC_HEALTH_JOURNAL_UNITS", "").split(",") if u]
    if units:
        since = state.get("journal_since", now - 600)
        lines = journal_fn(units, since)
        if lines is None:
            problems["journal_unreadable"] = "Could not read node logs (journalctl); log alerts are blind."
        else:
            for key, pat in cfg.get("journal_patterns", {}).items():
                hits = [ln for ln in lines if pat in ln]
                if hits:
                    problems[f"log_{key}"] = f"Node log: {len(hits)} x \"{pat}\" since last check, e.g.: {hits[-1][:160]}"
            state = {**state, "journal_since": now}
    return problems, state


# ---- reorg and miner-share alerts (both wired into v050_checks) ----
def reorg_problem(prev_hashes, now_hashes, alert_depth=3):
    """prev_hashes/now_hashes = {height: hash} of recent blocks from the last run and now.
    A reorg replaced every height whose hash changed; alert when that many >= alert_depth."""
    changed = [h for h, x in prev_hashes.items() if h in now_hashes and now_hashes[h] != x]
    if len(changed) >= alert_depth:
        return f"Chain rewrite: {len(changed)} blocks replaced (from block {min(changed)})."
    return None


def deep_reorg_problem(status, alert_depth=3):
    """status = a node's /status. Alert when the node accepted a reorg of alert_depth blocks or more in its last 24 h
    (1 or 2 blocks is normal and stays quiet; this is the same threshold as reorg_problem), or refused any chain that
    would have rewritten more than its reorg limit. Nodes older than v4 have no such fields: no alert."""
    try:
        depth = int((status or {}).get("max_reorg_depth_24h") or 0)
    except (TypeError, ValueError):
        depth = 0
    try:
        rejected = int((status or {}).get("rejected_deep_reorgs_24h") or 0)
        rejected_depth = int((status or {}).get("max_rejected_reorg_depth_24h") or 0)
    except (TypeError, ValueError):
        rejected, rejected_depth = 0, 0
    if depth >= int(alert_depth):
        return (f"The node accepted a reorg of {depth} blocks in the last 24 hours (normal is 1 to {int(alert_depth) - 1}; "
                f"{alert_depth} or more alerts, the same threshold as the chain-rewrite check). "
                f"Check docs/INCIDENT_PLAN.md before anything else.")
    if rejected > 0:
        return (f"The node REFUSED {rejected} chain(s) in the last 24 hours that would have rewritten up to {rejected_depth} "
                f"blocks (over its reorg limit). A peer is badly out of sync or attacking. Check docs/INCIDENT_PLAN.md.")
    return None


def unvalidated_refusals_note(status):
    """Information only (never an alert): how many deep-reorg offers the node refused in its last 24 h without being
    able to validate them (junk, or it arrived while the node's validation budget was spent). A few are normal on an
    open network; a steady stream means somebody is probing, and it also means the 'refused deep reorg' alert may be
    quieter than the real situation. Older nodes have no such field."""
    try:
        n = int((status or {}).get("refused_unvalidated_deep_reorgs_24h") or 0)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return (f"{n} deep-rewrite offer(s) refused without a full check in the last 24 hours (information only, not an alert: "
            f"made-up chains from peers, or offers that arrived while the node was busy checking another).")


def block_miners(chain):
    """Reward address of each block in `chain` (a /chain list of block dicts), in order. Blocks without a reward tx
    are skipped."""
    out = []
    for b in chain:
        txs = (b or {}).get("transactions") or []
        if txs and isinstance(txs[0], dict) and txs[0].get("type") == "reward" and txs[0].get("recipient"):
            out.append(txs[0]["recipient"])
    return out


def funding_sources(chain, ignore=()):
    """{address: the first ordinary sender that paid it}. Chain-issued coins (reward, payout, allocation) have no sender
    and are skipped, as are senders in `ignore` (a faucet or exchange that pays everybody would otherwise glue
    unrelated people into one cluster). This is a heuristic: it links addresses that move coins between each other."""
    ignore = set(ignore or ())
    out = {}
    for b in chain:
        for tx in (b or {}).get("transactions") or []:
            p = tx.get("payload") if isinstance(tx, dict) else None
            if not isinstance(p, dict) or tx.get("type") in ("reward", "payout", "allocation"):
                continue
            s_, r_ = p.get("sender"), p.get("recipient")
            if s_ and r_ and s_ != r_ and s_ not in ignore and r_ not in out:
                out[r_] = s_
    return out


def cluster_root(addr, funders, max_hops=25):
    """Follow 'who funded this address' links to the end of the chain of funders (cycle-safe)."""
    seen = {addr}
    cur = addr
    for _ in range(max_hops):
        nxt = funders.get(cur)
        if not nxt or nxt in seen:
            break
        seen.add(nxt)
        cur = nxt
    return cur


def miner_share_problems(miners, threshold_pct=40, min_blocks=50, funders=None, ignore=()):
    """miners = reward addresses of the most recent blocks. Alert when ONE address, or one CLUSTER of addresses that
    share a funding source (`funders` = funding_sources(chain)), mined more than threshold_pct of them.

    Honest limits: this catches a miner that spreads blocks over several addresses it has moved coins between. A miner
    who never links its addresses on chain looks like several small miners and is NOT caught. Because the mining pot
    follows block share (docs/POINTS_V4.md), a high block share is the thing to watch, however many addresses it uses.
    Addresses in `ignore` (the founder's own miners) are left out of the count."""
    ignore = set(ignore or ())
    miners = [m for m in miners if m not in ignore]
    if len(miners) < min_blocks:
        return {}
    funders = funders or {}
    counts, clusters = {}, {}
    for m in miners:
        counts[m] = counts.get(m, 0) + 1
        root = cluster_root(m, funders)
        clusters.setdefault(root, {})
        clusters[root][m] = clusters[root].get(m, 0) + 1
    out = {}
    for m, c in counts.items():
        pct = 100 * c / len(miners)
        if pct > threshold_pct:
            out[f"miner_share:{m}"] = f"One miner ({m[:12]}...) made {pct:.0f}% of the last {len(miners)} blocks."
    for root, members in clusters.items():
        if len(members) < 2:
            continue                                    # a single address was handled above
        c = sum(members.values())
        pct = 100 * c / len(miners)
        if pct > threshold_pct:
            out[f"miner_cluster:{root}"] = (f"{len(members)} addresses funded from the same source ({root[:12]}...) "
                                            f"made {pct:.0f}% of the last {len(miners)} blocks: probably one miner.")
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
    for node, item in sorted((state.get("info") or {}).items()):
        print(f"Info ({node}): {item['note']}")
    if not problems:
        print("healthy")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
