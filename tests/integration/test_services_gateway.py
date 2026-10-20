"""Permisos reales del Hub y Caddy simulado: ningún servicio externo ni secreto real."""

import copy
import json
import uuid
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from dyndnsarr_hub.clock import iso, utc_now
from dyndnsarr_hub.config import Settings
from dyndnsarr_hub.db import agents_repo, private_repo, services_repo
from dyndnsarr_hub.main import create_app
from dyndnsarr_hub.services import gateway
from dyndnsarr_hub.services.service_models import TOOLS, Grants
from dyndnsarr_shared.control import AgentStatus

OWNED = {"@id": "dyndnsarr", "handle": [{"handler": "subroute", "routes": []}]}
OTHER = {
    "match": [{"host": ["other.example.net"]}],
    "handle": [{"handler": "static_response", "body": "unrelated"}],
}


class FakeCaddy:
    def __init__(self):
        self.servers = {
            "gateway": {"listen": [":443"], "routes": [copy.deepcopy(OWNED), copy.deepcopy(OTHER)]}
        }
        self.etag = 1
        self.patches = 0
        self.fail = None
        self.lose_response = False
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        assert request.url.host == "127.0.0.1"
        if request.method == "GET":
            return httpx.Response(200, json=self.servers, headers={"ETag": str(self.etag)})
        assert request.method == "PATCH"
        assert request.headers["If-Match"] == str(self.etag)
        self.patches += 1
        if self.fail:
            return httpx.Response(self.fail, text="private canary detail")
        self.servers["gateway"]["routes"] = json.loads(request.content)
        self.etag += 1
        if self.lose_response:
            self.lose_response = False
            raise httpx.ReadTimeout("private exception canary")
        return httpx.Response(200)

    @property
    def published(self):
        return self.servers["gateway"]["routes"][0]["handle"][0]["routes"]


@pytest.fixture
def world(tmp_path):
    policy = tmp_path / "gateway.toml"
    policy.write_text("""admin_url = "http://127.0.0.1:2019"
admin_networks = ["127.0.0.1/32"]
server = "gateway"
domains = ["app.example.net", "watch.example.net"]
private_networks = ["192.168.1.0/24"]
[destinations.app]
host = "192.168.1.20"
ports = [8080, 8443]
scheme = "http"
""")
    fake = FakeCaddy()
    app = create_app(Settings(data_dir=tmp_path, services_enabled=True, gateway_file=policy))
    app.state.gateway_transport = httpx.MockTransport(fake)
    grants = Grants(
        actions=list(TOOLS),
        agents=["home"],
        domains=["app.example.net"],
        devices=["nas"],
        destinations=["app"],
        ports=[8080, 8443],
    )
    with TestClient(app) as client:

        def seed():
            agents_repo.create_agent(app.state.db, "home", "hash", utc_now())
            agents_repo.set_api_url(app.state.db, "home", "https://agent.test")
            agents_repo.set_api_token(app.state.db, "home", "agent-token-canary")
            status = AgentStatus(
                agent_id="home",
                version="1.1.0",
                time_utc=iso(utc_now()),
                config_version=0,
                config_hash=None,
                ip_status="confirmed",
                public_ip="83.45.12.134",
                public_ip_confirmed_at=iso(utc_now()),
                last_check_at=iso(utc_now()),
                hub=None,
                records=[
                    dict(
                        name=name,
                        mode=mode,
                        verification="verified",
                        since=iso(utc_now()),
                        failures=0,
                        next_attempt_at=None,
                        last_error_code=None,
                    )
                    for name, mode in [
                        ("app.example.net", "managed"),
                        ("watch.example.net", "monitored"),
                    ]
                ],
            )
            private_repo.save_snapshot(app.state.db, "home", "status", status, None, utc_now())
            return services_repo.add_client(app.state.db, "test", grants, utc_now())

        token = client.portal.call(seed)
        client.headers["Authorization"] = "Bearer " + token
        yield client, app, fake, token


def call(client, action, **data):
    return client.post("/service/v1/tools/" + action, json=data)


def publish(client, **extra):
    return call(
        client,
        "publish",
        **{
            "publication_id": "app",
            "operation_id": str(uuid.uuid4()),
            "expected_revision": 0,
            "name": "Test app",
            "domain": "app.example.net",
            "destination": "app",
            "port": 8080,
            **extra,
        },
    )


def test_auth_before_body_revocation_origin_and_public_separation(world):
    client, app, _, token = world
    for bad in ("", "Bearer dda_agent", "Bearer " + token + "wrong"):
        response = client.post(
            "/service/v1/tools/publish", content=b"x" * 20000, headers={"Authorization": bad}
        )
        assert response.status_code == 401
    assert (
        client.get("/service/v1/tools", headers={"Origin": "https://evil.test"}).status_code == 401
    )
    result = call(client, "get_status", agent_id="home")
    assert result.status_code == 200 and result.json()["stale"] is False
    assert "83.45.12.134" not in result.text and token not in result.text
    assert result.headers["Cache-Control"] == "no-store"
    assert client.get("/service/v1/tools").status_code == 200
    assert call(client, "get_status", agent_id="other").status_code == 403
    assert call(client, "get_status", agent_id="home", scopes=["admin"]).status_code == 400
    assert client.post("/service/v1/tools/publish", content=b"x" * 16385).status_code == 413
    client.portal.call(
        lambda: app.state.db.execute("UPDATE service_clients SET revoked_at=?", (iso(utc_now()),))
    )
    assert client.get("/service/v1/tools").status_code == 401
    assert client.get("/public/v1/status").status_code == 200


def test_read_only_filtered_records_and_denials_audited(world):
    client, app, _, _ = world

    def readonly():
        grants = Grants(actions=["list_domains"], agents=["home"], domains=["app.example.net"])
        return services_repo.add_client(app.state.db, "reader", grants, utc_now())

    token = client.portal.call(readonly)
    client.headers["Authorization"] = "Bearer " + token
    assert [t["name"] for t in client.get("/service/v1/tools").json()["tools"]] == ["list_domains"]
    records = call(client, "list_domains", agent_id="home").json()["records"]
    assert [r["name"] for r in records] == ["app.example.net"]
    assert publish(client).status_code == 403
    audit = client.portal.call(
        lambda: app.state.db.execute(
            "SELECT actor,result,code FROM audit_log WHERE action='publish'"
        ).fetchall()
    )
    assert ("client:reader", "denied", "forbidden") in audit


def test_publication_lifecycle_cas_idempotency_history_and_rollback(world):
    client, app, fake, _ = world
    key = str(uuid.uuid4())
    response = publish(client, operation_id=key)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "succeeded"
    assert fake.patches == 1 and len(fake.published) == 1
    assert fake.servers["gateway"]["routes"][1] == OTHER
    assert publish(client, operation_id=key).json() == response.json()
    assert fake.patches == 1
    assert publish(client, operation_id=key, port=8443).status_code == 409
    assert publish(client, operation_id=str(uuid.uuid4())).status_code == 409
    assert (
        publish(client, expected_revision=1, port=8443, access="private").json()["state"]
        == "succeeded"
    )
    route = fake.published[0]
    assert route["handle"][0]["routes"][0]["match"] == [
        {"remote_ip": {"ranges": ["192.168.1.0/24"]}}
    ]
    assert route["handle"][0]["routes"][1]["handle"][0]["status_code"] == 403
    response = call(
        client,
        "rollback_publication",
        publication_id="app",
        operation_id=str(uuid.uuid4()),
        expected_revision=2,
        revision=1,
    )
    assert response.json()["state"] == "succeeded"
    assert fake.published[0]["handle"][0]["upstreams"] == [{"dial": "192.168.1.20:8080"}]
    history = call(client, "publication_history", publication_id="app").json()["revisions"]
    assert [r["revision"] for r in history] == [3, 2, 1]
    assert (
        call(
            client,
            "disable_publication",
            publication_id="app",
            operation_id=str(uuid.uuid4()),
            expected_revision=3,
        ).json()["state"]
        == "succeeded"
    )
    assert fake.published == []
    assert (
        call(
            client,
            "delete_publication",
            publication_id="app",
            operation_id=str(uuid.uuid4()),
            expected_revision=4,
        ).json()["state"]
        == "succeeded"
    )
    assert call(client, "list_publications").json() == {"publications": []}
    assert client.portal.call(lambda: services_repo.publication(app.state.db, "app"))[0] == 5


@pytest.mark.parametrize(
    "override,code",
    [
        ({"domain": "other.example.net"}, "forbidden"),
        ({"destination": "other"}, "forbidden"),
        ({"port": 22}, "forbidden"),
        ({"domain": "*.example.net"}, "invalid_request"),
        ({"destination": "http://169.254.169.254"}, "invalid_request"),
        ({"expires_at": "2020-01-01T00:00:00Z"}, "expiration_in_past"),
    ],
)
def test_rejected_proposals_never_call_caddy(world, override, code):
    client, _, fake, _ = world
    count = len(fake.requests)
    result = publish(client, **override)
    assert result.json()["code"] == code
    assert len(fake.requests) == count


def test_invalid_caddy_preserves_config_and_sanitizes_result(world):
    client, _, fake, _ = world
    assert publish(client).json()["state"] == "succeeded"
    previous = copy.deepcopy(fake.servers)
    fake.fail = 400
    response = publish(client, expected_revision=1, port=8443)
    assert response.json()["state"] == "failed"
    assert response.json()["result"]["code"] == "caddy_rejected"
    assert fake.servers == previous and "canary" not in response.text
    assert call(client, "list_publications").json()["publications"][0]["port"] == 8080


def test_external_host_conflict_preserved(world):
    client, _, fake, _ = world
    fake.servers["gateway"]["routes"][1]["match"] = [{"host": ["*.example.net"]}]
    response = publish(client)
    assert response.json()["result"]["code"] == "domain_conflict"
    assert fake.patches == 0


def test_lost_response_reconciles_without_duplicate_patch_and_expiry_survives_restart(world):
    client, app, fake, _ = world
    fake.lose_response = True
    expiry = utc_now() + timedelta(minutes=1)
    response = publish(client, expires_at=iso(expiry))
    assert response.json()["state"] == "pending" and fake.patches == 1

    def reconcile(now):
        async def go():
            async with app.state.service_lock:
                await gateway.reconcile(
                    app.state.db,
                    app.state.gateway_policy,
                    app.state.settings,
                    app.state.gateway_transport,
                    lambda: now,
                )

        return go()

    client.portal.call(reconcile, utc_now())
    assert fake.patches == 1
    assert call(client, "list_publications").json()["publications"][0]["revision"] == 1
    client.portal.call(reconcile, expiry + timedelta(seconds=1))
    assert fake.published == []
    client.portal.call(reconcile, expiry + timedelta(seconds=2))
    assert fake.patches == 2
    assert call(client, "list_publications").json()["publications"][0]["enabled"] is False


def test_412_does_not_overwrite_and_can_reconcile(world):
    client, app, fake, _ = world
    fake.fail = 412
    result = publish(client)
    assert result.json()["state"] == "pending" and fake.published == []
    fake.fail = None
    client.portal.call(
        gateway.reconcile,
        app.state.db,
        app.state.gateway_policy,
        app.state.settings,
        app.state.gateway_transport,
    )
    assert len(fake.published) == 1


def test_commands_scoped_to_one_record_idempotent_and_monitored_read_only(world):
    client, app, _, _ = world
    requests = []

    def agent(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(
            200,
            json={
                "command_id": body["command_id"],
                "type": body["type"],
                "status": "received",
                "received_at": iso(utc_now()),
                "finished_at": None,
                "result": None,
            },
        )

    app.state.agent_transport = httpx.MockTransport(agent)
    key = str(uuid.uuid4())
    args = dict(agent_id="home", operation_id=key, domain="app.example.net")
    assert call(client, "sync_ddns", **args).status_code == 200
    assert requests[0]["resource_id"] == "app.example.net"
    assert requests[0]["requested_by"] == "client:test"
    assert call(client, "sync_ddns", **args).status_code == 200 and len(requests) == 1
    assert call(client, "check_dns", **args).status_code == 409
    assert call(client, "sync_ddns", **{**args, "domain": "watch.example.net"}).status_code == 403

    def watcher():
        grants = Grants(actions=["sync_ddns"], agents=["home"], domains=["watch.example.net"])
        return services_repo.add_client(app.state.db, "watcher", grants, utc_now())

    client.headers["Authorization"] = "Bearer " + client.portal.call(watcher)
    response = call(
        client,
        "sync_ddns",
        agent_id="home",
        operation_id=str(uuid.uuid4()),
        domain="watch.example.net",
    )
    assert response.json()["code"] == "monitored_read_only" and len(requests) == 1


def test_gateway_disabled_and_invalid_policy_do_not_silently_enable(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        assert client.get("/service/v1/tools").status_code == 404


def test_verification_observes_upstream_without_following_redirects(world):
    client, app, _, _ = world
    assert publish(client).json()["state"] == "succeeded"
    requests = []

    def upstream(request):
        requests.append(request)
        assert request.url.host == "192.168.1.20" and request.url.port == 8080
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/"})

    app.state.gateway_transport = httpx.MockTransport(upstream)
    response = call(client, "verify_publication", publication_id="app")
    assert response.json()["state"] == "unavailable" and len(requests) == 1
    assert response.json()["public_https"] == "unknown"
    record = call(client, "list_publications").json()["publications"][0]
    assert record["availability"]["observed_at"] and record["state"] == "configured"


def test_removing_local_policy_disables_routes_but_preserves_unrelated_config(world):
    client, app, fake, _ = world
    assert publish(client).json()["state"] == "succeeded"
    app.state.gateway_policy.domains = ["watch.example.net"]
    client.portal.call(
        gateway.reconcile,
        app.state.db,
        app.state.gateway_policy,
        app.state.settings,
        app.state.gateway_transport,
    )
    assert fake.published == [] and fake.servers["gateway"]["routes"][1] == OTHER
    assert call(client, "list_publications").json()["publications"][0]["enabled"] is False


def test_pending_intention_survives_real_hub_lifespan_restart(world):
    client, app, fake, token = world
    fake.fail = 412
    assert publish(client).json()["state"] == "pending"
    # Un segundo lifespan sobre otro archivo no prueba durabilidad: reutilizar exactamente la BD.
    # Cerrar el primer Hub antes de abrir el siguiente evita dos reconciliadores simultáneos.
    client.__exit__(None, None, None)
    fake.fail = None
    restarted = create_app(app.state.settings)
    restarted.state.gateway_transport = httpx.MockTransport(fake)
    with TestClient(restarted) as other:
        other.headers["Authorization"] = "Bearer " + token
        # Esperar la misma pasada que ejecuta la tarea de arranque, sin cambiar su reloj.
        other.portal.call(
            gateway.reconcile,
            restarted.state.db,
            restarted.state.gateway_policy,
            restarted.state.settings,
            restarted.state.gateway_transport,
        )
        values = call(other, "list_publications").json()["publications"]
        assert len(values) == 1 and values[0]["revision"] == 1 and len(fake.published) == 1


def test_command_completion_updates_operation_and_audit_once_from_events(world):
    from dyndnsarr_hub.services.events_handlers import handle_new_events
    from dyndnsarr_shared.events import AgentEvent

    client, app, _, _ = world

    def agent(request):
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "command_id": body["command_id"],
                "type": body["type"],
                "status": "received",
                "received_at": iso(utc_now()),
                "finished_at": None,
                "result": None,
            },
        )

    app.state.agent_transport = httpx.MockTransport(agent)
    key = str(uuid.uuid4())
    result = call(client, "check_dns", agent_id="home", domain="app.example.net", operation_id=key)
    assert result.json()["state"] == "pending"
    event = AgentEvent(
        agent_id="home",
        event_id=str(uuid.uuid4()),
        sequence=1,
        type="command_completed",
        occurred_at=iso(utc_now()),
        payload={
            "command_id": key,
            "status": "failed",
            "result": {"code": "secret-canary", "token": "secret-canary"},
        },
    )
    client.portal.call(handle_new_events, app.state.db, [event], utc_now())
    client.portal.call(handle_new_events, app.state.db, [event], utc_now())
    result = call(client, "get_operation", operation_id=key)
    assert result.json()["state"] == "failed" and "secret-canary" not in result.text
    rows = client.portal.call(
        lambda: app.state.db.execute(
            "SELECT actor,result,code FROM audit_log WHERE operation_id=? AND result='failed'",
            (key,),
        ).fetchall()
    )
    assert rows == [("client:test", "failed", "remote_error")]


def test_pihole_device_ids_can_be_delegated_and_inventory_is_filtered(world):
    client, app, _, _ = world

    def seed():
        content = {
            "observed_at": iso(utc_now()),
            "pihole": "ok",
            "devices": [
                {
                    "id": "pihole:192.168.1.20",
                    "name": "Allowed",
                    "ip": "192.168.1.20",
                    "state": "active",
                },
                {
                    "id": "pihole:192.168.1.21",
                    "name": "Hidden",
                    "ip": "192.168.1.21",
                    "state": "active",
                },
            ],
        }
        app.state.db.execute(
            "INSERT INTO integration_cache VALUES (?,?,?,NULL)",
            ("home", json.dumps(content), iso(utc_now())),
        )
        return services_repo.add_client(
            app.state.db,
            "devices",
            Grants(actions=["list_devices"], agents=["home"], devices=["pihole:192.168.1.20"]),
            utc_now(),
        )

    client.headers["Authorization"] = "Bearer " + client.portal.call(seed)
    response = call(client, "list_devices", agent_id="home")
    assert response.json()["stale"] is False
    assert [d["id"] for d in response.json()["devices"]] == ["pihole:192.168.1.20"]
    assert "Hidden" not in response.text and "192.168.1.21" not in response.text
