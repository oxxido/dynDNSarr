"""Pases manuales (CLI `check-dns`/`sync-ddns` y comandos del Hub): el mismo código para ambos."""

import asyncio
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime

import httpx

from dyndnsarr_agent.db import ddns_repo
from dyndnsarr_agent.ddns.config import DdnsConfig, Record
from dyndnsarr_agent.ddns.cycle import CycleResult, classify, record_dns_samples, run_cycle
from dyndnsarr_agent.dns.resolver import DnsObserver, ResolverAnswer
from dyndnsarr_agent.providers.base import DnsProvider
from dyndnsarr_shared.db import transaction


@dataclass(frozen=True)
class RecordCheck:
    name: str
    # None si aún no hay IP pública confirmada con la que comparar.
    verdict: str | None
    answers: list[ResolverAnswer]


@dataclass(frozen=True)
class DnsCheck:
    known_ip: str | None
    records: list[RecordCheck]


def managed_records(config: DdnsConfig) -> list[Record]:
    return [r for r in config.records if r.mode == "managed" and r.enabled]


async def check_dns(
    conn: sqlite3.Connection,
    config: DdnsConfig,
    observer: DnsObserver,
    clock: Callable[[], datetime],
) -> DnsCheck:
    """Consulta los resolvers por los registros habilitados y guarda las muestras. No toca el
    estado ni los eventos."""
    names = [r.name for r in config.records if r.enabled]
    all_answers = await asyncio.gather(*(observer.lookup(n) for n in names))
    known_ip = ddns_repo.get_state(conn, "public_ip")
    with transaction(conn):
        for name, answers in zip(names, all_answers, strict=True):
            record_dns_samples(conn, clock(), name, answers)
    return DnsCheck(
        known_ip,
        [
            RecordCheck(name, classify(known_ip, answers) if known_ip else None, answers)
            for name, answers in zip(names, all_answers, strict=True)
        ],
    )


async def sync_pass(
    conn: sqlite3.Connection,
    config: DdnsConfig,
    client: httpx.AsyncClient,
    observer: DnsObserver,
    providers: Mapping[str, DnsProvider],
    clock: Callable[[], datetime],
) -> CycleResult:
    """Un pase de los registros `managed`: como el ciclo, pero sin esperar el backoff.

    Quien llama toma antes el bloqueo (`try_lock`) y comprueba que haya registros `managed`.
    """
    config = config.model_copy(update={"records": managed_records(config)})
    return await run_cycle(conn, config, client, observer, clock, providers, manual=True)
