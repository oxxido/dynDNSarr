"""Efecto de los eventos del Agent en el Hub, a través de la ingestión real."""

import logging
import sqlite3
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from dyndnsarr_hub.auth.tokens import generate_token, hash_token
from dyndnsarr_hub.config import Settings
from dyndnsarr_hub.db import MIGRATIONS, agents_repo, control_repo
from dyndnsarr_hub.main import create_app
from dyndnsarr_hub.services import events_handlers
from dyndnsarr_shared.db import connect, migrate, transaction

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
AGENT = "raspberry-home"
H = "a" * 64
EXPIRES = "2026-10-19T12:05:00Z"


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


def event(type, payload, sequence=1, event_id=None):
    return {
        "event_id": event_id or str(uuid.uuid4()),
        "agent_id": AGENT,
        "sequence": sequence,
        "type": type,
        "occurred_at": "2026-10-19T11:00:00Z",
        "record_name": None,
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


def db(hub):
    conn = connect(hub.db_path)
    return conn


def observed(hub):
    conn = db(hub)
    try:
        return control_repo.get_observed(conn, AGENT)
    finally:
        conn.close()


def applied(version, sequence=1, hash=H):
    return event("config_applied", {"version": version, "hash": hash}, sequence)


def test_config_applied_updates_the_observed_state(hub):
    result = send(hub, applied(3))
    assert len(result["acked"]) == 1
    state = observed(hub)
    assert (state.config_version, state.config_hash, state.last_result) == (3, H, "applied")


def test_config_rejected_records_the_attempt_without_moving_the_version(hub):
    send(hub, applied(3))
    send(
        hub, event("config_rejected", {"version": 4, "hash": "b" * 64, "code": "invalid_config"}, 2)
    )
    state = observed(hub)
    assert (state.config_version, state.last_result, state.last_code) == (
        3,
        "rejected",
        "invalid_config",
    )
    assert state.last_attempt_version == 4


def test_an_event_with_a_lower_version_does_not_move_the_state_back(hub):
    send(hub, applied(5))
    send(hub, applied(3, sequence=2, hash="b" * 64))
    state = observed(hub)
    assert (state.config_version, state.config_hash) == (5, H)


def test_a_duplicate_event_is_not_processed_twice(hub, monkeypatch):
    seen = []
    real = events_handlers.handle_new_events

    def spy(conn, events, now):
        seen.append([e.event_id for e in events])
        real(conn, events, now)

    monkeypatch.setattr("dyndnsarr_hub.api.events.events_handlers.handle_new_events", spy)
    first = applied(3)
    send(hub, first)
    send(hub, first)
    assert seen == [[first["event_id"]], []]


def test_config_reset_returns_the_observed_version_to_zero(hub):
    send(hub, applied(5))
    send(hub, event("config_reset", {"previous_version": 5}, 2))
    state = observed(hub)
    assert (state.config_version, state.config_hash, state.last_result) == (0, None, "reset")


def new_command(hub, command_id):
    conn = db(hub)
    with transaction(conn):
        control_repo.create_command(conn, command_id, AGENT, "sync_ddns", "admin:cli", NOW, EXPIRES)
    conn.close()


def command(hub, command_id):
    conn = db(hub)
    try:
        return control_repo.get_command(conn, command_id)
    finally:
        conn.close()


def test_a_command_reaches_its_final_state_by_event_even_if_the_http_reply_was_lost(hub):
    command_id = str(uuid.uuid4())
    new_command(hub, command_id)  # el POST se perdió: sigue `pending`
    send(
        hub,
        event(
            "command_completed",
            {
                "command_id": command_id,
                "type": "sync_ddns",
                "status": "succeeded",
                "result": {"records": {"home.example.com": "verified"}},
            },
        ),
    )
    done = command(hub, command_id)
    assert (done.status, done.result) == (
        "succeeded",
        {"records": {"home.example.com": "verified"}},
    )


def test_events_of_unknown_commands_and_other_types_are_stored_and_harmless(hub):
    unknown = event("command_completed", {"command_id": "x", "status": "failed", "result": None})
    other = event("ip_changed", {"ip": "1.2.3.4"}, 2)
    result = send(hub, unknown, other)
    assert len(result["acked"]) == 2 and result["rejected"] == []


@pytest.mark.parametrize(
    "bad",
    [
        event("config_applied", {"version": "tres", "hash": H}),
        event("config_applied", {}),
        event("config_rejected", {"version": 1, "hash": 5}),
        event("command_completed", {"command_id": 1, "status": "succeeded"}),
        event("command_completed", {"command_id": "x", "status": "wat"}),
    ],
)
def test_a_malformed_control_event_is_acked_and_ignored(hub, bad):
    result = send(hub, bad)
    assert result["acked"] == [bad["event_id"]]
    assert observed(hub) is None


def test_a_failing_handler_does_not_reject_the_batch(hub, monkeypatch, caplog):
    def boom(conn, event, now):
        raise RuntimeError("fallo canario-0123")

    monkeypatch.setitem(events_handlers._HANDLERS, "config_applied", boom)
    with caplog.at_level(logging.ERROR):
        result = send(hub, applied(1))
    assert len(result["acked"]) == 1
    assert "canario-0123" not in caplog.text
    conn = sqlite3.connect(hub.db_path)
    assert conn.execute("SELECT count(*) FROM events").fetchone() == (1,)
