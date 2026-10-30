import socket
import urllib.error
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


def test_safe_urlopen_handles_localhost_dual_stack_resolution():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        with safe_urlopen(f"http://localhost:{server.server_port}/", allow_private=True) as response:
            assert response.read() == b"pinned"
    finally:
        server.shutdown()


def test_safe_urlopen_rejects_non_http_schemes():
    with pytest.raises(UnsafeAddressError):
        safe_urlopen("file:///etc/passwd")


def test_safe_urlopen_resolves_only_once_before_connecting(monkeypatch):
    """Une seconde résolution DNS ne doit pas pouvoir remplacer l'IP validée."""
    calls = []
    attempted = []

    def fake_getaddrinfo(host, port, type):
        calls.append((host, port))
        # Une deuxième résolution aurait renvoyé une IP privée : elle ne doit jamais arriver.
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]

    def fake_create_connection(address, timeout, source_address=None):
        attempted.append(address)
        raise OSError("connexion simulée")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", fake_create_connection)
    with pytest.raises(urllib.error.URLError):
        safe_urlopen("http://rebind.example/resource")

    assert calls == [("rebind.example", 80)]
    assert attempted == [("8.8.8.8", 80)]


# ------------------------------------------------------------------ HTTPS ----

def _self_signed(tmp_path, name="localhost"):
    import datetime
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_path, key_path


def test_https_without_explicit_port_uses_443(monkeypatch):
    """Régression : https://hôte/ partait sur le port 80 (« SSL: WRONG_VERSION_NUMBER »)."""
    import app.net as net
    ports = []

    def fake_connect(address, timeout=None, source_address=None):
        ports.append(address[1])
        raise ConnectionRefusedError("test")

    monkeypatch.setattr(net, "_resolve_pinned_all", lambda host, port, allow_private=False: ["127.0.0.1"])
    monkeypatch.setattr(net.socket, "create_connection", fake_connect)
    for url in ("https://login.example/oauth2/v2.0/devicecode", "http://catalog.example/data.json"):
        with pytest.raises(urllib.error.URLError):
            safe_urlopen(url)
    assert ports == [443, 80]


def test_https_round_trip_checks_certificate_hostname(tmp_path):
    import ssl
    cert, key = _self_signed(tmp_path)
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    Thread(target=server.serve_forever, daemon=True).start()
    trust = ssl.create_default_context(cafile=str(cert))
    try:
        port = server.server_address[1]
        with safe_urlopen(f"https://localhost:{port}/", allow_private=True, context=trust) as resp:
            assert resp.status == 200
        with pytest.raises(urllib.error.URLError, match="CERTIFICATE_VERIFY_FAILED|doesn't match|IP address mismatch"):
            safe_urlopen(f"https://127.0.0.1:{port}/", allow_private=True, context=trust)  # nom ≠ certificat
    finally:
        server.shutdown()
