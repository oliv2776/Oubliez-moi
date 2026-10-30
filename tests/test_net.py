import socket
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest

from app.net import UnsafeAddressError, _resolve_pinned, safe_urlopen


def test_dns_resolution_rejects_private_address_mixed_with_public(monkeypatch):
    def fake_getaddrinfo(host, port, type):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
        ]
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(UnsafeAddressError):
        _resolve_pinned("rebind.example", 443)


def test_dns_resolution_pins_explicit_private_destination(monkeypatch):
    def fake_getaddrinfo(host, port, type):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert _resolve_pinned("localhost", 8080, allow_private=True) == "127.0.0.1"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"pinned")
    def log_message(self, *args):
        pass


def test_safe_urlopen_connects_to_pinned_local_ip():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        with safe_urlopen(f"http://127.0.0.1:{server.server_port}/", allow_private=True) as response:
            assert response.read() == b"pinned"
    finally:
        server.shutdown()


def test_safe_urlopen_rejects_non_http_schemes():
    with pytest.raises(UnsafeAddressError):
        safe_urlopen("file:///etc/passwd")
