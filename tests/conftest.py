import functools
import hashlib
import http.server
import threading
from pathlib import Path

import pytest


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture
def webroot(tmp_path):
    root = tmp_path / "www"
    root.mkdir()
    handler = functools.partial(QuietHandler, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield root, f"http://127.0.0.1:{server.server_port}/"
    server.shutdown()
    thread.join()
    server.server_close()


def sha(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def repo(webroot):
    root, url = webroot

    def publish(
        version="8.4.1-1",
        binary=True,
        broken=False,
        content=b"source tarball",
        inrelease=False,
        by_hash=False,
    ):
        import datetime
        import email.utils
        import gzip

        files = {
            f"pool/php/php_{version}.orig.tar.gz": content,
            f"pool/php/php_{version}.dsc": b"source descriptor",
        }
        for path, data in files.items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        sources = (
            f"Package: php8.4\nVersion: {version}\nDirectory: pool/php\nChecksums-Sha256:\n"
            + "".join(
                f" {sha(data)} {len(data)} {Path(path).name}\n" for path, data in files.items()
            )
            + "\n"
        )
        binary_data = b"deb contents"
        binary_path = f"pool/php/php8.4-cli_{version}_amd64.deb"
        (root / binary_path).write_bytes(binary_data)
        packages = (
            f"Package: php8.4-cli\nVersion: {version}\nArchitecture: amd64\nSource: php8.4 ({version})\nFilename: {binary_path}\nSize: {len(binary_data)}\nSHA256: {sha(binary_data)}\n\n"
            if binary
            else ""
        )
        indexes = {
            "main/source/Sources.gz": gzip.compress(sources.encode(), mtime=0),
            "main/binary-amd64/Packages.gz": gzip.compress(packages.encode(), mtime=0),
        }
        base = root / "dists/trixie"
        for path, data in indexes.items():
            target = base / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            if by_hash:
                hashed = target.parent / "by-hash/SHA256" / sha(data)
                hashed.parent.mkdir(parents=True, exist_ok=True)
                hashed.write_bytes(data)
                target.unlink()
        date = email.utils.format_datetime(datetime.datetime.now(datetime.timezone.utc))
        release = (
            f"Origin: Test\nSuite: trixie\nCodename: trixie\nDate: {date}\nArchitectures: amd64\nComponents: main\n"
            + ("Acquire-By-Hash: yes\n" if by_hash else "")
            + "SHA256:\n"
            + "".join(f" {sha(data)} {len(data)} {path}\n" for path, data in indexes.items())
        )
        if broken:
            (base / "main/source/Sources.gz").write_bytes(b"broken")
        if inrelease:
            (base / "Release").unlink(missing_ok=True)
            (base / "InRelease").write_bytes(
                b"-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA256\n\n"
                + release.encode()
                + b"-----BEGIN PGP SIGNATURE-----\nnot-verified\n-----END PGP SIGNATURE-----\n"
            )
        else:
            (base / "Release").write_bytes(release.encode())
            (base / "InRelease").unlink(missing_ok=True)
        return release.encode()

    return {"publish": publish, "url": url, "root": root}


@pytest.fixture
def config(tmp_path, repo):
    import yaml

    from gills.config import load

    path = tmp_path / "config.yml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "state_dir": str(tmp_path / "state"),
                "watches": [
                    {
                        "name": "php",
                        "type": "apt",
                        "url": repo["url"],
                        "allow_http": True,
                        "allow_private_networks": True,
                        "suites": ["trixie"],
                        "filters": {"sources": ["php8.4"]},
                    }
                ],
            }
        )
    )
    return load(path)
