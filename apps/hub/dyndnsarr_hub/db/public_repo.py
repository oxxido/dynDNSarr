"""Proyección pública en SQLite (ADR-0007). No lee ni escribe `events`."""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from dyndnsarr_hub.clock import iso, parse_instant
from dyndnsarr_hub.public.dto import State
from dyndnsarr_hub.public.state import AgentProjection, RecordProjection

MAX_RENEWALS_PER_AGENT = 50
SAMPLE_RETENTION = timedelta(days=30)


@dataclass(frozen=True)
class PublicRenewal:
    agent_id: str
    record_name: str
    occurred_at: datetime
    ip_suffix: str
    verified: bool


def _optional(text: str | None) -> datetime | None:
    return parse_instant(text) if text else None


# --- renovaciones ---


def add_renewal(
    conn: sqlite3.Connection,
    agent_id: str,
    event_id: str,
    record_name: str,
    occurred_at: datetime,
    ip_suffix: str,
) -> None:
    """Idempotente por `(agent_id, event_id)`. Conserva las `MAX_RENEWALS_PER_AGENT` más recientes.
    La abre en transacción quien llama."""
    conn.execute(
        "INSERT OR IGNORE INTO public_renewals "
        "(agent_id, event_id, record_name, occurred_at, ip_suffix) VALUES (?, ?, ?, ?, ?)",
        (agent_id, event_id, record_name, iso(occurred_at), ip_suffix),
    )
    conn.execute(
        "DELETE FROM public_renewals WHERE agent_id = ? AND event_id NOT IN ("
        " SELECT event_id FROM public_renewals WHERE agent_id = ? "
        " ORDER BY occurred_at DESC, event_id DESC LIMIT ?)",
        (agent_id, agent_id, MAX_RENEWALS_PER_AGENT),
    )


def mark_verified(
    conn: sqlite3.Connection,
    agent_id: str,
    record_name: str,
    ip_suffix: str,
    verified_at: datetime,
) -> None:
    """Verifica las renovaciones pendientes del mismo registro y sufijo anteriores (o iguales) a
    `verified_at`: un `dns_verified` previo a su renovación no la verifica."""
    conn.execute(
        "UPDATE public_renewals SET verified_at = ? "
        "WHERE agent_id = ? AND record_name = ? AND ip_suffix = ? "
        "AND verified_at IS NULL AND occurred_at <= ?",
        (iso(verified_at), agent_id, record_name, ip_suffix, iso(verified_at)),
    )


def recent_renewals(conn: sqlite3.Connection, limit: int) -> list[PublicRenewal]:
    """Las más recientes de los agentes habilitados."""
    rows = conn.execute(
        "SELECT r.agent_id, r.record_name, r.occurred_at, r.ip_suffix, r.verified_at "
        "FROM public_renewals r JOIN agents a ON a.agent_id = r.agent_id "
        "WHERE a.disabled = 0 ORDER BY r.occurred_at DESC, r.event_id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [PublicRenewal(r[0], r[1], parse_instant(r[2]), r[3], r[4] is not None) for r in rows]


# --- estado de los agentes ---


def save_agent_status(conn: sqlite3.Connection, projection: AgentProjection) -> None:
    records = [
        {
            "name": r.name,
            "verification": r.verification,
            "since": iso(r.since) if r.since else None,
            "failures": r.failures,
            "next_attempt_at": iso(r.next_attempt_at) if r.next_attempt_at else None,
            "last_error_code": r.last_error_code,
        }
        for r in projection.records
    ]
    conn.execute(
        "INSERT OR REPLACE INTO public_agent_status "
        "(agent_id, refreshed_at, ip_status, public_ip_confirmed_at, last_check_at, records) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            projection.agent_id,
            iso(projection.refreshed_at),
            projection.ip_status,
            iso(projection.public_ip_confirmed_at) if projection.public_ip_confirmed_at else None,
            iso(projection.last_check_at) if projection.last_check_at else None,
            json.dumps(records, sort_keys=True),
        ),
    )


def enabled_agent_statuses(conn: sqlite3.Connection) -> list[AgentProjection | None]:
    """Una entrada por agente habilitado: su proyección, o `None` si aún no la tiene."""
    rows = conn.execute(
        "SELECT a.agent_id, s.refreshed_at, s.ip_status, s.public_ip_confirmed_at, "
        "s.last_check_at, s.records FROM agents a "
        "LEFT JOIN public_agent_status s ON s.agent_id = a.agent_id "
        "WHERE a.disabled = 0 ORDER BY a.agent_id"
    ).fetchall()
    statuses: list[AgentProjection | None] = []
    for agent_id, refreshed, ip_status, confirmed, last_check, records in rows:
        if refreshed is None:
            statuses.append(None)
            continue
        statuses.append(
            AgentProjection(
                agent_id,
                parse_instant(refreshed),
                ip_status,
                _optional(confirmed),
                _optional(last_check),
                tuple(
                    RecordProjection(
                        r["name"],
                        r["verification"],
                        _optional(r["since"]),
                        r["failures"],
                        _optional(r["next_attempt_at"]),
                        r["last_error_code"],
                    )
                    for r in json.loads(records)
                ),
            )
        )
    return statuses


# --- muestras del historial ---


def add_sample(conn: sqlite3.Connection, agent_id: str, sampled_at: datetime, state: State) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO public_status_samples (agent_id, sampled_at, state) "
        "VALUES (?, ?, ?)",
        (agent_id, iso(sampled_at), state),
    )


def purge_samples(conn: sqlite3.Connection, now: datetime) -> int:
    cursor = conn.execute(
        "DELETE FROM public_status_samples WHERE sampled_at < ?", (iso(now - SAMPLE_RETENTION),)
    )
    return cursor.rowcount


def samples_since(conn: sqlite3.Connection, since: datetime) -> list[tuple[datetime, State]]:
    """Muestras de los agentes habilitados desde `since`."""
    rows = conn.execute(
        "SELECT s.sampled_at, s.state FROM public_status_samples s "
        "JOIN agents a ON a.agent_id = s.agent_id "
        "WHERE a.disabled = 0 AND s.sampled_at >= ? ORDER BY s.sampled_at",
        (iso(since),),
    ).fetchall()
    return [(parse_instant(at), state) for at, state in rows]
