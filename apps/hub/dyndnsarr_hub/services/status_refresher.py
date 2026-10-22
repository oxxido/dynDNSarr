"""Refresco periódico del estado de los Agents para la vista pública (ADR-0007, D1 y D5).

La petición web nunca llama al Agent: esta tarea consulta `GET /internal/v1/status`, guarda la
proyección sin la IP completa y una muestra del historial. La red va antes de la transacción: no
hay un `await` con la base de datos abierta.
"""

import asyncio
import logging
import sqlite3
import ssl
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path

import httpx

from dyndnsarr_hub.clock import parse_instant, utc_now
from dyndnsarr_hub.db import agents_repo, private_repo, public_repo
from dyndnsarr_hub.public.state import AgentProjection, RecordProjection, derive_state
from dyndnsarr_hub.services.agent_client import AgentClient, build_http_client
from dyndnsarr_shared.control import AgentStatus
from dyndnsarr_shared.db import transaction

logger = logging.getLogger(__name__)


def _optional(text: str | None) -> datetime | None:
    return parse_instant(text) if text else None


def project(agent_id: str, status: AgentStatus, now: datetime) -> AgentProjection:
    """Lo público del estado de un Agent: `public_ip` se descarta aquí (D2)."""
    return AgentProjection(
        agent_id=agent_id,
        refreshed_at=now,
        ip_status=status.ip_status,
        public_ip_confirmed_at=_optional(status.public_ip_confirmed_at),
        last_check_at=_optional(status.last_check_at),
        records=tuple(
            RecordProjection(
                r.name,
                r.verification,
                _optional(r.since),
                r.failures,
                _optional(r.next_attempt_at),
                r.last_error_code,
            )
            for r in status.records
        ),
    )


async def refresh_once(
    conn: sqlite3.Connection,
    http: httpx.AsyncClient | None,
    refresh_seconds: int,
    clock: Callable[[], datetime] = utc_now,
    agent_ids: set[str] | None = None,
) -> dict[str, str | None]:
    """Una pasada por los agentes con API configurada. Devuelve `{agent_id: código de fallo}`
    (`None` si respondió). Un fallo no avanza `refreshed_at` y deja una muestra `unknown`.
    Sin `http` (no se pudo construir el cliente TLS) todos fallan con `tls_config`."""
    results: list[tuple[str, AgentStatus | None, str | None]] = []
    details, received = {}, {}
    for agent_id, url, token in agents_repo.api_targets(conn):
        if agent_ids is not None and agent_id not in agent_ids:
            continue
        if http is None:
            results.append((agent_id, None, "tls_config"))
            continue
        try:
            client = AgentClient(http, url, token)
        except ValueError:
            results.append((agent_id, None, "invalid_url"))
            continue
        reply = await client.status()
        if reply.value is None:
            results.append((agent_id, None, reply.error_code or reply.api_error or "no_status"))
        else:
            results.append((agent_id, reply.value, None))
            received[agent_id] = clock()
            cursor = private_repo.snapshot(conn, agent_id).get("diagnostics_cursor")
            try:
                details[agent_id] = (await client.config(), await client.diagnostics(cursor))
            except Exception as exc:
                logger.warning("private_refresh_error", extra={"error_type": type(exc).__name__})
    now = clock()
    with transaction(conn):
        for agent_id, status, _code in results:
            if status is None:
                public_repo.add_sample(conn, agent_id, now, "unknown")
                private_repo.save_snapshot(conn, agent_id, "status", None, _code, now)
                private_repo.mark_gap(conn, agent_id, now)
                continue
            projection = project(agent_id, status, received[agent_id])
            public_repo.save_agent_status(conn, projection)
            public_repo.add_sample(
                conn,
                agent_id,
                now,
                derive_state(projection, now, refresh_seconds, public_repo.monitoring_policy(conn)),
            )
        public_repo.purge_samples(conn, now)
    # Una proyección privada inválida no revierte la actualización pública.
    with transaction(conn):
        for agent_id, status, _code in results:
            if status is None:
                continue
            private_repo.save_snapshot(conn, agent_id, "status", status, None, received[agent_id])
            if agent_id in details:
                for kind, reply in zip(("config", "diagnostics"), details[agent_id], strict=True):
                    private_repo.save_snapshot(
                        conn, agent_id, kind, reply.value, reply.error_code or reply.api_error, now
                    )
            private_repo.add_state_samples(
                conn, project(agent_id, status, received[agent_id]), now, refresh_seconds
            )
        private_repo.purge(conn, now)
    return {agent_id: code for agent_id, _, code in results}


async def run_forever(
    conn: sqlite3.Connection,
    ca_file: Path | None,
    refresh_seconds: int,
    transport: httpx.AsyncBaseTransport | None = None,
    clock: Callable[[], datetime] = utc_now,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Refresca cada `refresh_seconds`. Un error inesperado no mata la tarea; los fallos de un
    agente se registran al empezar y al terminar la racha, solo con códigos seguros.

    El cliente se construye en cada pasada: así recoge las CA de los Agents recién emparejados
    (ADR-0013) y una CA global que aparezca más tarde."""
    failing: set[str] = set()
    while True:
        try:
            http: httpx.AsyncClient | None = None
            try:
                http = build_http_client(ca_file, transport, agents_repo.pinned_cas(conn))
            except (OSError, ssl.SSLError):
                # La CA aún no está (o es ilegible): se reintenta en la próxima pasada.
                http = None
            try:
                outcome = await refresh_once(conn, http, refresh_seconds, clock)
            finally:
                if http is not None:
                    await http.aclose()
            for agent_id, code in outcome.items():
                if code is not None and agent_id not in failing:
                    failing.add(agent_id)
                    logger.warning(
                        "status_refresh_failed", extra={"agent_id": agent_id, "code": code}
                    )
                elif code is None and agent_id in failing:
                    failing.discard(agent_id)
                    logger.info("status_refresh_recovered", extra={"agent_id": agent_id})
        except Exception as exc:
            # Solo el tipo: el texto de una excepción podría llevar URLs o datos.
            logger.error("status_refresh_error", extra={"error_type": type(exc).__name__})
        await sleep(refresh_seconds)
