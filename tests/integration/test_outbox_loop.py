import asyncio
import logging
from datetime import UTC, datetime

import pytest

from dyndnsarr_agent.db import MIGRATIONS, ddns_repo
from dyndnsarr_agent.outbox import loop
from dyndnsarr_agent.outbox.client import DeliveryResult
from dyndnsarr_shared.db import connect, migrate

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


class FakeHub:
    """Un `HubClient` de pruebas: acepta todo salvo lo que se le indique."""

    def __init__(self):
        self.batches: list[list[int]] = []
        self.errors: list[str | Exception] = []  # se consumen uno por llamada
        self.reject: dict[str, str] = {}
        self.stored: list[str] = []

    async def send(self, events):
        self.batches.append([e.sequence for e in events])
        if self.errors:
            error = self.errors.pop(0)
            if isinstance(error, Exception):
                raise error
            return DeliveryResult(error_code=error)
        acked, rejected = [], []
        for e in events:
            if e.event_id in self.reject:
                rejected.append((e.event_id, self.reject[e.event_id]))
            else:
                acked.append(e.event_id)
                if e.event_id not in self.stored:
                    self.stored.append(e.event_id)
        return DeliveryResult(acked=acked, rejected=rejected)


@pytest.fixture
def conn(tmp_path):
    conn = connect(tmp_path / "agent.db")
    migrate(conn, MIGRATIONS)
    return conn


def add_events(conn, n):
    return [ddns_repo.insert_event(conn, "ip_changed", NOW, None, {"n": i}) for i in range(n)]


def deliver(conn, hub):
    return asyncio.run(loop.deliver_pending(conn, hub, lambda: NOW))


def test_no_pending_means_no_network(conn):
    hub = FakeHub()
    assert deliver(conn, hub) is None
    assert hub.batches == []


def test_delivers_in_sequence_order_in_batches_of_50(conn):
    events = add_events(conn, 120)
    hub = FakeHub()
    assert deliver(conn, hub) is None
    assert [len(b) for b in hub.batches] == [50, 50, 20]
    assert [s for b in hub.batches for s in b] == [e.sequence for e in events]
    assert ddns_repo.count_pending(conn) == 0
    assert ddns_repo.get_state(conn, "outbox_last_success_at") == "2026-10-19T12:00:00Z"


def test_events_are_marked_only_after_the_ack(conn):
    add_events(conn, 3)
    hub = FakeHub()
    hub.errors = ["timeout"]
    assert deliver(conn, hub) == "timeout"
    assert ddns_repo.count_pending(conn) == 3
    assert ddns_repo.get_state(conn, "outbox_last_error_code") == "timeout"
    assert ddns_repo.get_state(conn, "outbox_last_error_at") == "2026-10-19T12:00:00Z"
    assert deliver(conn, hub) is None
    assert ddns_repo.count_pending(conn) == 0


def test_a_failure_in_the_second_batch_keeps_the_first_delivered(conn):
    add_events(conn, 60)
    hub = FakeHub()
    original_send = hub.send
    calls = []

    async def send(events):
        calls.append(1)
        if len(calls) == 2:
            return DeliveryResult(error_code="server_error")
        return await original_send(events)

    hub.send = send
    assert deliver(conn, hub) == "server_error"
    assert ddns_repo.count_pending(conn) == 10


def test_a_rejected_event_is_marked_and_does_not_block_the_next_ones(conn):
    events = add_events(conn, 3)
    hub = FakeHub()
    hub.reject[events[1].event_id] = "invalid_event"
    assert deliver(conn, hub) is None
    assert ddns_repo.count_rejected(conn) == 1 and ddns_repo.count_pending(conn) == 0
    assert hub.stored == [events[0].event_id, events[2].event_id]
    assert deliver(conn, hub) is None
    assert len(hub.batches) == 1  # lo rechazado no se reenvía


def test_a_hub_that_acknowledges_nothing_is_bad_response_not_an_endless_loop(conn):
    add_events(conn, 2)

    class Silent:
        async def send(self, events):
            return DeliveryResult()

    assert deliver(conn, Silent()) == "bad_response"
    assert ddns_repo.count_pending(conn) == 2


def test_backoff_is_30_60_120_up_to_900():
    assert [loop.backoff_seconds(n) for n in range(1, 9)] == [30, 60, 120, 240, 480, 900, 900, 900]


def test_jitter_stays_within_20_percent():
    assert all(0.8 <= loop.random_jitter() <= 1.2 for _ in range(200))


class Stop(Exception):
    pass


def run_loop(conn, hub, iterations, jitter=lambda: 1.0):
    """Corre `run_forever` con `sleep` inyectado: devuelve las esperas pedidas."""
    waits = []

    async def sleep(seconds):
        waits.append(seconds)
        if len(waits) >= iterations:
            raise Stop

    async def go():
        await loop.run_forever(conn, hub, lambda: NOW, sleep, jitter)

    with pytest.raises(Stop):
        asyncio.run(go())
    return waits


def test_waits_30s_when_idle_and_backs_off_on_failures_and_recovers(conn):
    add_events(conn, 1)
    hub = FakeHub()
    hub.errors = ["connect_error"] * 7
    waits = run_loop(conn, hub, 9)
    # 7 fallos: 30,60,120,240,480,900,900; luego entrega (30) y reposo (30).
    assert waits == [30, 60, 120, 240, 480, 900, 900, 30, 30]
    assert len(hub.batches) == 8  # el noveno ciclo no tiene pendientes: sin red


def test_jitter_scales_the_backoff_but_not_the_idle_wait(conn):
    add_events(conn, 1)
    hub = FakeHub()
    hub.errors = ["timeout"]
    assert run_loop(conn, hub, 2, jitter=lambda: 1.2) == [36.0, 30.0]


def test_the_streak_is_logged_once_and_the_recovery_once(conn, caplog):
    caplog.set_level(logging.INFO)
    add_events(conn, 1)
    hub = FakeHub()
    hub.errors = ["server_error"] * 4
    run_loop(conn, hub, 6)
    messages = [r.message for r in caplog.records if r.name == loop.__name__]
    assert messages == ["outbox_delivery_failed", "outbox_recovered"]


def test_auth_failure_is_logged_as_error_once_and_keeps_retrying(conn, caplog):
    caplog.set_level(logging.INFO)
    add_events(conn, 1)
    hub = FakeHub()
    hub.errors = ["auth_failed"] * 3
    run_loop(conn, hub, 4)
    failed = [r for r in caplog.records if r.message == "outbox_delivery_failed"]
    assert [r.levelno for r in failed] == [logging.ERROR] and failed[0].code == "auth_failed"
    assert ddns_repo.count_pending(conn) == 0  # al corregirse, se entrega


def test_an_unexpected_exception_does_not_stop_the_loop(conn, caplog):
    add_events(conn, 1)
    hub = FakeHub()
    hub.errors = [RuntimeError("detalle interno")]
    waits = run_loop(conn, hub, 2)
    assert waits == [30, 30]
    assert "detalle interno" not in caplog.text
    assert ddns_repo.count_pending(conn) == 0
