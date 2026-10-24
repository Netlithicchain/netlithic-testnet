"""HTTP helper: urlopen restricted to http/https (no file:, ftp:, data: ...),
never following redirects (review R3), with an optional connect-time
address guard (review R2: the address actually connected to is checked, so
DNS rebinding between a resolve-check and the connect can't reach an
internal address)."""
import http.client
import ipaddress
import urllib.error
import urllib.request
from urllib.parse import urlparse
from urllib.request import Request

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


def _guarded_conn(base, allow_ip):
    class Conn(base):
        def connect(self):
            super().connect()
            _check_sock(self.sock, allow_ip)
    return Conn


def _opener(allow_ip=None):
    handlers = [_NoRedirect()]
    if allow_ip is not None:
        http_conn = _guarded_conn(http.client.HTTPConnection, allow_ip)
        https_conn = _guarded_conn(http.client.HTTPSConnection, allow_ip)

        class H(urllib.request.HTTPHandler):
            def http_open(self, req):
                return self.do_open(http_conn, req)

        class HS(urllib.request.HTTPSHandler):
            def https_open(self, req):
                return self.do_open(https_conn, req, context=self._context)

        # no env proxies: the guard must see the real peer, not a proxy
        handlers += [urllib.request.ProxyHandler({}), H(), HS()]
    return urllib.request.build_opener(*handlers)


def urlopen(req, *args, allow_ip=None, **kwargs):
    """Drop-in for urllib.request.urlopen: refuses non-http(s) URLs and 3xx
    redirects. If allow_ip(ip_address) is given, the socket's connected
    address must pass it or AddressNotAllowed is raised before any request
    bytes are sent."""
    url = req.full_url if isinstance(req, Request) else str(req)
    check_url(url)
    try:
        return _opener(allow_ip).open(req, *args, **kwargs)  # nosec B310 - scheme checked above
    except urllib.error.URLError as e:
        if isinstance(getattr(e, "reason", None), AddressNotAllowed):
            raise e.reason from None
        raise
