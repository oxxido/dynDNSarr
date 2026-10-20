"""Cada superficie privada exige sesión y CSRF, incluso con la landing apagada."""

import re
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from dyndnsarr_hub.auth import sessions
from dyndnsarr_hub.clock import utc_now
from dyndnsarr_hub.config import Settings
from dyndnsarr_hub.db import MIGRATIONS
from dyndnsarr_hub.main import create_app
from dyndnsarr_shared.db import connect, migrate

PASSWORD = "una contraseña de prueba larga"
ORIGIN = "https://hub.test"


@contextmanager
def hub(tmp_path, public=True, admin=True):
    settings = Settings(data_dir=tmp_path, public_enabled=public, admin_origin=ORIGIN)
    db = connect(settings.db_path)
    migrate(db, MIGRATIONS)
    if admin:
        sessions.set_password(db, "admin", PASSWORD, utc_now(), create=True)
    db.close()
    app = create_app(settings)
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as client:
        yield client, app


def csrf(client):
    response = client.get("/admin/login")
    return re.search(r'name="csrf" value="([^"]+)"', response.text)[1]


def login(client):
    return client.post(
        "/admin/login",
        data={"username": "admin", "password": PASSWORD, "csrf": csrf(client)},
        headers={"Origin": ORIGIN},
    )


@pytest.mark.parametrize(
    "path",
    [
        "/admin/",
        "/admin/records",
        "/admin/history",
        "/admin/diagnostics",
        "/admin/config",
        "/admin/missing",
        "/admin/commands/missing",
    ],
)
def test_private_pages_require_login_before_resource_lookup(tmp_path, path):
    with hub(tmp_path) as (client, _):
        response = client.get(path)
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/login"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "same-origin"


def test_login_rotates_cookie_and_logout_revokes_server_session(tmp_path):
    with hub(tmp_path, public=False) as (client, app):
        token = csrf(client)
        old = client.cookies.get(sessions.COOKIE)
        response = client.post(
            "/admin/login",
            data={"csrf": token, "username": "admin", "password": PASSWORD},
            headers={"Origin": ORIGIN},
        )
        assert response.status_code == 303
        cookie = response.headers["set-cookie"]
        assert all(value in cookie for value in ["HttpOnly", "Secure", "SameSite=lax", "Path=/"])
        assert "Domain=" not in cookie
        raw = client.cookies.get(sessions.COOKIE)
        assert old != raw
        assert client.get("/admin/").status_code == 200
        assert client.get("/").status_code == 404
        assert client.get("/static/assets/dyno-green.png").status_code == 404
        assert client.get("/admin/static/assets/dyno-green.png").status_code == 200
        result = client.post(
            "/admin/logout", data={"csrf": sessions.csrf(raw)}, headers={"Origin": ORIGIN}
        )
        assert result.status_code == 303
        client.cookies.set(sessions.COOKIE, raw)
        assert client.get("/admin/api/v1/status").status_code == 401
        assert PASSWORD not in str(
            client.portal.call(lambda: app.state.db.execute("SELECT * FROM audit_log").fetchall())
        )


def test_csrf_origin_limits_and_service_identity(tmp_path):
    with hub(tmp_path) as (client, app):
        token = csrf(client)
        assert (
            client.post(
                "/admin/login", data={"csrf": token}, headers={"Origin": "https://evil.test"}
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/admin/login",
                data={"username": "admin", "password": PASSWORD},
                headers={"Origin": ORIGIN},
            ).status_code
            == 403
        )
        assert (
            client.get(
                "/admin/api/v1/status", headers={"Authorization": "Bearer a-service-token"}
            ).status_code
            == 401
        )
        assert login(client).status_code == 303
        assert client.post("/admin/api/v1/commands", json={}).status_code == 403
        assert (
            client.post(
                "/admin/api/v1/commands", content=b"x" * 270000, headers={"Origin": ORIGIN}
            ).status_code
            == 413
        )
        assert (
            client.portal.call(
                lambda: app.state.db.execute("SELECT count(*) FROM commands").fetchone()[0]
            )
            == 0
        )
        for path in ["/docs", "/redoc", "/openapi.json"]:
            assert client.get(path).status_code == 404


def test_persistent_block_and_no_bootstrap_web(tmp_path):
    with hub(tmp_path) as (client, _):
        token = csrf(client)
        data = {"csrf": token, "username": "admin", "password": "incorrecta"}
        for _ in range(5):
            assert (
                client.post("/admin/login", data=data, headers={"Origin": ORIGIN}).status_code
                == 401
            )
        response = client.post("/admin/login", data=data, headers={"Origin": ORIGIN})
        assert response.status_code == 429 and int(response.headers["retry-after"]) > 0
    with hub(tmp_path / "empty", admin=False) as (client, _):
        assert "aprovisionamiento" in client.get("/admin/login").text
        assert client.post("/admin/register").status_code == 303


def test_csrf_null_origin_is_rejected_even_with_valid_token(tmp_path):
    with hub(tmp_path) as (client, _app):
        token = csrf(client)
        response = client.post(
            "/admin/login",
            data={"csrf": token, "username": "admin", "password": PASSWORD},
            headers={"Origin": "null", "Referer": ORIGIN + "/admin/login"},
        )
        assert response.status_code == 403


def test_invalid_admin_origin_and_disabled_login(tmp_path):
    from pydantic import ValidationError

    for value in [
        "http://hub.test",
        "https://user:pass@hub.test",
        "https://hub.test/admin",
        "https://hub.test?secret=1",
    ]:
        with pytest.raises(ValidationError):
            Settings(admin_origin=value)
    assert Settings(admin_origin="").admin_origin is None
    app = create_app(Settings(data_dir=tmp_path))
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/admin/login").status_code == 503
        assert client.get("/").status_code == 200


def test_unexpected_private_error_is_sanitized_and_never_cacheable(tmp_path, caplog):
    async def boom():
        raise RuntimeError("private-canary-password")

    with hub(tmp_path) as (client, app):
        app.add_api_route("/admin/fail", boom)
        assert login(client).status_code == 303
        response = client.get("/admin/fail")
        assert response.status_code == 500
        assert response.headers["cache-control"] == "no-store"
        assert "nonce-" in response.headers["content-security-policy"]
        assert "private-canary-password" not in response.text + caplog.text
