import sqlite3
import uuid
from datetime import UTC, datetime

from dyndnsarr_shared.db import transaction


def record_boot(conn: sqlite3.Connection) -> tuple[str, int]:
    """Registra un arranque. Devuelve `(instance_id, boot_count)`.

    `instance_id` se genera la primera vez y se conserva; `boot_count` sube en cada arranque.
    """
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with transaction(conn):
        conn.execute(
            "INSERT INTO app_state (key, value, updated_at) VALUES ('instance_id', ?, ?) "
            "ON CONFLICT (key) DO NOTHING",
            (str(uuid.uuid4()), now),
        )
        conn.execute(
            "INSERT INTO app_state (key, value, updated_at) VALUES ('boot_count', '1', ?) "
            "ON CONFLICT (key) DO UPDATE SET "
            "value = CAST(value AS INTEGER) + 1, updated_at = excluded.updated_at",
            (now,),
        )
        state = dict(conn.execute("SELECT key, value FROM app_state"))
    return state["instance_id"], int(state["boot_count"])
