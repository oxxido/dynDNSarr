import asyncio
import logging

import httpx
import pytest
from pydantic import SecretStr

from dyndnsarr_agent.logging_setup import setup_logging
from dyndnsarr_agent.providers.dondominio import DonDominioProvider

IP = "203.0.113.10"
HOST = "home.example.com"
URL = "https://dondns.example.test/json/"
USER, KEY = "canary-user", "canary-key-0123456789"


def body(success, messages=(), host_updated=None, value=IP):
    data = {"success": success, "messages": list(messages), "user": USER if success else ""}
    if host_updated is not None:
        data["host"] = {"name": HOST, "value": value, "type": "A", "updated": host_updated}
    return data


# Respuestas reales del sondeo del 2026-10-15 (usuario, clave, host e IP sustituidos).
REAL = {
    "wrong_key": httpx.Response(200, json=body(False, ["Wrong key"])),
    "wrong_user": httpx.Response(200, json=body(False, ["Incorrect data"])),
    "no_a_record": httpx.Response(200, json=body(False, ["The DNS Zone doesn't exist"])),
    "foreign_domain": httpx.Response(200, json=body(False, ["Domain not found"])),
    "updated": httpx.Response(200, json=body(True, host_updated=True)),
    "same_ip": httpx.Response(200, json=body(True, host_updated=False)),
    "rate_limited": httpx.Response(
        429, text="<html><body><h1>429 Too Many Requests</h1></body></html>"
    ),
}


def provider_for(response=None, handler=None):
    requests = []

    def serve(request):
        requests.append(request)
        if handler:
            return handler(request)
        return response

    client = httpx.AsyncClient(transport=httpx.MockTransport(serve))
    return DonDominioProvider(client, URL, SecretStr(USER), SecretStr(KEY)), requests, client


def call(provider, client):
    async def go():
        try:
            return await provider.update(HOST, IP)
        finally:
            await client.aclose()

    return asyncio.run(go())


def update(response=None, handler=None):
    provider, requests, client = provider_for(response, handler)
    return call(provider, client), requests


def test_success_with_a_change():
    result, _ = update(REAL["updated"])
    assert (result.accepted, result.changed, result.error_code) == (True, True, None)


def test_success_without_a_change():
    result, _ = update(REAL["same_ip"])
    assert (result.accepted, result.changed) == (True, False)


def test_success_without_updated_flag_is_accepted_as_unchanged():
    data = body(True, host_updated=True)
    del data["host"]["updated"]
    result, _ = update(httpx.Response(200, json=data))
    assert (result.accepted, result.changed) == (True, False)


@pytest.mark.parametrize(
    "name,code",
    [
        ("wrong_key", "auth_failed"),
        ("wrong_user", "auth_failed"),
        ("no_a_record", "host_not_found"),
        ("foreign_domain", "host_not_found"),
    ],
)
def test_known_errors_are_definitive_safe_codes(name, code):
    result, _ = update(REAL[name])
    assert (result.accepted, result.error_code, result.retryable) == (False, code, False)


def test_unknown_message_is_rejected_and_retryable():
    result, _ = update(httpx.Response(200, json=body(False, ["Something new happened"])))
    assert (result.accepted, result.error_code, result.retryable) == (False, "rejected", True)


def test_failure_without_messages_is_rejected_and_retryable():
    result, _ = update(httpx.Response(200, json={"success": False}))
    assert (result.error_code, result.retryable) == ("rejected", True)


def test_a_definitive_message_among_others_is_still_definitive():
    result, _ = update(httpx.Response(200, json=body(False, ["noise", "Wrong key"])))
    assert (result.error_code, result.retryable) == ("auth_failed", False)


def test_rate_limit_is_read_from_the_status_not_from_the_html_body():
    result, _ = update(REAL["rate_limited"])
    assert (result.accepted, result.error_code, result.retryable) == (False, "rate_limited", True)


@pytest.mark.parametrize("status", [500, 502, 503, 404, 403])
def test_other_statuses_are_retryable_http_errors(status):
    # Incluso con un cuerpo JSON de éxito: el status se trata antes de leer el cuerpo.
    result, _ = update(httpx.Response(status, json=body(True, host_updated=True)))
    assert (result.accepted, result.error_code, result.retryable) == (False, "http_error", True)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>no json</html>"),
        httpx.Response(200, content=b"\xff\xfe\x00"),
        httpx.Response(200, json=["success"]),
        httpx.Response(200, json={"messages": []}),
        httpx.Response(200, json={"success": "true"}),
        httpx.Response(200, json={"success": 1, "host": {"value": IP}}),
        httpx.Response(200, json={"success": True}),
        httpx.Response(200, json={"success": True, "host": "x"}),
    ],
)
def test_unreadable_or_incomplete_responses_are_bad_responses(response):
    result, _ = update(response)
    assert (result.accepted, result.error_code, result.retryable) == (False, "bad_response", True)


def test_success_with_a_different_value_is_a_failure():
    # El API no valida `ip`: ante basura responde éxito con el valor actual del registro.
    result, _ = update(
        httpx.Response(200, json=body(True, host_updated=False, value="198.51.100.9"))
    )
    assert (result.accepted, result.error_code, result.retryable) == (False, "bad_response", True)


@pytest.mark.parametrize(
    "error,code",
    [
        (httpx.ConnectTimeout("t"), "timeout"),
        (httpx.ReadTimeout("t"), "timeout"),
        (httpx.ConnectError("c"), "connect_error"),
        (httpx.ReadError("r"), "http_error"),
    ],
)
def test_network_errors_become_safe_codes(error, code):
    def boom(request):
        raise error

    result, _ = update(handler=boom)
    assert (result.accepted, result.error_code, result.retryable) == (False, code, True)


def test_a_hung_server_times_out():
    async def go():
        async def hang(request):
            await asyncio.sleep(5)

        transport = httpx.MockTransport(hang)
        async with httpx.AsyncClient(transport=transport) as client:
            provider = DonDominioProvider(
                client, URL, SecretStr(USER), SecretStr(KEY), timeout=0.05
            )
            return await provider.update(HOST, IP)

    result = asyncio.run(go())
    assert (result.error_code, result.retryable) == ("timeout", True)


def test_request_carries_explicit_ip_and_english_messages():
    _, [request] = update(REAL["updated"])
    assert request.method == "GET"
    assert (request.url.scheme, request.url.host, request.url.path) == (
        "https",
        "dondns.example.test",
        "/json/",
    )
    assert dict(request.url.params) == {
        "user": USER,
        "password": KEY,
        "host": HOST,
        "ip": IP,
        "lang": "en",
    }


@pytest.mark.parametrize("name", REAL)
def test_the_key_never_appears_in_results(name):
    result, _ = update(REAL[name])
    assert KEY not in repr(result) and USER not in repr(result)


@pytest.fixture
def agent_logging():
    """Logging tal como lo deja el Agent al arrancar (`httpx`/`httpcore` en WARNING)."""
    names = ("", "httpx", "httpcore")
    saved = [(n, logging.getLogger(n).handlers[:], logging.getLogger(n).level) for n in names]
    setup_logging("DEBUG")
    yield
    for name, handlers, level in saved:
        logging.getLogger(name).handlers[:] = handlers
        logging.getLogger(name).setLevel(level)


def test_the_key_never_appears_in_logs_or_results(agent_logging, capsys):
    def boom(request):
        raise httpx.ConnectError(f"cannot reach {request.url}")

    results = [update(handler=boom)[0], update(REAL["wrong_key"])[0], update(REAL["updated"])[0]]
    results.append(update(httpx.Response(200, text=f"garbage {KEY}"))[0])
    captured = capsys.readouterr()
    assert KEY not in captured.err + captured.out and USER not in captured.err + captured.out
    assert all(KEY not in repr(r) and USER not in repr(r) for r in results)
    provider, _, client = provider_for(REAL["updated"])
    assert KEY not in repr(provider)
    asyncio.run(client.aclose())


def test_without_the_agent_logging_setup_httpx_would_leak_the_url(caplog):
    # Documenta por qué `setup_logging` fija `httpx` en WARNING: su INFO lleva la URL entera.
    with caplog.at_level(logging.INFO, logger="httpx"):
        update(REAL["updated"])
    assert KEY in caplog.text
