"""Destinos de integraciones: validación, resolución fijada y respuestas acotadas."""

import asyncio
import ipaddress
import json
import socket
from urllib.parse import urlsplit

import httpx


class DestinationError(ValueError):
    pass


def validate_url(url, *, allow_http=False):
    parts = urlsplit(url)
    if (
        parts.scheme not in (("http", "https") if allow_http else ("https",))
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
        or len(url) > 2048
    ):
        raise DestinationError("Destino de integración no válido")
    try:
        _port = parts.port
    except ValueError:
        raise DestinationError("Puerto no válido") from None
    return url


def allowed_address(address, networks):
    ip = ipaddress.ip_address(address)
    if str(ip) in ("100.100.100.200", "fd00:ec2::254"):
        raise DestinationError("Dirección de metadatos prohibida")
    if (
        ip.is_unspecified
        or ip.is_multicast
        or ip.is_link_local
        or (ip.is_reserved and not ip.is_loopback)
    ):
        raise DestinationError("Dirección sensible prohibida")
    if not ip.is_global and not any(ip in ipaddress.ip_network(n) for n in networks):
        raise DestinationError("Dirección local fuera de allowlist")
    return str(ip)


async def resolve(host, port, networks):
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        result = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM), 5
        )
        addresses = list(dict.fromkeys(row[4][0] for row in result))
    if not addresses:
        raise DestinationError("Destino sin dirección")
    # Rechazar respuestas mixtas, no escoger la dirección pública de un conjunto sensible.
    for address in addresses:
        allowed_address(address, networks)
    return min(addresses, key=lambda address: ipaddress.ip_address(address).version)


async def request(http, method, url, *, networks=(), headers=None, body=None, parse_json=True):
    """Conecta a la IP validada; conserva Host y SNI para verificación TLS completa.

    No sigue redirecciones ni usa proxies del entorno. El cliente debe tener trust_env=False.
    """
    validate_url(url, allow_http=bool(networks))
    original = httpx.URL(url)
    address = await resolve(
        original.host, original.port or (443 if original.scheme == "https" else 80), networks
    )
    if original.scheme == "http" and ipaddress.ip_address(address).is_global:
        raise DestinationError("HTTP solo para destinos locales explícitos")
    pinned = original.copy_with(host=address)
    authority = original.netloc.decode()
    async with http.stream(
        method,
        pinned,
        headers={**(headers or {}), "Host": authority},
        json=body,
        extensions={"sni_hostname": original.host},
        follow_redirects=False,
        timeout=5,
    ) as response:
        if response.status_code in (401, 403):
            raise DestinationError("auth_failed")
        if not 200 <= response.status_code < 300:
            raise DestinationError("http_failed")
        chunks, size = [], 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > 1024 * 1024:
                raise DestinationError("response_too_large")
            chunks.append(chunk)
    content = b"".join(chunks)
    return json.loads(content) if content and parse_json else {}
