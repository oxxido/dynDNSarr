"""Cliente SDK real sobre stdio → Hub TLS real; no se simula el protocolo MCP."""

import asyncio
import json
import sys
from datetime import timedelta

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from dyndnsarr_hub import cli
from dyndnsarr_hub.clock import iso, utc_now
from dyndnsarr_hub.db import MIGRATIONS, services_repo
from dyndnsarr_hub.mcp_server import Bridge
from dyndnsarr_hub.services.service_models import Grants
from dyndnsarr_shared.db import connect, migrate
from dyndnsarr_shared.tls import build_ssl_context
from tests.conftest import TlsHub


@pytest.fixture
def service_hub(tmp_path):
    hub = TlsHub(tmp_path, services_enabled=True)
    conn = connect(hub.db_path)
    try:
        token = services_repo.add_client(
            conn,
            "mcp-test",
            Grants(
                actions=["get_status", "list_domains"],
                agents=[hub.agent_id],
                domains=["app.example.net"],
            ),
            utc_now(),
        )
    finally:
        conn.close()
    try:
        yield hub, token
    finally:
        hub.close()


def test_real_mcp_sdk_stdio_tls_reads_scopes_and_revocation(service_hub, tmp_path):
    hub, token = service_hub
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "dyndnsarr_hub.mcp_server"],
        cwd=str(tmp_path),
        env={
            "DYNDNSARR_HUB_MCP_HUB_URL": hub.url,
            "DYNDNSARR_HUB_MCP_TOKEN": token,
            "DYNDNSARR_HUB_MCP_CA_FILE": str(hub.ca_file),
            "DYNDNSARR_HUB_DATA_DIR": str(tmp_path / "must-not-create"),
        },
    )

    async def scenario():
        async with stdio_client(params) as (read, write):
            async with ClientSession(
                read, write, read_timeout_seconds=timedelta(seconds=15)
            ) as session:
                init = await session.initialize()
                assert init.serverInfo.name == "DynDNSarr"
                tools = await session.list_tools()
                assert {t.name for t in tools.tools} == {"get_status", "list_domains"}
                status = await session.call_tool("get_status", {"agent_id": hub.agent_id})
                assert not status.isError
                assert json.loads(status.content[0].text)["stale"] is True
                denied = await session.call_tool("get_status", {"agent_id": "foreign"})
                assert denied.isError and "403" in denied.content[0].text
                # La identidad se revoca mientras el proceso MCP sigue vivo.
                conn = connect(hub.db_path)
                conn.execute(
                    "UPDATE service_clients SET revoked_at=? WHERE id='mcp-test'", (iso(utc_now()),)
                )
                conn.close()
                revoked = await session.call_tool("get_status", {"agent_id": hub.agent_id})
                assert revoked.isError and "401" in revoked.content[0].text

    asyncio.run(scenario())
    assert not (tmp_path / "must-not-create").exists()


def test_mcp_bridge_rejects_untrusted_ca_without_token_leak(service_hub):
    hub, token = service_hub

    async def scenario():
        async with httpx.AsyncClient(
            verify=build_ssl_context(hub.other_ca_file), trust_env=False
        ) as http:
            result = await Bridge(http, hub.url, token).request("GET", "/service/v1/tools")
            assert result == {"code": "service_unavailable"}
            assert token not in json.dumps(result)

    asyncio.run(scenario())


def test_service_client_cli_persists_hash_lists_and_revokes(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DYNDNSARR_HUB_DATA_DIR", str(tmp_path))
    policy = tmp_path / "grants.toml"
    policy.write_text('actions=["get_status"]\nagents=["home"]\n')
    assert cli.run(["client-add", "reader", str(policy)]) == 0
    token = capsys.readouterr().out.strip().splitlines()[-1]
    assert token.startswith("dds_")
    conn = connect(tmp_path / "hub.db")
    migrate(conn, MIGRATIONS)
    row = conn.execute("SELECT token_hash,grants FROM service_clients").fetchone()
    assert token not in str(row) and len(row[0]) == 64
    assert services_repo.authenticate(conn, token)[0] == "reader"
    assert cli.run(["client-list"]) == 0
    assert token not in capsys.readouterr().out
    assert cli.run(["client-add", "reader", str(policy)]) == 1
    assert cli.run(["client-revoke", "reader"]) == 0
    assert services_repo.authenticate(conn, token) is None
    conn.close()
