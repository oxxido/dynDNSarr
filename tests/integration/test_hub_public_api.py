"""`GET /public/v1/status` con el Hub real (slice 4): estados, canarios, desactivación, métodos."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from dyndnsarr_hub.auth.tokens import hash_token
from dyndnsarr_hub.clock import utc_now
from dyndnsarr_hub.config import Settings
from dyndnsarr_hub.db import MIGRATIONS, agents_repo, public_repo
from dyndnsarr_hub.main import create_app
from dyndnsarr_hub.public.builder import build_dto
from dyndnsarr_hub.public.state import AgentProjection, RecordProjection
from dyndnsarr_shared.db import connect, migrate, transaction

AGENT = "raspberry-home"
RECORD = "canary-record.example.com"
CANARY_IP = "83.45.12.134"
SECRETS = {
    "ip": CANARY_IP,
    "ip3": "83.45.12",
    "mac": "aa:bb:cc:dd:ee:ff",
    "device": "nas-del-salon",
    "agent_token": "dda_canary-agent-token",
    "api_token": "ddh_canary-api-token",
    "dondominio": "canary-dondominio-key",
}


def now():
    return utc_now().replace(microsecond=0)


def projection(agent=AGENT, records=None, **overrides):
    t = now()
    base = dict(
        agent_id=agent,
        refreshed_at=t,
        ip_status="confirmed",
        public_ip_confirmed_at=t - timedelta(minutes=1),
        last_check_at=t - timedelta(minutes=1),
        records=tuple(
            records
            if records is not None
            else [RecordProjection(RECORD, "verified", t - timedelta(days=1), 0, None, None)]
        ),
    )
    return AgentProjection(**{**base, **overrides})


def make_db(tmp_path, seed=None, agents=(AGENT,)):
    conn = connect(tmp_path / "hub.db")
    migrate(conn, MIGRATIONS)
    with transaction(conn):
        for agent in agents:
            agents_repo.create_agent(conn, agent, hash_token(SECRETS["agent_token"] + agent), now())
        # Lo privado que el Hub sí guarda: la vista pública no debe tocarlo.
        if AGENT in agents:
            agents_repo.set_api_token(conn, AGENT, SECRETS["api_token"])
            conn.execute(
                "INSERT INTO events (agent_id, event_id, sequence, type, occurred_at, received_at, "
                "record_name, payload) VALUES (?, 'e1', 1, 'dns_update_succeeded', ?, ?, ?, ?)",
                (
                    AGENT,
                    "2026-10-19T11:00:00Z",
                    "2026-10-19T11:00:00Z",
                    RECORD,
                    json.dumps(
                        {"ip": CANARY_IP, "mac": SECRETS["mac"], "device": SECRETS["device"]}
                    ),
                ),
            )
        if seed:
            seed(conn)
    conn.close()


@contextmanager
def hub(tmp_path, seed=None, agents=(AGENT,), **settings):
    make_db(tmp_path, seed, agents)
    with TestClient(create_app(Settings(data_dir=tmp_path, **settings))) as client:
        yield client


def get(client):
    response = client.get("/public/v1/status")
    assert response.status_code == 200
    return response.json()


def healthy_seed(conn):
    public_repo.save_agent_status(conn, projection())
    public_repo.add_renewal(conn, AGENT, "r1", RECORD, now() - timedelta(hours=2), "34")
    public_repo.add_sample(conn, AGENT, now() - timedelta(hours=1), "healthy")


def test_an_empty_hub_is_unknown_and_valid(tmp_path):
    with hub(tmp_path, agents=()) as client:
        body = get(client)
    assert body["state"] == "unknown" and body["last_successful_check_at"] is None
    assert body["renewals"] == []
    assert body["history"]["buckets"] == ["unknown"] * 84 and body["history"]["coverage"] == 0
    assert body["schema_version"] == 1
    assert [c["id"] for c in body["checks"]] == ["dns_resolution", "ipv4", "ipv6", "service"]
    assert {c["state"] for c in body["checks"]} == {"unknown", "unavailable"}


def test_an_agent_without_projection_is_unknown(tmp_path):
    with hub(tmp_path) as client:
        assert get(client)["state"] == "unknown"


def test_a_healthy_agent(tmp_path):
    with hub(tmp_path, healthy_seed) as client:
        body = get(client)
    assert body["state"] == "healthy" and body["dns"] == "ok"
    assert {c["id"]: c["state"] for c in body["checks"]} == {
        "dns_resolution": "ok",
        "ipv4": "ok",
        "ipv6": "unavailable",
        "service": "ok",
    }
    assert body["last_successful_check_at"] is not None
    assert [(r["ip_suffix"], r["verification"], r["record"]) for r in body["renewals"]] == [
        ("34", "pending", None)
    ]
    assert body["history"]["buckets"][-1] == "healthy"
    assert body["history"]["coverage"] == pytest.approx(1 / 84)


def test_degraded_and_critical(tmp_path):
    t = now()

    def degraded(conn):
        public_repo.save_agent_status(conn, projection(ip_status="uncertain"))

    def critical(conn):
        old = RecordProjection(RECORD, "mismatch", t - timedelta(hours=2), 0, None, None)
        public_repo.save_agent_status(conn, projection(records=[old]))

    with hub(tmp_path / "a", degraded) as client:
        body = get(client)
    assert (body["state"], {c["id"]: c["state"] for c in body["checks"]}["ipv4"]) == (
        "degraded",
        "attention",
    )
    with hub(tmp_path / "b", critical) as client:
        body = get(client)
    assert (body["state"], body["dns"]) == ("critical", "attention")


def test_a_stale_projection_is_unknown_but_keeps_the_real_last_check(tmp_path):
    t = now()

    def stale(conn):
        old = t - timedelta(hours=3)
        public_repo.save_agent_status(
            conn, projection(refreshed_at=old, public_ip_confirmed_at=old, last_check_at=old)
        )

    with hub(tmp_path, stale) as client:
        body = get(client)
    assert body["state"] == "unknown" and body["dns"] == "unknown"
    assert body["last_successful_check_at"] == (t - timedelta(hours=3)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def test_a_critical_agent_and_an_unknown_one_aggregate_as_critical_then_unknown(tmp_path):
    t = now()

    def seed(conn):
        old = RecordProjection(RECORD, "mismatch", t - timedelta(hours=2), 0, None, None)
        public_repo.save_agent_status(conn, projection(records=[old]))

    with hub(tmp_path / "a", seed, agents=(AGENT, "other")) as client:
        assert get(client)["state"] == "critical"

    def healthy_only(conn):
        public_repo.save_agent_status(conn, projection())

    with hub(tmp_path / "b", healthy_only, agents=(AGENT, "other")) as client:
        assert get(client)["state"] == "unknown"


def test_no_private_value_reaches_the_json_nor_the_headers(tmp_path):
    with hub(tmp_path, healthy_seed) as client:
        response = client.get("/public/v1/status")
    text = response.text + str(dict(response.headers))
    for name, secret in SECRETS.items():
        assert secret not in text, name
    assert RECORD not in text


def test_show_domains_adds_only_the_record_name(tmp_path):
    with hub(tmp_path / "a", healthy_seed) as client:
        hidden = get(client)
    with hub(tmp_path / "b", healthy_seed, public_show_domains=True) as client:
        shown = get(client)
    assert shown["renewals"][0]["record"] == RECORD
    shown["renewals"][0]["record"] = None
    for body in (hidden, shown):
        body.pop("generated_at"), body["history"].pop("from")
    assert hidden == shown


def test_response_headers(tmp_path):
    with hub(tmp_path, healthy_seed) as client:
        headers = client.get("/public/v1/status").headers
    assert headers["cache-control"] == "no-store"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["content-type"] == "application/json"


@pytest.mark.parametrize("method", ["post", "put", "delete", "patch"])
def test_only_get_is_allowed(tmp_path, method):
    with hub(tmp_path) as client:
        assert getattr(client, method)("/public/v1/status").status_code == 405


def test_disabled_public_routes_are_indistinguishable_from_missing_ones(tmp_path):
    with hub(tmp_path, healthy_seed, public_enabled=False) as client:
        missing = client.get("/no-such-route")
        for path in ("/public/v1/status", "/", "/static/assets/dyno-green.png", "/static/"):
            response = client.get(path)
            assert response.status_code == 404, path
            assert response.content == missing.content, path
            assert dict(response.headers) == dict(missing.headers), path
        assert client.get("/healthz").status_code == 200


def test_building_the_dto_never_reads_events(tmp_path):
    make_db(tmp_path, healthy_seed)
    conn = connect(tmp_path / "hub.db")
    read_tables = set()

    def authorizer(action, arg1, arg2, db_name, source):
        if action == sqlite3.SQLITE_READ:
            read_tables.add(arg1)
        return sqlite3.SQLITE_OK

    conn.set_authorizer(authorizer)
    build_dto(conn, datetime.now(UTC), Settings(data_dir=tmp_path))
    conn.set_authorizer(None)
    conn.close()
    assert read_tables
    assert "events" not in read_tables and "agent_credentials" not in read_tables
