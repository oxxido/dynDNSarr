import asyncio
import json
import logging
import sqlite3
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from dyndnsarr_hub.api.events import MAX_BODY_BYTES, MAX_EVENTS_PER_BATCH, MAX_PAYLOAD_BYTES
from dyndnsarr_hub.auth.tokens import generate_token, hash_token
from dyndnsarr_hub.config import Settings
from dyndnsarr_hub.db import MIGRATIONS, agents_repo
from dyndnsarr_hub.main import create_app
from dyndnsarr_shared.db import connect, migrate, transaction

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
AGENT = "raspberry-home"
URL = f"/api/v1/agents/{AGENT}/events"


def make_event(sequence=1, **overrides):
    return {
        "event_id": str(uuid.uuid4()),
        "agent_id": AGENT,
        "sequence": sequence,
        "type": "ip_changed",
        "occurred_at": "2026-10-19T11:00:00Z",
        "record_name": None,
        "payload": {"ip": "1.2.3.4"},
        **overrides,
    }


def provision(tmp_path, *agent_ids):
    """Crea la BD del Hub con agentes y devuelve {agent_id: token}."""
    conn = connect(tmp_path / "hub.db")
    migrate(conn, MIGRATIONS)
    tokens = {}
    for agent_id in agent_ids:
        tokens[agent_id] = generate_token()
        with transaction(conn):
            agents_repo.create_agent(conn, agent_id, hash_token(tokens[agent_id]), NOW)
    conn.close()
    return tokens


@pytest.fixture
def hub(tmp_path):
    tokens = provision(tmp_path, AGENT, "otro-agente")
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        client.tokens = tokens
        client.db_path = tmp_path / "hub.db"
        yield client


def post(hub, events, token=None, agent_id=AGENT, **kwargs):
    headers = {"Authorization": f"Bearer {token or hub.tokens[AGENT]}"}
    return hub.post(
        f"/api/v1/agents/{agent_id}/events", json={"events": events}, headers=headers, **kwargs
    )


def stored(hub, query="SELECT event_id FROM events ORDER BY sequence"):
    conn = sqlite3.connect(hub.db_path)
    try:
        return [r[0] for r in conn.execute(query)]
    finally:
        conn.close()


def test_valid_batch_is_stored_and_acked(hub):
    events = [make_event(1), make_event(2)]
    response = post(hub, events)
    assert response.status_code == 200
    assert response.json() == {"acked": [e["event_id"] for e in events], "rejected": []}
    assert stored(hub) == [e["event_id"] for e in events]
    assert stored(hub, "SELECT last_seen_at FROM agents WHERE agent_id = 'raspberry-home'")[0]


def test_empty_batch_is_ok(hub):
    assert post(hub, []).json() == {"acked": [], "rejected": []}


def test_401_is_identical_for_every_authentication_failure(hub):
    revoked = generate_token()
    conn = sqlite3.connect(hub.db_path, isolation_level=None)
    conn.execute(
        "INSERT INTO agent_credentials (agent_id, token_hash, created_at, revoked_at) "
        "VALUES (?, ?, 'x', 'y')",
        (AGENT, hash_token(revoked)),
    )
    conn.execute("UPDATE agents SET disabled = 1 WHERE agent_id = 'otro-agente'")
    conn.close()
    cases = [
        {},  # sin cabecera
        {"Authorization": "Bearer dda_malo"},
        {"Authorization": f"Bearer {revoked}"},  # revocado
        {"Authorization": "Basic abc"},
        {"Authorization": "Bearer "},
        {"Authorization": f"Bearer {hub.tokens['otro-agente']}"},  # agente deshabilitado
    ]
    bodies = []
    for headers in cases:
        response = hub.post(URL, json={"events": [make_event()]}, headers=headers)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        body = response.json()
        bodies.append({k: body[k] for k in ("type", "code", "message_safe")})
    assert all(b == bodies[0] for b in bodies)
    # Un agente inexistente en la ruta con un token válido no es 401 sino 403, y nada se guarda.
    assert stored(hub) == []


def test_unknown_agent_in_path_with_a_bad_token_is_401(hub):
    response = hub.post(
        "/api/v1/agents/no-existe/events",
        json={"events": []},
        headers={"Authorization": "Bearer dda_malo"},
    )
    assert response.status_code == 401


def test_403_when_the_token_belongs_to_another_agent(hub):
    response = post(hub, [make_event()], token=hub.tokens["otro-agente"])
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"
    assert stored(hub) == []


def test_duplicates_are_acked_without_a_second_row(hub):
    event = make_event()
    assert post(hub, [event]).json()["acked"] == [event["event_id"]]
    assert post(hub, [event]).json()["acked"] == [event["event_id"]]
    assert post(hub, [event, event]).json()["acked"] == [event["event_id"]]
    assert stored(hub) == [event["event_id"]]


def test_an_invalid_event_is_rejected_and_the_rest_is_stored(hub):
    good = make_event(1)
    bad = make_event(2, occurred_at="ayer")
    other_agent = make_event(3, agent_id="otro-agente")
    response = post(hub, [good, bad, other_agent, "no-es-un-objeto"])
    body = response.json()
    assert response.status_code == 200
    assert body["acked"] == [good["event_id"]]
    assert body["rejected"] == [
        {"event_id": bad["event_id"], "code": "invalid_event"},
        {"event_id": other_agent["event_id"], "code": "invalid_event"},
        {"event_id": "", "code": "invalid_event"},
    ]
    assert stored(hub) == [good["event_id"]]


def test_an_unknown_event_type_is_stored(hub):
    event = make_event(type="evento_de_un_agent_mas_nuevo")
    assert post(hub, [event]).json()["acked"] == [event["event_id"]]


def test_malformed_batch_is_400(hub):
    headers = {"Authorization": f"Bearer {hub.tokens[AGENT]}"}
    assert hub.post(URL, content=b"no json", headers=headers).status_code == 400
    assert hub.post(URL, json={"events": "x"}, headers=headers).status_code == 400
    assert hub.post(URL, json={"events": [], "otro": 1}, headers=headers).status_code == 400


def test_batch_over_the_event_limit_is_413(hub):
    events = [make_event(i + 1) for i in range(MAX_EVENTS_PER_BATCH + 1)]
    response = post(hub, events)
    assert response.status_code == 413
    assert response.json()["code"] == "batch_too_large"
    assert stored(hub) == []
    assert post(hub, events[:MAX_EVENTS_PER_BATCH]).status_code == 200


def test_body_over_the_limit_is_413_with_or_without_content_length(hub):
    headers = {"Authorization": f"Bearer {hub.tokens[AGENT]}"}
    big = b'{"events": []}' + b" " * MAX_BODY_BYTES
    declared = hub.post(URL, content=big, headers=headers)
    assert declared.status_code == 413 and declared.json()["code"] == "body_too_large"

    def chunks():  # sin Content-Length: se lee por trozos con tope
        for _ in range(8):
            yield b" " * (MAX_BODY_BYTES // 4)

    chunked = hub.post(URL, content=chunks(), headers=headers)
    assert chunked.status_code == 413


def test_payload_over_the_limit_rejects_only_that_event(hub):
    big = make_event(1, payload={"blob": "x" * MAX_PAYLOAD_BYTES})
    good = make_event(2)
    body = post(hub, [big, good]).json()
    assert body["acked"] == [good["event_id"]]
    assert body["rejected"] == [{"event_id": big["event_id"], "code": "payload_too_large"}]


def test_authentication_happens_before_the_body_is_read(tmp_path):
    provision(tmp_path, AGENT)
    app = create_app(Settings(data_dir=tmp_path))
    received = []

    async def go():
        async with app.router.lifespan_context(app):
            sent = []

            async def receive():
                received.append(1)
                return {"type": "http.request", "body": b"{}", "more_body": False}

            async def send(message):
                sent.append(message)

            scope = {
                "type": "http",
                "method": "POST",
                "path": URL,
                "raw_path": URL.encode(),
                "query_string": b"",
                "headers": [(b"authorization", b"Bearer dda_malo")],
                "app": app,
            }
            await app(scope, receive, send)
            return sent

    sent = asyncio.run(go())
    assert sent[0]["status"] == 401
    assert received == []


def test_the_token_never_appears_in_logs(hub, caplog):
    caplog.set_level(logging.DEBUG)
    token = hub.tokens[AGENT]
    post(hub, [make_event()])
    hub.post(URL, json={"events": []}, headers={"Authorization": f"Bearer {token}x"})
    post(hub, [make_event()], token=hub.tokens["otro-agente"])
    assert token not in caplog.text
    assert hub.tokens["otro-agente"] not in caplog.text
    assert json.dumps(token) not in caplog.text
