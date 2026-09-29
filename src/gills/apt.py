from __future__ import annotations

import datetime as dt
import email.utils
import io
import re

from debian.deb822 import Deb822

from .model import Artifact, IntegrityError, Package, Snapshot, GillsError
from .transport import FetchError, decompress, repository_url


def release_text(data):
    """Read the cleartext section of InRelease without verifying its signature."""
    data = data.replace(b"\r\n", b"\n")
    if not data.startswith(b"-----BEGIN PGP SIGNED MESSAGE-----\n"):
        raise IntegrityError("Malformed InRelease: expected cleartext metadata")
    try:
        _, content = data.split(b"\n\n", 1)
        body, _ = content.split(b"\n-----BEGIN PGP SIGNATURE-----", 1)
    except ValueError as exc:
        raise IntegrityError(
            "Malformed InRelease: missing cleartext boundaries"
        ) from exc
    return (
        b"\n".join(
            line[2:] if line.startswith(b"- ") else line for line in body.split(b"\n")
        )
        + b"\n"
    )


def paragraphs(data):
    try:
        yield from Deb822.iter_paragraphs(
            io.StringIO(data.decode("utf-8")), use_apt_pkg=False
        )
    except (UnicodeError, ValueError) as exc:
        raise IntegrityError("Malformed Debian metadata") from exc


def release_checksums(release):
    result = {}
    for line in release.get("SHA256", "").splitlines():
        if not line.strip():
            continue
        checksum, size, path = line.split()
        if path in result:
            raise IntegrityError("Duplicate index path in Release")
        result[path] = (checksum, int(size))
    if not result:
        raise IntegrityError("Release has no SHA256 index checksums")
    return result


def validate_date(release, watch, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    for field in ("Date", "Valid-Until"):
        if field not in release:
            continue
        try:
            value = email.utils.parsedate_to_datetime(release[field])
            if value.tzinfo is None:
                raise ValueError("missing timezone")
        except (ValueError, TypeError) as exc:
            raise IntegrityError(f"Invalid Release {field}") from exc
        if field == "Valid-Until" and value < now:
            raise IntegrityError("Repository metadata has expired (Valid-Until)")
        if field == "Date":
            if value > now + dt.timedelta(minutes=10):
                raise IntegrityError("Repository metadata is dated in the future")
            age = watch.get("max_release_age_seconds", 0)
            if age and (now - value).total_seconds() > age:
                raise IntegrityError(
                    "Repository metadata exceeds max_release_age_seconds"
                )
    if watch.get("max_release_age_seconds") and "Date" not in release:
        raise IntegrityError("Release lacks Date required for age checking")


def packages(data, base, suite, component, kind):
    result = []
    for stanza in paragraphs(data):
        for required in ("Package", "Version"):
            if required not in stanza:
                raise IntegrityError(f"Package stanza missing {required}")
        name, version = stanza["Package"], stanza["Version"]
        if kind == "source":
            artifacts = []
            directory = stanza.get("Directory", "")
            for line in stanza.get("Checksums-Sha256", "").splitlines():
                if not line.strip():
                    continue
                checksum, size, filename = line.split()
                artifacts.append(
                    Artifact(
                        repository_url(base, directory + "/" + filename),
                        checksum,
                        int(size),
                        filename,
                    )
                )
            if not artifacts:
                raise IntegrityError("Source stanza has no SHA256 artifacts")
            result.append(
                Package(
                    name,
                    version,
                    "source",
                    suite,
                    component,
                    kind,
                    name,
                    version,
                    tuple(artifacts),
                )
            )
        else:
            source = re.fullmatch(
                r"([^\s()]+)(?:\s+\(([^()]+)\))?", stanza.get("Source", name)
            )
            if not source:
                raise IntegrityError("Invalid binary Source field")
            for field in ("Architecture", "Filename", "SHA256", "Size"):
                if field not in stanza:
                    raise IntegrityError(f"Binary stanza missing {field}")
            filename = stanza["Filename"]
            result.append(
                Package(
                    name,
                    version,
                    stanza["Architecture"],
                    suite,
                    component,
                    kind,
                    source[1],
                    source[2] or version,
                    (
                        Artifact(
                            repository_url(base, filename),
                            stanza["SHA256"],
                            int(stanza["Size"]),
                            filename.rsplit("/", 1)[-1],
                        ),
                    ),
                )
            )
    return result


def scan(watch, client):
    result = Snapshot([])
    need_source = "source" in watch["kinds"] or watch["readiness"].get("require_source")
    need_binary = "binary" in watch["kinds"] or bool(watch["readiness"].get("binaries"))
    for suite in watch["suites"]:
        base = repository_url(watch["url"], f"dists/{suite}/")
        try:
            raw = client.fetch(base + "Release")
        except FetchError as exc:
            if exc.status != 404:
                raise
            raw = release_text(client.fetch(base + "InRelease"))
        releases = list(paragraphs(raw))
        if len(releases) != 1:
            raise IntegrityError("Expected one Release stanza")
        release = releases[0]
        validate_date(release, watch)
        checksums = release_checksums(release)
        result.releases[suite] = {
            k: release[k]
            for k in ("Origin", "Suite", "Codename", "Date", "Valid-Until")
            if k in release
        }
        if release.get("Components") and not set(watch["components"]) <= set(
            release["Components"].split()
        ):
            raise GillsError("Configured component is absent from Release")
        if (
            need_binary
            and release.get("Architectures")
            and not set(watch["architectures"]) <= set(release["Architectures"].split())
        ):
            raise GillsError("Configured architecture is absent from Release")
        for component in watch["components"]:
            targets = []
            if need_source:
                targets.append((f"{component}/source/Sources", "source"))
            if need_binary:
                targets.extend(
                    (f"{component}/binary-{arch}/Packages", "binary")
                    for arch in watch["architectures"]
                )
            for prefix, kind in targets:
                path = next(
                    (
                        prefix + ext
                        for ext in (".xz", ".gz", ".bz2", "")
                        if prefix + ext in checksums
                    ),
                    None,
                )
                if path is None:
                    raise GillsError(f"Required index absent: {prefix}")
                checksum, size = checksums[path]
                index_url = repository_url(base, path)
                if release.get("Acquire-By-Hash", "").lower() == "yes":
                    index_url = repository_url(
                        base, path.rsplit("/", 1)[0] + "/by-hash/SHA256/" + checksum
                    )
                payload = client.fetch(index_url, checksum, size)
                result.packages.extend(
                    packages(
                        decompress(payload, path, client.limit),
                        watch["url"],
                        suite,
                        component,
                        kind,
                    )
                )
    # Architecture: all can occur in several architecture indexes.
    unique = {}
    for package in result.packages:
        previous = unique.get(package.identity)
        if previous and previous.content != package.content:
            raise IntegrityError("Conflicting duplicate package metadata")
        unique[package.identity] = package
    result.packages = list(unique.values())
    return result
