import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from dyndnsarr_hub.clock import iso
from dyndnsarr_shared.events import AgentEvent


@dataclass(frozen=True)
class StoredEvent:
    agent_id: str
    event_id: str
    sequence: int
    type: str
    occurred_at: str
    received_at: str
    record_name: str | None
    payload: dict[str, Any]


def insert_events(
    conn: sqlite3.Connection, events: list[AgentEvent], received_at: datetime
) -> list[AgentEvent]:
    """Guarda los eventos; uno ya recibido `(agent_id, event_id)` se ignora (reenvío).

    Devuelve los que eran nuevos.
    """
    new = []
    for e in events:
        receipt = conn.execute(
            "INSERT OR IGNORE INTO event_receipts VALUES (?,?,?)",
            (e.agent_id, e.event_id, iso(received_at)),
        )
        if receipt.rowcount == 0:
            continue
        cursor = conn.execute(
            "INSERT OR IGNORE INTO events "
            "(agent_id, event_id, sequence, type, occurred_at, received_at, record_name, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                e.agent_id,
                e.event_id,
                e.sequence,
                e.type,
                e.occurred_at,
                iso(received_at),
                e.record_name,
                json.dumps(e.payload, sort_keys=True),
            ),
        )
        if cursor.rowcount == 1:
            new.append(e)
    return new


def list_events(
    conn: sqlite3.Connection, agent_id: str | None, limit: int, newest_first: bool = True
) -> list[StoredEvent]:
    order = "DESC" if newest_first else "ASC"
    where, params = ("WHERE agent_id = ?", (agent_id,)) if agent_id else ("", ())
    rows = conn.execute(
        "SELECT agent_id, event_id, sequence, type, occurred_at, received_at, record_name, "
        f"payload FROM events {where} ORDER BY received_at {order}, sequence {order} LIMIT ?",
        (*params, limit),
    ).fetchall()
    return [StoredEvent(*r[:7], json.loads(r[7])) for r in rows]
