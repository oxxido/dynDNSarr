import asyncio

import httpx
import pytest

from dyndnsarr_agent.ddns.ip_sources import (
    SourceResult,
    consensus,
    detect_public_ip,
    parse_ip,
)

A = "https://a.example/ip"
B = "https://b.example/ip"
C = "https://c.example/ip"


def ok(source, ip):
    return SourceResult(source, ip, None)


def fail(source, code="timeout"):
    return SourceResult(source, None, code)


# --- consenso (función pura) ---


def test_two_agreeing_sources_confirm():
    result = consensus([ok(A, "1.2.3.4"), ok(B, "1.2.3.4")])
    assert (result.status, result.ip, result.reason) == ("confirmed", "1.2.3.4", None)


def test_disagreement_is_uncertain():
    result = consensus([ok(A, "1.2.3.4"), ok(B, "5.6.7.8")])
    assert (result.status, result.ip, result.reason) == ("uncertain", None, "disagreement")


def test_single_responding_source_is_uncertain():
    result = consensus([ok(A, "1.2.3.4"), fail(B)])
    assert (result.status, result.reason) == ("uncertain", "insufficient_sources")


def test_no_source_responding_is_uncertain():
    assert consensus([fail(A), fail(B)]).reason == "insufficient_sources"
    assert consensus([]).reason == "insufficient_sources"


def test_third_source_failing_does_not_block_two_that_agree():
    assert consensus([ok(A, "1.2.3.4"), ok(B, "1.2.3.4"), fail(C)]).status == "confirmed"


def test_any_contradiction_among_responders_is_uncertain_even_with_majority():
    result = consensus([ok(A, "1.2.3.4"), ok(B, "1.2.3.4"), ok(C, "5.6.7.8")])
    assert (result.status, result.reason) == ("uncertain", "disagreement")


# --- interpretación de la respuesta ---


@pytest.mark.parametrize("text", ["1.2.3.4", "1.2.3.4\n", " 1.2.3.4 \r\n"])
def test_parse_ip_accepts_plain_ipv4(text):
    assert parse_ip(text) == "1.2.3.4"


@pytest.mark.parametrize(
    "text,code",
    [
        ("", "invalid_ip"),
        ("<html>oops</html>", "invalid_ip"),
        ("1.2.3", "invalid_ip"),
        ("1.2.3.4 extra", "invalid_ip"),
        ("2001:db8::1", "invalid_ip"),
        ("192.168.1.10", "non_global_ip"),
        ("10.0.0.1", "non_global_ip"),
        ("127.0.0.1", "non_global_ip"),
        ("169.254.1.1", "non_global_ip"),
        ("100.64.0.1", "non_global_ip"),
        ("0.0.0.0", "non_global_ip"),
        ("224.0.0.1", "non_global_ip"),
    ],
)
def test_parse_ip_rejects_garbage_and_non_global(text, code):
    with pytest.raises(ValueError, match=code):
        parse_ip(text)


# --- consulta HTTP ---


def run(handler, urls=(A, B), timeout=0.5):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await detect_public_ip(client, list(urls), timeout=timeout)

    return asyncio.run(go())


def test_detect_confirms_when_sources_agree():
    results, outcome = run(lambda request: httpx.Response(200, text="1.2.3.4\n"))
    assert [(r.source, r.ip, r.error_code) for r in results] == [
        (A, "1.2.3.4", None),
        (B, "1.2.3.4", None),
    ]
    assert outcome.status == "confirmed"


def test_detect_discrepancy_is_uncertain():
    def handler(request):
        return httpx.Response(200, text="1.2.3.4" if request.url.host == "a.example" else "5.6.7.8")

    _, outcome = run(handler)
    assert (outcome.status, outcome.reason) == ("uncertain", "disagreement")


def test_detect_one_source_down_is_uncertain():
    def handler(request):
        if request.url.host == "b.example":
            raise httpx.ConnectError("boom")
        return httpx.Response(200, text="1.2.3.4")

    results, outcome = run(handler)
    assert results[1].error_code == "connect_error"
    assert outcome.status == "uncertain"


def test_detect_http_error_status_is_a_safe_code():
    results, _ = run(lambda request: httpx.Response(503, text="1.2.3.4"))
    assert {r.error_code for r in results} == {"http_status"}


def test_detect_redirect_is_not_followed():
    results, _ = run(lambda request: httpx.Response(302, headers={"location": "http://evil/"}))
    assert {r.error_code for r in results} == {"http_status"}


def test_detect_rejects_responses_over_64_bytes():
    results, outcome = run(lambda request: httpx.Response(200, text="1.2.3.4" + " " * 200))
    assert {r.error_code for r in results} == {"response_too_large"}
    assert outcome.status == "uncertain"


def test_detect_rejects_private_ip_from_a_source():
    results, _ = run(lambda request: httpx.Response(200, text="192.168.0.5"))
    assert {r.error_code for r in results} == {"non_global_ip"}


def test_detect_httpx_timeout_is_a_safe_code():
    def handler(request):
        raise httpx.ReadTimeout("slow")

    results, _ = run(handler)
    assert {r.error_code for r in results} == {"timeout"}


def test_detect_hanging_source_times_out_without_blocking_the_other():
    async def go():
        async def handler(request):
            if request.url.host == "a.example":
                await asyncio.sleep(5)
            return httpx.Response(200, text="1.2.3.4")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await detect_public_ip(client, [A, B], timeout=0.1)

    results, outcome = asyncio.run(go())
    assert [r.error_code for r in results] == ["timeout", None]
    assert outcome.status == "uncertain"


def test_error_codes_never_contain_the_url():
    def handler(request):
        raise httpx.ConnectError(f"cannot reach {request.url}")

    results, _ = run(handler)
    assert all("example" not in (r.error_code or "") for r in results)
