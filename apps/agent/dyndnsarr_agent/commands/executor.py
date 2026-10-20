"""Comandos del Hub (ADR-0006, D6): persistidos antes de ejecutar, en serie y una sola vez."""

import asyncio
import json
import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from dyndnsarr_agent.db import control_repo, ddns_repo
from dyndnsarr_agent.db.control_repo import StoredCommand
from dyndnsarr_agent.ddns import manual
from dyndnsarr_agent.ddns.active_config import ConfigHolder
from dyndnsarr_agent.scheduler import try_lock, utc_now
from dyndnsarr_shared.control import COMMAND_TYPES, CommandRequest
from dyndnsarr_shared.db import transaction

logger = logging.getLogger(__name__)

QUEUE_SIZE = 16
# El Hub rechaza payloads de más de 4 KiB: un resultado más grande viaja resumido.
MAX_EVENT_RESULT_BYTES = 3000


class CommandError(Exception):
    """La petición no se acepta. `status`/`code` son los de la respuesta HTTP."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _fit(result: dict[str, Any]) -> dict[str, Any]:
    if len(json.dumps(result).encode()) <= MAX_EVENT_RESULT_BYTES:
        return result
    return {"code": result.get("code"), "truncated": True}


class CommandExecutor:
    def __init__(
        self,
        conn: sqlite3.Connection,
        holder: ConfigHolder,
        client: httpx.AsyncClient,
        lock_path: Path,
        clock: Callable[[], datetime] = utc_now,
        queue_size: int = QUEUE_SIZE,
        integrations=None,
    ) -> None:
        self._conn = conn
        self._holder = holder
        self._client = client
        self._lock_path = lock_path
        self._clock = clock
        self._queue: asyncio.Queue[str] = asyncio.Queue(queue_size)
        self._integrations = integrations

    def recover(self) -> None:
        """Al arrancar, un comando `received`/`running` no puede seguir en marcha: queda
        `failed/interrupted` y no se reejecuta (como WoL, §5): el Hub emite uno nuevo."""
        for command in control_repo.unfinished_commands(self._conn):
            self._finish(command, "failed", {"code": "interrupted"})
            logger.warning("command_interrupted", extra={"type": command.type})

    def submit(self, request: CommandRequest) -> tuple[StoredCommand, bool]:
        """Valida y persiste. Devuelve `(comando, nuevo)`; un `command_id` ya conocido devuelve su
        estado sin reejecutar. Lanza `CommandError`."""
        if request.type not in COMMAND_TYPES:
            raise CommandError(422, "unsupported_command", "Comando no soportado")
        if request.parameters or (request.resource_id and request.type != "wake_device"):
            raise CommandError(
                422, "invalid_command", "Este comando no admite parámetros ni resource_id"
            )
        existing = control_repo.get_command(self._conn, request.command_id)
        if existing is not None:
            if existing.type != request.type or existing.resource_id != request.resource_id:
                raise CommandError(409, "command_conflict", "Ese command_id es de otro comando")
            return existing, False
        if request.type == "wake_device" and (
            self._integrations is None
            or not any(
                d.id == request.resource_id and d.manual for d in self._integrations.config.devices
            )
        ):
            raise CommandError(422, "device_not_allowed", "Dispositivo no autorizado")
        now = self._clock()
        expired = ddns_repo.parse_iso(request.expires_at) <= now
        if not expired and self._queue.full():
            raise CommandError(429, "queue_full", "Hay demasiados comandos en cola")
        with transaction(self._conn):
            control_repo.insert_command(
                self._conn,
                request.command_id,
                request.type,
                request.requested_by,
                now,
                request.expires_at,
                resource_id=request.resource_id,
            )
        command = control_repo.get_command(self._conn, request.command_id)
        if expired:
            return self._finish(command, "expired", None), False
        self._queue.put_nowait(request.command_id)
        return command, True

    async def join(self) -> None:
        """Espera a que la cola esté vacía y el último comando termine."""
        await self._queue.join()

    async def run(self) -> None:
        while True:
            command_id = await self._queue.get()
            try:
                await self._execute(command_id)
            except Exception as exc:
                # Solo el tipo: el texto de una excepción puede llevar rutas o URLs.
                logger.error("command_failed", extra={"error_type": type(exc).__name__})
                command = control_repo.get_command(self._conn, command_id)
                if command is not None and command.status in ("received", "running"):
                    self._finish(command, "failed", {"code": "internal_error"})
            finally:
                self._queue.task_done()

    async def _execute(self, command_id: str) -> None:
        command = control_repo.get_command(self._conn, command_id)
        if command is None or command.status != "received":
            return
        if ddns_repo.parse_iso(command.expires_at) <= self._clock():
            self._finish(command, "expired", None)
            return
        with transaction(self._conn):
            control_repo.set_command_running(self._conn, command_id)
        if command.type == "wake_device":
            result = await self._integrations.wake(command.resource_id, command.command_id)
            status = "failed" if result.get("code") else "succeeded"
        elif command.type == "check_dns":
            status, result = await self._check_dns()
        else:
            status, result = await self._sync_ddns()
        self._finish(command, status, result)

    async def _check_dns(self) -> tuple[str, dict[str, Any]]:
        holder = self._holder
        check = await manual.check_dns(self._conn, holder.config, holder.observer, self._clock)
        return "succeeded", {
            "records": {r.name: r.verdict or "no_reference" for r in check.records}
        }

    async def _sync_ddns(self) -> tuple[str, dict[str, Any]]:
        config, providers, observer = (
            self._holder.config,
            self._holder.providers,
            self._holder.observer,
        )
        if not manual.managed_records(config):
            return "succeeded", {"ip_status": None, "records": {}, "updates": {}}
        with try_lock(self._lock_path) as taken:
            if not taken:
                return "failed", {"code": "busy"}
            cycle = await manual.sync_pass(
                self._conn, config, self._client, observer, providers, self._clock
            )
        result: dict[str, Any] = {
            "ip_status": cycle.ip_status,
            "records": cycle.records,
            "updates": {n: u.error_code or "accepted" for n, u in cycle.updates.items()},
        }
        if cycle.ip_status != "confirmed":
            return "failed", {"code": "ip_uncertain", **result}
        if any(not u.accepted for u in cycle.updates.values()):
            return "failed", {"code": "provider_failed", **result}
        return "succeeded", result

    def _finish(
        self, command: StoredCommand, status: str, result: dict[str, Any] | None
    ) -> StoredCommand:
        """Estado final y evento `command_completed` en una sola transacción."""
        at = self._clock()
        with transaction(self._conn):
            control_repo.finish_command(self._conn, command.command_id, status, at, result)
            ddns_repo.insert_event(
                self._conn,
                "command_completed",
                at,
                None,
                {
                    "command_id": command.command_id,
                    "type": command.type,
                    "status": status,
                    "result": _fit(result) if result is not None else None,
                },
            )
        logger.info("command_completed", extra={"type": command.type, "status": status})
        return control_repo.get_command(self._conn, command.command_id)
