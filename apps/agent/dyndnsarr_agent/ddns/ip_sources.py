import asyncio
import ipaddress
from dataclasses import dataclass
from typing import Literal

import httpx

MAX_RESPONSE_BYTES = 64


@dataclass(frozen=True)
class SourceResult:
    source: str
    ip: str | None
    error_code: str | None


@dataclass(frozen=True)
class Consensus:
    status: Literal["confirmed", "uncertain"]
    ip: str | None
    reason: str | None


def parse_ip(text: str) -> str:
    """Interpreta la respuesta de una fuente. `ValueError` con un código seguro si no sirve."""
    try:
        ip = ipaddress.IPv4Address(text.strip())
    except ValueError:
        raise ValueError("invalid_ip") from None
    if not ip.is_global or ip.is_multicast:
        raise ValueError("non_global_ip")
    return str(ip)


def consensus(results: list[SourceResult]) -> Consensus:
    """IP confirmada solo si responden ≥2 fuentes y todas las que responden coinciden (D3)."""
    answers = {r.ip for r in results if r.ip is not None}
    responders = sum(1 for r in results if r.ip is not None)
    if responders < 2:
        return Consensus("uncertain", None, "insufficient_sources")
    if len(answers) > 1:
        return Consensus("uncertain", None, "disagreement")
    return Consensus("confirmed", answers.pop(), None)


async def _read_limited(response: httpx.Response) -> bytes:
    body = b""
    async for chunk in response.aiter_bytes():
        body += chunk
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("response_too_large")
    return body


async def _fetch(client: httpx.AsyncClient, url: str, timeout: float) -> SourceResult:
    try:
        async with asyncio.timeout(timeout), client.stream("GET", url) as response:
            if response.status_code != 200:
                return SourceResult(url, None, "http_status")
            body = await _read_limited(response)
        return SourceResult(url, parse_ip(body.decode("ascii", errors="replace")), None)
    except TimeoutError:
        return SourceResult(url, None, "timeout")
    except httpx.TimeoutException:
        return SourceResult(url, None, "timeout")
    except httpx.ConnectError:
        return SourceResult(url, None, "connect_error")
    except httpx.HTTPError:
        return SourceResult(url, None, "http_error")
    except ValueError as exc:
        # Solo `_read_limited` y `parse_ip` lanzan ValueError, y ambos con un código seguro.
        return SourceResult(url, None, str(exc))


async def detect_public_ip(
    client: httpx.AsyncClient, urls: list[str], timeout: float = 10.0
) -> tuple[list[SourceResult], Consensus]:
    """Consulta las fuentes a la vez. Los errores quedan como códigos seguros (nunca la URL)."""
    results = await asyncio.gather(*(_fetch(client, url, timeout) for url in urls))
    return list(results), consensus(list(results))
