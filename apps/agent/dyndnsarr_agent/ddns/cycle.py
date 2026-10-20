import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from dyndnsarr_agent.db import ddns_repo
from dyndnsarr_agent.db.ddns_repo import RecordState, iso, parse_iso
from dyndnsarr_agent.ddns.config import DdnsConfig, Record
from dyndnsarr_agent.ddns.ip_sources import Consensus, SourceResult, detect_public_ip
from dyndnsarr_agent.ddns.reconcile import Decision, after_update, decide, starts_failure_streak
from dyndnsarr_agent.dns.resolver import DnsObserver, ResolverAnswer
from dyndnsarr_agent.providers.base import DnsProvider, UpdateResult
from dyndnsarr_shared.db import transaction

logger = logging.getLogger(__name__)

# El API de DonDominio devolvió 429 a la 6.ª llamada en ~2 s: una pausa entre llamadas del ciclo.
PROVIDER_PAUSE_SECONDS = 2.0


@dataclass(frozen=True)
class CycleResult:
    ip_status: str  # "confirmed" | "uncertain"
    public_ip: str | None
    # registro -> verified | mismatch | unknown | pending_verification (estado tras el ciclo)
    records: dict[str, str]
    # Registros `managed` a los que se llamó en este ciclo y qué respondió el proveedor.
    updates: dict[str, UpdateResult] = field(default_factory=dict)
    # Segundos hasta el próximo vencimiento (verificación o reintento), o None si no hay ninguno.
    next_wake_seconds: float | None = None


@dataclass(frozen=True)
class _ManagedOutcome:
    record: Record
    previous: RecordState
    decision: Decision
    result: UpdateResult | None
    called_at: datetime | None


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
        if previous_status == "uncertain":
            ddns_repo.insert_event(conn, "ip_recovered", at, None, {})
        if outcome.ip != previous_ip:
            ddns_repo.insert_event(
                conn, "ip_changed", at, None, {"previous": previous_ip, "ip": outcome.ip}
            )
            ddns_repo.set_state(conn, "public_ip", outcome.ip, at)
        ddns_repo.set_state(conn, "public_ip_confirmed_at", ddns_repo.iso(at), at)
    elif previous_status != "uncertain":
        ddns_repo.insert_event(conn, "ip_uncertain", at, None, {"reason": outcome.reason})
    ddns_repo.set_state(conn, "ip_status", outcome.status, at)


def _mismatch_payload(public_ip: str, answers: list[ResolverAnswer]) -> dict:
    observed = {a.resolver: ",".join(a.ips) or a.error_code for a in answers}
    return {"expected": public_ip, "observed": observed}


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
        ddns_repo.insert_event(
            conn, "dns_mismatch", at, name, _mismatch_payload(public_ip, answers)
        )
    return verification


async def _update_managed(
    conn: sqlite3.Connection,
    config: DdnsConfig,
    providers: Mapping[str, DnsProvider],
    records: list[Record],
    answers: dict[str, list[ResolverAnswer]],
    public_ip: str | None,
    at: datetime,
    clock: Callable[[], datetime],
    manual: bool,
    sleep: Callable[[float], Awaitable[None]],
) -> list[_ManagedOutcome]:
    """Decide por registro y llama al proveedor (red) sin escribir en la base de datos."""
    outcomes = []
    calls = 0
    for record in records:
        previous = ddns_repo.get_record_state(conn, record.name) or RecordState(
            record.name, "unknown", iso(at)
        )
        decision = decide(
            public_ip,
            answers[record.name],
            previous,
            at,
            config.update.verify_timeout_seconds,
            manual,
        )
        result = called_at = None
        if decision.call:
            if calls:
                await sleep(PROVIDER_PAUSE_SECONDS)
            result = await providers[record.provider].update(record.name, public_ip)
            called_at = clock()
            calls += 1
        outcomes.append(_ManagedOutcome(record, previous, decision, result, called_at))
    return outcomes


def _apply_managed(
    conn: sqlite3.Connection,
    at: datetime,
    outcome: _ManagedOutcome,
    public_ip: str | None,
    answers: list[ResolverAnswer],
) -> RecordState:
    name, decision, result = outcome.record.name, outcome.decision, outcome.result
    state = decision.state
    if decision.verification_timed_out_after is not None:
        ddns_repo.insert_event(
            conn,
            "dns_verification_failed",
            at,
            name,
            {"ip": public_ip, "waited_seconds": decision.verification_timed_out_after},
        )
    if result is not None:
        when = outcome.called_at
        ddns_repo.insert_update_attempt(
            conn,
            when,
            name,
            public_ip,
            result.accepted,
            result.changed if result.accepted else None,
            result.error_code,
            None if result.accepted else result.retryable,
        )
        new_state = after_update(state, public_ip, result, when)
        if result.accepted:
            payload = {"ip": public_ip, "changed": result.changed}
            ddns_repo.insert_event(conn, "dns_update_succeeded", when, name, payload)
        elif starts_failure_streak(state, public_ip, result):
            payload = {
                "code": result.error_code,
                "retryable": result.retryable,
                "attempt": new_state.failures,
            }
            ddns_repo.insert_event(conn, "dns_update_failed", when, name, payload)
        state = new_state
    if state.verification != outcome.previous.verification:
        if state.verification == "verified":
            ddns_repo.insert_event(conn, "dns_verified", at, name, {"ip": public_ip})
        elif state.verification == "mismatch":
            ddns_repo.insert_event(
                conn, "dns_mismatch", at, name, _mismatch_payload(public_ip, answers)
            )
    ddns_repo.save_record_state(conn, state)
    return state


def _next_wake_seconds(
    config: DdnsConfig, states: list[RecordState], now: datetime
) -> float | None:
    """Cuándo conviene volver a mirar: verificación en curso o reintento de un fallo."""
    waits = []
    for state in states:
        if state.verification == "pending_verification":
            waits.append(float(config.update.verify_retry_seconds))
        if state.next_attempt_at is not None:
            remaining = (parse_iso(state.next_attempt_at) - now).total_seconds()
            if remaining > 0:  # un vencimiento ya pasado no se reintenta si no hay nada que hacer
                waits.append(max(remaining, 1.0))
    return min(waits) if waits else None


async def run_cycle(
    conn: sqlite3.Connection,
    config: DdnsConfig,
    client: httpx.AsyncClient,
    observer: DnsObserver,
    clock: Callable[[], datetime],
    providers: Mapping[str, DnsProvider] | None = None,
    manual: bool = False,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> CycleResult:
    """Un ciclo: IP -> DNS por registro -> decidir -> llamar al proveedor -> eventos -> purga.

    Primero se observa (red), después se llama al proveedor (como máximo una vez por registro
    `managed`) y por último se escribe todo en una sola transacción: si algo falla antes, la base
    queda como estaba. Los `monitored` no reciben el proveedor: no pueden escribir.
    """
    providers = providers or {}
    at = clock()
    enabled = [r for r in config.records if r.enabled]
    (ip_results, outcome), *dns_answers = await asyncio.gather(
        detect_public_ip(client, config.ip_sources.urls),
        *(observer.lookup(r.name) for r in enabled),
    )
    answers = {r.name: a for r, a in zip(enabled, dns_answers, strict=True)}
    public_ip = outcome.ip if outcome.status == "confirmed" else None
    managed = await _update_managed(
        conn,
        config,
        providers,
        [r for r in enabled if r.mode == "managed"],
        answers,
        public_ip,
        at,
        clock,
        manual,
        sleep,
    )
    records: dict[str, str] = {}
    states: list[RecordState] = []
    updates = {o.record.name: o.result for o in managed if o.result is not None}
    with transaction(conn):
        record_ip_samples(conn, at, ip_results)
        _apply_ip(conn, at, outcome)
        for record in enabled:
            record_dns_samples(conn, at, record.name, answers[record.name])
        for o in managed:
            state = _apply_managed(conn, at, o, public_ip, answers[o.record.name])
            records[o.record.name] = state.verification
            states.append(state)
        for record in enabled:
            if record.mode == "managed":
                continue
            if public_ip is not None:
                records[record.name] = _apply_record(
                    conn, at, record.name, public_ip, answers[record.name]
                )
            else:
                state = ddns_repo.get_record_state(conn, record.name)
                records[record.name] = state.verification if state else "unknown"
        purged = ddns_repo.purge(
            conn, at, config.retention.samples_days, config.retention.events_days
        )
    records = {r.name: records[r.name] for r in enabled}
    logger.info(
        "cycle_completed",
        extra={
            "ip_status": outcome.status,
            "records": records,
            "updates": {n: r.error_code or "accepted" for n, r in updates.items()},
            "purged": purged,
        },
    )
    return CycleResult(
        outcome.status,
        outcome.ip,
        records,
        updates,
        _next_wake_seconds(config, states, clock()),
    )
