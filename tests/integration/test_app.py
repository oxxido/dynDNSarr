import logging
import sqlite3
import stat

import pytest
from fastapi.testclient import TestClient

from dyndnsarr_shared.db import SchemaVersionError


def read_state(db_path) -> dict[str, str]:
    conn = sqlite3.connect(db_path)
    try:
        return dict(conn.execute("SELECT key, value FROM app_state"))
    finally:
        conn.close()


def test_healthz_reports_ok(component, tmp_path):
    app = component.app(component.settings(tmp_path))
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "component": component.name,
        "version": component.module().__version__,
    }


def test_state_persists_between_instances(component, tmp_path):
    settings = component.settings(tmp_path)
    with TestClient(component.app(settings)):
        first = read_state(settings.db_path)
    with TestClient(component.app(settings)):
        second = read_state(settings.db_path)
    assert first["boot_count"] == "1"
    assert second["boot_count"] == "2"
    assert second["instance_id"] == first["instance_id"]


def test_database_file_is_private(component, tmp_path):
    settings = component.settings(tmp_path)
    with TestClient(component.app(settings)):
        pass
    assert stat.S_IMODE(settings.db_path.stat().st_mode) == 0o600


def test_healthz_returns_503_when_database_fails(component, tmp_path):
    settings = component.settings(tmp_path)
    with TestClient(component.app(settings)) as client:
        other = sqlite3.connect(settings.db_path)
        other.execute("DROP TABLE app_state")
        other.close()
        response = client.get("/healthz")
    assert response.status_code == 503
    assert response.json() == {
        "status": "error",
        "component": component.name,
        "version": component.module().__version__,
    }


def test_startup_refuses_database_from_a_newer_version(component, tmp_path):
    settings = component.settings(tmp_path)
    conn = sqlite3.connect(settings.db_path)
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(SchemaVersionError), TestClient(component.app(settings)):
        pass


def test_startup_log_describes_the_instance_without_secrets(component, tmp_path, caplog):
    settings = component.settings(tmp_path)
    with caplog.at_level(logging.INFO), TestClient(component.app(settings)):
        pass
    [record] = [r for r in caplog.records if r.getMessage() == "startup"]
    assert record.version == component.module().__version__
    assert record.host == settings.host
    assert record.port == settings.port
    assert record.data_dir == str(settings.data_dir)
    migrations = component.module("db").MIGRATIONS
    assert record.schema_version == len(
        [f for f in migrations.iterdir() if f.name.endswith(".sql")]
    )
