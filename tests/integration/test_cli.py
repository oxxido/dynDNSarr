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
from dyndnsarr_agent.scheduler import try_lock, utc_now
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
    assert "provider" in capsys.readouterr().err


def test_invalid_environment_aborts_with_clear_message(tmp_path, monkeypatch):
    monkeypatch.setenv("DYNDNSARR_AGENT_PORT", "nope")
    with pytest.raises(SystemExit) as exc:
        cli.run(["status"])
    assert "DYNDNSARR_AGENT_PORT" in str(exc.value.code)


# --- sync-ddns (slice 2b) ---

KEY = "canary-key-0123456789"
USER = "canary-user"
MANAGED_TOML = TOML.replace("monitored", "managed") + 'provider = "dondominio"\n'


class Dondominio:
    """DonDominio falso: anota cada petición y responde `behaviour`."""

    def __init__(self, behaviour="ok"):
        self.behaviour = behaviour
        self.requests = []

    def handler(self, request):
        if request.url.host != "dondns.dondominio.com":
            return httpx.Response(200, text=IP)
        self.requests.append(dict(request.url.params))
        if self.behaviour == "down":
            return httpx.Response(503, text="unavailable")
        if self.behaviour == "auth":
            return httpx.Response(200, json={"success": False, "messages": ["Wrong key"]})
        data = {"success": True, "messages": [], "host": {"value": IP, "updated": True}}
        return httpx.Response(200, json=data)

    @property
    def transport(self):
        return httpx.MockTransport(self.handler)


@pytest.fixture
def managed(env, monkeypatch):
    env.config_file.write_text(MANAGED_TOML, encoding="utf-8")
    monkeypatch.setenv("DYNDNSARR_AGENT_DONDOMINIO_USER", USER)
    monkeypatch.setenv("DYNDNSARR_AGENT_DONDOMINIO_KEY", KEY)
    return env


def sync(fake, dns=("9.9.9.9",)):
    return cli.run(["sync-ddns"], observer=Observer(*dns), transport=fake.transport)


def test_sync_ddns_updates_a_stale_record_with_the_confirmed_ip(managed, capsys):
    fake = Dondominio()
    assert sync(fake) == 0
    out = capsys.readouterr().out
    assert f"IP pública confirmada: {IP}" in out
    assert f"{NAME}  pending_verification  actualizado" in out
    [params] = fake.requests
    assert (params["host"], params["ip"], params["lang"]) == (NAME, IP, "en")
    assert db_rows(managed, "SELECT type FROM events ORDER BY sequence") == [
        ("ip_changed",),
        ("dns_update_succeeded",),
    ]
    assert db_rows(managed, "SELECT accepted FROM dns_update_attempts") == [(1,)]


def test_sync_ddns_does_not_call_when_dns_is_already_right(managed, capsys):
    fake = Dondominio()
    assert sync(fake, dns=(IP,)) == 0
    assert fake.requests == []
    assert f"{NAME}  verified  el DNS ya resuelve la IP: sin llamada" in capsys.readouterr().out


def test_sync_ddns_keeps_deduplication_but_ignores_the_backoff(managed, capsys):
    down = Dondominio("down")
    assert sync(down) == 1
    assert "fallo del proveedor: http_error (reintentable)" in capsys.readouterr().out
    assert len(down.requests) == 1
    # el daemon esperaría 60 s: `sync-ddns` reintenta ya
    fake = Dondominio()
    assert sync(fake) == 0
    assert len(fake.requests) == 1
    capsys.readouterr()
    # IP ya aceptada y en plazo: no vuelve a llamar
    again = Dondominio()
    assert sync(again) == 0
    assert again.requests == []
    assert "IP ya aceptada, esperando propagación: sin llamada" in capsys.readouterr().out


def test_sync_ddns_retries_a_definitive_error(managed, capsys):
    assert sync(Dondominio("auth")) == 1
    assert "auth_failed (definitivo)" in capsys.readouterr().out
    fake = Dondominio()
    assert sync(fake) == 0
    assert len(fake.requests) == 1


def test_sync_ddns_with_the_lock_taken_exits_one_without_calling(managed, capsys):
    fake = Dondominio()
    managed.data_dir.mkdir(parents=True, exist_ok=True)
    with try_lock(managed.lock_path) as taken:
        assert taken is True
        assert sync(fake) == 1
    assert fake.requests == []
    assert "ciclo del Agent en curso" in capsys.readouterr().err
    assert sync(fake) == 0  # libre otra vez


def test_sync_ddns_with_an_uncertain_ip_exits_one_without_calling(managed, capsys):
    fake = Dondominio()
    transport = httpx.MockTransport(
        lambda r: (
            httpx.Response(200, text="5.6.7.8") if r.url.host == "b.example" else fake.handler(r)
        )
    )
    assert cli.run(["sync-ddns"], observer=Observer("9.9.9.9"), transport=transport) == 1
    assert fake.requests == []
    assert "IP pública incierta" in capsys.readouterr().err


def test_sync_ddns_needs_credentials(env, monkeypatch, capsys):
    env.config_file.write_text(MANAGED_TOML, encoding="utf-8")
    monkeypatch.delenv("DYNDNSARR_AGENT_DONDOMINIO_USER", raising=False)
    monkeypatch.delenv("DYNDNSARR_AGENT_DONDOMINIO_KEY", raising=False)
    fake = Dondominio()
    assert sync(fake) == 1
    assert "DYNDNSARR_AGENT_DONDOMINIO_USER" in capsys.readouterr().err
    assert fake.requests == []


def test_sync_ddns_without_managed_records_has_nothing_to_do(env, capsys):
    fake = Dondominio()
    assert sync(fake) == 0
    assert "nada que sincronizar" in capsys.readouterr().out
    assert fake.requests == []


def test_sync_ddns_only_touches_managed_records(managed, capsys):
    managed.config_file.write_text(
        MANAGED_TOML + '\n[[records]]\nname = "watch.example.com"\nmode = "monitored"\n',
        encoding="utf-8",
    )
    fake = Dondominio()
    assert sync(fake) == 0
    assert db_rows(managed, "SELECT DISTINCT record_name FROM dns_checks") == [(NAME,)]
    assert db_rows(managed, "SELECT record_name FROM record_state") == [(NAME,)]


def test_sync_ddns_runs_with_the_daemon_stopped_and_creates_the_lock(managed):
    assert not managed.db_path.exists()
    assert sync(Dondominio()) == 0
    assert managed.lock_path.exists()


def test_the_key_never_shows_in_the_cli_output_or_the_database(managed, capsys):
    outputs = []
    for behaviour in ("down", "auth", "ok"):
        sync(Dondominio(behaviour))
        outputs.append(capsys.readouterr())
        cli.run(["status"])
        outputs.append(capsys.readouterr())
    for captured in outputs:
        assert KEY not in captured.out + captured.err
        assert USER not in captured.out + captured.err
    conn = sqlite3.connect(managed.db_path)
    dump = "\n".join(conn.iterdump())
    conn.close()
    assert KEY not in dump and USER not in dump


# --- status con el estado de actualización ---


def test_status_shows_acceptance_wait_and_last_error(managed, capsys):
    assert sync(Dondominio("down")) == 1
    capsys.readouterr()
    assert cli.run(["status"]) == 0
    out = capsys.readouterr().out
    assert f"{NAME}  mismatch  desde" in out
    assert "reintento a partir de" in out and "1 fallo(s), último error: http_error" in out
    assert sync(Dondominio("auth")) == 1
    capsys.readouterr()
    cli.run(["status"])
    out = capsys.readouterr().out
    assert "sin reintentos automáticos" in out and "auth_failed" in out and "sync-ddns" in out
    assert sync(Dondominio()) == 0
    capsys.readouterr()
    cli.run(["status"])
    out = capsys.readouterr().out
    assert f"{NAME}  pending_verification  desde" in out
    assert f"aceptada por el proveedor: {IP} el" in out
    assert "dns_update_succeeded" in out


def test_status_still_does_not_write_with_managed_records(managed):
    sync(Dondominio("down"))
    tables = ("events", "record_state", "app_state", "dns_update_attempts")
    snapshot = {t: db_rows(managed, f"SELECT * FROM {t}") for t in tables}
    mtime = managed.db_path.stat().st_mtime_ns
    assert cli.run(["status"]) == 0
    assert {t: db_rows(managed, f"SELECT * FROM {t}") for t in tables} == snapshot
    assert managed.db_path.stat().st_mtime_ns == mtime
