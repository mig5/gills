from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from . import __version__
from .config import load
from .engine import check
from .model import GillsError
from .notify import dispatch
from .state import State


def parser():
    p = argparse.ArgumentParser(description="Gills - swimming upstream for new packages.")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-c", "--config", default="config.yml")
    sub = p.add_subparsers(dest="command", required=True)
    checker = sub.add_parser("check", help="check repositories once and notify as configured")
    checker.add_argument(
        "--dry-run",
        action="store_true",
        help="print a preview without saving state or notifying",
    )
    prune = sub.add_parser("prune", help="remove old completed SQLite history")
    prune.add_argument(
        "--days", type=int, default=30, help="history retention in days (default: 30)"
    )
    return p


def print_json(value):
    print(json.dumps(value, indent=2, ensure_ascii=False))


def cycle(state, config, dry_run=False):
    before = {
        row["id"]: (row["payload"], row["state"])
        for row in state.db.execute("SELECT id,payload,state FROM events WHERE state='waiting'")
    }
    last_rowid = state.db.execute("SELECT COALESCE(MAX(rowid),0) FROM events").fetchone()[0]
    results = [check(state, config, watch, dry_run=dry_run) for watch in config["watches"]]
    events = []
    rows = state.db.execute(
        "SELECT id,payload,state FROM events WHERE rowid>? ORDER BY observed,rowid", (last_rowid,)
    ).fetchall()
    events.extend({**json.loads(row["payload"]), "state": row["state"]} for row in rows)
    for event_id, old in before.items():
        row = state.db.execute(
            "SELECT payload,state FROM events WHERE id=?", (event_id,)
        ).fetchone()
        if row and old != (row["payload"], row["state"]):
            events.append({**json.loads(row["payload"]), "state": row["state"]})
    delivered = (
        {"delivered_batches": 0, "failed_batches": 0} if dry_run else dispatch(state, config)
    )
    result = {"checks": results, "events": events, **delivered}
    return result, 1 if any(not r["ok"] for r in results) or delivered["failed_batches"] else 0


def execute(args, config):
    if args.command == "check" and args.dry_run:
        with tempfile.TemporaryDirectory(prefix="gills-preview-") as directory:
            preview_config = copy.deepcopy(config)
            preview_config["state_dir"] = directory
            preview = State(
                directory, memory=True, copy_from=Path(config["state_dir"]) / "gills.sqlite3"
            )
            try:
                result, code = cycle(preview, preview_config, dry_run=True)
                result.update(dry_run=True)
                print_json(result)
                return code
            finally:
                preview.close()
    if args.command == "prune":
        if args.days < 1:
            raise GillsError("--days must be positive")
        if not (Path(config["state_dir"]) / "gills.sqlite3").exists():
            print_json({"removed_events": 0, "removed_deliveries": 0, "removed_batches": 0})
            return 0
    state = State(config["state_dir"])
    try:
        with state.lock():
            if args.command == "prune":
                print_json(state.prune(time.time() - args.days * 86400))
                return 0
            result, code = cycle(state, config)
            print_json(result)
            return code
    finally:
        state.close()


def main(argv=None):
    os.umask(0o077)
    args = parser().parse_args(argv)
    try:
        return execute(args, load(args.config))
    except (GillsError, ValueError, OSError) as exc:
        print(
            f"gills: {exc if isinstance(exc, GillsError) else type(exc).__name__}",
            file=sys.stderr,
        )
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"gills: operation failed ({type(exc).__name__})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
