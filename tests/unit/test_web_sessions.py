"""Sesiones persistentes, límites y recuperación del administrador."""

from datetime import UTC, datetime, timedelta

import pytest

from dyndnsarr_hub.auth import sessions
from dyndnsarr_hub.db import MIGRATIONS
from dyndnsarr_shared.db import connect, migrate

NOW = datetime(2026, 10, 20, tzinfo=UTC)
PASSWORD = "una contraseña de prueba larga"


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "hub.db")
    migrate(conn, MIGRATIONS)
    yield conn
    conn.close()


def test_only_one_admin_password_hash_and_revocation(db):
    sessions.set_password(db, "admin", PASSWORD, NOW, create=True)
    encoded = db.execute("SELECT password_hash FROM users").fetchone()[0]
    assert encoded.startswith("$argon2id$") and PASSWORD not in encoded
    with pytest.raises(ValueError):
        sessions.set_password(db, "other", PASSWORD, NOW, create=True)
    raw, session = sessions.create(db, NOW, user_id=1)
    assert raw not in str(db.execute("SELECT * FROM sessions").fetchall())
    assert sessions.get(db, raw, NOW).user_id == 1
    sessions.set_password(db, "admin", PASSWORD + " nueva", NOW)
    assert sessions.get(db, raw, NOW) is None
    assert sessions.csrf(raw) != sessions.csrf(raw + "otra")
    assert session.user_id == 1


def test_idle_absolute_and_anonymous_expiry(db):
    sessions.set_password(db, "admin", PASSWORD, NOW, create=True)
    raw, _ = sessions.create(db, NOW, user_id=1)
    assert sessions.get(db, raw, NOW + timedelta(minutes=29))
    assert sessions.get(db, raw, NOW + timedelta(minutes=60)) is None
    raw, _ = sessions.create(db, NOW, user_id=1)
    for hour in range(1, 25):
        result = sessions.get(db, raw, NOW + timedelta(minutes=29 * hour))
        assert result is not None
    assert sessions.get(db, raw, NOW + timedelta(hours=12)) is None
    raw, _ = sessions.create(db, NOW)
    assert sessions.get(db, raw, NOW + timedelta(minutes=10)) is None


def test_throttle_is_persistent_and_unknown_user_is_equivalent(db):
    sessions.set_password(db, "admin", PASSWORD, NOW, create=True)
    for username in ["admin", "missing", "admin", "missing", "admin"]:
        assert sessions.authenticate(db, username, "incorrecta", NOW) == (None, 0)
    assert sessions.authenticate(db, "admin", PASSWORD, NOW) == (None, 900)
    assert len(db.execute("SELECT * FROM login_throttle").fetchall()) == 1
    sessions.set_password(db, "admin", PASSWORD, NOW)
    assert sessions.authenticate(db, "admin", PASSWORD, NOW) == (1, 0)
    audit = str(db.execute("SELECT * FROM audit_log").fetchall())
    assert PASSWORD not in audit and "incorrecta" not in audit
