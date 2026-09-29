"""Connect only to vetted addresses, keeping TLS verification bound to the hostname."""

import http.client
import ipaddress
import socket
import ssl
import urllib.parse
import urllib.request

from .model import GillsError


def validate_url(value, policy):
    try:
        if not isinstance(value, str) or any(ord(c) <= 32 or ord(c) == 127 for c in value):
            raise ValueError
        parsed = urllib.parse.urlsplit(value)
        if (
            parsed.scheme not in ("https", "http")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or "\\" in value
            or "%" in parsed.netloc
            or parsed.port == 0
        ):
            raise ValueError
    except ValueError as exc:
        raise GillsError("Invalid HTTP(S) URL") from exc
    if parsed.scheme != "https" and not policy.get("allow_http", False):
        raise GillsError("HTTPS is required; allow_http must be explicitly enabled")
    return parsed


def permitted(address, allow_private):
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            return permitted(str(ip.ipv4_mapped), allow_private)
        # Avoid transition mechanisms embedding addresses outside the validated family.
        if ip.sixtofour or ip.teredo or ip in ipaddress.ip_network("64:ff9b::/96"):
            return False
    if ip.is_multicast or ip.is_unspecified:
        return False
    if allow_private:
        return True
    if isinstance(ip, ipaddress.IPv6Address):
        # Keep classifications consistent on supported Python versions.
        return (
            ip.is_global
            and not ip.is_reserved
            and not ip.is_site_local
            and ip in ipaddress.ip_network("2000::/3")
            and ip not in ipaddress.ip_network("2001::/23")
        )
    return (
        ip.is_global
        and not ip.is_reserved
        and ip not in ipaddress.ip_network("192.0.0.0/24")
        and ip not in ipaddress.ip_network("192.88.99.0/24")
    )


def connect(address, timeout, source_address=None, *, allow_private=False):
    host, port = address
    # Resolve once, vet every result, and connect using the numeric sockaddr directly.
    # A separate hostname connection here would create a DNS-rebinding race.
    answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not answers or any(not permitted(a[4][0], allow_private) for a in answers):
        raise GillsError("Connection to a non-public or prohibited IP address refused")
    last_error = None
    for family, kind, protocol, _, sockaddr in answers:
        sock = socket.socket(family, kind, protocol)
        try:
            sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            last_error = exc
            sock.close()
    raise OSError("Could not connect to an approved address") from last_error


class SafeHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, policy):
        super().__init__()
        self.policy = policy

    def connection(self, host, **kwargs):
        connection = http.client.HTTPConnection(host, **kwargs)
        connection._create_connection = lambda address, timeout, source_address=None: connect(
            address,
            timeout,
            source_address,
            allow_private=self.policy.get("allow_private_networks", False),
        )
        return connection

    def http_open(self, req):
        validate_url(req.full_url, self.policy)
        return self.do_open(self.connection, req)


class SafeHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, policy):
        super().__init__()
        self.policy = policy

    def connection(self, host, **kwargs):
        # No insecure mode: validate the certificate chain and original DNS hostname.
        connection = http.client.HTTPSConnection(
            host, context=ssl.create_default_context(), **kwargs
        )
        connection._create_connection = lambda address, timeout, source_address=None: connect(
            address,
            timeout,
            source_address,
            allow_private=self.policy.get("allow_private_networks", False),
        )
        return connection

    def https_open(self, req):
        validate_url(req.full_url, self.policy)
        return self.do_open(self.connection, req)
