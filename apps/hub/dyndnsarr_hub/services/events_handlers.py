"""Efecto de los eventos del Agent en el estado del Hub (ADR-0006, D3).

Se llama desde la ingestión, en su misma transacción y solo con eventos nuevos: un reenvío no se
procesa dos veces. Un evento mal formado se ignora (queda guardado): no debe bloquear el outbox.
"""

import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime

from dyndnsarr_hub.db import control_repo
from dyndnsarr_shared.control import COMMAND_STATUSES
from dyndnsarr_shared.events import AgentEvent

logger = logging.getLogger(__name__)


def _config_result(conn: sqlite3.Connection, event: AgentEvent, now: datetime) -> None:
    payload = event.payload
    version, content_hash = payload.get("version"), payload.get("hash")
    if not isinstance(version, int) or not isinstance(content_hash, str):
        return
    applied = event.type == "config_applied"
    code = payload.get("code")
    control_repo.record_config_result(
        conn,
        event.agent_id,
        version,
        content_hash,
        "applied" if applied else "rejected",
        code if isinstance(code, str) else None,
        now,
    )


def _config_reset(conn: sqlite3.Connection, event: AgentEvent, now: datetime) -> None:
    control_repo.record_config_reset(conn, event.agent_id, now)


def _command_completed(conn: sqlite3.Connection, event: AgentEvent, now: datetime) -> None:
    payload = event.payload
    command_id, status, result = (
        payload.get("command_id"),
        payload.get("status"),
        payload.get("result"),
    )
    if not isinstance(command_id, str) or status not in COMMAND_STATUSES:
        return
    control_repo.update_command_from_agent(
        conn, command_id, event.agent_id, status, result if isinstance(result, dict) else None
    )


_HANDLERS: dict[str, Callable[[sqlite3.Connection, AgentEvent, datetime], None]] = {
    "config_applied": _config_result,
    "config_rejected": _config_result,
    "config_reset": _config_reset,
    "command_completed": _command_completed,
}


def handle_new_events(conn: sqlite3.Connection, events: list[AgentEvent], now: datetime) -> None:
    for event in events:
        handler = _HANDLERS.get(event.type)
        if handler is None:
            continue
        try:
            handler(conn, event, now)
        except Exception as exc:
            # Solo el tipo: un fallo aquí no debe rechazar el lote ni bloquear al Agent.
            logger.error(
                "event_handler_failed",
                extra={"event_type": event.type, "error_type": type(exc).__name__},
            )
