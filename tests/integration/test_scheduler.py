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
from dyndnsarr_agent.providers.base import UpdateResult
from dyndnsarr_agent.scheduler import run_forever, try_lock
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


# --- cadencia y bloqueo del slice 2b ---


MANAGED_CONFIG = {
    "check_interval_seconds": 300,
    "update": {"verify_retry_seconds": 60, "verify_timeout_seconds": 900},
    "ip_sources": {"urls": ["https://a.example/ip", "https://b.example/ip"]},
    "dns": {"resolvers": ["1.1.1.1"]},
    "records": [{"name": NAME, "mode": "managed", "provider": "dondominio"}],
}


class StaleObserver:
    """El DNS sigue apuntando a otra IP: tras actualizar queda pendiente de verificación."""

    def __init__(self):
        self.calls = 0

    async def lookup(self, name):
        self.calls += 1
        return [ResolverAnswer("1.1.1.1", ("9.9.9.9",), None)]


class RecordingProvider:
    def __init__(self, result=None):
        self.result = result or UpdateResult(True, changed=True)
        self.calls = []

    async def update(self, host, ip):
        self.calls.append((host, ip))
        return self.result


def drive_managed(tmp_path, provider, cycles, lock_path=None):
    conn = connect(tmp_path / "agent.db")
    migrate(conn, MIGRATIONS)
    sleeps = []

    async def sleep(seconds):
        if seconds == 2.0:  # pausa entre llamadas del ciclo: no es la espera del bucle
            return
        sleeps.append(seconds)
        if len(sleeps) >= cycles:
            raise Stop

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(sources)) as client:
            await run_forever(
                conn,
                DdnsConfig.model_validate(MANAGED_CONFIG),
                client,
                StaleObserver(),
                sleep=sleep,
                providers={"dondominio": provider},
                lock_path=lock_path,
            )

    with pytest.raises(Stop):
        asyncio.run(go())
    return sleeps, conn


def test_pending_verification_wakes_the_loop_in_verify_retry_seconds(tmp_path):
    provider = RecordingProvider()
    sleeps, conn = drive_managed(tmp_path, provider, cycles=3)
    assert sleeps == [60, 60, 60]  # con un intervalo de 300
    assert provider.calls == [(NAME, IP)]  # una sola llamada aunque se verifique varias veces


def test_backoff_wakes_the_loop_when_the_retry_is_due(tmp_path):
    provider = RecordingProvider(UpdateResult(False, error_code="http_error", retryable=True))
    sleeps, _ = drive_managed(tmp_path, provider, cycles=1)
    assert 1 <= sleeps[0] <= 60


def test_definitive_error_leaves_the_regular_interval(tmp_path):
    provider = RecordingProvider(UpdateResult(False, error_code="auth_failed", retryable=False))
    sleeps, _ = drive_managed(tmp_path, provider, cycles=2)
    assert sleeps == [300, 300]
    assert len(provider.calls) == 1


def test_a_cycle_with_the_lock_held_is_skipped_without_stopping_the_loop(tmp_path, caplog):
    lock = tmp_path / "ddns.lock"
    provider = RecordingProvider()
    with caplog.at_level(logging.INFO), try_lock(lock) as taken:
        assert taken is True
        sleeps, conn = drive_managed(tmp_path, provider, cycles=2, lock_path=lock)
    assert sleeps == [300, 300]
    assert provider.calls == []
    assert conn.execute("SELECT count(*) FROM events").fetchone() == (0,)
    assert "cycle_skipped" in [r.getMessage() for r in caplog.records]
    sleeps, _ = drive_managed(tmp_path, provider, cycles=1, lock_path=lock)  # libre: corre
    assert provider.calls == [(NAME, IP)]


def test_try_lock_is_exclusive_and_released_on_exit(tmp_path):
    lock = tmp_path / "ddns.lock"
    with try_lock(lock) as first:
        with try_lock(lock) as second:
            assert (first, second) == (True, False)
        with try_lock(lock) as still_blocked:
            assert still_blocked is False
    with try_lock(lock) as again:
        assert again is True
    assert oct(lock.stat().st_mode & 0o777) == "0o600"


def test_the_lock_is_released_when_the_task_is_cancelled_mid_cycle(tmp_path):
    lock = tmp_path / "ddns.lock"

    class Hanging:
        async def lookup(self, name):
            await asyncio.sleep(30)

    async def go():
        conn = connect(tmp_path / "agent.db")
        migrate(conn, MIGRATIONS)
        async with httpx.AsyncClient(transport=httpx.MockTransport(sources)) as client:
            task = asyncio.create_task(
                run_forever(
                    conn,
                    DdnsConfig.model_validate(MANAGED_CONFIG),
                    client,
                    Hanging(),
                    lock_path=lock,
                )
            )
            await asyncio.sleep(0.2)
            with try_lock(lock) as while_running:
                assert while_running is False
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        with try_lock(lock) as after_cancel:
            assert after_cancel is True

    asyncio.run(go())


def test_app_wires_provider_and_lock_into_the_daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("DYNDNSARR_AGENT_DONDOMINIO_USER", "u")
    monkeypatch.setenv("DYNDNSARR_AGENT_DONDOMINIO_KEY", "k")
    path = tmp_path / "agent.toml"
    path.write_text(TOML.replace("monitored", "managed") + 'provider = "dondominio"\n')
    seen = []

    def handler(request):
        if request.url.host == "dondns.dondominio.com":
            seen.append(dict(request.url.params))
            data = {"success": True, "messages": [], "host": {"value": IP, "updated": True}}
            return httpx.Response(200, json=data)
        return sources(request)

    settings = Settings(data_dir=tmp_path / "data", config_file=path)
    app = create_app(settings, observer=StaleObserver(), transport=httpx.MockTransport(handler))
    with TestClient(app):
        wait_for(
            settings,
            "SELECT type FROM events ORDER BY sequence",
            [("agent_started",), ("ip_changed",), ("dns_update_succeeded",)],
        )
    assert seen[0]["host"] == NAME and seen[0]["ip"] == IP and seen[0]["lang"] == "en"
    assert (tmp_path / "data" / "ddns.lock").exists()
