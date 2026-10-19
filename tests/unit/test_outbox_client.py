import asyncio
import json
import ssl
import uuid

import httpx
import pytest
from pydantic import SecretStr

from dyndnsarr_agent.db.ddns_repo import Event
from dyndnsarr_agent.outbox.client import HttpHubClient

TOKEN = "dda_canary-token-0123456789"
BASE = "https://hub.example.lan"


def make_events(n=2):
    return [
        Event(
            i + 1, str(uuid.uuid4()), "ip_changed", "2026-10-19T11:00:00Z", None, {"ip": "1.2.3.4"}
        )
        for i in range(n)
    ]


def client_with(handler):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HttpHubClient(http, BASE, "raspberry-home", SecretStr(TOKEN))


def send(handler, events=None):
    async def go():
        client = client_with(handler)
        try:
            return await client.send(events or make_events())
        finally:
            await client._client.aclose()

    return asyncio.run(go())


def ok(request):
    ids = [e["event_id"] for e in json.loads(request.content)["events"]]
    return httpx.Response(200, json={"acked": ids, "rejected": []})


def test_posts_the_batch_with_bearer_token_and_the_contract_shape():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return ok(request)

    events = make_events()
    result = send(handler, events)
    assert seen["url"] == f"{BASE}/api/v1/agents/raspberry-home/events"
    assert seen["auth"] == f"Bearer {TOKEN}"
    assert [e["sequence"] for e in seen["body"]["events"]] == [1, 2]
    assert set(seen["body"]["events"][0]) == {
        "event_id",
        "agent_id",
        "sequence",
        "type",
        "occurred_at",
        "record_name",
        "payload",
    }
    assert result.error_code is None
    assert result.acked == [e.event_id for e in events]


def test_rejected_events_are_reported_with_their_code():
    events = make_events()

    def handler(request):
        body = {
            "acked": [events[0].event_id],
            "rejected": [{"event_id": events[1].event_id, "code": "invalid_event"}],
        }
        return httpx.Response(200, json=body)

    result = send(handler, events)
    assert result.acked == [events[0].event_id]
    assert result.rejected == [(events[1].event_id, "invalid_event")]


def test_ids_the_hub_acks_but_were_not_sent_are_ignored():
    handler = lambda r: httpx.Response(200, json={"acked": ["otro"], "rejected": []})  # noqa: E731
    result = send(handler)
    assert result.acked == [] and result.rejected == []


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "auth_failed"),
        (403, "auth_failed"),
        (429, "rate_limited"),
        (500, "server_error"),
        (503, "server_error"),
        (400, "http_error"),
        (413, "http_error"),
        (404, "http_error"),
    ],
)
def test_status_codes_map_to_safe_codes(status, code):
    result = send(lambda r: httpx.Response(status, text=f"detalle interno {TOKEN}"))
    assert result.error_code == code


@pytest.mark.parametrize(
    "body", [b"no json", b'{"acked": 1, "rejected": []}', b'{"acked": [], "extra": 1}', b"[]"]
)
def test_unreadable_200_is_bad_response(body):
    result = send(lambda r: httpx.Response(200, content=body))
    assert result.error_code == "bad_response"


def test_transport_failures_map_to_safe_codes():
    def raising(exc):
        def handler(request):
            raise exc

        return handler

    assert (send(raising(httpx.ReadTimeout("t")))).error_code == "timeout"
    assert (send(raising(httpx.ConnectTimeout("t")))).error_code == "timeout"
    assert (send(raising(httpx.ConnectError("boom")))).error_code == "connect_error"
    assert (send(raising(httpx.ReadError("boom")))).error_code == "connect_error"
    assert (send(raising(httpx.RemoteProtocolError("boom")))).error_code == "connect_error"


def test_ssl_failures_map_to_tls_error():
    def handler(request):
        try:
            raise ssl.SSLCertVerificationError("certificate verify failed")
        except ssl.SSLError as inner:
            raise httpx.ConnectError(f"{BASE} {TOKEN}") from inner

    result = send(handler)
    assert result.error_code == "tls_error"
    assert TOKEN not in repr(result)
