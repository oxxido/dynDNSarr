import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from dyndnsarr_agent.db import MIGRATIONS, ddns_repo
from dyndnsarr_agent.ddns.config import DdnsConfig
from dyndnsarr_agent.ddns.cycle import run_cycle
from dyndnsarr_agent.dns.resolver import ResolverAnswer
from dyndnsarr_shared.db import connect, migrate

A, B = "1.2.3.4", "5.6.7.8"
NAME = "home.example.com"
T0 = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


class World:
    """Estado simulado de Internet: lo que dicen las fuentes de IP y los resolvers."""

    def __init__(self) -> None:
        self.sources = {"a.example": A, "b.example": A}  # host -> IP, o None si está caída
        self.dns: dict[str, list[ResolverAnswer]] = {}
        self.now = T0

    def handler(self, request: httpx.Request) -> httpx.Response:
        ip = self.sources[request.url.host]
        if ip is None:
            raise httpx.ConnectError("down")
        return httpx.Response(200, text=ip)

    def set_ip(self, ip):
        self.sources = {host: ip for host in self.sources}

    def set_dns(self, name, *ips):
        self.dns[name] = [
            ResolverAnswer(r, tuple(sorted(ips)), None) for r in ("1.1.1.1", "8.8.8.8")
        ]

    def dns_down(self, name, code="timeout"):
        self.dns[name] = [ResolverAnswer(r, (), code) for r in ("1.1.1.1", "8.8.8.8")]

    async def lookup(self, name):
        return self.dns[name]

    def clock(self):
        return self.now


def make_config(*records, **retention):
    return DdnsConfig.model_validate(
        {
            "ip_sources": {"urls": ["https://a.example/ip", "https://b.example/ip"]},
            "dns": {"resolvers": ["1.1.1.1", "8.8.8.8"]},
            "retention": retention,
            "records": [{"name": n, "mode": "monitored", **extra} for n, extra in records],
        }
    )


@pytest.fixture
def conn(tmp_path):
    conn = connect(tmp_path / "agent.db")
    migrate(conn, MIGRATIONS)
    return conn


@pytest.fixture
def world():
    w = World()
    w.set_dns(NAME, A)
    return w


def cycle(conn, world, config=None, advance=300):
    world.now += timedelta(seconds=advance)
    config = config or make_config((NAME, {}))

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(world.handler)) as client:
            return await run_cycle(conn, config, client, world, world.clock)

    return asyncio.run(go())


def types(conn):
    return [(e.type, e.record_name) for e in ddns_repo.list_events(conn, 100, newest_first=False)]


def test_first_cycle_confirms_ip_and_verifies_record(conn, world):
    result = cycle(conn, world)
    assert (result.ip_status, result.public_ip) == ("confirmed", A)
    assert result.records == {NAME: "verified"}
    assert types(conn) == [("ip_changed", None), ("dns_verified", NAME)]
    first = ddns_repo.list_events(conn, 1, newest_first=False)[0]
    assert first.payload == {"previous": None, "ip": A}
    assert ddns_repo.get_state(conn, "public_ip") == A
    assert ddns_repo.get_state(conn, "public_ip_confirmed_at") is not None
    assert ddns_repo.get_record_state(conn, NAME).verification == "verified"


def test_unchanged_ip_and_dns_produce_no_new_events(conn, world):
    cycle(conn, world)
    before = types(conn)
    for _ in range(3):
        cycle(conn, world)
    assert types(conn) == before


def test_ip_change_emits_ip_changed_and_one_mismatch(conn, world):
    cycle(conn, world)
    world.set_ip(B)
    cycle(conn, world)
    cycle(conn, world)  # el mismatch persiste: no se repite el evento
    assert types(conn)[2:] == [("ip_changed", None), ("dns_mismatch", NAME)]
    mismatch, changed = ddns_repo.list_events(conn, 2)
    assert changed.payload == {"previous": A, "ip": B}
    assert mismatch.payload["expected"] == B
    assert mismatch.payload["observed"] == {"1.1.1.1": A, "8.8.8.8": A}
    assert ddns_repo.get_state(conn, "public_ip") == B


def test_record_recovers_when_dns_catches_up(conn, world):
    cycle(conn, world)
    world.set_ip(B)
    cycle(conn, world)
    world.set_dns(NAME, B)
    cycle(conn, world)
    cycle(conn, world)
    assert types(conn)[-1] == ("dns_verified", NAME)
    assert [t for t, _ in types(conn)].count("dns_verified") == 2


def test_disagreeing_sources_are_uncertain_and_emit_once(conn, world):
    cycle(conn, world)
    world.sources = {"a.example": A, "b.example": B}
    first = cycle(conn, world)
    cycle(conn, world)
    assert (first.ip_status, first.public_ip) == ("uncertain", None)
    assert types(conn)[2:] == [("ip_uncertain", None)]
    assert ddns_repo.list_events(conn, 1)[0].payload == {"reason": "disagreement"}
    # no hay cambio de IP: la última confirmada se conserva
    assert ddns_repo.get_state(conn, "public_ip") == A


def test_source_down_is_uncertain(conn, world):
    world.sources["b.example"] = None
    result = cycle(conn, world)
    assert result.ip_status == "uncertain"
    assert types(conn) == [("ip_uncertain", None)]
    assert ddns_repo.list_events(conn, 1)[0].payload == {"reason": "insufficient_sources"}


def test_resuming_with_the_same_ip_emits_nothing_and_a_new_outage_emits_again(conn, world):
    cycle(conn, world)
    world.sources = {"a.example": A, "b.example": B}
    cycle(conn, world)
    world.set_ip(A)
    resumed = cycle(conn, world)
    assert resumed.ip_status == "confirmed"
    assert types(conn)[2:] == [("ip_uncertain", None)]
    world.sources["b.example"] = None
    cycle(conn, world)
    assert types(conn)[2:] == [("ip_uncertain", None), ("ip_uncertain", None)]


def test_resuming_with_a_new_ip_emits_ip_changed(conn, world):
    cycle(conn, world)
    world.sources = {"a.example": A, "b.example": B}
    cycle(conn, world)
    world.set_ip(B)
    cycle(conn, world)
    assert types(conn)[2:4] == [("ip_uncertain", None), ("ip_changed", None)]


def test_uncertain_ip_saves_dns_checks_but_does_not_reclassify(conn, world):
    cycle(conn, world)
    world.sources = {"a.example": A, "b.example": B}
    world.set_dns(NAME, B)  # habría sido mismatch con la IP A
    result = cycle(conn, world)
    assert result.records == {NAME: "verified"}
    assert ddns_repo.get_record_state(conn, NAME).verification == "verified"
    assert conn.execute("SELECT count(*) FROM dns_checks").fetchone()[0] == 4


def test_record_without_answers_is_unknown_without_event(conn, world):
    world.dns_down(NAME, "nxdomain")
    result = cycle(conn, world)
    assert result.records == {NAME: "unknown"}
    assert types(conn) == [("ip_changed", None)]
    assert ddns_repo.get_record_state(conn, NAME).verification == "unknown"
    world.set_dns(NAME, A)
    cycle(conn, world)
    assert types(conn)[-1] == ("dns_verified", NAME)


def test_samples_are_recorded_every_cycle_including_errors(conn, world):
    world.dns_down(NAME, "timeout")
    cycle(conn, world)
    cycle(conn, world)
    assert conn.execute("SELECT count(*) FROM public_ip_observations").fetchone()[0] == 4
    rows = conn.execute("SELECT resolver, ip, error_code FROM dns_checks").fetchall()
    assert len(rows) == 4
    assert set(rows) == {("1.1.1.1", None, "timeout"), ("8.8.8.8", None, "timeout")}


def test_dns_check_stores_the_answered_addresses(conn, world):
    world.set_dns(NAME, B, A)
    cycle(conn, world)
    ips = {r[0] for r in conn.execute("SELECT ip FROM dns_checks")}
    assert ips == {f"{A},{B}"}


def test_disabled_records_are_not_observed(conn, world):
    world.dns["off.example.com"] = []
    config = make_config((NAME, {}), ("off.example.com", {"enabled": False}))
    result = cycle(conn, world, config)
    assert list(result.records) == [NAME]
    assert ddns_repo.get_record_state(conn, "off.example.com") is None


def test_purge_runs_at_the_end_of_each_cycle(conn, world):
    ddns_repo.insert_ip_observation(conn, T0 - timedelta(days=40), "old-source", A, None)
    ddns_repo.insert_ip_observation(conn, T0 - timedelta(days=1), "recent-source", A, None)
    ddns_repo.insert_event(conn, "agent_started", T0 - timedelta(days=400), None, {})
    cycle(conn, world)
    sources = {r[0] for r in conn.execute("SELECT source FROM public_ip_observations")}
    assert "old-source" not in sources
    assert "recent-source" in sources
    assert "agent_started" not in [t for t, _ in types(conn)]


def test_a_failing_observer_leaves_the_database_untouched(conn, world):
    class Broken:
        async def lookup(self, name):
            raise RuntimeError("boom")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(world.handler)) as client:
            await run_cycle(conn, make_config((NAME, {})), client, Broken(), world.clock)

    with pytest.raises(RuntimeError):
        asyncio.run(go())
    assert types(conn) == []
    assert conn.execute("SELECT count(*) FROM public_ip_observations").fetchone()[0] == 0
    assert ddns_repo.get_state(conn, "public_ip") is None
