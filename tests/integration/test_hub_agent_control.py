"""Control Hub ↔ Agent en un solo proceso: cada app con su `lifespan` real y `ASGITransport`.

Los ciclos DDNS y las entregas del outbox se disparan a mano (el daemon espera minutos entre
vueltas); lo que se prueba es lo que la configuración y los comandos hacen sobre ellos.
"""

import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
from pydantic import SecretStr

from dyndnsarr_agent.config import Settings as AgentSettings
from dyndnsarr_agent.db import control_repo as agent_control_repo
from dyndnsarr_agent.db import ddns_repo
from dyndnsarr_agent.ddns.cycle import run_cycle
from dyndnsarr_agent.dns.resolver import ResolverAnswer
from dyndnsarr_agent.main import create_app as create_agent
from dyndnsarr_agent.outbox import loop
from dyndnsarr_agent.outbox.client import HttpHubClient
from dyndnsarr_hub.auth.tokens import generate_api_token, generate_token, hash_token
from dyndnsarr_hub.config import Settings as HubSettings
from dyndnsarr_hub.db import MIGRATIONS as HUB_MIGRATIONS
from dyndnsarr_hub.db import agents_repo, control_repo
from dyndnsarr_hub.main import create_app as create_hub
from dyndnsarr_hub.services.agent_client import AgentClient
from dyndnsarr_shared.control import CommandRequest, ConfigPut, config_hash
from dyndnsarr_shared.db import connect, migrate, transaction

AGENT_ID = "raspberry-home"
NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
IP = "1.2.3.4"
EXPIRES = "2099-01-01T00:00:00Z"

TOML = """
[ip_sources]
urls = ["https://a.example/ip", "https://b.example/ip"]
[dns]
resolvers = ["1.1.1.1"]
[[records]]
name = "old.example.com"
mode = "monitored"
[[records]]
name = "m.example.com"
mode = "managed"
provider = "dondominio"
"""

GOOD = {
    "check_interval_seconds": 120,
    "records": [
        {"name": "new.example.com", "mode": "monitored"},
        {"name": "m.example.com", "mode": "managed", "provider": "dondominio"},
    ],
}


class Observer:
    """Un DNS que aún no apunta a la IP (hay que actualizar los `managed`)."""

    def __init__(self):
        self.names = []

    async def lookup(self, name):
        self.names.append(name)
        return [ResolverAnswer("1.1.1.1", ("9.9.9.9",), None)]


class Link(httpx.AsyncBaseTransport):
    """El enlace Agent → Hub: normal o caído."""

    def __init__(self, hub_app):
        self.inner = httpx.ASGITransport(app=hub_app)
        self.down = False

    async def handle_async_request(self, request):
        if self.down:
            raise httpx.ConnectError("hub caído")
        return await self.inner.handle_async_request(request)


class World:
    def __init__(
        self, tmp_path, hub_app, agent_app, hub_token, api_token, link, provider_calls, public_ip
    ):
        self.hub_app, self.agent_app, self.link = hub_app, agent_app, link
        self.hub_token, self.api_token = hub_token, api_token
        self.provider_calls, self.public_ip = provider_calls, public_ip
        self.observer = agent_app.state.holder.observer
        self.extra = connect(tmp_path / "agent-data" / "agent.db")  # el outbox, como en 3a

    @property
    def hub(self):
        return self.hub_app.state.db

    @property
    def agent(self):
        return self.agent_app.state.db

    @property
    def holder(self):
        return self.agent_app.state.holder

    def api(self):
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.agent_app))
        return http, AgentClient(http, "https://agent.test", self.api_token)

    async def push(self, content, version=None, record=True):
        """Crea la versión siguiente en el Hub y la envía. Devuelve la respuesta del Agent."""
        with transaction(self.hub):
            desired = control_repo.add_desired_version(self.hub, AGENT_ID, content, NOW)
        return await self.send(
            ConfigPut(
                version=version or desired.version, hash=desired.content_hash, content=content
            ),
            record,
        )

    async def send(self, put, record=True):
        http, client = self.api()
        async with http:
            reply = await client.put_config(put)
        if record and reply.value is not None:
            with transaction(self.hub):
                control_repo.record_config_result(
                    self.hub,
                    AGENT_ID,
                    put.version,
                    put.hash,
                    reply.value.result,
                    reply.value.code,
                    NOW,
                )
        return reply

    async def command(self, type="sync_ddns", command_id=None):
        request = CommandRequest(
            command_id=command_id or str(uuid.uuid4()),
            type=type,
            requested_by="admin:cli",
            expires_at=EXPIRES,
        )
        with transaction(self.hub):
            if control_repo.get_command(self.hub, request.command_id) is None:
                control_repo.create_command(
                    self.hub, request.command_id, AGENT_ID, type, "admin:cli", NOW, EXPIRES
                )
        http, client = self.api()
        async with http:
            reply = await client.post_command(request)
        if reply.value is not None:
            with transaction(self.hub):
                control_repo.update_command_from_agent(
                    self.hub, request.command_id, AGENT_ID, reply.value.status, reply.value.result
                )
        return request, reply

    async def wait_command(self, command_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            row = agent_control_repo.get_command(self.agent, command_id)
            if row.status not in ("received", "running"):
                return row
            await asyncio.sleep(0.02)
        raise AssertionError("el comando no terminó")

    async def cycle(self):
        """Un ciclo DDNS como lo hace el scheduler: toma la configuración vigente del holder."""
        h = self.holder
        transport = httpx.MockTransport(lambda r: httpx.Response(200, text=self.public_ip["ip"]))
        async with httpx.AsyncClient(transport=transport) as http:
            return await run_cycle(
                self.agent, h.config, http, h.observer, lambda: NOW, h.providers, sleep=_no_sleep
            )

    async def deliver(self):
        http = httpx.AsyncClient(transport=self.link)
        client = HttpHubClient(http, "https://hub.test", AGENT_ID, SecretStr(self.hub_token))
        try:
            return await loop.deliver_pending(self.extra, client, lambda: NOW)
        finally:
            await http.aclose()

    def observed(self):
        return control_repo.get_observed(self.hub, AGENT_ID)

    def agent_versions(self):
        return self.agent.execute("SELECT version FROM configuration_versions").fetchall()

    def hub_event_types(self):
        return [r[0] for r in self.hub.execute("SELECT type FROM events ORDER BY sequence")]


async def _no_sleep(seconds):
    return None


@asynccontextmanager
async def world(tmp_path):
    hub_dir = tmp_path / "hub-data"
    conn = connect(hub_dir / "hub.db")
    migrate(conn, HUB_MIGRATIONS)
    hub_token, api_token = generate_token(), generate_api_token()
    with transaction(conn):
        agents_repo.create_agent(conn, AGENT_ID, hash_token(hub_token), NOW)
        agents_repo.set_api_url(conn, AGENT_ID, "https://agent.test")
        agents_repo.set_api_token(conn, AGENT_ID, api_token)
    conn.close()
    toml = tmp_path / "agent.toml"
    toml.write_text(TOML, encoding="utf-8")
    provider_calls, public_ip = [], {"ip": IP}

    def sources(request):
        if request.url.host == "dondns.dondominio.com":
            provider_calls.append(dict(request.url.params))
            host = {"value": request.url.params["ip"], "updated": True}
            return httpx.Response(200, json={"success": True, "messages": [], "host": host})
        return httpx.Response(200, text=public_ip["ip"])

    hub_app = create_hub(HubSettings(data_dir=hub_dir))
    link = Link(hub_app)
    agent_app = create_agent(
        AgentSettings(
            data_dir=tmp_path / "agent-data",
            config_file=toml,
            api_token=api_token,
            dondominio_user="user",
            dondominio_key="canary-key",
        ),
        observer=Observer(),
        transport=httpx.MockTransport(sources),
    )
    async with hub_app.router.lifespan_context(hub_app):
        async with agent_app.router.lifespan_context(agent_app):
            w = World(
                tmp_path, hub_app, agent_app, hub_token, api_token, link, provider_calls, public_ip
            )
            # El scheduler del daemon hace su primer ciclo al arrancar: se espera a que termine.
            deadline = time.monotonic() + 5
            while not w.agent.execute("SELECT 1 FROM events WHERE type = 'ip_changed'").fetchall():
                assert time.monotonic() < deadline
                await asyncio.sleep(0.02)
            w.observer.names.clear()
            w.provider_calls.clear()
            try:
                yield w
            finally:
                w.extra.close()


def test_a_valid_config_is_applied_seen_by_the_hub_and_used_by_the_next_cycle(tmp_path):
    async def go():
        async with world(tmp_path) as w:
            reply = await w.push(GOOD)
            assert reply.value.result == "applied"
            assert w.holder.version == 1 and w.agent_versions() == [(1,)]
            result = await w.cycle()
            assert sorted(w.observer.names) == ["m.example.com", "new.example.com"]
            assert set(result.records) == {"new.example.com", "m.example.com"}
            # El Hub lo ve por la respuesta...
            assert w.observed().config_version == 1
            # ...y por el evento, que llega por el outbox.
            assert await w.deliver() is None
            assert "config_applied" in w.hub_event_types()

    asyncio.run(go())


def test_the_hub_learns_the_result_from_the_event_even_if_it_never_saw_the_response(tmp_path):
    async def go():
        async with world(tmp_path) as w:
            await w.push(GOOD, record=False)  # la respuesta se "perdió"
            assert w.observed() is None
            assert await w.deliver() is None
            state = w.observed()
            assert (state.config_version, state.config_hash) == (1, config_hash(GOOD))
            assert state.last_result == "applied"

    asyncio.run(go())


def test_an_invalid_config_keeps_the_agents_version_and_behaviour(tmp_path):
    """Matriz §13, caso 9: configuración inválida desde el Hub → el Agent conserva la activa."""

    async def go():
        async with world(tmp_path) as w:
            await w.push(GOOD)
            await w.cycle()
            before = sorted(w.observer.names)
            w.observer.names.clear()

            bad = await w.push({"check_interval_seconds": 5})
            assert (bad.value.result, bad.value.code) == ("rejected", "invalid_config")
            assert w.holder.version == 1 and w.agent_versions() == [(1,)]
            assert w.holder.config.check_interval_seconds == 120
            await w.cycle()
            assert sorted(w.observer.names) == before  # el siguiente ciclo se comporta como antes

            # El Hub la ve rechazada por la respuesta y por el evento.
            state = w.observed()
            assert (state.config_version, state.last_result, state.last_code) == (
                1,
                "rejected",
                "invalid_config",
            )
            assert await w.deliver() is None
            assert w.hub.execute(
                "SELECT payload FROM events WHERE type = 'config_rejected'"
            ).fetchall() == [
                (
                    json.dumps(
                        {
                            "code": "invalid_config",
                            "hash": config_hash({"check_interval_seconds": 5}),
                            "version": 2,
                        },
                        sort_keys=True,
                    ),
                )
            ]

    asyncio.run(go())


def test_resending_a_version_is_idempotent_and_old_or_conflicting_ones_are_refused(tmp_path):
    async def go():
        async with world(tmp_path) as w:
            await w.push(GOOD)
            put = ConfigPut(version=1, hash=config_hash(GOOD), content=GOOD)
            again = await w.send(put)
            assert again.value.result == "applied" and w.agent_versions() == [(1,)]

            other = {"check_interval_seconds": 600}
            await w.push(other)  # versión 2
            old = await w.send(put)
            assert (old.value.result, old.value.code) == ("rejected", "stale_version")
            conflict = await w.send(
                ConfigPut(version=2, hash=config_hash(GOOD), content=GOOD), record=False
            )
            assert conflict.value.code == "version_conflict"
            assert w.holder.version == 2 and w.holder.config.check_interval_seconds == 600
            assert w.observed().config_version == 2

    asyncio.run(go())


def test_a_duplicate_command_runs_once_and_gives_the_same_result(tmp_path):
    async def go():
        async with world(tmp_path) as w:
            w.public_ip["ip"] = "5.6.7.8"  # la IP cambió: el comando tiene que actualizar
            request, first = await w.command("sync_ddns")
            assert first.http_status == 202
            await w.wait_command(request.command_id)
            _, again = await w.command("sync_ddns", command_id=request.command_id)
            assert again.http_status == 200 and again.value.status == "succeeded"
            assert again.value.result == {
                "ip_status": "confirmed",
                "records": {"m.example.com": "pending_verification"},
                "updates": {"m.example.com": "accepted"},
            }
            assert [c["ip"] for c in w.provider_calls] == ["5.6.7.8"]  # una sola llamada
            assert await w.deliver() is None
            row = control_repo.get_command(w.hub, request.command_id)
            assert (row.status, row.result["updates"]) == (
                "succeeded",
                {"m.example.com": "accepted"},
            )
            assert w.hub_event_types().count("command_completed") == 1

    asyncio.run(go())


def test_with_the_hub_down_the_result_arrives_by_event_once_it_returns(tmp_path):
    async def go():
        async with world(tmp_path) as w:
            w.link.down = True
            request, _ = await w.command("check_dns")
            await w.wait_command(request.command_id)
            assert await w.deliver() == "connect_error"
            # El Hub (que ya tenía el comando como `sent`) aún no sabe el resultado.
            assert control_repo.get_command(w.hub, request.command_id).status == "sent"
            await w.cycle()  # el Agent sigue con su DDNS mientras tanto
            w.link.down = False
            assert await w.deliver() is None
            row = control_repo.get_command(w.hub, request.command_id)
            assert row.status == "succeeded" and "records" in row.result
            assert await w.deliver() is None  # nada se reprocesa
            assert w.hub_event_types().count("command_completed") == 1

    asyncio.run(go())


def test_config_reset_on_the_agent_is_seen_by_the_hub_and_it_can_resend(tmp_path):
    async def go():
        async with world(tmp_path) as w:
            await w.push(GOOD)
            assert await w.deliver() is None
            with transaction(w.agent):
                agent_control_repo.delete_versions(w.agent)
                ddns_repo.insert_event(w.agent, "config_reset", NOW, None, {"previous_version": 1})
            assert await w.deliver() is None
            assert w.observed().config_version == 0 and w.observed().last_result == "reset"
            desired = control_repo.latest_desired(w.hub, AGENT_ID)
            resend = await w.send(
                ConfigPut(
                    version=desired.version, hash=desired.content_hash, content=desired.content
                )
            )
            assert resend.value.result == "applied"
            assert w.observed().config_version == 1

    asyncio.run(go())


@asynccontextmanager
async def admin_web(w):
    import re

    from dyndnsarr_hub.auth import sessions
    from dyndnsarr_hub.clock import utc_now
    from dyndnsarr_hub.services import status_refresher

    w.hub_app.state.settings.admin_origin = "https://hub.test"
    w.hub_app.state.agent_transport = httpx.ASGITransport(app=w.agent_app)
    sessions.set_password(w.hub, "admin", "una contraseña de prueba larga", utc_now(), create=True)
    async with httpx.AsyncClient(transport=w.hub_app.state.agent_transport) as agent_http:
        await status_refresher.refresh_once(w.hub, agent_http, 60)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=w.hub_app), base_url="https://hub.test"
    ) as web:
        response = await web.get("/admin/login")
        token = re.search(r'name="csrf" value="([^"]+)"', response.text)[1]
        response = await web.post(
            "/admin/login",
            data={"username": "admin", "password": "una contraseña de prueba larga", "csrf": token},
            headers={"Origin": "https://hub.test"},
        )
        assert response.status_code == 303
        web.headers.update(
            {
                "Origin": "https://hub.test",
                "X-CSRF-Token": sessions.csrf(web.cookies.get(sessions.COOKIE)),
            }
        )
        yield web


def web_operation(w, web, action, **data):
    from dyndnsarr_hub.auth import sessions
    from dyndnsarr_hub.clock import utc_now
    from dyndnsarr_hub.db import admin_repo

    agent = None if action == "policy" else AGENT_ID
    key = admin_repo.issue_operation(
        w.hub, sessions.digest(web.cookies.get(sessions.COOKIE)), agent, action, utc_now()
    )
    return {"operation_id": key, **({"agent_id": agent} if agent else {}), **data}


def test_admin_pages_real_config_and_actions_are_idempotent(tmp_path):
    async def go():
        async with world(tmp_path) as w, admin_web(w) as web:
            for path in [
                "/admin/",
                "/admin/records",
                "/admin/history",
                "/admin/diagnostics",
                "/admin/config",
            ]:
                response = await web.get(path)
                assert response.status_code == 200, response.text
                assert "canary-key" not in response.text
            history = await web.get("/admin/history")
            assert '<option value="" selected>Todos los Agents</option>' in history.text
            filtered = await web.get(
                "/admin/history",
                params={"agent": AGENT_ID, "type": "dns_verified", "since": "2026-10-19T00:00:00Z"},
            )
            assert 'name="type" value="dns_verified"' in filtered.text
            assert 'name="since" value="2026-10-19T00:00:00Z"' in filtered.text
            page = await web.get("/admin/config")
            assert 'name="active_version" value="0"' in page.text
            assert 'name="verify_retry_seconds" type="number" min="1" value="60"' in page.text
            assert 'name="verify_timeout_seconds" type="number" min="1" value="900"' in page.text
            body = web_operation(
                w, web, "config", active_version=0, active_hash="", desired_version=0, content=GOOD
            )
            response = await web.post("/admin/api/v1/config", json=body)
            assert response.status_code == 200, response.text
            assert response.json()["reference"] == "1" and w.holder.version == 1
            duplicate = await web.post("/admin/api/v1/config", json=body)
            assert duplicate.json()["duplicate"] is True and w.agent_versions() == [(1,)]
            altered = {**body, "content": {"check_interval_seconds": 600}}
            assert (await web.post("/admin/api/v1/config", json=altered)).status_code == 409
            stale = web_operation(
                w, web, "config", active_version=0, active_hash="", desired_version=0, content=GOOD
            )
            assert (await web.post("/admin/api/v1/config", json=stale)).status_code == 409
            bad = web_operation(
                w,
                web,
                "config",
                active_version=1,
                active_hash=w.holder.content_hash,
                desired_version=1,
                content={"check_interval_seconds": 1},
            )
            rejected = await web.post("/admin/api/v1/config", json=bad)
            assert rejected.json()["result"] == "rejected"
            assert w.holder.version == 1
            forbidden = web_operation(w, web, "config", content={"providers": {}})
            assert (await web.post("/admin/api/v1/config", json=forbidden)).status_code == 400
            w.public_ip["ip"] = "2.3.4.5"
            body = web_operation(w, web, "command", type="sync_ddns")
            submitted = await web.post("/admin/api/v1/command", json=body)
            assert submitted.status_code == 200, submitted.text
            command_id = submitted.json()["reference"]
            again = await web.post("/admin/api/v1/command", json=body)
            assert again.json()["reference"] == command_id and again.json()["duplicate"]
            await w.wait_command(command_id)
            assert [p["host"] for p in w.provider_calls] == ["m.example.com"]
            assert "new.example.com" not in [p["host"] for p in w.provider_calls]
            await w.deliver()
            command = control_repo.get_command(w.hub, command_id)
            assert command.status == "succeeded" and command.requested_by == "admin:1"
            csrf_bad = await web.post(
                "/admin/api/v1/command", json=body, headers={"X-CSRF-Token": "wrong"}
            )
            assert csrf_bad.status_code == 403

    asyncio.run(go())


def test_admin_lost_response_preserves_operation_and_reconciles(tmp_path):
    class LoseReply(httpx.AsyncBaseTransport):
        def __init__(self, app):
            self.inner = httpx.ASGITransport(app=app)
            self.lost = False

        async def handle_async_request(self, request):
            response = await self.inner.handle_async_request(request)
            if request.method == "POST" and request.url.path.endswith("/commands"):
                self.lost = True
                raise httpx.ReadTimeout("canary lost response")
            return response

    async def go():
        async with world(tmp_path) as w, admin_web(w) as web:
            transport = LoseReply(w.agent_app)
            w.hub_app.state.agent_transport = transport
            body = web_operation(w, web, "command", type="check_dns")
            response = await web.post("/admin/api/v1/command", json=body)
            command_id = response.json()["reference"]
            assert transport.lost and response.json()["result"] == "uncertain"
            assert (await web.post("/admin/api/v1/command", json=body)).json()["duplicate"]
            await w.wait_command(command_id)
            assert w.agent.execute("SELECT count(*) FROM commands").fetchone()[0] == 1
            w.hub_app.state.agent_transport = httpx.ASGITransport(app=w.agent_app)
            refresh = web_operation(w, web, "command-refresh", command_id=command_id)
            assert (
                await web.post("/admin/api/v1/command-refresh", json=refresh)
            ).status_code == 200
            assert control_repo.get_command(w.hub, command_id).status == "succeeded"
            audit = str(w.hub.execute("SELECT * FROM audit_log").fetchall())
            assert "canary lost response" not in audit
            assert w.api_token not in audit and "canary-key" not in audit

    asyncio.run(go())


def test_private_refresh_deduplicates_and_old_agent_does_not_break_public(tmp_path):
    from dyndnsarr_hub.db import private_repo
    from dyndnsarr_hub.services import status_refresher

    async def go():
        async with world(tmp_path) as w, admin_web(w) as web:
            before = w.hub.execute("SELECT count(*) FROM private_samples").fetchone()[0]
            before_observations = w.hub.execute(
                "SELECT count(*) FROM private_observations"
            ).fetchone()[0]
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=w.agent_app)) as client:
                await status_refresher.refresh_once(w.hub, client, 60)
            assert w.hub.execute("SELECT count(*) FROM private_samples").fetchone()[0] == before
            assert (
                w.hub.execute("SELECT count(*) FROM private_observations").fetchone()[0]
                == before_observations
            )

            class OldAgent(httpx.AsyncBaseTransport):
                async def handle_async_request(self, request):
                    if request.url.path.endswith(("/config", "/diagnostics")):
                        return httpx.Response(404, text="old")
                    return await httpx.ASGITransport(app=w.agent_app).handle_async_request(request)

            async with httpx.AsyncClient(transport=OldAgent()) as client:
                assert await status_refresher.refresh_once(w.hub, client, 60) == {AGENT_ID: None}
            value = private_repo.snapshot(w.hub, AGENT_ID)
            assert value["config_error"] == "bad_response" and value["status_error"] is None
            assert (await web.get("/public/v1/status")).status_code == 200
            assert (await web.get("/admin/config")).status_code == 200

    asyncio.run(go())


def test_admin_policy_persists_and_rejected_monitoring_only_sync_has_no_effect(tmp_path):
    from dyndnsarr_hub.clock import utc_now
    from dyndnsarr_hub.db import private_repo, public_repo
    from dyndnsarr_hub.public.builder import build_dto
    from dyndnsarr_hub.services import status_refresher

    async def go():
        async with world(tmp_path) as w, admin_web(w) as web:
            before = w.hub.execute("SELECT * FROM private_samples ORDER BY observed_at").fetchall()
            body = web_operation(
                w,
                web,
                "policy",
                observation_max_seconds=1200,
                critical_mismatch_seconds=30,
                stale_refresh_factor=5,
            )
            assert (await web.post("/admin/api/v1/policy", json=body)).status_code == 200
            assert public_repo.monitoring_policy(w.hub).observation_max_seconds == 1200
            assert (await web.post("/admin/api/v1/policy", json=body)).json()["duplicate"]
            assert public_repo.monitoring_policy(w.hub).version == 2
            assert (
                w.hub.execute("SELECT * FROM private_samples ORDER BY observed_at").fetchall()
                == before
            )
            invalid = web_operation(
                w,
                web,
                "policy",
                observation_max_seconds=1,
                critical_mismatch_seconds=30,
                stale_refresh_factor=5,
            )
            assert (await web.post("/admin/api/v1/policy", json=invalid)).status_code == 400
            assert public_repo.monitoring_policy(w.hub).version == 2
            assert build_dto(w.hub, utc_now(), w.hub_app.state.settings)
            # Se cambia explícitamente a solo monitorización y se refresca la caché del Hub.
            reply = await w.push({"records": [{"name": "m.example.com", "mode": "monitored"}]})
            assert reply.value.result == "applied"
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=w.agent_app)) as http:
                await status_refresher.refresh_once(w.hub, http, 60)
            calls = len(w.provider_calls)
            body = web_operation(w, web, "command", type="sync_ddns")
            assert (await web.post("/admin/api/v1/command", json=body)).status_code == 409
            assert len(w.provider_calls) == calls
            assert "Sincronizar" not in (await web.get("/admin/records")).text
            assert private_repo.snapshot(w.hub, AGENT_ID)["status_error"] is None

    asyncio.run(go())
