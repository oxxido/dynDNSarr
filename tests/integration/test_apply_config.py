"""`apply_config`: una prueba por fila de la tabla de D5, atomicidad e idempotencia."""

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest

from dyndnsarr_agent.config import Settings
from dyndnsarr_agent.db import MIGRATIONS, control_repo, ddns_repo
from dyndnsarr_agent.ddns.active_config import ConfigHolder, load_active
from dyndnsarr_agent.ddns.apply_config import apply_config
from dyndnsarr_agent.ddns.config import DdnsConfig
from dyndnsarr_agent.dns.resolver import ResolverAnswer
from dyndnsarr_agent.scheduler import run_forever
from dyndnsarr_shared.control import ConfigPut, config_hash
from dyndnsarr_shared.db import connect, migrate

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
IP = "1.2.3.4"
CANARY = "canary-secret-value-0123"

TOML = """
[ip_sources]
urls = ["https://a.example/ip", "https://b.example/ip"]
[dns]
resolvers = ["1.1.1.1"]
[[records]]
name = "toml.example.com"
mode = "monitored"
"""

CONTENT = {
    "check_interval_seconds": 120,
    "records": [{"name": "remote.example.com", "mode": "monitored"}],
}


class World:
    def __init__(self, tmp_path, **settings):
        path = tmp_path / "agent.toml"
        path.write_text(TOML, encoding="utf-8")
        self.settings = Settings(data_dir=tmp_path / "data", config_file=path, **settings)
        self.conn = connect(self.settings.db_path)
        migrate(self.conn, MIGRATIONS)
        active = load_active(self.settings)
        self.built = []
        self.holder = ConfigHolder(
            active.config,
            {},
            None,
            base_raw=active.base_raw,
            rebuild=self._rebuild,
        )

    def _rebuild(self, config):
        self.built.append(config)
        return {}, None

    def put(self, version, content=CONTENT, hash=None):
        return ConfigPut(version=version, hash=hash or config_hash(content), content=content)

    def apply(self, put):
        return apply_config(self.conn, self.holder, self.settings, put, lambda: NOW)

    def events(self):
        return [(e.type, e.payload) for e in ddns_repo.list_events(self.conn, 50, False)]

    def versions(self):
        return self.conn.execute("SELECT version FROM configuration_versions").fetchall()


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def test_a_greater_valid_version_is_applied_stored_and_announced(world):
    result = world.apply(world.put(3))
    assert (result.result, result.version, result.code) == ("applied", 3, None)
    assert world.versions() == [(3,)]
    assert (world.holder.version, world.holder.content_hash) == (3, config_hash(CONTENT))
    assert [r.name for r in world.holder.config.records] == ["remote.example.com"]
    assert world.events() == [("config_applied", {"version": 3, "hash": config_hash(CONTENT)})]
    assert load_active(world.settings).version == 3


def test_a_newer_version_replaces_the_active_one(world):
    world.apply(world.put(1))
    other = {"check_interval_seconds": 600}
    assert world.apply(world.put(2, other)).result == "applied"
    assert world.holder.config.check_interval_seconds == 600
    assert world.versions() == [(1,), (2,)]


def test_the_same_version_and_hash_is_idempotent_without_new_row_or_event(world):
    world.apply(world.put(3))
    again = world.apply(world.put(3))
    assert (again.result, again.version) == ("applied", 3)
    assert world.versions() == [(3,)]
    assert len(world.events()) == 1
    assert len(world.built) == 1


def test_the_same_version_with_another_hash_is_a_conflict(world):
    world.apply(world.put(3))
    result = world.apply(world.put(3, {"check_interval_seconds": 600}))
    assert (result.result, result.code) == ("rejected", "version_conflict")
    assert world.holder.config.check_interval_seconds == 120


def test_a_lower_version_is_stale(world):
    world.apply(world.put(3))
    result = world.apply(world.put(2, {"check_interval_seconds": 600}))
    assert (result.result, result.code) == ("rejected", "stale_version")
    assert world.holder.version == 3


def test_a_wrong_hash_is_rejected_before_anything_else(world):
    result = world.apply(world.put(1, hash="0" * 64))
    assert (result.result, result.code) == ("rejected", "hash_mismatch")
    assert world.versions() == []


@pytest.mark.parametrize(
    "section",
    [
        {"providers": {"dondominio": {"url": "https://evil.example/json/"}}},
        {"hub": {"url": "https://evil.example", "agent_id": "x"}},
        {"dondominio_key": CANARY},
        {"unknown": 1},
    ],
)
def test_local_or_unknown_sections_are_forbidden(world, section):
    content = {**CONTENT, **section}
    result = world.apply(world.put(1, content))
    assert (result.result, result.code) == ("rejected", "forbidden_section")
    assert list(section)[0] in result.message_safe
    assert CANARY not in json.dumps(result.model_dump())
    assert world.versions() == [] and world.built == []


@pytest.mark.parametrize(
    "content,path",
    [
        ({"check_interval_seconds": 5}, "check_interval_seconds"),
        ({"ip_sources": {"urls": ["https://a.example/ip"]}}, "ip_sources.urls"),
        ({"records": [{"name": "x.example.com", "mode": "managed"}]}, "records.0"),
        ({"records": [{"name": "a b", "mode": "monitored"}]}, "records.0.name"),
        ({"retention": {"samples_days": "mucho"}}, "retention.samples_days"),
    ],
)
def test_an_invalid_config_is_rejected_with_a_path_and_reason(world, content, path):
    result = world.apply(world.put(1, content))
    assert (result.result, result.code) == ("rejected", "invalid_config")
    assert path in result.message_safe
    assert len(result.message_safe) <= 1024
    assert world.versions() == []


def test_the_message_is_cut_to_1_kib(world):
    records = [{"name": f"bad name {i}", "mode": "monitored"} for i in range(200)]
    result = world.apply(world.put(1, {"records": records}))
    assert result.code == "invalid_config" and len(result.message_safe) == 1024


def test_a_managed_record_without_credentials_is_rejected(world):
    content = {"records": [{"name": "m.example.com", "mode": "managed", "provider": "dondominio"}]}
    result = world.apply(world.put(1, content))
    assert (result.result, result.code) == ("rejected", "missing_credentials")
    assert "DYNDNSARR_AGENT_DONDOMINIO_USER" in result.message_safe
    assert world.versions() == []


def test_a_managed_record_with_credentials_is_applied_and_the_holder_rebuilds(tmp_path):
    world = World(tmp_path, dondominio_user="u", dondominio_key=CANARY)
    content = {"records": [{"name": "m.example.com", "mode": "managed", "provider": "dondominio"}]}
    assert world.apply(world.put(1, content)).result == "applied"
    assert [r.name for r in world.built[0].records] == ["m.example.com"]


def test_a_rejection_emits_an_event_with_version_hash_and_code_but_no_content(world):
    put = world.put(7, {"check_interval_seconds": 5, "dondominio_key": CANARY})
    world.apply(put)
    [(kind, payload)] = world.events()
    assert kind == "config_rejected"
    assert payload == {"version": 7, "hash": put.hash, "code": "forbidden_section"}
    assert CANARY not in json.dumps(payload)


def test_a_failure_before_commit_leaves_no_row_and_no_change(world, monkeypatch):
    original = world.holder.config
    real = ddns_repo.insert_event

    def explode(conn, type, *args, **kwargs):
        if type == "config_applied":
            raise RuntimeError("fallo inyectado")
        return real(conn, type, *args, **kwargs)

    monkeypatch.setattr(ddns_repo, "insert_event", explode)
    with pytest.raises(RuntimeError):
        world.apply(world.put(1))
    assert world.versions() == []
    assert world.events() == []
    assert world.holder.config is original and world.holder.version == 0
    assert world.built == []
    assert load_active(world.settings).version == 0


def test_the_running_cycle_keeps_its_configuration(tmp_path):
    world = World(tmp_path)
    started, release = asyncio.Event(), asyncio.Event()
    seen = []

    class Observer:
        async def lookup(self, name):
            seen.append(name)
            if not started.is_set():
                started.set()
                await release.wait()
            return [ResolverAnswer("1.1.1.1", (IP,), None)]

    world.holder.observer = Observer()
    world.holder._rebuild = lambda config: ({}, world.holder.observer)

    class Stop(BaseException):
        pass

    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise Stop

    async def go():
        transport = httpx.MockTransport(lambda r: httpx.Response(200, text=IP))
        async with httpx.AsyncClient(transport=transport) as client:
            task = asyncio.create_task(run_forever(world.conn, world.holder, client, sleep=sleep))
            await started.wait()
            # El ciclo está a mitad: la configuración nueva se aplica, pero no lo afecta.
            assert world.apply(world.put(1)).result == "applied"
            release.set()
            with pytest.raises(Stop):
                await task

    asyncio.run(go())
    assert seen == ["toml.example.com", "remote.example.com"]
    assert sleeps == [300, 120]


def test_the_stored_version_is_what_validated(world):
    world.apply(world.put(1))
    stored = control_repo.latest_version(world.conn)
    assert stored.content == CONTENT and stored.content_hash == config_hash(CONTENT)
    assert DdnsConfig.model_validate({**world.holder.base_raw, **stored.content})
