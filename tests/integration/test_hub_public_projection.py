"""Proyección pública en la ingestión (slice 4, D2): sin IP completa, idempotente y tolerante."""

import sqlite3
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from dyndnsarr_hub.auth.tokens import generate_token, hash_token
from dyndnsarr_hub.config import Settings
from dyndnsarr_hub.db import MIGRATIONS, agents_repo
from dyndnsarr_hub.main import create_app
from dyndnsarr_shared.db import connect, migrate, transaction

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
AGENT = "raspberry-home"
RECORD = "home.example.com"
CANARY = "83.45.12.134"


@pytest.fixture
def hub(tmp_path):
    conn = connect(tmp_path / "hub.db")
    migrate(conn, MIGRATIONS)
    token = generate_token()
    with transaction(conn):
        agents_repo.create_agent(conn, AGENT, hash_token(token), NOW)
    conn.close()
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        client.token, client.db_path = token, tmp_path / "hub.db"
        yield client


def event(type, payload, at="2026-10-19T11:00:00Z", record=RECORD, event_id=None, sequence=1):
    return {
        "event_id": event_id or str(uuid.uuid4()),
        "agent_id": AGENT,
        "sequence": sequence,
        "type": type,
        "occurred_at": at,
        "record_name": record,
        "payload": payload,
    }


def send(hub, *events):
    response = hub.post(
        f"/api/v1/agents/{AGENT}/events",
        json={"events": list(events)},
        headers={"Authorization": f"Bearer {hub.token}"},
    )
    assert response.status_code == 200
    return response.json()


def rows(hub):
    conn = sqlite3.connect(hub.db_path)
    try:
        return conn.execute(
            "SELECT record_name, occurred_at, ip_suffix, verified_at FROM public_renewals "
            "ORDER BY occurred_at"
        ).fetchall()
    finally:
        conn.close()


def renewal(**kwargs):
    return event("dns_update_succeeded", {"ip": CANARY, "changed": True}, **kwargs)


def test_a_renewal_keeps_only_the_suffix(hub):
    send(hub, renewal())
    assert rows(hub) == [(RECORD, "2026-10-19T11:00:00Z", "34", None)]
    conn = sqlite3.connect(hub.db_path)
    public = "".join(
        str(r)
        for table in ("public_renewals", "public_agent_status", "public_status_samples")
        for r in conn.execute(f"SELECT * FROM {table}")
    )
    assert CANARY not in public


def test_the_verification_marks_the_pending_renewal(hub):
    send(hub, renewal(sequence=1))
    send(hub, event("dns_verified", {"ip": CANARY}, at="2026-10-19T11:05:00Z", sequence=2))
    assert rows(hub)[0][3] == "2026-10-19T11:05:00Z"


def test_a_verification_in_the_same_batch_and_for_another_ip_or_record(hub):
    send(
        hub,
        renewal(sequence=1),
        event("dns_verified", {"ip": "83.45.12.135"}, at="2026-10-19T11:05:00Z", sequence=2),
        event(
            "dns_verified",
            {"ip": CANARY},
            at="2026-10-19T11:05:00Z",
            record="x.example.com",
            sequence=3,
        ),
    )
    assert rows(hub)[0][3] is None
    send(hub, event("dns_verified", {"ip": CANARY}, at="2026-10-19T11:06:00Z", sequence=4))
    assert rows(hub)[0][3] == "2026-10-19T11:06:00Z"


def test_a_verification_before_its_renewal_does_not_verify_it(hub):
    send(hub, event("dns_verified", {"ip": CANARY}, at="2026-10-19T10:00:00Z", sequence=1))
    send(hub, renewal(sequence=2))
    assert rows(hub)[0][3] is None


def test_a_resent_batch_changes_nothing(hub):
    batch = [renewal(event_id=str(uuid.uuid4()))]
    send(hub, *batch)
    first = rows(hub)
    send(hub, *batch)
    assert rows(hub) == first and len(first) == 1


@pytest.mark.parametrize(
    "bad",
    [
        {"ip": "no-es-ip"},
        {"ip": "192.168.1.10"},
        {"ip": "2001:db8::1"},
        {"ip": 5},
        {},
    ],
)
def test_a_renewal_with_an_invalid_ip_is_ignored_without_rejecting_the_batch(hub, bad):
    good = renewal(at="2026-10-19T11:30:00Z", sequence=2)
    result = send(hub, event("dns_update_succeeded", bad, sequence=1), good)
    assert len(result["acked"]) == 2 and not result["rejected"]
    assert [r[1] for r in rows(hub)] == ["2026-10-19T11:30:00Z"]


def test_a_renewal_without_record_is_ignored(hub):
    send(hub, renewal(record=None))
    assert rows(hub) == []
