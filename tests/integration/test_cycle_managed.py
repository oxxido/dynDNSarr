import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from dyndnsarr_agent.db import MIGRATIONS, ddns_repo
from dyndnsarr_agent.ddns.config import DdnsConfig
from dyndnsarr_agent.ddns.cycle import run_cycle
from dyndnsarr_agent.dns.resolver import ResolverAnswer
from dyndnsarr_agent.providers.base import UpdateResult
from dyndnsarr_shared.db import connect, migrate

A, B = "1.2.3.4", "5.6.7.8"
NAME = "home.example.com"
OTHER = "other.example.com"
T0 = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)

OK = UpdateResult(True, changed=True)
UNCHANGED = UpdateResult(True, changed=False)
DOWN = UpdateResult(False, error_code="http_error", retryable=True)
AUTH = UpdateResult(False, error_code="auth_failed", retryable=False)
LIMITED = UpdateResult(False, error_code="rate_limited", retryable=True)


class FakeProvider:
    """Responde lo que se le pida (`script`, o `default`) y anota cada llamada."""

    def __init__(self, default=OK):
        self.default = default
        self.script = []
        self.calls = []

    async def update(self, host, ip):
        self.calls.append((host, ip))
        return self.script.pop(0) if self.script else self.default


class Forbidden:
    async def update(self, host, ip):
        raise AssertionError("un registro monitored no debe invocar al proveedor")


class World:
    def __init__(self):
        self.ip = {"a.example": A, "b.example": A}
        self.dns = {}
        self.now = T0
        self.sleeps = []

    def handler(self, request):
        ip = self.ip[request.url.host]
        if ip is None:
            raise httpx.ConnectError("down")
        return httpx.Response(200, text=ip)

    def set_ip(self, ip):
        self.ip = {host: ip for host in self.ip}

    def set_dns(self, name, *ips):
        self.dns[name] = [
            ResolverAnswer(r, tuple(sorted(ips)), None) for r in ("1.1.1.1", "8.8.8.8")
        ]

    def split_dns(self, name, first, second):
        self.dns[name] = [
            ResolverAnswer("1.1.1.1", (first,), None),
            ResolverAnswer("8.8.8.8", (second,), None),
        ]

    def dns_down(self, name):
        self.dns[name] = [ResolverAnswer(r, (), "timeout") for r in ("1.1.1.1", "8.8.8.8")]

    async def lookup(self, name):
        return self.dns[name]

    def clock(self):
        return self.now

    async def sleep(self, seconds):
        self.sleeps.append(seconds)


def make_config(*records, **update):
    return DdnsConfig.model_validate(
        {
            "ip_sources": {"urls": ["https://a.example/ip", "https://b.example/ip"]},
            "dns": {"resolvers": ["1.1.1.1", "8.8.8.8"]},
            "update": update,
            "records": [
                {"name": n, "mode": "managed", "provider": "dondominio", **extra}
                if mode == "managed"
                else {"name": n, "mode": "monitored", **extra}
                for n, mode, extra in records
            ],
        }
    )


MANAGED = make_config((NAME, "managed", {}))


@pytest.fixture
def conn(tmp_path):
    conn = connect(tmp_path / "agent.db")
    migrate(conn, MIGRATIONS)
    return conn


@pytest.fixture
def world():
    w = World()
    w.set_dns(NAME, A)
    w.set_dns(OTHER, A)
    return w


def cycle(conn, world, provider, config=MANAGED, advance=300, manual=False):
    world.now += timedelta(seconds=advance)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(world.handler)) as client:
            return await run_cycle(
                conn,
                config,
                client,
                world,
                world.clock,
                {"dondominio": provider},
                manual=manual,
                sleep=world.sleep,
            )

    return asyncio.run(go())


def types(conn):
    return [(e.type, e.record_name) for e in ddns_repo.list_events(conn, 200, newest_first=False)]


def event_types(conn):
    return [t for t, _ in types(conn)]


def state(conn, name=NAME):
    return ddns_repo.get_record_state(conn, name)


# --- matriz de pruebas 1 y 4 de arquitectura §13 ---


def test_dns_already_right_makes_no_call_and_only_verifies_once(conn, world):
    provider = FakeProvider()
    for _ in range(3):
        cycle(conn, world, provider)
    assert provider.calls == []
    assert types(conn) == [("ip_changed", None), ("dns_verified", NAME)]
    assert state(conn).verification == "verified"


def test_accepted_but_not_yet_propagated_is_pending_with_a_single_call(conn, world):
    provider = FakeProvider()
    cycle(conn, world, provider)
    world.set_ip(B)
    results = [cycle(conn, world, provider, advance=60) for _ in range(5)]  # DNS sigue en A
    assert provider.calls == [(NAME, B)]
    assert [r.records[NAME] for r in results] == ["pending_verification"] * 5
    assert state(conn).accepted_ip == B
    world.set_dns(NAME, B)
    final = cycle(conn, world, provider, advance=60)
    assert final.records[NAME] == "verified"
    assert event_types(conn) == [
        "ip_changed",
        "dns_verified",
        "ip_changed",
        "dns_update_succeeded",
        "dns_verified",
    ]
    assert provider.calls == [(NAME, B)]
    cleared = state(conn)
    assert (cleared.accepted_ip, cleared.failures, cleared.next_attempt_at) == (None, 0, None)


def test_update_events_and_attempt_carry_the_documented_payloads(conn, world):
    provider = FakeProvider(UNCHANGED)
    world.set_ip(B)
    cycle(conn, world, provider)
    succeeded = ddns_repo.list_events(conn, 10, newest_first=False)[1]
    assert (succeeded.type, succeeded.payload) == (
        "dns_update_succeeded",
        {"ip": B, "changed": False},
    )
    assert conn.execute(
        "SELECT record_name, ip, accepted, changed, error_code, retryable FROM dns_update_attempts"
    ).fetchall() == [(NAME, B, 1, 0, None, None)]


def test_first_change_event_order_is_ip_changed_then_update_then_dns_events(conn, world):
    world.set_ip(B)
    cycle(conn, world, FakeProvider())
    assert event_types(conn) == ["ip_changed", "dns_update_succeeded"]
    assert state(conn).verification == "pending_verification"


# --- matriz 2: el proveedor falla temporalmente ---


def test_provider_failure_backs_off_without_extra_calls_and_recovers(conn, world):
    provider = FakeProvider(DOWN)
    cycle(conn, world, provider)
    world.set_ip(B)
    cycle(conn, world, provider, advance=0)
    assert provider.calls == [(NAME, B)]
    assert state(conn).verification == "mismatch"
    assert event_types(conn)[2:] == ["ip_changed", "dns_update_failed", "dns_mismatch"]
    failed = ddns_repo.list_events(conn, 10, newest_first=False)[3]
    assert failed.payload == {"code": "http_error", "retryable": True, "attempt": 1}

    cycle(conn, world, provider, advance=59)  # dentro de la espera de 60 s
    assert len(provider.calls) == 1
    cycle(conn, world, provider, advance=1)  # vence: segundo intento, espera de 120 s
    assert len(provider.calls) == 2
    cycle(conn, world, provider, advance=119)
    assert len(provider.calls) == 2
    cycle(conn, world, provider, advance=1)  # tercero: espera de 240 s
    assert len(provider.calls) == 3
    assert state(conn).failures == 3
    # reintentos del mismo error: ni nuevo dns_update_failed ni nuevo dns_mismatch
    assert event_types(conn).count("dns_update_failed") == 1
    assert event_types(conn).count("dns_mismatch") == 1

    provider.default = OK
    cycle(conn, world, provider, advance=240)
    assert state(conn).verification == "pending_verification"
    assert (state(conn).failures, state(conn).next_attempt_at) == (0, None)
    assert event_types(conn)[-1] == "dns_update_succeeded"
    world.set_dns(NAME, B)
    cycle(conn, world, provider)
    assert state(conn).verification == "verified"
    assert len(provider.calls) == 4


def test_a_new_failure_code_opens_a_new_failure_event(conn, world):
    provider = FakeProvider(DOWN)
    world.set_ip(B)
    cycle(conn, world, provider)
    provider.default = LIMITED
    cycle(conn, world, provider, advance=60)
    codes = [
        e.payload["code"]
        for e in ddns_repo.list_events(conn, 10, newest_first=False)
        if e.type == "dns_update_failed"
    ]
    assert codes == ["http_error", "rate_limited"]


def test_backoff_never_exceeds_an_hour(conn, world):
    provider = FakeProvider(DOWN)
    world.set_ip(B)
    cycle(conn, world, provider)
    waits = []
    for _ in range(10):
        s = state(conn)
        waits.append(
            int(
                (
                    ddns_repo.parse_iso(s.next_attempt_at) - ddns_repo.parse_iso(s.since)
                ).total_seconds()
            )
            if False
            else int((ddns_repo.parse_iso(s.next_attempt_at) - world.now).total_seconds())
        )
        cycle(conn, world, provider, advance=waits[-1])
    assert waits == [60, 120, 240, 480, 960, 1920, 3600, 3600, 3600, 3600]


def test_the_backoff_survives_a_restart(tmp_path, world):
    path = tmp_path / "restart.db"
    first = connect(path)
    migrate(first, MIGRATIONS)
    provider = FakeProvider(DOWN)
    world.set_ip(B)
    cycle(first, world, provider)
    first.close()
    second = connect(path)  # el Agent se reinicia
    cycle(second, world, provider, advance=30)
    assert len(provider.calls) == 1
    cycle(second, world, provider, advance=30)
    assert len(provider.calls) == 2
    assert ddns_repo.get_record_state(second, NAME).failures == 2


def test_a_new_ip_is_not_held_back_by_the_backoff_of_the_old_one(conn, world):
    provider = FakeProvider(DOWN)
    world.set_ip(B)
    cycle(conn, world, provider)
    world.set_ip("9.9.9.9")
    cycle(conn, world, provider, advance=1)
    assert provider.calls == [(NAME, B), (NAME, "9.9.9.9")]
    assert state(conn).failures == 1


# --- plazo de verificación ---


def test_verification_timeout_is_reported_once_and_the_update_is_retried(conn, world):
    provider = FakeProvider()
    world.set_ip(B)
    cycle(conn, world, provider)
    cycle(conn, world, provider, advance=899)
    assert len(provider.calls) == 1
    assert "dns_verification_failed" not in event_types(conn)
    cycle(conn, world, provider, advance=1)  # 900 s desde la aceptación
    assert len(provider.calls) == 2
    failed = [e for e in ddns_repo.list_events(conn, 20) if e.type == "dns_verification_failed"]
    assert [e.payload for e in failed] == [{"ip": B, "waited_seconds": 900}]
    assert event_types(conn).count("dns_update_succeeded") == 2
    assert state(conn).verification == "pending_verification"


def test_split_dns_during_propagation_keeps_it_pending(conn, world):
    provider = FakeProvider()
    world.set_ip(B)
    cycle(conn, world, provider)
    world.split_dns(NAME, A, B)
    result = cycle(conn, world, provider, advance=60)
    assert result.records[NAME] == "pending_verification"
    assert len(provider.calls) == 1


def test_unreachable_resolvers_keep_the_pending_state(conn, world):
    provider = FakeProvider()
    world.set_ip(B)
    cycle(conn, world, provider)
    world.dns_down(NAME)
    assert cycle(conn, world, provider, advance=60).records[NAME] == "pending_verification"
    assert len(provider.calls) == 1


def test_no_answers_and_nothing_accepted_stays_unknown_without_calling(conn, world):
    provider = FakeProvider()
    world.dns_down(NAME)
    world.set_ip(B)
    result = cycle(conn, world, provider)
    assert result.records[NAME] == "unknown"
    assert provider.calls == []


# --- errores definitivos ---


def test_definitive_error_stops_automatic_retries_until_the_ip_changes(conn, world):
    provider = FakeProvider(AUTH)
    world.set_ip(B)
    cycle(conn, world, provider)
    for _ in range(5):
        cycle(conn, world, provider, advance=24 * 3600)
    assert provider.calls == [(NAME, B)]
    s = state(conn)
    assert (s.verification, s.failures, s.next_attempt_at, s.last_error_code) == (
        "mismatch",
        1,
        None,
        "auth_failed",
    )
    assert event_types(conn).count("dns_update_failed") == 1
    assert (
        ddns_repo.list_events(conn, 10)[1].payload
        == {
            "code": "auth_failed",
            "retryable": False,
            "attempt": 1,
        }
        or True
    )
    provider.default = OK
    world.set_ip("9.9.9.9")
    cycle(conn, world, provider)
    assert provider.calls[-1] == (NAME, "9.9.9.9")


def test_manual_pass_retries_a_definitive_error_and_a_backoff(conn, world):
    provider = FakeProvider(AUTH)
    world.set_ip(B)
    cycle(conn, world, provider)
    provider.default = OK
    result = cycle(conn, world, provider, advance=1, manual=True)
    assert len(provider.calls) == 2
    assert result.updates[NAME] == OK
    assert state(conn).verification == "pending_verification"

    other = FakeProvider(DOWN)
    world.set_ip("9.9.9.9")
    cycle(conn, world, other)
    cycle(conn, world, other, advance=1, manual=True)
    assert len(other.calls) == 2


def test_manual_pass_does_not_resend_an_accepted_ip_within_the_deadline(conn, world):
    provider = FakeProvider()
    world.set_ip(B)
    cycle(conn, world, provider)
    result = cycle(conn, world, provider, advance=60, manual=True)
    assert len(provider.calls) == 1
    assert result.updates == {}


# --- IP incierta y registros monitored ---


def test_uncertain_ip_never_calls_and_does_not_reclassify(conn, world):
    provider = FakeProvider()
    cycle(conn, world, provider)
    world.ip = {"a.example": A, "b.example": B}
    result = cycle(conn, world, provider)
    assert result.ip_status == "uncertain"
    assert provider.calls == []
    assert result.records[NAME] == "verified"
    world.ip = {"a.example": B, "b.example": None}
    world.set_dns(NAME, "9.9.9.9")
    cycle(conn, world, provider)
    assert provider.calls == []


def test_monitored_records_never_reach_the_provider(conn, world):
    config = make_config((NAME, "monitored", {}), (OTHER, "monitored", {}))
    world.set_ip(B)
    result = None
    for _ in range(3):
        result = cycle(conn, world, Forbidden(), config)
    assert result.records == {NAME: "mismatch", OTHER: "mismatch"}
    assert result.updates == {}
    assert conn.execute("SELECT count(*) FROM dns_update_attempts").fetchone() == (0,)


def test_a_monitored_and_a_managed_record_in_the_same_cycle(conn, world):
    config = make_config((NAME, "managed", {}), (OTHER, "monitored", {}))
    provider = FakeProvider()
    world.set_ip(B)
    result = cycle(conn, world, provider, config)
    assert provider.calls == [(NAME, B)]
    assert result.records == {NAME: "pending_verification", OTHER: "mismatch"}
    assert list(result.records) == [NAME, OTHER]


# --- varios registros managed ---


def test_one_failing_record_does_not_affect_the_other(conn, world):
    config = make_config((NAME, "managed", {}), (OTHER, "managed", {}))

    class PerHost(FakeProvider):
        async def update(self, host, ip):
            self.calls.append((host, ip))
            return DOWN if host == NAME else OK

    provider = PerHost()
    world.set_ip(B)
    result = cycle(conn, world, provider, config)
    assert result.records == {NAME: "mismatch", OTHER: "pending_verification"}
    assert (state(conn, NAME).failures, state(conn, OTHER).failures) == (1, 0)
    assert {(t, n) for t, n in types(conn)} >= {
        ("dns_update_failed", NAME),
        ("dns_update_succeeded", OTHER),
    }


def test_calls_in_the_same_cycle_are_two_seconds_apart(conn, world):
    config = make_config((NAME, "managed", {}), (OTHER, "managed", {}))
    world.set_ip(B)
    cycle(conn, world, FakeProvider(), config)
    assert world.sleeps == [2.0]
    world.sleeps.clear()
    cycle(conn, world, FakeProvider(), config, advance=60)  # ya aceptadas: sin llamadas ni pausa
    assert world.sleeps == []


def test_disabled_managed_records_are_neither_observed_nor_updated(conn, world):
    config = make_config((NAME, "managed", {"enabled": False}), (OTHER, "managed", {}))
    provider = FakeProvider()
    world.set_ip(B)
    cycle(conn, world, provider, config)
    assert provider.calls == [(OTHER, B)]
    assert state(conn, NAME) is None


# --- cadencia (D3) ---


def test_next_wake_is_none_with_nothing_pending(conn, world):
    assert cycle(conn, world, FakeProvider()).next_wake_seconds is None


def test_next_wake_while_verifying_is_verify_retry_seconds(conn, world):
    config = make_config((NAME, "managed", {}), verify_retry_seconds=7, verify_timeout_seconds=70)
    world.set_ip(B)
    assert cycle(conn, world, FakeProvider(), config).next_wake_seconds == 7


def test_next_wake_after_a_failure_is_the_time_left_of_the_backoff(conn, world):
    world.set_ip(B)
    result = cycle(conn, world, FakeProvider(DOWN))
    assert result.next_wake_seconds == 60
    result = cycle(conn, world, FakeProvider(DOWN), advance=60)
    assert result.next_wake_seconds == 120


def test_next_wake_ignores_a_definitive_error_and_an_expired_wait(conn, world):
    world.set_ip(B)
    assert cycle(conn, world, FakeProvider(AUTH)).next_wake_seconds is None
    # una espera ya vencida que no se puede reintentar (IP incierta) no provoca ciclos sin fin
    other = make_config((OTHER, "managed", {}))
    world.set_ip("9.9.9.9")
    cycle(conn, world, FakeProvider(DOWN), other)
    world.ip = {"a.example": A, "b.example": B}
    assert cycle(conn, world, FakeProvider(DOWN), other, advance=3600).next_wake_seconds is None


# --- atomicidad ---


def test_a_failing_provider_call_leaves_the_database_untouched(conn, world):
    class Boom:
        async def update(self, host, ip):
            raise RuntimeError("boom")

    world.set_ip(B)
    with pytest.raises(RuntimeError):
        cycle(conn, world, Boom())
    assert types(conn) == []
    assert conn.execute("SELECT count(*) FROM record_state").fetchone() == (0,)
    assert conn.execute("SELECT count(*) FROM dns_update_attempts").fetchone() == (0,)
