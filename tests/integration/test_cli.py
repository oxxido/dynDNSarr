import asyncio
import sqlite3

import httpx
import pytest

from dyndnsarr_agent import cli
from dyndnsarr_agent.config import Settings
from dyndnsarr_agent.db import MIGRATIONS
from dyndnsarr_agent.ddns.config import load_ddns_config
from dyndnsarr_agent.ddns.cycle import run_cycle
from dyndnsarr_agent.dns.resolver import ResolverAnswer
from dyndnsarr_agent.scheduler import utc_now
from dyndnsarr_shared.db import connect, migrate

IP = "1.2.3.4"
NAME = "home.example.com"

TOML = """
[ip_sources]
urls = ["https://a.example/ip", "https://b.example/ip"]
[dns]
resolvers = ["1.1.1.1", "8.8.8.8"]
[[records]]
name = "home.example.com"
mode = "monitored"
"""


class Observer:
    def __init__(self, *ips, code=None):
        self.ips, self.code = ips, code

    async def lookup(self, name):
        return [ResolverAnswer(r, self.ips, self.code) for r in ("1.1.1.1", "8.8.8.8")]


def transport(a=IP, b=IP):
    def handler(request):
        ip = a if request.url.host == "a.example" else b
        if ip is None:
            raise httpx.ConnectError("down")
        return httpx.Response(200, text=ip)

    return httpx.MockTransport(handler)


@pytest.fixture
def env(tmp_path, monkeypatch):
    config = tmp_path / "agent.toml"
    config.write_text(TOML, encoding="utf-8")
    monkeypatch.setenv("DYNDNSARR_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DYNDNSARR_AGENT_CONFIG_FILE", str(config))
    return Settings()


def db_rows(settings, sql):
    conn = sqlite3.connect(settings.db_path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def populate(settings, observer):
    """Deja en la BD lo que dejaría un ciclo del daemon."""
    conn = connect(settings.db_path)
    migrate(conn, MIGRATIONS)
    config = load_ddns_config(settings.config_file)

    async def go():
        async with httpx.AsyncClient(transport=transport()) as client:
            await run_cycle(conn, config, client, observer, utc_now)

    asyncio.run(go())
    conn.close()


# --- despacho ---


def test_no_arguments_serves(monkeypatch):
    called = []
    monkeypatch.setattr(cli, "serve", lambda: called.append(True))
    assert cli.run([]) == 0
    assert called == [True]


def test_serve_command_serves(monkeypatch):
    called = []
    monkeypatch.setattr(cli, "serve", lambda: called.append(True))
    cli.run(["serve"])
    assert called == [True]


def test_unknown_command_is_a_usage_error():
    with pytest.raises(SystemExit) as exc:
        cli.run(["explode"])
    assert exc.value.code == 2


# --- status ---


def test_status_without_database_fails_and_does_not_create_it(env, capsys):
    assert cli.run(["status"]) == 1
    assert "no ha arrancado" in capsys.readouterr().err
    assert not env.db_path.exists()


def test_status_without_engine_data_fails(env, capsys):
    conn = connect(env.db_path)  # BD del slice1: sin migración 0002
    conn.close()
    assert cli.run(["status"]) == 1
    assert "arranca el Agent" in capsys.readouterr().err


def test_status_reports_ip_records_and_events(env, capsys):
    populate(env, Observer(IP))
    assert cli.run(["status"]) == 0
    out = capsys.readouterr().out
    assert f"IP pública: {IP} (confirmada el" in out
    assert f"{NAME}  verified  desde" in out
    assert "ip_changed" in out and f"dns_verified {NAME}" in out


def test_status_does_not_write(env):
    populate(env, Observer(IP))
    tables = ("events", "record_state", "app_state", "public_ip_observations", "dns_checks")
    snapshot = {t: db_rows(env, f"SELECT * FROM {t}") for t in tables}
    mtime = env.db_path.stat().st_mtime_ns
    assert cli.run(["status"]) == 0
    assert {t: db_rows(env, f"SELECT * FROM {t}") for t in tables} == snapshot
    assert env.db_path.stat().st_mtime_ns == mtime


# --- check-ip ---


def test_check_ip_saves_samples_without_touching_state(env, capsys):
    assert cli.run(["check-ip"], transport=transport()) == 0
    out = capsys.readouterr().out
    assert f"IP pública confirmada: {IP}" in out
    assert db_rows(env, "SELECT count(*) FROM public_ip_observations") == [(2,)]
    assert db_rows(env, "SELECT count(*) FROM events") == [(0,)]
    assert db_rows(env, "SELECT count(*) FROM app_state WHERE key = 'public_ip'") == [(0,)]


def test_check_ip_uncertain_exits_non_zero_and_still_saves_samples(env, capsys):
    assert cli.run(["check-ip"], transport=transport(IP, "5.6.7.8")) == 1
    assert "incierta (disagreement)" in capsys.readouterr().err
    assert db_rows(env, "SELECT count(*) FROM public_ip_observations") == [(2,)]


def test_check_ip_shows_source_errors(env, capsys):
    assert cli.run(["check-ip"], transport=transport(IP, None)) == 1
    assert "error: connect_error" in capsys.readouterr().out


# --- check-dns ---


def test_check_dns_compares_with_the_last_confirmed_ip(env, capsys):
    populate(env, Observer(IP))
    assert cli.run(["check-dns"], observer=Observer("9.9.9.9")) == 0
    out = capsys.readouterr().out
    assert f"IP pública de referencia: {IP}" in out
    assert f"{NAME}  mismatch" in out
    assert "1.1.1.1  9.9.9.9" in out
    # solo muestras: el estado del registro lo sigue gobernando el ciclo
    assert db_rows(env, "SELECT verification FROM record_state") == [("verified",)]
    assert db_rows(env, "SELECT count(*) FROM dns_checks") == [(4,)]


def test_check_dns_without_reference_ip_says_so(env, capsys):
    assert cli.run(["check-dns"], observer=Observer(code="nxdomain")) == 0
    out = capsys.readouterr().out
    assert "desconocida" in out and "sin referencia" in out
    assert "error: nxdomain" in out


# --- configuración ---


@pytest.mark.parametrize("command", ["check-ip", "check-dns"])
def test_check_commands_need_the_config_file(tmp_path, monkeypatch, capsys, command):
    monkeypatch.setenv("DYNDNSARR_AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("DYNDNSARR_AGENT_CONFIG_FILE", raising=False)
    assert cli.run([command]) == 1
    assert "DYNDNSARR_AGENT_CONFIG_FILE" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["check-ip", "check-dns"])
def test_check_commands_report_an_invalid_config(env, capsys, command):
    env.config_file.write_text(TOML.replace("monitored", "managed"), encoding="utf-8")
    assert cli.run([command]) == 1
    assert "slice 2b" in capsys.readouterr().err


def test_invalid_environment_aborts_with_clear_message(tmp_path, monkeypatch):
    monkeypatch.setenv("DYNDNSARR_AGENT_PORT", "nope")
    with pytest.raises(SystemExit) as exc:
        cli.run(["status"])
    assert "DYNDNSARR_AGENT_PORT" in str(exc.value.code)
