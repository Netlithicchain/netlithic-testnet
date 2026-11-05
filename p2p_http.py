import hmac
import ipaddress
import json
import logging
import strict_json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from urllib.request import Request
from collections import OrderedDict

from netutil import urlopen, is_public_ip, CappedThreadingHTTPServer, normalize_ip, trusted_proxies_from_env

import netlithic_version
import pause
import registry
from core import SECURITY_FORK_HEIGHT, MAX_REORG_DEPTH, Block, Blockchain, block_work, is_security_fork, PreviewBusy, normalize_address, tx_id, normalize_tx, is_reward_tx, is_post_fork, points_v3_rules_hash, difficulty_rules_hash
import economics
from economics import format_points, to_nlt


HOST = "127.0.0.1"
PORT = 8000
ADVERTISE_URL = None  # public URL other nodes should use for us (set via --advertise-url)
HELLO_TIMEOUT_SECONDS = 5
# Whole-request limits (connect + headers + body) for peer fetches, so a slow peer cannot hold a thread open.
PEERS_FETCH_DEADLINE_SECONDS = 15
CHAIN_FETCH_DEADLINE_SECONDS = 120

PEERS_FILE = "peers.json"
MEMPOOL_FILE = "mempool.json"
AUTO_SYNC_SECONDS = 5
SEEN_TTL_SECONDS = 120

# Hardening limits
MAX_PEERS = 32
MAX_PEERS_PER_HOST = 2                  # one machine can't fill the peer list
MAX_MEMPOOL = int(os.environ.get("NETLITHIC_MAX_MEMPOOL", "5000"))
# Per-IP POST rate limit (token bucket). Loopback is exempt (local wallet/desktop app).
RATE_PER_SECOND = float(os.environ.get("NETLITHIC_RATE_PER_SECOND", "5"))
RATE_BURST = float(os.environ.get("NETLITHIC_RATE_BURST", "40"))
RATE_LIMIT_LOOPBACK = os.environ.get("NETLITHIC_RATE_LIMIT_LOOPBACK", "") == "1"
MAX_TRACKED_IPS = 10000
# /payout_preview recomputes a whole period (seconds at full size), so it gets its own, much smaller per-IP budget
# (GET included). Loopback is exempt like the POST limit. Answers are also cached per chain tip (core.payout_preview_cached).
PREVIEW_RATE_PER_SECOND = float(os.environ.get("NETLITHIC_PREVIEW_RATE_PER_SECOND", "0.2"))   # 12 per minute
PREVIEW_RATE_BURST = float(os.environ.get("NETLITHIC_PREVIEW_RATE_BURST", "5"))
MAX_POST_BYTES = 1 * 1024 * 1024        # any POST body (tx, block, peer)
MAX_FETCH_BYTES = 16 * 1024 * 1024      # a /chain or /peers response from a peer
ADMIN_TOKEN_ENV = "NETLITHIC_ADMIN_TOKEN"
ADMIN_TOKEN_HEADER = "X-Admin-Token"

# Review F1: a /receive_block "from_peer" may trigger a sync only if it is a
# peer we already know, resolves only to public addresses, and hasn't
# triggered one recently. NETLITHIC_ALLOW_PRIVATE_PEERS=1 lifts the address
# check for local multi-node test setups (127.0.0.1 / LAN).
SYNC_TRIGGER_MIN_SECONDS = float(os.environ.get("NETLITHIC_SYNC_TRIGGER_SECONDS", "10"))
ALLOW_PRIVATE_PEERS = os.environ.get("NETLITHIC_ALLOW_PRIVATE_PEERS", "") == "1"


blockchain = Blockchain()
peers = set()
mempool = []

seen_blocks = {}
seen_txs = {}

_auto_sync_thread = None
_auto_sync_running = False
state_lock = threading.Lock()
mine_lock = threading.Lock()   # serialises /mine searches; always taken BEFORE state_lock, never while holding it


# ----------------------------
# Helpers
# ----------------------------

def chain_id() -> str:
    """Our network id (from config/genesis_config.json via Economics)."""
    return blockchain.econ.chain_id


def _foreign_chain(data) -> bool:
    """True if a peer message says it belongs to a different network.
    Messages without chain_id are still checked by signature/genesis rules."""
    cid = data.get("chain_id") if isinstance(data, dict) else None
    return cid is not None and cid != chain_id()

BLOCK_INDEX_RE = re.compile(r"[0-9]{1,12}")   # GET /block?index=N: ASCII digits only


def _encode_json(handler, status_code, payload):
    try:
        # Strict JSON: never emit the non-standard number tokens (not-a-number, infinity) that a browser's JSON.parse rejects.
        return status_code, json.dumps(payload, indent=2, allow_nan=False).encode("utf-8")
    except ValueError:
        # A value that is not a real number reached the output; answer with a fixed, valid error instead of crashing.
        print(f"Refused to send a response containing a non-finite number ({getattr(handler, 'path', '?')})")
        return 500, b'{"error": "Internal error: response contained a value that is not a valid number"}'


def _write_reply(handler, status_code, body):
    handler._reply_size_hint = len(body)    # sizes the reply deadline (netutil._DeadlineWriter)
    try:
        handler.send_response(status_code)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
    except OSError:
        # client went away, or the reply deadline shut a stalled connection
        handler.close_connection = True


def _json_response(handler, status_code, payload):
    """Encode the reply now (so it is a snapshot of state taken under whatever lock the
    caller holds) but, for node handlers, send it only after the handler returns and every
    lock is released. Network writes never happen while state_lock or mine_lock is held."""
    status_code, body = _encode_json(handler, status_code, payload)
    if getattr(handler, "_defer_replies", False):
        handler._pending_reply = (status_code, body)
        return
    _write_reply(handler, status_code, body)


def _chain_body():
    """GET /chain body, cached per tip. Call with state_lock held."""
    tip = blockchain.get_latest_block()
    key = (len(blockchain.chain), getattr(tip, "hash", None))
    cached = _chain_cache.get("v")
    if cached is not None and cached[0] == key:
        return cached[1]
    _code, body = _encode_json(None, 200, {
        "chain_id": chain_id(),
        "length": len(blockchain.chain),
        "work": blockchain.chain_work(),
        "chain": blockchain.to_list(),
    })
    if _code == 200:
        _chain_cache["v"] = (key, body)
    return body


_chain_cache = {}


def strict_json_loads(text):
    """Strict JSON parsing for everything we receive (see strict_json.py)."""
    return strict_json.loads(text)


class BodyTooLarge(ValueError):
    pass


def _read_json(handler):
    length = int(handler.headers.get("Content-Length", 0))
    if length < 0:
        raise ValueError("Negative Content-Length")
    if length > MAX_POST_BYTES:
        raise BodyTooLarge(f"Body over {MAX_POST_BYTES} bytes")
    raw = handler.rfile.read(length) if length > 0 else b"{}"
    return strict_json_loads(raw.decode("utf-8"))


DEV_LOCAL_ADMIN_ENV = "NETLITHIC_DEV_LOCAL_ADMIN"


def _is_loopback(client_ip: str) -> bool:
    try:
        return ipaddress.ip_address(client_ip).is_loopback
    except ValueError:
        return False


def is_admin_request(client_ip: str, headers) -> bool:
    """Admin endpoints (/mine, /add_peer) need X-Admin-Token equal to the
    NETLITHIC_ADMIN_TOKEN env var on EVERY request, including from localhost
    (a local reverse proxy or gateway must not grant admin by accident).

    With no token configured, admin endpoints are disabled, except on a dev
    chain (chain_id starting with "netlithic-dev") when NETLITHIC_DEV_LOCAL_ADMIN=1
    is set: then loopback callers are allowed without a token."""
    expected = os.environ.get(ADMIN_TOKEN_ENV, "")
    if expected:
        given = headers.get(ADMIN_TOKEN_HEADER, "") or ""
        return hmac.compare_digest(given.encode(), expected.encode())
    return (
        os.environ.get(DEV_LOCAL_ADMIN_ENV, "") == "1"
        and chain_id().startswith("netlithic-dev")
        and _is_loopback(client_ip)
    )


def _normalize_peer(peer: str) -> str:
    """Return 'scheme://host:port' or raise ValueError.
    Only http/https, a host, an optional port; no credentials, path or query."""
    if not isinstance(peer, str):
        raise ValueError("Peer must be a string")
    peer = peer.strip()
    if "://" not in peer:
        peer = "http://" + peer
    peer = peer.rstrip("/")

    parsed = urlparse(peer)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Peer URL must be http or https")
    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Invalid peer host")
    if parsed.path or parsed.query or parsed.fragment:
        raise ValueError("Peer URL must not have a path or query")
    port = parsed.port  # raises ValueError if not a valid port number

    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    return f"{parsed.scheme}://{host}" + (f":{port}" if port else "")


LOCAL_HOSTNAMES = {"127.0.0.1", "localhost", "0.0.0.0", "[::1]"}  # nosec B104 - matches peer URLs to reject, never binds


def _is_loopback_peer(peer_url: str) -> bool:
    host = (urlparse(peer_url).hostname or "").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def shared_peers(client_ip: str) -> list:
    """Peers to show a caller. Loopback peers (127.x, ::1, localhost) only make
    sense on this machine, so they're hidden from non-local callers."""
    if _is_loopback(client_ip):
        return sorted(peers)
    return sorted(pr for pr in peers if not _is_loopback_peer(pr))


def _is_self(peer_url: str) -> bool:
    parsed = urlparse(peer_url)
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if ADVERTISE_URL and peer_url == ADVERTISE_URL:
        return True
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port != PORT:
        return False
    return host in LOCAL_HOSTNAMES or host == str(HOST).lower()


_rate_buckets = OrderedDict()
_rate_lock = threading.Lock()


def _bucket_key(client_ip: str) -> str:
    """The key a client is counted under. An IPv6 /64 is one customer (one subscriber is handed a whole /64, so
    counting single addresses would give an attacker billions of fresh budgets); IPv4-mapped IPv6 counts as IPv4."""
    try:
        addr = ipaddress.ip_address(str(client_ip).split("%", 1)[0])
    except ValueError:
        return str(client_ip)
    if addr.version == 6:
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)


def _take_token(buckets, client_ip: str, burst: float, per_second: float, now: float) -> bool:
    """Token bucket per client in a bounded LRU table: when it is full the least recently seen client is forgotten,
    one at a time. (The table used to be wiped wholesale at MAX_TRACKED_IPS, which reset everybody's budget at once.)
    Returns True if the client is over its budget. The caller holds _rate_lock."""
    key = _bucket_key(client_ip)
    tokens, last = buckets.get(key, (burst, now))
    tokens = min(burst, tokens + max(0.0, now - last) * per_second)
    over = tokens < 1
    buckets[key] = (tokens if over else tokens - 1, now)
    buckets.move_to_end(key)
    while len(buckets) > MAX_TRACKED_IPS:
        buckets.popitem(last=False)
    return over


def rate_limited(client_ip: str, now: float = None) -> bool:
    """True if this IP is over its POST budget. Token bucket per IP (per /64 for IPv6)."""
    if _is_loopback(client_ip) and not RATE_LIMIT_LOOPBACK:
        return False
    now = time.time() if now is None else now
    with _rate_lock:
        return _take_token(_rate_buckets, client_ip, RATE_BURST, RATE_PER_SECOND, now)


_preview_buckets = OrderedDict()


def preview_rate_limited(client_ip: str, now: float = None) -> bool:
    """True if this IP is over its /payout_preview budget (token bucket per IP / IPv6 /64, any method)."""
    if _is_loopback(client_ip) and not RATE_LIMIT_LOOPBACK:
        return False
    now = time.time() if now is None else now
    with _rate_lock:
        return _take_token(_preview_buckets, client_ip, PREVIEW_RATE_BURST, PREVIEW_RATE_PER_SECOND, now)


def _peer_host(peer_url: str) -> str:
    return (urlparse(peer_url).hostname or "").lower()


def _try_add_peer(peer: str, address_ok: bool = None) -> str:
    """Validate and add a peer (caller holds state_lock).
    Returns 'added', 'known', or raises ValueError with the reason.
    Review P5-1: never resolve DNS while holding state_lock. Callers that hold
    the lock pass `address_ok`, the result of peer_address_allowed() computed
    BEFORE taking the lock; only lock-free startup code may leave it None."""
    normalized = _normalize_peer(peer)
    if _is_self(normalized):
        raise ValueError("Cannot add self as peer")
    if normalized in peers:
        return "known"
    # Review R1: every way into the peer table (/hello, /add_peer, --peer,
    # peers.json, discovery) must resolve only to public addresses.
    if address_ok is None:
        address_ok = peer_address_allowed(normalized)
    if not address_ok:
        raise ValueError("Peer address not allowed (private, loopback, link-local or unresolvable); "
                         "set NETLITHIC_ALLOW_PRIVATE_PEERS=1 for a local test network")
    if len(peers) >= MAX_PEERS:
        raise ValueError(f"Peer limit reached ({MAX_PEERS})")
    host = _peer_host(normalized)
    if not _is_loopback_peer(normalized) and \
            sum(1 for p in peers if _peer_host(p) == host) >= MAX_PEERS_PER_HOST:
        raise ValueError(f"Too many peers from one host (max {MAX_PEERS_PER_HOST})")
    peers.add(normalized)
    return "added"


# ---- Peer gate (review F2). PEER LEVEL ONLY: it chooses who we talk to and never changes which blocks are valid. ----
GATE_RECHECK_SECONDS = float(os.environ.get("NETLITHIC_GATE_RECHECK_SECONDS", "60"))   # per peer, from block 4900
GATE_MAX_VERIFY_PER_DISCOVERY = 8       # gossip candidates we fetch /status from in one discovery round
GATE_REJECT_TTL_SECONDS = 600           # a gossip candidate that failed the gate is not fetched again for this long
GATE_REJECT_MAX = 512
_gate_rejected = OrderedDict()          # url -> time it failed (bounded LRU)
_gate_checked_at = {}                   # known peer url -> last recheck time
_gate_lock = threading.Lock()


def _gate_error(status, height: int):
    """Run the handshake gate on a peer's /status answer. Returns (error_body or None, upgrade_needed).
    Order and wording are those of /hello: chain id, points-rules fingerprint, version. Below
    UPGRADE_DEADLINE_HEIGHT old builds are let in (flagged), from it they are refused."""
    if not isinstance(status, dict):
        return {"error": "Could not verify peer: /status is not a JSON object"}, False
    if status.get("chain_id") != chain_id():
        return {"error": f"Peer is on a different chain_id (we are {chain_id()})"}, False
    peer_rules = status.get("points_v3_rules")
    if peer_rules is None or not isinstance(peer_rules, str):
        # fail closed (review M1): from the deadline height a peer must show its rules fingerprint
        if netlithic_version.rules_field_required(height):
            return {
                "error": ("Points rules fingerprint missing: from block "
                          f"{netlithic_version.RULES_FIELD_REQUIRED_HEIGHT} every peer must report "
                          "points_v3_rules in /status (Netlithic "
                          f"{netlithic_version.MIN_PEER_VERSION} or newer)."),
                "upgrade_needed": True,
                "min_version": netlithic_version.MIN_PEER_VERSION,
            }, False
    elif peer_rules != points_v3_rules_hash():
        return {
            "error": "Points rules mismatch: this peer computes community payouts differently from this node "
                     "(different Netlithic build). Update both to the same release.",
            "points_v3_rules": points_v3_rules_hash(),
        }, False
    peer_diff = status.get("difficulty_rules")
    if peer_diff is not None and peer_diff != difficulty_rules_hash():
        # only a peer that reports the field is compared (0.5.4 and newer); an older build is handled by the version gate
        return {
            "error": "Difficulty rules mismatch: this peer follows different mining-difficulty rules from this node "
                     "(different Netlithic build). Update both to the same release.",
            "difficulty_rules": difficulty_rules_hash(),
        }, False
    peer_version = status.get("version")
    if netlithic_version.is_outdated(peer_version):
        if height >= netlithic_version.UPGRADE_DEADLINE_HEIGHT:
            return {
                "error": (f"Upgrade needed: this network requires Netlithic {netlithic_version.MIN_PEER_VERSION} "
                          f"or newer from block {netlithic_version.UPGRADE_DEADLINE_HEIGHT} "
                          f"(you run {str(peer_version)[:40] if peer_version else 'an older version'})"),
                "upgrade_needed": True,
                "min_version": netlithic_version.MIN_PEER_VERSION,
            }, False
        return None, True
    return None, False


def _tip_height() -> int:
    with state_lock:
        return blockchain.get_latest_block().index


def _gate_check_url(url: str, strict: bool):
    """Fetch url/status (no lock held; the address must already have passed peer_address_allowed) and run the gate.
    Returns None if the peer passes, else a reason string. An unreachable peer passes unless strict=True."""
    try:
        status = _get_json(f"{url}/status", timeout=HELLO_TIMEOUT_SECONDS,
                           deadline_seconds=HELLO_TIMEOUT_SECONDS)
    except Exception:
        return "Could not verify peer: /status unreachable" if strict else None
    err, _ = _gate_error(status, _tip_height())
    return err["error"] if err else None


def _gate_remember_reject(url: str):
    with _gate_lock:
        _gate_rejected[url] = time.time()
        _gate_rejected.move_to_end(url)
        while len(_gate_rejected) > GATE_REJECT_MAX:
            _gate_rejected.popitem(last=False)


def _gate_recently_rejected(url: str) -> bool:
    with _gate_lock:
        t = _gate_rejected.get(url)
        if t is None:
            return False
        if time.time() - t > GATE_REJECT_TTL_SECONDS:
            _gate_rejected.pop(url, None)
            return False
        return True


def _drop_peer(url: str, reason: str):
    with state_lock:
        if url in peers:
            peers.discard(url)
            _save_peers()
    _gate_remember_reject(url)
    print(f"Dropped peer {url}: {reason}")


def _recheck_known_peers():
    """From block 4900 on, run the gate again on peers that were let in earlier (below 4900, before this build, or
    before they downgraded). An unreachable peer is left alone (the sync loop copes with dead peers)."""
    if _tip_height() < netlithic_version.UPGRADE_DEADLINE_HEIGHT:
        return
    now = time.time()
    for peer in list(peers):
        with _gate_lock:
            if now - _gate_checked_at.get(peer, 0) < GATE_RECHECK_SECONDS:
                continue
            _gate_checked_at[peer] = now
        reason = _gate_check_url(peer, strict=False)
        if reason:
            _drop_peer(peer, reason)


_sync_trigger_last = OrderedDict()   # LRU: oldest entries evicted, never a full reset
_sync_trigger_lock = threading.Lock()


def _resolve_host_ips(host: str) -> list:
    import socket
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return [ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos]


def _is_public_ip(ip) -> bool:
    return is_public_ip(ip)


def fetch_ip_allowed(ip) -> bool:
    """Connect-time policy for every outbound peer fetch (review R2): the
    address the socket actually connected to must be public."""
    return ALLOW_PRIVATE_PEERS or is_public_ip(ip)


# Review F9: /hello is open to anyone, so its DNS lookup runs in a small
# bounded worker pool with a short timeout, and each client IP may have only a
# couple of lookups in flight. IP literals skip the pool entirely.
HELLO_DNS_TIMEOUT_SECONDS = float(os.environ.get("NETLITHIC_HELLO_DNS_TIMEOUT", "2"))
HELLO_DNS_WORKERS = 4
HELLO_DNS_MAX_PENDING = 16
HELLO_DNS_PER_CLIENT = 2
_hello_dns_pool = None
_hello_dns_slots = threading.BoundedSemaphore(HELLO_DNS_MAX_PENDING)
_hello_dns_inflight = {}
_hello_dns_lock = threading.Lock()
HELLO_DNS_NEG_CACHE_SECONDS = 60
HELLO_DNS_NEG_CACHE_MAX = 1024
_hello_dns_neg = OrderedDict()  # host -> time a lookup failed/timed out/was refused


def _client_key(client_ip: str) -> str:
    """Per-client limit key: IPv6 clients are grouped by /64 (one subscriber
    usually owns a whole /64), IPv4 by address. Review P5-2."""
    try:
        ip = ipaddress.ip_address(str(client_ip).split("%")[0])
    except ValueError:
        return str(client_ip)
    if ip.version == 6:
        if ip.ipv4_mapped:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def _neg_cached(host: str, now: float) -> bool:
    with _hello_dns_lock:
        t = _hello_dns_neg.get(host)
        if t is None:
            return False
        if now - t > HELLO_DNS_NEG_CACHE_SECONDS:
            _hello_dns_neg.pop(host, None)
            return False
        return True


def _neg_remember(host: str, now: float) -> None:
    with _hello_dns_lock:
        _hello_dns_neg[host] = now
        _hello_dns_neg.move_to_end(host)
        while len(_hello_dns_neg) > HELLO_DNS_NEG_CACHE_MAX:
            _hello_dns_neg.popitem(last=False)


def _hello_pool():
    global _hello_dns_pool
    with _hello_dns_lock:
        if _hello_dns_pool is None:
            from concurrent.futures import ThreadPoolExecutor
            _hello_dns_pool = ThreadPoolExecutor(max_workers=HELLO_DNS_WORKERS, thread_name_prefix="hello-dns")
        return _hello_dns_pool


def hello_address_allowed(peer_url: str, client_ip: str = "?", resolver=None, timeout: float = None):
    """peer_address_allowed() for /hello, without tying up the request thread
    for a slow resolver. Returns (allowed, reason)."""
    if ALLOW_PRIVATE_PEERS:
        return True, "ok"
    host = (urlparse(peer_url).hostname or "").strip("[]")
    try:
        ipaddress.ip_address(host)
        literal = True
    except ValueError:
        literal = False
    if literal or not host:
        ok = peer_address_allowed(peer_url, resolver=resolver)
        return ok, "ok" if ok else "Peer address not allowed (private, loopback, link-local or unresolvable)"
    host = host.lower()
    now = time.time()
    if _neg_cached(host, now):
        return False, "Peer address recently failed lookup; try again later"
    client_ip = _client_key(client_ip)
    with _hello_dns_lock:
        if _hello_dns_inflight.get(client_ip, 0) >= HELLO_DNS_PER_CLIENT:
            return False, "Too many peer lookups in progress from your address; try again shortly"
        if not _hello_dns_slots.acquire(blocking=False):
            return False, "Peer lookups are busy; try again shortly"
        _hello_dns_inflight[client_ip] = _hello_dns_inflight.get(client_ip, 0) + 1

    def _done(f=None):
        # P5-4: only a lookup that really finished (resolver error -> False, or
        # non-public answer) is negative-cached; a timeout or cancel never is,
        # so a flood of slow names can't get a legitimate host blocked.
        try:
            if f is not None and not f.cancelled() and f.exception() is None and f.result() is False:
                _neg_remember(host, time.time())
        except Exception:  # nosec B110 - cache bookkeeping must never break slot release
            pass
        with _hello_dns_lock:
            n = _hello_dns_inflight.get(client_ip, 1) - 1
            if n <= 0:
                _hello_dns_inflight.pop(client_ip, None)
            else:
                _hello_dns_inflight[client_ip] = n
        _hello_dns_slots.release()

    try:
        fut = _hello_pool().submit(peer_address_allowed, peer_url, resolver)
    except Exception:
        _done()
        return False, "Peer lookups are unavailable"
    fut.add_done_callback(_done)  # slot freed only when the lookup really ends
    try:
        ok = fut.result(timeout=HELLO_DNS_TIMEOUT_SECONDS if timeout is None else timeout)
    except Exception:
        fut.cancel()  # a still-queued lookup never runs; its slot is freed via _done (P5-2)
        return False, "Peer address lookup timed out"  # not negative-cached (P5-4), "Peer address lookup timed out"
    if not ok:
        _neg_remember(host, time.time())
    return ok, "ok" if ok else "Peer address not allowed (private, loopback, link-local or unresolvable)"


def peer_address_allowed(peer_url: str, resolver=None) -> bool:
    """True if every address the peer's host resolves to is public (no
    private, loopback, link-local, multicast, reserved). Unresolvable = no."""
    if ALLOW_PRIVATE_PEERS:
        return True
    host = (urlparse(peer_url).hostname or "").strip("[]")
    if not host:
        return False
    try:
        ips = (resolver or _resolve_host_ips)(host)
    except Exception:
        return False
    return bool(ips) and all(_is_public_ip(ip) for ip in ips)


def sync_trigger_allowed(peer_url, now: float = None, resolver=None) -> tuple:
    """Gate for a sync triggered by an incoming /receive_block (review F1).
    Returns (allowed, reason)."""
    if not peer_url:
        return False, "no from_peer"
    with state_lock:
        known = peer_url in peers
    if not known:
        return False, "from_peer is not a known peer"
    if not peer_address_allowed(peer_url, resolver=resolver):
        return False, "from_peer resolves to a private/loopback/link-local address"
    now = time.time() if now is None else now
    with _sync_trigger_lock:
        last = _sync_trigger_last.get(peer_url)
        if last is not None and now - last < SYNC_TRIGGER_MIN_SECONDS:
            return False, "sync trigger rate-limited"
        _sync_trigger_last[peer_url] = now
        _sync_trigger_last.move_to_end(peer_url)
        while len(_sync_trigger_last) > MAX_PEERS * 4:
            _sync_trigger_last.popitem(last=False)
    return True, "ok"


def _save_peers():
    with open(PEERS_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(peers), f, indent=2)


def _load_peers():
    global peers

    if not os.path.exists(PEERS_FILE):
        return

    try:
        with open(PEERS_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)

        peers = set()
        for p in loaded:
            try:
                _try_add_peer(p)
            except Exception:
                continue
    except Exception:
        peers = set()


def _save_mempool():
    with open(MEMPOOL_FILE, "w", encoding="utf-8") as f:
        json.dump(mempool, f, indent=2)


def _load_mempool():
    global mempool

    if not os.path.exists(MEMPOOL_FILE):
        return

    try:
        with open(MEMPOOL_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)

        if not isinstance(loaded, list):
            mempool = []
            return

        cleaned = []
        seen = set()

        for tx in loaded:
            try:
                normalized = normalize_tx(tx)
                tid = tx_id(normalized)
                if tid in seen:
                    continue
                if is_reward_tx(normalized):
                    continue
                cleaned.append(normalized)
                seen.add(tid)
            except Exception:
                continue

        mempool = cleaned
    except Exception:
        mempool = []


def _revalidate_mempool():
    global mempool

    cleaned = []
    for tx in mempool:
        try:
            tx = normalize_tx(tx)
            if blockchain.validate_candidate_tx_against_chain_and_mempool(tx, cleaned):
                cleaned.append(tx)
        except Exception:
            continue

    mempool = cleaned
    _save_mempool()


def _now() -> float:
    return time.time()


def _prune_seen():
    cutoff = _now() - SEEN_TTL_SECONDS

    for cache in (seen_blocks, seen_txs):
        old_keys = [k for k, ts in cache.items() if ts < cutoff]
        for key in old_keys:
            cache.pop(key, None)


def _mark_seen_block(block_hash: str):
    seen_blocks[block_hash] = _now()


def _mark_seen_tx(tx_hash: str):
    seen_txs[tx_hash] = _now()


def _already_seen_block(block_hash: str) -> bool:
    _prune_seen()
    return block_hash in seen_blocks


def _already_seen_tx(tx_hash: str) -> bool:
    _prune_seen()
    return tx_hash in seen_txs


def _post_json(url: str, payload: dict, timeout: int = 5):
    data = json.dumps(payload).encode("utf-8")
    req = Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=timeout, allow_ip=fetch_ip_allowed) as resp:
        body = resp.read(MAX_FETCH_BYTES + 1)
        if len(body) > MAX_FETCH_BYTES:
            raise ValueError(f"Peer response over {MAX_FETCH_BYTES} bytes")
        body = body.decode("utf-8")
        return strict_json_loads(body) if body else {}


def _read_capped(resp, deadline=None):
    """Read a peer reply, at most MAX_FETCH_BYTES + 1 bytes. With a deadline (time.monotonic() value) the WHOLE
    read must finish by then, so a peer that sends a byte at a time cannot hold the call open."""
    if deadline is None:
        return resp.read(MAX_FETCH_BYTES + 1)
    sock = getattr(getattr(getattr(resp, "fp", None), "raw", None), "_sock", None)
    chunks, size = [], 0
    while size <= MAX_FETCH_BYTES:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("Peer reply took too long")
        if getattr(resp, "isclosed", lambda: False)():
            break
        if sock is not None:
            try:
                sock.settimeout(left)
            except OSError:
                pass
        chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    if time.monotonic() > deadline:
        raise TimeoutError("Peer reply took too long")
    return b"".join(chunks)


def _get_json(url: str, timeout: int = 5, deadline_seconds: float = None):
    """deadline_seconds, when given, bounds the whole call with one deadline: connect, response headers and
    body together, not each read separately."""
    req = Request(url, headers={"Accept": "application/json"})
    deadline = None if deadline_seconds is None else time.monotonic() + deadline_seconds
    with urlopen(req, timeout=timeout, allow_ip=fetch_ip_allowed, deadline=deadline) as resp:
        try:
            body = _read_capped(resp, deadline)
        finally:
            getattr(resp, "cancel_deadline", lambda: None)()
        if len(body) > MAX_FETCH_BYTES:
            raise ValueError(f"Peer response over {MAX_FETCH_BYTES} bytes")
        body = body.decode("utf-8")
        return strict_json_loads(body) if body else {}


def self_url() -> str:
    if ADVERTISE_URL:
        return ADVERTISE_URL
    return f"http://{HOST}:{PORT}"


def _advertised_sender():
    """Our URL for a relayed block's from_peer, or None when we only listen on
    loopback (a desktop node). Nobody else can reach that address, so naming it
    would make receivers try to fetch from themselves; leaving it out is
    harmless because a receiver only syncs from a sender it already knows."""
    if ADVERTISE_URL or ALLOW_PRIVATE_PEERS:
        return self_url()
    host = (HOST or "").strip("[]")
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host.lower() == "localhost"
    return None if loopback else self_url()


def _tx_exists_in_mempool(tx: dict) -> bool:
    tid = tx_id(tx)
    return any(tx_id(existing) == tid for existing in mempool)


def _remove_confirmed_txs_from_mempool():
    global mempool

    confirmed_ids = set()

    for block in blockchain.chain:
        for tx in block.transactions:
            try:
                confirmed_ids.add(tx_id(tx))
            except Exception:
                continue

    remaining = []
    for tx in mempool:
        try:
            if tx_id(tx) not in confirmed_ids:
                remaining.append(tx)
        except Exception:
            continue

    mempool = remaining
    _save_mempool()


def _introduce_to(peer_url: str):
    """Ask a peer to add us back via /hello (no admin token needed; the peer
    verifies our URL answers /status), then learn its peers and sync."""
    try:
        _post_json(f"{peer_url}/hello", {"url": self_url(), "chain_id": chain_id()})
    except Exception:
        pass
    _discover_peers_from(peer_url)
    _sync_with_peer(peer_url)


def _discover_peers_from(peer_url: str):
    try:
        data = _get_json(f"{peer_url}/peers", deadline_seconds=PEERS_FETCH_DEADLINE_SECONDS)
        remote_peers = data.get("peers", [])
        changed = False

        # P5-1: resolve outside state_lock (bounded list, strings only)
        checked = []
        for p in [x for x in remote_peers if isinstance(x, str)][:MAX_PEERS * 2]:
            try:
                n = _normalize_peer(p)
            except Exception:
                continue
            with state_lock:
                if n in peers or len(peers) >= MAX_PEERS:
                    continue
            if _gate_recently_rejected(n) or len(checked) >= GATE_MAX_VERIFY_PER_DISCOVERY:
                continue
            ok = peer_address_allowed(n)
            # review F2: a gossiped peer gets the same /status gate as a /hello peer, and must answer it
            if ok and _gate_check_url(n, strict=True):
                _gate_remember_reject(n)
                continue
            checked.append((n, ok))

        with state_lock:
            for p, ok in checked:
                try:
                    if _try_add_peer(p, address_ok=ok) == "added":
                        changed = True
                except Exception:
                    continue

            if changed:
                _save_peers()
    except Exception:
        pass


# New blocks are passed on to all peers AT THE SAME TIME, so one slow or dead peer cannot hold up the others.
# Everything is bounded: a fixed pool of worker threads, a short timeout per peer, one overall deadline for the
# call, and a cap on sends waiting for a worker. No new endpoint: it is the same /receive_block call as before.
RELAY_WORKERS = 16
RELAY_PEER_TIMEOUT = 3          # seconds per peer
RELAY_DEADLINE = 6              # seconds the caller waits for the whole round at most
RELAY_MAX_PENDING = 64          # sends allowed in flight or queued; more are skipped (peers sync on their own)
_relay_pool = None
_relay_lock = threading.Lock()
_relay_pending = 0
_relay_skipped_total = 0       # peers not sent to because RELAY_MAX_PENDING was reached (counted and logged)


def _relay_executor():
    global _relay_pool
    with _relay_lock:
        if _relay_pool is None:
            from concurrent.futures import ThreadPoolExecutor
            _relay_pool = ThreadPoolExecutor(max_workers=RELAY_WORKERS, thread_name_prefix="relay")
        return _relay_pool


def _relay_send_one(peer: str, payload: dict) -> None:
    global _relay_pending
    try:
        _post_json(f"{peer}/receive_block", payload, timeout=RELAY_PEER_TIMEOUT)
    except Exception:
        pass
    finally:
        with _relay_lock:
            _relay_pending -= 1


def _broadcast_block(block_data: dict, exclude_peer: str = None):
    global _relay_pending, _relay_skipped_total
    block_hash = block_data.get("hash")
    if isinstance(block_hash, str):
        _mark_seen_block(block_hash)

    payload = {
        "block": block_data,
        "from_peer": _advertised_sender(),
        "chain_id": chain_id(),
    }
    pool = _relay_executor()
    futures = []
    targets = [p for p in list(peers) if not (exclude_peer and p == exclude_peer)]
    for position, peer in enumerate(targets):
        with _relay_lock:
            if _relay_pending >= RELAY_MAX_PENDING:
                # Not silent: the peers left over (this one and all after it) are counted and logged. They still
                # learn about the block on their own, through their normal sync.
                skipped = len(targets) - position
                _relay_skipped_total += skipped
                print(f"Relay: {RELAY_MAX_PENDING} sends already pending, skipped {skipped} of {len(targets)} peers "
                      f"for block {block_data.get('index')} ({_relay_skipped_total} skipped in total)")
                break
            _relay_pending += 1
        try:
            futures.append(pool.submit(_relay_send_one, peer, payload))
        except Exception:
            with _relay_lock:
                _relay_pending -= 1

    if futures:
        from concurrent.futures import wait
        _done, not_done = wait(futures, timeout=RELAY_DEADLINE)
        dropped = 0
        for f in not_done:
            if f.cancel():      # a send that never got a worker is dropped; running ones end at their own timeout
                dropped += 1
                with _relay_lock:
                    _relay_pending -= 1
        if dropped:
            with _relay_lock:
                _relay_skipped_total += dropped
            print(f"Relay: {dropped} sends never got a worker before the {RELAY_DEADLINE} s deadline and were dropped "
                  f"for block {block_data.get('index')}")


def _requeue_orphaned_txs(old_chain):
    """After a reorg, put non-reward txs from the abandoned blocks back in the mempool.

    Without this a transfer, or a signed pause/resume, that was only in an orphaned block
    silently disappears from this node. The caller revalidates the mempool afterwards, so
    anything the new chain makes invalid (already confirmed, bad nonce, no funds) is dropped.
    """
    global mempool

    confirmed_ids = set()
    for block in blockchain.chain:
        for tx in block.transactions:
            try:
                confirmed_ids.add(tx_id(tx))
            except Exception:
                continue

    present = set()
    for tx in mempool:
        try:
            present.add(tx_id(tx))
        except Exception:
            continue

    requeue = []
    for block in old_chain:
        for tx in block.transactions:
            try:
                tx = normalize_tx(tx)
                if is_reward_tx(tx):
                    continue
                tid = tx_id(tx)
            except Exception:
                continue
            if tid in confirmed_ids or tid in present:
                continue
            present.add(tid)
            requeue.append(tx)

    if requeue:
        mempool = (requeue + mempool)[:MAX_MEMPOOL]


def _broadcast_tx(tx: dict, exclude_peer: str = None):
    tid = tx_id(tx)
    _mark_seen_tx(tid)

    for peer in list(peers):
        if exclude_peer and peer == exclude_peer:
            continue

        try:
            _post_json(
                f"{peer}/receive_tx",
                {
                    "tx": tx,
                    "from_peer": self_url(),
                    "chain_id": chain_id(),
                },
            )
        except Exception:
            continue


# Sync is checked in stages so that a peer's offer costs us the node lock only for the final, cheap swap:
#   1. same length and tip as ours: nothing to do;
#   2. a tip this peer already offered and that we rejected: skipped until its backoff runs out;
#   3. cheap structure and proof-of-work check of the blocks that differ from ours (no lock);
#   4. work compared, using only blocks that passed step 3 (so every difficulty is one consensus allows);
#   5. full validation of the offered chain (no lock; the verdict depends only on the offered chain);
#   6. under the lock: replace_chain re-checks depth and work against our chain as it is NOW, then swaps.
# A peer's claims can only make us skip ITS offer; adopting a chain always needs steps 3-6 to pass.
SYNC_BAD_TIP_BACKOFF_MIN = 60.0
SYNC_BAD_TIP_BACKOFF_MAX = 3600.0
SYNC_BAD_TIP_KEEP = 256
_sync_bad_tips = OrderedDict()       # peer -> (tip hash, retry-after monotonic time, current backoff)
_sync_stats_lock = threading.Lock()
_sync_stats = {"identical": 0, "backoff": 0, "precheck_failed": 0, "lower_work": 0,
               "lower_work_deep_forks": 0, "deep_refused": 0, "invalid": 0, "adopted": 0}
_log = logging.getLogger("netlithic.sync")
_sync_last_deep_fork = OrderedDict()  # peer -> depth of the last deep lower-work fork it offered (bounded, see below)
SYNC_DEEP_FORK_KEEP = 256            # same bound as _sync_bad_tips; the oldest entries are dropped first


def _sync_note_deep_fork(peer: str, depth: int) -> None:
    """Remember that `peer` sits on a deep lower-work fork. Bounded to SYNC_DEEP_FORK_KEEP entries and kept only
    for peers we still know, so a stream of made-up peer names cannot grow it without limit."""
    with _sync_stats_lock:
        _sync_last_deep_fork[peer] = depth
        _sync_last_deep_fork.move_to_end(peer)
        while len(_sync_last_deep_fork) > SYNC_DEEP_FORK_KEEP:
            _sync_last_deep_fork.popitem(last=False)


def _sync_prune_deep_forks(known_peers) -> None:
    """Drop entries for peers we no longer have (called once per sync round)."""
    known = set(known_peers)
    with _sync_stats_lock:
        for p in [p for p in _sync_last_deep_fork if p not in known]:
            del _sync_last_deep_fork[p]


def _sync_count(key: str, n: int = 1) -> None:
    with _sync_stats_lock:
        _sync_stats[key] = _sync_stats.get(key, 0) + n


def _sync_info() -> dict:
    with _sync_stats_lock:
        out = {f"sync_skipped_{k}": v for k, v in _sync_stats.items() if k != "adopted"}
        out["sync_adopted"] = _sync_stats.get("adopted", 0)
        out["sync_peers_on_deep_lower_work_fork"] = len(_sync_last_deep_fork)
    return out


def _sync_backoff_active(peer: str, tip) -> bool:
    with _sync_stats_lock:
        entry = _sync_bad_tips.get(peer)
        return entry is not None and entry[0] == tip and time.monotonic() < entry[1]


def _sync_mark_bad(peer: str, tip) -> None:
    with _sync_stats_lock:
        entry = _sync_bad_tips.get(peer)
        wait = SYNC_BAD_TIP_BACKOFF_MIN
        if entry is not None and entry[0] == tip:
            wait = min(SYNC_BAD_TIP_BACKOFF_MAX, entry[2] * 2)
        _sync_bad_tips[peer] = (tip, time.monotonic() + wait, wait)
        _sync_bad_tips.move_to_end(peer)
        while len(_sync_bad_tips) > SYNC_BAD_TIP_KEEP:
            _sync_bad_tips.popitem(last=False)


def _record_rejected_reorg_locked(depth: int, now: float) -> None:
    """Only the short bookkeeping step (list append, rate-limited disk write) runs under the node lock."""
    with state_lock:
        blockchain.record_rejected_reorg(depth, now=now)


def _sync_precheck(peer: str, chain_data: list) -> bool:
    """Steps 1-5 above, without the node lock. Returns None (skip), "deep" (a too-deep reorg: let replace_chain
    refuse it and count it) or "valid" (fully validated; worth the locked swap)."""
    if not isinstance(chain_data, list) or not chain_data or not isinstance(chain_data[-1], dict):
        return None
    ours = list(blockchain.chain)       # one atomic copy; a stale view can only cost or save one round
    tip = chain_data[-1].get("hash")
    if len(chain_data) == len(ours) and tip == ours[-1].hash:
        _sync_count("identical")
        return None
    if _sync_backoff_active(peer, tip):
        _sync_count("backoff")
        return None
    n = min(len(ours), len(chain_data))
    common = 0
    while common < n and isinstance(chain_data[common], dict) and chain_data[common].get("hash") == ours[common].hash:
        common += 1
    depth = len(ours) - common
    if common >= len(chain_data):
        _sync_count("lower_work")       # the offer is a prefix of our chain
        return None
    if is_security_fork(ours[-1].index) and depth > MAX_REORG_DEPTH:
        # Too deep to ever adopt. Judge it for the deep-reorg counters/alerts HERE, outside the node lock (cheap
        # precheck, then at most one budgeted full validation), back off this tip, and never take the lock for it.
        _sync_count("deep_refused")
        _sync_mark_bad(peer, tip)
        _sync_note_deep_fork(peer, depth)
        blockchain._note_refused_deep_reorg(chain_data, depth, ours=ours, record=_record_rejected_reorg_locked)
        return None
    if not blockchain._refused_chain_precheck(chain_data, depth, ours=ours):
        _sync_count("precheck_failed")
        _sync_mark_bad(peer, tip)
        return None
    # every block from `common` on now has a consensus-allowed difficulty that its hash really meets
    offered = blockchain.chain_work(ours[:common]) + sum(block_work(Block.from_dict(x)) for x in chain_data[common:])
    if offered <= blockchain.chain_work(ours):
        _sync_count("lower_work")
        if depth > MAX_REORG_DEPTH:
            _sync_count("lower_work_deep_forks")
            _sync_note_deep_fork(peer, depth)
            _log.info("sync: peer %s offers a lower-work fork %d blocks deep; ignored", peer, depth)
        return None
    if not blockchain.is_valid_chain(chain_data):
        _sync_count("invalid")
        _sync_mark_bad(peer, tip)
        return None
    return "valid"


def _sync_with_peer(peer: str):
    try:
        data = _get_json(f"{peer}/chain", deadline_seconds=CHAIN_FETCH_DEADLINE_SECONDS)
        if data.get("chain_id") != chain_id():
            return  # different (or unlabelled) network: never adopt its chain
        incoming_chain = data.get("chain", [])

        if not incoming_chain:
            return
        verdict = _sync_precheck(peer, incoming_chain)
        if verdict is None:
            return
        if verdict == "valid":
            with _sync_stats_lock:
                _sync_last_deep_fork.pop(peer, None)
        prevalidated = verdict == "valid"

        with state_lock:
            old_chain = list(blockchain.chain)
            replaced = blockchain.replace_chain(incoming_chain, prevalidated=prevalidated)
            if replaced:
                _remove_confirmed_txs_from_mempool()
                _requeue_orphaned_txs(old_chain)
                _revalidate_mempool()
        if replaced:
            _sync_count("adopted")
    except Exception:
        pass


def _sync_loop():
    global _auto_sync_running

    while _auto_sync_running:
        try:
            for peer in list(peers):
                _sync_with_peer(peer)
                _discover_peers_from(peer)

            _recheck_known_peers()
            _prune_seen()
            _sync_prune_deep_forks(peers)
        except Exception:
            pass

        time.sleep(AUTO_SYNC_SECONDS)


def _start_sync_thread():
    global _auto_sync_thread, _auto_sync_running

    if _auto_sync_running:
        return

    _auto_sync_running = True
    _auto_sync_thread = threading.Thread(target=_sync_loop, daemon=True)
    _auto_sync_thread.start()


# ----------------------------
# HTTP Handler
# ----------------------------

# Inbound connection limits. A client that goes quiet is dropped after
# INBOUND_READ_TIMEOUT_SECONDS of silence. The request line and headers must arrive
# within HEADER_DEADLINE_SECONDS in total, and a body within
# BODY_DEADLINE_BASE_SECONDS + Content-Length / BODY_MIN_BYTES_PER_SECOND.
# At most MAX_INBOUND_CONNECTIONS are served at once, at most
# MAX_CONNECTIONS_PER_IP from one address, and RESERVED_PEER_SLOTS of the total
# are kept for our configured peers/seeds and loopback (healthcheck).
INBOUND_READ_TIMEOUT_SECONDS = 20
MAX_INBOUND_CONNECTIONS = 256
HEADER_DEADLINE_SECONDS = 10
BODY_DEADLINE_BASE_SECONDS = 10
BODY_MIN_BYTES_PER_SECOND = 64 * 1024
RESERVED_PEER_SLOTS = 32
# Peers we were told about at startup (--peer). Only these, the built-in seed list,
# loopback and NETLITHIC_RESERVED_IPS get reserved slots; peers learned later
# (/hello, /peers discovery) do not.
CONFIGURED_PEER_URLS = set()
RESERVED_IPS_REFRESH_SECONDS = 300


def _env_int(name, default):
    try:
        v = int(os.environ.get(name, ""))
        return v if v > 0 else default
    except ValueError:
        return default


MAX_CONNECTIONS_PER_IP = _env_int("NETLITHIC_MAX_CONNECTIONS_PER_IP", 12)
MAX_CONNECTIONS_PER_TRUSTED_IP = _env_int("NETLITHIC_MAX_CONNECTIONS_PER_TRUSTED_IP", 16)


class P2PServer(CappedThreadingHTTPServer):
    """The node's server: total cap, per-address cap, and slots kept back for trusted addresses."""

    def __init__(self, server_address, handler, max_connections=None,
                 max_per_ip=None, reserved_slots=None, reserved_ips=(), trusted_max_per_ip=None,
                 proxy_share=0.5):
        super().__init__(server_address, handler, proxy_share=proxy_share,
                         trusted_max_per_ip=trusted_max_per_ip or MAX_CONNECTIONS_PER_TRUSTED_IP,
                         header_deadline=HEADER_DEADLINE_SECONDS,
                         body_deadline_base=BODY_DEADLINE_BASE_SECONDS,
                         body_min_rate=BODY_MIN_BYTES_PER_SECOND,
                         max_body_bytes=MAX_POST_BYTES,
                         max_connections=max_connections or MAX_INBOUND_CONNECTIONS,
                         max_per_ip=max_per_ip or MAX_CONNECTIONS_PER_IP,
                         reserved_slots=RESERVED_PEER_SLOTS if reserved_slots is None else reserved_slots,
                         reserved_ips=reserved_ips)


def _peer_ips(urls):
    import socket
    out = set()
    for u in urls:
        host = urlparse(u).hostname
        if not host:
            continue
        try:
            out.add(str(ipaddress.ip_address(host)))
            continue
        except ValueError:
            pass
        try:
            for info in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP):
                out.add(info[4][0])
        except OSError:
            pass
    return out


def _reserved_ips():
    """Loopback (local healthcheck), NETLITHIC_RESERVED_IPS, and the addresses of the
    configured peers (--peer) and the built-in seed list. Never peers learned at run time.
    With NETLITHIC_TRUSTED_PROXIES set, loopback is left out (a local proxy would hand the
    reserve to every proxied client); use NETLITHIC_HEALTH_PORT for the healthcheck."""
    ips = set() if trusted_proxies_from_env() else {"127.0.0.1", "::1"}
    ips |= {x.strip() for x in os.environ.get("NETLITHIC_RESERVED_IPS", "").split(",") if x.strip()}
    return ips | _peer_ips(set(CONFIGURED_PEER_URLS) | set(_builtin_seeds()))


# Same list as easy_node.SEEDS (a test keeps them equal). Kept here so packaged builds
# that ship without easy_node still reserve slots for the seeds.
BUILTIN_SEEDS = ("http://137.184.199.238:8000", "http://157.230.63.97:8000")


def _builtin_seeds():
    return list(BUILTIN_SEEDS)


def _refresh_reserved_ips(server):
    """Re-resolve the trusted set and log what changed (a --peer hostname now pointing
    somewhere else shows up here)."""
    new = frozenset(normalize_ip(i) for i in _reserved_ips())
    old = server.reserved_ips
    if new != old:
        added, removed = sorted(new - old), sorted(old - new)
        print(f"Trusted addresses changed: added {added}, removed {removed}", flush=True)
        server.reserved_ips = new
    return new


def _keep_reserved_ips_fresh(server):
    while True:
        time.sleep(RESERVED_IPS_REFRESH_SECONDS)
        try:
            _refresh_reserved_ips(server)
        except Exception:
            pass


def _report_trusted_proxies(server):
    """Print the proxy set and warn: a node's P2P port should be served directly."""
    if not server.trusted_proxies:
        return
    import sys
    print(f"Trusted proxies (up to {server.max_per_proxy} connections each, no reserved slots): "
          f"{sorted(server.trusted_proxies)}", flush=True)
    print("WARNING: NETLITHIC_TRUSTED_PROXIES is set on a node. Serve the P2P port directly; "
          "never put it behind a proxy (see RUNNING.md).", file=sys.stderr, flush=True)


HEALTH_MAX_CONNECTIONS = 8


def _start_health_listener(port):
    """Optional loopback-only listener (NETLITHIC_HEALTH_PORT) for the local healthcheck,
    with its own small connection pool, so busy public traffic can't lock it out.
    It answers GET /status and /health only (same reply); everything else is 404."""
    srv = CappedThreadingHTTPServer(("127.0.0.1", port), HealthHandler,
                                    max_connections=HEALTH_MAX_CONNECTIONS,
                                    max_per_ip=HEALTH_MAX_CONNECTIONS,
                                    header_deadline=HEADER_DEADLINE_SECONDS,
                                    body_deadline_base=BODY_DEADLINE_BASE_SECONDS,
                                    body_min_rate=BODY_MIN_BYTES_PER_SECOND,
                                    max_body_bytes=MAX_POST_BYTES, trusted_proxies=())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class P2PHandler(BaseHTTPRequestHandler):
    timeout = INBOUND_READ_TIMEOUT_SECONDS
    _defer_replies = True

    def handle_one_request(self):
        # Routes build their reply (under state_lock where needed); the reply is written here,
        # after the route returned and released every lock.
        self._pending_reply = None
        try:
            super().handle_one_request()
        finally:
            pending, self._pending_reply = self._pending_reply, None
            if pending is not None:
                _write_reply(self, *pending)


    def log_message(self, format, *args):
        return

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/chain":
            with state_lock:
                body = _chain_body()
            self._pending_reply = (200, body)
            return

        if path == "/block":
            # One block by height (read-only, one block of data). Lets a light client check
            # whether its tip sits on this node's chain without downloading the chain.
            # Plain ASCII digits only: int() would also accept "1_0", signs, surrounding spaces and Unicode digits.
            raw_index = (params.get("index") or [""])[0]
            if not BLOCK_INDEX_RE.fullmatch(raw_index):
                return _json_response(self, 400, {"error": "index must be a whole number (digits 0-9 only)"})
            index = int(raw_index)
            with state_lock:
                if not 0 <= index < len(blockchain.chain):
                    return _json_response(self, 404, {"error": "No block at that height"})
                return _json_response(self, 200, {"block": blockchain.chain[index].to_dict()})

        if path == "/mempool":
            with state_lock:
                return _json_response(
                    self,
                    200,
                    {
                        "count": len(mempool),
                        "mempool": mempool,
                    },
                )

        if path == "/peers":
            with state_lock:
                return _json_response(
                    self,
                    200,
                    {
                        "self": self_url(),
                        "peers": shared_peers(self.client_address[0]),
                    },
                )

        if path == "/pause_status":
            with state_lock:
                st = pause.state(blockchain.chain)
            return _json_response(self, 200, st)

        if path == "/registry":
            # Registered contributors (TEMPORARY, centralized): the list, what is pending, and every update.
            q = parse_qs(urlparse(self.path).query)
            with state_lock:
                try:
                    return _json_response(self, 200, blockchain.registry_info(
                        (q.get("address") or [None])[0]))
                except ValueError as e:
                    return _json_response(self, 400, {"error": str(e)})

        if path == "/status":
            with state_lock:
                latest = blockchain.get_latest_block()
                return _json_response(
                    self,
                    200,
                    {
                        "node": self_url(),
                        "version": netlithic_version.VERSION,
                        "min_peer_version": netlithic_version.MIN_PEER_VERSION,
                        "upgrade_deadline_height": netlithic_version.UPGRADE_DEADLINE_HEIGHT,
                        "points_v3_rules": points_v3_rules_hash(),   # name kept for older tools; it now fingerprints the v4 rules
                        "points_rules": points_v3_rules_hash(),
                        "difficulty_rules": difficulty_rules_hash(),
                        **blockchain.reorg_info(),
                        **_sync_info(),
                        "security_fork_height": SECURITY_FORK_HEIGHT,
                        "fork_ready": True,
                        "pause_enabled": bool(pause.PAUSE_PUBKEY),
                        "pause_key_id": pause.key_id(),
                        "pause_key_valid": pause.key_problem() == "",   # false = empty or unusable key (information only)
                        "pause_key_problem": pause.key_problem(),
                        "registry_enabled": bool(registry.REGISTRY_ENABLED),
                        "registry_key_id": registry.key_id(),
                        "checkpoints_ok": blockchain.checkpoints_ok(),
                        "chain_id": chain_id(),
                        "height": latest.index,
                        "latest_hash": latest.hash,
                        "latest_timestamp": float(latest.timestamp),
                        "difficulty": latest.difficulty,
                        "difficulty_kind": "work" if is_post_fork(latest.index) else "leading_zeros",
                        "peers": shared_peers(self.client_address[0]),
                        "mempool_size": len(mempool),
                        "contributors": len(blockchain.contribution_points()),
                        "supply": blockchain.supply_info(),
                        "buckets": blockchain.buckets_info(),
                        "community_payouts": blockchain.payout_info(),
                    },
                )

        if path == "/multisig":
            with state_lock:
                return _json_response(self, 200, blockchain.multisig_info(mempool))

        if path == "/contributions":
            with state_lock:
                users = blockchain.contribution_points()
                ranked = sorted(users.items(), key=lambda kv: (-kv[1]["cp_tenths"], kv[0]))
                return _json_response(
                    self,
                    200,
                    {
                        "height": blockchain.get_latest_block().index,
                        "rules": {
                            "per_block_mined": format_points(blockchain.econ.points_per_block),
                            "per_tx_sent": format_points(blockchain.econ.points_per_tx),
                            "empty_self_mined_blocks": f"0 mining points below block {SECURITY_FORK_HEIGHT}; every block counts from block {SECURITY_FORK_HEIGHT}",
                        },
                        "contributors": [{"address": a, **u} for a, u in ranked],
                    },
                )

        if path == "/points_explain":
            address = params.get("address", [None])[0]
            try:
                with state_lock:
                    return _json_response(self, 200, blockchain.explain_points(address))
            except Exception:
                return _json_response(self, 400, {"error": "Invalid address"})

        if path == "/payout_preview":
            if preview_rate_limited(self.client_address[0]):
                return _json_response(self, 429, {"error": "Too many /payout_preview requests, slow down"})
            try:
                raw = params.get("period", [None])[0]
                period = int(raw) if raw not in (None, "") else None
                with state_lock:
                    snapshot = list(blockchain.chain)      # the lock is held only to copy the list of blocks
                return _json_response(self, 200, blockchain.payout_preview_cached(period, snapshot))
            except PreviewBusy:
                return _json_response(self, 503, {"error": "Busy computing other payout previews, try again shortly"})
            except (TypeError, ValueError):
                return _json_response(self, 400, {"error": "period must be a whole number"})

        if path == "/payouts":
            address = params.get("address", [None])[0]
            try:
                with state_lock:
                    if isinstance(address, str):
                        return _json_response(self, 200, blockchain.payout_account(address))
                    return _json_response(self, 200, blockchain.payout_info())
            except Exception:
                return _json_response(self, 400, {"error": "Invalid address"})

        if path == "/leaderboard":
            with state_lock:
                e = blockchain.econ
                users = blockchain.contribution_points_this_epoch()
                ranked = sorted(((a, u) for a, u in users.items()
                                 if a not in e.locked_addresses and u["cp_tenths"] > 0),
                                key=lambda kv: (-kv[1]["cp_tenths"], kv[0]))[:100]
                h = len(blockchain.chain)
                return _json_response(self, 200, {
                    "height": h - 1,
                    "epoch": e.epoch_id(h),
                    "next_payout_height": e.next_epoch_height(h - 1),
                    "rules": {
                        "per_block_with_transfers": format_points(e.points_per_block),
                        "per_transfer_sent": format_points(e.points_per_tx),
                        "min_transfer_nlt": economics.to_nlt(e.min_point_transfer),
                        "pair_cap_per_epoch": e.point_pair_cap,
                        "empty_or_self_only_blocks": f"0 below block {SECURITY_FORK_HEIGHT}; from block {SECURITY_FORK_HEIGHT} every mined block counts (the mining pot follows block share)",
                        "self_sends": "0",
                    },
                    "leaders": [{"rank": i + 1, "address": a, "points": u["cp"],
                                 "blocks_with_transfers": u["blocks_mined"] - u["empty_blocks"],
                                 "transfers_sent": u["tx_count"],
                                 "transfers_that_earned": u["point_tx_count"]}
                                for i, (a, u) in enumerate(ranked)],
                })

        if path == "/balance":
            address = params.get("address", [None])[0]
            if not isinstance(address, str):
                return _json_response(self, 400, {"error": "Missing address"})

            try:
                with state_lock:
                    address = normalize_address(address)
                    return _json_response(
                        self,
                        200,
                        {
                            "address": address,
                            "balance": blockchain.get_balance(address),
                            **blockchain.account_info(address),
                        },
                    )
            except Exception:
                return _json_response(self, 400, {"error": "Invalid address"})

        if path == "/history":
            # Read-only activity list for one address (newest first, paged). No consensus effect.
            from history import address_history
            address = params.get("address", [None])[0]
            try:
                address = normalize_address(address)
                offset = int(params.get("offset", ["0"])[0])
                limit = int(params.get("limit", ["20"])[0])
            except Exception:
                return _json_response(self, 400, {"error": "Invalid address"})
            with state_lock:
                blocks = list(blockchain.chain)
            res = address_history(blocks, address, offset, limit)
            res["address"] = address
            return _json_response(self, 200, res)

        if path == "/account":
            address = params.get("address", [None])[0]
            if not isinstance(address, str):
                return _json_response(self, 400, {"error": "Missing address"})

            try:
                with state_lock:
                    address = normalize_address(address)
                    confirmed_nonce = blockchain.get_confirmed_nonce(address)
                    pending_nonce = blockchain.get_pending_nonce(address, mempool)
                    next_nonce = max(confirmed_nonce, pending_nonce) + 1

                    return _json_response(
                        self,
                        200,
                        {
                            "address": address,
                            "balance": blockchain.get_balance(address),
                            "confirmed_nonce": confirmed_nonce,
                            "pending_nonce": pending_nonce,
                            "next_nonce": next_nonce,
                            "payouts": {k: v for k, v in blockchain.payout_account(address).items()
                                        if k not in ("address", "payouts")},
                        },
                    )
            except Exception:
                return _json_response(self, 400, {"error": "Invalid address"})

        return _json_response(self, 404, {"error": "Not found"})

    def do_POST(self):
        if rate_limited(self.client_address[0]):
            self.close_connection = True
            return _json_response(self, 429, {"error": "Too many requests, slow down"})
        try:
            data = _read_json(self)
        except BodyTooLarge as e:
            self.close_connection = True
            return _json_response(self, 413, {"error": str(e)})
        except Exception:
            return _json_response(self, 400, {"error": "Invalid JSON"})

        path = urlparse(self.path).path

        if path in ("/hello", "/receive_tx", "/receive_block", "/submit_tx", "/tx") and _foreign_chain(data):
            return _json_response(self, 400, {"error": f"Wrong chain_id: this node is on {chain_id()}"})

        if path in ("/mine", "/add_peer") and not is_admin_request(
            self.client_address[0], self.headers
        ):
            return _json_response(
                self,
                403,
                {"error": f"Admin only: send {ADMIN_TOKEN_HEADER} (set {ADMIN_TOKEN_ENV} on the node)"},
            )

        if path == "/add_peer":
            return self.handle_add_peer(data)

        if path == "/hello":
            return self.handle_hello(data)

        if path in ("/submit_tx", "/tx"):
            return self.handle_submit_tx(data)

        if path == "/receive_tx":
            return self.handle_receive_tx(data)

        if path == "/mine":
            return self.handle_mine(data)

        if path == "/receive_block":
            return self.handle_receive_block(data)

        if path == "/sync":
            return self.handle_sync()

        return _json_response(self, 404, {"error": "Not found"})

    def handle_add_peer(self, data):
        peer = data.get("peer")
        if not isinstance(peer, str) or not peer.strip():
            return _json_response(self, 400, {"error": "Missing peer"})

        try:
            normalized = _normalize_peer(peer)
            if not peer_address_allowed(normalized):  # review R1: before any fetch
                raise ValueError("Peer address not allowed (private, loopback, link-local or unresolvable); "
                                 "set NETLITHIC_ALLOW_PRIVATE_PEERS=1 for a local test network")
            # Red-team R1: a peer that never answers /status within the hello timeout is not added.
            reason = _gate_check_url(normalized, strict=True)
            if reason:
                raise ValueError(reason)
            with state_lock:  # address already checked above, outside the lock (P5-1)
                newly_added = _try_add_peer(normalized, address_ok=True) == "added"
                if newly_added:
                    _save_peers()
        except ValueError as e:
            return _json_response(self, 400, {"error": str(e)})

        # Even for a known peer, re-introduce ourselves: it may not know us yet.
        _introduce_to(normalized)

        return _json_response(
            self,
            200,
            {
                "message": "Peer added" if newly_added else "Peer already known",
                "peer": normalized,
                "peers": sorted(peers),
            },
        )

    def handle_hello(self, data):
        """Peer handshake, open to anyone. The caller advertises its URL; we
        only add it after GETting <url>/status and seeing that it reports
        itself as exactly that URL. Same URL rules and peer cap as /add_peer."""
        try:
            url = _normalize_peer(data.get("url"))
            if _is_self(url):
                raise ValueError("Cannot add self as peer")
            # review R1: before any fetch; review F9: bounded, off-thread DNS
            client_ip = (getattr(self, "client_address", None) or ("?",))[0]
            allowed, reason = hello_address_allowed(url, client_ip)
            if not allowed:
                raise ValueError(reason)
        except ValueError as e:
            return _json_response(self, 400, {"error": str(e)})

        with state_lock:
            known = url in peers

        try:
            status = _get_json(f"{url}/status", timeout=HELLO_TIMEOUT_SECONDS, deadline_seconds=HELLO_TIMEOUT_SECONDS)
            reported = _normalize_peer(status.get("node"))
        except Exception:
            if known:       # a known peer that is briefly unreachable stays; the sync loop copes with dead peers
                return _json_response(self, 200, {"message": "Peer already known", "peer": url})
            return _json_response(self, 400, {"error": "Could not verify peer: /status unreachable"})
        if reported != url:
            if known:
                return _json_response(self, 200, {"message": "Peer already known", "peer": url})
            return _json_response(self, 400, {"error": "Could not verify peer: /status reports a different URL"})

        # Peer gate (review M1, M4, F3; F2: it now also runs for peers we already know). Chain id, points rules
        # fingerprint, version. See netlithic_version for the rules; this never touches block validity.
        hello_height = _tip_height()
        gate_error, upgrade_needed = _gate_error(status, hello_height)
        if gate_error:
            if known:
                _drop_peer(url, gate_error["error"])
            return _json_response(self, 400, gate_error)
        if known:
            return _json_response(self, 200, {"message": "Peer already known", "peer": url})

        try:
            with state_lock:  # address checked by hello_address_allowed, outside the lock (P5-1)
                added = _try_add_peer(url, address_ok=True) == "added"
                if added:
                    _save_peers()
        except ValueError as e:
            return _json_response(self, 400, {"error": str(e)})

        out = {"message": "Peer added" if added else "Peer already known", "peer": url}
        if upgrade_needed:
            out["upgrade_needed"] = True
            out["warning"] = (f"Upgrade needed: Netlithic {netlithic_version.MIN_PEER_VERSION} or newer is required "
                              f"before block {netlithic_version.UPGRADE_DEADLINE_HEIGHT}, or this node will split off at block "
                              f"{netlithic_version.FORK_READY_HEIGHT}.")
        return _json_response(self, 200, out)

    def handle_submit_tx(self, data):
        tx = data.get("tx")
        if not isinstance(tx, dict):
            return _json_response(self, 400, {"error": "Missing tx"})

        try:
            tx = normalize_tx(tx)
            tid = tx_id(tx)
        except Exception:
            return _json_response(self, 400, {"error": "Invalid tx format"})

        with state_lock:
            if _tx_exists_in_mempool(tx):
                return _json_response(
                    self,
                    200,
                    {
                        "message": "Transaction already in mempool",
                        "tx_id": tid,
                    },
                )

            if len(mempool) >= MAX_MEMPOOL:
                return _json_response(self, 503, {"error": "Mempool full, try again shortly"})

            if not blockchain.validate_candidate_tx_against_chain_and_mempool(tx, mempool):
                return _json_response(self, 400, {"error": "Invalid transaction"})

            mempool.append(tx)
            _mark_seen_tx(tid)
            _save_mempool()

        _broadcast_tx(tx)

        return _json_response(
            self,
            200,
            {
                "message": "Transaction accepted",
                "tx_id": tid,
                "mempool_size": len(mempool),
            },
        )

    def handle_receive_tx(self, data):
        tx = data.get("tx")
        from_peer = data.get("from_peer")

        if not isinstance(tx, dict):
            return _json_response(self, 400, {"error": "Missing tx"})

        try:
            tx = normalize_tx(tx)
            tid = tx_id(tx)
        except Exception:
            return _json_response(self, 400, {"error": "Invalid tx format"})

        try:
            if isinstance(from_peer, str) and from_peer.strip():
                from_peer = _normalize_peer(from_peer)
            else:
                from_peer = None
        except Exception:
            from_peer = None

        with state_lock:
            if _already_seen_tx(tid) or _tx_exists_in_mempool(tx):
                return _json_response(
                    self,
                    200,
                    {
                        "message": "Transaction already seen",
                        "tx_id": tid,
                    },
                )

            if len(mempool) >= MAX_MEMPOOL:
                return _json_response(self, 503, {"error": "Mempool full, try again shortly"})

            if not blockchain.validate_candidate_tx_against_chain_and_mempool(tx, mempool):
                return _json_response(self, 400, {"error": "Invalid transaction"})

            mempool.append(tx)
            _mark_seen_tx(tid)
            _save_mempool()

        _broadcast_tx(tx, exclude_peer=from_peer)

        return _json_response(
            self,
            200,
            {
                "message": "Transaction received",
                "tx_id": tid,
            },
        )

    def handle_mine(self, data):
        miner_address = data.get("miner_address")
        if not isinstance(miner_address, str) or not miner_address.strip():
            return _json_response(self, 400, {"error": "Missing miner_address"})

        try:
            miner_address = normalize_address(miner_address)
        except Exception:
            return _json_response(self, 400, {"error": "Invalid miner_address"})

        # The nonce search runs OUTSIDE state_lock, so a mining node keeps answering /status, /receive_block and sync.
        # mine_lock keeps one search at a time, as the old single lock did (a second /mine waits its turn).
        with mine_lock:
            with state_lock:
                template = blockchain.build_block_template(miner_address, mempool)
            tip_hash = template["previous_hash"]

            def _tip_moved():
                return blockchain.get_latest_block().hash != tip_hash   # lock-free read of the last list item

            block = blockchain.solve_block_template(template, should_abort=_tip_moved)
            if block is None:
                return _json_response(self, 409, {"error": "Chain tip moved while mining; retry"})

        with state_lock:
            if blockchain.get_latest_block().hash != tip_hash:
                return _json_response(self, 409, {"error": "Chain tip moved while mining; retry"})

            added = blockchain.add_block(block.to_dict())
            if not added:
                return _json_response(
                    self,
                    500,
                    {
                        "error": "Failed to add mined block locally",
                        "reason": blockchain.last_reject_reason,
                    },
                )

            _remove_confirmed_txs_from_mempool()
            _revalidate_mempool()
            _mark_seen_block(block.hash)

        _broadcast_block(block.to_dict())

        return _json_response(
            self,
            200,
            {
                "message": "Block mined",
                "block": block.to_dict(),
            },
        )

    def handle_receive_block(self, data):
        block_data = data.get("block")
        from_peer = data.get("from_peer")

        if not isinstance(block_data, dict):
            return _json_response(self, 400, {"error": "Missing block"})

        try:
            block_hash = str(block_data.get("hash"))
            if not block_hash:
                raise ValueError
        except Exception:
            return _json_response(self, 400, {"error": "Invalid block hash"})

        try:
            if isinstance(from_peer, str) and from_peer.strip():
                from_peer = _normalize_peer(from_peer)
            else:
                from_peer = None
        except Exception:
            from_peer = None

        with state_lock:
            if _already_seen_block(block_hash):
                return _json_response(
                    self,
                    200,
                    {
                        "message": "Block already seen",
                        "hash": block_hash,
                    },
                )

            latest = blockchain.get_latest_block()
            incoming_index = int(block_data.get("index", -1))
            incoming_prev = str(block_data.get("previous_hash", ""))

            if incoming_index == latest.index + 1 and incoming_prev == latest.hash:
                added = blockchain.add_block(block_data)
                if not added:
                    return _json_response(
                        self,
                        400,
                        {"error": "Invalid block", "reason": blockchain.last_reject_reason},
                    )

                _mark_seen_block(block_hash)
                _remove_confirmed_txs_from_mempool()
                _revalidate_mempool()

            elif incoming_index > latest.index + 1:
                pass
            else:
                return _json_response(
                    self,
                    200,
                    {
                        "message": "Block not needed",
                        "hash": block_hash,
                    },
                )

        if int(block_data.get("index", 0)) > blockchain.get_latest_block().index + 1:
            allowed, why = sync_trigger_allowed(from_peer)
            if allowed:
                _sync_with_peer(from_peer)
                msg = "Behind current chain, sync attempted"
            else:
                msg = f"Behind current chain; no sync triggered ({why}); regular sync will catch up"

            return _json_response(
                self,
                200,
                {
                    "message": msg,
                    "hash": block_hash,
                },
            )

        _broadcast_block(block_data, exclude_peer=from_peer)

        return _json_response(
            self,
            200,
            {
                "message": "Block accepted",
                "hash": block_hash,
            },
        )

    def handle_sync(self):
        for peer in list(peers):
            _sync_with_peer(peer)

        with state_lock:
            return _json_response(
                self,
                200,
                {
                    "message": "Sync complete",
                    "height": blockchain.get_latest_block().index,
                    "work": blockchain.chain_work(),
                },
            )


# ----------------------------
# Startup
# ----------------------------

def startup_checkpoint_check() -> bool:
    """Review F10: after the checkpoints are loaded, compare every checkpoint
    at or below our height with our own chain. On a mismatch the node logs
    loudly and refuses to start (or quarantines + resyncs if
    NETLITHIC_CHECKPOINT_RESYNC=1)."""
    from core import checkpoint_mismatches
    with state_lock:
        bad = checkpoint_mismatches(blockchain.chain)
        if bad:
            blockchain.handle_checkpoint_mismatch(bad)  # raises unless resync is enabled
    return not bad


def _add_extra_peers(extra_peers) -> None:
    """--peer URLs: resolved outside state_lock (P5-1), private ones skipped."""
    for p in extra_peers:
        try:
            n = _normalize_peer(p)
            ok = peer_address_allowed(n)
            reason = _gate_check_url(n, strict=False) if ok else None      # review F2: unreachable is allowed here
            if reason:
                raise ValueError(reason)
            with state_lock:
                if _try_add_peer(p, address_ok=ok) == "added":
                    _save_peers()
        except ValueError as e:
            print(f"Skipping peer {p!r}: {e}")


def _warn_pause_key():
    """Loud startup warning when the pause key slot holds a key nobody could sign with. Never stops the node and never
    touches consensus; an empty slot is a different, quieter note (the switch is simply off)."""
    problem = pause.key_problem()
    if problem == "empty":
        print("NOTE: the pause key slot is empty, so the payout pause switch is OFF on this node.")
    elif problem:
        print(f"WARNING: {problem}. Do not run this build on the public network; fix pause.PAUSE_PUBKEY "
              f"(ops/set_pause_pubkey.py --check).")
    return problem


class HealthHandler(P2PHandler):
    """Status only: the health listener never serves /chain (up to 16 MiB), admin or POST routes."""
    HEALTH_PATHS = ("/status", "/health")

    def do_GET(self):
        if urlparse(self.path).path not in self.HEALTH_PATHS:
            return _json_response(self, 404, {"error": "Not found"})
        self.path = "/status"
        return super().do_GET()

    def do_POST(self):
        return _json_response(self, 404, {"error": "Not found"})


def run_server(host: str = HOST, port: int = PORT, advertise_url: str = None, extra_peers=()):
    # Review F4: a broken config/checkpoints.json must stop the node loudly,
    # not silently run with no checkpoints.
    # Review R5: loaded ONCE here and cached; a later bad edit can't make
    # request handlers raise. Restart the node to pick up a new file.
    from core import init_checkpoints
    init_checkpoints()
    startup_checkpoint_check()
    _warn_pause_key()
    global HOST, PORT, ADVERTISE_URL
    HOST = host
    PORT = port
    ADVERTISE_URL = _normalize_peer(advertise_url) if advertise_url else None

    _load_peers()
    CONFIGURED_PEER_URLS.update(p for p in extra_peers if isinstance(p, str))
    _add_extra_peers(extra_peers)
    _load_mempool()
    _remove_confirmed_txs_from_mempool()
    _revalidate_mempool()
    _start_sync_thread()

    server = P2PServer((HOST, PORT), P2PHandler, reserved_ips=_reserved_ips())
    print(f"Trusted addresses: {sorted(server.reserved_ips)}")
    _report_trusted_proxies(server)
    health_port = _env_int("NETLITHIC_HEALTH_PORT", 0)
    if health_port:
        _start_health_listener(health_port)
        print(f"Health listener on http://127.0.0.1:{health_port}")
    threading.Thread(target=_keep_reserved_ips_fresh, args=(server,), daemon=True).start()
    for p in list(peers):
        # once we're listening (so peers can verify us), say hello to known peers
        threading.Thread(target=_introduce_to, args=(p,), daemon=True).start()
    print(f"Netlithic node running at {self_url()}")
    print(f"Loaded peers: {sorted(peers)}")
    print(f"Loaded mempool txs: {len(mempool)}")
    if blockchain.econ.placeholders:
        print("NOTE: genesis allocations use PLACEHOLDER addresses "
              f"({', '.join(blockchain.econ.placeholders)}). Dev/test only; set real ones before testnet.")
    server.serve_forever()


def run_node(port: int = PORT, host: str = HOST, advertise_url: str = None, extra_peers=()):
    run_server(host=host, port=port, advertise_url=advertise_url, extra_peers=extra_peers)


if __name__ == "__main__":
    import sys

    host = HOST
    port = PORT

    if len(sys.argv) >= 2:
        port = int(sys.argv[1])

    if len(sys.argv) >= 3:
        host = sys.argv[2]

    run_server(host=host, port=port)