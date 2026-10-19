import asyncio
import logging
import random
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import datetime

from dyndnsarr_agent.db import ddns_repo
from dyndnsarr_agent.outbox.client import DeliveryResult, HubClient
from dyndnsarr_agent.scheduler import utc_now
from dyndnsarr_shared.db import transaction

logger = logging.getLogger(__name__)

BATCH_SIZE = 50
IDLE_SECONDS = 30.0
BACKOFF_START_SECONDS = 30.0
BACKOFF_MAX_SECONDS = 900.0
JITTER = 0.2


def backoff_seconds(failures: int) -> float:
    """30 s, 60 s, 120 s… hasta 900 s (sin jitter). `failures` ≥ 1."""
    return min(BACKOFF_MAX_SECONDS, BACKOFF_START_SECONDS * 2 ** (failures - 1))


def random_jitter() -> float:
    return random.uniform(1 - JITTER, 1 + JITTER)


async def deliver_pending(
    conn: sqlite3.Connection, client: HubClient, clock: Callable[[], datetime] = utc_now
) -> str | None:
    """Envía lotes por `sequence` hasta vaciar los pendientes. Devuelve el código de fallo o `None`.

    Solo se marca un evento tras su ACK (o rechazo explícito). Nunca hay una transacción abierta
    durante el `await` de red.
    """
    while True:
        events = ddns_repo.pending_events(conn, BATCH_SIZE)
        if not events:
            return None
        result = await client.send(events)
        now = clock()
        if result.error_code is None and not result.acked and not result.rejected:
            # El Hub no reconoció ni uno: sin avance no se puede seguir sin un bucle infinito.
            result = DeliveryResult(error_code="bad_response")
        if result.error_code is not None:
            with transaction(conn):
                ddns_repo.set_state(conn, "outbox_last_error_code", result.error_code, now)
                ddns_repo.set_state(conn, "outbox_last_error_at", ddns_repo.iso(now), now)
            return result.error_code
        with transaction(conn):
            ddns_repo.mark_delivered(conn, result.acked, now)
            ddns_repo.mark_rejected(conn, result.rejected)
            ddns_repo.set_state(conn, "outbox_last_success_at", ddns_repo.iso(now), now)
        if result.rejected:
            logger.warning("outbox_events_rejected", extra={"count": len(result.rejected)})


async def run_forever(
    conn: sqlite3.Connection,
    client: HubClient,
    clock: Callable[[], datetime] = utc_now,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    jitter: Callable[[], float] = random_jitter,
) -> None:
    """Cada 30 s mira la BD local (sin red si no hay pendientes) y entrega. Un fallo del Hub no
    afecta al DDNS: reintenta con backoff y jitter y solo registra el inicio y el fin de la racha.
    """
    failures = 0
    while True:
        wait = IDLE_SECONDS
        try:
            code = await deliver_pending(conn, client, clock)
        except Exception as exc:
            # Solo el tipo: el texto de una excepción podría llevar rutas o datos.
            code = "internal_error"
            logger.error("outbox_failed", extra={"error_type": type(exc).__name__})
        if code is None:
            if failures:
                logger.info("outbox_recovered", extra={"failures": failures})
            failures = 0
        else:
            failures += 1
            if failures == 1 and code != "internal_error":
                level = logging.ERROR if code == "auth_failed" else logging.WARNING
                logger.log(level, "outbox_delivery_failed", extra={"code": code})
            wait = backoff_seconds(failures) * jitter()
        await sleep(wait)
