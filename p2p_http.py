import hmac
import ipaddress
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from core import Blockchain, normalize_address, tx_id, normalize_tx, is_reward_tx
from economics import format_points, to_nlt


HOST = "127.0.0.1"
PORT = 8000
ADVERTISE_URL = None  # public URL other nodes should use for us (set via --advertise-url)
HELLO_TIMEOUT_SECONDS = 5

PEERS_FILE = "peers.json"
MEMPOOL_FILE = "mempool.json"
AUTO_SYNC_SECONDS = 5
SEEN_TTL_SECONDS = 120

# Hardening limits
MAX_PEERS = 32
MAX_POST_BYTES = 1 * 1024 * 1024        # any POST body (tx, block, peer)
MAX_FETCH_BYTES = 16 * 1024 * 1024      # a /chain or /peers response from a peer
ADMIN_TOKEN_ENV = "NETLITHIC_ADMIN_TOKEN"
ADMIN_TOKEN_HEADER = "X-Admin-Token"


blockchain = Blockchain()
peers = set()
mempool = []

seen_blocks = {}
seen_txs = {}

_auto_sync_thread = None
_auto_sync_running = False
state_lock = threading.Lock()


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


def _json_response(handler, status_code, payload):
    try:
        body = json.dumps(payload, indent=2).encode("utf-8")
        handler.send_response(status_code)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        pass


class BodyTooLarge(ValueError):
    pass


def _read_json(handler):
    length = int(handler.headers.get("Content-Length", 0))
    if length < 0:
        raise ValueError("Negative Content-Length")
    if length > MAX_POST_BYTES:
        raise BodyTooLarge(f"Body over {MAX_POST_BYTES} bytes")
    raw = handler.rfile.read(length) if length > 0 else b"{}"
    return json.loads(raw.decode("utf-8"))


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


LOCAL_HOSTNAMES = {"127.0.0.1", "localhost", "0.0.0.0", "[::1]"}


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


def _try_add_peer(peer: str) -> str:
    """Validate and add a peer (caller holds state_lock).
    Returns 'added', 'known', or raises ValueError with the reason."""
    normalized = _normalize_peer(peer)
    if _is_self(normalized):
        raise ValueError("Cannot add self as peer")
    if normalized in peers:
        return "known"
    if len(peers) >= MAX_PEERS:
        raise ValueError(f"Peer limit reached ({MAX_PEERS})")
    peers.add(normalized)
    return "added"


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
    with urlopen(req, timeout=timeout) as resp:
        body = resp.read(MAX_FETCH_BYTES + 1)
        if len(body) > MAX_FETCH_BYTES:
            raise ValueError(f"Peer response over {MAX_FETCH_BYTES} bytes")
        body = body.decode("utf-8")
        return json.loads(body) if body else {}


def _get_json(url: str, timeout: int = 5):
    req = Request(url, headers={"Accept": "application/json"})
    with urlopen(req, timeout=timeout) as resp:
        body = resp.read(MAX_FETCH_BYTES + 1)
        if len(body) > MAX_FETCH_BYTES:
            raise ValueError(f"Peer response over {MAX_FETCH_BYTES} bytes")
        body = body.decode("utf-8")
        return json.loads(body) if body else {}


def self_url() -> str:
    if ADVERTISE_URL:
        return ADVERTISE_URL
    return f"http://{HOST}:{PORT}"


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
        data = _get_json(f"{peer_url}/peers")
        remote_peers = data.get("peers", [])
        changed = False

        with state_lock:
            for p in remote_peers:
                try:
                    if _try_add_peer(p) == "added":
                        changed = True
                except Exception:
                    continue

            if changed:
                _save_peers()
    except Exception:
        pass


def _broadcast_block(block_data: dict, exclude_peer: str = None):
    block_hash = block_data.get("hash")
    if isinstance(block_hash, str):
        _mark_seen_block(block_hash)

    for peer in list(peers):
        if exclude_peer and peer == exclude_peer:
            continue

        try:
            _post_json(
                f"{peer}/receive_block",
                {
                    "block": block_data,
                    "from_peer": self_url(),
                    "chain_id": chain_id(),
                },
            )
        except Exception:
            continue


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


def _sync_with_peer(peer: str):
    try:
        data = _get_json(f"{peer}/chain")
        if data.get("chain_id") != chain_id():
            return  # different (or unlabelled) network: never adopt its chain
        incoming_chain = data.get("chain", [])

        if not incoming_chain:
            return

        with state_lock:
            replaced = blockchain.replace_chain(incoming_chain)
            if replaced:
                _remove_confirmed_txs_from_mempool()
                _revalidate_mempool()
    except Exception:
        pass


def _sync_loop():
    global _auto_sync_running

    while _auto_sync_running:
        try:
            for peer in list(peers):
                _sync_with_peer(peer)
                _discover_peers_from(peer)

            _prune_seen()
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

class P2PHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/chain":
            with state_lock:
                return _json_response(
                    self,
                    200,
                    {
                        "chain_id": chain_id(),
                        "length": len(blockchain.chain),
                        "work": blockchain.chain_work(),
                        "chain": blockchain.to_list(),
                    },
                )

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

        if path == "/status":
            with state_lock:
                latest = blockchain.get_latest_block()
                return _json_response(
                    self,
                    200,
                    {
                        "node": self_url(),
                        "chain_id": chain_id(),
                        "height": latest.index,
                        "latest_hash": latest.hash,
                        "difficulty": latest.difficulty,
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
                            "empty_self_mined_blocks": "0 mining points",
                        },
                        "contributors": [{"address": a, **u} for a, u in ranked],
                    },
                )

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
                        },
                    )
            except Exception:
                return _json_response(self, 400, {"error": "Invalid address"})

        return _json_response(self, 404, {"error": "Not found"})

    def do_POST(self):
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
            with state_lock:
                newly_added = _try_add_peer(normalized) == "added"
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
        except ValueError as e:
            return _json_response(self, 400, {"error": str(e)})

        with state_lock:
            if url in peers:
                return _json_response(self, 200, {"message": "Peer already known", "peer": url})

        try:
            status = _get_json(f"{url}/status", timeout=HELLO_TIMEOUT_SECONDS)
            reported = _normalize_peer(status.get("node"))
        except Exception:
            return _json_response(self, 400, {"error": "Could not verify peer: /status unreachable"})
        if reported != url:
            return _json_response(self, 400, {"error": "Could not verify peer: /status reports a different URL"})
        if status.get("chain_id") != chain_id():
            return _json_response(self, 400, {"error": f"Peer is on a different chain_id (we are {chain_id()})"})

        try:
            with state_lock:
                added = _try_add_peer(url) == "added"
                if added:
                    _save_peers()
        except ValueError as e:
            return _json_response(self, 400, {"error": str(e)})

        return _json_response(self, 200, {"message": "Peer added" if added else "Peer already known", "peer": url})

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

        with state_lock:
            block = blockchain.mine_block(miner_address, mempool)

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
            if from_peer:
                _sync_with_peer(from_peer)

            return _json_response(
                self,
                200,
                {
                    "message": "Behind current chain, sync attempted",
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

def run_server(host: str = HOST, port: int = PORT, advertise_url: str = None, extra_peers=()):
    global HOST, PORT, ADVERTISE_URL
    HOST = host
    PORT = port
    ADVERTISE_URL = _normalize_peer(advertise_url) if advertise_url else None

    _load_peers()
    for p in extra_peers:
        try:
            with state_lock:
                if _try_add_peer(p) == "added":
                    _save_peers()
        except ValueError as e:
            print(f"Skipping peer {p!r}: {e}")
    _load_mempool()
    _remove_confirmed_txs_from_mempool()
    _revalidate_mempool()
    _start_sync_thread()

    server = ThreadingHTTPServer((HOST, PORT), P2PHandler)
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