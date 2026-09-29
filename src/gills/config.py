from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from .model import GillsError, digest
from .network import validate_url

EVENTS = {
    "package.added",
    "package.updated",
    "package.removed",
    "package.downgraded",
    "package.repacked",
    "repository.error",
    "repository.recovered",
    "readiness.timeout",
}


def fail(message):
    raise GillsError("Configuration: " + message)


def keys(mapping, allowed, where):
    if not isinstance(mapping, dict):
        fail(f"{where} must be a mapping")
    extra = set(mapping) - set(allowed.split())
    if extra:
        fail(f"unknown keys in {where}: {', '.join(sorted(extra))}")


def strings(value, where, nonempty=False):
    if not isinstance(value, list) or any(
        not isinstance(v, str) or not v for v in value
    ):
        fail(f"{where} must be a list of nonempty strings")
    if nonempty and not value:
        fail(f"{where} cannot be empty")


def integer(value, where, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        fail(f"{where} must be an integer >= {minimum}")


def boolean(value, where):
    if not isinstance(value, bool):
        fail(f"{where} must be true or false")


def network_policy(mapping, name):
    for field in ("allow_http", "allow_private_networks"):
        mapping.setdefault(field, False)
        boolean(mapping[field], f"{name}.{field}")


def url(value, where):
    if not isinstance(value, str) or not value:
        fail(f"{where} must be a nonempty URL string")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in ("https", "http")
        or not parsed.netloc
        or parsed.username
        or parsed.password
    ):
        fail(f"{where} must be an HTTP(S) URL without embedded credentials")
    if parsed.fragment:
        fail(f"{where} cannot contain a fragment")


def load(path):
    path = Path(path).resolve()
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise GillsError(f"Cannot read configuration: {type(exc).__name__}") from exc
    keys(
        data,
        "version state_dir notify timeout_seconds max_index_bytes watches destinations",
        "root",
    )
    if data.get("version") != 1:
        fail("version must be 1")
    data.setdefault("state_dir", "./state")
    if not isinstance(data["state_dir"], str) or not data["state_dir"]:
        fail("state_dir must be a nonempty path string")
    data["state_dir"] = str((path.parent / data["state_dir"]).resolve())
    data.setdefault("notify", True)
    boolean(data["notify"], "notify")
    for k, default in (
        ("timeout_seconds", 30),
        ("max_index_bytes", 536870912),
    ):
        data.setdefault(k, default)
        integer(data[k], k, 1)
    data.setdefault("destinations", {})
    if not isinstance(data["destinations"], dict):
        fail("destinations must be a mapping")
    for name, d in data["destinations"].items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
            fail("destination names must be letters/digits/hyphens/underscores")
        keys(
            d,
            "type enabled url url_env headers_env secret_env host port username_env password_env from to tls number_env recipients events digest_seconds allow_http allow_private_networks",
            f"destination {name}",
        )
        for field in (
            "url_env",
            "headers_env",
            "secret_env",
            "username_env",
            "password_env",
            "number_env",
        ):
            if field in d and (
                not isinstance(d[field], str)
                or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", d[field])
            ):
                fail(f"{name}.{field} must name an environment variable")
        network_policy(d, name)
        d.setdefault("enabled", True)
        boolean(d["enabled"], f"{name}.enabled")
        if d.get("type") not in ("webhook", "slack", "signal", "email", "stdout"):
            fail(f"invalid destination type for {name}")
        for k in ("events", "to", "recipients"):
            if k in d:
                strings(d[k], f"{name}.{k}")
        if set(d.get("events", [])) - EVENTS:
            fail(f"unknown event in destination {name}")
        d.setdefault("digest_seconds", 0)
        integer(d["digest_seconds"], f"{name}.digest_seconds")
        if d["type"] in ("webhook", "slack", "signal"):
            if bool(d.get("url")) == bool(d.get("url_env")):
                fail(f"{name} needs exactly one of url or url_env")
            if d.get("url"):
                url(d["url"], f"{name}.url")
                validate_url(d["url"], d)
        if d["type"] == "signal" and (
            not d.get("number_env") or not d.get("recipients")
        ):
            fail(f"{name} needs number_env and recipients")
        if d["type"] == "email":
            if not all(d.get(k) for k in ("host", "from", "to")):
                fail(f"{name} needs host, from, and to")
            if bool(d.get("username_env")) != bool(d.get("password_env")):
                fail(
                    f"{name} needs both username_env and password_env for authentication"
                )
            for field in ("host", "from"):
                if not isinstance(d[field], str) or any(c in d[field] for c in "\r\n"):
                    fail(f"{name}.{field} must be a string without newlines")
            d.setdefault("tls", "starttls")
            if d["tls"] not in ("starttls", "ssl", "none"):
                fail(f"invalid TLS mode for {name}")
            integer(d.get("port", 465 if d["tls"] == "ssl" else 587), f"{name}.port", 1)
    if not isinstance(data.get("watches"), list) or not data["watches"]:
        fail("watches must be a nonempty list")
    names = set()
    for w in data["watches"]:
        keys(
            w,
            "name type url suites components architectures kinds filters events destinations version_policy notify_initial group_by_source readiness delivery_delay_seconds failure_threshold max_release_age_seconds headers_env allow_http allow_private_networks",
            "watch",
        )
        name = w.get("name", "")
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[a-zA-Z0-9_-]+", name)
            or name in names
        ):
            fail("watch names must be unique letters/digits/hyphens/underscores")
        names.add(name)
        w.setdefault("destinations", [])
        strings(w["destinations"], f"{name}.destinations")
        if set(w["destinations"]) - data["destinations"].keys():
            fail(f"{name} references an unknown destination")
        if w.get("type") not in ("apt", "rpm"):
            fail(f"{name}.type must be apt or rpm")
        if not isinstance(w.get("url"), str):
            fail(f"{name}.url is required")
        network_policy(w, name)
        url(w["url"], f"{name}.url")
        validate_url(w["url"], w)
        if urlsplit(w["url"]).query:
            fail(f"{name}.url must be a repository base URL without a query")
        w["url"] = w["url"].rstrip("/") + "/"
        for k, default in (
            ("suites", [] if w["type"] == "apt" else ["default"]),
            ("components", ["main"]),
            ("architectures", ["amd64"] if w["type"] == "apt" else ["x86_64"]),
            ("kinds", ["source"]),
        ):
            w.setdefault(k, default)
            strings(w[k], f"{name}.{k}", True)
        if w["type"] == "rpm" and (
            len(w["suites"]) != 1 or w["components"] != ["main"]
        ):
            fail(
                "RPM uses one concrete base URL per watch, one suite label, and component main"
            )
        for k in ("suites", "components", "architectures"):
            if any(not re.fullmatch(r"[a-zA-Z0-9_.+-]+", v) for v in w[k]):
                fail(
                    f"unsafe {name}.{k}; use explicit suite/component/architecture names"
                )
        if set(w["kinds"]) - {"source", "binary"}:
            fail(f"invalid {name}.kinds")
        for k, default in (
            ("notify_initial", False),
            ("group_by_source", True),
        ):
            w.setdefault(k, default)
            boolean(w[k], f"{name}.{k}")
        for k, default in (
            ("delivery_delay_seconds", 0),
            ("failure_threshold", 3),
            ("max_release_age_seconds", 0),
        ):
            w.setdefault(k, default)
            integer(w[k], f"{name}.{k}", 1 if k == "failure_threshold" else 0)
        w.setdefault("version_policy", "any")
        if w["version_policy"] not in ("any", "upstream", "packaging"):
            fail(f"invalid {name}.version_policy")
        w.setdefault(
            "events",
            [
                "package.added",
                "package.updated",
                "package.downgraded",
                "package.repacked",
            ],
        )
        strings(w["events"], f"{name}.events")
        if set(w["events"]) - EVENTS:
            fail(f"unknown {name}.events")
        f = w.setdefault("filters", {})
        keys(
            f,
            "packages sources exclude_packages exclude_sources package_regex source_regex version_include version_exclude version_field min_version",
            f"{name}.filters",
        )
        for k in ("packages", "sources", "exclude_packages", "exclude_sources"):
            if k in f:
                strings(f[k], f"{name}.{k}")
        for k in (
            "package_regex",
            "source_regex",
            "version_include",
            "version_exclude",
        ):
            if k in f:
                try:
                    re.compile(f[k])
                except (re.error, TypeError) as exc:
                    fail(f"invalid {name}.{k}: {exc}")
        if "min_version" in f:
            if not isinstance(f["min_version"], str):
                fail("min_version must be a quoted version string")
            from .versions import compare

            try:
                compare(f["min_version"], f["min_version"], w["type"])
            except (ValueError, TypeError) as exc:
                fail(f"invalid min_version: {type(exc).__name__}")
        f.setdefault("version_field", "full")
        if f["version_field"] not in ("full", "upstream"):
            fail(f"invalid {name}.version_field")
        r = w.setdefault("readiness", {})
        keys(
            r,
            "require_source binaries architectures suites timeout_seconds",
            f"{name}.readiness",
        )
        if r:
            r.setdefault("require_source", True)
            boolean(r["require_source"], "readiness.require_source")
            for k, default in (
                ("binaries", []),
                ("architectures", w["architectures"]),
                ("suites", w["suites"]),
            ):
                r.setdefault(k, default)
                strings(r[k], f"readiness.{k}", k != "binaries")
            if not set(r["suites"]) <= set(w["suites"]) or not set(
                r["architectures"]
            ) <= set(w["architectures"]):
                fail("readiness suites/architectures must be included in the watch")
            r.setdefault("timeout_seconds", 3600)
            integer(r["timeout_seconds"], "readiness.timeout_seconds", 1)
        # Scope changes deliberately rebaseline; operational tweaks do not.
        w["scope"] = digest(
            {
                k: w[k]
                for k in (
                    "type",
                    "url",
                    "suites",
                    "components",
                    "architectures",
                    "kinds",
                    "filters",
                )
            }
        )
    return data


def secret(env_name):
    try:
        value = os.environ[env_name]
    except KeyError as exc:
        raise GillsError(
            f"Required environment variable {env_name} is not set"
        ) from exc
    if not value:
        raise GillsError(f"Environment variable {env_name} is empty")
    return value
