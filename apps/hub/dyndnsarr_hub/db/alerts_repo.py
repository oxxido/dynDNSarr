"""Señales y entregas sin payload original, URLs o credenciales."""

import uuid
from datetime import timedelta

from dyndnsarr_hub.clock import iso
from dyndnsarr_hub.db import auth_repo


def observe(conn, config, event, now):
    kind = event.type
    if kind == "integration_health":
        if event.payload.get("state") not in ("ok", "unavailable"):
            return
        kind = (
            "integration_recovered" if event.payload.get("state") == "ok" else "integration_failed"
        )
    subject = event.agent_id + ":" + (event.record_name or "")
    for rule in config.rules:
        if kind not in rule.events and kind not in rule.recovery_events:
            continue
        row = conn.execute(
            "SELECT phase,since_at,last_notified_at,last_sequence FROM alert_signals "
            "WHERE rule_id=? AND "
            "subject=?",
            (rule.id, subject),
        ).fetchone()
        if row and event.sequence <= row[3]:
            continue
        if kind in rule.recovery_events:
            if not row or row[0] != "alert":
                conn.execute(
                    "INSERT INTO alert_signals VALUES (?,?,'idle',?,NULL,?) "
                    "ON CONFLICT(rule_id,subject) "
                    "DO UPDATE SET last_sequence=excluded.last_sequence",
                    (rule.id, subject, iso(now), event.sequence),
                )
                continue
            conn.execute(
                "UPDATE alert_deliveries SET status='cancelled' WHERE rule_id=? AND subject=? "
                "AND status='pending' AND phase='alert'",
                (rule.id, subject),
            )
            # Recuperación solo si se había intentado notificar la incidencia.
            if row[2] is not None:
                enqueue(conn, rule, subject, now, "recovery")
            conn.execute(
                "UPDATE alert_signals SET phase='recovery',since_at=?,last_sequence=? "
                "WHERE rule_id=? AND "
                "subject=?",
                (iso(now), event.sequence, rule.id, subject),
            )
        elif kind in rule.events:
            since = row[1] if row and row[0] == "alert" else iso(now)
            conn.execute(
                "INSERT INTO alert_signals VALUES (?,?, 'alert', ?,NULL,?) ON "
                "CONFLICT(rule_id,subject) DO UPDATE SET phase='alert',since_at=excluded.since_at,"
                "last_sequence=excluded.last_sequence",
                (rule.id, subject, since, event.sequence),
            )


def enqueue(conn, rule, subject, now, phase="alert"):
    if (
        conn.execute("SELECT count(*) FROM alert_deliveries WHERE status='pending'").fetchone()[0]
        >= 10000
    ):
        auth_repo.audit(conn, now, "system:alerts", "alert_queue_full", result="rejected")
        return
    for channel in dict.fromkeys(rule.channels):
        pending = conn.execute(
            "SELECT 1 FROM alert_deliveries WHERE rule_id=? AND subject=? AND phase=? AND "
            "channel=? AND status='pending'",
            (rule.id, subject, phase, channel),
        ).fetchone()
        if pending:
            continue
        delivery_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO alert_deliveries "
            "(id,rule_id,subject,channel,phase,severity,created_at,next_attempt_at) VALUES "
            "(?,?,?,?,?,?,?,?)",
            (delivery_id, rule.id, subject, channel, phase, rule.severity, iso(now), iso(now)),
        )
        auth_repo.audit(conn, now, "system:alerts", "alert_queued", operation_id=delivery_id)


def evaluate(conn, config, now):
    for rule in config.rules:
        for (subject,) in conn.execute(
            "SELECT subject FROM alert_signals WHERE rule_id=? AND "
            "phase='alert' AND since_at<=? AND (last_notified_at IS NULL OR last_notified_at<=?) "
            "ORDER BY since_at LIMIT 500",
            (
                rule.id,
                iso(now - timedelta(seconds=rule.threshold_seconds)),
                iso(now - timedelta(seconds=rule.suppress_seconds)),
            ),
        ).fetchall():
            enqueue(conn, rule, subject, now)
            conn.execute(
                "UPDATE alert_signals SET last_notified_at=? WHERE rule_id=? AND subject=?",
                (iso(now), rule.id, subject),
            )
            if not rule.recovery_events:
                conn.execute(
                    "UPDATE alert_signals SET phase='idle' WHERE rule_id=? AND subject=?",
                    (rule.id, subject),
                )


def delivery_result(conn, delivery_id, attempts, code, now):
    status = "sent" if code is None else ("failed" if attempts >= 8 else "pending")
    conn.execute(
        "UPDATE alert_deliveries SET attempts=?,status=?,error_code=?,next_attempt_at=? WHERE id=?",
        (
            attempts,
            status,
            code,
            iso(now + timedelta(seconds=min(3600, 30 * 2 ** min(attempts - 1, 7)))),
            delivery_id,
        ),
    )
    auth_repo.audit(
        conn,
        now,
        "system:alerts",
        "alert_delivery",
        operation_id=delivery_id,
        result=status,
        code=code,
    )
