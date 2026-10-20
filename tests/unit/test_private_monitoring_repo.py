"""Upgrade no destructivo, recuperación y huecos de las observaciones privadas."""

import uuid
from datetime import UTC, datetime, timedelta

from dyndnsarr_hub.auth import sessions
from dyndnsarr_hub.auth.tokens import hash_token
from dyndnsarr_hub.db import (
    MIGRATIONS,
    admin_repo,
    agents_repo,
    control_repo,
    events_repo,
    private_repo,
    public_repo,
)
from dyndnsarr_hub.monitoring import metrics
from dyndnsarr_shared.control import AgentStatus, EffectiveConfig
from dyndnsarr_shared.db import connect, migrate, transaction
from dyndnsarr_shared.events import AgentEvent

NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)


def test_upgrade_from_0004_backup_and_restore_preserve_data_and_revocation(tmp_path):
    db = connect(tmp_path / "hub.db")
    scripts = sorted(f for f in MIGRATIONS.iterdir() if f.name.endswith(".sql"))
    for version, script in enumerate(scripts[:4], 1):
        db.executescript(f"BEGIN;\n{script.read_text()}\nPRAGMA user_version={version}; COMMIT;")
    with transaction(db):
        agents_repo.create_agent(db, "home", hash_token("test"), NOW)
        # Datos del esquema anterior, sin usar repositorios del esquema nuevo.
        db.execute(
            "INSERT INTO events VALUES (?,?,?,?,?,?,?,?)",
            (
                "home",
                str(uuid.uuid4()),
                1,
                "ip_changed",
                "2026-10-19T11:00:00Z",
                "2026-10-19T12:00:00Z",
                "home.example.com",
                '{"ip":"83.45.12.134"}',
            ),
        )
        control_repo.add_desired_version(db, "home", {"records": []}, NOW)
        db.execute(
            "INSERT INTO commands VALUES (?,?,?,?,?,?,?,?)",
            (
                str(uuid.uuid4()),
                "home",
                "check_dns",
                "admin:cli",
                "2026-10-20T12:00:00Z",
                "2026-10-20T12:05:00Z",
                "pending",
                None,
            ),
        )
        public_repo.add_sample(db, "home", NOW, "healthy")
    migrate(db, MIGRATIONS)
    for table in [
        "agents",
        "events",
        "commands",
        "desired_config_versions",
        "public_status_samples",
    ]:
        assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1
    sessions.set_password(db, "admin", "una contraseña suficientemente larga", NOW, create=True)
    raw, _ = sessions.create(db, NOW, user_id=1)
    private_repo.update_policy(db, 1200, 3000, 4)
    restored = connect(tmp_path / "restore.db")
    db.backup(restored)
    db.close()
    assert sessions.get(restored, raw, NOW + timedelta(minutes=1))
    assert public_repo.monitoring_policy(restored).observation_max_seconds == 1200
    sessions.set_password(restored, "admin", "otra contraseña suficientemente larga", NOW)
    restored.close()
    restored = connect(tmp_path / "restore.db")
    assert sessions.get(restored, raw, NOW) is None
    restored.close()


def test_history_pagination_filters_and_payload_whitelist(tmp_path):
    db = connect(tmp_path / "hub.db")
    migrate(db, MIGRATIONS)
    agents_repo.create_agent(db, "home", hash_token("test"), NOW)
    for sequence in range(1, 4):
        event = AgentEvent(
            agent_id="home",
            event_id=str(uuid.uuid4()),
            sequence=sequence,
            type="dns_verified",
            record_name="home.example.com",
            occurred_at="2026-10-20T12:00:00Z",
            payload={"password": "secret-canary", "code": "secret-canary"},
        )
        events_repo.insert_events(db, [event, event], NOW)
    one = admin_repo.history(db, agent="home", record="home.example.com", limit=2)
    two = admin_repo.history(
        db, agent="home", record="home.example.com", limit=2, cursor=one["next_cursor"]
    )
    assert len(one["entries"]) == 2 and len(two["entries"]) == 1
    assert "secret-canary" not in str(one) + str(two)
    assert len({e["event_id"] for e in one["entries"] + two["entries"]}) == 3
    assert admin_repo.history(db, event_type="ip_changed")["entries"] == []
    assert admin_repo.history(db, since="2026-10-21T00:00:00Z")["entries"] == []
    db.close()


def test_repeated_snapshot_and_gap_do_not_recreate_healthy_time(tmp_path):
    from dyndnsarr_hub.services.status_refresher import project

    db = connect(tmp_path / "hub.db")
    migrate(db, MIGRATIONS)
    agents_repo.create_agent(db, "home", hash_token("test"), NOW)
    status = AgentStatus(
        agent_id="home",
        version="0.1",
        time_utc="2026-10-20T12:00:00Z",
        config_version=0,
        config_hash=None,
        ip_status="confirmed",
        public_ip="83.45.12.134",
        public_ip_confirmed_at="2026-10-20T12:00:00Z",
        last_check_at="2026-10-20T12:00:00Z",
        records=[
            dict(
                name="home.example.com",
                mode="monitored",
                verification="verified",
                since="2026-10-20T12:00:00Z",
                failures=0,
                next_attempt_at=None,
                last_error_code=None,
            )
        ],
        hub=None,
    )
    config = EffectiveConfig(version=0, hash=None, content={"check_interval_seconds": 300})
    private_repo.save_snapshot(db, "home", "config", config, None, NOW)
    private_repo.add_state_samples(db, project("home", status, NOW), NOW, 60)
    private_repo.mark_gap(db, "home", NOW + timedelta(seconds=30))
    private_repo.add_state_samples(db, project("home", status, NOW), NOW, 60)
    result = metrics(private_repo.samples(db, "home", "", NOW), NOW + timedelta(hours=1), 24)
    assert result["seconds"]["healthy"] == 30
    assert db.execute("SELECT count(*) FROM private_samples WHERE resource = ''").fetchone()[0] == 2
    private_repo.purge(db, NOW + timedelta(days=31))
    assert private_repo.samples(db, "home", "", NOW) == []
    db.close()


def test_reused_agent_observation_ids_are_distinct_by_timestamp(tmp_path):
    from dyndnsarr_shared.control import DiagnosticPage

    db = connect(tmp_path / "hub.db")
    migrate(db, MIGRATIONS)
    agents_repo.create_agent(db, "home", hash_token("test"), NOW)
    for at in ["2026-10-19T12:00:00Z", "2026-10-20T12:00:00Z"]:
        page = DiagnosticPage(
            observations=[
                dict(
                    id=1,
                    kind="dns",
                    observed_at=at,
                    record_name="home.example.com",
                    source="1.1.1.1",
                    ips=["83.45.12.134"],
                )
            ]
        )
        private_repo.save_snapshot(db, "home", "diagnostics", page, None, NOW)
        private_repo.save_snapshot(db, "home", "diagnostics", page, None, NOW)
    assert db.execute("SELECT count(*) FROM private_observations").fetchone()[0] == 2
    db.close()
