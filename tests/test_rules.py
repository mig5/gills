import pytest

from gills.model import Artifact, Package
from gills.rules import changes, group, match
from gills.versions import change_level, compare


def pkg(version, name="php8.4", arch="source", checksum="a"):
    return Package(
        name,
        version,
        arch,
        "trixie",
        "main",
        "source" if arch == "source" else "binary",
        "php8.4",
        version,
        (Artifact("https://example.org/source", checksum * 64, 1, "source"),),
    )


@pytest.mark.parametrize(
    "a,b",
    [
        ("1.0~rc1-1", "1.0-1"),
        ("1.9-1", "1.10-1"),
        ("1:1-1", "2:0-1"),
        ("1.0-2", "1.0-10"),
    ],
)
def test_debian_order(a, b):
    assert compare(a, b, "apt") < 0


@pytest.mark.parametrize(
    "a,b",
    [
        ("0:1.0~rc1-1", "0:1.0-1"),
        ("0:1.0-1", "0:1.0^git1-1"),
        ("0:1.0^git1-1", "0:1.0.1-1"),
        ("0:1.9-1", "0:1.10-1"),
        ("1:1-1", "2:0-1"),
        ("0:1.0a-1", "0:1.0.1-1"),
    ],
)
def test_rpm_order(a, b):
    assert compare(a, b, "rpm") < 0
    assert compare(b, a, "rpm") > 0
    assert compare(a, a, "rpm") == 0


def test_version_policy(config):
    w = config["watches"][0]
    assert change_level("8.4.1-1", "8.4.1-2", "apt") == "packaging"
    assert change_level("1:8.4.1-1", "2:8.4.1-1", "apt") == "upstream"
    w["version_policy"] = "upstream"
    assert not changes([pkg("8.4.1-1")], [pkg("8.4.1-2")], w)
    assert changes([pkg("8.4.1-1")], [pkg("8.4.2-1")], w)


def test_filter_explanations(config):
    w = config["watches"][0]
    w["filters"]["version_exclude"] = "(?i)(alpha|beta|rc)"
    assert match(pkg("8.4~RC1-1"), w) == (False, "matches version_exclude")
    assert match(pkg("8.4.1-1"), w)[0]


def test_repacking(config):
    w = config["watches"][0]
    result = changes([pkg("1", checksum="a")], [pkg("1", checksum="b")], w)
    assert result[0]["type"] == "package.repacked"


def test_grouping(config):
    w = config["watches"][0]
    w["kinds"] = ["source", "binary"]
    result = changes([], [pkg("1"), pkg("1", "php8.4-cli", "amd64")], w)
    assert len(group(result, w)) == 1


def test_downgrade_and_removal(config):
    w = config["watches"][0]
    w["events"].append("package.removed")
    assert changes([pkg("2")], [pkg("1")], w)[0]["type"] == "package.downgraded"
    assert changes([pkg("2")], [], w)[0]["type"] == "package.removed"
    assert all(
        e["type"] != "package.removed" for e in changes([pkg("1")], [pkg("2")], w)
    )


def test_older_version_added_to_multiversion_repo(config):
    assert (
        changes([pkg("2")], [pkg("1"), pkg("2")], config["watches"][0])[0]["type"]
        == "package.added"
    )


def test_downgrade_to_already_present_version(config):
    events = changes([pkg("1"), pkg("2")], [pkg("1")], config["watches"][0])
    assert len(events) == 1
    assert events[0]["type"] == "package.downgraded"
    assert events[0]["old"]["version"] == "2"
    assert events[0]["new"]["version"] == "1"
