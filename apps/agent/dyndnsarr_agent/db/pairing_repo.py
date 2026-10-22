"""Estado del emparejamiento con el Hub (ADR-0013): la CA obtenida y el último intento."""

import sqlite3
from datetime import datetime

from dyndnsarr_agent.db.ddns_repo import iso, set_state

_ERROR_CODE = "pairing_last_error_code"
_ATTEMPT_AT = "pairing_last_attempt_at"


def hub_ca(conn: sqlite3.Connection) -> tuple[str, str] | None:
    """`(hub_ca_pem, paired_at)`, o `None` si el Agent no está emparejado."""
    row = conn.execute("SELECT hub_ca_pem, paired_at FROM hub_pairing WHERE id = 1").fetchone()
    return (row[0], row[1]) if row else None


def save_paired(conn: sqlite3.Connection, hub_ca_pem: str, now: datetime) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO hub_pairing (id, hub_ca_pem, paired_at) VALUES (1, ?, ?)",
        (hub_ca_pem, iso(now)),
    )
    conn.execute("DELETE FROM app_state WHERE key = ?", (_ERROR_CODE,))
    set_state(conn, _ATTEMPT_AT, iso(now), now)


def save_failure(conn: sqlite3.Connection, code: str, now: datetime) -> None:
    set_state(conn, _ERROR_CODE, code, now)
    set_state(conn, _ATTEMPT_AT, iso(now), now)


def last_attempt(conn: sqlite3.Connection) -> tuple[str | None, str | None]:
    """`(código del último fallo, instante del último intento)`."""
    values = dict(
        conn.execute(
            "SELECT key, value FROM app_state WHERE key IN (?, ?)", (_ERROR_CODE, _ATTEMPT_AT)
        ).fetchall()
    )
    return values.get(_ERROR_CODE), values.get(_ATTEMPT_AT)


def reset(conn: sqlite3.Connection) -> bool:
    """Olvida la CA del Hub y el último intento. Devuelve si estaba emparejado."""
    cursor = conn.execute("DELETE FROM hub_pairing")
    conn.execute("DELETE FROM app_state WHERE key IN (?, ?)", (_ERROR_CODE, _ATTEMPT_AT))
    return cursor.rowcount > 0
