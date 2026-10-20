import re
import uuid

import pytest
from fastapi.testclient import TestClient

from dyndnsarr_hub.auth import sessions
from dyndnsarr_hub.clock import utc_now
from dyndnsarr_hub.config import Settings
from dyndnsarr_hub.db import agents_repo, events_repo
from dyndnsarr_hub.main import create_app
from dyndnsarr_hub.services.alert_config import AlertConfig
from dyndnsarr_shared.events import AgentEvent


@pytest.fixture
def web(tmp_path):
    app = create_app(
        Settings(
            data_dir=tmp_path,
            admin_origin="https://hub.test",
            homarr_origin="https://homarr.test",
            application_url="https://status.example.com",
        )
    )
    with TestClient(app, base_url="https://hub.test", follow_redirects=False) as client:
        yield client, app


def login(client, app):
    client.portal.call(
        lambda: sessions.set_password(
            app.state.db, "admin", "a synthetic long password", utc_now(), create=True
        )
    )
    csrf = re.search(r'name="csrf" value="([^"]+)"', client.get("/admin/login").text)[1]
    assert (
        client.post(
            "/admin/login",
            data={"username": "admin", "password": "a synthetic long password", "csrf": csrf},
            headers={"Origin": "https://hub.test"},
        ).status_code
        == 303
    )


@pytest.mark.parametrize(
    "path",
    ["/admin/integrations", "/admin/alerts", "/admin/api/v1/devices", "/admin/api/v1/export"],
)
def test_new_routes_require_session_before_reading_data(web, path):
    client, _ = web
    response = client.get(path)
    assert response.status_code == (401 if "/api/" in path else 303)
    assert response.headers["Cache-Control"] == "no-store"


def test_alert_test_csrf_idempotence_and_export_whitelist(web):
    client, app = web
    login(client, app)
    app.state.alert_config = AlertConfig.model_validate({"webhook": {}})
    page = client.get("/admin/alerts")
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
    key = re.search(r'name="operation_id" value="([^"]+)"', page.text)[1]
    data = {"operation_id": key, "csrf": csrf}
    url = "/admin/integrations/test-alert"
    assert client.post(url, data=data, headers={"Origin": "https://evil.test"}).status_code == 403
    assert client.post(url, data=data, headers={"Origin": "https://hub.test"}).status_code == 303
    assert client.post(url, data=data, headers={"Origin": "https://hub.test"}).status_code == 303
    assert (
        client.portal.call(
            lambda: app.state.db.execute("SELECT count(*) FROM alert_deliveries").fetchone()[0]
        )
        == 1
    )

    def seed():
        agents_repo.create_agent(app.state.db, "home", "canary-token", utc_now())
        events_repo.insert_events(
            app.state.db,
            [
                AgentEvent(
                    agent_id="home",
                    event_id=str(uuid.uuid4()),
                    sequence=1,
                    type="ip_changed",
                    occurred_at="2026-10-20T12:00:00Z",
                    payload={"password": "secret-canary", "ip": "83.45.12.134"},
                )
            ],
            utc_now(),
        )

    client.portal.call(seed)
    export = client.get("/admin/api/v1/export")
    assert export.status_code == 200 and len(export.json()["events"]) == 1
    assert "secret-canary" not in export.text and "83.45.12.134" not in export.text
    assert "attachment" in export.headers["Content-Disposition"]
    assert client.get("/admin/api/v1/export?limit=101").status_code == 400


def test_homarr_csp_summary_public_switch_and_no_admin_embedding(web):
    client, app = web
    widget = client.get("/public/homarr")
    assert "frame-ancestors https://homarr.test" in widget.headers["Content-Security-Policy"]
    body = client.get("/public/v1/homarr").json()
    assert body == dict(
        state="unknown",
        last_successful_check_at=None,
        last_ip_change_at=None,
        application_url="https://status.example.com",
    )
    assert "frame-ancestors 'none'" in client.get("/admin/login").headers["Content-Security-Policy"]
    disabled = create_app(
        Settings(
            data_dir=app.state.settings.data_dir / "disabled",
            public_enabled=False,
            admin_origin="https://hub.test",
        )
    )
    with TestClient(disabled) as other:
        assert other.get("/public/homarr").status_code == 404
        assert other.get("/public/v1/homarr").status_code == 404
        assert other.get("/admin/login").status_code == 200
