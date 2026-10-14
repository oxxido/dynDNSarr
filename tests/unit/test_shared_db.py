import sqlite3
import stat
from pathlib import Path

import pytest

from dyndnsarr_shared.db import MigrationError, SchemaVersionError, connect, migrate, transaction


def write_migrations(directory: Path, scripts: dict[str, str]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, sql in scripts.items():
        (directory / name).write_text(sql)
    return directory


def user_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def test_connect_configures_wal_foreign_keys_and_busy_timeout(tmp_path):
    conn = connect(tmp_path / "x.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] > 0


def test_connect_creates_file_with_0600_and_missing_parent(tmp_path):
    path = tmp_path / "nested" / "x.db"
    connect(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_connect_keeps_permissions_of_existing_file(tmp_path):
    path = tmp_path / "x.db"
    connect(path).close()
    path.chmod(0o640)
    connect(path).close()
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_migrate_applies_in_order_and_sets_user_version(tmp_path):
    migrations = write_migrations(
        tmp_path / "m",
        {
            "0002_b.sql": "INSERT INTO t VALUES (2);",
            "0001_a.sql": "CREATE TABLE t (n INTEGER);",
        },
    )
    conn = connect(tmp_path / "x.db")
    assert migrate(conn, migrations) == 2
    assert [r[0] for r in conn.execute("SELECT n FROM t")] == [2]
    assert user_version(conn) == 2


def test_migrate_is_idempotent(tmp_path):
    migrations = write_migrations(
        tmp_path / "m", {"0001_a.sql": "CREATE TABLE t (n INTEGER); INSERT INTO t VALUES (1);"}
    )
    conn = connect(tmp_path / "x.db")
    migrate(conn, migrations)
    assert migrate(conn, migrations) == 1
    assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 1


def test_migrate_applies_only_pending_migrations(tmp_path):
    migrations = write_migrations(tmp_path / "m", {"0001_a.sql": "CREATE TABLE t (n INTEGER);"})
    conn = connect(tmp_path / "x.db")
    migrate(conn, migrations)
    write_migrations(migrations, {"0002_b.sql": "INSERT INTO t VALUES (7);"})
    assert migrate(conn, migrations) == 2
    assert conn.execute("SELECT n FROM t").fetchall() == [(7,)]


def test_failed_migration_rolls_back_and_keeps_user_version(tmp_path):
    migrations = write_migrations(
        tmp_path / "m",
        {
            "0001_a.sql": "CREATE TABLE t (n INTEGER);",
            "0002_bad.sql": "CREATE TABLE u (n INTEGER); INSERT INTO t VALUES (1); BOOM;",
        },
    )
    conn = connect(tmp_path / "x.db")
    with pytest.raises(sqlite3.Error):
        migrate(conn, migrations)
    assert user_version(conn) == 1
    assert not conn.in_transaction
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"t"}
    assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 0


def test_database_newer_than_known_migrations_is_rejected(tmp_path):
    migrations = write_migrations(tmp_path / "m", {"0001_a.sql": "CREATE TABLE t (n INTEGER);"})
    conn = connect(tmp_path / "x.db")
    conn.execute("PRAGMA user_version = 5")
    with pytest.raises(SchemaVersionError):
        migrate(conn, migrations)


def test_gap_in_migration_numbers_is_rejected(tmp_path):
    migrations = write_migrations(
        tmp_path / "m",
        {"0001_a.sql": "CREATE TABLE t (n INTEGER);", "0003_c.sql": "SELECT 1;"},
    )
    with pytest.raises(MigrationError):
        migrate(connect(tmp_path / "x.db"), migrations)


def test_unexpected_file_name_is_rejected(tmp_path):
    migrations = write_migrations(tmp_path / "m", {"init.sql": "SELECT 1;"})
    with pytest.raises(MigrationError):
        migrate(connect(tmp_path / "x.db"), migrations)


def test_transaction_commits_and_rolls_back(tmp_path):
    conn = connect(tmp_path / "x.db")
    conn.execute("CREATE TABLE t (n INTEGER)")
    with transaction(conn):
        conn.execute("INSERT INTO t VALUES (1)")
    with pytest.raises(RuntimeError), transaction(conn):
        conn.execute("INSERT INTO t VALUES (2)")
        raise RuntimeError
    assert conn.execute("SELECT n FROM t").fetchall() == [(1,)]
