import socket
import ssl
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock

import pytest

from gills.model import GillsError
from gills.network import connect, permitted, validate_url
from gills.transport import Client, RestrictedRedirect, headers_from_env


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "100.100.100.200",
        "0.0.0.0",
        "224.0.0.1",
        "192.0.2.1",
        "::1",
        "fe80::1",
        "fc00::1",
        "::",
        "ff02::1",
        "::ffff:127.0.0.1",
        "64:ff9b::a00:1",
        "2002:7f00:1::",
        "fec0::1",
        "192.0.0.8",
        "192.88.99.1",
    ],
)
def test_nonpublic_addresses_denied(address):
    assert not permitted(address, False)


def test_private_opt_in():
    assert permitted("10.0.0.1", True)
    assert permitted("::1", True)
    assert permitted("8.8.8.8", False)
    assert permitted("2606:4700:4700::1111", False)
    assert not permitted("0.0.0.0", True)


@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.169.254"])
def test_mixed_dns_rejected_before_connect(monkeypatch, address):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)),
        ],
    )
    factory = Mock()
    monkeypatch.setattr(socket, "socket", factory)
    with pytest.raises(GillsError, match="non-public"):
        connect(("repo.example", 443), 1)
    factory.assert_not_called()


def test_dns_is_resolved_once_and_socket_is_pinned(monkeypatch):
    resolver = Mock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))])
    sock = Mock()
    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    monkeypatch.setattr(socket, "socket", Mock(return_value=sock))
    assert connect(("repo.example", 443), 2) is sock
    resolver.assert_called_once()
    sock.connect.assert_called_once_with(("8.8.8.8", 443))


@pytest.mark.parametrize(
    "url",
    [
        "http://example.org/",
        "file:///etc/passwd",
        "https://user:password@example.org/",
        "https://example.org/\nfoo",
        "https://example.org:invalid/",
        "https://[fe80::1%25eth0]/",
    ],
)
def test_unsafe_urls_rejected(url):
    with pytest.raises(GillsError):
        validate_url(url, {})


def test_redirect_policy_and_credential_stripping():
    handler = RestrictedRedirect({})
    req = urllib.request.Request(
        "https://example.org/a", headers={"Authorization": "secret", "X-Key": "secret"}
    )
    with pytest.raises(GillsError):
        handler.redirect_request(req, None, 302, "redirect", {}, "http://example.org/b")
    redirected = handler.redirect_request(req, None, 302, "redirect", {}, "https://other.example/b")
    assert not redirected.has_header("Authorization")
    assert not redirected.has_header("X-key")
    post = urllib.request.Request("https://example.org/a", data=b"{}")
    with pytest.raises(GillsError, match="redirects"):
        handler.redirect_request(post, None, 302, "redirect", {}, "https://example.org/b")


def test_private_redirect_connection_is_checked(config, monkeypatch):
    handler = RestrictedRedirect({})
    req = urllib.request.Request("https://example.org/a")
    redirected = handler.redirect_request(
        req, None, 302, "redirect", {}, "https://127.0.0.1/private"
    )
    sock = Mock()
    monkeypatch.setattr(socket, "socket", sock)
    with pytest.raises(GillsError, match="non-public"):
        Client(config, config["state_dir"]).open(redirected.full_url)
    sock.assert_not_called()


def test_proxy_environment_ignored(config, monkeypatch):
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:1")
    client = Client(config, config["state_dir"])
    assert not any(isinstance(h, urllib.request.ProxyHandler) for h in client.opener.handlers)


@pytest.mark.parametrize(
    "header", ["Host", "Content-Length", "Transfer-Encoding", "Connection", "Proxy-Authorization"]
)
def test_routing_headers_rejected(header, monkeypatch):
    import json

    monkeypatch.setenv("HEADERS", json.dumps({header: "unsafe"}))
    with pytest.raises(GillsError):
        headers_from_env("HEADERS")


def test_tls_certificate_and_hostname_verification(config, monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    fixtures = Path(__file__).parent / "fixtures"
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(fixtures / "localhost.crt", fixtures / "localhost.key")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = Client(config, config["state_dir"], policy={"allow_private_networks": True})
        with pytest.raises(GillsError):
            client.open(f"https://localhost:{server.server_port}/")
        original = ssl.create_default_context
        monkeypatch.setattr(
            ssl, "create_default_context", lambda: original(cafile=str(fixtures / "localhost.crt"))
        )
        with client.open(f"https://localhost:{server.server_port}/") as response:
            assert response.read() == b"ok"
        with pytest.raises(GillsError):
            client.open(f"https://127.0.0.1:{server.server_port}/")
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
