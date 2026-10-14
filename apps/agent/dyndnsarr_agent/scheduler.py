import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import httpx

from dyndnsarr_agent.ddns.config import DdnsConfig
from dyndnsarr_agent.ddns.cycle import run_cycle
from dyndnsarr_agent.dns.resolver import DnsObserver

logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(UTC)


async def run_forever(
    conn: sqlite3.Connection,
    config: DdnsConfig,
    client: httpx.AsyncClient,
    observer: DnsObserver,
    clock: Callable[[], datetime] = utc_now,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Ciclo inmediato y luego uno cada `check_interval_seconds`; un ciclo fallido no lo detiene."""
    while True:
        try:
            await run_cycle(conn, config, client, observer, clock)
        except Exception as exc:
            # Solo el tipo: el texto de una excepción de red puede llevar la URL (en 2b, la clave).
            logger.error("cycle_failed", extra={"error_type": type(exc).__name__})
        await sleep(config.check_interval_seconds)
