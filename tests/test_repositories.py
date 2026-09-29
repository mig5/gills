import json

import pytest

from gills import apt
from gills.engine import check
from gills.model import IntegrityError
from gills.state import State
from gills.transport import Client, repository_url


def test_scan_without_artifact_downloads(repo, config):
    repo["publish"]()
    state = State(config["state_dir"])
    w = config["watches"][0]
    apt.scan(w, Client(config, config["state_dir"], config["watches"][0]))
    baseline = check(state, config, w, now=100)
    assert baseline["ok"], baseline
    assert baseline["baseline"]
    assert not state.history()
    repo["publish"]("8.4.2-1")
    result = check(state, config, w, now=200)
    assert result["ok"], result
    event = state.history()[0]
    assert event["type"] == "package.updated"
    assert event["changes"][0]["level"] == "upstream"
    assert "captured_sources" not in event
    assert not (state.root / "sources").exists()
    assert not (state.root / "cache").exists()
    assert check(state, config, w, now=300)["events"] == 0


def test_checksum_mismatch_no_false_removals(repo, config):
    state = State(config["state_dir"])
    w = config["watches"][0]
    w["events"].append("package.removed")
    repo["publish"]()
    assert check(state, config, w)["ok"]
    before = state.watch("php")["snapshot"]
    repo["publish"]("8.4.2-1", broken=True)
    assert not check(state, config, w)["ok"]
    assert state.watch("php")["snapshot"] == before
    assert all(e["type"] != "package.removed" for e in state.history())


@pytest.mark.parametrize("inrelease,by_hash", [(True, False), (False, True)])
def test_release_and_by_hash(repo, config, inrelease, by_hash):
    repo["publish"](inrelease=inrelease, by_hash=by_hash)
    result = apt.scan(
        config["watches"][0], Client(config, config["state_dir"], config["watches"][0])
    )
    assert result.packages[0].name == "php8.4"


def test_readiness_timeout_and_recovery(repo, config):
    w = config["watches"][0]
    w["notify_initial"] = True
    w["readiness"] = {
        "require_source": True,
        "binaries": ["php*-cli"],
        "architectures": ["amd64"],
        "suites": ["trixie"],
        "timeout_seconds": 60,
    }
    config["destinations"] = {"build": {"type": "stdout"}}
    w["destinations"] = ["build"]
    state = State(config["state_dir"])
    repo["publish"](binary=False)
    result = check(state, config, w, now=100)
    assert result["ok"], result
    assert state.history()[0]["state"] == "waiting"
    assert state.db.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 0
    check(state, config, w, now=170)
    assert state.history()[0]["type"] == "readiness.timeout"
    check(state, config, w, now=180)
    assert sum(e["type"] == "readiness.timeout" for e in state.history()) == 1
    repo["publish"](binary=True)
    result = check(state, config, w, now=200)
    assert result["became_ready"] == 1
    assert all(e["state"] == "ready" for e in state.history())
    assert state.db.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 2


def test_source_artifacts_are_not_requested(repo, config):
    w = config["watches"][0]
    state = State(config["state_dir"])
    repo["publish"]()
    check(state, config, w)
    repo["publish"]("8.4.2-1")
    (repo["root"] / "pool/php/php_8.4.2-1.orig.tar.gz").unlink()
    assert check(state, config, w)["ok"]
    assert any(e["type"] == "package.updated" for e in state.history())


@pytest.mark.parametrize(
    "path",
    [
        "../secret",
        "%2e%2e/secret",
        "/etc/passwd",
        "https://evil.invalid/a",
        "//evil/a",
        "x?token=a",
        "foo\\bar",
    ],
)
def test_metadata_path_safety(path):
    with pytest.raises(IntegrityError):
        repository_url("https://example.org/repo/", path)


def test_release_expiry():
    with pytest.raises(IntegrityError, match="expired"):
        apt.validate_date({"Valid-Until": "Mon, 01 Jan 2001 00:00:00 UTC"}, {})


def test_scope_change_quietly_rebaselines(repo, config):
    state = State(config["state_dir"])
    w = config["watches"][0]
    repo["publish"]()
    check(state, config, w)
    repo["publish"]("8.4.2-1")
    w["scope"] = "new-scope"
    result = check(state, config, w)
    assert result["baseline"] and result["events"] == 0
    assert json.loads(state.watch("php")["snapshot"])[0]["version"] == "8.4.2-1"


def test_late_architecture_merges_waiting_source_event(repo, config):
    w = config["watches"][0]
    w["kinds"] = ["source", "binary"]
    w["notify_initial"] = True
    w["readiness"] = {
        "require_source": True,
        "binaries": ["php*-cli"],
        "architectures": ["amd64"],
        "suites": ["trixie"],
        "timeout_seconds": 3600,
    }
    state = State(config["state_dir"])
    repo["publish"](binary=False)
    assert check(state, config, w, now=100)["ok"]
    event_id = state.history()[0]["id"]
    repo["publish"](binary=True)
    assert check(state, config, w, now=200)["ok"]
    events = state.history()
    assert len(events) == 1 and events[0]["state"] == "ready"
    assert events[0]["id"] == event_id
    assert len(events[0]["changes"]) == 2


def test_release_rollback_rejected(repo, config):
    state = State(config["state_dir"])
    w = config["watches"][0]
    raw = repo["publish"]()
    assert check(state, config, w)["ok"]
    import re

    older = re.sub(rb"Date: [^\n]+", b"Date: Mon, 01 Jan 2001 00:00:00 UTC", raw)
    (repo["root"] / "dists/trixie/Release").write_bytes(older)
    result = check(state, config, w)
    assert not result["ok"] and "backwards" in result["error"]
