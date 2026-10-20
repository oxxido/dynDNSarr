"""Migración 0004 y repositorio de la proyección pública (slice 4)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from dyndnsarr_hub.auth.tokens import generate_token, hash_token
from dyndnsarr_hub.db import MIGRATIONS, agents_repo, events_repo, public_repo
from dyndnsarr_hub.public.state import AgentProjection, RecordProjection, ip_suffix
from dyndnsarr_shared.db import connect, migrate, transaction
from dyndnsarr_shared.events import AgentEvent

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
CANARY = "83.45.12.134"


@pytest.fixture
def conn(tmp_path):
    conn = connect(tmp_path / "hub.db")
    migrate(conn, MIGRATIONS)
    for agent_id in ("raspberry-home", "raspberry-office"):
        with transaction(conn):
            agents_repo.create_agent(conn, agent_id, hash_token(generate_token()), NOW)
    yield conn
    conn.close()


def renew(conn, event_id, at, agent="raspberry-home", record="home.example.com", suffix="34"):
    with transaction(conn):
        public_repo.add_renewal(conn, agent, event_id, record, at, suffix)


def test_migration_0004_applies_over_a_0003_database_with_data(tmp_path):
    conn = connect(tmp_path / "hub.db")
    scripts = sorted(f for f in MIGRATIONS.iterdir() if f.name.endswith(".sql"))
    assert scripts[3].name.startswith("0004")
    for version, script in enumerate(scripts[:3], start=1):
        sql = script.read_text(encoding="utf-8")
        conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")
    with transaction(conn):
        agents_repo.create_agent(conn, "raspberry-home", hash_token("dda_x"), NOW)
        events_repo.insert_events(
            conn,
            [
                AgentEvent(
                    event_id=str(uuid.uuid4()),
                    agent_id="raspberry-home",
                    sequence=1,
                    type="dns_update_succeeded",
                    occurred_at="2026-10-19T11:00:00Z",
                    record_name="home.example.com",
                    payload={"ip": CANARY},
                )
            ],
            NOW,
        )
    assert migrate(conn, MIGRATIONS) == len(scripts)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"public_renewals", "public_agent_status", "public_status_samples"} <= tables
    assert conn.execute("SELECT count(*) FROM events").fetchone() == (1,)
    # Las renovaciones anteriores a la migración no se proyectan.
    assert conn.execute("SELECT count(*) FROM public_renewals").fetchone() == (0,)


def test_add_renewal_is_idempotent_by_event_id(conn):
    renew(conn, "e1", NOW)
    renew(conn, "e1", NOW + timedelta(hours=1))
    [renewal] = public_repo.recent_renewals(conn, 5)
    assert renewal.occurred_at == NOW and renewal.ip_suffix == "34" and not renewal.verified


def test_each_agent_keeps_at_most_fifty_renewals(conn):
    for i in range(55):
        renew(conn, f"h{i:02}", NOW + timedelta(minutes=i))
    renew(conn, "o1", NOW, agent="raspberry-office")
    count = lambda agent: conn.execute(  # noqa: E731
        "SELECT count(*) FROM public_renewals WHERE agent_id = ?", (agent,)
    ).fetchone()[0]
    assert (count("raspberry-home"), count("raspberry-office")) == (50, 1)
    oldest = conn.execute(
        "SELECT min(event_id) FROM public_renewals WHERE agent_id = 'raspberry-home'"
    ).fetchone()[0]
    assert oldest == "h05"


def test_recent_renewals_newest_first_limited_and_skip_disabled_agents(conn):
    renew(conn, "e1", NOW)
    renew(conn, "e2", NOW + timedelta(hours=1))
    renew(conn, "e3", NOW + timedelta(hours=2), agent="raspberry-office")
    assert [r.occurred_at.hour for r in public_repo.recent_renewals(conn, 2)] == [14, 13]
    with transaction(conn):
        agents_repo.revoke(conn, "raspberry-office", NOW, everything=True)
    assert [r.occurred_at.hour for r in public_repo.recent_renewals(conn, 5)] == [13, 12]


def test_mark_verified_matches_record_and_suffix_and_ignores_later_renewals(conn):
    renew(conn, "e1", NOW)
    renew(conn, "e2", NOW, record="other.example.com")
    renew(conn, "e3", NOW, suffix="35")
    renew(conn, "e4", NOW + timedelta(minutes=10))
    with transaction(conn):
        public_repo.mark_verified(
            conn, "raspberry-home", "home.example.com", "34", NOW + timedelta(minutes=5)
        )
    verified = dict(conn.execute("SELECT event_id, verified_at IS NOT NULL FROM public_renewals"))
    assert verified == {"e1": 1, "e2": 0, "e3": 0, "e4": 0}


def test_a_verification_before_its_renewal_does_not_verify_it(conn):
    with transaction(conn):
        public_repo.mark_verified(
            conn, "raspberry-home", "home.example.com", "34", NOW - timedelta(hours=1)
        )
    renew(conn, "e1", NOW)
    assert not public_repo.recent_renewals(conn, 5)[0].verified


def projection(agent="raspberry-home", **overrides):
    base = dict(
        agent_id=agent,
        refreshed_at=NOW,
        ip_status="confirmed",
        public_ip_confirmed_at=NOW - timedelta(minutes=1),
        last_check_at=NOW - timedelta(minutes=1),
        records=(
            RecordProjection(
                "home.example.com", "mismatch", NOW - timedelta(hours=2), 2, NOW, "timeout"
            ),
        ),
    )
    return AgentProjection(**{**base, **overrides})


def test_agent_status_roundtrip_and_agents_without_status_are_none(conn):
    with transaction(conn):
        public_repo.save_agent_status(conn, projection())
    home, office = public_repo.enabled_agent_statuses(conn)
    assert home == projection()
    assert office is None
    with transaction(conn):
        public_repo.save_agent_status(conn, projection(ip_status="uncertain", records=()))
    assert public_repo.enabled_agent_statuses(conn)[0].ip_status == "uncertain"


def test_disabled_agents_are_not_listed(conn):
    with transaction(conn):
        agents_repo.revoke(conn, "raspberry-office", NOW, everything=True)
    assert len(public_repo.enabled_agent_statuses(conn)) == 1


def test_samples_are_replaced_per_instant_purged_after_30_days_and_filtered_by_window(conn):
    with transaction(conn):
        public_repo.add_sample(conn, "raspberry-home", NOW, "healthy")
        public_repo.add_sample(conn, "raspberry-home", NOW, "degraded")
        public_repo.add_sample(conn, "raspberry-home", NOW - timedelta(days=29), "healthy")
        public_repo.add_sample(conn, "raspberry-home", NOW - timedelta(days=31), "critical")
        public_repo.add_sample(conn, "raspberry-office", NOW - timedelta(days=10), "unknown")
    with transaction(conn):
        assert public_repo.purge_samples(conn, NOW) == 1
    assert public_repo.samples_since(conn, NOW - timedelta(days=7)) == [(NOW, "degraded")]
    assert len(public_repo.samples_since(conn, NOW - timedelta(days=30))) == 3


def test_public_tables_never_hold_the_full_ip(conn):
    with transaction(conn):
        public_repo.add_renewal(
            conn, "raspberry-home", "e1", "home.example.com", NOW, ip_suffix(CANARY)
        )
        public_repo.save_agent_status(conn, projection())
        public_repo.add_sample(conn, "raspberry-home", NOW, "healthy")
    dump = "\n".join(
        str(row)
        for table in ("public_renewals", "public_agent_status", "public_status_samples")
        for row in conn.execute(f"SELECT * FROM {table}")
    )
    assert CANARY not in dump and "83.45.12" not in dump
    assert "'34'" in dump
