import asyncio
import logging
import sqlite3
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from dyndnsarr_agent.config import Settings
from dyndnsarr_agent.db import MIGRATIONS
from dyndnsarr_agent.ddns.config import ConfigError, DdnsConfig
from dyndnsarr_agent.dns.resolver import ResolverAnswer
from dyndnsarr_agent.main import create_app
from dyndnsarr_agent.scheduler import run_forever
from dyndnsarr_shared.db import connect, migrate

IP = "1.2.3.4"
NAME = "home.example.com"

TOML = """
[ip_sources]
urls = ["https://a.example/ip", "https://b.example/ip"]
[dns]
resolvers = ["1.1.1.1"]
[[records]]
name = "home.example.com"
mode = "monitored"
"""


class Observer:
    def __init__(self, fail_first=0):
        self.calls = 0
        self.fail_first = fail_first

    async def lookup(self, name):
        self.calls += 1
        if self.calls <= self.fail_first:
            raise RuntimeError("secret-url-with-key")
        return [ResolverAnswer("1.1.1.1", (IP,), None)]


def sources(request):
    return httpx.Response(200, text=IP)


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "agent.toml"
    path.write_text(TOML, encoding="utf-8")
    return path


def make_app(tmp_path, config_file=None, observer=None):
    settings = Settings(data_dir=tmp_path / "data", config_file=config_file)
    return create_app(settings, observer=observer, transport=httpx.MockTransport(sources)), settings


def query(settings, sql):
    conn = sqlite3.connect(settings.db_path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def wait_for(settings, sql, expected, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = query(settings, sql)
        if rows == expected:
            return
        time.sleep(0.02)
    pytest.fail(f"{sql!r}: se esperaba {expected}, hay {rows}")


EVENT_TYPES = "SELECT type FROM events ORDER BY sequence"


def test_engine_is_disabled_without_config_file(tmp_path, caplog):
    app, settings = make_app(tmp_path)
    with caplog.at_level(logging.INFO), TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert app.state.ddns_task is None
    assert query(settings, EVENT_TYPES) == []
    assert "ddns_engine_disabled" in [r.getMessage() for r in caplog.records]


def test_startup_runs_an_immediate_check(tmp_path, config_file):
    app, settings = make_app(tmp_path, config_file, Observer())
    with TestClient(app) as client:
        wait_for(
            settings,
            EVENT_TYPES,
            [("agent_started",), ("ip_changed",), ("dns_verified",)],
        )
        assert client.get("/healthz").status_code == 200


def test_restart_keeps_history_and_checks_again_immediately(tmp_path, config_file):
    first_app, settings = make_app(tmp_path, config_file, Observer())
    with TestClient(first_app):
        wait_for(settings, "SELECT count(*) FROM public_ip_observations", [(2,)])
    second_app, _ = make_app(tmp_path, config_file, Observer())
    with TestClient(second_app):
        wait_for(settings, "SELECT count(*) FROM public_ip_observations", [(4,)])
    # historial intacto; la IP no cambió, así que solo se añade un nuevo arranque
    assert query(settings, EVENT_TYPES) == [
        ("agent_started",),
        ("ip_changed",),
        ("dns_verified",),
        ("agent_started",),
    ]


def test_a_failing_cycle_does_not_take_the_server_down(tmp_path, config_file, caplog):
    app, settings = make_app(tmp_path, config_file, Observer(fail_first=1))
    with caplog.at_level(logging.ERROR), TestClient(app) as client:
        deadline = time.monotonic() + 5
        while not any(r.getMessage() == "cycle_failed" for r in caplog.records):
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert client.get("/healthz").status_code == 200
    [record] = [r for r in caplog.records if r.getMessage() == "cycle_failed"]
    assert record.error_type == "RuntimeError"
    assert "secret-url-with-key" not in caplog.text


def test_scheduler_task_is_cancelled_cleanly_on_shutdown(tmp_path, config_file):
    app, settings = make_app(tmp_path, config_file, Observer())
    with TestClient(app):
        task = app.state.ddns_task
        assert task is not None and not task.done()
    assert task.cancelled() or task.done()


def test_missing_config_file_aborts_app_creation(tmp_path):
    with pytest.raises(ConfigError, match="no existe"):
        make_app(tmp_path, tmp_path / "missing.toml")


def test_main_aborts_with_clear_message_on_managed_record(tmp_path, monkeypatch):
    from dyndnsarr_agent.__main__ import main

    path = tmp_path / "agent.toml"
    path.write_text(TOML.replace("monitored", "managed"), encoding="utf-8")
    monkeypatch.setenv("DYNDNSARR_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DYNDNSARR_AGENT_CONFIG_FILE", str(path))
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code not in (0, None)
    assert "provider" in str(exc.value.code)


def test_main_aborts_on_managed_record_without_credentials(tmp_path, monkeypatch):
    from dyndnsarr_agent.__main__ import main

    path = tmp_path / "agent.toml"
    path.write_text(
        TOML.replace("monitored", "managed") + 'provider = "dondominio"\n', encoding="utf-8"
    )
    monkeypatch.setenv("DYNDNSARR_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DYNDNSARR_AGENT_CONFIG_FILE", str(path))
    monkeypatch.delenv("DYNDNSARR_AGENT_DONDOMINIO_USER", raising=False)
    monkeypatch.delenv("DYNDNSARR_AGENT_DONDOMINIO_KEY", raising=False)
    with pytest.raises(SystemExit) as exc:
        main()
    assert "DYNDNSARR_AGENT_DONDOMINIO_USER" in str(exc.value.code)
    assert "DYNDNSARR_AGENT_DONDOMINIO_KEY" in str(exc.value.code)


def test_main_aborts_when_config_file_is_missing(tmp_path, monkeypatch):
    from dyndnsarr_agent.__main__ import main

    monkeypatch.setenv("DYNDNSARR_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DYNDNSARR_AGENT_CONFIG_FILE", str(tmp_path / "nope.toml"))
    with pytest.raises(SystemExit) as exc:
        main()
    assert "no existe" in str(exc.value.code)


# --- run_forever, con reloj de sleep controlado ---


class Stop(BaseException):
    pass


def drive(conn, config, observer, cycles):
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= cycles:
            raise Stop

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(sources)) as client:
            await run_forever(conn, config, client, observer, sleep=sleep)

    with pytest.raises(Stop):
        asyncio.run(go())
    return sleeps


def test_run_forever_survives_a_failed_cycle_and_keeps_the_interval(tmp_path):
    conn = connect(tmp_path / "agent.db")
    migrate(conn, MIGRATIONS)
    config = DdnsConfig.model_validate(
        {
            "check_interval_seconds": 90,
            "ip_sources": {"urls": ["https://a.example/ip", "https://b.example/ip"]},
            "dns": {"resolvers": ["1.1.1.1"]},
            "records": [{"name": NAME, "mode": "monitored"}],
        }
    )
    observer = Observer(fail_first=1)
    sleeps = drive(conn, config, observer, cycles=3)
    assert sleeps == [90, 90, 90]
    assert observer.calls == 3
    assert conn.execute("SELECT type FROM events ORDER BY sequence").fetchall() == [
        ("ip_changed",),
        ("dns_verified",),
    ]
