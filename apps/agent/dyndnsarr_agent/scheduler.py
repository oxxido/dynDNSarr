import asyncio
import contextlib
import fcntl
import logging
import os
import sqlite3
from collections.abc import Awaitable, Callable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path

import httpx

from dyndnsarr_agent.ddns.config import DdnsConfig
from dyndnsarr_agent.ddns.cycle import run_cycle
from dyndnsarr_agent.dns.resolver import DnsObserver
from dyndnsarr_agent.providers.base import DnsProvider

logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(UTC)


@contextlib.contextmanager
def try_lock(path: Path) -> Iterator[bool]:
    """`flock` no bloqueante sobre `path`: rinde si se tomó. Se libera al salir (o al morir)."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            taken = False
        else:
            taken = True
        yield taken
    finally:
        os.close(fd)  # cerrar el descriptor libera el bloqueo


async def run_forever(
    conn: sqlite3.Connection,
    config: DdnsConfig,
    client: httpx.AsyncClient,
    observer: DnsObserver,
    clock: Callable[[], datetime] = utc_now,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    providers: Mapping[str, DnsProvider] | None = None,
    lock_path: Path | None = None,
) -> None:
    """Ciclo inmediato y luego uno cada `check_interval_seconds`, o antes si hay algo que vence.

    Un ciclo fallido no detiene el bucle. Con `lock_path`, cada ciclo toma el bloqueo que usa
    `sync-ddns` y, si lo encuentra tomado, se salta.
    """
    while True:
        wait = float(config.check_interval_seconds)
        try:
            with try_lock(lock_path) if lock_path else contextlib.nullcontext(True) as taken:
                if taken:
                    result = await run_cycle(
                        conn, config, client, observer, clock, providers, sleep=sleep
                    )
                    if result.next_wake_seconds is not None:
                        wait = min(wait, result.next_wake_seconds)
                else:
                    logger.info("cycle_skipped", extra={"reason": "sync_in_progress"})
        except Exception as exc:
            # Solo el tipo: el texto de una excepción de red puede llevar la URL (y la clave).
            logger.error("cycle_failed", extra={"error_type": type(exc).__name__})
        await sleep(wait)
