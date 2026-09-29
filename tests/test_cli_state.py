import json

import pytest
import yaml

from gills.cli import main
from gills.config import load
from gills.engine import check
from gills.model import GillsError
from gills.state import State


def test_lock_excludes_second_process(config):
    a, b = State(config["state_dir"]), State(config["state_dir"])
    with a.lock(), pytest.raises(GillsError, match="Another"):
        with b.lock():
            pass
    a.close()
    b.close()


def test_dry_run_does_not_advance(repo, config, tmp_path, capsys):
    state = State(config["state_dir"])
    repo["publish"]()
    assert check(state, config, config["watches"][0])["ok"]
    before = state.watch("php")
    repo["publish"]("8.4.2-1")
    path = tmp_path / "dry.yml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "state_dir": config["state_dir"],
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
    assert main(["-c", str(path), "check", "--dry-run"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["events"][0]["type"] == "package.updated"
    assert before == state.watch("php")
    assert state.history() == []


@pytest.mark.parametrize(
    "bad",
    [
        {"typo": 1},
        {"allow_unsigned": "false"},
        {"filters": {"version_exclude": "["}},
        {"suites": ["../private"]},
        {"trusted_fingerprints": ["12345678"]},
    ],
)
def test_strict_config(tmp_path, repo, bad):
    path = tmp_path / "bad.yml"
    watch = {
        "name": "php",
        "type": "apt",
        "url": repo["url"],
        "allow_http": True,
        "allow_private_networks": True,
        "suites": ["trixie"],
        **bad,
    }
    path.write_text(yaml.safe_dump({"version": 1, "watches": [watch]}))
    with pytest.raises(GillsError, match="Configuration"):
        load(path)


def write_config(path, repo, state_dir, **extra):
    data = {
        "version": 1,
        "state_dir": str(state_dir),
        "watches": [
            {
                "name": "php",
                "type": "apt",
                "url": repo["url"],
                "allow_http": True,
                "allow_private_networks": True,
                "suites": ["trixie"],
                "notify_initial": True,
            }
        ],
        **extra,
    }
    data["watches"][0]["destinations"] = list(data.get("destinations", {}))
    path.write_text(yaml.safe_dump(data))
    return path


def test_only_check_and_prune_are_commands():
    import argparse

    from gills.cli import parser

    commands = next(a for a in parser()._actions if isinstance(a, argparse._SubParsersAction))
    assert set(commands.choices) == {"check", "prune"}


def test_dry_run_creates_no_state_or_notifications(repo, tmp_path, monkeypatch, capsys):
    from gills import cli

    repo["publish"]()
    state_dir = tmp_path / "not-created"
    path = write_config(
        tmp_path / "preview.yml",
        repo,
        state_dir,
        destinations={"hook": {"type": "webhook", "url": "https://example.invalid/unused"}},
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("dry-run dispatched notifications")

    monkeypatch.setattr(cli, "dispatch", forbidden)
    assert main(["-c", str(path), "check", "--dry-run"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["events"][0]["type"] == "package.added"
    assert not state_dir.exists()
    assert not list(tmp_path.rglob("*.sqlite3"))


def test_check_implicitly_delivers_and_honours_notify(repo, tmp_path, monkeypatch, capsys):
    from gills import notify

    repo["publish"]()
    path = write_config(
        tmp_path / "check.yml",
        repo,
        tmp_path / "state",
        notify=False,
        destinations={"hook": {"type": "webhook", "url": "https://example.invalid/unused"}},
    )
    sent = []
    monkeypatch.setattr(notify, "send", lambda d, b, c: sent.append(b))
    assert main(["-c", str(path), "check"]) == 0
    assert not sent
    repo["publish"]("8.4.2-1")
    data = yaml.safe_load(path.read_text())
    data["notify"] = True
    path.write_text(yaml.safe_dump(data))
    assert main(["-c", str(path), "check"]) == 0
    assert len(sent) == 1 and sent[0]["events"][0]["type"] == "package.updated"
    capsys.readouterr()


def test_disabled_destination_pauses_existing_retry(repo, config, monkeypatch):
    from gills import notify

    repo["publish"]()
    config["watches"][0]["notify_initial"] = True
    config["destinations"] = {"hook": {"type": "stdout"}}
    config["watches"][0]["destinations"] = ["hook"]
    state = State(config["state_dir"])
    check(state, config, config["watches"][0], now=100)

    def fail(*args):
        raise RuntimeError("failed")

    monkeypatch.setattr(notify, "send", fail)
    assert notify.dispatch(state, config, 100)["failed_batches"] == 1
    config["destinations"]["hook"]["enabled"] = False
    monkeypatch.setattr(notify, "send", lambda *args: pytest.fail("disabled delivery attempted"))
    assert notify.dispatch(state, config, 200)["failed_batches"] == 0


def test_prune_without_database_is_noop(repo, tmp_path, capsys):
    state_dir = tmp_path / "prune-state"
    path = write_config(tmp_path / "prune.yml", repo, state_dir)
    assert main(["-c", str(path), "prune", "--days", "30"]) == 0
    assert json.loads(capsys.readouterr().out)["removed_events"] == 0
    assert not state_dir.exists()


def test_existing_v1_database_migrates_without_losing_baseline(repo, config):
    import sqlite3

    state = State(config["state_dir"])
    repo["publish"]()
    assert check(state, config, config["watches"][0])["ok"]
    before = state.watch("php")["snapshot"]
    with state.db:
        state.db.execute("ALTER TABLE watches ADD COLUMN signers TEXT")
        state.db.execute("PRAGMA user_version=1")
    state.close()
    migrated = State(config["state_dir"])
    assert migrated.watch("php")["snapshot"] == before
    assert "signers" not in migrated.watch("php")
    assert migrated.db.execute("PRAGMA user_version").fetchone()[0] == 2
    assert check(migrated, config, config["watches"][0])["events"] == 0
    migrated.close()
    with sqlite3.connect(config["state_dir"] + "/gills.sqlite3") as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_dry_run_leaves_legacy_database_unmigrated(repo, config, tmp_path, capsys):
    from pathlib import Path

    state = State(config["state_dir"])
    repo["publish"]()
    check(state, config, config["watches"][0])
    with state.db:
        state.db.execute("ALTER TABLE watches ADD COLUMN signers TEXT")
        state.db.execute("PRAGMA user_version=1")
    state.close()
    database = Path(config["state_dir"]) / "gills.sqlite3"
    before = database.read_bytes()
    path = write_config(tmp_path / "legacy.yml", repo, config["state_dir"])
    assert main(["-c", str(path), "check", "--dry-run"]) == 0
    assert database.read_bytes() == before
    capsys.readouterr()


@pytest.mark.parametrize("selection", ["hook", ["missing"], [1]])
def test_invalid_watch_destination_selection(repo, tmp_path, selection):
    from gills.config import load
    from gills.model import GillsError

    path = write_config(
        tmp_path / "config.yml", repo, tmp_path / "state", destinations={"hook": {"type": "stdout"}}
    )
    data = yaml.safe_load(path.read_text())
    data["watches"][0]["destinations"] = selection
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(GillsError):
        load(path)


def test_destination_cannot_select_watches(repo, tmp_path):
    from gills.config import load
    from gills.model import GillsError

    path = write_config(
        tmp_path / "config.yml",
        repo,
        tmp_path / "state",
        destinations={"hook": {"type": "stdout", "watches": ["php"]}},
    )
    with pytest.raises(GillsError, match="unknown keys"):
        load(path)
