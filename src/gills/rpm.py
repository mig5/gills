"""RPM-MD adapter using repository metadata and its artifact checksums."""

import io

from defusedxml import ElementTree as ET

from .model import Artifact, IntegrityError, Package, Snapshot, GillsError
from .transport import decompress, repository_url

R = "{http://linux.duke.edu/metadata/repo}"
C = "{http://linux.duke.edu/metadata/common}"
P = "{http://linux.duke.edu/metadata/rpm}"


def scan(watch, client):
    raw = client.fetch(repository_url(watch["url"], "repodata/repomd.xml"))
    root = ET.fromstring(raw)
    primary = root.find(f"{R}data[@type='primary']")
    if primary is None:
        raise GillsError("RPM repository has no primary XML metadata")
    location, checksum, size = (
        primary.find(R + "location"),
        primary.find(R + "checksum"),
        primary.find(R + "size"),
    )
    if location is None or checksum is None or checksum.get("type") != "sha256":
        raise IntegrityError("RPM primary metadata requires location and SHA256 checksum")
    path = location.attrib["href"]
    payload = client.fetch(
        repository_url(watch["url"], path),
        checksum.text,
        int(size.text) if size is not None else None,
    )
    payload = decompress(payload, path, client.limit)
    suite = watch["suites"][0]
    result = Snapshot([], {suite: {"revision": root.findtext(R + "revision")}})
    for _, element in ET.iterparse(io.BytesIO(payload), events=("end",)):
        if element.tag != C + "package":
            continue
        name, arch = element.findtext(C + "name"), element.findtext(C + "arch")
        ver = element.find(C + "version")
        checksum, location, size = (
            element.find(C + "checksum"),
            element.find(C + "location"),
            element.find(C + "size"),
        )
        if (
            not name
            or not arch
            or ver is None
            or checksum is None
            or location is None
            or size is None
            or checksum.get("type") != "sha256"
        ):
            raise IntegrityError("Incomplete RPM package metadata or missing SHA256")
        epoch = ver.get("epoch", "0")
        version = f"{epoch}:{ver.attrib['ver']}-{ver.attrib['rel']}"
        kind = "source" if arch in ("src", "nosrc") else "binary"
        source, source_version = name, version
        source_rpm = element.findtext(f"{C}format/{P}sourcerpm")
        if kind == "binary" and source_rpm:
            stem = source_rpm.removesuffix(".rpm").rsplit(".", 1)[0]
            try:
                source, source_ver, source_rel = stem.rsplit("-", 2)
                source_version = f"{epoch}:{source_ver}-{source_rel}"
            except ValueError as exc:
                raise IntegrityError("Invalid RPM source RPM field") from exc
        if kind == "source" or arch in watch["architectures"] or arch == "noarch":
            path = location.attrib["href"]
            artifact = Artifact(
                repository_url(watch["url"], path),
                checksum.text,
                int(size.attrib["package"]),
                path.rsplit("/", 1)[-1],
            )
            result.packages.append(
                Package(
                    name,
                    version,
                    "source" if kind == "source" else arch,
                    suite,
                    "main",
                    kind,
                    source,
                    source_version,
                    (artifact,),
                )
            )
        element.clear()
    unique = {}
    for package in result.packages:
        previous = unique.get(package.identity)
        if previous and previous.content != package.content:
            raise IntegrityError("Conflicting duplicate RPM package metadata")
        unique[package.identity] = package
    result.packages = list(unique.values())
    return result
