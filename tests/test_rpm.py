import gzip
import hashlib

import pytest

from gills import rpm
from gills.transport import Client


def test_rpm_primary_and_source_mapping(webroot, config):
    root, url = webroot
    primary = b"""<metadata xmlns="http://linux.duke.edu/metadata/common" xmlns:rpm="http://linux.duke.edu/metadata/rpm" packages="2">
<package type="rpm"><name>php-cli</name><arch>x86_64</arch><version epoch="0" ver="8.4.2" rel="1.el10"/><checksum type="sha256">aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa</checksum><size package="1"/><location href="Packages/php-cli.rpm"/><format><rpm:sourcerpm>php-8.4.2-1.el10.src.rpm</rpm:sourcerpm></format></package>
<package type="rpm"><name>php</name><arch>src</arch><version epoch="0" ver="8.4.2" rel="1.el10"/><checksum type="sha256">bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb</checksum><size package="2"/><location href="Packages/php.src.rpm"/></package></metadata>"""
    raw = gzip.compress(primary)
    (root / "repodata").mkdir()
    (root / "repodata/primary.xml.gz").write_bytes(raw)
    repomd = f"""<repomd xmlns="http://linux.duke.edu/metadata/repo"><revision>1</revision><data type="primary"><checksum type="sha256">{hashlib.sha256(raw).hexdigest()}</checksum><size>{len(raw)}</size><location href="repodata/primary.xml.gz"/></data></repomd>""".encode()
    (root / "repodata/repomd.xml").write_bytes(repomd)
    w = {
        **config["watches"][0],
        "url": url,
        "type": "rpm",
        "suites": ["10"],
        "architectures": ["x86_64"],
        "kinds": ["source", "binary"],
    }
    result = rpm.scan(w, Client(config, config["state_dir"], config["watches"][0]))
    assert len(result.packages) == 2
    assert result.packages[0].source == "php"
    assert result.packages[0].source_version == result.packages[1].version == "0:8.4.2-1.el10"


@pytest.mark.parametrize("extension", ["gz", "xz", "bz2", "zst", ""])
def test_compression_formats(extension):
    import bz2
    import lzma

    import zstandard

    from gills.transport import decompress

    raw = b"a repository index\n" * 100
    encoders = {
        "gz": gzip.compress,
        "xz": lzma.compress,
        "bz2": bz2.compress,
        "zst": zstandard.ZstdCompressor().compress,
        "": lambda b: b,
    }
    assert decompress(encoders[extension](raw), "index." + extension, 10000) == raw
    from gills.transport import FetchError

    with pytest.raises(FetchError):
        decompress(encoders[extension](raw), "index." + extension, 100)
