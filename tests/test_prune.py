import json

from gills.engine import check
from gills.state import State


def test_prune_preserves_baseline_pending_work_and_recent_delivery(repo, config):
    watch = config["watches"][0]
    repo["publish"]()
    state = State(config["state_dir"])
    assert check(state, config, watch, now=10)["ok"]
    baseline = state.watch(watch["name"])
    cases = [
        ("old", "ready", "sent", "sent", 20),
        ("waiting", "waiting", None, None, None),
        ("retry", "ready", "pending", "pending", None),
        ("unqueued", "ready", None, None, None),
        ("recent-delivery", "ready", "sent", "sent", 200),
        ("recent-event", "ready", None, None, None),
        ("cancelled", "superseded", "cancelled", "cancelled", None),
    ]
    with state.db:
        for name, status, delivery, batch, delivered in cases:
            observed = 200 if name == "recent-event" else 10
            event = {
                "id": name,
                "watch": watch["name"],
                "type": "package.updated",
                "observed_at": observed,
                "changes": [],
            }
            state.add_event(event, watch, config, state=status)
            if delivery:
                state.db.execute(
                    "INSERT INTO batches(id,destination,payload,state,next_attempt,delivered_at) VALUES(?,?,?,?,?,?)",
                    (
                        name,
                        "hook",
                        json.dumps({"events": [event]}),
                        batch,
                        10,
                        delivered,
                    ),
                )
                state.db.execute(
                    "INSERT INTO deliveries(event_id,destination,batch_id,state) VALUES(?,?,?,?)",
                    (name, "hook", name, delivery),
                )
    # Prune must not touch files left by older versions.
    (state.root / "cache").mkdir()
    (state.root / "cache/old").write_text("legacy")
    with state.lock():
        result = state.prune(100)
    assert result == {
        "removed_events": 3,
        "removed_deliveries": 2,
        "removed_batches": 2,
    }
    assert {r[0] for r in state.db.execute("SELECT id FROM events")} == {
        "waiting",
        "retry",
        "recent-delivery",
        "recent-event",
    }
    assert state.watch(watch["name"]) == baseline
    assert (state.root / "cache/old").read_text() == "legacy"
    # Cleanup cannot cause a previously detected package to be announced again.
    assert check(state, config, watch, now=300)["events"] == 0
    assert state.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not state.db.execute("PRAGMA foreign_key_check").fetchall()
