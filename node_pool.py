"""Try node A first, fall back to the next node when it can't be reached.

Used by the web wallet, faucet and explorer. A node that is down (connection refused,
timeout, 5xx) is skipped and the next one is tried. A clear "no" from a node (4xx, such
as a rejected transaction) is passed straight back, never retried elsewhere.
After a failure the node is skipped for `cooldown` seconds so visitors don't wait on
a timeout every page load; then it is tried first again.
"""
import json
import socket
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request
from netutil import urlopen

DEFAULT_FALLBACK = "http://157.230.63.97:8000"  # netlithic-testnet-2 (public, read/relay only)


class NoNodeReachable(OSError):
    pass


def parse_nodes(*values):
    out = []
    for v in values:
        if not v:
            continue
        for part in (v if isinstance(v, (list, tuple)) else str(v).split(",")):
            u = str(part).strip().rstrip("/")
            if u and u not in out:
                out.append(u)
    return out


class NodePool:
    def __init__(self, urls, timeout=10, cooldown=30, clock=time.time, max_bytes=None):
        self.urls = parse_nodes(urls)
        if not self.urls:
            raise ValueError("at least one node URL is needed")
        self.timeout, self.cooldown, self.clock, self.max_bytes = timeout, cooldown, clock, max_bytes
        self._down = {}  # url -> time it failed
        self._lock = threading.Lock()
        self.last_used = self.urls[0]

    @property
    def primary(self):
        return self.urls[0]

    def _order(self):
        now = self.clock()
        with self._lock:
            up = [u for u in self.urls if now - self._down.get(u, -1e18) >= self.cooldown]
            down = [u for u in self.urls if u not in up]
        return up + down  # resting nodes still get a last try

    def _mark(self, url, ok):
        with self._lock:
            if ok:
                self._down.pop(url, None)
            else:
                self._down[url] = self.clock()

    def _one(self, url, path, payload, headers):
        data = None if payload is None else json.dumps(payload).encode()
        h = {"Accept": "application/json", **(headers or {})}
        if data is not None:
            h["Content-Type"] = "application/json"
        req = Request(url + path, data=data, headers=h, method="GET" if data is None else "POST")
        try:
            with urlopen(req, timeout=self.timeout) as r:
                raw = r.read(self.max_bytes) if self.max_bytes else r.read()
                return r.status, json.loads(raw.decode() or "{}")
        except HTTPError as e:
            try:
                body = json.loads(e.read(65536).decode() or "{}")
            except Exception:
                body = {}
            return e.code, body

    def request(self, path, payload=None, headers=None):
        """Returns (status, json). Raises NoNodeReachable if every node is down."""
        last = None
        for url in self._order():
            try:
                code, body = self._one(url, path, payload, headers)
            except (URLError, socket.timeout, TimeoutError, ConnectionError, OSError, ValueError) as e:
                last = e
                self._mark(url, False)
                continue
            if code >= 500:
                last = NoNodeReachable(f"{url} answered {code}")
                self._mark(url, False)
                continue
            self._mark(url, True)
            self.last_used = url
            return code, body
        raise NoNodeReachable(f"no node reachable ({last})")
