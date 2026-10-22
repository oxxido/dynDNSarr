"""Persistencia del emparejamiento por código (ADR-0013): códigos consumidos y bloqueo."""

import sqlite3
from datetime import datetime

from dyndnsarr_hub.clock import iso


def consumed(conn: sqlite3.Connection, code_hash: str) -> tuple[str, str] | None:
    """`(agent_id, content_hash)` con el que se consumió el código, o `None`."""
    row = conn.execute(
        "SELECT agent_id, content_hash FROM pairing WHERE code_hash = ?", (code_hash,)
    ).fetchone()
    return (row[0], row[1]) if row else None


def consume(
    conn: sqlite3.Connection, code_hash: str, agent_id: str, content_hash: str, now: datetime
) -> None:
    conn.execute(
        "INSERT INTO pairing (code_hash, agent_id, content_hash, consumed_at) VALUES (?, ?, ?, ?)",
        (code_hash, agent_id, content_hash, iso(now)),
    )


def throttle(conn: sqlite3.Connection) -> tuple[str, int, str | None] | None:
    return conn.execute(
        "SELECT window_at, failures, blocked_until FROM pairing_throttle WHERE id = 1"
    ).fetchone()


def save_throttle(
    conn: sqlite3.Connection, window: str, failures: int, blocked_until: str | None
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO pairing_throttle VALUES (1, ?, ?, ?)",
        (window, failures, blocked_until),
    )
