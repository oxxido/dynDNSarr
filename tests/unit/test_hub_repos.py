import sqlite3
import uuid
from datetime import UTC, datetime

import pytest

from dyndnsarr_hub.auth.tokens import generate_token, hash_token
from dyndnsarr_hub.db import MIGRATIONS, agents_repo, events_repo
from dyndnsarr_shared.db import connect, migrate, transaction
from dyndnsarr_shared.events import AgentEvent

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path):
    conn = connect(tmp_path / "hub.db")
    migrate(conn, MIGRATIONS)
    yield conn
    conn.close()


def make_agent(conn, agent_id="raspberry-home"):
    token = generate_token()
    with transaction(conn):
        agents_repo.create_agent(conn, agent_id, hash_token(token), NOW)
    return token


def event(sequence=1, agent_id="raspberry-home", event_id=None):
    return AgentEvent.model_validate(
        {
            "event_id": event_id or str(uuid.uuid4()),
            "agent_id": agent_id,
            "sequence": sequence,
            "type": "ip_changed",
            "occurred_at": "2026-10-19T11:00:00Z",
            "record_name": None,
            "payload": {"ip": "1.2.3.4"},
        }
    )


def test_migration_0002_applies_over_a_0001_database(tmp_path):
    conn = connect(tmp_path / "hub.db")
    assert migrate(conn, MIGRATIONS) == len(
        [f for f in MIGRATIONS.iterdir() if f.name.endswith(".sql")]
    )
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"app_state", "agents", "agent_credentials", "events"} <= tables


def test_token_has_prefix_and_entropy_and_hash_is_stable():
    token = generate_token()
    assert token.startswith("dda_") and len(token) >= 40
    assert generate_token() != token
    assert hash_token(token) == hash_token(token) != token
    assert len(hash_token(token)) == 64


def test_token_is_authenticated_only_by_its_hash(conn):
    token = make_agent(conn)
    assert agents_repo.authenticate(conn, hash_token(token)) == "raspberry-home"
    assert agents_repo.authenticate(conn, hash_token("dda_otro")) is None


def test_the_clear_token_is_in_no_table(conn):
    token = make_agent(conn)
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
        for row in conn.execute(f"SELECT * FROM {table}"):  # noqa: S608 (nombres de sqlite_master)
            assert token not in [str(v) for v in row]


def test_token_hash_is_unique(conn):
    make_agent(conn)
    with pytest.raises(sqlite3.IntegrityError), transaction(conn):
        agents_repo.create_agent(conn, "otro", hash_token("x"), NOW)
        agents_repo.add_token(conn, "otro", hash_token("x"), NOW)


def test_duplicate_agent_and_unknown_agent(conn):
    make_agent(conn)
    with pytest.raises(agents_repo.AgentExistsError), transaction(conn):
        agents_repo.create_agent(conn, "raspberry-home", hash_token("y"), NOW)
    with pytest.raises(agents_repo.UnknownAgentError), transaction(conn):
        agents_repo.add_token(conn, "nadie", hash_token("y"), NOW)
    with pytest.raises(agents_repo.UnknownAgentError), transaction(conn):
        agents_repo.revoke(conn, "nadie", NOW, everything=False)


def test_rotate_keeps_the_old_token_and_revoke_leaves_only_the_last(conn):
    old = make_agent(conn)
    new = generate_token()
    with transaction(conn):
        agents_repo.add_token(conn, "raspberry-home", hash_token(new), NOW)
    assert agents_repo.authenticate(conn, hash_token(old)) == "raspberry-home"
    assert agents_repo.authenticate(conn, hash_token(new)) == "raspberry-home"
    with transaction(conn):
        assert agents_repo.revoke(conn, "raspberry-home", NOW, everything=False) == 1
    assert agents_repo.authenticate(conn, hash_token(old)) is None
    assert agents_repo.authenticate(conn, hash_token(new)) == "raspberry-home"


def test_revoke_all_disables_the_agent_and_a_new_token_reenables_it(conn):
    token = make_agent(conn)
    with transaction(conn):
        assert agents_repo.revoke(conn, "raspberry-home", NOW, everything=True) == 1
    assert agents_repo.authenticate(conn, hash_token(token)) is None
    assert agents_repo.list_agents(conn)[0].disabled
    fresh = generate_token()
    with transaction(conn):
        agents_repo.add_token(conn, "raspberry-home", hash_token(fresh), NOW)
    assert agents_repo.authenticate(conn, hash_token(fresh)) == "raspberry-home"
    assert not agents_repo.list_agents(conn)[0].disabled


def test_list_agents_counts_active_tokens_and_has_no_hashes(conn):
    make_agent(conn)
    (row,) = agents_repo.list_agents(conn)
    assert row.active_tokens == 1 and row.last_seen_at is None
    assert not hasattr(row, "token_hash")


def test_insert_events_deduplicates_by_agent_and_event_id(conn):
    make_agent(conn)
    e = event()
    with transaction(conn):
        events_repo.insert_events(conn, [e, e], NOW)
    with transaction(conn):
        events_repo.insert_events(conn, [e], NOW)
    assert len(events_repo.list_events(conn, "raspberry-home", 10)) == 1


def test_same_event_id_is_allowed_for_different_agents(conn):
    make_agent(conn)
    make_agent(conn, "otro-agente")
    shared_id = str(uuid.uuid4())
    with transaction(conn):
        events_repo.insert_events(
            conn,
            [event(event_id=shared_id), event(agent_id="otro-agente", event_id=shared_id)],
            NOW,
        )
    assert len(events_repo.list_events(conn, None, 10)) == 2


def test_list_events_returns_payload_and_orders_by_sequence(conn):
    make_agent(conn)
    with transaction(conn):
        events_repo.insert_events(conn, [event(1), event(2), event(3)], NOW)
    listed = events_repo.list_events(conn, "raspberry-home", 10, newest_first=False)
    assert [e.sequence for e in listed] == [1, 2, 3]
    assert listed[0].payload == {"ip": "1.2.3.4"}
