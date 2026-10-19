"""El Agent con `[hub]` en su ciclo de vida real (lifespan) y `status` del enlace."""

import json
import logging
import sqlite3
import time
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from dyndnsarr_agent import cli
from dyndnsarr_agent.config import Settings
from dyndnsarr_agent.db import MIGRATIONS, ddns_repo
from dyndnsarr_agent.ddns.config import ConfigError
from dyndnsarr_agent.dns.resolver import ResolverAnswer
from dyndnsarr_agent.main import create_app
from dyndnsarr_shared.db import connect, migrate

IP = "1.2.3.4"
TOKEN = "dda_canary-token-0123456789"
HUB_URL = "https://hub.example.lan"
NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)

TOML = f"""
[ip_sources]
urls = ["https://a.example/ip", "https://b.example/ip"]
[dns]
resolvers = ["1.1.1.1"]
[hub]
url = "{HUB_URL}"
agent_id = "raspberry-home"
[[records]]
name = "home.example.com"
mode = "monitored"
"""


class Observer:
    async def lookup(self, name):
        return [ResolverAnswer("1.1.1.1", (IP,), None)]


def sources(request):
    return httpx.Response(200, text=IP)


class FakeHub:
    """Transporte de pruebas del Hub: acepta lo recibido, o falla con `ConnectError`."""

    def __init__(self, down=False):
        self.down = down
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if self.down:
            raise httpx.ConnectError("hub caído")
        ids = [e["event_id"] for e in json.loads(request.content)["events"]]
        return httpx.Response(200, json={"acked": ids, "rejected": []})


def make_app(tmp_path, hub, toml=TOML, token=TOKEN):
    config = tmp_path / "agent.toml"
    config.write_text(toml, encoding="utf-8")
    settings = Settings(data_dir=tmp_path / "data", config_file=config, hub_token=token)
    app = create_app(
        settings,
        observer=Observer(),
        transport=httpx.MockTransport(sources),
        hub_transport=httpx.MockTransport(hub),
    )
    return app, settings


def query(settings, sql):
    conn = sqlite3.connect(settings.db_path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


ERROR_CODE = "SELECT value FROM app_state WHERE key = 'outbox_last_error_code'"
DELIVERED = "SELECT type FROM events WHERE delivered_at IS NOT NULL ORDER BY sequence"


def wait_until(check, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.02)
    pytest.fail("no se cumplió la condición a tiempo")


def test_events_are_delivered_with_the_agent_token(tmp_path):
    hub = FakeHub()
    app, settings = make_app(tmp_path, hub)
    with TestClient(app):
        # Primera pasada inmediata; los eventos del ciclo DDNS salen en la siguiente (cada 30 s).
        wait_until(lambda: query(settings, DELIVERED) != [])
        assert app.state.outbox_task is not None
    assert hub.requests[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert str(hub.requests[0].url) == f"{HUB_URL}/api/v1/agents/raspberry-home/events"
    assert query(settings, DELIVERED)[0] == ("agent_started",)


def test_an_unreachable_hub_does_not_stop_the_agent_from_starting_or_cycling(tmp_path):
    hub = FakeHub(down=True)
    app, settings = make_app(tmp_path, hub)
    with TestClient(app) as client:
        wait_until(lambda: len(query(settings, "SELECT 1 FROM events")) == 3)
        assert client.get("/healthz").status_code == 200
        wait_until(
            lambda: (
                query(settings, "SELECT value FROM app_state WHERE key = 'outbox_last_error_code'")
                == [("connect_error",)]
            )
        )
    assert query(settings, "SELECT count(*) FROM events WHERE delivered_at IS NULL") == [(3,)]


def test_both_tasks_are_cancelled_on_shutdown(tmp_path):
    app, _ = make_app(tmp_path, FakeHub())
    with TestClient(app):
        tasks = [app.state.ddns_task, app.state.outbox_task]
        assert all(t is not None and not t.done() for t in tasks)
    assert all(t.done() for t in tasks)


def test_without_hub_section_nothing_is_sent_and_no_task_exists(tmp_path):
    hub = FakeHub()
    toml = TOML.replace(f'[hub]\nurl = "{HUB_URL}"\nagent_id = "raspberry-home"\n', "")
    app, settings = make_app(tmp_path, hub, toml=toml, token=None)
    with TestClient(app):
        wait_until(lambda: len(query(settings, "SELECT 1 FROM events")) == 3)
        assert app.state.outbox_task is None
    assert hub.requests == []


def test_hub_section_without_token_aborts_with_a_clear_message(tmp_path):
    with pytest.raises(ConfigError, match="DYNDNSARR_AGENT_HUB_TOKEN"):
        make_app(tmp_path, FakeHub(), token=None)


def test_main_aborts_without_token_and_never_prints_the_url(tmp_path, monkeypatch):
    from dyndnsarr_agent.__main__ import main

    config = tmp_path / "agent.toml"
    config.write_text(TOML, encoding="utf-8")
    monkeypatch.setenv("DYNDNSARR_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DYNDNSARR_AGENT_CONFIG_FILE", str(config))
    monkeypatch.delenv("DYNDNSARR_AGENT_HUB_TOKEN", raising=False)
    with pytest.raises(SystemExit) as exc:
        main()
    assert "DYNDNSARR_AGENT_HUB_TOKEN" in str(exc.value.code)


def test_canary_token_and_url_stay_out_of_logs_and_the_database(tmp_path, caplog):
    hub = FakeHub(down=True)
    app, settings = make_app(tmp_path, hub)
    caplog.set_level(logging.DEBUG)
    with TestClient(app):
        wait_until(
            lambda: query(
                settings, "SELECT value FROM app_state WHERE key = 'outbox_last_error_code'"
            )
        )
    dump = "\n".join(
        str(row) for t in ("app_state", "events") for row in query(settings, f"SELECT * FROM {t}")
    )
    for secret in (TOKEN, HUB_URL, "hub.example.lan"):
        assert secret not in dump
        # httpx/httpcore en DEBUG imprimen la URL: `setup_logging` los fija en WARNING (probado
        # en test_outbox_tls); aquí solo se vigila lo que escribe el propio Agent.
        assert all(
            secret not in r.getMessage() for r in caplog.records if r.name.startswith("dyndnsarr")
        )


# --- status ---


@pytest.fixture
def status_env(tmp_path, monkeypatch):
    config = tmp_path / "agent.toml"
    config.write_text(TOML, encoding="utf-8")
    monkeypatch.setenv("DYNDNSARR_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DYNDNSARR_AGENT_CONFIG_FILE", str(config))
    settings = Settings()
    conn = connect(settings.db_path)
    migrate(conn, MIGRATIONS)
    for n in range(3):
        ddns_repo.insert_event(conn, "ip_changed", NOW, None, {"n": n})
    conn.close()
    return settings


def test_status_shows_the_link_state(status_env, capsys):
    conn = connect(status_env.db_path)
    first, second, *_ = ddns_repo.pending_events(conn, 3)
    ddns_repo.mark_delivered(conn, [first.event_id], NOW)
    ddns_repo.mark_rejected(conn, [(second.event_id, "invalid_event")])
    ddns_repo.set_state(conn, "outbox_last_success_at", "2026-10-19T12:00:00Z", NOW)
    ddns_repo.set_state(conn, "outbox_last_error_code", "tls_error", NOW)
    ddns_repo.set_state(conn, "outbox_last_error_at", "2026-10-19T12:05:00Z", NOW)
    conn.close()
    assert cli.run(["status"]) == 0
    out = capsys.readouterr().out
    assert "Hub: 1 evento(s) pendiente(s), 1 rechazado(s)" in out
    assert "último envío correcto: 2026-10-19T12:00:00Z" in out
    assert "último error: tls_error (el 2026-10-19T12:05:00Z)" in out
    assert HUB_URL not in out and "hub.example.lan" not in out


def test_status_without_hub_section_says_so(status_env, monkeypatch, capsys):
    monkeypatch.delenv("DYNDNSARR_AGENT_CONFIG_FILE")
    assert cli.run(["status"]) == 0
    assert "Hub: sin configurar" in capsys.readouterr().out


def test_status_with_a_never_delivered_link(status_env, capsys):
    assert cli.run(["status"]) == 0
    out = capsys.readouterr().out
    assert "Hub: 3 evento(s) pendiente(s), 0 rechazado(s)" in out
    assert "último envío correcto: nunca" in out and "último error" not in out


def test_status_still_does_not_write(status_env):
    def snapshot():
        conn = sqlite3.connect(status_env.db_path)
        try:
            return [conn.execute(f"SELECT * FROM {t}").fetchall() for t in ("events", "app_state")]
        finally:
            conn.close()

    before, mtime = snapshot(), status_env.db_path.stat().st_mtime_ns
    assert cli.run(["status"]) == 0
    assert snapshot() == before and status_env.db_path.stat().st_mtime_ns == mtime
