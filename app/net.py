"""HTTP et sockets sortants avec résolution DNS épinglée contre le DNS rebinding."""
from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import urllib.parse
import urllib.request


class UnsafeAddressError(OSError):
    """URL invalide, destination interdite ou hôte impossible à résoudre."""


def _resolve_pinned_all(host: str, port: int, allow_private: bool = False) -> list[str]:
    """Résout une seule fois et valide toutes les adresses retournées."""
    try:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise UnsafeAddressError(f"résolution DNS impossible pour {host}") from exc
    addresses = []
    for record in records:
        try:
            address = ipaddress.ip_address(record[4][0].split("%", 1)[0])
        except ValueError as exc:
            raise UnsafeAddressError(f"adresse DNS invalide pour {host}") from exc
        if not allow_private and not address.is_global:
            raise UnsafeAddressError(f"destination non publique refusée pour {host}")
        value = str(address)
        if value not in addresses:
            addresses.append(value)
    if not addresses:
        raise UnsafeAddressError(f"aucune adresse DNS pour {host}")
    return addresses


def _resolve_pinned(host: str, port: int, allow_private: bool = False) -> str:
    """Retourne la première IP validée (utile aux tests et diagnostics)."""
    return _resolve_pinned_all(host, port, allow_private)[0]


def create_pinned_socket(host: str, port: int, timeout=None, source_address=None, *, allow_private: bool = True):
    """Ouvre une socket vers une des IP résolues, sans nouvelle résolution DNS."""
    addresses = _resolve_pinned_all(host, port, allow_private=allow_private)
    last_error = None
    for address in addresses:
        try:
            return socket.create_connection((address, port), timeout, source_address)
        except OSError as exc:
            last_error = exc
    raise last_error or OSError(f"connexion impossible à {host}:{port}")


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, *, pinned_ips: list[str], **kwargs):
        super().__init__(host, **kwargs)
        self._pinned_ips = pinned_ips

    def connect(self):
        last_error = None
        for address in self._pinned_ips:
            try:
                self.sock = socket.create_connection((address, self.port), self.timeout, self.source_address)
                break
            except OSError as exc:
                last_error = exc
        else:
            raise last_error or OSError(f"connexion impossible à {self.host}:{self.port}")
        if self._tunnel_host:
            self._tunnel()


class _PinnedHTTPSConnection(_PinnedHTTPConnection):
    # Hérite de HTTPConnection (port 80 par défaut) : sans cette ligne, une URL https
    # sans port explicite partait sur le port 80 → « SSL: WRONG_VERSION_NUMBER ».
    default_port = http.client.HTTPS_PORT

    def __init__(self, host, *, pinned_ips: list[str], context: ssl.SSLContext, **kwargs):
        super().__init__(host, pinned_ips=pinned_ips, **kwargs)
        self._context = context

    def connect(self):
        super().connect()
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def safe_urlopen(url_or_request, *, timeout: float = 20, allow_private: bool = False,
                 context: ssl.SSLContext | None = None):
    request = url_or_request if isinstance(url_or_request, urllib.request.Request) else urllib.request.Request(url_or_request)
    parsed = urllib.parse.urlsplit(request.full_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise UnsafeAddressError("URL HTTP(S) invalide ou contenant des identifiants")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise UnsafeAddressError("port de destination invalide") from exc
    # Les adresses sont validées une fois puis utilisées directement par la socket.
    pinned_ips = _resolve_pinned_all(parsed.hostname, port, allow_private=allow_private)

    class _HTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(lambda host, **kw: _PinnedHTTPConnection(host, pinned_ips=pinned_ips, **kw), req)

    class _HTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            tls = context or ssl.create_default_context()
            return self.do_open(
                lambda host, **kw: _PinnedHTTPSConnection(host, pinned_ips=pinned_ips, context=tls, **kw), req
            )

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _HTTPHandler(), _HTTPSHandler(), _NoRedirect())
    return opener.open(request, timeout=timeout)
