"""Refresco del estado para la vista pública (slice 4, D1 y D5), con un Agent real en proceso."""

import asyncio
import logging
import ssl
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from dyndnsarr_agent.config import Settings as AgentSettings
from dyndnsarr_agent.dns.resolver import ResolverAnswer
from dyndnsarr_agent.main import create_app as create_agent
from dyndnsarr_hub.auth.tokens import generate_api_token, generate_token, hash_token
from dyndnsarr_hub.config import Settings as HubSettings
from dyndnsarr_hub.db import MIGRATIONS, agents_repo, public_repo
from dyndnsarr_hub.main import create_app as create_hub
from dyndnsarr_hub.services import status_refresher
from dyndnsarr_shared.db import connect, migrate, transaction

AGENT_ID = "raspberry-home"
CANARY = "83.45.12.134"
API_TOKEN_MARK = "ddh_canary-token"
TOML = """
[ip_sources]
urls = ["https://a.example/ip", "https://b.example/ip"]
[dns]
resolvers = ["1.1.1.1"]
[[records]]
name = "home.example.com"
mode = "monitored"
"""


class Observer:
    """Un DNS que ya apunta a la IP: el registro queda `verified`."""

    async def lookup(self, name):
        return [ResolverAnswer("1.1.1.1", (CANARY,), None)]


def utc():
    return datetime.now(UTC)


@asynccontextmanager
async def stack(tmp_path, api_token=None):
    hub_dir = tmp_path / "hub-data"
    conn = connect(hub_dir / "hub.db")
    migrate(conn, MIGRATIONS)
    api_token = api_token or generate_api_token()
    with transaction(conn):
        agents_repo.create_agent(conn, AGENT_ID, hash_token(generate_token()), utc())
    conn.close()
    toml = tmp_path / "agent.toml"
    toml.write_text(TOML, encoding="utf-8")
    hub_app = create_hub(HubSettings(data_dir=hub_dir))
    agent_app = create_agent(
        AgentSettings(data_dir=tmp_path / "agent-data", config_file=toml, api_token=api_token),
        observer=Observer(),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=CANARY)),
    )
    async with hub_app.router.lifespan_context(hub_app):
        async with agent_app.router.lifespan_context(agent_app):
            deadline = time.monotonic() + 5
            while not agent_app.state.db.execute(
                "SELECT 1 FROM events WHERE type = 'dns_verified'"
            ).fetchall():
                assert time.monotonic() < deadline
                await asyncio.sleep(0.02)
            # La API se configura ya arrancado el Hub: su tarea real hizo su primera pasada sin
            # agentes con API y no toca la red durante la prueba (las pasadas son a mano).
            hub = hub_app.state.db
            with transaction(hub):
                agents_repo.set_api_url(hub, AGENT_ID, "https://agent.test")
                agents_repo.set_api_token(hub, AGENT_ID, api_token)
            yield hub, agent_app


def http_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def dump_public(conn):
    return "".join(
        str(r)
        for table in ("public_renewals", "public_agent_status", "public_status_samples")
        for r in conn.execute(f"SELECT * FROM {table}")
    )


def samples(conn):
    return conn.execute("SELECT state FROM public_status_samples ORDER BY sampled_at").fetchall()


def test_a_healthy_agent_is_projected_without_its_full_ip(tmp_path):
    async def go():
        api_token = generate_api_token()
        async with stack(tmp_path, api_token) as (hub, agent_app):
            http = httpx.AsyncClient(transport=httpx.ASGITransport(app=agent_app))
            async with http:
                outcome = await status_refresher.refresh_once(hub, http, 60)
            assert outcome == {AGENT_ID: None}
            [projection] = public_repo.enabled_agent_statuses(hub)
            assert projection.ip_status == "confirmed"
            assert [(r.name, r.verification) for r in projection.records] == [
                ("home.example.com", "verified")
            ]
            assert samples(hub) == [("healthy",)]
            assert CANARY not in dump_public(hub) and api_token not in dump_public(hub)

    asyncio.run(go())


@pytest.mark.parametrize(
    ("handler", "code"),
    [
        (lambda r: httpx.Response(401, json={}), "auth_failed"),
        (lambda r: httpx.Response(503), "server_error"),
        (lambda r: httpx.Response(200, text="no-json"), "bad_response"),
    ],
)
def test_a_failed_refresh_keeps_refreshed_at_and_leaves_an_unknown_sample(tmp_path, handler, code):
    async def go():
        async with stack(tmp_path) as (hub, agent_app):
            good = httpx.AsyncClient(transport=httpx.ASGITransport(app=agent_app))
            async with good:
                await status_refresher.refresh_once(hub, good, 60)
            before = hub.execute("SELECT refreshed_at FROM public_agent_status").fetchone()
            await asyncio.sleep(1.1)
            async with http_for(handler) as bad:
                outcome = await status_refresher.refresh_once(hub, bad, 60)
            assert outcome == {AGENT_ID: code}
            assert hub.execute("SELECT refreshed_at FROM public_agent_status").fetchone() == before
            assert samples(hub)[-1] == ("unknown",)

    asyncio.run(go())


def test_connection_and_tls_errors_are_safe_codes(tmp_path):
    def down(request):
        raise httpx.ConnectError("agent.test canario-url")

    def bad_tls(request):
        error = httpx.ConnectError("x")
        error.__cause__ = ssl.SSLError("certificate verify failed")
        raise error

    async def go():
        async with stack(tmp_path) as (hub, _):
            codes = []
            for handler in (down, bad_tls):
                async with http_for(handler) as http:
                    codes.append(await status_refresher.refresh_once(hub, http, 60))
            assert codes == [{AGENT_ID: "connect_error"}, {AGENT_ID: "tls_error"}]
            assert hub.execute("SELECT count(*) FROM public_agent_status").fetchone() == (0,)

    asyncio.run(go())


def test_agents_without_api_or_disabled_are_skipped(tmp_path):
    async def go():
        async with stack(tmp_path) as (hub, agent_app):
            with transaction(hub):
                agents_repo.create_agent(hub, "no-api", hash_token("dda_a"), utc())
                agents_repo.create_agent(hub, "revoked", hash_token("dda_b"), utc())
                agents_repo.set_api_url(hub, "revoked", "https://other.test")
                agents_repo.set_api_token(hub, "revoked", "ddh_x")
                agents_repo.revoke(hub, "revoked", utc(), everything=True)
            seen = []

            def handler(request):
                seen.append(request.url.host)
                return httpx.Response(503)

            async with http_for(handler) as http:
                outcome = await status_refresher.refresh_once(hub, http, 60)
            assert outcome == {AGENT_ID: "server_error"} and seen == ["agent.test"]
            assert hub.execute(
                "SELECT DISTINCT agent_id FROM public_status_samples"
            ).fetchall() == [(AGENT_ID,)]

    asyncio.run(go())


def test_old_samples_are_purged_on_refresh(tmp_path):
    async def go():
        async with stack(tmp_path) as (hub, _):
            with transaction(hub):
                public_repo.add_sample(hub, AGENT_ID, utc() - timedelta(days=31), "healthy")
                public_repo.add_sample(hub, AGENT_ID, utc() - timedelta(days=29), "healthy")
            async with http_for(lambda r: httpx.Response(503)) as http:
                await status_refresher.refresh_once(hub, http, 60)
            assert len(samples(hub)) == 2

    asyncio.run(go())


def test_without_a_tls_client_every_agent_fails_with_tls_config(tmp_path):
    async def go():
        async with stack(tmp_path) as (hub, _):
            assert await status_refresher.refresh_once(hub, None, 60) == {AGENT_ID: "tls_config"}
            assert samples(hub) == [("unknown",)]

    asyncio.run(go())


def test_the_loop_survives_errors_logs_only_safe_codes_and_stops_when_cancelled(tmp_path, caplog):
    async def go():
        async with stack(tmp_path) as (hub, _):
            passes = []
            calls = {"n": 0}

            def clock():
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("fallo canario-0123")
                return utc()

            async def sleep(seconds):
                passes.append(seconds)
                if len(passes) >= 3:
                    await asyncio.Event().wait()

            def down(request):
                raise httpx.ConnectError("canario-url")

            task = asyncio.create_task(
                status_refresher.run_forever(
                    hub,
                    None,
                    15,
                    transport=httpx.MockTransport(down),
                    clock=clock,
                    sleep=sleep,
                )
            )
            deadline = time.monotonic() + 5
            while len(passes) < 3:
                assert time.monotonic() < deadline
                await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert passes == [15, 15, 15]

    with caplog.at_level(logging.INFO):
        asyncio.run(go())
    assert "canario-0123" not in caplog.text and "canario-url" not in caplog.text
    assert "agent.test" not in caplog.text
    failures = [r for r in caplog.records if r.message == "status_refresh_failed"]
    assert len(failures) == 1 and failures[0].code == "connect_error"
    errors = [r for r in caplog.records if r.message == "status_refresh_error"]
    assert [r.error_type for r in errors] == ["RuntimeError"]


def test_a_missing_ca_file_does_not_stop_the_loop(tmp_path):
    async def go():
        async with stack(tmp_path) as (hub, _):
            done = asyncio.Event()

            async def sleep(seconds):
                done.set()
                await asyncio.Event().wait()

            task = asyncio.create_task(
                status_refresher.run_forever(hub, tmp_path / "no-ca.pem", 15, sleep=sleep)
            )
            await asyncio.wait_for(done.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert samples(hub)[-1] == ("unknown",)

    asyncio.run(go())
