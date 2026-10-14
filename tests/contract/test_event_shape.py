import asyncio
import uuid
from datetime import UTC, datetime

import httpx

from dyndnsarr_agent.db import MIGRATIONS, ddns_repo
from dyndnsarr_agent.ddns.config import DdnsConfig
from dyndnsarr_agent.ddns.cycle import run_cycle
from dyndnsarr_agent.dns.resolver import ResolverAnswer
from dyndnsarr_shared.db import connect, migrate


class Observer:
    async def lookup(self, name):
        return [ResolverAnswer("1.1.1.1", ("5.6.7.8",), None)]


def test_events_follow_the_architecture_section_5_shape(tmp_path):
    conn = connect(tmp_path / "agent.db")
    migrate(conn, MIGRATIONS)
    config = DdnsConfig.model_validate(
        {
            "ip_sources": {"urls": ["https://a.example/ip", "https://b.example/ip"]},
            "dns": {"resolvers": ["1.1.1.1"]},
            "records": [{"name": "home.example.com", "mode": "monitored"}],
        }
    )

    async def go():
        transport = httpx.MockTransport(lambda request: httpx.Response(200, text="1.2.3.4"))
        async with httpx.AsyncClient(transport=transport) as client:
            await run_cycle(conn, config, client, Observer(), lambda: datetime.now(UTC))

    asyncio.run(go())
    events = ddns_repo.list_events(conn, 10, newest_first=False)
    assert [e.type for e in events] == ["ip_changed", "dns_mismatch"]
    sequences = [e.sequence for e in events]
    assert sequences == sorted(sequences) and len(set(sequences)) == len(sequences)
    for event in events:
        assert str(uuid.UUID(event.event_id)) == event.event_id
        assert datetime.strptime(event.occurred_at, "%Y-%m-%dT%H:%M:%SZ")
        assert isinstance(event.payload, dict)
        assert event.type in ddns_repo.EVENT_TYPES
