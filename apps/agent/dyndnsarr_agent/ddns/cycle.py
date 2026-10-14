import asyncio
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import httpx

from dyndnsarr_agent.db import ddns_repo
from dyndnsarr_agent.ddns.config import DdnsConfig
from dyndnsarr_agent.ddns.ip_sources import Consensus, SourceResult, detect_public_ip
from dyndnsarr_agent.dns.resolver import DnsObserver, ResolverAnswer
from dyndnsarr_shared.db import transaction

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CycleResult:
    ip_status: str  # "confirmed" | "uncertain"
    public_ip: str | None
    records: dict[str, str]  # registro -> verified | mismatch | unknown (estado tras el ciclo)


def classify(public_ip: str, answers: list[ResolverAnswer]) -> str:
    """Estado de un registro `monitored` frente a la IP pública confirmada.

    Solo cuentan los resolvers que responden con registros A; si ninguno lo hace, `unknown`.
    """
    responding = [a for a in answers if a.error_code is None and a.ips]
    if not responding:
        return "unknown"
    if all(a.ips == (public_ip,) for a in responding):
        return "verified"
    return "mismatch"


def record_ip_samples(conn: sqlite3.Connection, at: datetime, results: list[SourceResult]) -> None:
    for r in results:
        ddns_repo.insert_ip_observation(conn, at, r.source, r.ip, r.error_code)


def record_dns_samples(
    conn: sqlite3.Connection, at: datetime, name: str, answers: list[ResolverAnswer]
) -> None:
    for a in answers:
        ddns_repo.insert_dns_check(
            conn, at, name, a.resolver, ",".join(a.ips) or None, a.error_code
        )


def _apply_ip(conn: sqlite3.Connection, at: datetime, outcome: Consensus) -> None:
    previous_ip = ddns_repo.get_state(conn, "public_ip")
    previous_status = ddns_repo.get_state(conn, "ip_status")
    if outcome.status == "confirmed":
        if outcome.ip != previous_ip:
            ddns_repo.insert_event(
                conn, "ip_changed", at, None, {"previous": previous_ip, "ip": outcome.ip}
            )
            ddns_repo.set_state(conn, "public_ip", outcome.ip, at)
        ddns_repo.set_state(conn, "public_ip_confirmed_at", ddns_repo.iso(at), at)
    elif previous_status != "uncertain":
        ddns_repo.insert_event(conn, "ip_uncertain", at, None, {"reason": outcome.reason})
    ddns_repo.set_state(conn, "ip_status", outcome.status, at)


def _apply_record(
    conn: sqlite3.Connection, at: datetime, name: str, public_ip: str, answers: list[ResolverAnswer]
) -> str:
    verification = classify(public_ip, answers)
    previous = ddns_repo.get_record_state(conn, name)
    if previous is not None and previous.verification == verification:
        return verification
    ddns_repo.set_record_state(conn, name, verification, at)
    if verification == "verified":
        ddns_repo.insert_event(conn, "dns_verified", at, name, {"ip": public_ip})
    elif verification == "mismatch":
        observed = {a.resolver: ",".join(a.ips) or a.error_code for a in answers}
        ddns_repo.insert_event(
            conn, "dns_mismatch", at, name, {"expected": public_ip, "observed": observed}
        )
    return verification


async def run_cycle(
    conn: sqlite3.Connection,
    config: DdnsConfig,
    client: httpx.AsyncClient,
    observer: DnsObserver,
    clock: Callable[[], datetime],
) -> CycleResult:
    """Un ciclo de observación: IP -> DNS por registro -> clasificar -> eventos -> purga.

    Primero se observa (red) y después se escribe todo en una sola transacción: si algo falla
    antes, la base queda como estaba.
    """
    at = clock()
    names = [r.name for r in config.records if r.enabled]
    (ip_results, outcome), *dns_answers = await asyncio.gather(
        detect_public_ip(client, config.ip_sources.urls),
        *(observer.lookup(name) for name in names),
    )
    records: dict[str, str] = {}
    with transaction(conn):
        record_ip_samples(conn, at, ip_results)
        _apply_ip(conn, at, outcome)
        for name, answers in zip(names, dns_answers, strict=True):
            record_dns_samples(conn, at, name, answers)
            if outcome.status == "confirmed":
                records[name] = _apply_record(conn, at, name, outcome.ip, answers)
            else:
                state = ddns_repo.get_record_state(conn, name)
                records[name] = state.verification if state else "unknown"
        purged = ddns_repo.purge(
            conn, at, config.retention.samples_days, config.retention.events_days
        )
    logger.info(
        "cycle_completed",
        extra={"ip_status": outcome.status, "records": records, "purged": purged},
    )
    return CycleResult(outcome.status, outcome.ip, records)
