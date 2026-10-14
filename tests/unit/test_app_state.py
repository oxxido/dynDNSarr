import uuid

from dyndnsarr_shared.db import connect, migrate


def open_db(component, tmp_path):
    conn = connect(tmp_path / component.db_filename)
    migrate(conn, component.module("db").MIGRATIONS)
    return conn


def test_first_boot_creates_instance_id_and_counts_one(component, tmp_path):
    conn = open_db(component, tmp_path)
    instance_id, boot_count = component.module("db.state").record_boot(conn)
    assert str(uuid.UUID(instance_id)) == instance_id
    assert boot_count == 1


def test_next_boots_keep_instance_id_and_increment_count(component, tmp_path):
    record_boot = component.module("db.state").record_boot
    conn = open_db(component, tmp_path)
    first_id, _ = record_boot(conn)
    conn.close()
    conn = open_db(component, tmp_path)
    assert record_boot(conn) == (first_id, 2)
    assert record_boot(conn) == (first_id, 3)


def test_migration_0001_creates_app_state_table(component, tmp_path):
    conn = open_db(component, tmp_path)
    columns = [r[1] for r in conn.execute("PRAGMA table_info(app_state)")]
    assert columns == ["key", "value", "updated_at"]
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
