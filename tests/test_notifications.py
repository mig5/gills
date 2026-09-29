import hashlib
import hmac
import http.server
import json
import threading
from unittest.mock import MagicMock

import pytest

from gills import notify
from gills.engine import check
from gills.state import State


def test_independent_retries(repo, config, monkeypatch):
    w = config["watches"][0]
    w["notify_initial"] = True
    config["destinations"] = {
        "good": {"type": "stdout", "label": "good"},
        "bad": {"type": "stdout", "label": "bad"},
    }
    w["destinations"] = list(config["destinations"])
    state = State(config["state_dir"])
    repo["publish"]()
    assert check(state, config, w, now=100)["ok"]
    calls = []

    def send(destination, batch, config):
        calls.append((destination["label"], batch["id"], batch["events"][0]["id"]))
        if destination["label"] == "bad":
            raise RuntimeError("private secret must not leak")

    monkeypatch.setattr(notify, "send", send)
    assert notify.dispatch(state, config, 100) == {"delivered_batches": 1, "failed_batches": 1}
    assert len(calls) == 2
    assert notify.dispatch(state, config, 110)["failed_batches"] == 0
    error = state.db.execute("SELECT last_error FROM batches WHERE state='pending'").fetchone()[0]
    assert "private secret" not in error
    monkeypatch.setattr(
        notify, "send", lambda d, b, c: calls.append((d["label"], b["id"], b["events"][0]["id"]))
    )
    assert notify.dispatch(state, config, 131)["delivered_batches"] == 1
    assert calls[-1] == next(call for call in calls[:2] if call[0] == "bad")
    assert len([call for call in calls if call[0] == "good"]) == 1


def test_digest_and_delay(repo, config, monkeypatch):
    w = config["watches"][0]
    w["notify_initial"] = True
    w["delivery_delay_seconds"] = 20
    config["destinations"] = {"digest": {"type": "stdout", "digest_seconds": 60}}
    w["destinations"] = list(config["destinations"])
    state = State(config["state_dir"])
    repo["publish"]()
    check(state, config, w, now=100)
    repo["publish"]("8.4.2-1")
    check(state, config, w, now=130)
    sent = []
    monkeypatch.setattr(notify, "send", lambda d, b, c: sent.append(b))
    notify.dispatch(state, config, 179)
    assert not sent
    notify.dispatch(state, config, 180)
    assert len(sent) == 1 and len(sent[0]["events"]) == 2


def test_webhook_real_http_hmac(config, monkeypatch):
    received = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((dict(self.headers), raw))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    monkeypatch.setenv("HOOK_KEY", "test-secret")
    try:
        batch = {"schema_version": 1, "id": "delivery-123", "events": []}
        notify.send(
            {
                "type": "webhook",
                "url": f"http://127.0.0.1:{server.server_port}/hook",
                "allow_http": True,
                "allow_private_networks": True,
                "secret_env": "HOOK_KEY",
            },
            batch,
            config,
        )
        headers, raw = received[0]
        assert headers["X-Gills-Delivery"] == batch["id"]
        assert (
            headers["X-Gills-Signature"]
            == "sha256=" + hmac.new(b"test-secret", raw, hashlib.sha256).hexdigest()
        )
        assert json.loads(raw) == batch
    finally:
        server.shutdown()
        worker.join()
        server.server_close()


def test_email_starttls_and_attachment(config, monkeypatch):
    smtp = MagicMock()
    smtp.__enter__.return_value = smtp
    smtp.send_message.return_value = {}
    monkeypatch.setattr(notify.smtplib, "SMTP", lambda *a, **kw: smtp)
    notify.send(
        {
            "type": "email",
            "host": "smtp.example.invalid",
            "from": "a@example.invalid",
            "to": ["b@example.invalid"],
            "tls": "starttls",
        },
        {"id": "test", "events": []},
        config,
    )
    smtp.starttls.assert_called_once()
    message = smtp.send_message.call_args.args[0]
    assert list(message.iter_attachments())[0].get_filename() == "gills-events.json"


@pytest.mark.parametrize("kind", ["signal", "slack"])
def test_chat_payloads(kind, config, monkeypatch):
    received = []
    response = MagicMock()
    response.__enter__.return_value = response
    response.status = 200

    def opened(self, url, headers=None, data=None):
        received.append(json.loads(data))
        return response

    monkeypatch.setattr(notify.Client, "open", opened)
    monkeypatch.setenv("SIGNAL_NUMBER", "+61000000000")
    event = {"id": "1", "watch": "php", "type": "test", "message": "<!channel>"}
    notify.send(
        {
            "type": kind,
            "url": "https://example.invalid/send",
            "number_env": "SIGNAL_NUMBER",
            "recipients": ["+61000000001"],
        },
        {"id": "batch", "events": [event]},
        config,
    )
    if kind == "slack":
        assert received[0]["blocks"][0]["text"]["type"] == "plain_text"
    else:
        assert received[0]["number"] == "+61000000000"
        assert received[0]["recipients"] == ["+61000000001"]


def test_watch_routes_only_to_selected_destinations(repo, config, monkeypatch):
    import copy

    first = config["watches"][0]
    first["notify_initial"] = True
    first["destinations"] = ["one", "one"]
    second = copy.deepcopy(first)
    second.update(name="other", destinations=["two"])
    config["watches"].append(second)
    config["destinations"] = {name: {"type": "stdout"} for name in ["one", "two", "unused"]}
    state = State(config["state_dir"])
    repo["publish"]()
    for watch in config["watches"]:
        assert check(state, config, watch, now=100)["ok"]
    rows = state.db.execute(
        "SELECT e.watch,d.destination FROM deliveries d JOIN events e ON e.id=d.event_id"
    ).fetchall()
    assert sorted(tuple(row) for row in rows) == [("other", "two"), ("php", "one")]
    first["destinations"] = []
    repo["publish"]("8.4.2-1")
    assert check(state, config, first, now=200)["ok"]
    assert state.db.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 2
    # Existing queued deliveries keep their original routing after configuration edits.
    sent = []
    monkeypatch.setattr(notify, "send", lambda d, b, c: sent.append(b))
    assert notify.dispatch(state, config, 200)["delivered_batches"] == 2
    assert len(sent) == 2


@pytest.mark.parametrize("selection", [None, [], ["one"]])
def test_health_event_routing(config, selection):
    config["destinations"] = {
        "one": {"type": "stdout", "events": ["repository.error"]},
        "two": {"type": "stdout"},
    }
    watch = config["watches"][0]
    watch.pop("destinations", None)
    if selection is not None:
        watch["destinations"] = selection
    state = State(config["state_dir"])
    # Route an already stored health event using the same path as package events.
    event = {"id": "health", "watch": watch["name"], "type": "repository.error"}
    state.db.execute(
        "INSERT INTO events(id,watch,type,payload,state,observed,ready_at,scope) VALUES(?,?,?,?,?,?,?,?)",
        ("health", watch["name"], "repository.error", json.dumps(event), "ready", 1, 1, "test"),
    )
    state.route(event, config)
    assert state.db.execute("SELECT count(*) FROM deliveries").fetchone()[0] == (
        1 if selection else 0
    )
