import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

EVENT_TYPES = frozenset(
    {"agent_started", "ip_changed", "ip_uncertain", "dns_verified", "dns_mismatch"}
)


def iso(moment: datetime) -> str:
    """Instante en UTC ISO 8601 con sufijo Z (orden lexicográfico = orden cronológico)."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Event:
    sequence: int
    event_id: str
    type: str
    occurred_at: str
    record_name: str | None
    payload: dict[str, Any]


@dataclass(frozen=True)
class RecordState:
    record_name: str
    verification: str
    since: str


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_state(conn: sqlite3.Connection, key: str, value: str, now: datetime) -> None:
    conn.execute(
        "INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, iso(now)),
    )


def insert_ip_observation(
    conn: sqlite3.Connection, at: datetime, source: str, ip: str | None, error_code: str | None
) -> None:
    conn.execute(
        "INSERT INTO public_ip_observations (observed_at, source, ip, error_code) "
        "VALUES (?, ?, ?, ?)",
        (iso(at), source, ip, error_code),
    )


def insert_dns_check(
    conn: sqlite3.Connection,
    at: datetime,
    record_name: str,
    resolver: str,
    ip: str | None,
    error_code: str | None,
) -> None:
    conn.execute(
        "INSERT INTO dns_checks (observed_at, record_name, resolver, ip, error_code) "
        "VALUES (?, ?, ?, ?, ?)",
        (iso(at), record_name, resolver, ip, error_code),
    )


def get_record_state(conn: sqlite3.Connection, record_name: str) -> RecordState | None:
    row = conn.execute(
        "SELECT record_name, verification, since FROM record_state WHERE record_name = ?",
        (record_name,),
    ).fetchone()
    return RecordState(*row) if row else None


def set_record_state(
    conn: sqlite3.Connection, record_name: str, verification: str, since: datetime
) -> None:
    conn.execute(
        "INSERT INTO record_state (record_name, verification, since) VALUES (?, ?, ?) "
        "ON CONFLICT (record_name) DO UPDATE SET "
        "verification = excluded.verification, since = excluded.since",
        (record_name, verification, iso(since)),
    )


def insert_event(
    conn: sqlite3.Connection,
    type: str,
    occurred_at: datetime,
    record_name: str | None,
    payload: dict[str, Any],
) -> Event:
    if type not in EVENT_TYPES:
        raise ValueError(f"tipo de evento desconocido: {type}")
    event_id = str(uuid.uuid4())
    cursor = conn.execute(
        "INSERT INTO events (event_id, type, occurred_at, record_name, payload) "
        "VALUES (?, ?, ?, ?, ?)",
        (event_id, type, iso(occurred_at), record_name, json.dumps(payload, sort_keys=True)),
    )
    return Event(cursor.lastrowid, event_id, type, iso(occurred_at), record_name, payload)


def list_events(conn: sqlite3.Connection, limit: int, newest_first: bool = True) -> list[Event]:
    order = "DESC" if newest_first else "ASC"
    rows = conn.execute(
        "SELECT sequence, event_id, type, occurred_at, record_name, payload FROM events "
        f"ORDER BY sequence {order} LIMIT ?",
        (limit,),
    ).fetchall()
    return [Event(*row[:5], json.loads(row[5])) for row in rows]


def purge(
    conn: sqlite3.Connection, now: datetime, samples_days: int, events_days: int
) -> dict[str, int]:
    """Borra lo más antiguo que la retención. Devuelve cuántas filas se borraron por tabla."""
    samples_limit = iso(now - timedelta(days=samples_days))
    events_limit = iso(now - timedelta(days=events_days))
    deleted = {}
    for table, column, limit in (
        ("public_ip_observations", "observed_at", samples_limit),
        ("dns_checks", "observed_at", samples_limit),
        ("events", "occurred_at", events_limit),
    ):
        # Tabla y columna son constantes de este módulo, nunca entrada externa.
        deleted[table] = conn.execute(f"DELETE FROM {table} WHERE {column} < ?", (limit,)).rowcount
    return deleted
