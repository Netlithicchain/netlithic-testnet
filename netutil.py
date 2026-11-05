"""HTTP helper: urlopen restricted to http/https (no file:, ftp:, data: ...),
never following redirects (review R3), with an optional connect-time
address guard (review R2: the address actually connected to is checked, so
DNS rebinding between a resolve-check and the connect can't reach an
internal address)."""
import http.client
import io
import ipaddress
import socket
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse
from urllib.request import Request
from http.server import ThreadingHTTPServer

ALLOWED_SCHEMES = ("http", "https")


def check_url(url: str) -> str:
    scheme = urlparse(url).scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise ValueError(f"URL scheme not allowed: {scheme or '(none)'}")
    return url


def is_public_ip(ip) -> bool:
    """True for globally routable unicast addresses only."""
    if not isinstance(ip, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        ip = ipaddress.ip_address(str(ip).split("%")[0])
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
                or ip.is_reserved or ip.is_unspecified or not ip.is_global)


class AddressNotAllowed(OSError):
    """The connected peer address failed the caller's address policy."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, f"redirect to {newurl!r} refused", headers, fp)


def _check_sock(sock, allow_ip):
    ip = sock.getpeername()[0]
    if not allow_ip(ipaddress.ip_address(str(ip).split("%")[0])):
        sock.close()
        raise AddressNotAllowed(f"connected address {ip} not allowed")


class _Deadline:
    """One overall deadline (a time.monotonic() value) for a whole request: connect, TLS, headers and body.
    When it passes, the socket is shut down, which ends any read still waiting on it."""

    def __init__(self, deadline):
        self.deadline = deadline
        self.timers = []

    def left(self):
        return self.deadline - time.monotonic()

    def watch(self, sock):
        def stop():
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        t = threading.Timer(max(self.left(), 0), stop)
        t.daemon = True
        t.start()
        self.timers.append(t)

    def cancel(self):
        for t in self.timers:
            t.cancel()


def _guarded_conn(base, allow_ip, limit=None):
    class Conn(base):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            if limit is not None:
                create = self._create_connection

                def create_with_deadline(address, timeout=None, source_address=None, **kw):
                    left = limit.left()
                    if left <= 0:
                        raise TimeoutError("request deadline passed")
                    if timeout is None or timeout == socket._GLOBAL_DEFAULT_TIMEOUT or timeout > left:
                        timeout = left
                    sock = create(address, timeout, source_address, **kw)
                    limit.watch(sock)
                    return sock
                self._create_connection = create_with_deadline

        def connect(self):
            super().connect()
            if allow_ip is not None:
                _check_sock(self.sock, allow_ip)
    return Conn


def _opener(allow_ip=None, limit=None):
    handlers = [_NoRedirect()]
    if allow_ip is not None or limit is not None:
        http_conn = _guarded_conn(http.client.HTTPConnection, allow_ip, limit)
        https_conn = _guarded_conn(http.client.HTTPSConnection, allow_ip, limit)

        class H(urllib.request.HTTPHandler):
            def http_open(self, req):
                return self.do_open(http_conn, req)

        class HS(urllib.request.HTTPSHandler):
            def https_open(self, req):
                return self.do_open(https_conn, req, context=self._context)

        # no env proxies: the guard must see the real peer, not a proxy
        handlers += [urllib.request.ProxyHandler({}), H(), HS()]
    return urllib.request.build_opener(*handlers)


def urlopen(req, *args, allow_ip=None, deadline=None, **kwargs):
    """Drop-in for urllib.request.urlopen: refuses non-http(s) URLs and 3xx
    redirects. If allow_ip(ip_address) is given, the socket's connected
    address must pass it or AddressNotAllowed is raised before any request
    bytes are sent. If deadline (a time.monotonic() value) is given, the whole
    request, from connect through the last body byte, must finish by then;
    call resp.cancel_deadline() once done with the response."""
    url = req.full_url if isinstance(req, Request) else str(req)
    check_url(url)
    limit = _Deadline(deadline) if deadline is not None else None
    try:
        resp = _opener(allow_ip, limit).open(req, *args, **kwargs)  # nosec B310 - scheme checked above
    except urllib.error.URLError as e:
        if limit is not None:
            limit.cancel()
        if isinstance(getattr(e, "reason", None), AddressNotAllowed):
            raise e.reason from None
        if limit is not None and limit.left() <= 0:
            raise TimeoutError("request deadline passed") from None
        raise
    except Exception:
        if limit is not None:
            limit.cancel()
            if limit.left() <= 0:
                raise TimeoutError("request deadline passed") from None
        raise
    resp.cancel_deadline = limit.cancel if limit is not None else (lambda: None)
    return resp


def normalize_ip(ip) -> str:
    """One key per address: IPv4-mapped IPv6 ('::ffff:1.2.3.4') counts as the IPv4 address."""
    try:
        a = ipaddress.ip_address(str(ip).split("%", 1)[0])
    except ValueError:
        return str(ip)
    if a.version == 6 and a.ipv4_mapped is not None:
        a = a.ipv4_mapped
    return str(a)


def trusted_proxies_from_env(env=None):
    """NETLITHIC_TRUSTED_PROXIES: comma-separated IP literals of reverse proxies you run.
    Hostnames and invalid entries are ignored (with a warning), so a DNS change can't
    widen the set."""
    import os
    import sys
    raw = (os.environ if env is None else env).get("NETLITHIC_TRUSTED_PROXIES", "")
    out = set()
    for x in raw.split(","):
        x = x.strip()
        if not x:
            continue
        try:
            ipaddress.ip_address(x.split("%", 1)[0])
        except ValueError:
            print(f"WARNING: NETLITHIC_TRUSTED_PROXIES entry {x!r} is not an IP address; ignored",
                  file=sys.stderr)
            continue
        out.add(normalize_ip(x))
    return frozenset(out)


class _DeadlineWriter(io.RawIOBase):
    """Wraps a handler's wfile. The first write of a reply arms one deadline for the whole
    reply (reply_deadline_base + size hint / reply_min_rate seconds); the handler cancels it
    when the request ends."""

    def __init__(self, handler, raw):
        super().__init__()
        self._h, self._raw = handler, raw

    def writable(self):
        return True

    def write(self, b):
        h = self._h
        if not getattr(h, "_reply_armed", False):
            server = getattr(h, "server", None)
            base = getattr(server, "reply_deadline_base", 30)
            rate = getattr(server, "reply_min_rate", 64 * 1024)
            size = max(len(b), getattr(h, "_reply_size_hint", 0) or 0)
            h._arm_deadline(base + size / rate)
            h._reply_armed = True
        return self._raw.write(b)

    def flush(self):
        if self.closed or self._raw.closed:
            return None
        return self._raw.flush()

    def close(self):
        if self.closed:
            return
        try:
            super().close()         # runs our flush while the raw stream is still open
        finally:
            self._raw.close()


class _DeadlineHandler:
    """Mixed into every handler served by CappedThreadingHTTPServer.

    The request line and headers must arrive within server.header_deadline seconds.
    A request body (POST/PUT/PATCH with a Content-Length up to server.max_body_bytes)
    is read up front and must arrive within
    server.body_deadline_base + length / server.body_min_rate seconds.
    The limit ends once the body is in, so the handler's own work and the reply are
    not limited (the idle timeout still applies). Requests with no body to read
    (GET, or a body over the size limit, which the handler refuses) arm no body limit."""

    _BODY_METHODS = ("POST", "PUT", "PATCH")

    def setup(self):
        super().setup()
        # Reply-phase deadline: every write must finish within
        # reply_deadline_base + len(data) / reply_min_rate seconds, or the socket is shut
        # and the handler thread is freed. A reader that stops reading can't hold a thread.
        self.wfile = _DeadlineWriter(self, self.wfile)

    def _arm_deadline(self, seconds):
        self._cancel_deadline()
        conn = self.connection

        def expire():
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self._deadline = threading.Timer(seconds, expire)
        self._deadline.daemon = True
        self._deadline.start()

    def _cancel_deadline(self):
        t = getattr(self, "_deadline", None)
        if t is not None:
            t.cancel()
            self._deadline = None

    def send_header(self, keyword, value):
        # Size the reply deadline from the declared body length, for every handler (not only the
        # ones that set _reply_size_hint themselves), so a large reply read at the minimum rate isn't cut.
        if str(keyword).lower() == "content-length":
            try:
                n = int(value)
            except (TypeError, ValueError):
                n = 0
            if n > (getattr(self, "_reply_size_hint", 0) or 0):
                self._reply_size_hint = n
        super().send_header(keyword, value)

    def handle_one_request(self):
        self._arm_deadline(self.server.header_deadline)
        stream = self.rfile
        self._reply_armed = False
        self._reply_size_hint = 0
        try:
            super().handle_one_request()
        finally:
            self._cancel_deadline()
            self._reply_armed = False
            self.rfile = stream

    def parse_request(self):
        ok = super().parse_request()
        self._cancel_deadline()
        if not ok or self.command not in self._BODY_METHODS:
            return ok
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return ok
        if not 0 < length <= self.server.max_body_bytes:
            return ok
        self._arm_deadline(self.server.body_deadline_base + length / self.server.body_min_rate)
        try:
            body = self.rfile.read(length)
        except OSError:
            body = b""
        self._cancel_deadline()
        if len(body) != length:
            self.close_connection = True
            return False
        self.rfile = io.BytesIO(body)
        return ok


class CappedThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with a total connection cap, a per-address cap and
    slots kept back for trusted addresses. Over-limit connections are closed
    straight away without a thread."""
    daemon_threads = True
    request_queue_size = 128    # listen backlog; the default of 5 drops bursts before they are counted

    def __init__(self, server_address, handler, max_connections=256,
                 max_per_ip=None, reserved_slots=0, reserved_ips=(), trusted_max_per_ip=None,
                 header_deadline=10, body_deadline_base=10, body_min_rate=64 * 1024,
                 max_body_bytes=1024 * 1024, trusted_proxies=None, proxy_share=0.5,
                 reply_deadline_base=30, reply_min_rate=64 * 1024):
        if not issubclass(handler, _DeadlineHandler):
            handler = type(handler.__name__, (_DeadlineHandler, handler), {})
        super().__init__(server_address, handler)
        self.header_deadline = header_deadline
        self.body_deadline_base = body_deadline_base
        self.body_min_rate = body_min_rate
        self.max_body_bytes = max_body_bytes
        self.reply_deadline_base = reply_deadline_base
        self.reply_min_rate = reply_min_rate
        total = max_connections
        reserved = max(0, min(reserved_slots, total - 1))
        self.max_per_ip = max_per_ip or total
        # Trusted (configured) addresses: up to max_per_ip from the open pool like anyone else;
        # beyond that, or when the open pool is full, only from the reserved pool, up to trusted_max_per_ip.
        self.trusted_max_per_ip = max(self.max_per_ip, trusted_max_per_ip or self.max_per_ip)
        self._slots = threading.BoundedSemaphore(total - reserved)     # anyone
        # Each trusted proxy may hold at most this many open-pool slots (default: half),
        # so one busy or abusive proxy can't starve learned peers and miners.
        self.max_per_proxy = max(1, int((total - reserved) * proxy_share))
        self._reserved = threading.BoundedSemaphore(reserved) if reserved else None
        self.reserved_ips = reserved_ips
        # Reverse proxies you run: every client behind one shares its address, so it is
        # exempt from the per-address caps (but held to max_per_proxy). It never gets reserved slots.
        self.trusted_proxies = trusted_proxies_from_env() if trusted_proxies is None else trusted_proxies
        self._per_ip = {}
        self._lock = threading.Lock()
        self._held = {}     # id(request) -> (ip, semaphore)

    @property
    def reserved_ips(self):
        return self._reserved_ips

    @reserved_ips.setter
    def reserved_ips(self, ips):
        self._reserved_ips = frozenset(normalize_ip(i) for i in ips)

    @property
    def trusted_proxies(self):
        return self._trusted_proxies

    @trusted_proxies.setter
    def trusted_proxies(self, ips):
        self._trusted_proxies = frozenset(normalize_ip(i) for i in ips)

    def _take(self, ip):
        ip = normalize_ip(ip)
        proxy = ip in self._trusted_proxies
        trusted = ip in self._reserved_ips and not proxy
        with self._lock:
            n = self._per_ip.get(ip, 0)
            if proxy:
                if n >= self.max_per_proxy or not self._slots.acquire(blocking=False):
                    return None
                self._per_ip[ip] = n + 1
                return self._slots
            if n >= (self.trusted_max_per_ip if trusted else self.max_per_ip):
                return None
            if n < self.max_per_ip and self._slots.acquire(blocking=False):
                sem = self._slots
            elif trusted and self._reserved is not None and self._reserved.acquire(blocking=False):
                sem = self._reserved
            else:
                return None
            self._per_ip[ip] = self._per_ip.get(ip, 0) + 1
            return sem

    def _give(self, ip, sem):
        with self._lock:
            n = self._per_ip.get(ip, 1) - 1
            if n > 0:
                self._per_ip[ip] = n
            else:
                self._per_ip.pop(ip, None)
            sem.release()

    def process_request(self, request, client_address):
        ip = normalize_ip(client_address[0]) if client_address else "?"
        sem = self._take(ip)
        if sem is None:
            self.shutdown_request(request)
            return
        self._held[id(request)] = (ip, sem)
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._held.pop(id(request), None)
            self._give(ip, sem)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            held = self._held.pop(id(request), None)
            if held:
                self._give(*held)

    def handle_error(self, request, client_address):
        import sys
        if isinstance(sys.exc_info()[1], OSError):
            return      # client went away or hit a deadline: nothing to report
        super().handle_error(request, client_address)
