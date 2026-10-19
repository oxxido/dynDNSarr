import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from dyndnsarr_hub.clock import iso
from dyndnsarr_shared.control import FINAL_COMMAND_STATUSES, canonical_json, config_hash


@dataclass(frozen=True)
class DesiredVersion:
    agent_id: str
    version: int
    content_hash: str
    content: dict[str, Any]
    created_at: str


@dataclass(frozen=True)
class ObservedState:
    agent_id: str
    config_version: int
    config_hash: str | None
    last_result: str | None
    last_code: str | None
    last_attempt_version: int | None
    last_attempt_at: str | None


@dataclass(frozen=True)
class HubCommand:
    command_id: str
    agent_id: str
    type: str
    requested_by: str
    created_at: str
    expires_at: str
    status: str
    result: dict[str, Any] | None


_DESIRED = "agent_id, version, content_hash, content, created_at"
_OBSERVED = (
    "agent_id, config_version, config_hash, last_result, last_code, "
    "last_attempt_version, last_attempt_at"
)
_COMMAND = "command_id, agent_id, type, requested_by, created_at, expires_at, status, result"


# --- configuración deseada ---


def add_desired_version(
    conn: sqlite3.Connection, agent_id: str, content: dict[str, Any], now: datetime
) -> DesiredVersion:
    """Crea la versión siguiente (monótona por agente). La abre en transacción quien llama."""
    version = conn.execute(
        "SELECT COALESCE(max(version), 0) + 1 FROM desired_config_versions WHERE agent_id = ?",
        (agent_id,),
    ).fetchone()[0]
    content_hash = config_hash(content)
    conn.execute(
        f"INSERT INTO desired_config_versions ({_DESIRED}) VALUES (?, ?, ?, ?, ?)",
        (agent_id, version, content_hash, canonical_json(content), iso(now)),
    )
    return DesiredVersion(agent_id, version, content_hash, content, iso(now))


def latest_desired(conn: sqlite3.Connection, agent_id: str) -> DesiredVersion | None:
    row = conn.execute(
        f"SELECT {_DESIRED} FROM desired_config_versions WHERE agent_id = ? "
        "ORDER BY version DESC LIMIT 1",
        (agent_id,),
    ).fetchone()
    return DesiredVersion(*row[:3], json.loads(row[3]), row[4]) if row else None


# --- estado observado ---


def get_observed(conn: sqlite3.Connection, agent_id: str) -> ObservedState | None:
    row = conn.execute(
        f"SELECT {_OBSERVED} FROM observed_agent_state WHERE agent_id = ?", (agent_id,)
    ).fetchone()
    return ObservedState(*row) if row else None


def record_config_result(
    conn: sqlite3.Connection,
    agent_id: str,
    version: int,
    content_hash: str,
    result: str,
    code: str | None,
    now: datetime,
) -> None:
    """Anota un intento de configuración, venga de la respuesta del `PUT` o de un evento.

    Es idempotente y no retrocede: una versión `applied` menor que la observada no la sustituye y
    un intento de una versión menor que el último no pisa al último.
    """
    state = get_observed(conn, agent_id) or ObservedState(agent_id, 0, None, None, None, None, None)
    version_now, hash_now = state.config_version, state.config_hash
    if result == "applied" and version >= version_now:
        version_now, hash_now = version, content_hash
    attempt = (
        state.last_result,
        state.last_code,
        state.last_attempt_version,
        state.last_attempt_at,
    )
    if state.last_attempt_version is None or version >= state.last_attempt_version:
        attempt = (result, code, version, iso(now))
    conn.execute(
        f"INSERT OR REPLACE INTO observed_agent_state ({_OBSERVED}) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (agent_id, version_now, hash_now, *attempt),
    )


def record_config_reset(conn: sqlite3.Connection, agent_id: str, now: datetime) -> None:
    """El Agent volvió a su TOML: la versión observada es 0 y se puede reenviar cualquiera."""
    conn.execute(
        f"INSERT OR REPLACE INTO observed_agent_state ({_OBSERVED}) "
        "VALUES (?, 0, NULL, 'reset', NULL, NULL, ?)",
        (agent_id, iso(now)),
    )


# --- comandos ---


def _command(row: tuple) -> HubCommand:
    return HubCommand(*row[:7], json.loads(row[7]) if row[7] else None)


def create_command(
    conn: sqlite3.Connection,
    command_id: str,
    agent_id: str,
    type: str,
    requested_by: str,
    now: datetime,
    expires_at: str,
) -> None:
    conn.execute(
        f"INSERT INTO commands ({_COMMAND}) VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL)",
        (command_id, agent_id, type, requested_by, iso(now), expires_at),
    )


def get_command(conn: sqlite3.Connection, command_id: str) -> HubCommand | None:
    row = conn.execute(
        f"SELECT {_COMMAND} FROM commands WHERE command_id = ?", (command_id,)
    ).fetchone()
    return _command(row) if row else None


def update_command_from_agent(
    conn: sqlite3.Connection,
    command_id: str,
    agent_id: str,
    agent_status: str,
    result: dict[str, Any] | None,
) -> None:
    """Refleja lo que dice el Agent (respuesta o evento). Un estado final no retrocede y un
    comando de otro agente se ignora. `received`/`running` del Agent son `sent` aquí."""
    status = agent_status if agent_status in FINAL_COMMAND_STATUSES else "sent"
    conn.execute(
        "UPDATE commands SET status = ?, result = COALESCE(?, result) "
        "WHERE command_id = ? AND agent_id = ? "
        "AND status NOT IN ('succeeded', 'failed', 'expired')",
        (
            status,
            json.dumps(result, sort_keys=True) if result is not None else None,
            command_id,
            agent_id,
        ),
    )
