"""Agent real y Hub real en el mismo proceso (el Hub tras `ASGITransport`)."""

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
from pydantic import SecretStr

from dyndnsarr_agent.db import MIGRATIONS, ddns_repo
from dyndnsarr_agent.ddns.config import DdnsConfig
from dyndnsarr_agent.ddns.cycle import run_cycle
from dyndnsarr_agent.dns.resolver import ResolverAnswer
from dyndnsarr_agent.outbox import loop
from dyndnsarr_agent.outbox.client import HttpHubClient
from dyndnsarr_hub.auth.tokens import generate_token, hash_token
from dyndnsarr_hub.config import Settings
from dyndnsarr_hub.db import MIGRATIONS as HUB_MIGRATIONS
from dyndnsarr_hub.db import agents_repo
from dyndnsarr_hub.main import create_app
from dyndnsarr_shared.db import connect, migrate, transaction

AGENT_ID = "raspberry-home"
NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)

CONFIG = DdnsConfig.model_validate(
    {
        "ip_sources": {"urls": ["https://a.example/ip", "https://b.example/ip"]},
        "dns": {"resolvers": ["1.1.1.1"]},
        "records": [{"name": "home.example.com", "mode": "monitored"}],
    }
)


class Observer:
    async def lookup(self, name):
        return [ResolverAnswer("1.1.1.1", ("9.9.9.9",), None)]


class Link(httpx.AsyncBaseTransport):
    """El enlace Agent → Hub: normal, caído, o con el ACK perdido (el Hub sí guarda)."""

    def __init__(self, hub_app):
        self.inner = httpx.ASGITransport(app=hub_app)
        self.mode = "ok"
        self.requests = 0

    async def handle_async_request(self, request):
        self.requests += 1
        if self.mode == "down":
            raise httpx.ConnectError("hub caído")
        response = await self.inner.handle_async_request(request)
        if self.mode == "lose_ack":
            raise httpx.ReadTimeout("la respuesta se perdió")
        return response


class World:
    def __init__(self, tmp_path, hub_app, token):
        self.agent = connect(tmp_path / "agent.db")
        migrate(self.agent, MIGRATIONS)
        self.hub_db = tmp_path / "hub-data" / "hub.db"
        self.link = Link(hub_app)
        self.token = token
        self.public_ip = "1.0.0.1"

    def client(self, token=None):
        http = httpx.AsyncClient(transport=self.link)
        return HttpHubClient(http, "https://hub.test", AGENT_ID, SecretStr(token or self.token))

    async def ddns_cycle(self, ip=None):
        """Un ciclo DDNS real con una IP pública (`FakeIp`) distinta en cada llamada."""
        self.public_ip = ip or f"1.0.0.{int(self.public_ip.split('.')[-1]) + 1}"
        transport = httpx.MockTransport(lambda r: httpx.Response(200, text=self.public_ip))
        async with httpx.AsyncClient(transport=transport) as http:
            return await run_cycle(self.agent, CONFIG, http, Observer(), lambda: NOW)

    async def deliver(self, token=None):
        client = self.client(token)
        try:
            return await loop.deliver_pending(self.agent, client, lambda: NOW)
        finally:
            await client._client.aclose()

    def hub_events(self):
        conn = sqlite3.connect(self.hub_db)
        try:
            return conn.execute(
                "SELECT event_id, sequence FROM events ORDER BY sequence"
            ).fetchall()
        finally:
            conn.close()


@asynccontextmanager
async def world(tmp_path):
    hub_dir = tmp_path / "hub-data"
    conn = connect(hub_dir / "hub.db")
    migrate(conn, HUB_MIGRATIONS)
    token = generate_token()
    with transaction(conn):
        agents_repo.create_agent(conn, AGENT_ID, hash_token(token), NOW)
    conn.close()
    app = create_app(Settings(data_dir=hub_dir))
    async with app.router.lifespan_context(app):
        w = World(tmp_path, app, token)
        try:
            yield w
        finally:
            w.agent.close()


def agent_events(w):
    return ddns_repo.list_events(w.agent, 1000, newest_first=False)


def test_hub_down_for_several_cycles_then_everything_arrives_once_and_in_order(tmp_path):
    async def go():
        async with world(tmp_path) as w:
            ddns_repo.insert_event(w.agent, "agent_started", NOW, None, {"version": "x"})
            w.link.mode = "down"
            for _ in range(4):  # varios ciclos DDNS con el Hub caído
                await w.ddns_cycle()
                assert await w.deliver() == "connect_error"
            produced = agent_events(w)
            assert len(produced) > 4 and w.hub_events() == []
            assert ddns_repo.count_pending(w.agent) == len(produced)

            w.link.mode = "ok"
            assert await w.deliver() is None
            received = w.hub_events()
            assert [e[0] for e in received] == [e.event_id for e in produced]
            assert [e[1] for e in received] == [e.sequence for e in produced]
            assert ddns_repo.count_pending(w.agent) == 0
            assert await w.deliver() is None and len(w.hub_events()) == len(produced)

    asyncio.run(go())


def test_lost_ack_makes_the_agent_resend_and_the_hub_keeps_one_row(tmp_path):
    async def go():
        async with world(tmp_path) as w:
            await w.ddns_cycle()
            produced = agent_events(w)
            w.link.mode = "lose_ack"
            assert await w.deliver() == "timeout"
            assert len(w.hub_events()) == len(produced)  # el Hub guardó: la respuesta se perdió
            assert ddns_repo.count_pending(w.agent) == len(produced)
            w.link.mode = "ok"
            assert await w.deliver() is None
            assert len(w.hub_events()) == len(produced)
            assert ddns_repo.count_pending(w.agent) == 0

    asyncio.run(go())


def test_manual_resend_of_the_same_batch_keeps_one_row(tmp_path):
    async def go():
        async with world(tmp_path) as w:
            await w.ddns_cycle()
            produced = agent_events(w)
            assert await w.deliver() is None
            with transaction(w.agent):  # el operador devuelve los eventos a pendientes
                w.agent.execute("UPDATE events SET delivered_at = NULL")
            assert await w.deliver() is None
            assert len(w.hub_events()) == len(produced)

    asyncio.run(go())


def test_a_rejected_event_is_marked_and_does_not_block_the_following_ones(tmp_path):
    async def go():
        async with world(tmp_path) as w:
            ddns_repo.insert_event(w.agent, "agent_started", NOW, None, {"version": "x"})
            huge = ddns_repo.insert_event(w.agent, "ip_changed", NOW, None, {"blob": "x" * 5000})
            last = ddns_repo.insert_event(w.agent, "ip_changed", NOW, None, {"ip": "1.2.3.4"})
            assert await w.deliver() is None
            ids = [e[0] for e in w.hub_events()]
            assert huge.event_id not in ids and last.event_id in ids and len(ids) == 2
            assert ddns_repo.count_rejected(w.agent) == 1 and ddns_repo.count_pending(w.agent) == 0
            code = w.agent.execute(
                "SELECT rejected_code FROM events WHERE event_id = ?", (huge.event_id,)
            ).fetchone()[0]
            assert code == "payload_too_large"
            requests = w.link.requests
            assert await w.deliver() is None and w.link.requests == requests  # no se reenvía

    asyncio.run(go())


def test_a_revoked_token_retains_events_and_a_corrected_token_delivers_them(tmp_path):
    async def go():
        async with world(tmp_path) as w:
            await w.ddns_cycle()
            produced = agent_events(w)
            fresh = generate_token()
            hub = connect(w.hub_db)
            with transaction(hub):
                agents_repo.add_token(hub, AGENT_ID, hash_token(fresh), NOW)
                agents_repo.revoke(hub, AGENT_ID, NOW, everything=False)  # deja solo `fresh`
            hub.close()
            assert await w.deliver() == "auth_failed"
            assert w.hub_events() == []
            assert ddns_repo.count_pending(w.agent) == len(produced)
            assert await w.deliver(token=fresh) is None
            assert len(w.hub_events()) == len(produced)

    asyncio.run(go())


def test_a_dead_hub_does_not_change_what_the_ddns_cycle_does(tmp_path):
    async def run(link_mode):
        async with world(tmp_path / link_mode) as w:
            w.link.mode = link_mode
            results = []
            for _ in range(3):
                result = await w.ddns_cycle()
                await w.deliver()
                results.append((result.ip_status, result.public_ip, dict(result.records)))
            return results, [(e.type, e.record_name) for e in agent_events(w)]

    ok = asyncio.run(run("ok"))
    down = asyncio.run(run("down"))
    assert ok == down
