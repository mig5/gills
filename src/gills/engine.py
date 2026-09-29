from __future__ import annotations

import json
import time

from . import apt, rpm
from .model import IntegrityError, Package, GillsError, canonical, digest
from .rules import changes, group, match, missing_requirements
from .transport import Client


def event_for(watch, generation, content, now):
    identity = digest([watch["name"], watch["scope"], generation, content])
    return {
        "schema_version": 1,
        "id": identity,
        "watch": watch["name"],
        "repository": watch["url"],
        "backend": watch["type"],
        "observed_at": now,
        **content,
    }


def check(state, config, watch, now=None, dry_run=False):
    now = time.time() if now is None else now
    previous = state.watch(watch["name"])
    same_scope = previous and previous["scope"] == watch["scope"]
    generation = (previous["generation"] if previous else 0) + 1
    new_events = []
    client = Client(config, config["state_dir"], watch)
    try:
        snapshot = (apt.scan if watch["type"] == "apt" else rpm.scan)(watch, client)
        if same_scope and previous["releases"] and watch["type"] == "apt":
            import email.utils

            old_releases = json.loads(previous["releases"])
            for suite, release in snapshot.releases.items():
                older = old_releases.get(suite, {}).get("Date")
                newer = release.get("Date")
                if (
                    older
                    and newer
                    and email.utils.parsedate_to_datetime(newer)
                    < email.utils.parsedate_to_datetime(older)
                ):
                    raise IntegrityError(
                        "Release Date moved backwards; refusing potential metadata replay"
                    )
        selected = [p for p in snapshot.packages if match(p, watch)[0]]
        old_packages = (
            [Package.from_dict(p) for p in json.loads(previous["snapshot"])]
            if same_scope and previous["snapshot"] is not None
            else []
        )
        baseline = not same_scope or previous["snapshot"] is None
        changeset = (
            changes(old_packages, selected, watch)
            if not baseline or watch["notify_initial"]
            else []
        )
        for content in group(changeset, watch):
            new_events.append(event_for(watch, generation, content, now))
        if previous and previous["error_notified"]:
            new_events.append(
                event_for(
                    watch,
                    generation,
                    {
                        "type": "repository.recovered",
                        "message": "Repository checks are succeeding again",
                    },
                    now,
                )
            )
        existing = state.db.execute(
            "SELECT * FROM events WHERE watch=? AND scope=? AND state='waiting'",
            (watch["name"], watch["scope"]),
        ).fetchall()
        pending = [(json.loads(row["payload"]), row) for row in existing]
        if watch["group_by_source"]:
            # Architecture publication can span checks; enrich one pending source event.
            for pending_event, _ in pending:
                for new_event in list(new_events):
                    if (
                        new_event["type"] == pending_event["type"]
                        and new_event.get("source") == pending_event.get("source")
                        and new_event.get("source_version") == pending_event.get("source_version")
                    ):
                        merged = {
                            digest(c): c for c in pending_event["changes"] + new_event["changes"]
                        }
                        pending_event["changes"] = list(merged.values())
                        new_events.remove(new_event)
        waiting_updates = []
        for event, row in pending + [
            (event, None) for event in new_events if event["type"].startswith("package.")
        ]:
            missing = missing_requirements(event, snapshot.packages, watch)
            event["missing"] = missing
            status = "waiting" if missing else "ready"
            timed_out = bool(row and row["timeout_notified"])
            if (
                missing
                and not timed_out
                and now - event["observed_at"] >= watch["readiness"]["timeout_seconds"]
            ):
                new_events.append(
                    event_for(
                        watch,
                        generation,
                        {
                            "type": "readiness.timeout",
                            "source": event["source"],
                            "source_version": event["source_version"],
                            "message": "Waiting for: " + "; ".join(missing),
                            "waiting_event_id": event["id"],
                        },
                        now,
                    )
                )
                timed_out = True
            event.pop("verified_signers", None)  # Remove legacy fields from pending events.
            waiting_updates.append((event, row, status, timed_out))
        package_states = {e["id"]: (status, timeout) for e, _, status, timeout in waiting_updates}
        with state.db:
            if not same_scope:
                state.db.execute(
                    "UPDATE events SET state='superseded' WHERE watch=? AND state='waiting'",
                    (watch["name"],),
                )
            for event in new_events:
                status, timeout = package_states.get(event["id"], ("ready", False))
                ready_at = now + (
                    watch["delivery_delay_seconds"] if event["type"].startswith("package.") else 0
                )
                state.add_event(event, watch, config, status, ready_at)
                if timeout:
                    state.db.execute(
                        "UPDATE events SET timeout_notified=1 WHERE id=?", (event["id"],)
                    )
            for event, row, status, timeout in waiting_updates:
                if row is None:
                    continue
                state.db.execute(
                    "UPDATE events SET payload=?,state=?,timeout_notified=?,ready_at=? WHERE id=?",
                    (
                        canonical(event),
                        status,
                        int(timeout),
                        now + watch["delivery_delay_seconds"],
                        event["id"],
                    ),
                )
                if status == "ready":
                    state.route(event, config)
            state.db.execute(
                "INSERT INTO watches(name,scope,snapshot,releases,generation,last_checked,last_success) VALUES(?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET scope=excluded.scope,snapshot=excluded.snapshot,releases=excluded.releases,generation=excluded.generation,last_checked=excluded.last_checked,last_success=excluded.last_success,failures=0,last_error=NULL,error_notified=0",
                (
                    watch["name"],
                    watch["scope"],
                    canonical([p.to_dict() for p in selected]),
                    canonical(snapshot.releases),
                    generation,
                    now,
                    now,
                ),
            )
        return {
            "watch": watch["name"],
            "ok": True,
            "baseline": baseline,
            "packages": len(selected),
            "events": len(new_events),
            **(
                {"matched_packages": [p.to_dict() for p in selected]}
                if dry_run and baseline
                else {}
            ),
            "became_ready": sum(
                1 for _, row, status, _ in waiting_updates if row and status == "ready"
            ),
        }
    except Exception as exc:
        message = (
            str(exc)
            if isinstance(exc, GillsError)
            else f"Repository check failed ({type(exc).__name__})"
        )
        failures = (previous["failures"] if previous else 0) + 1
        notified = previous["error_notified"] if previous else 0
        should_alert = isinstance(exc, IntegrityError) or failures >= watch["failure_threshold"]
        with state.db:
            if should_alert and (not notified or previous["last_error"] != message):
                state.add_event(
                    event_for(
                        watch,
                        generation,
                        {
                            "type": "repository.error",
                            "message": message,
                            "integrity_failure": isinstance(exc, IntegrityError),
                        },
                        now,
                    ),
                    watch,
                    config,
                )
                notified = 1
            state.db.execute(
                "INSERT INTO watches(name,scope,generation,failures,last_checked,last_error,error_notified) VALUES(?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET generation=excluded.generation,failures=excluded.failures,last_checked=excluded.last_checked,last_error=excluded.last_error,error_notified=excluded.error_notified",
                (watch["name"], watch["scope"], generation, failures, now, message, notified),
            )
        return {"watch": watch["name"], "ok": False, "error": message}
