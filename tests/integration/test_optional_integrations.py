"""Integraciones falsas, persistencia WoL y separación respecto al motor DDNS."""

import asyncio
import json
import socket
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from dyndnsarr_agent.db import MIGRATIONS
from dyndnsarr_agent.integrations.config import IntegrationConfig, Pihole, WakePolicy
from dyndnsarr_agent.integrations.pihole import PiholeClient, inventory
from dyndnsarr_agent.integrations.worker import IntegrationWorker, read_snapshot, send_magic
from dyndnsarr_shared.db import connect, migrate
from dyndnsarr_shared.outbound import DestinationError, request
from dyndnsarr_shared.tls import build_ssl_context

NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)
POLICY = dict(
    id="nas",
    name="NAS",
    ip="192.168.1.10",
    mac="00:11:22:33:44:55",
    broadcast="192.168.1.255",
    manual=True,
    automatic=True,
)


def test_pihole_official_fields_and_dhcp_never_proves_activity():
    network = {
        "devices": [
            {
                "hwaddr": "00:11:22:33:44:55",
                "lastQuery": int(NOW.timestamp()) - 60,
                "ips": [{"ip": "192.168.1.2", "name": "nas"}],
            }
        ]
    }
    leases = {
        "leases": [
            {"hwaddr": "00:11:22:33:44:66", "expires": 0, "ip": "192.168.1.3", "name": "dhcp-only"}
        ]
    }
    values = inventory(network, leases, NOW)
    assert [v.state for v in values] == ["likely_active", "known"]
    assert values[1].last_activity_at is None
    assert values[1].lease_expires_at is None
    assert inventory(network, leases, NOW + timedelta(hours=1))[0].state == "stale"


def test_pihole_only_auth_and_get_and_secrets_remain_in_memory():
    calls = []

    def handle(req):
        calls.append(req)
        if req.url.path.endswith("/auth"):
            assert json.loads(req.content) == {"password": "canary-password"}
            return httpx.Response(200, json={"session": {"valid": True, "sid": "canary-sid"}})
        assert req.method == "GET" and req.headers["sid"] == "canary-sid"
        return httpx.Response(
            200, json={"devices": []} if "network" in req.url.path else {"leases": []}
        )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle), trust_env=False
        ) as http:
            client = PiholeClient(
                http,
                Pihole(url="https://192.168.1.2", networks=["192.168.1.0/24"]),
                "canary-password",
            )
            assert await client.read(NOW) == []
            assert await client.read(NOW) == []

    asyncio.run(run())
    assert [r.method for r in calls] == ["POST", "GET", "GET", "GET", "GET"]


@pytest.mark.parametrize(
    "changes",
    [
        {"ip": "1.1.1.1"},
        {"ip": "127.0.0.1"},
        {"broadcast": "169.254.169.254"},
        {"mac": "invalid"},
        {"cooldown_seconds": 1},
    ],
)
def test_wake_policy_rejects_unsafe_destinations(changes):
    with pytest.raises(ValidationError):
        WakePolicy(**{**POLICY, **changes})


def test_wake_idempotence_cooldown_restart_and_probe(tmp_path):
    path = tmp_path / "agent.db"
    conn = connect(path)
    migrate(conn, MIGRATIONS)
    sent = []
    clock = [NOW]
    reachable = [False]

    async def check(device):
        return reachable[0]

    def worker(db):
        return IntegrationWorker(
            db,
            IntegrationConfig(devices=[POLICY]),
            None,
            clock=lambda: clock[0],
            check=check,
            send=lambda d: sent.append(d.id),
        )

    async def run():
        w = worker(conn)
        assert await w.wake("nas", "operation-1") == {"wake_state": "sent"}
        assert await w.wake("nas", "operation-1") == {"wake_state": "sent"}
        assert (await w.wake("nas", "operation-2"))["code"] == "cooldown"
        assert (await w.refresh()).targets[0].wake_state == "booting"
        conn.close()
        other = connect(path)
        try:
            w = worker(other)
            assert (await w.wake("nas", "operation-1"))["wake_state"] == "booting"
            assert (await w.wake("nas", "operation-3"))["code"] == "cooldown"
            clock[0] += timedelta(minutes=16)
            assert (await w.refresh()).targets[0].wake_state == "unconfirmed"
            reachable[0] = True
            assert (await w.wake("nas", "operation-4"))["wake_state"] == "reachable"
            assert (await w.refresh()).targets[0].state == "active"
            with pytest.raises(ValueError):
                await w.wake("not-allowed", "operation-5")
            with pytest.raises(ValueError):
                await w.wake("other", "operation-1")
        finally:
            other.close()

    asyncio.run(run())
    assert sent == ["nas"]


def test_wake_reservation_survives_lost_reply_and_concurrency(tmp_path):
    conn = connect(tmp_path / "agent.db")
    migrate(conn, MIGRATIONS)
    gate = asyncio.Event()
    sent = []

    async def check(_):
        await gate.wait()
        return False

    async def run():
        w = IntegrationWorker(
            conn,
            IntegrationConfig(devices=[POLICY]),
            None,
            clock=lambda: NOW,
            check=check,
            send=lambda _: sent.append(1),
        )
        pending = asyncio.create_task(w.wake("nas", "one"))
        await asyncio.sleep(0)
        assert (await w.wake("nas", "two"))["code"] == "cooldown"
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert (await w.wake("nas", "one"))["wake_state"] == "unconfirmed"
        assert sent == []

    asyncio.run(run())
    conn.close()


def test_udp_packet_has_correct_magic_sequence():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(1)
        send_magic(
            SimpleNamespace(
                mac=POLICY["mac"], broadcast="127.0.0.1", port=receiver.getsockname()[1]
            )
        )
        packet, _ = receiver.recvfrom(1024)
    assert packet == b"\xff" * 6 + bytes.fromhex("001122334455") * 16


def test_pihole_failure_retains_snapshot_without_claiming_activity(tmp_path):
    conn = connect(tmp_path / "agent.db")
    migrate(conn, MIGRATIONS)
    calls = [0]

    async def read(now):
        calls[0] += 1
        if calls[0] > 1:
            raise ValueError("secret-canary")
        return inventory(
            {"devices": []}, {"leases": [{"ip": "192.168.1.4", "name": "known", "expires": 0}]}, now
        )

    async def run():
        w = IntegrationWorker(conn, IntegrationConfig(), None, clock=lambda: NOW)
        w.pihole = SimpleNamespace(read=read)
        assert (await w.refresh()).pihole == "ok"
        failed = await w.refresh()
        assert failed.pihole == "unavailable" and failed.last_success_at
        assert failed.devices[0].state == "unknown"
        assert "secret-canary" not in read_snapshot(conn).model_dump_json()

    asyncio.run(run())
    conn.close()


def test_outbound_pins_dns_host_sni_and_rejects_redirect(monkeypatch):
    from dyndnsarr_shared import outbound

    async def resolve(*args):
        return "1.1.1.1"

    monkeypatch.setattr(outbound, "resolve", resolve)

    def handle(req):
        assert req.url.host == "1.1.1.1" and req.headers["host"] == "hooks.example"
        assert req.extensions["sni_hostname"] == "hooks.example"
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/"})

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle), trust_env=False
        ) as http:
            with pytest.raises(DestinationError):
                await request(http, "POST", "https://hooks.example/x")

    asyncio.run(run())


def test_outbound_tls_verifies_hostname_after_pinning(tls_hub):
    async def run():
        url = tls_hub.url.replace("127.0.0.1", "localhost") + "/healthz"
        async with httpx.AsyncClient(
            verify=build_ssl_context(tls_hub.ca_file), trust_env=False
        ) as http:
            result = await request(http, "GET", url, networks=["127.0.0.0/8", "::1/128"])
            assert result["status"] == "ok"
        async with httpx.AsyncClient(
            verify=build_ssl_context(tls_hub.other_ca_file), trust_env=False
        ) as http:
            with pytest.raises(httpx.TransportError):
                await request(http, "GET", url, networks=["127.0.0.0/8", "::1/128"])

    asyncio.run(run())


def test_auto_wake_only_after_recovery_and_cooldown_survives_boots(tmp_path, monkeypatch):
    from dyndnsarr_agent.integrations import worker as module

    conn = connect(tmp_path / "agent.db")
    migrate(conn, MIGRATIONS)
    sent = []

    async def check(_):
        return False

    async def stop(_):
        raise asyncio.CancelledError

    monkeypatch.setattr(module.asyncio, "sleep", stop)
    config = IntegrationConfig(devices=[POLICY, {**POLICY, "id": "disabled", "automatic": False}])

    async def run():
        w = IntegrationWorker(
            conn, config, None, clock=lambda: NOW, check=check, send=lambda d: sent.append(d.id)
        )
        with pytest.raises(asyncio.CancelledError):
            await w.run(recovered=False)
        assert sent == []
        with pytest.raises(asyncio.CancelledError):
            await w.run(recovered=True)
        with pytest.raises(asyncio.CancelledError):
            await w.run(recovered=True)
        assert sent == ["nas"]

    asyncio.run(run())
    conn.close()


def test_stalled_pihole_does_not_delay_ddns_or_shutdown(tmp_path):
    from dyndnsarr_agent.config import Settings
    from dyndnsarr_agent.db import ddns_repo
    from dyndnsarr_agent.dns.resolver import ResolverAnswer
    from dyndnsarr_agent.main import create_app

    config = tmp_path / "agent.toml"
    config.write_text(
        '[ip_sources]\nurls=["https://a.example/ip","https://b.example/ip"]\n[dns]\nresolvers=["1.1.1.1"]\n[[records]]\nname="home.example.com"\nmode="monitored"\n'
    )
    integrations = tmp_path / "integrations.toml"
    integrations.write_text('[pihole]\nurl="https://192.168.1.2"\nnetworks=["192.168.1.0/24"]\n')

    class Observer:
        async def lookup(self, _):
            return [ResolverAnswer("1.1.1.1", ("1.2.3.4",), None)]

    async def run():
        gate = asyncio.Event()

        async def stalled(_):
            await gate.wait()

        app = create_app(
            Settings(
                data_dir=tmp_path / "data", config_file=config, integrations_file=integrations
            ),
            observer=Observer(),
            transport=httpx.MockTransport(lambda _: httpx.Response(200, text="1.2.3.4")),
            integration_transport=httpx.MockTransport(stalled),
        )
        async with app.router.lifespan_context(app):
            for _ in range(100):
                if ddns_repo.get_state(app.state.db, "public_ip"):
                    break
                await asyncio.sleep(0.01)
            assert ddns_repo.get_state(app.state.db, "public_ip") == "1.2.3.4"
            assert not gate.is_set()

    asyncio.run(asyncio.wait_for(run(), 5))
