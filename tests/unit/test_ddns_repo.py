import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest

from dyndnsarr_agent.db import MIGRATIONS, ddns_repo
from dyndnsarr_shared.db import connect, migrate

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path):
    conn = connect(tmp_path / "agent.db")
    migrate(conn, MIGRATIONS)
    return conn


def test_migration_0002_applies_over_the_slice1_database(tmp_path):
    path = tmp_path / "agent.db"
    old = connect(path)
    # Simula una BD que solo conoce el slice1.
    old.executescript(
        "BEGIN; CREATE TABLE app_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
        "updated_at TEXT NOT NULL); INSERT INTO app_state VALUES ('boot_count', '4', 'x'); "
        "PRAGMA user_version = 1; COMMIT;"
    )
    assert migrate(old, MIGRATIONS) == 3
    tables = {r[0] for r in old.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"public_ip_observations", "dns_checks", "record_state", "events"} <= tables
    assert old.execute("SELECT value FROM app_state WHERE key = 'boot_count'").fetchone() == ("4",)


def test_iso_uses_utc_with_z_suffix():
    assert ddns_repo.iso(NOW) == "2026-10-14T12:00:00Z"
    local = datetime(2026, 10, 14, 14, 0, tzinfo=timezone(timedelta(hours=2)))
    assert ddns_repo.iso(local) == "2026-10-14T12:00:00Z"


def test_events_have_uuid_and_monotonic_sequence(conn):
    first = ddns_repo.insert_event(conn, "agent_started", NOW, None, {"a": 1})
    second = ddns_repo.insert_event(conn, "ip_changed", NOW, "home.example.com", {"ip": "1.2.3.4"})
    assert str(uuid.UUID(first.event_id)) == first.event_id
    assert second.sequence == first.sequence + 1
    [row_a, row_b] = ddns_repo.list_events(conn, limit=10, newest_first=False)
    assert (row_a.type, row_a.occurred_at, row_a.record_name) == (
        "agent_started",
        "2026-10-14T12:00:00Z",
        None,
    )
    assert row_a.payload == {"a": 1}
    assert row_b.record_name == "home.example.com"


def test_event_sequence_is_never_reused_after_purge(conn):
    old = NOW - timedelta(days=400)
    last = ddns_repo.insert_event(conn, "agent_started", old, None, {})
    ddns_repo.purge(conn, NOW, samples_days=30, events_days=365)
    assert ddns_repo.list_events(conn, limit=10) == []
    assert ddns_repo.insert_event(conn, "agent_started", NOW, None, {}).sequence > last.sequence


def test_event_type_must_be_known(conn):
    with pytest.raises(ValueError):
        ddns_repo.insert_event(conn, "bogus", NOW, None, {})


def test_record_state_roundtrip_and_update(conn):
    assert ddns_repo.get_record_state(conn, "home.example.com") is None
    ddns_repo.set_record_state(conn, "home.example.com", "verified", NOW)
    ddns_repo.set_record_state(conn, "home.example.com", "mismatch", NOW + timedelta(minutes=5))
    state = ddns_repo.get_record_state(conn, "home.example.com")
    assert (state.verification, state.since) == ("mismatch", "2026-10-14T12:05:00Z")
    with pytest.raises(sqlite3.IntegrityError):
        ddns_repo.set_record_state(conn, "home.example.com", "bogus", NOW)


def test_app_state_values(conn):
    assert ddns_repo.get_state(conn, "public_ip") is None
    ddns_repo.set_state(conn, "public_ip", "1.2.3.4", NOW)
    ddns_repo.set_state(conn, "public_ip", "5.6.7.8", NOW)
    assert ddns_repo.get_state(conn, "public_ip") == "5.6.7.8"


def test_state_persists_between_connections(tmp_path):
    path = tmp_path / "agent.db"
    first = connect(path)
    migrate(first, MIGRATIONS)
    ddns_repo.set_record_state(first, "home.example.com", "verified", NOW)
    ddns_repo.insert_event(first, "ip_changed", NOW, None, {})
    first.close()
    second = connect(path)
    assert ddns_repo.get_record_state(second, "home.example.com").verification == "verified"
    assert len(ddns_repo.list_events(second, limit=10)) == 1


def test_purge_removes_only_what_is_older_than_the_limit(conn):
    old = NOW - timedelta(days=31)
    recent = NOW - timedelta(days=29)
    for when in (old, recent):
        ddns_repo.insert_ip_observation(conn, when, "https://a.example", "1.2.3.4", None)
        ddns_repo.insert_dns_check(conn, when, "home.example.com", "1.1.1.1", "1.2.3.4", None)
    ddns_repo.insert_event(conn, "agent_started", NOW - timedelta(days=100), None, {})
    result = ddns_repo.purge(conn, NOW, samples_days=30, events_days=365)
    assert result == {
        "public_ip_observations": 1,
        "dns_checks": 1,
        "events": 0,
        "dns_update_attempts": 0,
    }
    assert conn.execute("SELECT observed_at FROM public_ip_observations").fetchall() == [
        (ddns_repo.iso(recent),)
    ]
    assert conn.execute("SELECT count(*) FROM dns_checks").fetchone() == (1,)
    assert len(ddns_repo.list_events(conn, limit=10)) == 1


def test_samples_keep_error_codes_without_ip(conn):
    ddns_repo.insert_ip_observation(conn, NOW, "https://a.example", None, "timeout")
    ddns_repo.insert_dns_check(conn, NOW, "home.example.com", "1.1.1.1", None, "nxdomain")
    assert conn.execute("SELECT ip, error_code FROM public_ip_observations").fetchone() == (
        None,
        "timeout",
    )
    assert conn.execute("SELECT ip, error_code FROM dns_checks").fetchone() == (None, "nxdomain")


def test_event_payload_is_stored_as_json(conn):
    ddns_repo.insert_event(conn, "ip_changed", NOW, None, {"previous": None, "ip": "1.2.3.4"})
    raw = conn.execute("SELECT payload FROM events").fetchone()[0]
    assert json.loads(raw) == {"previous": None, "ip": "1.2.3.4"}


def test_migration_0003_keeps_record_state_of_a_0002_database(tmp_path):
    path = tmp_path / "agent.db"
    old = connect(path)
    # Aplica solo 0001 y 0002 (con datos) y luego deja que `migrate` aplique la 0003.
    migrate_dir = MIGRATIONS
    scripts = sorted(f for f in migrate_dir.iterdir() if f.name.endswith(".sql"))
    for version, script in enumerate(scripts[:2], start=1):
        sql = script.read_text(encoding="utf-8")
        old.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")
    old.execute(
        "INSERT INTO record_state VALUES ('a.example.com', 'mismatch', '2026-10-14T12:00:00Z')"
    )
    old.execute(
        "INSERT INTO record_state VALUES ('b.example.com', 'verified', '2026-10-14T13:00:00Z')"
    )
    assert migrate(old, MIGRATIONS) == 3
    assert ddns_repo.list_record_states(old) == [
        ddns_repo.RecordState("a.example.com", "mismatch", "2026-10-14T12:00:00Z"),
        ddns_repo.RecordState("b.example.com", "verified", "2026-10-14T13:00:00Z"),
    ]
    assert old.execute("SELECT count(*) FROM dns_update_attempts").fetchone() == (0,)


def test_record_state_accepts_pending_verification_and_still_rejects_others(conn):
    ddns_repo.save_record_state(
        conn, ddns_repo.RecordState("home.example.com", "pending_verification", "x")
    )
    assert ddns_repo.get_record_state(conn, "home.example.com").verification == (
        "pending_verification"
    )
    with pytest.raises(sqlite3.IntegrityError):
        ddns_repo.save_record_state(conn, ddns_repo.RecordState("home.example.com", "bogus", "x"))


def test_update_state_persists_between_connections(tmp_path):
    path = tmp_path / "agent.db"
    first = connect(path)
    migrate(first, MIGRATIONS)
    state = ddns_repo.RecordState(
        "home.example.com",
        "mismatch",
        "2026-10-14T12:00:00Z",
        accepted_ip="1.2.3.4",
        accepted_at="2026-10-14T11:00:00Z",
        failures=3,
        failure_ip="5.6.7.8",
        next_attempt_at="2026-10-14T12:04:00Z",
        last_error_code="rate_limited",
    )
    ddns_repo.save_record_state(first, state)
    first.close()
    second = connect(path)
    assert ddns_repo.get_record_state(second, "home.example.com") == state
    ddns_repo.save_record_state(second, ddns_repo.RecordState("home.example.com", "verified", "z"))
    assert ddns_repo.get_record_state(second, "home.example.com").failures == 0


def test_set_record_state_leaves_update_fields_alone(conn):
    ddns_repo.save_record_state(
        conn, ddns_repo.RecordState("home.example.com", "mismatch", "x", failures=2)
    )
    ddns_repo.set_record_state(conn, "home.example.com", "verified", NOW)
    assert ddns_repo.get_record_state(conn, "home.example.com").failures == 2


def test_update_attempts_are_stored_and_purged_with_the_event_retention(conn):
    for when in (NOW - timedelta(days=400), NOW - timedelta(days=1)):
        ddns_repo.insert_update_attempt(
            conn, when, "home.example.com", "1.2.3.4", True, True, None, None
        )
    ddns_repo.insert_update_attempt(
        conn, NOW, "home.example.com", "1.2.3.4", False, None, "rate_limited", True
    )
    assert ddns_repo.purge(conn, NOW, samples_days=30, events_days=365)["dns_update_attempts"] == 1
    rows = conn.execute(
        "SELECT accepted, changed, error_code, retryable FROM dns_update_attempts ORDER BY id"
    ).fetchall()
    assert rows == [(1, 1, None, None), (0, None, "rate_limited", 1)]


def test_parse_iso_roundtrips_iso():
    assert ddns_repo.parse_iso(ddns_repo.iso(NOW)) == NOW
