"""Reglas de estado de la vista pública y agregación del historial (ADR-0007, D3 y D5).

Funciones puras, sin HTTP ni SQL. Las constantes son provisionales (el slice 5 las hará
configurables, A4): no están verificadas con datos reales.
"""

import ipaddress
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from dyndnsarr_hub.public.dto import BUCKET_SECONDS, HISTORY_BUCKETS, State

FRESH_OBSERVATION = timedelta(minutes=15)
CRITICAL_MISMATCH = timedelta(hours=1)
STALE_REFRESH_FACTOR = 3

# De peor a mejor: un agente sin datos impide afirmar «operativo» (D3).
_SEVERITY: tuple[State, ...] = ("critical", "degraded", "unknown", "healthy")
_KNOWN_SEVERITY: tuple[State, ...] = ("critical", "degraded", "healthy")


@dataclass(frozen=True)
class RecordProjection:
    name: str
    verification: str
    since: datetime | None
    failures: int
    next_attempt_at: datetime | None
    last_error_code: str | None


@dataclass(frozen=True)
class AgentProjection:
    """Lo último observado de un agente, sin la IP completa."""

    agent_id: str
    refreshed_at: datetime
    ip_status: str | None
    public_ip_confirmed_at: datetime | None
    last_check_at: datetime | None
    records: tuple[RecordProjection, ...]


def ip_suffix(ip: object) -> str | None:
    """Los dos últimos dígitos de una IPv4 pública (`83.45.12.134` → `34`, `.7` → `7`).

    Cualquier otra cosa (no IPv4, privada, reservada) devuelve `None`.
    """
    if not isinstance(ip, str):
        return None
    try:
        address = ipaddress.IPv4Address(ip)
    except ValueError:
        return None
    if not address.is_global or address.is_multicast:
        return None
    return str(address).rsplit(".", 1)[1][-2:]


def _stale(moment: datetime | None, now: datetime, limit: timedelta) -> bool:
    return moment is None or now - moment > limit


def derive_state(projection: AgentProjection | None, now: datetime, refresh_seconds: int) -> State:
    """Estado de un agente. Se deriva al leer: si el refresco deja de correr, la proyección
    envejece y pasa a `unknown` sin que nadie la toque."""
    if projection is None or not projection.records:
        return "unknown"
    if now - projection.refreshed_at > timedelta(seconds=STALE_REFRESH_FACTOR * refresh_seconds):
        return "unknown"
    if _stale(projection.last_check_at, now, FRESH_OBSERVATION) or _stale(
        projection.public_ip_confirmed_at, now, FRESH_OBSERVATION
    ):
        return "unknown"
    for record in projection.records:
        persistent_mismatch = (
            record.verification == "mismatch"
            and record.since is not None
            and now - record.since > CRITICAL_MISMATCH
        )
        definitive_error = record.failures > 0 and record.next_attempt_at is None
        if persistent_mismatch or definitive_error:
            return "critical"
    if projection.ip_status == "uncertain" or any(
        r.verification != "verified" or r.failures > 0 for r in projection.records
    ):
        return "degraded"
    return "healthy"


def worst(states: Iterable[State]) -> State:
    """El peor de varios estados (`critical > degraded > unknown > healthy`); sin agentes,
    `unknown`."""
    present = set(states)
    return next((s for s in _SEVERITY if s in present), "unknown")


def history_start(now: datetime) -> datetime:
    return now - timedelta(seconds=BUCKET_SECONDS * HISTORY_BUCKETS)


def build_buckets(
    samples: Iterable[tuple[datetime, State]], now: datetime
) -> tuple[list[State], float]:
    """84 barras de 2 h que terminan en `now`. Cada barra es el peor estado **conocido** de sus
    muestras y `unknown` si no hay ninguna conocida. Devuelve también la fracción con datos."""
    start = history_start(now)
    found: list[set[State]] = [set() for _ in range(HISTORY_BUCKETS)]
    for sampled_at, state in samples:
        # Una muestra tomada justo en `now` cae en la última barra.
        index = min(
            int((sampled_at - start).total_seconds() // BUCKET_SECONDS), HISTORY_BUCKETS - 1
        )
        if state != "unknown" and index >= 0 and sampled_at <= now:
            found[index].add(state)
    buckets: list[State] = [
        next((s for s in _KNOWN_SEVERITY if s in known), "unknown") for known in found
    ]
    coverage = sum(1 for known in found if known) / HISTORY_BUCKETS
    return buckets, coverage
