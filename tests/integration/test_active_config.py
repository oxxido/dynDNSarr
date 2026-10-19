"""Configuración activa: TOML + versión remota, `ConfigHolder` y `config-reset`."""

import asyncio
import logging
import sqlite3
from datetime import UTC, datetime

import httpx
import pytest

from dyndnsarr_agent import cli
from dyndnsarr_agent.config import Settings
from dyndnsarr_agent.db import MIGRATIONS, control_repo
from dyndnsarr_agent.ddns.active_config import ConfigHolder, load_active
from dyndnsarr_agent.ddns.config import ConfigError, DdnsConfig
from dyndnsarr_agent.dns.resolver import ResolverAnswer
from dyndnsarr_agent.main import create_app
from dyndnsarr_agent.providers.base import UpdateResult
from dyndnsarr_agent.scheduler import run_forever
from dyndnsarr_shared.control import canonical_json, config_hash
from dyndnsarr_shared.db import connect, migrate

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
IP = "1.2.3.4"

TOML = """
check_interval_seconds = 300
[ip_sources]
urls = ["https://a.example/ip", "https://b.example/ip"]
[dns]
resolvers = ["1.1.1.1"]
[providers.dondominio]
url = "https://dondns.dondominio.com/json/"
[[records]]
name = "toml.example.com"
mode = "monitored"
"""


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    import os

    for name in list(os.environ):
        if name.startswith("DYNDNSARR_"):
            monkeypatch.delenv(name)


@pytest.fixture
def settings(tmp_path):
    path = tmp_path / "agent.toml"
    path.write_text(TOML, encoding="utf-8")
    return Settings(data_dir=tmp_path / "data", config_file=path)


def store_version(settings, version, content):
    conn = connect(settings.db_path)
    migrate(conn, MIGRATIONS)
    control_repo.insert_version(conn, version, config_hash(content), canonical_json(content), NOW)
    conn.close()


REMOTE = {
    "check_interval_seconds": 120,
    "records": [{"name": "remote.example.com", "mode": "monitored"}],
}


def test_without_versions_the_toml_is_active(settings):
    active = load_active(settings)
    assert (active.version, active.content_hash) == (0, None)
    assert active.config.check_interval_seconds == 300
    assert [r.name for r in active.config.records] == ["toml.example.com"]


def test_a_database_before_migration_0005_means_toml(settings):
    old = connect(settings.db_path)
    old.executescript("BEGIN; CREATE TABLE app_state (key TEXT); PRAGMA user_version = 4; COMMIT;")
    old.close()
    assert load_active(settings).version == 0


def test_version_sections_win_and_local_sections_come_from_the_toml(settings):
    store_version(settings, 3, REMOTE)
    active = load_active(settings)
    assert (active.version, active.content_hash) == (3, config_hash(REMOTE))
    assert active.config.check_interval_seconds == 120
    assert [r.name for r in active.config.records] == ["remote.example.com"]
    # Secciones que la versión no menciona: las del TOML (incl. las locales).
    assert [u for u in active.config.ip_sources.urls] == [
        "https://a.example/ip",
        "https://b.example/ip",
    ]
    assert active.config.providers.dondominio.url == "https://dondns.dondominio.com/json/"
    assert active.config.hub is None


def test_a_warning_is_logged_when_the_version_overrides_the_toml(settings, caplog):
    store_version(settings, 1, REMOTE)
    with caplog.at_level(logging.WARNING):
        load_active(settings)
    [record] = [r for r in caplog.records if r.message == "config_overrides_toml"]
    assert record.sections == ["check_interval_seconds", "records"]


def test_no_warning_when_the_version_matches_the_toml(settings, caplog):
    store_version(settings, 1, {"check_interval_seconds": 300})
    with caplog.at_level(logging.WARNING):
        load_active(settings)
    assert not [r for r in caplog.records if r.message == "config_overrides_toml"]


def test_a_stored_version_that_no_longer_validates_blocks_startup_and_names_reset(settings):
    store_version(settings, 2, {"check_interval_seconds": 5})
    with pytest.raises(ConfigError) as exc:
        load_active(settings)
    assert "versión 2" in str(exc.value) and "config-reset" in str(exc.value)


def test_a_broken_toml_still_fails_even_with_a_version(settings):
    store_version(settings, 1, REMOTE)
    settings.config_file.write_text("esto no = = es toml")
    with pytest.raises(ConfigError, match="TOML inválido"):
        load_active(settings)


def test_create_app_refuses_a_version_that_needs_missing_credentials_and_names_reset(settings):
    content = {"records": [{"name": "m.example.com", "mode": "managed", "provider": "dondominio"}]}
    store_version(settings, 4, content)
    with pytest.raises(ConfigError) as exc:
        create_app(settings)
    assert "credenciales" in str(exc.value) and "config-reset" in str(exc.value)


def test_create_app_exposes_the_active_version(settings):
    store_version(settings, 4, REMOTE)
    app = create_app(settings)
    assert app.state.active.version == 4
    assert app.state.ddns_config.check_interval_seconds == 120


# --- config-reset ---


def reset(settings, capsys):
    code = cli.run(["config-reset"])
    return code, capsys.readouterr().out


def test_config_reset_restores_the_toml_and_emits_the_event(settings, monkeypatch, capsys):
    monkeypatch.setenv("DYNDNSARR_AGENT_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("DYNDNSARR_AGENT_CONFIG_FILE", str(settings.config_file))
    store_version(settings, 3, REMOTE)
    code, out = reset(settings, capsys)
    assert code == 0 and "versión 3" in out and "Reinicia" in out
    assert load_active(settings).version == 0
    conn = sqlite3.connect(settings.db_path)
    rows = conn.execute("SELECT type, payload FROM events").fetchall()
    assert rows == [("config_reset", '{"previous_version": 3}')]


def test_config_reset_without_versions_is_a_no_op(settings, monkeypatch, capsys):
    monkeypatch.setenv("DYNDNSARR_AGENT_DATA_DIR", str(settings.data_dir))
    code, out = reset(settings, capsys)
    assert code == 0 and "ya manda el TOML" in out
    conn = sqlite3.connect(settings.db_path)
    assert conn.execute("SELECT count(*) FROM events").fetchone() == (0,)


def test_status_shows_the_active_configuration(settings, monkeypatch, capsys):
    monkeypatch.setenv("DYNDNSARR_AGENT_DATA_DIR", str(settings.data_dir))
    store_version(settings, 3, REMOTE)
    assert cli.run(["status"]) == 0
    assert f"Configuración: versión 3 (hash {config_hash(REMOTE)[:12]})" in capsys.readouterr().out
    monkeypatch.setenv("DYNDNSARR_AGENT_CONFIG_FILE", str(settings.config_file))
    cli.run(["config-reset"])
    capsys.readouterr()
    cli.run(["status"])
    assert "Configuración: TOML local" in capsys.readouterr().out


def test_the_cli_commands_use_the_active_configuration(settings, monkeypatch, capsys):
    monkeypatch.setenv("DYNDNSARR_AGENT_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("DYNDNSARR_AGENT_CONFIG_FILE", str(settings.config_file))
    store_version(settings, 3, REMOTE)

    class Observer:
        async def lookup(self, name):
            return [ResolverAnswer("1.1.1.1", (IP,), None)]

    cli.run(["check-dns"], observer=Observer())
    assert "remote.example.com" in capsys.readouterr().out


# --- ConfigHolder y scheduler ---


class Observer:
    def __init__(self):
        self.names = []

    async def lookup(self, name):
        self.names.append(name)
        return [ResolverAnswer("1.1.1.1", (IP,), None)]


class Stop(BaseException):
    pass


def make_config(*names, interval=300):
    return DdnsConfig.model_validate(
        {
            "check_interval_seconds": max(interval, 60),
            "ip_sources": {"urls": ["https://a.example/ip", "https://b.example/ip"]},
            "dns": {"resolvers": ["1.1.1.1"]},
            "records": [{"name": n, "mode": "monitored"} for n in names],
        }
    )


def test_the_next_cycle_uses_the_replaced_configuration(tmp_path):
    conn = connect(tmp_path / "agent.db")
    migrate(conn, MIGRATIONS)
    observer = Observer()
    holder = ConfigHolder(make_config("one.example.com"), {}, observer)
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 1:
            holder.replace(make_config("two.example.com", interval=120), 2, "h" * 64)
        if len(sleeps) == 2:
            raise Stop

    async def go():
        transport = httpx.MockTransport(lambda r: httpx.Response(200, text=IP))
        async with httpx.AsyncClient(transport=transport) as client:
            await run_forever(conn, holder, client, sleep=sleep)

    with pytest.raises(Stop):
        asyncio.run(go())
    assert observer.names == ["one.example.com", "two.example.com"]
    assert sleeps == [300, 120]
    assert (holder.version, holder.content_hash) == (2, "h" * 64)


def test_replace_rebuilds_providers_so_a_new_managed_record_has_one(tmp_path):
    built = []

    class Provider:
        async def update(self, host, ip):
            return UpdateResult(True)

    def rebuild(config):
        built.append([r.name for r in config.records])
        managed = any(r.mode == "managed" for r in config.records)
        return ({"dondominio": Provider()} if managed else {}), Observer()

    holder = ConfigHolder(make_config("one.example.com"), {}, Observer(), rebuild=rebuild)
    assert holder.providers == {}
    new = DdnsConfig.model_validate(
        {
            "ip_sources": {"urls": ["https://a.example/ip", "https://b.example/ip"]},
            "dns": {"resolvers": ["1.1.1.1"]},
            "records": [{"name": "m.example.com", "mode": "managed", "provider": "dondominio"}],
        }
    )
    holder.replace(new, 5, "h" * 64)
    assert list(holder.providers) == ["dondominio"]
    assert built == [["m.example.com"]]


def test_a_failing_rebuild_leaves_the_holder_untouched():
    def rebuild(config):
        raise RuntimeError("no se pudo")

    original = make_config("one.example.com")
    holder = ConfigHolder(original, {}, Observer(), version=1, rebuild=rebuild)
    with pytest.raises(RuntimeError):
        holder.replace(make_config("two.example.com"), 2, "h" * 64)
    assert holder.config is original and holder.version == 1
