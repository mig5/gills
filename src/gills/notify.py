from __future__ import annotations

import hashlib
import hmac
import json
import smtplib
import ssl
import time
import uuid
from email.message import EmailMessage

from .config import secret
from .model import GillsError, canonical
from .transport import Client, headers_from_env


def summary(event):
    heading = f"[{event['watch']}] {event['type']}"
    if event.get("source"):
        heading += f" — {event['source']} {event['source_version']}"
    lines = [heading]
    for change in event.get("changes", []):
        package = change["new"] or change["old"]
        before = change["old"]["version"] if change["old"] else "absent"
        after = change["new"]["version"] if change["new"] else "absent"
        lines.append(
            f"  {package['suite']}/{package['architecture']} {package['name']}: {before} -> {after}"
            + (f" ({change['level']})" if change.get("level") else "")
        )
    if event.get("message"):
        lines.append(event["message"])
    lines.append(f"Event: {event['id']}")
    return "\n".join(lines)


def send(destination, batch, config):
    kind = destination["type"]
    body = "\n\n".join(summary(event) for event in batch["events"])
    if kind == "stdout":
        print(canonical(batch), flush=True)
        return
    if kind == "email":
        message = EmailMessage()
        message["Subject"] = f"Gills: {len(batch['events'])} repository event(s)"
        message["From"] = destination["from"]
        message["To"] = ", ".join(destination["to"])
        message["Message-ID"] = f"<{batch['id']}@gills.local>"
        message.set_content(body)
        message.add_attachment(
            canonical(batch).encode(),
            maintype="application",
            subtype="json",
            filename="gills-events.json",
        )
        tls = destination["tls"]
        factory = smtplib.SMTP_SSL if tls == "ssl" else smtplib.SMTP
        kwargs = {"timeout": config["timeout_seconds"]}
        if tls == "ssl":
            kwargs["context"] = ssl.create_default_context()
        with factory(
            destination["host"],
            destination.get("port", 465 if tls == "ssl" else 587),
            **kwargs,
        ) as smtp:
            if tls == "starttls":
                smtp.starttls(context=ssl.create_default_context())
            if destination.get("username_env"):
                smtp.login(
                    secret(destination["username_env"]),
                    secret(destination["password_env"]),
                )
            refused = smtp.send_message(message)
            if refused:
                raise GillsError("SMTP refused one or more recipients; batch will retry")
        return
    target = secret(destination["url_env"]) if destination.get("url_env") else destination["url"]
    headers = headers_from_env(destination.get("headers_env"))
    if kind == "slack":
        # Plain text blocks prevent untrusted repository strings becoming mentions.
        payload = {
            "text": f"Gills: {len(batch['events'])} repository event(s)",
            "blocks": [{"type": "section", "text": {"type": "plain_text", "text": body[:2900]}}],
        }
    elif kind == "signal":
        payload = {
            "message": body,
            "number": secret(destination["number_env"]),
            "recipients": destination["recipients"],
        }
    else:
        payload = batch
    raw = canonical(payload).encode()
    headers.update({"Content-Type": "application/json", "X-Gills-Delivery": batch["id"]})
    if destination.get("secret_env"):
        signature = hmac.new(
            secret(destination["secret_env"]).encode(), raw, hashlib.sha256
        ).hexdigest()
        headers["X-Gills-Signature"] = "sha256=" + signature
    client = Client(config, config["state_dir"], policy=destination)
    with client.open(target, headers, raw) as response:
        if not 200 <= response.status < 300:
            raise GillsError(f"Notification HTTP {response.status}")
        response.read(4096)


def dispatch(state, config, now=None):
    if not config.get("notify", True):
        return {"delivered_batches": 0, "failed_batches": 0}
    now = time.time() if now is None else now
    db = state.db
    with db:
        for name, destination in config["destinations"].items():
            if not destination.get("enabled", True):
                continue
            rows = db.execute(
                "SELECT e.payload,e.ready_at FROM deliveries d JOIN events e ON e.id=d.event_id WHERE d.destination=? AND d.state='pending' AND d.batch_id IS NULL AND e.state='ready' AND e.ready_at<=? ORDER BY e.ready_at,e.id LIMIT 100",
                (name, now),
            ).fetchall()
            if not rows or rows[0]["ready_at"] + destination.get("digest_seconds", 0) > now:
                continue
            if not destination.get("digest_seconds"):
                groups = [[row] for row in rows]
            else:
                groups = [rows]
            for group in groups:
                batch_id = str(uuid.uuid4())
                events = [json.loads(row["payload"]) for row in group]
                batch = {
                    "schema_version": 1,
                    "id": batch_id,
                    "created_at": now,
                    "events": events,
                }
                db.execute(
                    "INSERT INTO batches(id,destination,payload,next_attempt) VALUES(?,?,?,?)",
                    (batch_id, name, canonical(batch), now),
                )
                db.executemany(
                    "UPDATE deliveries SET batch_id=? WHERE event_id=? AND destination=?",
                    [(batch_id, event["id"], name) for event in events],
                )
    failed, delivered = 0, 0
    for row in db.execute(
        "SELECT * FROM batches WHERE state='pending' AND next_attempt<=? ORDER BY next_attempt,id",
        (now,),
    ).fetchall():
        destination = config["destinations"].get(row["destination"])
        if destination is None:
            failed += 1
            continue
        if not destination.get("enabled", True):
            continue
        try:
            send(destination, json.loads(row["payload"]), config)
        except Exception as exc:
            # Adapter exceptions can contain SMTP usernames or tokens: redact their text.
            message = (
                str(exc)
                if isinstance(exc, GillsError)
                else f"Delivery failed ({type(exc).__name__})"
            )
            delay = min(3600, 30 * 2 ** min(row["attempts"], 7))
            with db:
                db.execute(
                    "UPDATE batches SET attempts=attempts+1,next_attempt=?,last_error=? WHERE id=?",
                    (now + delay, message, row["id"]),
                )
            failed += 1
        else:
            with db:
                db.execute(
                    "UPDATE batches SET state='delivered',attempts=attempts+1,last_error=NULL,delivered_at=? WHERE id=?",
                    (now, row["id"]),
                )
                db.execute(
                    "UPDATE deliveries SET state='delivered' WHERE batch_id=?",
                    (row["id"],),
                )
            delivered += 1
    return {"delivered_batches": delivered, "failed_batches": failed}
