from __future__ import annotations

import contextlib
import fcntl
import json
import sqlite3
from pathlib import Path

from .model import GillsError, canonical

SCHEMA = """
CREATE TABLE IF NOT EXISTS watches (
 name TEXT PRIMARY KEY, scope TEXT NOT NULL, snapshot TEXT,
 releases TEXT, generation INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
 last_checked REAL, last_success REAL, last_error TEXT, error_notified INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
 id TEXT PRIMARY KEY, watch TEXT NOT NULL, type TEXT NOT NULL, payload TEXT NOT NULL,
 observed REAL NOT NULL, ready_at REAL NOT NULL, state TEXT NOT NULL,
 timeout_notified INTEGER NOT NULL DEFAULT 0, scope TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_watch ON events(watch, state);
CREATE TABLE IF NOT EXISTS deliveries (
 event_id TEXT NOT NULL REFERENCES events(id), destination TEXT NOT NULL,
 batch_id TEXT, state TEXT NOT NULL DEFAULT 'pending',
 PRIMARY KEY(event_id,destination)
);
CREATE TABLE IF NOT EXISTS batches (
 id TEXT PRIMARY KEY, destination TEXT NOT NULL, payload TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt REAL NOT NULL, last_error TEXT, delivered_at REAL
);
PRAGMA user_version=2;
"""


class State:
    def __init__(self, directory, *, memory=False, copy_from=None):
        self.root = Path(directory)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if memory:
            self._open(memory, copy_from)
        else:
            with lock_directory(self.root):
                self._open(memory, copy_from)

    def _open(self, memory, copy_from):
        self.db = sqlite3.connect(
            ":memory:" if memory else self.root / "gills.sqlite3", timeout=30
        )
        if copy_from is not None and Path(copy_from).is_file():
            # Read the real baseline without opening it for schema setup or writes.
            source = sqlite3.connect(
                Path(copy_from).resolve().as_uri() + "?mode=ro", uri=True
            )
            try:
                source.backup(self.db)
            finally:
                source.close()
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute(
            "PRAGMA journal_mode=MEMORY" if memory else "PRAGMA journal_mode=WAL"
        )
        self.db.execute("PRAGMA synchronous=FULL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2):
            raise GillsError("State database is from an unsupported version")
        if version == 1:
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                columns = {
                    row[1] for row in self.db.execute("PRAGMA table_info(watches)")
                }
                if "signers" in columns:
                    # Older distro SQLite libraries lack ALTER TABLE DROP COLUMN.
                    # Rebuild inside this transaction, preserving all baseline/health fields.
                    # No other tables reference watches with a foreign key.
                    self.db.execute(
                        """CREATE TABLE watches_v2 (
                        name TEXT PRIMARY KEY, scope TEXT NOT NULL, snapshot TEXT,
                        releases TEXT, generation INTEGER NOT NULL DEFAULT 0,
                        failures INTEGER NOT NULL DEFAULT 0, last_checked REAL,
                        last_success REAL, last_error TEXT,
                        error_notified INTEGER NOT NULL DEFAULT 0)"""
                    )
                    self.db.execute(
                        """INSERT INTO watches_v2
                        (name,scope,snapshot,releases,generation,failures,
                         last_checked,last_success,last_error,error_notified)
                        SELECT name,scope,snapshot,releases,generation,failures,
                               last_checked,last_success,last_error,error_notified
                        FROM watches"""
                    )
                    self.db.execute("DROP TABLE watches")
                    self.db.execute("ALTER TABLE watches_v2 RENAME TO watches")
                # Preserve history but retire undelivered signer alerts from the old version.
                self.db.execute(
                    "UPDATE deliveries SET state='cancelled' WHERE event_id IN (SELECT id FROM events WHERE type='signer.changed') AND batch_id IS NULL"
                )
                for row in self.db.execute(
                    "SELECT id,payload FROM batches WHERE state='pending'"
                ).fetchall():
                    batch = json.loads(row["payload"])
                    retained = [
                        e for e in batch["events"] if e["type"] != "signer.changed"
                    ]
                    if len(retained) != len(batch["events"]):
                        # Requeue other events instead of changing a previously attempted batch body.
                        self.db.execute(
                            "UPDATE batches SET state='cancelled' WHERE id=?",
                            (row["id"],),
                        )
                        self.db.execute(
                            "UPDATE deliveries SET state='cancelled' WHERE batch_id=?",
                            (row["id"],),
                        )
                        for event in retained:
                            self.db.execute(
                                "UPDATE deliveries SET state='pending',batch_id=NULL WHERE event_id=? AND batch_id=?",
                                (event["id"], row["id"]),
                            )
                self.db.execute("PRAGMA user_version=2")
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    def lock(self):
        return lock_directory(self.root)

    def watch(self, name):
        row = self.db.execute("SELECT * FROM watches WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None

    def add_event(self, event, watch, config, state="ready", ready_at=None):
        event_id = event["id"]
        self.db.execute(
            "INSERT INTO events(id,watch,type,payload,observed,ready_at,state,scope) VALUES(?,?,?,?,?,?,?,?)",
            (
                event_id,
                watch["name"],
                event["type"],
                canonical(event),
                event["observed_at"],
                ready_at if ready_at is not None else event["observed_at"],
                state,
                watch["scope"],
            ),
        )
        if state == "ready":
            self.route(event, config)

    def route(self, event, config):
        if not config.get("notify", True):
            return
        watch = next(
            (w for w in config["watches"] if w["name"] == event["watch"]), None
        )
        if watch is None:
            return
        for name in set(watch.get("destinations", [])):
            destination = config["destinations"][name]
            if not destination.get("enabled", True):
                continue
            if destination.get("events") and event["type"] not in destination["events"]:
                continue
            self.db.execute(
                "INSERT OR IGNORE INTO deliveries(event_id,destination) VALUES(?,?)",
                (event["id"], name),
            )

    def prune(self, older_than):
        """Retain baselines, waiting events, pending work and recently sent history."""
        with self.db:
            self.db.execute("CREATE TEMP TABLE prune_events(id TEXT PRIMARY KEY)")
            self.db.execute(
                """INSERT INTO prune_events SELECT e.id FROM events e
                WHERE e.observed < ? AND e.ready_at < ? AND e.state != 'waiting'
                AND NOT EXISTS (SELECT 1 FROM deliveries d LEFT JOIN batches b
                    ON b.id=d.batch_id WHERE d.event_id=e.id AND
                    (d.state='pending' OR b.state='pending' OR b.delivered_at >= ?))""",
                (older_than, older_than, older_than),
            )
            deliveries = self.db.execute(
                "DELETE FROM deliveries WHERE event_id IN (SELECT id FROM prune_events)"
            ).rowcount
            events = self.db.execute(
                "DELETE FROM events WHERE id IN (SELECT id FROM prune_events)"
            ).rowcount
            batches = self.db.execute(
                """DELETE FROM batches WHERE state != 'pending'
                AND COALESCE(delivered_at,next_attempt) < ?
                AND NOT EXISTS (SELECT 1 FROM deliveries d WHERE d.batch_id=batches.id)""",
                (older_than,),
            ).rowcount
            self.db.execute("DROP TABLE prune_events")
        self.db.execute("VACUUM")
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return {
            "removed_events": events,
            "removed_deliveries": deliveries,
            "removed_batches": batches,
        }

    def history(self, limit=50, watch=None):
        query = "SELECT payload,state FROM events"
        args = []
        if watch:
            query += " WHERE watch=?"
            args.append(watch)
        query += " ORDER BY observed DESC,rowid DESC LIMIT ?"
        args.append(limit)
        return [
            {**json.loads(row["payload"]), "state": row["state"]}
            for row in self.db.execute(query, args)
        ]

    def status(self):
        watches = [
            dict(r)
            for r in self.db.execute(
                "SELECT name,failures,last_checked,last_success,last_error FROM watches ORDER BY name"
            )
        ]
        deliveries = [
            dict(r)
            for r in self.db.execute(
                "SELECT destination,state,count(*) AS count FROM deliveries GROUP BY destination,state"
            )
        ]
        waiting = self.db.execute(
            "SELECT count(*) FROM events WHERE state='waiting'"
        ).fetchone()[0]
        return {"watches": watches, "deliveries": deliveries, "waiting": waiting}


@contextlib.contextmanager
def lock_directory(root):
    with (Path(root) / "gills.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise GillsError(
                "Another gills process is using this state directory"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
