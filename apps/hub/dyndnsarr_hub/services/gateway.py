"""Intenciones duraderas y reconciliación de publicaciones; red fuera de transacciones."""

import asyncio
import logging

import httpx

from dyndnsarr_hub.clock import iso, utc_now
from dyndnsarr_hub.db import auth_repo, services_repo
from dyndnsarr_hub.services.caddy import CaddyClient, GatewayError, routes, validate_publication
from dyndnsarr_shared.db import transaction
from dyndnsarr_shared.outbound import request
from dyndnsarr_shared.tls import build_ssl_context

logger = logging.getLogger(__name__)


def visible(grants, value):
    return (
        value["domain"] in grants.domains
        and value["destination"] in grants.destinations
        and value["port"] in grants.ports
    )


async def apply(conn, policy, settings, transport, now, op=None):
    publications = services_repo.publications(conn)
    desired = []
    for publication in publications:
        try:
            validate_publication(policy, publication)
            desired.append(publication)
        except GatewayError:
            pass
    if op:
        candidate = op["request"]["candidate"]
        desired = [p for p in desired if p["publication_id"] != op["resource"]]
        if candidate:
            desired.append(candidate)
    try:
        async with httpx.AsyncClient(
            verify=build_ssl_context(settings.gateway_ca_file), trust_env=False, transport=transport
        ) as http:
            await CaddyClient(http, policy, settings.gateway_token).apply(
                routes(policy, desired, now)
            )
    except GatewayError as exc:
        if op:
            with transaction(conn):
                services_repo.finish(
                    conn, op["id"], "pending" if exc.retry else "failed", {"code": exc.code}, now
                )
        return exc.code
    if op:
        with transaction(conn):
            saved = services_repo.save_publication(conn, op["resource"], candidate, op["id"], now)
            services_repo.finish(conn, op["id"], "succeeded", {"publication": saved}, now)
    return None


async def reconcile(conn, policy, settings, transport=None, clock=utc_now):
    """Llamada bajo el mismo lock que las mutaciones API, también tras reinicio."""
    if policy is None:
        return
    now = clock()
    pending = conn.execute(
        "SELECT id FROM service_operations WHERE state='pending' "
        "AND action IN ('publish','disable_publication','delete_publication',"
        "'rollback_publication') "
        "ORDER BY created_at,id LIMIT 1"
    ).fetchone()
    if pending:
        op = services_repo.operation(conn, pending[0])
        await apply(conn, policy, settings, transport, now, op)
        if services_repo.operation(conn, op["id"])["state"] == "pending":
            return
    # Retirar siempre expiradas, incluso si otra intención falló. No resucitar al reiniciar.
    expired = [
        p
        for p in services_repo.publications(conn)
        if p["enabled"] and p["expires_at"] and p["expires_at"] <= iso(now)
    ]
    # Revalidar política local y reparar deriva del bloque reservado, sin tocar rutas ajenas.
    for p in services_repo.publications(conn):
        try:
            validate_publication(policy, p)
        except GatewayError:
            if p["enabled"] and p not in expired:
                expired.append(p)
    if await apply(conn, policy, settings, transport, now) is None:
        with transaction(conn):
            for p in expired:
                services_repo.save_publication(
                    conn, p["publication_id"], {**p, "enabled": False}, None, now
                )
                auth_repo.audit(
                    conn,
                    now,
                    "system",
                    "expire_publication"
                    if p["expires_at"] and p["expires_at"] <= iso(now)
                    else "policy_disable_publication",
                    p["publication_id"],
                    result="succeeded",
                )


async def run(conn, policy, settings, lock, transport=None):
    while True:
        try:
            async with lock:
                await reconcile(conn, policy, settings, transport)
        except Exception as exc:
            logger.warning("gateway_reconcile_failed", extra={"error_type": type(exc).__name__})
        await asyncio.sleep(15)


async def verify(policy, value, transport=None, clock=utc_now):
    """Sondeo del upstream autorizado, distinto de validación de ruta/TLS público."""
    from dyndnsarr_hub.services.caddy import validate_publication

    target = validate_publication(policy, value)
    address = target.host
    code, state = None, "reachable"
    try:
        async with httpx.AsyncClient(trust_env=False, transport=transport) as http:
            await request(
                http,
                "GET",
                f"{target.scheme}://{address}:{value['port']}/",
                networks=[address + "/32"],
                parse_json=False,
            )
    except (httpx.HTTPError, OSError, ValueError):
        code, state = "upstream_unavailable", "unavailable"
    return {
        "state": state,
        "observed_at": iso(clock()),
        "code": code,
        "scope": "upstream",
        "public_https": "unknown",
    }
