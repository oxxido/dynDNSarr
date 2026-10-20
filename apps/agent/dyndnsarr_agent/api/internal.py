"""API de control Hub → Agent (`/internal/v1`, ADR-0006). Solo existe con token configurado."""

import logging
import secrets
import sqlite3
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from dyndnsarr_agent import __version__
from dyndnsarr_agent.commands.executor import CommandError
from dyndnsarr_agent.db import ddns_repo
from dyndnsarr_agent.db.control_repo import StoredCommand, get_command
from dyndnsarr_agent.ddns.apply_config import apply_config
from dyndnsarr_agent.scheduler import utc_now
from dyndnsarr_shared.control import (
    AgentHealth,
    AgentStatus,
    CommandRequest,
    CommandStatus,
    ConfigPut,
    HubLinkStatus,
    RecordStatus,
)
from dyndnsarr_shared.events import ErrorResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/v1")

MAX_CONFIG_BYTES = 256 * 1024
MAX_COMMAND_BYTES = 16 * 1024


@router.get("/integrations", response_model=None)
async def integrations(request: Request):
    if (denied := _denied(request)) is not None:
        return denied
    from dyndnsarr_agent.integrations.worker import read_snapshot

    snapshot = read_snapshot(request.app.state.db)
    if request.app.state.integration_error:
        snapshot.pihole, snapshot.error_code = "unavailable", "integration_failed"
    return JSONResponse(
        snapshot.model_dump(mode="json"),
        headers={"Cache-Control": "no-store"},
    )


class _BodyTooLargeError(Exception):
    pass


def _error(status: int, type_: str, code: str, message: str, headers: dict[str, str] | None = None):
    body = ErrorResponse(
        type=type_,
        code=code,
        message_safe=message,
        timestamp=ddns_repo.iso(utc_now()),
        request_id=uuid.uuid4().hex,
    )
    return JSONResponse(body.model_dump(), status_code=status, headers=headers)


def _denied(request: Request) -> JSONResponse | None:
    """401 (siempre el mismo cuerpo) si falta el token o no coincide; `None` si todo va bien.

    Va antes de leer el cuerpo y de tocar nada. La comparación es en tiempo constante.
    """
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    expected = request.app.state.settings.api_token.get_secret_value()
    if scheme.lower() == "bearer" and secrets.compare_digest(
        token.strip().encode(), expected.encode()
    ):
        return None
    logger.warning("internal_auth_failed")
    return _error(
        401, "auth_error", "unauthorized", "Credenciales no válidas", {"WWW-Authenticate": "Bearer"}
    )


async def _read_body(request: Request, limit: int) -> bytes:
    """Lee el cuerpo con tope también sin `Content-Length` (o con uno falso)."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise _BodyTooLargeError
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise _BodyTooLargeError
        chunks.append(chunk)
    return b"".join(chunks)


def _agent_id(request: Request) -> str | None:
    hub = request.app.state.holder.config.hub
    return hub.agent_id if hub else None


def _command_status(command: StoredCommand) -> CommandStatus:
    return CommandStatus(
        command_id=command.command_id,
        type=command.type,
        status=command.status,
        received_at=command.received_at,
        finished_at=command.finished_at,
        result=command.result,
    )


@router.get("/health", response_model=None)
async def health(request: Request):
    if (denied := _denied(request)) is not None:
        return denied
    db: sqlite3.Connection = request.app.state.db
    status = "ok"
    try:
        db.execute("SELECT 1 FROM app_state LIMIT 1")
        last_dns = db.execute("SELECT max(observed_at) FROM dns_checks").fetchone()[0]
    except sqlite3.Error:
        logger.error("internal_health: la base de datos no responde", exc_info=True)
        status, last_dns = "error", None
    body = AgentHealth(
        agent_id=_agent_id(request),
        version=__version__,
        status=status,
        time_utc=ddns_repo.iso(utc_now()),
        last_local_dns_check_at=last_dns,
    )
    return JSONResponse(body.model_dump(), status_code=200 if status == "ok" else 503)


@router.get("/status", response_model=None)
async def status(request: Request):
    if (denied := _denied(request)) is not None:
        return denied
    db: sqlite3.Connection = request.app.state.db
    holder = request.app.state.holder
    config = holder.config
    states = {s.record_name: s for s in ddns_repo.list_record_states(db)}
    records = []
    for record in config.records:
        if not record.enabled:
            continue
        state = states.get(record.name)
        records.append(
            RecordStatus(
                name=record.name,
                mode=record.mode,
                verification=state.verification if state else "unknown",
                since=state.since if state else None,
                failures=state.failures if state else 0,
                next_attempt_at=state.next_attempt_at if state else None,
                last_error_code=state.last_error_code if state else None,
            )
        )
    hub = None
    if config.hub is not None:
        hub = HubLinkStatus(
            pending_events=ddns_repo.count_pending(db),
            rejected_events=ddns_repo.count_rejected(db),
            last_success_at=ddns_repo.get_state(db, "outbox_last_success_at"),
            last_error_code=ddns_repo.get_state(db, "outbox_last_error_code"),
            last_error_at=ddns_repo.get_state(db, "outbox_last_error_at"),
        )
    return AgentStatus(
        agent_id=_agent_id(request),
        version=__version__,
        time_utc=ddns_repo.iso(utc_now()),
        config_version=holder.version,
        config_hash=holder.content_hash,
        ip_status=ddns_repo.get_state(db, "ip_status"),
        public_ip=ddns_repo.get_state(db, "public_ip"),
        public_ip_confirmed_at=ddns_repo.get_state(db, "public_ip_confirmed_at"),
        last_check_at=db.execute("SELECT max(observed_at) FROM public_ip_observations").fetchone()[
            0
        ],
        records=records,
        hub=hub,
    )


@router.put("/config", response_model=None)
async def put_config(request: Request):
    if (denied := _denied(request)) is not None:
        return denied
    try:
        body = await _read_body(request, MAX_CONFIG_BYTES)
    except _BodyTooLargeError:
        return _error(413, "request_error", "body_too_large", "El cuerpo supera el límite")
    try:
        put = ConfigPut.model_validate_json(body)
    except ValidationError:
        return _error(
            400, "request_error", "invalid_request", "El cuerpo no tiene la forma esperada"
        )
    state = request.app.state
    return apply_config(state.db, state.holder, state.settings, put)


@router.post("/commands", response_model=None)
async def post_command(request: Request):
    if (denied := _denied(request)) is not None:
        return denied
    try:
        body = await _read_body(request, MAX_COMMAND_BYTES)
    except _BodyTooLargeError:
        return _error(413, "request_error", "body_too_large", "El cuerpo supera el límite")
    try:
        command_request = CommandRequest.model_validate_json(body)
    except ValidationError:
        return _error(
            400, "request_error", "invalid_request", "El cuerpo no tiene la forma esperada"
        )
    try:
        command, is_new = request.app.state.executor.submit(command_request)
    except CommandError as exc:
        return _error(exc.status, "command_error", exc.code, exc.message)
    return JSONResponse(_command_status(command).model_dump(), status_code=202 if is_new else 200)


@router.get("/commands/{command_id}", response_model=None)
async def command_status(command_id: str, request: Request):
    if (denied := _denied(request)) is not None:
        return denied
    command = get_command(request.app.state.db, command_id)
    if command is None:
        return _error(404, "request_error", "not_found", "Comando desconocido")
    return _command_status(command)


@router.get("/config", response_model=None)
async def get_config(request: Request):
    if (denied := _denied(request)) is not None:
        return denied
    from dyndnsarr_shared.control import EffectiveConfig

    holder = request.app.state.holder
    config = holder.config
    content = {
        "check_interval_seconds": config.check_interval_seconds,
        "ip_sources": config.ip_sources.model_dump(),
        "dns": {"resolvers": [r.label for r in config.dns.resolvers]},
        "retention": config.retention.model_dump(),
        "update": config.update.model_dump(),
        "records": [r.model_dump() for r in config.records],
    }
    return EffectiveConfig(version=holder.version, hash=holder.content_hash, content=content)


@router.get("/diagnostics", response_model=None)
async def get_diagnostics(request: Request):
    if (denied := _denied(request)) is not None:
        return denied
    from dyndnsarr_agent.db.diagnostics_repo import diagnostics

    params = request.query_params
    name, cursor = params.get("record"), params.get("cursor")
    limit_text = params.get("limit", "100")
    if (
        not limit_text.isdigit()
        or len(limit_text) > 3
        or not 1 <= int(limit_text) <= 100
        or (cursor and len(cursor) > 1024)
    ):
        return _error(400, "request_error", "invalid_query", "Paginación no válida")
    names = {r.name for r in request.app.state.holder.config.records}
    if name is not None and name not in names:
        return _error(404, "request_error", "not_found", "Registro desconocido")
    try:
        return diagnostics(request.app.state.db, name, int(limit_text), cursor, record_names=names)
    except ValueError:
        return _error(400, "request_error", "invalid_cursor", "Cursor no válido")
