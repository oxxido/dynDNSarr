"""Setup web del primer administrador con token de un solo uso (slice 8, D5)."""

import logging
import re
import sqlite3
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from dyndnsarr_hub.auth import sessions
from dyndnsarr_hub.clock import iso, utc_now
from dyndnsarr_hub.config import Settings
from dyndnsarr_hub.db import MIGRATIONS
from dyndnsarr_hub.main import create_app
from dyndnsarr_shared.db import connect, migrate

ORIGIN = "https://hub.test"
PASSWORD = "una contraseña de prueba larga"


def start(tmp_path, caplog, origin=ORIGIN):
    """Arranca el Hub y devuelve el cliente y los códigos emitidos en el log."""
    caplog.clear()
    caplog.set_level(logging.INFO)
    app = create_app(Settings(data_dir=tmp_path, admin_origin=origin))
    client = TestClient(app, base_url=ORIGIN, follow_redirects=False)
    client.__enter__()
    codes = [r.setup_code for r in caplog.records if r.message == "admin_setup_available"]
    return client, codes


def form(client, path="/admin/setup"):
    response = client.get(path)
    return response, (re.search(r'name="csrf" value="([^"]+)"', response.text) or [None, None])[1]


def submit(client, code, password2=PASSWORD, username="admin"):
    _, csrf = form(client)
    return client.post(
        "/admin/setup",
        data={
            "csrf": csrf,
            "setup_code": code,
            "username": username,
            "password": PASSWORD,
            "password2": password2,
        },
        headers={"Origin": ORIGIN},
    )


def users(tmp_path):
    conn = sqlite3.connect(tmp_path / "hub.db")
    try:
        return conn.execute("SELECT username FROM users").fetchall()
    finally:
        conn.close()


def test_token_is_logged_once_per_boot_and_creates_a_single_admin(tmp_path, caplog):
    client, codes = start(tmp_path, caplog)
    assert len(codes) == 1
    client.__exit__(None, None, None)
    client, second = start(tmp_path, caplog)
    assert len(second) == 1 and second != codes
    try:
        # El token del arranque anterior ya no vale: cada arranque emite uno nuevo.
        assert submit(client, codes[0]).status_code == 401
        assert submit(client, second[0], password2="otra contraseña larga").status_code == 400
        response = submit(client, second[0])
        assert response.status_code == 303 and response.headers["location"] == "/admin/login"
        assert users(tmp_path) == [("admin",)]
        assert client.get("/admin/setup").status_code == 404
        # Sin formulario no hay CSRF válido; en cualquier caso el token ya está consumido.
        assert submit(client, second[0]).status_code in (403, 404)
        assert users(tmp_path) == [("admin",)]
        login, csrf = form(client, "/admin/login")
        logged = client.post(
            "/admin/login",
            data={"csrf": csrf, "username": "admin", "password": PASSWORD},
            headers={"Origin": ORIGIN},
        )
        assert logged.status_code == 303
    finally:
        client.__exit__(None, None, None)
    client, after = start(tmp_path, caplog)
    client.__exit__(None, None, None)
    assert after == []


def test_expired_token_is_rejected(tmp_path, caplog):
    client, codes = start(tmp_path, caplog)
    try:
        conn = sqlite3.connect(tmp_path / "hub.db")
        conn.execute("UPDATE admin_setup SET expires_at = ?", (iso(utc_now() - timedelta(1)),))
        conn.commit()
        conn.close()
        assert submit(client, codes[0]).status_code == 401
        assert users(tmp_path) == []
    finally:
        client.__exit__(None, None, None)


def test_setup_requires_csrf_and_origin_and_counts_for_the_global_block(tmp_path, caplog):
    client, codes = start(tmp_path, caplog)
    try:
        _, csrf = form(client)
        data = {"setup_code": codes[0], "username": "admin"}
        data |= {"password": PASSWORD, "password2": PASSWORD}
        no_csrf = client.post("/admin/setup", data=data, headers={"Origin": ORIGIN})
        assert no_csrf.status_code == 403
        wrong_origin = client.post(
            "/admin/setup", data={**data, "csrf": csrf}, headers={"Origin": "https://evil.test"}
        )
        assert wrong_origin.status_code == 403
        for _ in range(5):
            assert submit(client, "wrong").status_code == 401
        blocked = submit(client, codes[0])
        assert blocked.status_code == 429 and "retry-after" in blocked.headers
        assert users(tmp_path) == []
    finally:
        client.__exit__(None, None, None)


def test_no_token_without_origin_or_with_admin(tmp_path, caplog):
    client, codes = start(tmp_path, caplog, origin=None)
    try:
        assert codes == []
        assert client.get("/admin/setup").status_code == 503
    finally:
        client.__exit__(None, None, None)
    conn = connect(tmp_path / "hub.db")
    migrate(conn, MIGRATIONS)
    sessions.set_password(conn, "admin", PASSWORD, datetime.now(UTC), create=True)
    conn.close()
    client, codes = start(tmp_path, caplog)
    try:
        assert codes == []
        assert client.get("/admin/setup").status_code == 404
    finally:
        client.__exit__(None, None, None)


def test_login_points_to_setup_while_there_is_no_admin(tmp_path, caplog):
    client, _ = start(tmp_path, caplog)
    try:
        page = client.get("/admin/login").text
        assert 'href="/admin/setup"' in page and "aprovisionamiento" in page
    finally:
        client.__exit__(None, None, None)


def test_setup_code_survives_log_redaction():
    from dyndnsarr_shared.logging import RedactingFilter

    record = logging.makeLogRecord({"msg": "admin_setup_available", "setup_code": "visible"})
    RedactingFilter().filter(record)
    assert record.setup_code == "visible"
