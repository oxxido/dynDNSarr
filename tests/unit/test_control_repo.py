from datetime import UTC, datetime, timedelta

import pytest

from dyndnsarr_agent.db import MIGRATIONS, control_repo, ddns_repo
from dyndnsarr_shared.db import connect, migrate

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
ID = "6f9619ff-8b86-4011-b42d-00c04fc964ff"


@pytest.fixture
def conn(tmp_path):
    conn = connect(tmp_path / "agent.db")
    migrate(conn, MIGRATIONS)
    return conn


def test_migration_0005_applies_over_a_0004_database_with_data(tmp_path):
    old = connect(tmp_path / "agent.db")
    scripts = sorted(f for f in MIGRATIONS.iterdir() if f.name.endswith(".sql"))
    for version, script in enumerate(scripts[:4], start=1):
        sql = script.read_text(encoding="utf-8")
        old.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")
    ddns_repo.insert_event(old, "agent_started", NOW, None, {"version": "x"})
    ddns_repo.set_state(old, "public_ip", "1.2.3.4", NOW)
    assert migrate(old, MIGRATIONS) == 7
    tables = {r[0] for r in old.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"configuration_versions", "commands"} <= tables
    assert ddns_repo.get_state(old, "public_ip") == "1.2.3.4"
    assert ddns_repo.count_pending(old) == 1


def test_versions_persist_between_connections_and_latest_wins(tmp_path):
    path = tmp_path / "agent.db"
    first = connect(path)
    migrate(first, MIGRATIONS)
    assert control_repo.latest_version(first) is None
    control_repo.insert_version(first, 1, "a" * 64, '{"x":1}', NOW)
    control_repo.insert_version(first, 3, "b" * 64, '{"x":3}', NOW)
    first.close()
    latest = control_repo.latest_version(connect(path))
    assert (latest.version, latest.content_hash, latest.content) == (3, "b" * 64, {"x": 3})
    assert latest.applied_at == "2026-10-19T12:00:00Z"


def test_a_version_number_cannot_be_inserted_twice(conn):
    control_repo.insert_version(conn, 1, "a" * 64, "{}", NOW)
    with pytest.raises(Exception, match="UNIQUE"):
        control_repo.insert_version(conn, 1, "b" * 64, "{}", NOW)


def test_delete_versions_returns_to_the_toml(conn):
    control_repo.insert_version(conn, 1, "a" * 64, "{}", NOW)
    control_repo.insert_version(conn, 2, "b" * 64, "{}", NOW)
    assert control_repo.delete_versions(conn) == 2
    assert control_repo.latest_version(conn) is None


def test_commands_persist_between_connections(tmp_path):
    path = tmp_path / "agent.db"
    first = connect(path)
    migrate(first, MIGRATIONS)
    control_repo.insert_command(first, ID, "check_dns", "admin:1", NOW, "2026-10-19T12:05:00Z")
    control_repo.set_command_running(first, ID)
    first.close()
    second = connect(path)
    command = control_repo.get_command(second, ID)
    assert (command.type, command.status, command.finished_at) == ("check_dns", "running", None)
    control_repo.finish_command(second, ID, "succeeded", NOW, {"records": {"a": "verified"}})
    done = control_repo.get_command(second, ID)
    assert (done.status, done.finished_at, done.result) == (
        "succeeded",
        "2026-10-19T12:00:00Z",
        {"records": {"a": "verified"}},
    )
    assert control_repo.get_command(second, "otro") is None


def test_unfinished_commands_are_received_and_running_only(conn):
    ids = [f"6f9619ff-8b86-4011-b42d-00c04fc964f{i}" for i in range(4)]
    for i, command_id in enumerate(ids):
        control_repo.insert_command(
            conn,
            command_id,
            "sync_ddns",
            "admin:1",
            NOW + timedelta(seconds=i),
            "2026-10-19T13:00:00Z",
        )
    control_repo.set_command_running(conn, ids[1])
    control_repo.finish_command(conn, ids[2], "succeeded", NOW, {})
    control_repo.finish_command(conn, ids[3], "failed", NOW, {"code": "busy"})
    assert [c.command_id for c in control_repo.unfinished_commands(conn)] == ids[:2]


def test_retention_purge_removes_old_commands(conn):
    old = NOW - timedelta(days=400)
    control_repo.insert_command(conn, ID, "check_dns", "admin:1", old, "2026-01-01T00:00:00Z")
    control_repo.insert_command(
        conn, ID[:-1] + "0", "check_dns", "admin:1", NOW, "2026-10-19T12:05:00Z"
    )
    deleted = ddns_repo.purge(conn, NOW, samples_days=30, events_days=365)
    assert deleted["commands"] == 1
    assert control_repo.get_command(conn, ID) is None
