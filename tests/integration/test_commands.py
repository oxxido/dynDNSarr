"""Ejecutor de comandos: idempotencia, expiración, serie, `busy`, reinicio y eventos."""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from dyndnsarr_agent.commands.executor import CommandError, CommandExecutor
from dyndnsarr_agent.db import MIGRATIONS, control_repo, ddns_repo
from dyndnsarr_agent.ddns.active_config import ConfigHolder
from dyndnsarr_agent.ddns.config import DdnsConfig
from dyndnsarr_agent.dns.resolver import ResolverAnswer
from dyndnsarr_agent.providers.base import UpdateResult
from dyndnsarr_agent.scheduler import try_lock
from dyndnsarr_shared.control import CommandRequest
from dyndnsarr_shared.db import connect, migrate

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
EXPIRES = "2026-10-19T12:05:00Z"
IP = "1.2.3.4"
NAME = "home.example.com"
CANARY = "canary-secret-0123"

CONFIG = DdnsConfig.model_validate(
    {
        "ip_sources": {"urls": ["https://a.example/ip", "https://b.example/ip"]},
        "dns": {"resolvers": ["1.1.1.1"]},
        "records": [
            {"name": NAME, "mode": "managed", "provider": "dondominio"},
            {"name": "watch.example.com", "mode": "monitored"},
        ],
    }
)


class Provider:
    def __init__(self):
        self.calls = []
        self.result = UpdateResult(True, changed=True)

    async def update(self, host, ip):
        self.calls.append((host, ip))
        return self.result


class Observer:
    """Responde una IP antigua (el DNS no apunta aún a `IP`)."""

    def __init__(self):
        self.log = []
        self.delay = 0.0
        self.error = None

    async def lookup(self, name):
        self.log.append(("start", name))
        if self.error:
            raise self.error
        if self.delay:
            await asyncio.sleep(self.delay)
        self.log.append(("end", name))
        return [ResolverAnswer("1.1.1.1", ("9.9.9.9",), None)]


class World:
    def __init__(self, tmp_path, queue_size=16, ip=IP):
        self.conn = connect(tmp_path / "agent.db")
        migrate(self.conn, MIGRATIONS)
        self.provider, self.observer = Provider(), Observer()
        self.holder = ConfigHolder(CONFIG, {"dondominio": self.provider}, self.observer)
        self.lock = tmp_path / "ddns.lock"
        self.ip = ip
        self.now = NOW
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self._sources))
        self.executor = CommandExecutor(
            self.conn,
            self.holder,
            self.client,
            self.lock,
            clock=lambda: self.now,
            queue_size=queue_size,
        )

    def _sources(self, request):
        if self.ip is None:
            raise httpx.ConnectError("down")
        return httpx.Response(200, text=self.ip if request.url.host == "a.example" else self.ip)

    def request(self, type="sync_ddns", command_id=None, expires_at=EXPIRES, **extra):
        return CommandRequest(
            command_id=command_id or str(uuid.uuid4()),
            type=type,
            requested_by="admin:1",
            expires_at=expires_at,
            **extra,
        )

    async def run(self, *requests):
        """Envía y deja que el ejecutor los procese todos."""
        worker = asyncio.create_task(self.executor.run())
        try:
            submitted = [self.executor.submit(r) for r in requests]
            await self.executor.join()
        finally:
            worker.cancel()
        return submitted

    def completed(self):
        return [
            e.payload
            for e in ddns_repo.list_events(self.conn, 100, False)
            if e.type == "command_completed"
        ]


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def go(coro):
    return asyncio.run(coro)


def test_check_dns_saves_samples_and_reports_verdicts_without_the_ip(world):
    ddns_repo.set_state(world.conn, "public_ip", IP, NOW)
    request = world.request("check_dns")
    [(row, new)] = go(world.run(request))
    assert new and row.status == "received"
    done = control_repo.get_command(world.conn, request.command_id)
    assert done.status == "succeeded" and done.finished_at == "2026-10-19T12:00:00Z"
    assert done.result == {"records": {NAME: "mismatch", "watch.example.com": "mismatch"}}
    assert world.conn.execute("SELECT count(*) FROM dns_checks").fetchone() == (2,)
    [event] = world.completed()
    assert event["command_id"] == request.command_id and event["status"] == "succeeded"
    assert IP not in json.dumps(event)


def test_check_dns_without_a_reference_ip(world):
    request = world.request("check_dns")
    go(world.run(request))
    result = control_repo.get_command(world.conn, request.command_id).result
    assert set(result["records"].values()) == {"no_reference"}


def test_sync_ddns_calls_the_provider_once_and_waits_for_verification(world):
    request = world.request()
    go(world.run(request))
    assert world.provider.calls == [(NAME, IP)]
    done = control_repo.get_command(world.conn, request.command_id)
    assert done.status == "succeeded"
    assert done.result == {
        "ip_status": "confirmed",
        "records": {NAME: "pending_verification"},
        "updates": {NAME: "accepted"},
    }
    assert [p["status"] for p in world.completed()] == ["succeeded"]


def test_a_repeated_command_id_runs_once_and_returns_the_same_state(world):
    request = world.request()
    first, again = go(world.run(request, request))
    assert first[1] is True and again[1] is False
    assert world.provider.calls == [(NAME, IP)]
    assert len(world.completed()) == 1
    # También más tarde, ya terminado: mismo estado y resultado, sin reejecutar.
    row, new = world.executor.submit(request)
    assert (
        new is False and row.status == "succeeded" and row.result["updates"] == {NAME: "accepted"}
    )
    assert world.provider.calls == [(NAME, IP)]


def test_the_same_id_with_another_type_is_a_conflict(world):
    request = world.request("sync_ddns")
    go(world.run(request))
    with pytest.raises(CommandError) as exc:
        world.executor.submit(world.request("check_dns", command_id=request.command_id))
    assert (exc.value.status, exc.value.code) == (409, "command_conflict")


def test_an_expired_command_is_not_executed_but_is_announced(world):
    request = world.request(expires_at="2026-10-19T11:59:59Z")
    [(row, new)] = go(world.run(request))
    assert new is False and row.status == "expired"
    assert world.provider.calls == []
    assert [p["status"] for p in world.completed()] == ["expired"]


def test_a_command_that_expires_while_queued_is_not_executed(world):
    async def scenario():
        request = world.request(expires_at="2026-10-19T12:00:30Z")
        world.executor.submit(request)
        world.now = NOW + timedelta(minutes=1)  # llega la hora antes de que se ejecute
        worker = asyncio.create_task(world.executor.run())
        await world.executor.join()
        worker.cancel()
        return request

    request = go(scenario())
    assert control_repo.get_command(world.conn, request.command_id).status == "expired"
    assert world.provider.calls == []


@pytest.mark.parametrize(
    "kwargs,code",
    [
        ({"type": "wake_device"}, "unsupported_command"),
        ({"type": "rm_rf"}, "unsupported_command"),
        ({"parameters": {"x": 1}}, "invalid_command"),
        ({"resource_id": "nas-1"}, "invalid_command"),
    ],
)
def test_unsupported_or_parameterised_commands_are_rejected_and_not_stored(world, kwargs, code):
    request = world.request(**kwargs)
    with pytest.raises(CommandError) as exc:
        world.executor.submit(request)
    assert (exc.value.status, exc.value.code) == (422, code)
    assert control_repo.get_command(world.conn, request.command_id) is None


def test_a_full_queue_answers_429_and_stores_nothing(tmp_path):
    world = World(tmp_path, queue_size=2)
    world.executor.submit(world.request())
    world.executor.submit(world.request())
    third = world.request()
    with pytest.raises(CommandError) as exc:
        world.executor.submit(third)
    assert (exc.value.status, exc.value.code) == (429, "queue_full")
    assert control_repo.get_command(world.conn, third.command_id) is None


def test_commands_run_one_at_a_time_in_order(world):
    world.observer.delay = 0.05
    first, second = world.request("check_dns"), world.request("check_dns")
    go(world.run(first, second))
    starts_ends = [kind for kind, _ in world.observer.log]
    # Cada comando consulta 2 registros en paralelo: start, start, end, end, y luego el siguiente.
    assert starts_ends == ["start", "start", "end", "end"] * 2
    ids = [p["command_id"] for p in world.completed()]
    assert ids == [first.command_id, second.command_id]


def test_sync_ddns_is_busy_while_a_cycle_holds_the_lock(world):
    request = world.request()
    with try_lock(world.lock) as taken:
        assert taken
        go(world.run(request))
    done = control_repo.get_command(world.conn, request.command_id)
    assert (done.status, done.result) == ("failed", {"code": "busy"})
    assert world.provider.calls == []


def test_sync_ddns_without_a_confirmed_ip_fails(tmp_path):
    world = World(tmp_path, ip=None)
    request = world.request()
    go(world.run(request))
    done = control_repo.get_command(world.conn, request.command_id)
    assert (done.status, done.result["code"]) == ("failed", "ip_uncertain")
    assert world.provider.calls == []


def test_a_provider_failure_marks_the_command_failed_with_the_safe_code(world):
    world.provider.result = UpdateResult(False, error_code="http_error", retryable=True)
    request = world.request()
    go(world.run(request))
    done = control_repo.get_command(world.conn, request.command_id)
    assert (done.status, done.result["code"]) == ("failed", "provider_failed")
    assert done.result["updates"] == {NAME: "http_error"}


def test_sync_ddns_with_no_managed_records_succeeds_with_nothing(world):
    world.holder.config = DdnsConfig.model_validate(
        {
            "ip_sources": {"urls": ["https://a.example/ip", "https://b.example/ip"]},
            "dns": {"resolvers": ["1.1.1.1"]},
            "records": [{"name": "w.example.com", "mode": "monitored"}],
        }
    )
    request = world.request()
    go(world.run(request))
    done = control_repo.get_command(world.conn, request.command_id)
    assert (done.status, done.result["records"]) == ("succeeded", {})


def test_an_unexpected_error_fails_the_command_without_leaking_its_text(world, caplog):
    world.observer.error = RuntimeError(f"https://x/?key={CANARY}")
    request = world.request("check_dns")
    go(world.run(request))
    done = control_repo.get_command(world.conn, request.command_id)
    assert (done.status, done.result) == ("failed", {"code": "internal_error"})
    blob = json.dumps(world.completed()) + caplog.text + json.dumps(done.result)
    assert CANARY not in blob


def test_a_big_result_travels_summarised_in_the_event(tmp_path):
    world = World(tmp_path)
    records = [{"name": f"r{i}.example.com", "mode": "monitored"} for i in range(150)]
    world.holder.config = DdnsConfig.model_validate(
        {
            "ip_sources": {"urls": ["https://a.example/ip", "https://b.example/ip"]},
            "dns": {"resolvers": ["1.1.1.1"]},
            "records": records,
        }
    )
    request = world.request("check_dns")
    go(world.run(request))
    [event] = world.completed()
    assert event["result"] == {"code": None, "truncated": True}
    full = control_repo.get_command(world.conn, request.command_id).result
    assert len(full["records"]) == 150


def test_after_a_restart_unfinished_commands_become_interrupted_and_never_rerun(tmp_path):
    first = World(tmp_path)
    received, running, done = (first.request() for _ in range(3))
    for command in (received, running, done):
        control_repo.insert_command(
            first.conn, command.command_id, "sync_ddns", "admin:1", NOW, EXPIRES
        )
    control_repo.set_command_running(first.conn, running.command_id)
    control_repo.finish_command(first.conn, done.command_id, "succeeded", NOW, {})
    first.conn.close()

    second = World(tmp_path)  # el Agent arranca de nuevo sobre la misma BD
    second.executor.recover()
    go(second.run())  # el ejecutor arranca: no hay nada en cola
    states = {
        name: control_repo.get_command(second.conn, c.command_id)
        for name, c in (("received", received), ("running", running), ("done", done))
    }
    assert states["received"].status == "failed" and states["received"].result == {
        "code": "interrupted"
    }
    assert states["running"].status == "failed" and states["running"].result == {
        "code": "interrupted"
    }
    assert states["done"].status == "succeeded"
    assert second.provider.calls == []
    assert sorted(p["command_id"] for p in second.completed()) == sorted(
        [received.command_id, running.command_id]
    )
    # Y reenviar el mismo id no lo reejecuta.
    row, new = second.executor.submit(received)
    assert new is False and row.status == "failed"
    assert second.provider.calls == []
