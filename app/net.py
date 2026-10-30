"""HTTP sortant avec résolution DNS épinglée pour limiter le DNS rebinding.

L'adresse IP utilisée pour la connexion est celle validée juste avant la requête ;
la résolution n'est donc pas refaite par la couche socket après validation.
Les redirections sont refusées par défaut : chaque nouvelle destination doit être
validée explicitement par l'appelant.
"""
from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request


class UnsafeAddressError(ValueError):
    """URL invalide, destination interdite ou hôte impossible à résoudre."""


def _resolve_pinned(host: str, port: int, allow_private: bool = False) -> str:
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
    # Une seule IP est choisie et réutilisée pour la socket, sans seconde résolution.
    return addresses[0]


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, *, pinned_ip: str, **kwargs):
        super().__init__(host, **kwargs)
        self._pinned_ip = pinned_ip

    def connect(self):
        self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout, self.source_address)
        if self._tunnel_host:
            self._tunnel()


class _PinnedHTTPSConnection(_PinnedHTTPConnection):
    def __init__(self, host, *, pinned_ip: str, context: ssl.SSLContext, **kwargs):
        super().__init__(host, pinned_ip=pinned_ip, **kwargs)
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
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    pinned_ip = _resolve_pinned(parsed.hostname, port, allow_private=allow_private)
    request.timeout = timeout

    class _HTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(lambda host, **kw: _PinnedHTTPConnection(host, pinned_ip=pinned_ip, **kw), req)

    class _HTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            tls = context or ssl.create_default_context()
            return self.do_open(
                lambda host, **kw: _PinnedHTTPSConnection(host, pinned_ip=pinned_ip, context=tls, **kw), req
            )

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _HTTPHandler(), _HTTPSHandler(), _NoRedirect())
    return opener.open(request, timeout=timeout)
