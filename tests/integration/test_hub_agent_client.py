"""Cliente del Hub hacia el Agent: TLS real contra un Agent de pruebas y códigos seguros."""

import asyncio
import json
import uuid

import httpx
import pytest

from dyndnsarr_hub.services.agent_client import AgentClient, build_http_client, check_api_url
from dyndnsarr_shared.control import CommandRequest, ConfigPut, config_hash

CONTENT = {"check_interval_seconds": 120}
EXPIRES = "2099-01-01T00:00:00Z"
CANARY = "ddh_canary-token-0123456789"


def call(url, ca_file, token, method, *args, transport=None):
    async def go():
        async with build_http_client(ca_file, transport) as http:
            return await getattr(AgentClient(http, url, token), method)(*args)

    return asyncio.run(go())


def command(type="check_dns", **extra):
    return CommandRequest(
        command_id=str(uuid.uuid4()),
        type=type,
        requested_by="admin:cli",
        expires_at=EXPIRES,
        **extra,
    )


def test_health_status_config_and_commands_over_verified_tls(tls_agent):
    url, ca, token = tls_agent.url, tls_agent.ca_file, tls_agent.token
    health = call(url, ca, token, "health")
    assert health.ok and health.value.status == "ok" and health.value.agent_id is None
    status = call(url, ca, token, "status")
    assert status.ok and status.value.config_version == 0
    put = ConfigPut(version=1, hash=config_hash(CONTENT), content=CONTENT)
    applied = call(url, ca, token, "put_config", put)
    assert applied.ok and applied.value.result == "applied" and applied.http_status == 200
    request = command()
    accepted = call(url, ca, token, "post_command", request)
    assert accepted.ok and accepted.http_status == 202
    assert call(url, ca, token, "command", request.command_id).ok


def test_a_rejected_config_is_a_value_not_an_error(tls_agent):
    put = ConfigPut(version=1, hash="0" * 64, content=CONTENT)
    reply = call(tls_agent.url, tls_agent.ca_file, tls_agent.token, "put_config", put)
    assert reply.ok and (reply.value.result, reply.value.code) == ("rejected", "hash_mismatch")
    assert reply.error_code is None


def test_an_agent_refusal_is_reported_as_api_error(tls_agent):
    reply = call(
        tls_agent.url,
        tls_agent.ca_file,
        tls_agent.token,
        "post_command",
        command("wake_device", resource_id="nas-1"),
    )
    assert (reply.ok, reply.error_code, reply.http_status, reply.api_error) == (
        False,
        None,
        422,
        "unsupported_command",
    )
    missing = call(tls_agent.url, tls_agent.ca_file, tls_agent.token, "command", "nope")
    assert (missing.http_status, missing.api_error) == (404, "not_found")


def test_a_wrong_token_is_auth_failed(tls_agent):
    reply = call(tls_agent.url, tls_agent.ca_file, "ddh_otro", "health")
    assert (reply.ok, reply.error_code, reply.http_status) == (False, "auth_failed", 401)


def test_an_untrusted_ca_is_a_tls_error_and_nothing_is_sent(tls_agent):
    for ca in (None, tls_agent.other_ca_file):
        reply = call(tls_agent.url, ca, tls_agent.token, "health")
        assert (reply.ok, reply.error_code) == (False, "tls_error")


def test_a_closed_port_is_a_connect_error():
    reply = call("https://127.0.0.1:1", None, CANARY, "health")
    assert reply.error_code == "connect_error"


def mock(handler):
    return httpx.MockTransport(handler)


def test_timeouts_server_errors_and_garbage_have_their_own_codes():
    def timeout(request):
        raise httpx.ReadTimeout("lento")

    assert call("https://pi.lan", None, CANARY, "health", transport=mock(timeout)).error_code == (
        "timeout"
    )
    server = mock(lambda r: httpx.Response(503, text="x"))
    assert call("https://pi.lan", None, CANARY, "health", transport=server).error_code == (
        "server_error"
    )
    junk = mock(lambda r: httpx.Response(200, text="<html>"))
    assert (
        call("https://pi.lan", None, CANARY, "health", transport=junk).error_code == "bad_response"
    )
    odd = mock(lambda r: httpx.Response(418, text="soy una tetera"))
    reply = call("https://pi.lan", None, CANARY, "health", transport=odd)
    assert (reply.error_code, reply.http_status) == ("bad_response", 418)


def test_the_request_carries_the_token_and_the_body_only_where_expected():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"result": "applied", "version": 1})

    put = ConfigPut(version=1, hash=config_hash(CONTENT), content=CONTENT)
    call("https://pi.lan:8001/", None, CANARY, "put_config", put, transport=mock(handler))
    [request] = seen
    assert request.method == "PUT" and str(request.url) == "https://pi.lan:8001/internal/v1/config"
    assert request.headers["authorization"] == f"Bearer {CANARY}"
    assert json.loads(request.content)["version"] == 1


def test_the_token_never_appears_in_a_reply_repr():
    reply = call(
        "https://pi.lan", None, CANARY, "health", transport=mock(lambda r: httpx.Response(401))
    )
    assert CANARY not in repr(reply)


@pytest.mark.parametrize(
    "url",
    [
        "http://pi.lan:8001",
        "ftp://pi.lan",
        "https://user:pw@pi.lan",
        "https://pi.lan?x=1",
        "https://pi.lan#frag",
        "pi.lan",
    ],
)
def test_unsafe_api_urls_are_refused(url):
    with pytest.raises(ValueError):
        check_api_url(url)
    with pytest.raises(ValueError):
        AgentClient(httpx.AsyncClient(), url, CANARY)


def test_https_and_loopback_http_are_accepted_and_the_slash_is_trimmed():
    assert check_api_url("https://pi.lan:8001/") == "https://pi.lan:8001"
    assert check_api_url("http://127.0.0.1:8001") == "http://127.0.0.1:8001"
    assert check_api_url("http://localhost:8001") == "http://localhost:8001"
