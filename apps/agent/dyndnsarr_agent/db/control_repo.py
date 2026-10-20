import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from dyndnsarr_agent.db.ddns_repo import iso


@dataclass(frozen=True)
class StoredVersion:
    version: int
    content_hash: str
    content: dict[str, Any]
    applied_at: str


@dataclass(frozen=True)
class StoredCommand:
    command_id: str
    type: str
    requested_by: str
    received_at: str
    expires_at: str
    status: str
    finished_at: str | None
    result: dict[str, Any] | None
    resource_id: str | None = None


_VERSION_COLUMNS = "version, content_hash, content, applied_at"
_COMMAND_COLUMNS = (
    "command_id, type, requested_by, received_at, expires_at, status, finished_at, result, "
    "resource_id"
)


def insert_version(
    conn: sqlite3.Connection, version: int, content_hash: str, content_json: str, at: datetime
) -> None:
    conn.execute(
        f"INSERT INTO configuration_versions ({_VERSION_COLUMNS}) VALUES (?, ?, ?, ?)",
        (version, content_hash, content_json, iso(at)),
    )


def latest_version(conn: sqlite3.Connection) -> StoredVersion | None:
    row = conn.execute(
        f"SELECT {_VERSION_COLUMNS} FROM configuration_versions ORDER BY version DESC LIMIT 1"
    ).fetchone()
    return StoredVersion(row[0], row[1], json.loads(row[2]), row[3]) if row else None


def delete_versions(conn: sqlite3.Connection) -> int:
    return conn.execute("DELETE FROM configuration_versions").rowcount


def _command(row: tuple) -> StoredCommand:
    return StoredCommand(*row[:7], json.loads(row[7]) if row[7] else None, row[8])


def insert_command(
    conn: sqlite3.Connection,
    command_id: str,
    type: str,
    requested_by: str,
    received_at: datetime,
    expires_at: str,
    status: str = "received",
    resource_id: str | None = None,
) -> None:
    conn.execute(
        f"INSERT INTO commands ({_COMMAND_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?)",
        (command_id, type, requested_by, iso(received_at), expires_at, status, resource_id),
    )


def get_command(conn: sqlite3.Connection, command_id: str) -> StoredCommand | None:
    row = conn.execute(
        f"SELECT {_COMMAND_COLUMNS} FROM commands WHERE command_id = ?", (command_id,)
    ).fetchone()
    return _command(row) if row else None


def set_command_running(conn: sqlite3.Connection, command_id: str) -> None:
    conn.execute("UPDATE commands SET status = 'running' WHERE command_id = ?", (command_id,))


def finish_command(
    conn: sqlite3.Connection,
    command_id: str,
    status: str,
    at: datetime,
    result: dict[str, Any] | None,
) -> None:
    conn.execute(
        "UPDATE commands SET status = ?, finished_at = ?, result = ? WHERE command_id = ?",
        (
            status,
            iso(at),
            json.dumps(result, sort_keys=True) if result is not None else None,
            command_id,
        ),
    )


def unfinished_commands(conn: sqlite3.Connection) -> list[StoredCommand]:
    """Comandos `received` o `running`: al arrancar, ninguno puede seguir en marcha."""
    rows = conn.execute(
        f"SELECT {_COMMAND_COLUMNS} FROM commands WHERE status IN ('received', 'running') "
        "ORDER BY received_at, command_id"
    ).fetchall()
    return [_command(r) for r in rows]
