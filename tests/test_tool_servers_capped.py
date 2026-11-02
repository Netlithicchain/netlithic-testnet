"""The webwallet, faucet, explorer and desktop servers use the capped server class."""
import os
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler

import netutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_tool_servers_are_capped_and_drop_quiet_clients():
    for rel in ("webwallet.py", "faucet.py", "explorer.py", os.path.join("desktop", "netlithic_desktop.py")):
        src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        assert re.search(r"CappedThreadingHTTPServer\(.*max_connections=\d+", src), rel
        assert "handler.timeout = 20" in src, rel
        assert not re.search(r"(?<!Capped)ThreadingHTTPServer\(", src), rel


def test_capped_server_refuses_beyond_the_total_and_recovers():
    class H(BaseHTTPRequestHandler):
        timeout = 5

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass
    srv = netutil.CappedThreadingHTTPServer(("127.0.0.1", 0), H, max_connections=3)
    assert srv.max_per_ip == 3          # no per-address cap by default (proxies share one address)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    held = [socket.create_connection(("127.0.0.1", port)) for _ in range(3)]
    for c in held:
        c.sendall(b"GET / HTTP/1.0\r\n")
    time.sleep(0.3)
    extra = socket.create_connection(("127.0.0.1", port))
    extra.settimeout(3)
    extra.sendall(b"GET / HTTP/1.0\r\n\r\n")
    try:
        got = extra.recv(100)
    except OSError:
        got = b""
    extra.close()
    assert got == b""
    for c in held:
        c.close()
    time.sleep(0.3)
    c = socket.create_connection(("127.0.0.1", port), timeout=3)
    c.sendall(b"GET / HTTP/1.0\r\n\r\n")
    assert c.recv(100).startswith(b"HTTP/1.0 200")
    c.close()
    srv.shutdown()
    srv.server_close()


def _echo_server(**kw):
    class H(BaseHTTPRequestHandler):
        timeout = 20

        def do_GET(self):
            time.sleep(float(self.headers.get("X-Work", "0")))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(n)
            time.sleep(float(self.headers.get("X-Work", "0")))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"got %d" % len(body))

        def log_message(self, *a):
            pass
    srv = netutil.CappedThreadingHTTPServer(("127.0.0.1", 0), H, **kw)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def _reply(c, limit=10):
    c.settimeout(limit)
    data = b""
    try:
        while True:
            chunk = c.recv(4096)
            if not chunk:
                break
            data += chunk
    except OSError:
        pass
    c.close()
    return data


def _drip_until_closed(c, limit, drip):
    c.settimeout(0.3)
    t0 = time.monotonic()
    while time.monotonic() - t0 < limit:
        try:
            c.sendall(drip)
            if c.recv(4096) == b"":
                return time.monotonic() - t0
        except socket.timeout:
            continue
        except OSError:
            return time.monotonic() - t0
    return None


def test_shared_server_cuts_slow_headers_and_bodies():
    srv, port = _echo_server(header_deadline=1, body_deadline_base=1)
    try:
        c = socket.create_connection(("127.0.0.1", port))
        c.sendall(b"GET / HTTP/1.0\r\nX-a: ")
        took = _drip_until_closed(c, 8, b"a")
        c.close()
        assert took is not None and took < 3.5
        c = socket.create_connection(("127.0.0.1", port))
        c.sendall(b"POST / HTTP/1.0\r\nContent-Length: 3000\r\n\r\n")
        took = _drip_until_closed(c, 8, b"a")
        c.close()
        assert took is not None and took < 3.5
    finally:
        srv.shutdown()
        srv.server_close()


def test_shared_server_body_limit_ends_once_the_body_is_in():
    srv, port = _echo_server(header_deadline=1, body_deadline_base=1)
    try:
        c = socket.create_connection(("127.0.0.1", port))
        c.sendall(b"POST / HTTP/1.0\r\nX-Work: 2\r\nContent-Length: 5\r\n\r\nhello")
        assert _reply(c).endswith(b"got 5")
    finally:
        srv.shutdown()
        srv.server_close()


def test_get_with_content_length_arms_no_body_limit():
    srv, port = _echo_server(header_deadline=1, body_deadline_base=0.5)
    try:
        c = socket.create_connection(("127.0.0.1", port))
        c.sendall(b"GET / HTTP/1.0\r\nX-Work: 1.5\r\nContent-Length: 10\r\n\r\n")
        assert _reply(c).endswith(b"ok")
    finally:
        srv.shutdown()
        srv.server_close()


def test_webwallet_handler_gets_the_deadlines():
    import webwallet
    up = webwallet.Upstream("http://127.0.0.1:9", "http://127.0.0.1:9")
    srv = netutil.CappedThreadingHTTPServer(("127.0.0.1", 0), webwallet.make_handler(up), header_deadline=1)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = socket.create_connection(("127.0.0.1", srv.server_address[1]))
        c.sendall(b"GET / HTTP/1.0\r\nX-a: ")
        took = _drip_until_closed(c, 8, b"a")
        c.close()
        assert took is not None and took < 3.5
    finally:
        srv.shutdown()
        srv.server_close()
