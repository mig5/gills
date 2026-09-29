from __future__ import annotations

import bz2
import gzip
import hashlib
import io
import json
import lzma
import re
import urllib.error
import urllib.parse
import urllib.request

import zstandard

from .config import secret
from .model import GillsError, IntegrityError
from .network import SafeHTTPHandler, SafeHTTPSHandler, validate_url


class FetchError(GillsError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def repository_url(base, relative):
    """Treat metadata paths as untrusted; never leave the configured origin/root."""
    parsed = urllib.parse.urlsplit(relative)
    decoded = urllib.parse.unquote(parsed.path)
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or decoded.startswith("/")
        or "\\" in decoded
        or any(p in ("..", ".") for p in decoded.split("/"))
        or any(ord(c) < 32 for c in decoded)
    ):
        raise IntegrityError("Unsafe artifact or index path in repository metadata")
    return base.rstrip("/") + "/" + urllib.parse.quote(decoded, safe="/+~:@!$&'()*,-_=")


class RestrictedRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, policy):
        self.policy = policy
        super().__init__()

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl, self.policy)
        old, new = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        if new.scheme not in ("http", "https") or (old.scheme == "https" and new.scheme != "https"):
            raise FetchError("Unsafe HTTP redirect refused")
        if req.get_method() != "GET":
            raise FetchError("Notification redirects are refused")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if (old.scheme, old.netloc) != (new.scheme, new.netloc):
            # Authentication headers must never follow cross-origin redirects.
            redirected.headers = {
                "User-agent": "gills/0.1.0",
                "Accept-encoding": "identity",
            }
        return redirected


def headers_from_env(name):
    if not name:
        return {}
    try:
        value = json.loads(secret(name))
    except json.JSONDecodeError as exc:
        raise GillsError(f"{name} must contain a JSON object of HTTP headers") from exc
    if not isinstance(value, dict) or any(
        not isinstance(k, str)
        or not isinstance(v, str)
        or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", k)
        or any(ord(c) < 32 or ord(c) == 127 for c in v)
        or k.lower()
        in {
            "host",
            "content-length",
            "transfer-encoding",
            "connection",
            "proxy-authorization",
            "proxy-connection",
            "upgrade",
        }
        for k, v in value.items()
    ):
        raise GillsError(
            f"{name} must contain safe string HTTP headers; routing/framing headers are forbidden"
        )
    return value


class Client:
    def __init__(self, config, state_dir, watch=None, *, policy=None):
        self.timeout = config["timeout_seconds"]
        self.limit = config["max_index_bytes"]
        self.headers = headers_from_env((watch or {}).get("headers_env"))
        self.policy = watch if watch is not None else (policy or {})
        # Environment proxies could resolve/connect elsewhere, bypassing address checks.
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            RestrictedRedirect(self.policy),
            SafeHTTPHandler(self.policy),
            SafeHTTPSHandler(self.policy),
        )

    def open(self, url, headers=None, data=None):
        validate_url(url, self.policy)
        req = urllib.request.Request(  # noqa: S310 -- scheme validated above
            url,
            data=data,
            headers={
                "User-Agent": "gills/0.1.0",
                "Accept-Encoding": "identity",
                **(headers or {}),
            },
        )
        try:
            return self.opener.open(req, timeout=self.timeout)  # noqa: S310 -- HTTP(S) validated above and on redirects
        except urllib.error.HTTPError as exc:
            # Never expose URLs: webhook paths and query strings can be secrets.
            raise FetchError(f"HTTP {exc.code}", exc.code) from exc
        except (OSError, urllib.error.URLError, ValueError) as exc:
            raise FetchError(f"HTTP request failed ({type(exc).__name__})") from exc

    def fetch(self, url, expected=None, size=None):
        if size is not None and (size < 0 or size > self.limit):
            raise FetchError("Index exceeds configured size limit")
        if expected:
            _checksum(expected)
        with self.open(url, self.headers) as response:
            data = response.read(self.limit + 1)
        if len(data) > self.limit:
            raise FetchError("Index exceeds configured size limit")
        if size is not None and len(data) != size:
            raise IntegrityError("Repository index size mismatch; publication may be in progress")
        if expected:
            if hashlib.sha256(data).hexdigest() != expected:
                raise IntegrityError(
                    "Repository index checksum mismatch; publication may be in progress"
                )
        return data


def _checksum(value):
    if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise IntegrityError("Missing or invalid SHA256 checksum in repository metadata")


def decompress(data, path, limit):
    factory = next(
        (
            f
            for suffix, f in (
                (".xz", lzma.LZMAFile),
                (".gz", gzip.open),
                (".bz2", bz2.BZ2File),
            )
            if path.endswith(suffix)
        ),
        None,
    )
    try:
        if path.endswith((".zst", ".zstd")):
            with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data)) as stream:
                data = stream.read(limit + 1)
        elif factory:
            with factory(io.BytesIO(data)) as stream:
                data = stream.read(limit + 1)
    except (OSError, EOFError, lzma.LZMAError, zstandard.ZstdError) as exc:
        raise IntegrityError("Invalid compressed repository index") from exc
    if len(data) > limit:
        raise FetchError("Decompressed index exceeds configured size limit")
    return data
