import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from itertools import count

import httpx
import pytest

from dyndnsarr_hub.config import Settings
from dyndnsarr_hub.db import MIGRATIONS, agents_repo, alerts_repo, events_repo
from dyndnsarr_hub.services.alert_config import AlertConfig
from dyndnsarr_hub.services.maintenance import purge
from dyndnsarr_hub.services.notifications import deliver, send
from dyndnsarr_shared.db import connect, migrate
from dyndnsarr_shared.events import AgentEvent

NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)
CONFIG = {
    "webhook": {},
    "rules": [
        {
            "id": "dns",
            "events": ["dns_mismatch"],
            "recovery_events": ["dns_verified"],
            "channels": ["webhook"],
            "threshold_seconds": 60,
            "suppress_seconds": 900,
        }
    ],
}
SEQUENCES = count(1)


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "hub.db")
    migrate(conn, MIGRATIONS)
    agents_repo.create_agent(conn, "home", "token-hash", NOW)
    yield conn
    conn.close()


def event(kind="dns_mismatch"):
    return AgentEvent(
        agent_id="home",
        event_id=str(uuid.uuid4()),
        sequence=next(SEQUENCES),
        type=kind,
        record_name="home.example",
        occurred_at="2026-10-20T12:00:00Z",
        payload={"password": "canary-secret", "ip": "83.45.12.134"},
    )


def test_threshold_suppression_recovery_and_no_payload_in_queue(db):
    config = AlertConfig.model_validate(CONFIG)
    alerts_repo.observe(db, config, event(), NOW)
    alerts_repo.evaluate(db, config, NOW + timedelta(seconds=59))
    assert db.execute("SELECT count(*) FROM alert_deliveries").fetchone()[0] == 0
    alerts_repo.evaluate(db, config, NOW + timedelta(seconds=60))
    alerts_repo.observe(db, config, event(), NOW + timedelta(seconds=90))
    alerts_repo.evaluate(db, config, NOW + timedelta(seconds=90))
    assert db.execute("SELECT count(*) FROM alert_deliveries").fetchone()[0] == 1
    text = str(db.execute("SELECT * FROM alert_deliveries").fetchall())
    assert "canary-secret" not in text and "83.45.12.134" not in text
    alerts_repo.observe(db, config, event("dns_verified"), NOW + timedelta(seconds=120))
    assert db.execute("SELECT phase,status FROM alert_deliveries ORDER BY phase").fetchall() == [
        ("alert", "cancelled"),
        ("recovery", "pending"),
    ]


def test_recovery_before_threshold_cancels_without_notification(db):
    config = AlertConfig.model_validate(CONFIG)
    alerts_repo.observe(db, config, event(), NOW)
    alerts_repo.observe(db, config, event("dns_verified"), NOW + timedelta(seconds=30))
    alerts_repo.evaluate(db, config, NOW + timedelta(seconds=120))
    assert db.execute("SELECT count(*) FROM alert_deliveries").fetchone()[0] == 0


def test_delayed_failure_does_not_reopen_a_newer_recovery(db):
    config = AlertConfig.model_validate(CONFIG)
    failure = event()
    recovery = event("dns_verified")
    alerts_repo.observe(db, config, recovery, NOW)
    alerts_repo.observe(db, config, failure, NOW + timedelta(minutes=5))
    alerts_repo.evaluate(db, config, NOW + timedelta(minutes=10))
    assert db.execute("SELECT count(*) FROM alert_deliveries").fetchone()[0] == 0


def test_instant_events_do_not_repeat_forever(db):
    config = AlertConfig.model_validate(
        {"webhook": {}, "rules": [{"id": "ip", "events": ["ip_changed"], "channels": ["webhook"]}]}
    )
    alerts_repo.observe(db, config, event("ip_changed"), NOW)
    alerts_repo.evaluate(db, config, NOW)
    alerts_repo.evaluate(db, config, NOW + timedelta(days=1))
    assert db.execute("SELECT count(*) FROM alert_deliveries").fetchone()[0] == 1


def test_retry_keeps_id_and_backoff_after_reopen(tmp_path):
    path = tmp_path / "hub.db"
    conn = connect(path)
    migrate(conn, MIGRATIONS)
    config = AlertConfig.model_validate(CONFIG)
    alerts_repo.enqueue(conn, config.rules[0], "home:", NOW)
    attempts = []

    async def failing(c, s, d):
        attempts.append(d[0])
        raise OSError("canary-secret-url")

    asyncio.run(deliver(conn, config, Settings(), clock=lambda: NOW, sender=failing))
    assert conn.execute("SELECT attempts,status,error_code FROM alert_deliveries").fetchone() == (
        1,
        "pending",
        "delivery_failed",
    )
    conn.close()
    conn = connect(path)
    asyncio.run(
        deliver(conn, config, Settings(), clock=lambda: NOW + timedelta(seconds=29), sender=failing)
    )
    assert len(attempts) == 1

    async def succeeds(c, s, d):
        attempts.append(d[0])

    asyncio.run(
        deliver(
            conn, config, Settings(), clock=lambda: NOW + timedelta(seconds=30), sender=succeeds
        )
    )
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    assert conn.execute("SELECT status FROM alert_deliveries").fetchone()[0] == "sent"
    assert "canary-secret-url" not in str(conn.execute("SELECT * FROM audit_log").fetchall())
    conn.close()


def test_webhook_slack_plaintext_success_has_stable_id_and_no_secrets():
    config = AlertConfig.model_validate({"webhook": {"networks": ["127.0.0.0/8"]}})
    settings = Settings(webhook_url="http://127.0.0.1/secret-url-canary")

    def handle(req):
        assert req.headers["idempotency-key"] == "delivery-id"
        assert "secret-url-canary" not in req.content.decode()
        assert "DynDNSarr" in req.content.decode()
        return httpx.Response(200, text="ok")

    asyncio.run(
        send(
            config,
            settings,
            ("delivery-id", "webhook", "test", "info", "2026-10-20T12:00:00Z", "test"),
            transport=httpx.MockTransport(handle),
        )
    )


def test_retention_preserves_pending_and_tombstones_prevent_resurrection(db):
    old = NOW - timedelta(days=10)
    evt = event()
    assert events_repo.insert_events(db, [evt], old) == [evt]
    config = AlertConfig.model_validate(CONFIG)
    alerts_repo.enqueue(db, config.rules[0], "home:", old)
    purge(db, NOW, 1, 400)
    assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM alert_deliveries").fetchone()[0] == 1
    assert events_repo.insert_events(db, [evt], NOW) == []
    assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 0


def test_smtp_host_pinned_tls_and_email_id(monkeypatch):
    from dyndnsarr_hub.services import notifications
    from dyndnsarr_hub.services.alert_config import Smtp

    calls = []

    class FakeSmtp:
        def __init__(self, **kwargs):
            assert kwargs["timeout"] == 5

        def connect(self, host, port):
            calls.append((host, port, self._host))

        def login(self, user, password):
            calls.append((user, password))

        def send_message(self, message):
            calls.append(message)

        def close(self):
            pass

    monkeypatch.setattr(notifications.smtplib, "SMTP_SSL", FakeSmtp)
    cfg = Smtp(host="mail.example", sender="from@example.com", recipients=["to@example.com"])
    notifications.smtp_send(cfg, "1.1.1.1", "user", "password", "test", "stable-id")
    assert calls[0] == ("1.1.1.1", 465, "mail.example")
    assert calls[2]["Message-ID"] == "<stable-id@dyndnsarr.local>"
    assert "password" not in calls[2].as_string()


def test_smtp_real_tls_verifies_original_hostname_with_pinned_ip(tmp_path):
    import socket
    import ssl
    import threading

    import trustme

    from dyndnsarr_hub.services.alert_config import Smtp
    from dyndnsarr_hub.services.notifications import smtp_send

    ca = trustme.CA()
    cert = ca.issue_cert("mail.test")
    ca_file, key_file, cert_file = [tmp_path / name for name in ("ca.pem", "key.pem", "cert.pem")]
    ca.cert_pem.write_to_path(ca_file)
    cert.private_key_pem.write_to_path(key_file)
    cert.cert_chain_pems[0].write_to_path(cert_file)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    messages = []
    errors = []
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        server.settimeout(5)

        def serve():
            try:
                conn, _ = server.accept()
                with context.wrap_socket(conn, server_side=True) as tls:
                    tls.settimeout(5)
                    with tls.makefile("rwb") as stream:
                        stream.write(b"220 mail.test ready\r\n")
                        stream.flush()
                        while line := stream.readline():
                            if line.upper().startswith(b"EHLO"):
                                stream.write(b"250-mail.test\r\n250 SIZE 100000\r\n")
                            elif line.upper().startswith(b"DATA"):
                                stream.write(b"354 continue\r\n")
                                stream.flush()
                                body = []
                                while (part := stream.readline()) not in (b".\r\n", b""):
                                    body.append(part)
                                messages.append(b"".join(body))
                                stream.write(b"250 accepted\r\n")
                            else:
                                stream.write(b"250 OK\r\n")
                            stream.flush()
            except Exception as exc:
                errors.append(type(exc).__name__)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        config = Smtp(
            host="mail.test",
            port=server.getsockname()[1],
            sender="from@example.com",
            recipients=["to@example.com"],
            ca_file=ca_file,
        )
        smtp_send(config, "127.0.0.1", "", "", "synthetic test", "stable-delivery")
        thread.join(5)
    assert not thread.is_alive() and not errors
    assert len(messages) == 1 and b"stable-delivery@dyndnsarr.local" in messages[0]
